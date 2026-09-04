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

from grad_backtest import KNOWN_QUOTES, WSOL, candidate_mints

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "executor_state.json"
TRADES_FILE = DATA_DIR / "live_trades.csv"
LOG_FILE = DATA_DIR / "executor.log"
STOP_FLAG = DATA_DIR / "executor.stop"
PANIC_FLAG = DATA_DIR / "executor.panic"

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS = 1_000_000_000
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_ACCOUNT_RENT_SOL = 0.00203928
SYSTEM_PROGRAM = "11111111111111111111111111111111"

SKIPS_FILE = DATA_DIR / "skips.csv"

TRADE_COLUMNS = [
    "opened_at", "closed_at", "mint", "mode", "position_usd", "exit_usd",
    "net_return", "exit_reason", "buy_signature", "sell_signature", "entry_price_impact_pct",
    "peak_gain_pct", "entry_market_cap_usd", "entry_curve_age_seconds", "entry_top_holder_pct",
]


def now_ts() -> float:
    return time.time()


RATE_LIMIT_BACKOFF = (0.5, 1.0, 2.0, 4.0)


def is_rate_limited(exc: BaseException) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 429 or " 429 " in f" {exc} "


def with_backoff(fn, what: str):
    """Call fn(); on HTTP 429 wait and retry a few times. Helius and Jupiter's free tiers both
    answer bursts with 429, and a burst is exactly what startup reconciliation and a busy
    position loop produce. Anything other than 429 is raised immediately."""
    for i, delay in enumerate(RATE_LIMIT_BACKOFF):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            if not is_rate_limited(exc):
                raise
            time.sleep(delay)
    return fn()


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
        # Pump.fun tokens graduate around $69k and genuine ones sit near $30k-200k at entry. A
        # market cap far above that 30s after migration means a bundled buy already pumped it
        # and we would be buying the top of someone else's pump, which then dumps into us.
        # Observed live: 15-holder tokens at $5M, $150M caps one minute old. 0 disables.
        self.max_entry_market_cap_usd = float(os.getenv("MAX_ENTRY_MARKET_CAP_USD", "300000"))
        # Floor: graduation is ~$69k, so a token far below that a minute later was already dumped
        # into its own pool. SOLL: the creator sold 78% of supply 24s after migration and we bought
        # at a $450 cap. Overnight, sub-$30k entries went 1 for 7. 0 disables.
        self.min_entry_market_cap_usd = float(os.getenv("MIN_ENTRY_MARKET_CAP_USD", "25000"))
        # Bundle guards. A curve that fills in seconds was bought by one party (SOLL graduated 29s
        # after creation with six buyers), and a wallet holding a big slice of supply at entry is
        # the one that dumps on us (SOLL's creator held 59% at graduation). 0 disables either.
        self.min_curve_age_seconds = float(os.getenv("MIN_CURVE_AGE_SECONDS", "120"))
        self.max_top_holder_pct = float(os.getenv("MAX_TOP_HOLDER_PCT", "20"))
        self.max_entry_lateness_seconds = float(os.getenv("MAX_ENTRY_LATENESS_SECONDS", "60"))
        # Startup wallet reconciliation (live only): adopt untracked holdings worth at least
        # MIN_ADOPT_USD as managed positions so a restart never strands a bag, and close empty
        # token accounts to reclaim their rent. Holdings under the threshold are left alone,
        # so nothing the wallet held before the bot is ever burned.
        self.min_adopt_usd = float(os.getenv("MIN_ADOPT_USD", "1.0"))
        self.close_empty_accounts = os.getenv("CLOSE_EMPTY_ACCOUNTS", "1") == "1"
        # A position whose sells keep failing this long past its time stop is moved to
        # state["stuck"] so it stops blocking a slot; panic still tries to liquidate it.
        self.stuck_after_minutes = float(os.getenv("STUCK_AFTER_MINUTES", "15"))
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


def entry_market_cap_usd(size_usd: float, out_amount_raw: int, supply_ui: float, decimals: int) -> float | None:
    """Market cap implied by the executable buy quote: the USD we put in divided by the tokens
    we actually receive is the price we are really paying, times circulating supply."""
    tokens_ui = out_amount_raw / (10 ** decimals)
    if tokens_ui <= 0 or supply_ui <= 0:
        return None
    return supply_ui * (size_usd / tokens_ui)


def entry_guard_reason(
    cfg: Config,
    graduated_ts: float,
    now: float,
    price_impact_pct: float | None,
    market_cap_usd: float | None = None,
    curve_age_seconds: float | None = None,
    top_holder_pct: float | None = None,
) -> str | None:
    """Reject entries that are no longer the trade the backtest models.

    Unknown inputs (None) never block: a failed metadata lookup is logged, not traded on."""
    lateness = now - (graduated_ts + cfg.entry_delay_seconds)
    if lateness > cfg.max_entry_lateness_seconds:
        return f"stale entry: {lateness:.0f}s past target"
    if price_impact_pct is not None and price_impact_pct > cfg.max_price_impact_pct:
        return f"price impact {price_impact_pct:.1f}% > {cfg.max_price_impact_pct:.1f}% (pool too thin for our size)"
    if (
        market_cap_usd is not None
        and cfg.max_entry_market_cap_usd > 0
        and market_cap_usd > cfg.max_entry_market_cap_usd
    ):
        return (
            f"market cap ${market_cap_usd:,.0f} > ${cfg.max_entry_market_cap_usd:,.0f} "
            "(already pumped far past graduation)"
        )
    if (
        market_cap_usd is not None
        and cfg.min_entry_market_cap_usd > 0
        and market_cap_usd < cfg.min_entry_market_cap_usd
    ):
        return (
            f"market cap ${market_cap_usd:,.0f} < ${cfg.min_entry_market_cap_usd:,.0f} "
            "(already dumped since graduation)"
        )
    if (
        curve_age_seconds is not None
        and cfg.min_curve_age_seconds > 0
        and curve_age_seconds < cfg.min_curve_age_seconds
    ):
        return (
            f"graduated {curve_age_seconds:.0f}s after creation < {cfg.min_curve_age_seconds:.0f}s "
            "(curve filled by one buyer: bundle)"
        )
    if (
        top_holder_pct is not None
        and cfg.max_top_holder_pct > 0
        and top_holder_pct > cfg.max_top_holder_pct
    ):
        return (
            f"top wallet holds {top_holder_pct:.1f}% of supply > {cfg.max_top_holder_pct:.0f}% "
            "(one holder can dump the pool)"
        )
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
        return with_backoff(lambda: self._call(method, params), method)

    def _call(self, method: str, params: list[Any]) -> Any:
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

    def token_accounts(self, owner: str, mint: str | None = None) -> list[dict[str, Any]]:
        """Token accounts the wallet owns: every one across both token programs, or those for a mint."""
        filters = [{"mint": mint}] if mint else [{"programId": TOKEN_PROGRAM}, {"programId": TOKEN_2022_PROGRAM}]
        out: list[dict[str, Any]] = []
        for flt in filters:
            result = self.call("getTokenAccountsByOwner", [owner, flt, {"encoding": "jsonParsed"}])
            for acct in result.get("value") or []:
                info = acct["account"]["data"]["parsed"]["info"]
                out.append(
                    {
                        "pubkey": acct["pubkey"],
                        "program": acct["account"]["owner"],
                        "mint": info["mint"],
                        "amount": int(info["tokenAmount"]["amount"]),
                        "decimals": int(info["tokenAmount"]["decimals"]),
                    }
                )
        return out

    def token_supply(self, mint: str) -> tuple[float, int]:
        """(circulating supply in UI units, decimals) for a mint."""
        value = self.call("getTokenSupply", [mint])["value"]
        return float(value["uiAmountString"]), int(value["decimals"])

    def mint_first_seen(self, mint: str, stop_before_ts: float, max_pages: int = 3) -> float | None:
        """Block time of the mint's earliest transaction. Stops paging early once it has seen a
        transaction older than stop_before_ts, since that already proves the token is at least
        that old, and returns the oldest time seen. None when the history is too long to
        conclude within max_pages (a busy, established token), which the caller treats as
        unknown rather than as a rejection."""
        before: str | None = None
        oldest: float | None = None
        for _ in range(max_pages):
            opts: dict[str, Any] = {"limit": 1000}
            if before:
                opts["before"] = before
            sigs = self.call("getSignaturesForAddress", [mint, opts]) or []
            times = [s["blockTime"] for s in sigs if s.get("blockTime")]
            if times:
                page_oldest = min(times)
                oldest = page_oldest if oldest is None else min(oldest, page_oldest)
            if len(sigs) < 1000:
                return oldest
            if oldest is not None and oldest < stop_before_ts:
                return oldest
            before = sigs[-1]["signature"]
        return None

    def top_wallet_holder(self, mint: str, exclude: set[str] | None = None) -> tuple[str, int] | None:
        """(owner, raw amount) of the largest plain-wallet holder among the mint's 20 largest
        token accounts. Accounts owned by programs (AMM pools, bonding curves, Mayhem vaults)
        cannot dump on us and are skipped, as are owners in `exclude` (our own wallet)."""
        exclude = exclude or set()
        largest = self.call("getTokenLargestAccounts", [mint]).get("value") or []
        if not largest:
            return None
        addresses = [entry["address"] for entry in largest]
        accounts = self.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}]).get("value") or []
        owners: list[tuple[str, int]] = []
        for entry, acct in zip(largest, accounts):
            if not acct:
                continue
            try:
                owner = acct["data"]["parsed"]["info"]["owner"]
            except (KeyError, TypeError):
                continue
            owners.append((owner, int(entry["amount"])))
        if not owners:
            return None
        owner_accounts = self.call(
            "getMultipleAccounts", [[owner for owner, _ in owners], {"encoding": "base64"}]
        ).get("value") or []
        best: tuple[str, int] | None = None
        for (owner, amount), acct in zip(owners, owner_accounts):
            program = acct["owner"] if acct else SYSTEM_PROGRAM  # unfunded wallet: still a wallet
            if program != SYSTEM_PROGRAM or owner in exclude:
                continue
            if best is None or amount > best[1]:
                best = (owner, amount)
        return best

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
        return with_backoff(lambda: self._quote(input_mint, output_mint, amount, slippage_bps), "quote")

    def _quote(
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
        return with_backoff(lambda: self._swap_transaction(quote, pubkey), "swap")

    def _swap_transaction(self, quote: dict[str, Any], pubkey: str) -> bytes:
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

    def sign_instructions(self, instructions: list, blockhash: str) -> bytes:
        from solders.hash import Hash
        from solders.transaction import Transaction

        tx = Transaction.new_signed_with_payer(
            instructions, self.keypair.pubkey(), [self.keypair], Hash.from_string(blockhash)
        )
        return bytes(tx)


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
        """SOL/USD from a 1 SOL -> USDC quote, cached for 30s: it only converts position sizes and
        P&L, and re-quoting it every 5s loop was a third of our Jupiter request budget."""
        cached = getattr(self, "_sol_price", None)
        if cached and now_ts() - cached[0] < 30:
            return cached[1]
        quote = self.jup.quote(WSOL, USDC, LAMPORTS)  # 1 SOL -> USDC (6 decimals)
        price = int(quote["outAmount"]) / 1e6
        self._sol_price = (now_ts(), price)
        return price

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
        signature = None
        for attempt in range(2):
            raw = self.jup.swap_transaction(quote, self.wallet.pubkey)
            signed = self.wallet.sign(raw)
            try:
                signature = self.rpc.send_raw(signed)
                break
            except Exception as exc:
                # The RPC node had not seen the blockhash Jupiter built the transaction on
                # (it lags, or the hash expired while we were rate-limited). A fresh build
                # on the same quote is the fix; anything else is a real failure.
                if attempt == 0 and "BlockhashNotFound" in str(exc):
                    time.sleep(1)
                    continue
                raise
        if not self.rpc.confirmed(signature):
            raise RuntimeError(f"transaction {signature} not confirmed within timeout")
        return signature

    def skip(self, mint: str, reason: str) -> None:
        record_skip(mint, reason)
        log(f"SKIP {mint}: {reason}")

    def entry_metadata(
        self, mint: str, graduated_ts: float, size_usd: float, tokens: int
    ) -> tuple[float | None, float | None, float | None, str | None]:
        """(market cap, seconds from creation to graduation, top wallet holder %, that wallet).
        Every lookup is optional: a failure is logged and leaves that value None, because an
        entry must never be blocked, or forced, by a metadata call that timed out."""
        cfg = self.cfg
        market_cap = curve_age = top_holder_pct = None
        top_holder = None
        supply_ui = decimals = None
        if cfg.max_entry_market_cap_usd > 0 or cfg.min_entry_market_cap_usd > 0 or cfg.max_top_holder_pct > 0:
            try:
                supply_ui, decimals = self.rpc.token_supply(mint)
                market_cap = entry_market_cap_usd(size_usd, tokens, supply_ui, decimals)
            except Exception as exc:
                log(f"WARN {mint}: market cap check unavailable ({describe_error(exc)})")
        if cfg.min_curve_age_seconds > 0:
            try:
                created = self.rpc.mint_first_seen(mint, graduated_ts - cfg.min_curve_age_seconds)
                if created is not None:
                    curve_age = max(0.0, graduated_ts - created)
            except Exception as exc:
                log(f"WARN {mint}: curve age check unavailable ({describe_error(exc)})")
        if cfg.max_top_holder_pct > 0 and supply_ui:
            try:
                exclude = {self.wallet.pubkey} if self.wallet else set()
                holder = self.rpc.top_wallet_holder(mint, exclude)
                if holder:
                    top_holder, amount = holder
                    top_holder_pct = amount / (10 ** decimals) / supply_ui * 100
            except Exception as exc:
                log(f"WARN {mint}: holder concentration check unavailable ({describe_error(exc)})")
        return market_cap, curve_age, top_holder_pct, top_holder

    def try_enter(self, item: dict[str, Any], sol_price: float) -> None:
        mint = item["mint"]
        daily_pnl = float(self.state["daily"]["realized_pnl_usd"])
        # Adopted bags are money already in the market, not a choice we are making now, so they
        # do not take an entry slot: three $2 leftovers must not block every new graduation.
        open_slots = sum(1 for p in self.state["positions"] if not p.get("adopted"))
        size_usd = position_size_usd(self.cfg, self.equity_usd(sol_price), open_slots, daily_pnl)
        if size_usd <= 0:
            self.skip(mint, f"sizing guards (open={open_slots}, daily_pnl={daily_pnl:.2f})")
            return
        lamports = int(size_usd / sol_price * LAMPORTS)
        quote = self.jup.quote(WSOL, mint, lamports)
        tokens = int(quote["outAmount"])
        if tokens <= 0:
            raise RuntimeError("zero-token quote")
        impact = quote_price_impact_pct(quote)
        market_cap, curve_age, top_holder_pct, top_holder = self.entry_metadata(mint, item["graduated_ts"], size_usd, tokens)
        guard = entry_guard_reason(
            self.cfg, item["graduated_ts"], now_ts(), impact, market_cap, curve_age, top_holder_pct
        )
        if guard:
            if top_holder and "top wallet" in guard:
                guard += f" [{top_holder}]"
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
                "entry_market_cap_usd": round(market_cap) if market_cap else None,
                "entry_curve_age_seconds": round(curve_age) if curve_age is not None else None,
                "entry_top_holder_pct": round(top_holder_pct, 1) if top_holder_pct is not None else None,
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
        if self.cfg.mode == "live" and keep == 0 and self.cfg.close_empty_accounts:
            self.reclaim_rent(mint)

    def close_token_account(self, account: str, program: str, mint: str, burn_amount: int = 0) -> str:
        """Reclaim a token account's rent (~0.002 SOL). Burns leftover dust first; only ever
        called with burn_amount > 0 on tokens the bot itself bought."""
        from solders.instruction import AccountMeta, Instruction
        from solders.pubkey import Pubkey

        owner = self.wallet.keypair.pubkey()
        prog, acct, mint_pk = Pubkey.from_string(program), Pubkey.from_string(account), Pubkey.from_string(mint)
        instructions = []
        if burn_amount > 0:
            instructions.append(
                Instruction(
                    prog,
                    bytes([8]) + int(burn_amount).to_bytes(8, "little"),  # SPL Token: Burn
                    [AccountMeta(acct, False, True), AccountMeta(mint_pk, False, True), AccountMeta(owner, True, False)],
                )
            )
        instructions.append(
            Instruction(
                prog,
                bytes([9]),  # SPL Token: CloseAccount (rent returns to owner)
                [AccountMeta(acct, False, True), AccountMeta(owner, False, True), AccountMeta(owner, True, False)],
            )
        )
        blockhash = self.rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
        return self.rpc.send_raw(self.wallet.sign_instructions(instructions, blockhash))

    def reclaim_rent(self, mint: str) -> None:
        try:
            for acct in self.rpc.token_accounts(self.wallet.pubkey, mint=mint):
                sig = self.close_token_account(acct["pubkey"], acct["program"], mint, burn_amount=acct["amount"])
                log(f"RENT reclaimed ~{TOKEN_ACCOUNT_RENT_SOL:.4f} SOL from {mint}'s token account ({sig[:16]}…)")
        except Exception as exc:
            log(f"WARN {mint}: could not close token account: {describe_error(exc)}")

    def reconcile_wallet(self, sol_price: float) -> None:
        """Startup pass over every token account the wallet holds. Untracked holdings worth at
        least MIN_ADOPT_USD become managed positions (basis = current value, clock starts now)
        so a restart can never strand a bag; empty accounts are closed for their rent; anything
        under the threshold is left untouched."""
        tracked = {p["mint"] for p in self.state["positions"]}
        tracked |= {b["mint"] for b in self.state.get("moon_bags", [])}
        tracked |= {b["mint"] for b in self.state.get("stuck", [])}
        closed = adopted = 0
        for acct in self.rpc.token_accounts(self.wallet.pubkey):
            mint = acct["mint"]
            if mint in KNOWN_QUOTES:
                continue
            if acct["amount"] == 0:
                if self.cfg.close_empty_accounts:
                    try:
                        self.close_token_account(acct["pubkey"], acct["program"], mint)
                        closed += 1
                        time.sleep(1.0)  # each close is two RPC calls; stay under the rate limit
                    except Exception as exc:
                        log(f"WARN could not close empty account {acct['pubkey'][:8]}…: {describe_error(exc)}")
                continue
            if mint in tracked:
                continue
            try:
                quote = self.jup.quote(mint, WSOL, acct["amount"], slippage_bps=self.cfg.sell_slippage_bps)
                value = int(quote["outAmount"]) / LAMPORTS * sol_price
            except Exception as exc:
                log(f"WARN {mint}: held but not quotable ({describe_error(exc)}); leaving it alone")
                continue
            if value < self.cfg.min_adopt_usd:
                continue
            self.state["positions"].append(
                {
                    "mint": mint,
                    "tokens": acct["amount"],
                    "position_usd": round(value, 2),
                    "opened_ts": now_ts(),
                    "opened_at": utc_iso(),
                    "graduated_at": None,
                    "buy_signature": "adopted",
                    "entry_price_impact_pct": None,
                    "entry_market_cap_usd": None,
                    "entry_curve_age_seconds": None,
                    "entry_top_holder_pct": None,
                    "peak_usd": value,
                    "adopted": True,
                }
            )
            adopted += 1
            log(f"ADOPTED untracked holding {mint} worth ${value:.2f}; managing it from here (basis = current value)")
        if closed or adopted:
            save_state(self.state)
        log(f"wallet reconciled: adopted {adopted} position(s), closed {closed} empty token account(s)"
            + (f" (~{closed * TOKEN_ACCOUNT_RENT_SOL:.4f} SOL rent)" if closed else ""))

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
                "entry_market_cap_usd": pos.get("entry_market_cap_usd"),
                "entry_curve_age_seconds": pos.get("entry_curve_age_seconds"),
                "entry_top_holder_pct": pos.get("entry_top_holder_pct"),
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
                else:
                    pos["sell_failures"] = 0
            except Exception as exc:
                reason_text = describe_error(exc)
                log(f"WARN managing {pos['mint']}: {reason_text}")
                pos["sell_failures"] = int(pos.get("sell_failures", 0)) + 1
                pos["last_sell_error"] = reason_text
                overdue = pos["opened_ts"] + (self.cfg.time_stop_minutes + self.cfg.stuck_after_minutes) * 60
                if now_ts() > overdue and pos["sell_failures"] >= 3:
                    self.state["positions"].remove(pos)
                    self.state.setdefault("stuck", []).append({**pos, "stuck_at": utc_iso()})
                    save_state(self.state)
                    log(f"STUCK {pos['mint']}: {pos['sell_failures']} consecutive sell failures past its time stop "
                        f"({reason_text}). Slot freed; moved to state.stuck. Panic will retry it, or sell manually.")

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
                "entry_market_cap_usd": pos.get("entry_market_cap_usd"),
                "entry_curve_age_seconds": pos.get("entry_curve_age_seconds"),
                "entry_top_holder_pct": pos.get("entry_top_holder_pct"),
                "peak_gain_pct": round(peak_gain * 100, 1) if peak_gain is not None else None,
            }
        )
        log(f"SCALE-OUT {mint}: sold {frac:.0%} for ${proceeds:.2f} ({net:+.1%}); remainder basis ${pos['position_usd']:.2f}")

    def liquidate_bags(self, sol_price: float, key: str, reason: str) -> None:
        """Market-sell everything in state[key] (moon bags or stuck positions) during panic."""
        for bag in list(self.state.get(key, [])):
            try:
                amount = bag["tokens"]
                if self.cfg.mode == "live":
                    amount = self.rpc.token_balance(self.wallet.pubkey, bag["mint"]) or amount
                quote = self.jup.quote(bag["mint"], WSOL, int(amount), slippage_bps=self.cfg.sell_slippage_bps)
                sig = self.execute_swap(quote) if self.cfg.mode == "live" else ""
                proceeds = int(quote["outAmount"]) / LAMPORTS * sol_price
                if self.cfg.mode == "paper":
                    self.state["paper_balance_usd"] = float(self.state["paper_balance_usd"]) + proceeds
                self.state[key].remove(bag)
                basis = float(bag.get("cost_usd", bag.get("position_usd", 0.0)) or 0.0)
                record_trade(
                    {
                        "opened_at": bag.get("created_at") or bag.get("opened_at"),
                        "closed_at": utc_iso(),
                        "mint": bag["mint"],
                        "mode": self.cfg.mode,
                        "position_usd": round(basis, 2),
                        "exit_usd": round(proceeds, 2),
                        "net_return": round(proceeds / basis - 1.0, 4) if basis else 0.0,
                        "exit_reason": reason,
                        "sell_signature": sig,
                    }
                )
                log(f"EXIT {key[:-1] if key.endswith('s') else key} {bag['mint']} ${proceeds:.2f}")
            except Exception as exc:
                log(f"WARN liquidating {key} {bag['mint']}: {describe_error(exc)}")

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
            f"mcap=${self.cfg.min_entry_market_cap_usd:,.0f}-${self.cfg.max_entry_market_cap_usd:,.0f} "
            f"curve_age>={self.cfg.min_curve_age_seconds:.0f}s top_holder<={self.cfg.max_top_holder_pct:.0f}% "
            f"adopt>=${self.cfg.min_adopt_usd} "
            f"stuck_after={self.cfg.stuck_after_minutes:.0f}m "
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
        if self.cfg.mode == "live":
            try:
                self.reconcile_wallet(self.sol_price_usd())
            except Exception as exc:
                log(f"WARN wallet reconciliation failed: {describe_error(exc)}")
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
                if panic and (self.state.get("moon_bags") or self.state.get("stuck")):
                    price = sol_price or self.sol_price_usd()
                    self.liquidate_bags(price, "moon_bags", "panic_moon_bag")
                    self.liquidate_bags(price, "stuck", "panic_stuck")
                if panic and not self.state["positions"] and not self.state.get("moon_bags") and not self.state.get("stuck"):
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
