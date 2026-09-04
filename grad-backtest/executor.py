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

TRADE_COLUMNS = [
    "opened_at", "closed_at", "mint", "mode", "position_usd", "exit_usd",
    "net_return", "exit_reason", "buy_signature", "sell_signature",
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
        self.time_stop_minutes = float(os.getenv("TIME_STOP_MINUTES", "30"))
        self.entry_delay_seconds = float(os.getenv("ENTRY_DELAY_SECONDS", "30"))
        self.max_entry_age_seconds = float(os.getenv("MAX_ENTRY_AGE_SECONDS", "120"))
        self.slippage_bps = int(os.getenv("SLIPPAGE_BPS", "300"))
        self.poll_seconds = float(os.getenv("POLL_SECONDS", "5"))
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
    new = not TRADES_FILE.exists()
    with TRADES_FILE.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k) for k in TRADE_COLUMNS})


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


def decide_exit(entry_usd: float, current_usd: float, opened_ts: float, now: float, cfg: Config) -> str | None:
    """TP/SL/time-stop against the executable exit value of the whole position."""
    if current_usd >= entry_usd * (1.0 + cfg.take_profit):
        return "take_profit"
    if current_usd <= entry_usd * (1.0 - cfg.stop_loss):
        return "stop_loss"
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

    def quote(self, input_mint: str, output_mint: str, amount: int) -> dict[str, Any]:
        resp = self.session.get(
            f"{self.cfg.jupiter_base}/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": self.cfg.slippage_bps,
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
                    log(f"SKIP ambiguous graduation tx {signature[:16]}… ({len(mints)} candidate mints)")
                continue
            age = now_ts() - int(timestamp)
            if age > self.cfg.max_entry_age_seconds:
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

    def try_enter(self, item: dict[str, Any], sol_price: float) -> None:
        mint = item["mint"]
        daily_pnl = float(self.state["daily"]["realized_pnl_usd"])
        size_usd = position_size_usd(self.cfg, self.equity_usd(sol_price), len(self.state["positions"]), daily_pnl)
        if size_usd <= 0:
            log(f"SKIP {mint}: sizing guards (open={len(self.state['positions'])}, daily_pnl={daily_pnl:.2f})")
            return
        lamports = int(size_usd / sol_price * LAMPORTS)
        quote = self.jup.quote(WSOL, mint, lamports)
        tokens = int(quote["outAmount"])
        if tokens <= 0:
            raise RuntimeError("zero-token quote")
        buy_sig = ""
        if self.cfg.mode == "live":
            buy_sig = self.execute_swap(quote)
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
            }
        )
        save_state(self.state)
        log(f"ENTER {mint} ${size_usd:.2f} ({'live ' + buy_sig[:16] + '…' if buy_sig else 'paper fill'})")

    def close_position(self, pos: dict[str, Any], reason: str, sol_price: float) -> None:
        mint = pos["mint"]
        sell_sig = ""
        if self.cfg.mode == "live":
            amount = self.rpc.token_balance(self.wallet.pubkey, mint)
            if amount <= 0:
                log(f"WARN {mint}: no tokens on-chain to sell; dropping position")
                self.state["positions"].remove(pos)
                save_state(self.state)
                return
            quote = self.jup.quote(mint, WSOL, amount)
            sell_sig = self.execute_swap(quote)
            exit_usd = int(quote["outAmount"]) / LAMPORTS * sol_price
        else:
            quote = self.jup.quote(mint, WSOL, int(pos["tokens"]))
            exit_usd = int(quote["outAmount"]) / LAMPORTS * sol_price
            self.state["paper_balance_usd"] = float(self.state["paper_balance_usd"]) + exit_usd
        net = exit_usd / pos["position_usd"] - 1.0
        self.state["daily"]["realized_pnl_usd"] = float(self.state["daily"]["realized_pnl_usd"]) + (exit_usd - pos["position_usd"])
        self.state["positions"].remove(pos)
        save_state(self.state)
        record_trade(
            {
                "opened_at": pos["opened_at"],
                "closed_at": utc_iso(),
                "mint": mint,
                "mode": self.cfg.mode,
                "position_usd": round(pos["position_usd"], 2),
                "exit_usd": round(exit_usd, 2),
                "net_return": round(net, 4),
                "exit_reason": reason,
                "buy_signature": pos.get("buy_signature", ""),
                "sell_signature": sell_sig,
            }
        )
        log(f"EXIT {mint} {reason} ${exit_usd:.2f} ({net:+.1%})")

    def manage_positions(self, sol_price: float, panic: bool) -> None:
        for pos in list(self.state["positions"]):
            try:
                quote = self.jup.quote(pos["mint"], WSOL, int(pos["tokens"]))
                current_usd = int(quote["outAmount"]) / LAMPORTS * sol_price
                reason = "panic" if panic else decide_exit(
                    pos["position_usd"], current_usd, pos["opened_ts"], now_ts(), self.cfg
                )
                if reason:
                    self.close_position(pos, reason, sol_price)
            except Exception as exc:
                log(f"WARN managing {pos['mint']}: {exc}")

    # ---- main loop -------------------------------------------------------
    def run(self) -> None:
        log(f"executor starting: mode={self.cfg.mode} fraction={self.cfg.account_fraction} "
            f"max_pos=${self.cfg.max_position_usd} tp=+{self.cfg.take_profit:.0%} sl=-{self.cfg.stop_loss:.0%} "
            f"time_stop={self.cfg.time_stop_minutes:.0f}m daily_loss_limit=${self.cfg.daily_loss_limit_usd}")
        if self.cfg.mode == "live":
            log(f"live wallet: {self.wallet.pubkey} (burner only!)")
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
                    try:
                        self.try_enter(item, sol_price)
                    except Exception as exc:
                        log(f"WARN entry {item['mint']} failed: {exc}")
                if self.state["positions"]:
                    self.manage_positions(sol_price, panic)
                if panic and not self.state["positions"]:
                    PANIC_FLAG.unlink(missing_ok=True)
                    STOP_FLAG.touch()
                    log("panic complete: all positions closed, executor draining")
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
