#!/usr/bin/env python3
"""Pump.fun graduation collector and post-graduation backtester.

Data sources:
  * Helius Enhanced Transactions for migration-address history
  * GeckoTerminal keyless API for pool discovery and OHLCV

This is research code, not a live trading system.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests


DATA_DIR = os.getenv("DATA_DIR", "data")
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
KNOWN_QUOTES = {WSOL, USDC, USDT}
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
HELIUS_BASE = "https://api-mainnet.helius-rpc.com"
SUPPORTED_DEX_HINTS = ("pump", "raydium")


def utc_iso(epoch: int | float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)) and not pd.isna(value):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


class ApiClient:
    def __init__(self, requests_per_minute: float, timeout: int = 30) -> None:
        self.session = requests.Session()
        self.timeout = timeout
        self.min_interval = 60.0 / max(requests_per_minute, 0.1)
        self.last_request = 0.0

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(7):
            wait = self.min_interval - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            response = self.session.get(url, params=params, timeout=self.timeout)
            self.last_request = time.monotonic()
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 6:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(60.0, 2**attempt + random.random())
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")


def candidate_mints(tx: dict[str, Any]) -> list[str]:
    """Extract non-quote SPL mints from a Helius enhanced transaction."""
    found: set[str] = set()
    for transfer in tx.get("tokenTransfers") or []:
        mint = transfer.get("mint")
        if mint and mint not in KNOWN_QUOTES:
            found.add(mint)
    for account in tx.get("accountData") or []:
        for change in account.get("tokenBalanceChanges") or []:
            mint = change.get("mint")
            if mint and mint not in KNOWN_QUOTES:
                found.add(mint)
    return sorted(found)


def collect_graduations(args: argparse.Namespace) -> None:
    api_key = args.helius_api_key or os.getenv("HELIUS_API_KEY")
    migration_address = args.migration_address or os.getenv("MIGRATION_ADDRESS")
    if not api_key:
        raise SystemExit("Missing Helius key. Set HELIUS_API_KEY or pass --helius-api-key.")
    if not migration_address:
        raise SystemExit(
            "Missing migration address. Verify the current address, then set MIGRATION_ADDRESS "
            "or pass --migration-address. It is intentionally not hard-coded."
        )

    client = ApiClient(args.requests_per_minute)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    before: str | None = None

    while len(rows) < args.count:
        params: dict[str, Any] = {
            "api-key": api_key,
            "limit": 100,
            "sort-order": "desc",
            "commitment": "finalized",
        }
        if before:
            params["before-signature"] = before
        if args.start_time:
            params["gte-time"] = parse_timestamp(args.start_time)
        if args.end_time:
            params["lte-time"] = parse_timestamp(args.end_time)

        url = f"{HELIUS_BASE}/v0/addresses/{migration_address}/transactions"
        batch = client.get_json(url, params=params)
        if not batch:
            break

        for tx in batch:
            signature = tx.get("signature") or ""
            timestamp = tx.get("timestamp") or tx.get("blockTime")
            if not timestamp:
                continue
            mints = candidate_mints(tx)
            status = "confirmed" if len(mints) == 1 else "needs_review"
            for mint in mints:
                key = (mint, signature)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "mint_address": mint,
                        "graduation_timestamp": utc_iso(parse_timestamp(timestamp)),
                        "tx_signature": signature,
                        "extraction_status": status,
                        "candidate_count_in_tx": len(mints),
                        "helius_source": tx.get("source"),
                        "helius_type": tx.get("type"),
                    }
                )
                if len(rows) >= args.count:
                    break
            if len(rows) >= args.count:
                break

        before = batch[-1].get("signature")
        if not before:
            break

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "mint_address",
        "graduation_timestamp",
        "tx_signature",
        "extraction_status",
        "candidate_count_in_tx",
        "helius_source",
        "helius_type",
    ]
    pd.DataFrame(rows, columns=columns).to_csv(out, index=False)
    confirmed = sum(row["extraction_status"] == "confirmed" for row in rows)
    print(f"Wrote {len(rows)} candidates ({confirmed} confirmed) to {out}")
    if confirmed < len(rows):
        print("Review needs_review rows against the migration transaction before pricing them.")


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class TradeResult:
    mint_address: str
    graduation_timestamp: str
    pool_address: str
    dex_id: str
    entry_timestamp: str
    entry_price: float
    exit_timestamp: str
    exit_price: float
    exit_reason: str
    gross_return: float
    net_return: float
    both_barriers_same_candle: bool
    moon_bag_fraction: float = 0.0
    moon_bag_price: float = 0.0  # price the kept fraction is valued/sold at (last candle, ~24h)
    scale_out_price: float = 0.0  # price at which the partial take-profit leg sold (0 = none)


class GeckoTerminal:
    def __init__(self, requests_per_minute: float) -> None:
        self.client = ApiClient(requests_per_minute)

    def select_pool(self, mint: str, graduation_ts: int) -> tuple[str, str]:
        url = f"{GECKO_BASE}/networks/solana/tokens/{mint}/pools"
        payload = self.client.get_json(
            url,
            params={"include": "base_token,quote_token,dex", "page": 1},
        )
        candidates: list[tuple[tuple[float, float, float], str, str]] = []
        for pool in payload.get("data") or []:
            attrs = pool.get("attributes") or {}
            rel = pool.get("relationships") or {}
            dex_id = (((rel.get("dex") or {}).get("data") or {}).get("id") or "").lower()
            if not any(hint in dex_id for hint in SUPPORTED_DEX_HINTS):
                continue
            created_raw = attrs.get("pool_created_at")
            if not created_raw:
                continue
            created = parse_timestamp(created_raw)
            delta = abs(created - graduation_ts)
            # A migration pool should be created close to the recorded migration.
            if delta > 6 * 3600:
                continue
            liquidity = float(attrs.get("reserve_in_usd") or 0)
            preferred = 0.0 if "pump" in dex_id else 1.0
            score = (float(delta), preferred, -liquidity)
            candidates.append((score, attrs.get("address") or "", dex_id))
        if not candidates:
            raise ValueError("No PumpSwap/Raydium pool created within 6h of graduation")
        _, address, dex_id = min(candidates, key=lambda item: item[0])
        if not address:
            raise ValueError("Selected pool has no address")
        return address, dex_id

    def ohlcv(
        self,
        pool: str,
        mint: str,
        timeframe: str,
        aggregate: int,
        before_timestamp: int,
        limit: int,
    ) -> list[Candle]:
        url = f"{GECKO_BASE}/networks/solana/pools/{pool}/ohlcv/{timeframe}"
        payload = self.client.get_json(
            url,
            params={
                "aggregate": aggregate,
                "before_timestamp": before_timestamp,
                "limit": min(limit, 1000),
                "currency": "usd",
                "token": mint,
                "include_empty_intervals": "true",
            },
        )
        raw = (((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
        return [Candle(int(x[0]), *map(float, x[1:6])) for x in raw]

    def minute_path(self, pool: str, mint: str, start: int, end: int) -> list[Candle]:
        all_candles: dict[int, Candle] = {}
        cursor = end + 120
        for _ in range(4):
            candles = self.ohlcv(pool, mint, "minute", 1, cursor, 1000)
            if not candles:
                break
            for candle in candles:
                if start - 120 <= candle.timestamp <= end + 120:
                    all_candles[candle.timestamp] = candle
            oldest = min(c.timestamp for c in candles)
            if oldest <= start - 60:
                break
            cursor = oldest
        return sorted(all_candles.values(), key=lambda c: c.timestamp)

    def entry_candles(self, pool: str, mint: str, target: int) -> list[Candle]:
        candles = self.ohlcv(pool, mint, "second", 30, target + 120, 8)
        return sorted(candles, key=lambda c: c.timestamp)


def price_at_or_after(candles: Iterable[Candle], target: int, tolerance: int) -> tuple[float, int]:
    ordered = sorted(candles, key=lambda c: c.timestamp)
    after = [c for c in ordered if c.timestamp >= target and c.timestamp - target <= tolerance]
    if after:
        candle = after[0]
        # GeckoTerminal timestamps identify the start of each OHLCV bucket.
        return candle.open, candle.timestamp
    before = [c for c in ordered if c.timestamp < target and target - c.timestamp <= tolerance]
    if before:
        candle = before[-1]
        return candle.close, candle.timestamp
    raise ValueError(f"No candle within {tolerance}s of {utc_iso(target)}")


def apply_costs(entry_price: float, exit_price: float, side_cost: float) -> tuple[float, float]:
    gross = exit_price / entry_price - 1.0
    net = (exit_price * (1.0 - side_cost)) / (entry_price * (1.0 + side_cost)) - 1.0
    return gross, net


def simulate_trade(
    mint: str,
    graduation_ts: int,
    pool: str,
    dex_id: str,
    entry_price: float,
    entry_ts: int,
    minute_candles: list[Candle],
    take_profit: float,
    stop_loss: float,
    time_stop_minutes: int,
    side_cost: float,
    trailing_stop: float = 0.0,
    moon_bag: float = 0.0,
    scale_out_at: float = 0.0,
    scale_out_fraction: float = 0.5,
) -> TradeResult:
    tp_price = entry_price * (1.0 + take_profit)
    sl_price = entry_price * (1.0 - stop_loss)
    deadline = entry_ts + time_stop_minutes * 60
    path = [c for c in minute_candles if entry_ts <= c.timestamp <= deadline]
    if not path:
        raise ValueError("No 1-minute price path after entry")

    exit_price = path[-1].close
    exit_ts = path[-1].timestamp
    reason = "time_stop"
    ambiguous = False
    peak = entry_price  # high-water mark of PRIOR candles only, so a candle's
    # own high never arms the trailing stop that its own low then triggers.
    scale_price = entry_price * (1.0 + scale_out_at) if scale_out_at > 0 else 0.0
    scale_out_price = 0.0
    for candle in path:
        trail_price = peak * (1.0 - trailing_stop) if trailing_stop > 0 else 0.0
        hit_tp = candle.high >= tp_price
        hit_sl = candle.low <= sl_price
        hit_trail = trailing_stop > 0 and candle.low <= trail_price
        if hit_tp and hit_sl:
            # Intraminute ordering is unknowable from OHLC. Use adverse ordering.
            exit_price, exit_ts, reason, ambiguous = sl_price, candle.timestamp, "sl_ambiguous", True
            break
        if hit_sl:
            exit_price, exit_ts, reason = sl_price, candle.timestamp, "stop_loss"
            break
        if hit_trail and trail_price > sl_price:
            exit_price, exit_ts, reason = trail_price, candle.timestamp, "trailing_stop"
            if hit_tp:
                ambiguous = True  # both barriers in one candle; adverse ordering again
            break
        if scale_price and not scale_out_price and candle.high >= scale_price:
            # Partial take-profit leg fills at its threshold; the remainder keeps running under
            # the same price thresholds (a reduced cost basis leaves them at the same token price).
            scale_out_price = scale_price
        if hit_tp:
            exit_price, exit_ts, reason = tp_price, candle.timestamp, "take_profit"
            break
        peak = max(peak, candle.high)

    # Moon bag: sell only (1-mb) at the primary exit, hold mb until the end of
    # the collected path (~24h) and sell there. Both sell legs pay side_cost, so
    # the blended effective exit price folds into the same cost formula.
    moon_bag_price = 0.0
    remainder_exit = exit_price
    if moon_bag > 0:
        moon_bag_price = sorted(minute_candles, key=lambda c: c.timestamp)[-1].close
        remainder_exit = exit_price * (1.0 - moon_bag) + moon_bag_price * moon_bag
    # Scale-out leg sold early at scale_out_price; the remaining fraction exits as above.
    # Proportional costs are identical per leg, so blending before apply_costs is exact.
    if scale_out_price:
        effective_exit = scale_out_price * scale_out_fraction + remainder_exit * (1.0 - scale_out_fraction)
    else:
        effective_exit = remainder_exit
    gross, net = apply_costs(entry_price, effective_exit, side_cost)
    return TradeResult(
        mint_address=mint,
        graduation_timestamp=utc_iso(graduation_ts),
        pool_address=pool,
        dex_id=dex_id,
        entry_timestamp=utc_iso(entry_ts),
        entry_price=entry_price,
        exit_timestamp=utc_iso(exit_ts),
        exit_price=exit_price,
        exit_reason=reason,
        gross_return=gross,
        net_return=net,
        both_barriers_same_candle=ambiguous,
        moon_bag_fraction=moon_bag,
        moon_bag_price=moon_bag_price,
        scale_out_price=scale_out_price,
    )


def snapshot_rows(
    mint: str,
    graduation_ts: int,
    pool: str,
    minute_path: list[Candle],
) -> list[dict[str, Any]]:
    offsets = {"migration": 0, "1m": 60, "5m": 300, "30m": 1800, "2h": 7200, "24h": 86400}
    rows = []
    for label, offset in offsets.items():
        target = graduation_ts + offset
        try:
            price, observed_ts = price_at_or_after(minute_path, target, tolerance=180)
        except ValueError:
            price, observed_ts = math.nan, 0
        rows.append(
            {
                "mint_address": mint,
                "pool_address": pool,
                "snapshot": label,
                "target_timestamp": utc_iso(target),
                "observed_timestamp": utc_iso(observed_ts) if observed_ts else None,
                "price_usd": price,
            }
        )
    return rows


def make_summary(results: pd.DataFrame, hold_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if results.empty or "net_return" not in results.columns:
        return {"trade_count": 0, "error": "No valid trades"}
    valid = results.dropna(subset=["net_return"]).copy()
    hold = pd.DataFrame(hold_rows)
    if valid.empty:
        return {"trade_count": 0, "error": "No valid trades"}
    total_profit = float(valid["net_return"].sum())
    top5 = valid[valid["net_return"] > 0].nlargest(5, "net_return")
    top5_profit = float(top5["net_return"].sum())
    without_top5 = valid.drop(top5.index)
    hold_valid = hold.dropna(subset=["net_return"])
    median_strategy = float(valid["net_return"].median())
    median_hold = float(hold_valid["net_return"].median()) if not hold_valid.empty else None
    mean_without_top5 = float(without_top5["net_return"].mean()) if not without_top5.empty else None
    paired = valid[["mint_address", "net_return"]].merge(
        hold_valid[["mint_address", "net_return"]], on="mint_address", suffixes=("_strategy", "_hold")
    ) if not hold_valid.empty else pd.DataFrame()
    return {
        "trade_count": int(len(valid)),
        "median_net_return": median_strategy,
        "mean_net_return": float(valid["net_return"].mean()),
        "win_rate": float((valid["net_return"] > 0).mean()),
        "total_net_return_sum": total_profit,
        "top_5_share_of_total_net_profit": top5_profit / total_profit if total_profit > 0 else None,
        "top_5_share_of_positive_profit": (
            top5_profit / float(valid.loc[valid["net_return"] > 0, "net_return"].sum())
            if float(valid.loc[valid["net_return"] > 0, "net_return"].sum()) > 0
            else None
        ),
        "median_without_top_5": float(without_top5["net_return"].median()) if not without_top5.empty else None,
        "mean_without_top_5": mean_without_top5,
        "total_net_return_sum_without_top_5": float(without_top5["net_return"].sum()) if not without_top5.empty else None,
        "hold_30m_median_net_return": median_hold,
        "tp_sl_median_minus_hold_30m": (
            median_strategy - median_hold
            if median_hold is not None
            else None
        ),
        "tp_sl_beats_hold_fraction": (
            float((paired["net_return_strategy"] > paired["net_return_hold"]).mean()) if not paired.empty else None
        ),
        "question_1_median_positive_after_costs": median_strategy > 0,
        "question_2_tp_sl_beats_hold_by_median": median_hold is not None and median_strategy > median_hold,
        "question_3_positive_after_deleting_top_5": mean_without_top5 is not None and mean_without_top5 > 0,
        "ambiguous_bar_count": int(valid["both_barriers_same_candle"].sum()),
    }


def run_backtest(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    frame = pd.read_csv(input_path)
    required = {"mint_address", "graduation_timestamp"}
    if not required.issubset(frame.columns):
        raise SystemExit(f"{input_path} must contain: {', '.join(sorted(required))}")
    if "extraction_status" in frame and not args.include_needs_review:
        frame = frame[frame["extraction_status"].fillna("confirmed") == "confirmed"]
    if args.limit:
        frame = frame.head(args.limit)

    out_dir = Path(args.output_dir)
    cache_dir = out_dir / "cache"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    gecko = GeckoTerminal(args.requests_per_minute)
    trades: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    hold_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for i, row in enumerate(frame.itertuples(index=False), start=1):
        mint = str(row.mint_address)
        graduation_ts = parse_timestamp(row.graduation_timestamp)
        cache_file = cache_dir / f"{mint}.json"
        try:
            if cache_file.exists() and not args.refresh:
                cached = json.loads(cache_file.read_text())
                pool, dex_id = cached["pool_address"], cached["dex_id"]
                minute_path = [Candle(**item) for item in cached["minute_path"]]
                entry_candles = [Candle(**item) for item in cached["entry_candles"]]
            else:
                pool, dex_id = gecko.select_pool(mint, graduation_ts)
                minute_path = gecko.minute_path(pool, mint, graduation_ts, graduation_ts + 86400)
                entry_candles = gecko.entry_candles(pool, mint, graduation_ts + args.entry_delay_seconds)
                cache_file.write_text(
                    json.dumps(
                        {
                            "pool_address": pool,
                            "dex_id": dex_id,
                            "minute_path": [asdict(c) for c in minute_path],
                            "entry_candles": [asdict(c) for c in entry_candles],
                        }
                    )
                )

            entry_target = graduation_ts + args.entry_delay_seconds
            entry_price, entry_ts = price_at_or_after(entry_candles, entry_target, tolerance=90)
            trade = simulate_trade(
                mint,
                graduation_ts,
                pool,
                dex_id,
                entry_price,
                entry_ts,
                minute_path,
                args.take_profit,
                args.stop_loss,
                args.time_stop_minutes,
                args.side_cost,
                args.trailing_stop,
                args.moon_bag,
                args.scale_out_at,
                args.scale_out_fraction,
            )
            trades.append(asdict(trade))
            snapshots.extend(snapshot_rows(mint, graduation_ts, pool, minute_path))

            hold_target = entry_ts + 30 * 60
            hold_exit, hold_ts = price_at_or_after(minute_path, hold_target, tolerance=180)
            hold_gross, hold_net = apply_costs(entry_price, hold_exit, args.side_cost)
            hold_rows.append(
                {
                    "mint_address": mint,
                    "exit_timestamp": utc_iso(hold_ts),
                    "gross_return": hold_gross,
                    "net_return": hold_net,
                }
            )
            print(f"[{i}/{len(frame)}] OK {mint}")
        except Exception as exc:  # Keep a long batch resumable and auditable.
            errors.append({"mint_address": mint, "error": str(exc)})
            print(f"[{i}/{len(frame)}] ERROR {mint}: {exc}", file=sys.stderr)

        pd.DataFrame(trades).to_csv(out_dir / "trade_results.csv", index=False)
        pd.DataFrame(snapshots).to_csv(out_dir / "price_snapshots.csv", index=False)
        pd.DataFrame(hold_rows).to_csv(out_dir / "hold_30m_results.csv", index=False)
        pd.DataFrame(errors).to_csv(out_dir / "errors.csv", index=False)

    results_df = pd.DataFrame(trades)
    summary = make_summary(results_df, hold_rows)
    summary.update(
        {
            "entry_delay_seconds": args.entry_delay_seconds,
            "take_profit": args.take_profit,
            "stop_loss": args.stop_loss,
            "trailing_stop": args.trailing_stop,
            "moon_bag": args.moon_bag,
            "scale_out_at": args.scale_out_at,
            "scale_out_fraction": args.scale_out_fraction,
            "time_stop_minutes": args.time_stop_minutes,
            "cost_each_side": args.side_cost,
            "round_trip_cost_at_flat_price": 1 - (1 - args.side_cost) / (1 + args.side_cost),
            "errors": len(errors),
        }
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Collect graduation candidates through Helius")
    collect.add_argument("--helius-api-key")
    collect.add_argument("--migration-address")
    collect.add_argument("--count", type=int, default=500)
    collect.add_argument("--start-time", help="ISO-8601 or Unix timestamp")
    collect.add_argument("--end-time", help="ISO-8601 or Unix timestamp")
    collect.add_argument("--requests-per-minute", type=float, default=30)
    collect.add_argument("--output", default=os.path.join(DATA_DIR, "graduations.csv"))
    collect.set_defaults(func=collect_graduations)

    run = sub.add_parser("run", help="Fetch OHLCV, simulate trades, and summarize")
    run.add_argument("--input", default=os.path.join(DATA_DIR, "graduations.csv"))
    run.add_argument("--output-dir", default=DATA_DIR)
    run.add_argument("--limit", type=int)
    run.add_argument("--include-needs-review", action="store_true")
    run.add_argument("--refresh", action="store_true")
    run.add_argument("--requests-per-minute", type=float, default=float(os.getenv("GECKO_REQUESTS_PER_MINUTE", "9")))
    run.add_argument("--entry-delay-seconds", type=int, default=30)
    run.add_argument("--take-profit", type=float, default=0.75)
    run.add_argument("--stop-loss", type=float, default=0.30)
    run.add_argument(
        "--trailing-stop", type=float, default=0.0,
        help="Exit when price falls this fraction from its post-entry peak (0 disables)",
    )
    run.add_argument(
        "--moon-bag", type=float, default=0.0,
        help="Keep this fraction of the position at the primary exit and sell it at ~24h (0 disables)",
    )
    run.add_argument(
        "--scale-out-at", type=float, default=0.0,
        help="Partial take-profit: sell --scale-out-fraction of the position at this gain (0 disables)",
    )
    run.add_argument("--scale-out-fraction", type=float, default=0.5, help="Fraction sold at the scale-out target")
    run.add_argument("--time-stop-minutes", type=int, default=30)
    run.add_argument("--side-cost", type=float, default=0.03, help="Fraction charged on entry and exit")
    run.set_defaults(func=run_backtest)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
