"""Snapshot recorder: one row per token per poll with everything replay needs.

Rows carry contemporaneous executable quotes (the exit ladder), the DLMM pool's cumulative
volume and fees (so per-interval fee income is a difference, not a rolling-window guess),
participation counts and data age. Files live under DATA_DIR/mm_snapshots/<mint>.csv."""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any

from .config import MMConfig, WSOL
from .screener import Candidate, append_rows, load_universe, utc_iso
from .sources import Sources, depth_at_impact, impact_at_size

SNAPSHOT_COLUMNS = [
    "ts", "recorded_at", "mint", "symbol", "price_usd", "sol_price_usd", "pool_liquidity_usd",
    "quote_reserve_usd", "dlmm_pool", "dlmm_tvl_usd", "dlmm_price", "dlmm_token_x_amount",
    "dlmm_token_y_amount", "dlmm_cum_volume", "dlmm_cum_fees", "dlmm_fees_1h", "dlmm_volume_1h",
    "dlmm_fee_tvl_24h", "dlmm_dynamic_fee_pct", "dlmm_base_fee_pct", "dlmm_protocol_fee_pct",
    "volume_1h", "buy_volume_1h", "sell_volume_1h", "traders_1h", "organic_buyers_1h",
    "liquidity_change_1h_pct", "holder_change_1h_pct", "impact_at_max_position_pct",
    "depth_routine_usd", "depth_emergency_usd", "ladder", "data_age_s", "errors",
]


def snapshot_path(cfg: MMConfig, mint: str) -> Path:
    return cfg.snapshots_dir / f"{mint}.csv"


def load_snapshots(cfg: MMConfig, mint: str) -> list[dict[str, Any]]:
    path = snapshot_path(cfg, mint)
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda r: float(r.get("ts") or 0))
    return rows


class Recorder:
    def __init__(self, cfg: MMConfig, sources: Sources) -> None:
        self.cfg = cfg
        self.src = sources
        self._sol_price: tuple[float, float] | None = None

    def sol_price(self) -> float | None:
        if self._sol_price and time.monotonic() - self._sol_price[0] < 30:
            return self._sol_price[1]
        price = self.src.jupiter.sol_price()
        if price:
            self._sol_price = (time.monotonic(), price)
        return price

    def snapshot(self, entry: dict[str, Any], with_ladder: bool = True) -> dict[str, Any]:
        """`entry` is a universe row (dict from mm_universe.csv or Candidate.row())."""
        cfg = self.cfg
        mint = entry["mint"]
        started = time.time()
        row: dict[str, Any] = {"ts": round(started, 3), "recorded_at": utc_iso(started), "mint": mint,
                               "symbol": entry.get("symbol", ""), "errors": ""}
        errors: list[str] = []
        token = None
        try:
            token = self.src.jupiter.token(mint, max_age=0)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"jupiter token: {exc}")
        if token:
            row["price_usd"] = token.get("usdPrice")
            row["pool_liquidity_usd"] = token.get("liquidity")
            s1 = token.get("stats1h") or {}
            buy, sell = float(s1.get("buyVolume") or 0), float(s1.get("sellVolume") or 0)
            row.update({"volume_1h": buy + sell, "buy_volume_1h": buy, "sell_volume_1h": sell,
                        "traders_1h": s1.get("numTraders"), "organic_buyers_1h": s1.get("numOrganicBuyers"),
                        "liquidity_change_1h_pct": s1.get("liquidityChange"),
                        "holder_change_1h_pct": s1.get("holderChange")})
        try:
            row["sol_price_usd"] = self.sol_price()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"sol price: {exc}")
        pool_addr = entry.get("dlmm_pool") or ""
        row["dlmm_pool"] = pool_addr
        if pool_addr:
            try:
                pool = self.src.meteora.pool(pool_addr)
            except Exception as exc:  # noqa: BLE001
                pool = None
                errors.append(f"meteora pool: {exc}")
            if pool:
                config = pool.get("pool_config") or {}
                cum = pool.get("cumulative_metrics") or {}
                x, y = pool.get("token_x") or {}, pool.get("token_y") or {}
                quote_is_y = y.get("address") in cfg.quotes
                quote_amount = float(pool.get("token_y_amount" if quote_is_y else "token_x_amount") or 0)
                quote_price = float((y if quote_is_y else x).get("price") or 0)
                row.update({
                    "dlmm_tvl_usd": pool.get("tvl"), "dlmm_price": pool.get("current_price"),
                    "dlmm_token_x_amount": pool.get("token_x_amount"), "dlmm_token_y_amount": pool.get("token_y_amount"),
                    "dlmm_cum_volume": cum.get("volume"), "dlmm_cum_fees": cum.get("fees"),
                    "dlmm_fees_1h": (pool.get("fees") or {}).get("1h"), "dlmm_volume_1h": (pool.get("volume") or {}).get("1h"),
                    "dlmm_fee_tvl_24h": (pool.get("fee_tvl_ratio") or {}).get("24h"),
                    "dlmm_dynamic_fee_pct": pool.get("dynamic_fee_pct"), "dlmm_base_fee_pct": config.get("base_fee_pct"),
                    "dlmm_protocol_fee_pct": config.get("protocol_fee_pct"),
                    "quote_reserve_usd": quote_amount * quote_price if quote_price else None,
                })
                if not row.get("price_usd") and pool.get("current_price") and quote_price:
                    price = float(pool["current_price"])
                    row["price_usd"] = price * quote_price if quote_is_y else quote_price / price
        if row.get("quote_reserve_usd") is None and entry.get("quote_reserve_usd"):
            row["quote_reserve_usd"] = entry["quote_reserve_usd"]
        price = float(row.get("price_usd") or 0)
        if with_ladder and price > 0:
            decimals = int(token.get("decimals") if token and token.get("decimals") is not None else entry.get("decimals") or 6)
            # Three rungs per tick keep the public quote quota intact at a 60s poll.
            sizes = [max(1.0, cfg.max_position_usd / 4), cfg.max_position_usd, cfg.max_position_usd * cfg.exit_depth_multiple]
            try:
                ladder = self.src.jupiter.exit_ladder(mint, decimals, price, sizes)
            except Exception as exc:  # noqa: BLE001
                ladder = []
                errors.append(f"exit ladder: {exc}")
            row["impact_at_max_position_pct"] = impact_at_size(ladder, cfg.max_position_usd)
            row["depth_routine_usd"] = depth_at_impact(ladder, cfg.routine_exit_impact_pct)
            row["depth_emergency_usd"] = depth_at_impact(ladder, cfg.emergency_exit_impact_pct)
            row["ladder"] = ";".join(f"{r['size_usd']:.0f}:{r['impact_pct']:.4f}" for r in ladder if r.get("impact_pct") is not None)
            if ladder and row["impact_at_max_position_pct"] is None:
                errors.append("exit ladder returned no usable quote")
        row["data_age_s"] = round(time.time() - started, 2)
        row["errors"] = "; ".join(errors)
        return row

    def tick(self, universe: list[dict[str, Any]], with_ladder: bool = True) -> list[dict[str, Any]]:
        rows = []
        for entry in universe:
            try:
                row = self.snapshot(entry, with_ladder=with_ladder)
            except Exception as exc:  # noqa: BLE001 - one source outage must not stop the tape
                row = {"ts": round(time.time(), 3), "recorded_at": utc_iso(), "mint": entry["mint"],
                       "symbol": entry.get("symbol", ""), "errors": f"snapshot failed: {exc}"}
            append_rows(snapshot_path(self.cfg, entry["mint"]), SNAPSHOT_COLUMNS, [row])
            rows.append(row)
        return rows

    def run(self, hours: float, universe: list[dict[str, Any]] | None = None, log=print) -> int:
        universe = universe or load_universe(self.cfg.universe_file)
        if not universe:
            raise SystemExit("universe is empty: run `python -m mm screen` first")
        deadline = time.time() + hours * 3600
        ticks = 0
        while True:
            started = time.time()
            rows = self.tick(universe)
            ticks += 1
            errors = sum(1 for r in rows if r.get("errors"))
            log(f"tick {ticks}: {len(rows)} snapshots, {errors} with errors, {self.src.http.calls} HTTP calls total")
            if time.time() >= deadline:
                return ticks
            time.sleep(max(1.0, self.cfg.poll_seconds - (time.time() - started)))
