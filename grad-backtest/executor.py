#!/usr/bin/env python3
"""Live/paper executor for the Pump.fun graduation strategy.

Watches the migration address for new graduations via Helius, enters
ENTRY_DELAY_SECONDS after migration through Jupiter, then manages each
position against take-profit / stop-loss / time-stop using executable
Jupiter sell quotes (not candle prices).

Modes (EXECUTOR_MODE):
  paper (default) — real detection, real quotes, simulated fills, no wallet
  live            — signs and sends real swaps with WALLET_PRIVATE_KEY

Safety rails, enforced in both modes:
  * position size = ACCOUNT_FRACTION of equity, hard-capped by MAX_POSITION_USD
  * MAX_CONCURRENT_POSITIONS open at once
  * DAILY_LOSS_LIMIT_USD of realized losses stops new entries until next UTC day
  * touch DATA_DIR/executor.stop  -> drain: manage open positions, no new buys
  * touch DATA_DIR/executor.panic -> market-sell everything now, then drain

Use a dedicated burner wallet holding only what you can afford to lose.
Never use your main wallet or anything derived from a seed phrase you care
about. This is experimental research software, not investment advice.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from grad_backtest import WSOL, candidate_mints

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "executor_state.json"
TRADES_FILE = DATA_DIR / "live_trades.csv"
LOG_FILE = DATA_DIR / "executor.log"
STOP_FLAG = DATA_DIR / "executor.stop"
PANIC_FLAG = DATA_DIR / "executor.panic"

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS = 1_000_000_000

SKIPS_FILE = DATA_DIR / "skips.csv"

TRADE_COLUMNS = [
    "opened_at", "closed_at", "mint", "mode", "position_usd", "exit_usd",
    "net_return", "exit_reason", "buy_signature", "sell_signature", "entry_price_impact_pct",
    "peak_gain_pct",
]


def now_ts() -> float:
    return time.time()


def utc_iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch or now_ts(), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(message: str) -> None:
    line = f"{utc_iso()} {message}"
    print(line, flush=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as fh:
        fh.write(line + "\n")


class Config:
    def __init__(self) -> None:
        self.mode = os.getenv("EXECUTOR_MODE", "paper").lower()
        if self.mode not in ("paper", "live"):
            raise SystemExit("EXECUTOR_MODE must be 'paper' or 'live'")
        self.helius_api_key = os.getenv("HELIUS_API_KEY") or ""
        self.migration_address = os.getenv("MIGRATION_ADDRESS") or ""
        self.rpc_url = os.getenv("RPC_URL") or f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        self.jupiter_base = os.getenv("JUPITER_BASE_URL", "https://lite-api.jup.ag/swap/v1").rstrip("/")
        self.account_fraction = float(os.getenv("ACCOUNT_FRACTION", "0.10"))
        self.max_position_usd = float(os.getenv("MAX_POSITION_USD", "20"))
        self.min_position_usd = float(os.getenv("MIN_POSITION_USD", "5"))
        self.max_concurrent = int(os.getenv("MAX_CONCURRENT_POSITIONS", "2"))
        self.daily_loss_limit_usd = float(os.getenv("DAILY_LOSS_LIMIT_USD", "30"))
        self.take_profit = float(os.getenv("TAKE_PROFIT", "0.75"))
        self.stop_loss = float(os.getenv("STOP_LOSS", "0.30"))
        self.trailing_stop = float(os.getenv("TRAILING_STOP", "0"))  # fraction off peak; 0 disables
        self.moon_bag = min(0.5, max(0.0, float(os.getenv("MOON_BAG", "0"))))  # fraction kept at exit; 0 disables
        # Partial take-profit: at +SCALE_OUT_AT sell SCALE_OUT_FRACTION of the position and let the
        # remainder ride to the full take-profit under the same rules. 0 disables.
        self.scale_out_at = float(os.getenv("SCALE_OUT_AT", "0"))
        self.scale_out_fraction = min(0.9, max(0.0, float(os.getenv("SCALE_OUT_FRACTION", "0.5"))))
        self.time_stop_minutes = float(os.getenv("TIME_STOP_MINUTES", "30"))
        self.entry_delay_seconds = float(os.getenv("ENTRY_DELAY_SECONDS", "30"))
        self.max_entry_age_seconds = float(os.getenv("MAX_ENTRY_AGE_SECONDS", "120"))
        # Fresh pools move several percent in the ~1s between quote and execution; 3% failed
        # most live swaps with Jupiter error 6001. Sells get more room than buys because a
        # rejected sell in a falling market is the worst outcome available.
        self.slippage_bps = int(os.getenv("SLIPPAGE_BPS", "1000"))
        self.sell_slippage_bps = int(os.getenv("SELL_SLIPPAGE_BPS", "1500"))
        self.entry_retries = int(os.getenv("ENTRY_RETRIES", "2"))
        self.entry_retry_seconds = float(os.getenv("ENTRY_RETRY_SECONDS", "3"))
        self.poll_seconds = float(os.getenv("POLL_SECONDS", "5"))
        self.max_price_impact_pct = float(os.getenv("MAX_PRICE_IMPACT_PCT", "5"))
        self.max_entry_lateness_seconds = float(os.getenv("MAX_ENTRY_LATENESS_SECONDS", "60"))
        self.min_sol_reserve = float(os.getenv("MIN_SOL_RESERVE", "0.05"))
        self.paper_start_balance = float(os.getenv("START_BALANCE", "100"))
        self.wallet_key = os.getenv("WALLET_PRIVATE_KEY") or ""

    def validate(self) -> None:
        if not self.helius_api_key or not self.migration_address:
            raise SystemExit("HELIUS_API_KEY and MIGRATION_ADDRESS are required")
        if self.mode == "live" and not self.wallet_key:
            raise SystemExit("EXECUTOR_MODE=live requires WALLET_PRIVATE_KEY (burner wallet only)")


def load_state(cfg: Config) -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log("WARNING: executor_state.json unreadable; starting fresh")
    return {
        "mode": cfg.mode,
        "paper_balance_usd": cfg.paper_start_balance,
        "positions": [],
        "daily": {"date": utc_iso()[:10], "realized_pnl_usd": 0.0},
        "seen_signatures": [],
        "updated_at": utc_iso(),
        "draining": False,
    }


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_iso()
    state["seen_signatures"] = state["seen_signatures"][-500:]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def roll_daily(state: dict[str, Any]) -> None:
    today = utc_iso()[:10]
    if state["daily"]["date"] != today:
        state["daily"] = {"date": today, "realized_pnl_usd": 0.0}


def record_trade(row: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    columns = TRADE_COLUMNS
    new = not TRADES_FILE.exists()
    if not new:  # keep appending against whatever header the file already has
        with TRADES_FILE.open() as fh:
            existing = fh.readline().strip().split(",")
        if existing and existing != [""]:
            columns = existing
    with TRADES_FILE.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k) for k in columns})


def record_skip(mint: str, reason: str) -> None:
    """Audit trail of everything the bot passed on, and why."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not SKIPS_FILE.exists()
    with SKIPS_FILE.open("a", newline="") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["timestamp", "mint", "reason"])
        writer.writerow([utc_iso(), mint, reason])


def quote_price_impact_pct(quote: dict[str, Any]) -> float | None:
    raw = quote.get("priceImpactPct")
    try:
        return abs(float(raw)) * 100 if raw is not None else None
    except (TypeError, ValueError):
        return None


def describe_error(exc: BaseException) -> str:
    """Compact, human-readable form of swap/RPC failures for the log. The raw RPC error
    carries the whole simulation log (thousands of characters); the code is what matters."""
    text = str(exc)
    if "0x1771" in text or "'Custom': 6001" in text or '"Custom": 6001' in text or '"Custom":6001' in text:
        return "Jupiter 6001: slippage tolerance exceeded (price moved past tolerance between quote and execution)"
    if "0x1770" in text or "'Custom': 6000" in text or '"Custom": 6000' in text:
        return "Jupiter 6000: route no longer valid"
    return text[:240] + "…" if len(text) > 240 else text


def entry_guard_reason(cfg: Config, graduated_ts: float, now: float, price_impact_pct: float | None) -> str | None:
    """Reject entries that are no longer the trade the backtest models."""
    lateness = now - (graduated_ts + cfg.entry_delay_seconds)
    if lateness > cfg.max_entry_lateness_seconds:
        return f"stale entry: {lateness:.0f}s past target"
    if price_impact_pct is not None and price_impact_pct > cfg.max_price_impact_pct:
        return f"price impact {price_impact_pct:.1f}% > {cfg.max_price_impact_pct:.1f}% (pool too thin for our size)"
    return None


def position_size_usd(cfg: Config, equity_usd: float, open_positions: int, daily_pnl: float) -> float:
    """Sizing with every guard applied; 0 means 'do not enter'."""
    if open_positions >= cfg.max_concurrent:
        return 0.0
    if daily_pnl <= -cfg.daily_loss_limit_usd:
        return 0.0
    size = min(equity_usd * cfg.account_fraction, cfg.max_position_usd)
    if size < cfg.min_position_usd or size > equity_usd:
        return 0.0
    return round(size, 2)


def decide_exit(
    entry_usd: float,
    current_usd: float,
    opened_ts: float,
    now: float,
    cfg: Config,
    peak_usd: float | None = None,
) -> str | None:
    """TP/SL/trailing/time-stop against the executable exit value of the whole position."""
    if current_usd >= entry_usd * (1.0 + cfg.take_profit):
        return "take_profit"
    if current_usd <= entry_usd * (1.0 - cfg.stop_loss):
        return "stop_loss"
    if cfg.trailing_stop > 0 and peak_usd and current_usd <= peak_usd * (1.0 - cfg.trailing_stop):
        return "trailing_stop"
    if now - opened_ts >= cfg.time_stop_minutes * 60:
        return "time_stop"
    return None


class Rpc:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()

    def call(self, method: str, params: list[Any]) -> Any:
        resp = self.session.post(
            self.cfg.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"RPC {method}: {body['error']}")
        return body["result"]

    def sol_balance(self, pubkey: str) -> float:
        return self.call("getBalance", [pubkey])["value"] / LAMPORTS

    def token_balance(self, owner: str, mint: str) -> int:
        result = self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        total = 0
        for acct in result.get("value") or []:
            info = acct["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
        return total

    def send_raw(self, raw: bytes) -> str:
        return self.call(
            "sendTransaction",
            [base64.b64encode(raw).decode(), {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
        )

    def confirmed(self, signature: str, timeout_s: float = 60) -> bool:
        deadline = now_ts() + timeout_s
        while now_ts() < deadline:
            statuses = self.call("getSignatureStatuses", [[signature]])["value"]
            status = statuses[0]
            if status:
                if status.get("err"):
                    raise RuntimeError(f"transaction failed on-chain: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            time.sleep(2)
        return False


class Jupiter:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()

    def quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int | None = None
    ) -> dict[str, Any]:
        resp = self.session.get(
            f"{self.cfg.jupiter_base}/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": self.cfg.slippage_bps if slippage_bps is None else slippage_bps,
            },
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        if "outAmount" not in body:
            raise RuntimeError(f"no route: {body}")
        return body

    def swap_transaction(self, quote: dict[str, Any], pubkey: str) -> bytes:
        resp = self.session.post(
            f"{self.cfg.jupiter_base}/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": pubkey,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return base64.b64decode(resp.json()["swapTransaction"])


class Wallet:
    """Live-mode signer. Only constructed when EXECUTOR_MODE=live."""

    def __init__(self, cfg: Config) -> None:
        from solders.keypair import Keypair

        self.keypair = Keypair.from_base58_string(cfg.wallet_key)
        self.pubkey = str(self.keypair.pubkey())

    def sign(self, raw_tx: bytes) -> bytes:
        from solders.transaction import VersionedTransaction

        tx = VersionedTransaction.from_bytes(raw_tx)
        return bytes(VersionedTransaction(tx.message, [self.keypair]))


class Executor:
    def __init__(self, cfg: Config) -> None:
        cfg.validate()
        self.cfg = cfg
        self.rpc = Rpc(cfg)
        self.jup = Jupiter(cfg)
        self.wallet = Wallet(cfg) if cfg.mode == "live" else None
        self.state = load_state(cfg)
        self.state["mode"] = cfg.mode
        self.pending: list[dict[str, Any]] = []  # graduations waiting for entry delay

    # ---- pricing helpers -------------------------------------------------
    def sol_price_usd(self) -> float:
        quote = self.jup.quote(WSOL, USDC, LAMPORTS)  # 1 SOL -> USDC (6 decimals)
        return int(quote["outAmount"]) / 1e6

    def equity_usd(self, sol_price: float) -> float:
        if self.cfg.mode == "paper":
            return float(self.state["paper_balance_usd"])
        spendable = max(0.0, self.rpc.sol_balance(self.wallet.pubkey) - self.cfg.min_sol_reserve)
        return spendable * sol_price

    # ---- detection -------------------------------------------------------
    def poll_graduations(self) -> None:
        url = f"https://api-mainnet.helius-rpc.com/v0/addresses/{self.cfg.migration_address}/transactions"
        try:
            batch = requests.get(url, params={"api-key": self.cfg.helius_api_key, "limit": 10}, timeout=20).json()
        except Exception as exc:
            log(f"WARN helius poll failed: {exc}")
            return
        if not isinstance(batch, list):
            log(f"WARN helius poll unexpected response: {str(batch)[:200]}")
            return
        for tx in batch:
            signature = tx.get("signature") or ""
            timestamp = tx.get("timestamp") or tx.get("blockTime")
            if not signature or not timestamp or signature in self.state["seen_signatures"]:
                continue
            self.state["seen_signatures"].append(signature)
            mints = candidate_mints(tx)
            if len(mints) != 1:
                if mints:
                    self.skip(",".join(mints), f"ambiguous graduation tx {signature[:16]}… ({len(mints)} candidate mints)")
                continue
            age = now_ts() - int(timestamp)
            if age > self.cfg.max_entry_age_seconds:
                self.skip(mints[0], f"graduation too old at detection ({age:.0f}s)")
                continue
            enter_at = int(timestamp) + self.cfg.entry_delay_seconds
            self.pending.append({"mint": mints[0], "graduated_ts": int(timestamp), "enter_at": enter_at})
            log(f"DETECTED graduation {mints[0]} (age {age:.0f}s, entering at +{self.cfg.entry_delay_seconds:.0f}s)")

    # ---- trading ---------------------------------------------------------
    def execute_swap(self, quote: dict[str, Any]) -> str:
        raw = self.jup.swap_transaction(quote, self.wallet.pubkey)
        signed = self.wallet.sign(raw)
        signature = self.rpc.send_raw(signed)
        if not self.rpc.confirmed(signature):
            raise RuntimeError(f"transaction {signature} not confirmed within timeout")
        return signature

    def skip(self, mint: str, reason: str) -> None:
        record_skip(mint, reason)
        log(f"SKIP {mint}: {reason}")

    def try_enter(self, item: dict[str, Any], sol_price: float) -> None:
        mint = item["mint"]
        daily_pnl = float(self.state["daily"]["realized_pnl_usd"])
        size_usd = position_size_usd(self.cfg, self.equity_usd(sol_price), len(self.state["positions"]), daily_pnl)
        if size_usd <= 0:
            self.skip(mint, f"sizing guards (open={len(self.state['positions'])}, daily_pnl={daily_pnl:.2f})")
            return
        lamports = int(size_usd / sol_price * LAMPORTS)
        quote = self.jup.quote(WSOL, mint, lamports)
        tokens = int(quote["outAmount"])
        if tokens <= 0:
            raise RuntimeError("zero-token quote")
        impact = quote_price_impact_pct(quote)
        guard = entry_guard_reason(self.cfg, item["graduated_ts"], now_ts(), impact)
        if guard:
            self.skip(mint, guard)
            return
        # Honeypot guard: a token you can buy but not sell has no reverse route.
        try:
            self.jup.quote(mint, WSOL, tokens)
        except Exception as exc:
            self.skip(mint, f"no sell route (possible honeypot): {exc}")
            return
        buy_sig = ""
        if self.cfg.mode == "live":
            try:
                buy_sig = self.execute_swap(quote)
            except Exception as exc:
                # A swap can still land after our confirmation timeout. Check for
                # the tokens before giving up, or they become an untracked bag
                # sitting in the wallet that nothing will ever sell.
                time.sleep(15)
                landed = self.rpc.token_balance(self.wallet.pubkey, mint)
                if landed <= 0:
                    raise
                log(f"WARN {mint}: entry reported failure ({exc}) but {landed} tokens landed; adopting position")
                buy_sig = "unconfirmed"
            tokens = self.rpc.token_balance(self.wallet.pubkey, mint) or tokens
        else:
            self.state["paper_balance_usd"] = float(self.state["paper_balance_usd"]) - size_usd
        self.state["positions"].append(
            {
                "mint": mint,
                "tokens": tokens,
                "position_usd": size_usd,
                "opened_ts": now_ts(),
                "opened_at": utc_iso(),
                "graduated_at": utc_iso(item["graduated_ts"]),
                "buy_signature": buy_sig,
                "entry_price_impact_pct": impact,
                "peak_usd": size_usd,
            }
        )
        save_state(self.state)
        log(f"ENTER {mint} ${size_usd:.2f} ({'live ' + buy_sig[:16] + '…' if buy_sig else 'paper fill'})")

    def close_position(self, pos: dict[str, Any], reason: str, sol_price: float) -> None:
        mint = pos["mint"]
        sell_sig = ""
        # Panic dumps everything; otherwise keep the configured moon-bag fraction.
        mb = 0.0 if reason == "panic" else self.cfg.moon_bag
        if self.cfg.mode == "live":
            amount = self.rpc.token_balance(self.wallet.pubkey, mint)
            if amount <= 0:
                # A previous sell most likely landed after our confirmation timeout. Record the
                # close at the last quoted value so the trade log stays complete, and flag it.
                est = float(pos.get("last_value_usd") or 0.0)
                log(
                    f"WARN {mint}: no tokens on-chain to sell; closing position at last quoted value "
                    f"(${est:.2f}). Confirm the actual proceeds on an explorer."
                )
                self.state["daily"]["realized_pnl_usd"] = float(self.state["daily"]["realized_pnl_usd"]) + (est - pos["position_usd"])
                self.state["positions"].remove(pos)
                save_state(self.state)
                self._record_close(pos, f"{reason}_unconfirmed", est, "", pos["position_usd"])
                return
        else:
            amount = int(pos["tokens"])
        keep = int(amount * mb)
        sell_amount = amount - keep
        quote = self.jup.quote(mint, WSOL, sell_amount, slippage_bps=self.cfg.sell_slippage_bps)
        if self.cfg.mode == "live":
            sell_sig = self.execute_swap(quote)
        exit_usd = int(quote["outAmount"]) / LAMPORTS * sol_price
        if self.cfg.mode == "paper":
            self.state["paper_balance_usd"] = float(self.state["paper_balance_usd"]) + exit_usd
        sold_cost = pos["position_usd"] * (1.0 - mb)
        net = exit_usd / sold_cost - 1.0 if sold_cost > 0 else 0.0
        if keep > 0:
            self.state.setdefault("moon_bags", []).append(
                {
                    "mint": mint,
                    "tokens": keep,
                    "cost_usd": round(pos["position_usd"] * mb, 2),
                    "created_at": utc_iso(),
                    "from_exit": reason,
                }
            )
            log(f"MOONBAG {mint}: keeping {mb:.0%} ({keep} tokens, ${pos['position_usd'] * mb:.2f} basis)")
        self.state["daily"]["realized_pnl_usd"] = float(self.state["daily"]["realized_pnl_usd"]) + (exit_usd - sold_cost)
        self.state["positions"].remove(pos)
        save_state(self.state)
        self._record_close(pos, reason, exit_usd, sell_sig, sold_cost)

    def _record_close(self, pos: dict[str, Any], reason: str, exit_usd: float, sell_sig: str, sold_cost: float) -> None:
        net = exit_usd / sold_cost - 1.0 if sold_cost > 0 else 0.0
        peak_gain = float(pos.get("peak_usd", 0.0)) / pos["position_usd"] - 1.0 if pos["position_usd"] else None
        record_trade(
            {
                "opened_at": pos["opened_at"],
                "closed_at": utc_iso(),
                "mint": pos["mint"],
                "mode": self.cfg.mode,
                "position_usd": round(sold_cost, 2),
                "exit_usd": round(exit_usd, 2),
                "net_return": round(net, 4),
                "exit_reason": reason,
                "buy_signature": pos.get("buy_signature", ""),
                "sell_signature": sell_sig,
                "entry_price_impact_pct": pos.get("entry_price_impact_pct"),
                "peak_gain_pct": round(peak_gain * 100, 1) if peak_gain is not None else None,
            }
        )
        peaked = f", peaked {peak_gain:+.1%}" if peak_gain is not None else ""
        log(f"EXIT {pos['mint']} {reason} ${exit_usd:.2f} ({net:+.1%}{peaked})")

    def manage_positions(self, sol_price: float, panic: bool) -> None:
        for pos in list(self.state["positions"]):
            try:
                quote = self.jup.quote(pos["mint"], WSOL, int(pos["tokens"]))
                current_usd = int(quote["outAmount"]) / LAMPORTS * sol_price
                pos["peak_usd"] = max(float(pos.get("peak_usd", pos["position_usd"])), current_usd)
                pos["last_value_usd"] = current_usd
                if (
                    not panic
                    and self.cfg.scale_out_at > 0
                    and not pos.get("scaled_out")
                    and current_usd >= pos["position_usd"] * (1.0 + self.cfg.scale_out_at)
                ):
                    self.scale_out(pos, sol_price)
                    continue  # remainder is re-evaluated against a fresh quote next cycle
                reason = "panic" if panic else decide_exit(
                    pos["position_usd"], current_usd, pos["opened_ts"], now_ts(), self.cfg, pos["peak_usd"]
                )
                if reason:
                    self.close_position(pos, reason, sol_price)
            except Exception as exc:
                log(f"WARN managing {pos['mint']}: {describe_error(exc)}")

    def scale_out(self, pos: dict[str, Any], sol_price: float) -> None:
        """Bank part of a winner at the first target. The remainder keeps the same TP/SL/trailing
        rules on its reduced cost basis, which leaves every threshold at the same token price."""
        mint = pos["mint"]
        frac = self.cfg.scale_out_fraction
        amount = self.rpc.token_balance(self.wallet.pubkey, mint) if self.cfg.mode == "live" else int(pos["tokens"])
        sell_amount = int(amount * frac)
        if sell_amount <= 0:
            return
        quote = self.jup.quote(mint, WSOL, sell_amount, slippage_bps=self.cfg.sell_slippage_bps)
        sig = self.execute_swap(quote) if self.cfg.mode == "live" else ""
        proceeds = int(quote["outAmount"]) / LAMPORTS * sol_price
        if self.cfg.mode == "paper":
            self.state["paper_balance_usd"] = float(self.state["paper_balance_usd"]) + proceeds
        sold_cost = pos["position_usd"] * frac
        net = proceeds / sold_cost - 1.0 if sold_cost > 0 else 0.0
        peak_gain = float(pos["peak_usd"]) / pos["position_usd"] - 1.0 if pos["position_usd"] else None
        self.state["daily"]["realized_pnl_usd"] = float(self.state["daily"]["realized_pnl_usd"]) + (proceeds - sold_cost)
        pos["tokens"] = amount - sell_amount
        pos["position_usd"] = pos["position_usd"] - sold_cost
        pos["peak_usd"] = float(pos["peak_usd"]) * (1.0 - frac)
        pos["scaled_out"] = True
        save_state(self.state)
        record_trade(
            {
                "opened_at": pos["opened_at"],
                "closed_at": utc_iso(),
                "mint": mint,
                "mode": self.cfg.mode,
                "position_usd": round(sold_cost, 2),
                "exit_usd": round(proceeds, 2),
                "net_return": round(net, 4),
                "exit_reason": "scale_out",
                "buy_signature": pos.get("buy_signature", ""),
                "sell_signature": sig,
                "entry_price_impact_pct": pos.get("entry_price_impact_pct"),
                "peak_gain_pct": round(peak_gain * 100, 1) if peak_gain is not None else None,
            }
        )
        log(f"SCALE-OUT {mint}: sold {frac:.0%} for ${proceeds:.2f} ({net:+.1%}); remainder basis ${pos['position_usd']:.2f}")

    def liquidate_moon_bags(self, sol_price: float) -> None:
        for bag in list(self.state.get("moon_bags", [])):
            try:
                amount = bag["tokens"]
                if self.cfg.mode == "live":
                    amount = self.rpc.token_balance(self.wallet.pubkey, bag["mint"]) or amount
                quote = self.jup.quote(bag["mint"], WSOL, int(amount), slippage_bps=self.cfg.sell_slippage_bps)
                sig = self.execute_swap(quote) if self.cfg.mode == "live" else ""
                proceeds = int(quote["outAmount"]) / LAMPORTS * sol_price
                if self.cfg.mode == "paper":
                    self.state["paper_balance_usd"] = float(self.state["paper_balance_usd"]) + proceeds
                self.state["moon_bags"].remove(bag)
                record_trade(
                    {
                        "opened_at": bag["created_at"],
                        "closed_at": utc_iso(),
                        "mint": bag["mint"],
                        "mode": self.cfg.mode,
                        "position_usd": bag["cost_usd"],
                        "exit_usd": round(proceeds, 2),
                        "net_return": round(proceeds / bag["cost_usd"] - 1.0, 4) if bag["cost_usd"] else 0.0,
                        "exit_reason": "panic_moon_bag",
                        "sell_signature": sig,
                    }
                )
                log(f"EXIT moon bag {bag['mint']} ${proceeds:.2f}")
            except Exception as exc:
                log(f"WARN liquidating moon bag {bag['mint']}: {describe_error(exc)}")

    def enter_with_retry(self, item: dict[str, Any], sol_price: float) -> None:
        """Slippage rejections are transient (the price moved during the ~1s between quote and
        send), so re-quote and try again a couple of times. try_enter re-runs every guard on
        each attempt, so the staleness window still bounds how late an entry can land."""
        attempts = 1 + max(0, self.cfg.entry_retries)
        for attempt in range(1, attempts + 1):
            try:
                self.try_enter(item, sol_price)
                return
            except Exception as exc:
                reason = describe_error(exc)
                if attempt < attempts:
                    log(f"WARN entry {item['mint']} attempt {attempt}/{attempts} failed: {reason}; retrying")
                    time.sleep(self.cfg.entry_retry_seconds)
                else:
                    log(f"WARN entry {item['mint']} failed after {attempts} attempts: {reason}")

    # ---- main loop -------------------------------------------------------
    def run(self) -> None:
        log(f"executor starting: mode={self.cfg.mode} fraction={self.cfg.account_fraction} "
            f"max_pos=${self.cfg.max_position_usd} tp=+{self.cfg.take_profit:.0%} sl=-{self.cfg.stop_loss:.0%} "
            f"time_stop={self.cfg.time_stop_minutes:.0f}m trail={self.cfg.trailing_stop:.0%} "
            f"scale_out={self.cfg.scale_out_at:.0%}x{self.cfg.scale_out_fraction:.0%} moon_bag={self.cfg.moon_bag:.0%} "
            f"slippage={self.cfg.slippage_bps}/{self.cfg.sell_slippage_bps}bps(buy/sell) "
            f"retries={self.cfg.entry_retries} daily_loss_limit=${self.cfg.daily_loss_limit_usd}")
        if self.cfg.mode == "live":
            log(f"live wallet: {self.wallet.pubkey} (burner only!)")
            try:
                balance = self.rpc.sol_balance(self.wallet.pubkey)
                log(f"wallet balance: {balance:.4f} SOL ({self.cfg.min_sol_reserve} SOL reserved for fees)")
                if balance <= self.cfg.min_sol_reserve:
                    log("WARNING: balance at or below the fee reserve — no entries will be taken until funded")
            except Exception as exc:
                log(f"WARN could not read wallet balance: {exc}")
        if self.state["positions"]:
            log(f"resuming {len(self.state['positions'])} open position(s) from state file")
        while True:
            try:
                roll_daily(self.state)
                panic = PANIC_FLAG.exists()
                draining = STOP_FLAG.exists() or panic
                self.state["draining"] = draining
                if not draining:
                    self.poll_graduations()
                sol_price = None
                due = [p for p in self.pending if now_ts() >= p["enter_at"]]
                if (due and not draining) or self.state["positions"]:
                    sol_price = self.sol_price_usd()
                for item in due:
                    self.pending.remove(item)
                    if draining:
                        continue
                    self.enter_with_retry(item, sol_price)
                if self.state["positions"]:
                    self.manage_positions(sol_price, panic)
                if panic and self.state.get("moon_bags"):
                    self.liquidate_moon_bags(sol_price or self.sol_price_usd())
                if panic and not self.state["positions"] and not self.state.get("moon_bags"):
                    PANIC_FLAG.unlink(missing_ok=True)
                    STOP_FLAG.touch()
                    log("panic complete: all positions and moon bags closed, executor draining")
                save_state(self.state)
            except Exception as exc:
                log(f"ERROR loop: {exc}")
            time.sleep(self.cfg.poll_seconds)


def main() -> None:
    cfg = Config()
    if "--print-config" in sys.argv:
        safe = {k: v for k, v in vars(cfg).items() if k != "wallet_key"}
        print(json.dumps(safe, indent=2))
        return
    Executor(cfg).run()


if __name__ == "__main__":
    main()
