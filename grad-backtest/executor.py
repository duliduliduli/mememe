#!/usr/bin/env python3
"""Live/paper executor for the Pump.fun graduation strategy.

Watches the migration address for new graduations through standard Solana RPC/WSS, enters
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
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import websocket

from bundle_analysis import (
    CEX_FUNDERS,
    ancestry_clusters,
    bundle_slot_pct,
    cluster_supply_pct,
    cluster_wallets,
    coordinated_buy_pct,
    early_buy_pct,
    related_holder_wallets,
    top_wallets_supply_pct,
    transfer_clusters,
    wallets_supply_pct,
)
from grad_backtest import KNOWN_QUOTES, WSOL

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "executor_state.json"
TRADES_FILE = DATA_DIR / "live_trades.csv"
LOG_FILE = DATA_DIR / "executor.log"
STOP_FLAG = DATA_DIR / "executor.stop"
PANIC_FLAG = DATA_DIR / "executor.panic"
WALLET_GRAPH_CACHE_FILE = DATA_DIR / "wallet_graph_cache.json"

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS = 1_000_000_000
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_ACCOUNT_RENT_SOL = 0.00203928
SYSTEM_PROGRAM = "11111111111111111111111111111111"
DEX_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
}

SKIPS_FILE = DATA_DIR / "skips.csv"

TRADE_COLUMNS = [
    "opened_at", "closed_at", "mint", "mode", "position_usd", "exit_usd",
    "net_return", "exit_reason", "buy_signature", "sell_signature", "entry_price_impact_pct",
    "entry_round_trip_pct",
    "peak_gain_pct", "entry_market_cap_usd", "entry_curve_age_seconds", "entry_top_holder_pct",
    "entry_bundle_slot_pct", "entry_cluster_pct", "entry_dev_cluster_pct",
    "entry_ancestry_cluster_pct", "entry_transfer_cluster_pct", "entry_coordinated_buy_pct",
    "entry_repeat_cohort_pct",
    "entry_top10_wallet_pct", "entry_early_buy_pct", "entry_funder_coverage_pct",
    "entry_funder_lookup_pct", "entry_funder_sample_count",
    "entry_holder_sample_count", "entry_bundle_confidence",
]


def now_ts() -> float:
    return time.time()


def percent_env(name: str, default: float) -> float:
    """Read a percentage while accepting either 30 or the legacy 0.30 spelling."""
    value = float(os.getenv(name, str(default)))
    return value * 100 if 0 < value <= 1 else value


RATE_LIMIT_BACKOFF = (0.5, 1.0, 2.0, 4.0)


def is_rate_limited(exc: BaseException) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 429 or " 429 " in f" {exc} "


def is_provider_unavailable(exc: BaseException) -> bool:
    """Errors for which trying a second RPC endpoint is safe and useful."""
    if is_rate_limited(exc) or isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    resp = getattr(exc, "response", None)
    if getattr(resp, "status_code", 0) >= 500:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in ("node is unhealthy", "service unavailable", "too many requests"))


def split_urls(value: str) -> list[str]:
    """Comma/newline-separated endpoint list with stable de-duplication."""
    urls: list[str] = []
    for item in re.split(r"[,\n]", value or ""):
        url = item.strip().rstrip("/")
        if url and url not in urls:
            urls.append(url)
    return urls


def websocket_url(http_url: str) -> str:
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://"):]
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://"):]
    return http_url


def redact_endpoint(url: str) -> str:
    """Show only a provider host; API keys commonly live in either path or query."""
    match = re.match(r"^(https?://[^/\s?]+)", url or "")
    return f"{match.group(1)}/…" if match else "configured"


def with_backoff(fn, what: str):
    """Call fn(); on HTTP 429 wait and retry a few times. Free RPC and Jupiter tiers often
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
        legacy_rpc = os.getenv("RPC_URL") or ""
        if not legacy_rpc and self.helius_api_key:
            legacy_rpc = f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        self.rpc_urls = split_urls(os.getenv("RPC_URLS") or legacy_rpc)
        self.rpc_url = self.rpc_urls[0] if self.rpc_urls else ""
        explicit_ws = split_urls(os.getenv("RPC_WS_URLS") or os.getenv("RPC_WS_URL") or "")
        self.rpc_ws_urls = explicit_ws or [websocket_url(url) for url in self.rpc_urls]
        self.rpc_ws_url = self.rpc_ws_urls[0] if self.rpc_ws_urls else ""
        self.rpc_timeout_seconds = max(1.0, float(os.getenv("RPC_TIMEOUT_SECONDS", "8")))
        self.rpc_batch_size = min(50, max(1, int(os.getenv("RPC_BATCH_SIZE", "20"))))
        self.discovery_mode = os.getenv("DISCOVERY_MODE", "websocket").lower()
        self.discovery_catchup_seconds = max(5.0, float(os.getenv("DISCOVERY_CATCHUP_SECONDS", "30")))
        self.discovery_poll_limit = min(100, max(1, int(os.getenv("DISCOVERY_POLL_LIMIT", "10"))))
        self.discovery_timeout_seconds = max(
            1.0,
            float(os.getenv("DISCOVERY_TIMEOUT_SECONDS", os.getenv("HELIUS_POLL_TIMEOUT_SECONDS", "5"))),
        )
        self.provider_backoff_max_seconds = max(
            30.0,
            float(os.getenv("RPC_BACKOFF_MAX_SECONDS", os.getenv("HELIUS_POLL_BACKOFF_MAX_SECONDS", "900"))),
        )
        # Raw is portable across standard Solana providers. Helius parsing remains an explicit
        # opt-in compatibility mode for historical users, not a runtime requirement.
        self.transaction_history_mode = os.getenv("TRANSACTION_HISTORY_MODE", "raw").lower()
        self.raw_history_signature_limit = min(
            100, max(5, int(os.getenv("RAW_HISTORY_SIGNATURE_LIMIT", "40")))
        )
        self.raw_funder_signature_limit = min(
            self.raw_history_signature_limit,
            max(3, int(os.getenv("RAW_FUNDER_SIGNATURE_LIMIT", "10"))),
        )
        self.rpc_das_enabled = os.getenv("RPC_DAS_ENABLED", "0") == "1"
        self.das_mint_param = os.getenv("DAS_MINT_PARAM", "mint")
        self.jupiter_base = os.getenv("JUPITER_BASE_URL", "https://lite-api.jup.ag/swap/v1").rstrip("/")
        self.account_fraction = float(os.getenv("ACCOUNT_FRACTION", "0.10"))
        self.max_position_usd = float(os.getenv("MAX_POSITION_USD", "20"))
        self.min_position_usd = float(os.getenv("MIN_POSITION_USD", "5"))
        self.max_concurrent = int(os.getenv("MAX_CONCURRENT_POSITIONS", "5"))
        self.daily_loss_limit_usd = float(os.getenv("DAILY_LOSS_LIMIT_USD", "30"))
        self.take_profit = float(os.getenv("TAKE_PROFIT", "0.75"))
        self.stop_loss = float(os.getenv("STOP_LOSS", "0.30"))
        self.trailing_stop = float(os.getenv("TRAILING_STOP", "0"))  # fraction off peak; 0 disables
        self.moon_bag = min(0.5, max(0.0, float(os.getenv("MOON_BAG", "0"))))  # fraction kept at exit; 0 disables
        # Keep a bag only when the exit was profitable (a stop-loss remnant just rides to zero and
        # locks its rent), and sell a bag once it is worth MOON_BAG_TARGET_X times what was kept
        # (0 = hold forever; panic is then the only way out). Bags are re-quoted every
        # MOON_BAG_CHECK_SECONDS, not every loop, since they are off the Jupiter budget otherwise.
        self.moon_bag_winners_only = os.getenv("MOON_BAG_WINNERS_ONLY", "1") == "1"
        self.moon_bag_target_x = max(0.0, float(os.getenv("MOON_BAG_TARGET_X", "100")))
        self.moon_bag_check_seconds = float(os.getenv("MOON_BAG_CHECK_SECONDS", "60"))
        # A bag worth less than MIN_MOON_BAG_USD is not worth its own rent (0.002 SOL) and is
        # sold with the rest. A bag that has fallen to MOON_BAG_DEAD_PCT of the value it was kept
        # at is burned and its account closed: the rent is worth more than the tokens.
        self.min_moon_bag_usd = float(os.getenv("MIN_MOON_BAG_USD", "0.5"))
        self.moon_bag_dead_pct = float(os.getenv("MOON_BAG_DEAD_PCT", "5"))
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
        # Entry discovery and bundle analysis are best-effort; protecting money already in the
        # market is not.  Bound both sources of entry work so an active position is never starved
        # behind a burst of graduations.
        self.max_entries_per_cycle = max(1, int(os.getenv("MAX_ENTRIES_PER_CYCLE", "1")))
        self.max_price_impact_pct = float(os.getenv("MAX_PRICE_IMPACT_PCT", "5"))
        # A route existing is not enough: require that the just-quoted tokens can immediately
        # be sold back for most of the input. This is an executable liquidity check, not a UI badge.
        self.min_entry_round_trip_pct = percent_env("MIN_ENTRY_ROUND_TRIP_PCT", 80)
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
        # Multi-wallet bundle checks. The old .env example expressed percentages as fractions
        # (0.30 == 30%), so percent_env accepts both forms during rollout.
        self.max_bundle_slot_pct = percent_env("MAX_BUNDLE_SLOT_PCT", 30)
        self.max_cluster_pct = percent_env("MAX_CLUSTER_PCT", 30)
        self.max_ancestry_cluster_pct = percent_env("MAX_ANCESTRY_CLUSTER_PCT", 20)
        self.max_transfer_cluster_pct = percent_env("MAX_TRANSFER_CLUSTER_PCT", 12)
        self.max_coordinated_buy_pct = percent_env("MAX_COORDINATED_BUY_PCT", 20)
        self.max_repeat_cohort_pct = percent_env("MAX_REPEAT_COHORT_PCT", 12)
        self.max_dev_cluster_pct = percent_env("MAX_DEV_CLUSTER_PCT", 15)
        self.max_top10_wallet_pct = percent_env("MAX_TOP10_WALLET_PCT", 50)
        self.max_early_buy_pct = percent_env("MAX_EARLY_BUY_PCT", 30)
        self.min_funder_coverage_pct = percent_env("MIN_FUNDER_COVERAGE_PCT", 30)
        # Live free-provider evidence showed 20 concurrent address-history calls complete only
        # 20-50% before throttling. Keep lookup health visible, but do not demand a higher
        # completion percentage than the evidence threshold itself.
        self.min_funder_lookup_pct = percent_env("MIN_FUNDER_LOOKUP_PCT", 30)
        self.high_confidence_funder_coverage_pct = percent_env("HIGH_CONFIDENCE_FUNDER_COVERAGE_PCT", 60)
        self.partial_coverage_limit_multiplier = max(
            0.1, min(1.0, float(os.getenv("PARTIAL_COVERAGE_LIMIT_MULTIPLIER", "0.67")))
        )
        self.coordinated_window_slots = max(0, int(os.getenv("COORDINATED_WINDOW_SLOTS", "12")))
        self.coordinated_min_wallets = max(2, int(os.getenv("COORDINATED_MIN_WALLETS", "3")))
        self.bundle_log_only = os.getenv("BUNDLE_LOG_ONLY", "0") == "1"
        self.bundle_fail_closed = os.getenv("BUNDLE_FAIL_CLOSED", "1") == "1"
        self.bundle_lookup_timeout_ms = max(250, int(os.getenv("BUNDLE_LOOKUP_TIMEOUT_MS", "1500")))
        self.bundle_lookup_workers = min(12, max(1, int(os.getenv("BUNDLE_LOOKUP_WORKERS", "6"))))
        self.bundle_max_wallets = min(100, max(2, int(os.getenv("BUNDLE_MAX_WALLETS", "50"))))
        # Keep the broad sample for transfer/coordination/concentration checks, but trace
        # funding only for the largest wallets. Adding small holders must not mechanically
        # lower the funding coverage of the economically important sample.
        self.bundle_funder_max_wallets = min(
            self.bundle_max_wallets,
            max(2, int(os.getenv("BUNDLE_FUNDER_MAX_WALLETS", "20"))),
        )
        self.wallet_graph_cache_days = max(1.0, float(os.getenv("WALLET_GRAPH_CACHE_DAYS", "30")))
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
        if not self.migration_address:
            raise SystemExit("MIGRATION_ADDRESS is required")
        if not self.rpc_urls:
            raise SystemExit("Set RPC_URL or RPC_URLS (HELIUS_API_KEY is optional)")
        if self.discovery_mode not in ("websocket", "poll"):
            raise SystemExit("DISCOVERY_MODE must be 'websocket' or 'poll'")
        if self.transaction_history_mode not in ("raw", "helius"):
            raise SystemExit("TRANSACTION_HISTORY_MODE must be 'raw' or 'helius'")
        if self.transaction_history_mode == "helius" and not self.helius_api_key:
            raise SystemExit("TRANSACTION_HISTORY_MODE=helius requires HELIUS_API_KEY")
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
        "pending": [],
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
    text = re.sub(r"api-key=[^&\s]+", "api-key=…", str(exc))
    # Most non-Helius providers put the credential in the URL path. Never let an exception
    # copy that private endpoint into Railway logs.
    text = re.sub(r"(https?://[^/\s?]+)(?:/[^\s?]*)?(?:\?[^\s]*)?", r"\1/…", text)
    if "0x1771" in text or "'Custom': 6001" in text or '"Custom": 6001' in text or '"Custom":6001' in text:
        return "Jupiter 6001: slippage tolerance exceeded (price moved past tolerance between quote and execution)"
    if "0x1770" in text or "'Custom': 6000" in text or '"Custom": 6000' in text:
        return "Jupiter 6000: route no longer valid"
    return text[:240] + "…" if len(text) > 240 else text


def rpc_account_keys(tx: dict[str, Any]) -> list[str]:
    message = ((tx.get("transaction") or {}).get("message") or {})
    keys: list[str] = []
    for item in message.get("accountKeys") or []:
        keys.append(str(item.get("pubkey")) if isinstance(item, dict) else str(item))
    return keys


def rpc_instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    message = ((tx.get("transaction") or {}).get("message") or {})
    instructions = list(message.get("instructions") or [])
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    return [item for item in instructions if isinstance(item, dict)]


def candidate_mints_from_rpc_transaction(tx: dict[str, Any]) -> list[str]:
    """Extract the launched mint from a standard jsonParsed getTransaction response."""
    found: set[str] = set()
    meta = tx.get("meta") or {}
    for balance in [*(meta.get("preTokenBalances") or []), *(meta.get("postTokenBalances") or [])]:
        mint = balance.get("mint")
        if mint and mint not in KNOWN_QUOTES:
            found.add(mint)
    for instruction in rpc_instructions(tx):
        parsed = instruction.get("parsed") or {}
        info = parsed.get("info") or {}
        mint = info.get("mint")
        if mint and mint not in KNOWN_QUOTES:
            found.add(mint)
    return sorted(found)


def normalize_rpc_transaction(tx: dict[str, Any], signature: str = "") -> dict[str, Any]:
    """Convert standard jsonParsed transaction data into the small normalized shape used by
    the bundle detector. This intentionally reconstructs only evidence we consume."""
    keys = rpc_account_keys(tx)
    meta = tx.get("meta") or {}
    fee_payer = keys[0] if keys else None
    native_transfers: list[dict[str, Any]] = []
    for instruction in rpc_instructions(tx):
        parsed = instruction.get("parsed") or {}
        info = parsed.get("info") or {}
        if instruction.get("program") != "system" or parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue
        source = info.get("source")
        destination = info.get("destination")
        lamports = info.get("lamports")
        if source and destination and lamports is not None:
            native_transfers.append(
                {"fromUserAccount": source, "toUserAccount": destination, "amount": int(lamports)}
            )

    def token_balances(rows: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, Any]]:
        out: dict[tuple[int, str], dict[str, Any]] = {}
        for row in rows:
            mint = row.get("mint")
            index = row.get("accountIndex")
            token = row.get("uiTokenAmount") or {}
            if mint is None or index is None:
                continue
            out[(int(index), mint)] = {
                "amount": int(token.get("amount") or 0),
                "decimals": int(token.get("decimals") or 0),
                "owner": row.get("owner"),
            }
        return out

    before = token_balances(meta.get("preTokenBalances") or [])
    after = token_balances(meta.get("postTokenBalances") or [])
    changes: list[dict[str, Any]] = []
    for key in set(before) | set(after):
        pre = before.get(key) or {"amount": 0, "decimals": 0, "owner": None}
        post = after.get(key) or {"amount": 0, "decimals": pre["decimals"], "owner": pre["owner"]}
        delta = int(post["amount"]) - int(pre["amount"])
        if delta:
            changes.append(
                {
                    "mint": key[1],
                    "delta": delta,
                    "decimals": int(post.get("decimals") or pre.get("decimals") or 0),
                    "owner": post.get("owner") or pre.get("owner"),
                }
            )
    negative_owners: dict[str, list[str]] = {}
    for change in changes:
        if change["delta"] < 0 and change.get("owner"):
            negative_owners.setdefault(change["mint"], []).append(change["owner"])
    token_transfers: list[dict[str, Any]] = []
    for change in changes:
        if change["delta"] <= 0 or not change.get("owner"):
            continue
        senders = list(dict.fromkeys(negative_owners.get(change["mint"], [])))
        token_transfers.append(
            {
                "mint": change["mint"],
                "fromUserAccount": senders[0] if len(senders) == 1 else None,
                "toUserAccount": change["owner"],
                "rawTokenAmount": {
                    "tokenAmount": str(change["delta"]),
                    "decimals": change["decimals"],
                },
            }
        )
    return {
        "signature": signature,
        "timestamp": tx.get("blockTime"),
        "blockTime": tx.get("blockTime"),
        "slot": tx.get("slot"),
        "feePayer": fee_payer,
        "type": "SWAP" if set(keys) & DEX_PROGRAMS else "TRANSFER",
        "nativeTransfers": native_transfers,
        "tokenTransfers": token_transfers,
    }


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
    bundle: dict[str, Any] | None = None,
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
    if bundle is not None:
        if not bundle.get("complete", False) and cfg.bundle_fail_closed:
            detail = bundle.get("error") or "required holder/funding history was incomplete"
            return f"bundle data unavailable ({detail})"
        coverage = bundle.get("funder_coverage_pct")
        confidence_multiplier = 1.0
        if coverage is not None and coverage < cfg.high_confidence_funder_coverage_pct:
            confidence_multiplier = cfg.partial_coverage_limit_multiplier
        checks = (
            ("bundle_slot_pct", cfg.max_bundle_slot_pct, "same-slot wallets"),
            ("cluster_pct", cfg.max_cluster_pct, "connected funding cluster"),
            ("ancestry_cluster_pct", cfg.max_ancestry_cluster_pct, "shared two-hop funding cluster"),
            ("transfer_cluster_pct", cfg.max_transfer_cluster_pct, "token-transfer cluster"),
            ("coordinated_buy_pct", cfg.max_coordinated_buy_pct, "coordinated wallet burst"),
            ("repeat_cohort_pct", cfg.max_repeat_cohort_pct, "repeat-launch wallet cohort"),
            ("dev_cluster_pct", cfg.max_dev_cluster_pct, "creator-linked wallet cluster"),
            ("top10_wallet_pct", cfg.max_top10_wallet_pct, "top ten wallets"),
            ("early_buy_pct", cfg.max_early_buy_pct, "first three slots"),
        )
        for key, limit, label in checks:
            value = bundle.get(key)
            effective_limit = limit * confidence_multiplier
            if value is not None and limit > 0 and value > effective_limit:
                confidence = " under partial-coverage rules" if confidence_multiplier < 1 else ""
                return f"{label} hold {value:.1f}% of supply > {effective_limit:.1f}%{confidence}"
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
        self._active_endpoint = 0
        self.wallet_graph_cache: dict[str, Any] = {"version": 1, "funders": {}, "appearances": {}}
        try:
            loaded = json.loads(WALLET_GRAPH_CACHE_FILE.read_text())
            if isinstance(loaded, dict) and isinstance(loaded.get("funders"), dict):
                self.wallet_graph_cache = loaded
                self.wallet_graph_cache.setdefault("appearances", {})
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def call(self, method: str, params: Any, timeout: float | None = None) -> Any:
        return with_backoff(lambda: self._call(method, params, timeout), method)

    def _endpoint_order(self) -> list[tuple[int, str]]:
        total = len(self.cfg.rpc_urls)
        return [((self._active_endpoint + offset) % total, self.cfg.rpc_urls[(self._active_endpoint + offset) % total])
                for offset in range(total)]

    def _post(self, endpoint: str, payload: Any, timeout: float | None = None) -> Any:
        resp = self.session.post(
            endpoint,
            json=payload,
            timeout=timeout or self.cfg.rpc_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()

    def _call(self, method: str, params: Any, timeout: float | None = None) -> Any:
        last_error: BaseException | None = None
        for index, endpoint in self._endpoint_order():
            try:
                body = self._post(
                    endpoint,
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout,
                )
                if "error" in body:
                    error = RuntimeError(f"RPC {method}: {body['error']}")
                    if is_provider_unavailable(error):
                        raise error
                    raise error
                self._active_endpoint = index
                return body["result"]
            except Exception as exc:
                last_error = exc
                if not is_provider_unavailable(exc):
                    raise
        assert last_error is not None
        raise last_error

    def batch_call(self, calls: list[tuple[str, Any]], timeout: float | None = None) -> list[Any]:
        """Execute standard JSON-RPC batches while preserving input order and provider failover."""
        if not calls:
            return []
        results: list[Any] = []
        for start in range(0, len(calls), self.cfg.rpc_batch_size):
            chunk = calls[start : start + self.cfg.rpc_batch_size]
            payload = [
                {"jsonrpc": "2.0", "id": offset + 1, "method": method, "params": params}
                for offset, (method, params) in enumerate(chunk)
            ]
            body = with_backoff(lambda: self._batch_post(payload, timeout), "RPC batch")
            by_id = {int(item.get("id", 0)): item for item in body if isinstance(item, dict)}
            for offset, (method, _params) in enumerate(chunk):
                item = by_id.get(offset + 1) or {}
                if "error" in item:
                    raise RuntimeError(f"RPC {method}: {item['error']}")
                results.append(item.get("result"))
        return results

    def _batch_post(self, payload: list[dict[str, Any]], timeout: float | None = None) -> list[dict[str, Any]]:
        last_error: BaseException | None = None
        for index, endpoint in self._endpoint_order():
            try:
                body = self._post(endpoint, payload, timeout)
                if not isinstance(body, list):
                    raise RuntimeError(f"RPC batch returned {str(body)[:120]}")
                provider_errors = [
                    RuntimeError(f"RPC batch: {item['error']}")
                    for item in body
                    if isinstance(item, dict)
                    and "error" in item
                    and is_provider_unavailable(RuntimeError(f"RPC batch: {item['error']}"))
                ]
                if provider_errors:
                    # Providers sometimes return HTTP 200 while the JSON-RPC batch says it is
                    # throttled or unhealthy. Treat that exactly like an HTTP 429 and fail over.
                    raise provider_errors[0]
                self._active_endpoint = index
                return body
            except Exception as exc:
                last_error = exc
                if not is_provider_unavailable(exc):
                    raise
        assert last_error is not None
        raise last_error

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

    def token_account_balance(self, account: str) -> int:
        """Confirmed raw balance for one token account.

        A freshly confirmed swap can be newer than a default/finalized account listing.  Rent
        reclaim must use this targeted confirmed read or it can try to burn the pre-sell balance.
        """
        value = self.call("getTokenAccountBalance", [account, {"commitment": "confirmed"}])["value"]
        return int(value["amount"])

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

    def plain_wallet_holders(
        self, mint: str, exclude: set[str] | None = None, limit: int = 20
    ) -> list[tuple[str, int]]:
        """Largest plain-wallet holders, combining multiple token accounts per owner.

        Optional Metaplex DAS is used first because standard getTokenLargestAccounts is
        hard-capped at 20. DAS is disabled by default so unsupported extension calls do not
        burn free-provider quota; the portable top-20 path remains the default.
        """
        exclude = exclude or set()
        try:
            if not self.cfg.rpc_das_enabled:
                raise RuntimeError("DAS disabled")
            # DAS does not promise balance ordering, so inspect the full first page and sort
            # locally rather than asking it for only N arbitrary accounts.
            das = self.call(
                "getTokenAccounts",
                {self.cfg.das_mint_param: mint, "page": 1, "limit": 1000},
            ) or {}
            rows = das.get("token_accounts") or das.get("tokenAccounts") or []
            totals: dict[str, int] = {}
            for row in rows:
                owner = row.get("owner")
                amount = row.get("amount")
                if owner and owner not in exclude and amount is not None:
                    totals[owner] = totals.get(owner, 0) + int(amount)
            if totals:
                ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]
                owner_accounts = self.call(
                    "getMultipleAccounts", [[owner for owner, _ in ordered], {"encoding": "base64"}]
                ).get("value") or []
                wallets = [
                    (owner, amount)
                    for (owner, amount), acct in zip(ordered, owner_accounts)
                    if (acct["owner"] if acct else SYSTEM_PROGRAM) == SYSTEM_PROGRAM
                ]
                if wallets:
                    return wallets
        except Exception:
            # DAS availability varies by provider/plan; retain the standard 20-account path.
            pass
        largest = self.call("getTokenLargestAccounts", [mint]).get("value") or []
        if not largest:
            return []
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
            return []
        owner_accounts = self.call(
            "getMultipleAccounts", [[owner for owner, _ in owners], {"encoding": "base64"}]
        ).get("value") or []
        totals: dict[str, int] = {}
        for (owner, amount), acct in zip(owners, owner_accounts):
            program = acct["owner"] if acct else SYSTEM_PROGRAM  # unfunded wallet: still a wallet
            if program != SYSTEM_PROGRAM or owner in exclude:
                continue
            totals[owner] = totals.get(owner, 0) + amount
        return sorted(totals.items(), key=lambda row: row[1], reverse=True)[:limit]

    def top_wallet_holder(self, mint: str, exclude: set[str] | None = None) -> tuple[str, int] | None:
        """(owner, raw amount) of the largest plain-wallet holder."""
        holders = self.plain_wallet_holders(mint, exclude, 1)
        return holders[0] if holders else None

    def transaction(self, signature: str) -> dict[str, Any] | None:
        return self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )

    def raw_transactions(self, address: str, **params: Any) -> list[dict[str, Any]]:
        """Portable address history built from standard signatures + batched transactions."""
        requested = int(params.get("limit") or self.cfg.raw_history_signature_limit)
        limit = min(self.cfg.raw_history_signature_limit, max(1, requested))
        options: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if params.get("before-signature"):
            options["before"] = params["before-signature"]
        signatures = self.call("getSignaturesForAddress", [address, options]) or []
        gte = int(params.get("gte-time") or 0)
        lte = int(params.get("lte-time") or 2**63 - 1)
        rows = [
            row for row in signatures
            if row.get("signature") and not row.get("err") and gte <= int(row.get("blockTime") or 0) <= lte
        ]
        calls = [
            (
                "getTransaction",
                [
                    row["signature"],
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            for row in rows
        ]
        bodies = self.batch_call(calls, timeout=self.cfg.bundle_lookup_timeout_ms / 1000)
        normalized = [
            normalize_rpc_transaction(body, row["signature"])
            for row, body in zip(rows, bodies)
            if isinstance(body, dict)
        ]
        if params.get("sort-order") == "asc":
            normalized.reverse()
        return normalized

    def enhanced_transactions(self, address: str, **params: Any) -> list[dict[str, Any]]:
        """Normalized history for the bundle gate; raw RPC by default, Helius only by opt-in."""
        if self.cfg.transaction_history_mode == "raw":
            return self.raw_transactions(address, **params)
        url = f"https://api-mainnet.helius-rpc.com/v0/addresses/{address}/transactions"
        query = {"api-key": self.cfg.helius_api_key, "commitment": "confirmed", **params}
        timeout = self.cfg.bundle_lookup_timeout_ms / 1000
        response = requests.get(url, params=query, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list):
            raise RuntimeError(f"Helius history returned {str(body)[:160]}")
        return body

    def origin_funder(self, wallet: str, before_ts: float) -> str | None:
        """Oldest meaningful inbound SOL sender visible in the bounded pre-graduation sample."""
        transactions = self.enhanced_transactions(
            wallet,
            **{
                "sort-order": "asc",
                "lte-time": int(before_ts),
                "limit": self.cfg.raw_funder_signature_limit
                if self.cfg.transaction_history_mode == "raw"
                else 25,
            },
        )
        for tx in transactions:
            for transfer in tx.get("nativeTransfers") or []:
                sender = transfer.get("fromUserAccount")
                recipient = transfer.get("toUserAccount")
                amount = int(transfer.get("amount") or 0)
                if recipient == wallet and sender and sender != wallet and amount >= 100_000:
                    return sender
        return None

    def cached_origin_funder(self, wallet: str, before_ts: float) -> str | None:
        """Origin funder with a persistent TTL cache shared across token analyses."""
        entries = self.wallet_graph_cache.setdefault("funders", {})
        cached = entries.get(wallet)
        now = now_ts()
        if isinstance(cached, dict):
            age = now - float(cached.get("checked_at") or 0)
            ttl = self.cfg.wallet_graph_cache_days * 86400 if cached.get("funder") else 3600
            if age <= ttl:
                return cached.get("funder")
        funder = self.origin_funder(wallet, before_ts)
        entries[wallet] = {"funder": funder, "checked_at": now}
        return funder

    def save_wallet_graph_cache(self) -> None:
        try:
            # Bound a long-running Railway volume: retain the most recently checked wallets.
            funders = self.wallet_graph_cache.setdefault("funders", {})
            if len(funders) > 10_000:
                newest = sorted(
                    funders.items(), key=lambda item: float(item[1].get("checked_at") or 0), reverse=True
                )[:10_000]
                self.wallet_graph_cache["funders"] = dict(newest)
            appearances = self.wallet_graph_cache.setdefault("appearances", {})
            if len(appearances) > 10_000:
                keep = set(self.wallet_graph_cache["funders"])
                self.wallet_graph_cache["appearances"] = {
                    wallet: history for wallet, history in appearances.items() if wallet in keep
                }
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = WALLET_GRAPH_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.wallet_graph_cache, separators=(",", ":")))
            os.replace(tmp, WALLET_GRAPH_CACHE_FILE)
        except OSError as exc:
            log(f"WARN wallet graph cache could not be saved ({describe_error(exc)})")

    def _lookup_funders(
        self, wallets: set[str], before_ts: float
    ) -> tuple[dict[str, str | None], set[str]]:
        """Return funders and wallets whose lookup actually completed.

        A completed lookup with no identifiable funder is not an API failure. The old
        representation collapsed both outcomes to ``None`` and hid provider timeouts.
        """
        results: dict[str, str | None] = {wallet: None for wallet in wallets}
        completed: set[str] = set()
        if not wallets:
            return results, completed
        timeout = self.cfg.bundle_lookup_timeout_ms / 1000
        workers = min(self.cfg.bundle_lookup_workers, len(wallets))
        total_timeout = timeout * ((len(wallets) + workers - 1) // workers) + 0.5
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = {pool.submit(self.cached_origin_funder, wallet, before_ts): wallet for wallet in wallets}
        try:
            for future in as_completed(futures, timeout=total_timeout):
                wallet = futures[future]
                try:
                    results[wallet] = future.result()
                    completed.add(wallet)
                except Exception:
                    results[wallet] = None
        except TimeoutError:
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return results, completed

    def bundle_snapshot(
        self,
        mint: str,
        supply_ui: float,
        decimals: int,
        created_ts: float,
        graduated_ts: float,
        exclude: set[str] | None = None,
    ) -> dict[str, Any]:
        """Build the live equivalent of a Bubblemap from the largest plain-wallet holders."""
        holders = self.plain_wallet_holders(mint, exclude, self.cfg.bundle_max_wallets)
        if len(holders) < 2:
            return {"complete": False, "error": "fewer than two plain-wallet holders returned"}
        wallet_amounts = {wallet: raw / (10 ** decimals) for wallet, raw in holders}
        holder_set = set(wallet_amounts)
        transactions = self.enhanced_transactions(
            mint,
            **{
                "sort-order": "asc",
                "gte-time": int(created_ts) - 2,
                "lte-time": int(graduated_ts + self.cfg.entry_delay_seconds) + 2,
                "limit": 100,
            },
        )
        buys: list[dict[str, Any]] = []
        transfer_edges: list[tuple[str, str]] = []
        creator = None
        create_slot = None
        for tx in transactions:
            slot = tx.get("slot")
            if slot is None:
                continue
            if create_slot is None or int(slot) < create_slot:
                create_slot = int(slot)
                creator = tx.get("feePayer") or creator
            native_payers = {
                transfer.get("fromUserAccount")
                for transfer in tx.get("nativeTransfers") or []
                if int(transfer.get("amount") or 0) >= 100_000
            }
            amounts: dict[str, float] = {}
            for transfer in tx.get("tokenTransfers") or []:
                wallet = transfer.get("toUserAccount")
                if transfer.get("mint") != mint or wallet not in holder_set:
                    continue
                raw_field = transfer.get("rawTokenAmount")
                if isinstance(raw_field, dict):
                    raw = float(raw_field.get("tokenAmount") or 0)
                    transfer_decimals = int(raw_field.get("decimals", decimals))
                    amount = raw / (10 ** transfer_decimals)
                else:
                    raw = float(transfer.get("tokenAmount") or raw_field or 0)
                    transfer_decimals = int(transfer.get("decimals", decimals))
                    amount = raw / (10 ** transfer_decimals) if raw_field is not None else raw
                # Legacy Enhanced Transactions reports tokenAmount in UI units; Parsed Events uses
                # rawTokenAmount. Prefer the raw field when it is present.
                amounts[wallet] = amounts.get(wallet, 0.0) + amount
                sender = transfer.get("fromUserAccount")
                # A swap links every buyer to the AMM vault and would create a giant false
                # cluster. Only retain unpaid wallet-to-wallet distributions.
                if (
                    sender
                    and sender != wallet
                    and wallet not in native_payers
                    and str(tx.get("type") or "").upper() != "SWAP"
                ):
                    transfer_edges.append((sender, wallet))
            buys.extend({"wallet": wallet, "slot": int(slot), "amount": amount} for wallet, amount in amounts.items())
        if not buys or create_slot is None:
            return {"complete": False, "error": "mint purchase history was empty or not yet indexed"}

        funding_wallets = {
            wallet
            for wallet, _amount in sorted(
                wallet_amounts.items(), key=lambda item: item[1], reverse=True
            )[: self.cfg.bundle_funder_max_wallets]
        }
        funders, funder_lookups_completed = self._lookup_funders(funding_wallets, graduated_ts)
        first_hop = {funder for funder in funders.values() if funder and funder not in CEX_FUNDERS}
        second_hop, _second_hop_completed = self._lookup_funders(first_hop, graduated_ts)
        ancestry = {
            wallet: [node for node in (funder, second_hop.get(funder) if funder else None) if node]
            for wallet, funder in funders.items()
        }
        covered_amount = sum(wallet_amounts[w] for w, funder in funders.items() if funder)
        tracked_amount = sum(wallet_amounts[w] for w in funding_wallets)
        coverage_pct = covered_amount / tracked_amount * 100 if tracked_amount else 0.0
        completed_amount = sum(wallet_amounts[w] for w in funder_lookups_completed)
        lookup_pct = completed_amount / tracked_amount * 100 if tracked_amount else 0.0
        clusters = cluster_wallets(funders)
        largest_cluster_pct, cluster_members = cluster_supply_pct(clusters, wallet_amounts, supply_ui)
        ancestry_groups = ancestry_clusters(ancestry)
        ancestry_cluster_pct, ancestry_members = cluster_supply_pct(ancestry_groups, wallet_amounts, supply_ui)
        transfer_groups = transfer_clusters(transfer_edges, holder_set)
        transfer_cluster_pct, transfer_members = cluster_supply_pct(transfer_groups, wallet_amounts, supply_ui)
        appearances = self.wallet_graph_cache.setdefault("appearances", {})
        previous_launches = {wallet: list(appearances.get(wallet) or []) for wallet in holder_set}
        repeat_groups = ancestry_clusters(previous_launches, set())
        repeat_cohort_pct, repeat_members = cluster_supply_pct(repeat_groups, wallet_amounts, supply_ui)
        creator_ancestry = set([creator] if creator else [])
        if creator:
            try:
                creator_funder = self.cached_origin_funder(creator, graduated_ts)
                if creator_funder and creator_funder not in CEX_FUNDERS:
                    creator_ancestry.add(creator_funder)
                    creator_parent = self.cached_origin_funder(creator_funder, graduated_ts)
                    if creator_parent and creator_parent not in CEX_FUNDERS:
                        creator_ancestry.add(creator_parent)
            except Exception:
                pass  # optional creator ancestry must not invalidate otherwise complete evidence
        dev_members = [
            wallet for wallet, path in ancestry.items()
            if set([wallet, *path]) & creator_ancestry
        ]
        if not dev_members:
            dev_members = related_holder_wallets(funders, creator)
        for wallet in holder_set:
            history = [seen for seen in appearances.get(wallet, []) if seen != mint]
            appearances[wallet] = [*history[-24:], mint]
        self.save_wallet_graph_cache()
        same_slot_pct, bundle_slot, bundle_wallets = bundle_slot_pct(buys, supply_ui)
        coordinated_pct, coordinated_slot, coordinated_wallets = coordinated_buy_pct(
            buys, supply_ui, self.cfg.coordinated_window_slots, self.cfg.coordinated_min_wallets
        )
        enough_lookup = lookup_pct >= self.cfg.min_funder_lookup_pct
        enough_coverage = coverage_pct >= self.cfg.min_funder_coverage_pct
        complete = enough_lookup and enough_coverage
        if not enough_lookup:
            error = f"funder lookups completed for {lookup_pct:.1f}% of funding sample"
        elif not enough_coverage:
            error = f"funder coverage {coverage_pct:.1f}%"
        else:
            error = None
        if not complete:
            confidence = "insufficient"
        elif coverage_pct >= self.cfg.high_confidence_funder_coverage_pct:
            confidence = "high"
        else:
            confidence = "partial"
        return {
            "complete": complete,
            "error": error,
            "bundle_slot_pct": same_slot_pct,
            "bundle_slot": bundle_slot,
            "bundle_wallets": bundle_wallets,
            "cluster_pct": largest_cluster_pct,
            "cluster_wallets": cluster_members,
            "ancestry_cluster_pct": ancestry_cluster_pct,
            "ancestry_cluster_wallets": ancestry_members,
            "transfer_cluster_pct": transfer_cluster_pct,
            "transfer_cluster_wallets": transfer_members,
            "coordinated_buy_pct": coordinated_pct,
            "coordinated_slot": coordinated_slot,
            "coordinated_wallets": coordinated_wallets,
            "repeat_cohort_pct": repeat_cohort_pct,
            "repeat_cohort_wallets": repeat_members,
            "dev_cluster_pct": wallets_supply_pct(dev_members, wallet_amounts, supply_ui),
            "top10_wallet_pct": top_wallets_supply_pct(wallet_amounts, supply_ui),
            "early_buy_pct": early_buy_pct(buys, supply_ui, create_slot),
            "funder_coverage_pct": coverage_pct,
            "funder_lookup_pct": lookup_pct,
            "funder_sample_count": len(funding_wallets),
            "holder_sample_count": len(holders),
            "bundle_confidence": confidence,
            "creator": creator,
        }

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


class MigrationStream:
    """Background standard-RPC logsSubscribe client.

    It only queues signatures; transaction decoding stays in the executor's entry phase so the
    exit-first ordering remains intact. Periodic HTTP catch-up covers disconnect gaps.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.signatures: queue.Queue[str] = queue.Queue(maxsize=500)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.connected = False

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="migration-stream", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        backoff = 1.0
        endpoint_index = 0
        while not self.stop_event.is_set():
            endpoint = self.cfg.rpc_ws_urls[endpoint_index % len(self.cfg.rpc_ws_urls)]
            endpoint_index += 1
            conn = None
            try:
                conn = websocket.create_connection(
                    endpoint,
                    timeout=self.cfg.discovery_timeout_seconds,
                    enable_multithread=True,
                )
                conn.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [self.cfg.migration_address]},
                                {"commitment": "confirmed"},
                            ],
                        }
                    )
                )
                response = json.loads(conn.recv())
                if "error" in response:
                    raise RuntimeError(f"logsSubscribe: {response['error']}")
                self.connected = True
                backoff = 1.0
                while not self.stop_event.is_set():
                    try:
                        body = json.loads(conn.recv())
                    except websocket.WebSocketTimeoutException:
                        continue
                    value = (((body.get("params") or {}).get("result") or {}).get("value") or {})
                    signature = value.get("signature")
                    if not signature or value.get("err"):
                        continue
                    try:
                        self.signatures.put_nowait(signature)
                    except queue.Full:
                        # Catch-up polling is authoritative; dropping the oldest live hint is safe.
                        try:
                            self.signatures.get_nowait()
                        except queue.Empty:
                            pass
                        self.signatures.put_nowait(signature)
            except Exception as exc:
                self.connected = False
                log(f"WARN migration WebSocket disconnected: {describe_error(exc)}; retrying in {backoff:.0f}s")
                self.stop_event.wait(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                self.connected = False
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def drain(self, limit: int = 50) -> list[str]:
        out: list[str] = []
        while len(out) < limit:
            try:
                out.append(self.signatures.get_nowait())
            except queue.Empty:
                break
        return out


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
        self.migration_stream: MigrationStream | None = None
        self._next_discovery_catchup = 0.0
        # Keep pending entries in the persisted state.  A deploy should not silently forget the
        # queue and then rediscover/process an unbounded burst before checking open positions.
        self.pending: list[dict[str, Any]] = self.state.setdefault("pending", [])
        self._prune_stale_pending()

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
    def note_provider_rate_limit(self, minimum_seconds: float = 30.0) -> float:
        """Open an exponentially increasing circuit breaker for entry/discovery provider work.

        Position monitoring and exits deliberately ignore this breaker and can fail over across
        RPC_URLS. Only new-entry work is paused when every provider is unavailable.
        """
        current = float(getattr(self, "_provider_backoff_seconds", 30.0))
        delay = min(self.cfg.provider_backoff_max_seconds, max(minimum_seconds, current))
        self._provider_cooldown_until = max(
            float(getattr(self, "_provider_cooldown_until", 0.0)), now_ts() + delay
        )
        self._provider_backoff_seconds = min(self.cfg.provider_backoff_max_seconds, delay * 2)
        return delay

    # Compatibility for callers/tests deployed during the Helius-only rollout.
    note_helius_rate_limit = note_provider_rate_limit

    def _queue_graduation(self, signature: str, timestamp_hint: float | None = None) -> None:
        if not signature or signature in self.state["seen_signatures"]:
            return
        tx = self.rpc.transaction(signature)
        if not tx:
            return  # confirmed data may lag briefly; periodic catch-up will retry it
        timestamp = tx.get("blockTime") or timestamp_hint
        if not timestamp:
            return
        self.state["seen_signatures"].append(signature)
        mints = candidate_mints_from_rpc_transaction(tx)
        if len(mints) != 1:
            if mints:
                self.skip(
                    ",".join(mints),
                    f"ambiguous graduation tx {signature[:16]}… ({len(mints)} candidate mints)",
                )
            return
        mint = mints[0]
        already_queued = any(p.get("mint") == mint for p in self.pending)
        already_owned = any(p.get("mint") == mint for p in self.state.get("positions", []))
        if already_queued or already_owned:
            return
        age = now_ts() - int(timestamp)
        if age > self.cfg.max_entry_age_seconds:
            self.skip(mint, f"graduation too old at detection ({age:.0f}s)")
            return
        enter_at = int(timestamp) + self.cfg.entry_delay_seconds
        self.pending.append({"mint": mint, "graduated_ts": int(timestamp), "enter_at": enter_at})
        log(
            f"DETECTED graduation {mint} (age {age:.0f}s, "
            f"entering at +{self.cfg.entry_delay_seconds:.0f}s)"
        )

    def poll_graduations(self) -> None:
        if now_ts() < float(getattr(self, "_provider_cooldown_until", 0.0)):
            return
        signatures = self.migration_stream.drain() if self.migration_stream else []
        try:
            for signature in signatures:
                self._queue_graduation(signature)
            if now_ts() < self._next_discovery_catchup:
                return
            self._next_discovery_catchup = now_ts() + self.cfg.discovery_catchup_seconds
            rows = self.rpc.call(
                "getSignaturesForAddress",
                [
                    self.cfg.migration_address,
                    {"limit": self.cfg.discovery_poll_limit, "commitment": "confirmed"},
                ],
                timeout=self.cfg.discovery_timeout_seconds,
            ) or []
            for row in reversed(rows):
                if not row.get("err"):
                    self._queue_graduation(row.get("signature") or "", row.get("blockTime"))
        except Exception as exc:
            if is_rate_limited(exc):
                delay = self.note_provider_rate_limit()
                log(f"WARN RPC discovery rate limited; pausing new-entry work for {delay:.0f}s")
            else:
                log(f"WARN RPC discovery failed: {describe_error(exc)}")
            return
        self._provider_backoff_seconds = 30.0

    def _prune_stale_pending(self) -> None:
        """Drop entry work that became unsafe while the process was stopped."""
        cutoff = now_ts()
        keep = []
        for item in self.pending:
            if cutoff > float(item.get("enter_at", 0)) + self.cfg.max_entry_lateness_seconds:
                self.skip(item.get("mint", "unknown"), "stale after restart")
            else:
                keep.append(item)
        self.pending[:] = keep

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
    ) -> tuple[float | None, float | None, float | None, str | None, dict[str, Any]]:
        """Entry metadata plus a mandatory multi-wallet bundle snapshot."""
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
        bundle: dict[str, Any]
        if not supply_ui or decimals is None:
            bundle = {"complete": False, "error": "token supply unavailable"}
        elif curve_age is None:
            bundle = {"complete": False, "error": "creation time unavailable"}
        else:
            try:
                exclude = {self.wallet.pubkey} if self.wallet else set()
                bundle = self.rpc.bundle_snapshot(
                    mint,
                    supply_ui,
                    decimals,
                    graduated_ts - curve_age,
                    graduated_ts,
                    exclude,
                )
            except Exception as exc:
                bundle = {"complete": False, "error": describe_error(exc)}
                log(f"WARN {mint}: bundle snapshot unavailable ({describe_error(exc)})")
        return market_cap, curve_age, top_holder_pct, top_holder, bundle

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
        market_cap, curve_age, top_holder_pct, top_holder, bundle = self.entry_metadata(
            mint, item["graduated_ts"], size_usd, tokens
        )
        guard = entry_guard_reason(
            self.cfg, item["graduated_ts"], now_ts(), impact, market_cap, curve_age, top_holder_pct
        )
        if guard:
            if top_holder and "top wallet" in guard:
                guard += f" [{top_holder}]"
            self.skip(mint, guard)
            return
        bundle_guard = entry_guard_reason(
            self.cfg,
            item["graduated_ts"],
            now_ts(),
            impact,
            market_cap,
            curve_age,
            top_holder_pct,
            bundle,
        )
        summary = " ".join(
            f"{key.removesuffix('_pct')}={bundle[key]:.1f}%"
            for key in (
                "bundle_slot_pct",
                "cluster_pct",
                "ancestry_cluster_pct",
                "transfer_cluster_pct",
                "coordinated_buy_pct",
                "repeat_cohort_pct",
                "dev_cluster_pct",
                "top10_wallet_pct",
                "early_buy_pct",
                "funder_coverage_pct",
                "funder_lookup_pct",
            )
            if bundle.get(key) is not None
        )
        if bundle.get("holder_sample_count") is not None:
            summary += (
                f" funders={bundle.get('funder_sample_count', 0)}/{bundle['holder_sample_count']}"
                f" confidence={bundle.get('bundle_confidence', 'unknown')}"
            )
        log(f"BUNDLE {mint}: {summary or bundle.get('error', 'no metrics')}")
        if bundle_guard:
            if self.cfg.bundle_log_only:
                log(f"WARN {mint}: bundle log-only would skip: {bundle_guard}")
            else:
                self.skip(mint, bundle_guard)
                return
        # Honeypot/liquidity guard: a token must not only have a sell route, but that route
        # must return most of the proposed input immediately. This catches routes whose visible
        # price is not backed by executable two-way liquidity.
        try:
            reverse_quote = self.jup.quote(mint, WSOL, tokens)
        except Exception as exc:
            self.skip(mint, f"no sell route (possible honeypot): {exc}")
            return
        round_trip_pct = int(reverse_quote["outAmount"]) / lamports * 100 if lamports > 0 else 0.0
        if self.cfg.min_entry_round_trip_pct > 0 and round_trip_pct < self.cfg.min_entry_round_trip_pct:
            self.skip(
                mint,
                f"round-trip liquidity returns {round_trip_pct:.1f}% < "
                f"{self.cfg.min_entry_round_trip_pct:.1f}% of proposed buy",
            )
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
                "entry_round_trip_pct": round(round_trip_pct, 1),
                "entry_market_cap_usd": round(market_cap) if market_cap else None,
                "entry_curve_age_seconds": round(curve_age) if curve_age is not None else None,
                "entry_top_holder_pct": round(top_holder_pct, 1) if top_holder_pct is not None else None,
                "entry_bundle_slot_pct": round(bundle["bundle_slot_pct"], 1) if bundle.get("bundle_slot_pct") is not None else None,
                "entry_cluster_pct": round(bundle["cluster_pct"], 1) if bundle.get("cluster_pct") is not None else None,
                "entry_ancestry_cluster_pct": round(bundle["ancestry_cluster_pct"], 1) if bundle.get("ancestry_cluster_pct") is not None else None,
                "entry_transfer_cluster_pct": round(bundle["transfer_cluster_pct"], 1) if bundle.get("transfer_cluster_pct") is not None else None,
                "entry_coordinated_buy_pct": round(bundle["coordinated_buy_pct"], 1) if bundle.get("coordinated_buy_pct") is not None else None,
                "entry_repeat_cohort_pct": round(bundle["repeat_cohort_pct"], 1) if bundle.get("repeat_cohort_pct") is not None else None,
                "entry_dev_cluster_pct": round(bundle["dev_cluster_pct"], 1) if bundle.get("dev_cluster_pct") is not None else None,
                "entry_top10_wallet_pct": round(bundle["top10_wallet_pct"], 1) if bundle.get("top10_wallet_pct") is not None else None,
                "entry_early_buy_pct": round(bundle["early_buy_pct"], 1) if bundle.get("early_buy_pct") is not None else None,
                "entry_funder_coverage_pct": round(bundle["funder_coverage_pct"], 1) if bundle.get("funder_coverage_pct") is not None else None,
                "entry_funder_lookup_pct": round(bundle["funder_lookup_pct"], 1) if bundle.get("funder_lookup_pct") is not None else None,
                "entry_funder_sample_count": bundle.get("funder_sample_count"),
                "entry_holder_sample_count": bundle.get("holder_sample_count"),
                "entry_bundle_confidence": bundle.get("bundle_confidence"),
                "peak_usd": size_usd,
            }
        )
        save_state(self.state)
        log(f"ENTER {mint} ${size_usd:.2f} ({'live ' + buy_sig[:16] + '…' if buy_sig else 'paper fill'})")

    def close_position(self, pos: dict[str, Any], reason: str, sol_price: float) -> None:
        mint = pos["mint"]
        sell_sig = ""
        # Panic dumps everything; otherwise keep the configured moon-bag fraction, and only on
        # a winning exit when MOON_BAG_WINNERS_ONLY is set (the value the position was just
        # managed at is what decided the exit, so it is the right profit test).
        mb = 0.0 if reason == "panic" else self.cfg.moon_bag
        if mb > 0 and self.cfg.moon_bag_winners_only:
            last_value = pos.get("last_value_usd")
            if last_value is None or float(last_value) <= float(pos["position_usd"]):
                mb = 0.0
        if mb > 0 and float(pos.get("last_value_usd") or 0.0) * mb < self.cfg.min_moon_bag_usd:
            mb = 0.0  # too small to be worth the rent it would lock
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
            kept_usd = exit_usd * keep / sell_amount if sell_amount else 0.0  # what we gave up by keeping it
            self.state.setdefault("moon_bags", []).append(
                {
                    "mint": mint,
                    "tokens": keep,
                    "cost_usd": round(pos["position_usd"] * mb, 2),
                    "kept_usd": round(kept_usd, 2),
                    "peak_usd": round(kept_usd, 2),
                    "created_at": utc_iso(),
                    "from_exit": reason,
                }
            )
            target = f", sells at {self.cfg.moon_bag_target_x:.0f}x (${kept_usd * self.cfg.moon_bag_target_x:,.0f})" if self.cfg.moon_bag_target_x > 0 else ", held until panic"
            log(f"MOONBAG {mint}: keeping {mb:.0%} ({keep} tokens, worth ${kept_usd:.2f} now{target})")
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
                # token_accounts() may be served at finalized commitment and briefly report the
                # pre-sell amount.  Re-read the exact account at confirmed commitment before
                # deciding whether a burn instruction belongs in the close transaction.
                burn_amount = self.rpc.token_account_balance(acct["pubkey"])
                sig = self.close_token_account(
                    acct["pubkey"], acct["program"], mint, burn_amount=burn_amount
                )
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
                "entry_round_trip_pct": pos.get("entry_round_trip_pct"),
                "entry_market_cap_usd": pos.get("entry_market_cap_usd"),
                "entry_curve_age_seconds": pos.get("entry_curve_age_seconds"),
                "entry_top_holder_pct": pos.get("entry_top_holder_pct"),
                "entry_bundle_slot_pct": pos.get("entry_bundle_slot_pct"),
                "entry_cluster_pct": pos.get("entry_cluster_pct"),
                "entry_ancestry_cluster_pct": pos.get("entry_ancestry_cluster_pct"),
                "entry_transfer_cluster_pct": pos.get("entry_transfer_cluster_pct"),
                "entry_coordinated_buy_pct": pos.get("entry_coordinated_buy_pct"),
                "entry_repeat_cohort_pct": pos.get("entry_repeat_cohort_pct"),
                "entry_dev_cluster_pct": pos.get("entry_dev_cluster_pct"),
                "entry_top10_wallet_pct": pos.get("entry_top10_wallet_pct"),
                "entry_early_buy_pct": pos.get("entry_early_buy_pct"),
                "entry_funder_coverage_pct": pos.get("entry_funder_coverage_pct"),
                "entry_funder_lookup_pct": pos.get("entry_funder_lookup_pct"),
                "entry_funder_sample_count": pos.get("entry_funder_sample_count"),
                "entry_holder_sample_count": pos.get("entry_holder_sample_count"),
                "entry_bundle_confidence": pos.get("entry_bundle_confidence"),
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
                "entry_round_trip_pct": pos.get("entry_round_trip_pct"),
                "entry_market_cap_usd": pos.get("entry_market_cap_usd"),
                "entry_curve_age_seconds": pos.get("entry_curve_age_seconds"),
                "entry_top_holder_pct": pos.get("entry_top_holder_pct"),
                "entry_bundle_slot_pct": pos.get("entry_bundle_slot_pct"),
                "entry_cluster_pct": pos.get("entry_cluster_pct"),
                "entry_ancestry_cluster_pct": pos.get("entry_ancestry_cluster_pct"),
                "entry_transfer_cluster_pct": pos.get("entry_transfer_cluster_pct"),
                "entry_coordinated_buy_pct": pos.get("entry_coordinated_buy_pct"),
                "entry_repeat_cohort_pct": pos.get("entry_repeat_cohort_pct"),
                "entry_dev_cluster_pct": pos.get("entry_dev_cluster_pct"),
                "entry_top10_wallet_pct": pos.get("entry_top10_wallet_pct"),
                "entry_early_buy_pct": pos.get("entry_early_buy_pct"),
                "entry_funder_coverage_pct": pos.get("entry_funder_coverage_pct"),
                "entry_funder_lookup_pct": pos.get("entry_funder_lookup_pct"),
                "entry_funder_sample_count": pos.get("entry_funder_sample_count"),
                "entry_holder_sample_count": pos.get("entry_holder_sample_count"),
                "entry_bundle_confidence": pos.get("entry_bundle_confidence"),
                "peak_gain_pct": round(peak_gain * 100, 1) if peak_gain is not None else None,
            }
        )
        log(f"SCALE-OUT {mint}: sold {frac:.0%} for ${proceeds:.2f} ({net:+.1%}); remainder basis ${pos['position_usd']:.2f}")

    def sell_bag(self, bag: dict[str, Any], key: str, reason: str, sol_price: float, quote: dict[str, Any] | None = None) -> float:
        """Market-sell one bag from state[key] (moon bags or stuck positions), record the trade,
        reclaim its rent. Returns the USD proceeds."""
        if quote is None:
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
        self.state["daily"]["realized_pnl_usd"] = float(self.state["daily"]["realized_pnl_usd"]) + (proceeds - basis)
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
                "peak_gain_pct": round((float(bag["peak_usd"]) / basis - 1.0) * 100, 1) if basis and bag.get("peak_usd") else None,
            }
        )
        save_state(self.state)
        if self.cfg.mode == "live" and self.cfg.close_empty_accounts:
            self.reclaim_rent(bag["mint"])
        return proceeds

    def manage_moon_bags(self, sol_price: float) -> None:
        """Every MOON_BAG_CHECK_SECONDS, value each moon bag and sell any that has reached
        MOON_BAG_TARGET_X times the value it was kept at. Quote failures are silent: a dead bag
        is expected to be unquotable and would otherwise fill the log once a minute."""
        if self.cfg.moon_bag_target_x <= 0 or not self.state.get("moon_bags"):
            return
        if now_ts() - float(self.state.get("moon_bags_checked_ts") or 0) < self.cfg.moon_bag_check_seconds:
            return
        self.state["moon_bags_checked_ts"] = now_ts()
        for bag in list(self.state["moon_bags"]):
            try:
                quote = self.jup.quote(bag["mint"], WSOL, int(bag["tokens"]), slippage_bps=self.cfg.sell_slippage_bps)
            except Exception:
                continue
            value = int(quote["outAmount"]) / LAMPORTS * sol_price
            kept = float(bag.get("kept_usd") or bag.get("cost_usd") or 0.0)
            bag["last_value_usd"] = round(value, 2)
            bag["peak_usd"] = round(max(float(bag.get("peak_usd") or 0.0), value), 2)
            bag["last_x"] = round(value / kept, 1) if kept > 0 else None
            if kept > 0 and value >= kept * self.cfg.moon_bag_target_x:
                try:
                    proceeds = self.sell_bag(bag, "moon_bags", "moon_bag_target", sol_price, quote)
                    log(f"MOONBAG TARGET {bag['mint']}: worth ${value:,.2f} = {value / kept:.0f}x the ${kept:.2f} kept; sold for ${proceeds:,.2f}")
                except Exception as exc:
                    log(f"WARN moon bag {bag['mint']} hit {value / kept:.0f}x but sell failed: {describe_error(exc)}")
            elif kept > 0 and self.cfg.moon_bag_dead_pct > 0 and value < kept * self.cfg.moon_bag_dead_pct / 100:
                self.burn_dead_bag(bag, value)
        save_state(self.state)

    def burn_dead_bag(self, bag: dict[str, Any], value: float) -> None:
        """A bag worth a few cents is not worth a swap; burn it and take the rent back."""
        mint = bag["mint"]
        try:
            if self.cfg.mode == "live":
                for acct in self.rpc.token_accounts(self.wallet.pubkey, mint=mint):
                    self.close_token_account(acct["pubkey"], acct["program"], mint, burn_amount=acct["amount"])
            self.state["moon_bags"].remove(bag)
            basis = float(bag.get("cost_usd") or 0.0)
            self.state["daily"]["realized_pnl_usd"] = float(self.state["daily"]["realized_pnl_usd"]) - basis
            record_trade(
                {
                    "opened_at": bag.get("created_at"),
                    "closed_at": utc_iso(),
                    "mint": mint,
                    "mode": self.cfg.mode,
                    "position_usd": round(basis, 2),
                    "exit_usd": 0.0,
                    "net_return": -1.0 if basis else 0.0,
                    "exit_reason": "moon_bag_dead",
                    "sell_signature": "",
                    "peak_gain_pct": round((float(bag["peak_usd"]) / basis - 1.0) * 100, 1) if basis and bag.get("peak_usd") else None,
                }
            )
            log(f"MOONBAG DEAD {mint}: worth ${value:.2f} vs ${bag.get('kept_usd', 0):.2f} kept; burned, rent reclaimed (~{TOKEN_ACCOUNT_RENT_SOL:.4f} SOL)")
        except Exception as exc:
            log(f"WARN {mint}: could not burn dead moon bag: {describe_error(exc)}")

    def liquidate_bags(self, sol_price: float, key: str, reason: str) -> None:
        """Market-sell everything in state[key] (moon bags or stuck positions) during panic."""
        for bag in list(self.state.get(key, [])):
            try:
                proceeds = self.sell_bag(bag, key, reason, sol_price)
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
    def run_cycle(self) -> None:
        """Run one executor iteration with exits strictly ahead of new-entry work."""
        roll_daily(self.state)
        if (
            self.cfg.mode == "live"
            and getattr(self, "_reconcile_pending", False)
            and now_ts() >= float(getattr(self, "_provider_cooldown_until", 0.0))
        ):
            try:
                self.reconcile_wallet(self.sol_price_usd())
                self._reconcile_pending = False
            except Exception as exc:
                if is_rate_limited(exc):
                    delay = self.note_provider_rate_limit(minimum_seconds=300)
                    log(f"WARN wallet reconciliation still rate limited; retrying in {delay:.0f}s")
                else:
                    log(f"WARN deferred wallet reconciliation failed: {describe_error(exc)}")
                    self._reconcile_pending = False
        panic = PANIC_FLAG.exists()
        draining = STOP_FLAG.exists() or panic
        self.state["draining"] = draining
        sol_price = None

        # Capital already at risk always goes first.  The old loop analyzed every due entry before
        # reaching this block, which could miss an entire pump-and-dump during a launch burst.
        if self.state["positions"]:
            sol_price = self.sol_price_usd()
            self.manage_positions(sol_price, panic)
        if self.state.get("moon_bags") and not panic:
            self.manage_moon_bags(sol_price or self.sol_price_usd())
        if panic and (self.state.get("moon_bags") or self.state.get("stuck")):
            price = sol_price or self.sol_price_usd()
            self.liquidate_bags(price, "moon_bags", "panic_moon_bag")
            self.liquidate_bags(price, "stuck", "panic_stuck")
        if panic and not self.state["positions"] and not self.state.get("moon_bags") and not self.state.get("stuck"):
            PANIC_FLAG.unlink(missing_ok=True)
            STOP_FLAG.touch()
            log("panic complete: all positions and moon bags closed, executor draining")

        if not draining:
            self.poll_graduations()
            due = [p for p in self.pending if now_ts() >= p["enter_at"]]
            if due:
                sol_price = sol_price or self.sol_price_usd()
                for item in due[: self.cfg.max_entries_per_cycle]:
                    self.pending.remove(item)
                    self.enter_with_retry(item, sol_price)
        save_state(self.state)

    def run(self) -> None:
        log(f"executor starting: mode={self.cfg.mode} fraction={self.cfg.account_fraction} "
            f"max_pos=${self.cfg.max_position_usd} slots={self.cfg.max_concurrent} "
            f"tp=+{self.cfg.take_profit:.0%} sl=-{self.cfg.stop_loss:.0%} "
            f"time_stop={self.cfg.time_stop_minutes:.0f}m trail={self.cfg.trailing_stop:.0%} "
            f"scale_out={self.cfg.scale_out_at:.0%}x{self.cfg.scale_out_fraction:.0%} moon_bag={self.cfg.moon_bag:.0%}"
            f"{'(winners only)' if self.cfg.moon_bag_winners_only else ''}"
            f"{f'@{self.cfg.moon_bag_target_x:.0f}x' if self.cfg.moon_bag_target_x > 0 else '@hold'}"
            f"(min${self.cfg.min_moon_bag_usd:.2f},dead<{self.cfg.moon_bag_dead_pct:.0f}%) "
            f"slippage={self.cfg.slippage_bps}/{self.cfg.sell_slippage_bps}bps(buy/sell) "
            f"round_trip>={self.cfg.min_entry_round_trip_pct:.0f}% "
            f"mcap=${self.cfg.min_entry_market_cap_usd:,.0f}-${self.cfg.max_entry_market_cap_usd:,.0f} "
            f"curve_age>={self.cfg.min_curve_age_seconds:.0f}s top_holder<={self.cfg.max_top_holder_pct:.0f}% "
            f"bundle_slot<={self.cfg.max_bundle_slot_pct:.0f}% cluster<={self.cfg.max_cluster_pct:.0f}% "
            f"ancestry<={self.cfg.max_ancestry_cluster_pct:.0f}% transfer<={self.cfg.max_transfer_cluster_pct:.0f}% "
            f"coordinated<={self.cfg.max_coordinated_buy_pct:.0f}% repeat<={self.cfg.max_repeat_cohort_pct:.0f}% "
            f"dev_cluster<={self.cfg.max_dev_cluster_pct:.0f}% top10<={self.cfg.max_top10_wallet_pct:.0f}% "
            f"early_buy<={self.cfg.max_early_buy_pct:.0f}% funder_coverage>={self.cfg.min_funder_coverage_pct:.0f}% "
            f"funder_lookup>={self.cfg.min_funder_lookup_pct:.0f}% "
            f"high_confidence>={self.cfg.high_confidence_funder_coverage_pct:.0f}% "
            f"holders={self.cfg.bundle_max_wallets} funders={self.cfg.bundle_funder_max_wallets} "
            f"funder_workers={self.cfg.bundle_lookup_workers} "
            f"providers={len(self.cfg.rpc_urls)} discovery={self.cfg.discovery_mode} "
            f"history={self.cfg.transaction_history_mode} das={'on' if self.cfg.rpc_das_enabled else 'off'} "
            f"bundle_mode={'log' if self.cfg.bundle_log_only else 'block'} "
            f"adopt>=${self.cfg.min_adopt_usd} "
            f"stuck_after={self.cfg.stuck_after_minutes:.0f}m "
            f"retries={self.cfg.entry_retries} daily_loss_limit=${self.cfg.daily_loss_limit_usd}")
        if self.cfg.mode == "live":
            log(f"live wallet: {self.wallet.pubkey} (burner only!)")
            startup_provider_limited = False
            try:
                balance = self.rpc.sol_balance(self.wallet.pubkey)
                log(f"wallet balance: {balance:.4f} SOL ({self.cfg.min_sol_reserve} SOL reserved for fees)")
                if balance <= self.cfg.min_sol_reserve:
                    log("WARNING: balance at or below the fee reserve — no entries will be taken until funded")
            except Exception as exc:
                startup_provider_limited = is_provider_unavailable(exc)
                if startup_provider_limited:
                    delay = self.note_provider_rate_limit(minimum_seconds=300)
                    log(f"WARN could not read wallet balance: RPC providers unavailable; pausing new-entry work for {delay:.0f}s")
                else:
                    log(f"WARN could not read wallet balance: {describe_error(exc)}")
        if self.state["positions"]:
            log(f"resuming {len(self.state['positions'])} open position(s) from state file")
        if self.cfg.mode == "live":
            if startup_provider_limited:
                self._reconcile_pending = True
                log("WARN wallet reconciliation deferred until the provider cooldown expires")
            else:
                try:
                    self.reconcile_wallet(self.sol_price_usd())
                except Exception as exc:
                    if is_provider_unavailable(exc):
                        delay = self.note_provider_rate_limit(minimum_seconds=300)
                        self._reconcile_pending = True
                        log(f"WARN wallet reconciliation provider unavailable; deferred for {delay:.0f}s")
                    else:
                        log(f"WARN wallet reconciliation failed: {describe_error(exc)}")
        if self.cfg.discovery_mode == "websocket":
            self.migration_stream = MigrationStream(self.cfg)
            self.migration_stream.start()
            log(f"graduation discovery: WebSocket stream + {self.cfg.discovery_catchup_seconds:.0f}s RPC catch-up")
        else:
            log(f"graduation discovery: RPC polling every {self.cfg.discovery_catchup_seconds:.0f}s")
        while True:
            try:
                self.run_cycle()
            except Exception as exc:
                log(f"ERROR loop: {exc}")
            time.sleep(self.cfg.poll_seconds)


def main() -> None:
    cfg = Config()
    if "--print-config" in sys.argv:
        safe = {
            key: value
            for key, value in vars(cfg).items()
            if key
            not in {
                "wallet_key",
                "helius_api_key",
                "rpc_url",
                "rpc_urls",
                "rpc_ws_url",
                "rpc_ws_urls",
                "jupiter_base",
            }
        }
        safe["rpc_urls"] = [redact_endpoint(url) for url in cfg.rpc_urls]
        safe["rpc_ws_urls"] = [
            redact_endpoint(url.replace("wss://", "https://", 1).replace("ws://", "http://", 1))
            for url in cfg.rpc_ws_urls
        ]
        safe["jupiter_base"] = redact_endpoint(cfg.jupiter_base)
        print(json.dumps(safe, indent=2))
        return
    Executor(cfg).run()


if __name__ == "__main__":
    main()
