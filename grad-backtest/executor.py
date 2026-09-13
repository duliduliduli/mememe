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
import copy
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
from decimal import Decimal, InvalidOperation
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
    "entry_seconds_after_graduation", "entry_in_boost_window",
    "exit_seconds_after_graduation", "exit_in_boost_window",
    "entry_curve_tx_count", "entry_early_sell_pct", "entry_creator",
    "entry_creator_prior_launches", "entry_creator_hold_pct",
    "entry_history_total", "entry_history_decoded",
]

SKIP_COLUMNS = [
    "timestamp", "mint", "reason", "seconds_after_graduation", "in_boost_window",
    "market_cap_usd", "price_impact_pct", "curve_age_seconds", "curve_tx_count",
    "top_holder_pct", "creator", "creator_prior_launches", "creator_hold_pct",
    "early_sell_pct", "early_seller", "bundle_confidence", "bundle_slot_pct", "cluster_pct",
    "dev_cluster_pct", "top10_wallet_pct", "early_buy_pct", "funder_coverage_pct",
    "history_total", "history_decoded", "round_trip_pct",
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
    if is_rate_limited(exc) or isinstance(
        exc, (requests.Timeout, requests.ConnectionError, json.JSONDecodeError)
    ):
        return True
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", 0)
    if status in {401, 403, 408, 425} or status >= 500:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "node is unhealthy",
            "service unavailable",
            "too many requests",
            "expecting value",
            "malformed rpc response",
            "rpc batch returned",
        )
    )


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


# Public, keyless mainnet services. Explicit RPC_URL(S) still overrides this list.
# Sources: https://solana.publicnode.com/ and https://solana.com/docs/references/clusters
DEFAULT_RPC_URLS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet.solana.com",
)


class Config:
    def __init__(self) -> None:
        self.mode = os.getenv("EXECUTOR_MODE", "paper").lower()
        if self.mode not in ("paper", "live"):
            raise SystemExit("EXECUTOR_MODE must be 'paper' or 'live'")
        self.helius_api_key = os.getenv("HELIUS_API_KEY") or ""
        self.migration_address = os.getenv("MIGRATION_ADDRESS") or ""
        self.rpc_urls = split_urls(os.getenv("RPC_URLS") or os.getenv("RPC_URL") or "")
        if not self.rpc_urls:
            # An existing, exhausted Helius key must not override the keyless defaults.
            self.rpc_urls = list(DEFAULT_RPC_URLS)
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
            max(3, int(os.getenv("RAW_FUNDER_SIGNATURE_LIMIT", "25"))),
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
        # Never sell the whole position: a slice stays in the wallet in case the token runs
        # after the exit. MOON_BAG=0 restores full exits.
        # 10% of a winning exit is the lottery ticket: 28 bags kept from losing exits in one
        # day were worth 42% of what was kept eight hours later and none had doubled, while
        # the capital and the rent sat idle. Bags stay only where a 100x has real odds.
        self.moon_bag = min(0.5, max(0.0, float(os.getenv("MOON_BAG", "0.10"))))  # fraction kept at exit; 0 disables
        # Keep a bag only when the exit was profitable (a stop-loss remnant just rides to zero and
        # locks its rent), and sell a bag once it is worth MOON_BAG_TARGET_X times what was kept
        # (0 = hold forever; panic is then the only way out). Bags are re-quoted every
        # MOON_BAG_CHECK_SECONDS, not every loop, since they are off the Jupiter budget otherwise.
        self.moon_bag_winners_only = os.getenv("MOON_BAG_WINNERS_ONLY", "1") == "1"  # 0: losers keep a bag too
        self.moon_bag_target_x = max(0.0, float(os.getenv("MOON_BAG_TARGET_X", "100")))
        self.moon_bag_check_seconds = float(os.getenv("MOON_BAG_CHECK_SECONDS", "180"))
        # A bag worth less than MIN_MOON_BAG_USD is not worth its own rent (0.002 SOL) and is
        # sold with the rest. A bag that has fallen to MOON_BAG_DEAD_PCT of the value it was kept
        # at is burned and its account closed: the rent is worth more than the tokens.
        self.min_moon_bag_usd = float(os.getenv("MIN_MOON_BAG_USD", "0.5"))
        self.moon_bag_dead_pct = float(os.getenv("MOON_BAG_DEAD_PCT", "10"))  # 0: bags are never burned
        # Startup sweep: an untracked holding worth less than this (about one account's rent)
        # is burned and its account closed, so dust never piles up across restarts.
        self.dust_sweep_below_usd = float(os.getenv("DUST_SWEEP_BELOW_USD", "0.25"))
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
        # On a $5 order 5% impact is $0.25; the guard's job is to reject drained pools (30-79%
        # impact seen live on $3 pools), not fresh ones. ARGOS was skipped at 5.3% and doubled.
        self.max_price_impact_pct = float(os.getenv("MAX_PRICE_IMPACT_PCT", "10"))
        # A route existing is not enough: require that the just-quoted tokens can immediately
        # be sold back for most of the input. This is an executable liquidity check, not a UI badge.
        self.min_entry_round_trip_pct = percent_env("MIN_ENTRY_ROUND_TRIP_PCT", 80)
        # Pump.fun tokens graduate around $69k and genuine ones sit near $30k-200k at entry. A
        # market cap far above that 30s after migration means a bundled buy already pumped it
        # and we would be buying the top of someone else's pump, which then dumps into us.
        # Observed live: 15-holder tokens at $5M, $150M caps one minute old. 0 disables.
        # Off by default since 2026-09-05: a $2M ceiling rejected $13M-$26M graduations carrying
        # $300K-$440K of real liquidity, the healthiest cohort on the tape, while the bundle and
        # dump checks below catch the pumped-then-dumped case directly. Set a value to re-enable.
        self.max_entry_market_cap_usd = float(os.getenv("MAX_ENTRY_MARKET_CAP_USD", "0"))
        # Floor: graduation is ~$69k, so a token far below that a minute later was already dumped
        # into its own pool. SOLL: the creator sold 78% of supply 24s after migration and we bought
        # at a $450 cap. Overnight, sub-$30k entries went 1 for 7. 0 disables.
        self.min_entry_market_cap_usd = float(os.getenv("MIN_ENTRY_MARKET_CAP_USD", "25000"))
        # Runner mode: swing-trade the graduations that prove themselves. Every graduation goes
        # on a watchlist for RUNNER_WATCH_HOURS; its market cap is re-read every
        # RUNNER_CHECK_SECONDS from Jupiter's batched price feed, and the bot enters once the cap
        # is inside RUNNER_MIN..RUNNER_MAX_MARKET_CAP_USD (the $400k-$4M "sweet spot") and up at
        # least RUNNER_MIN_GAIN_PCT from its low of the last RUNNER_MOMENTUM_MINUTES. Every other
        # entry guard (impact, round trip, holders, bundles) still applies at that moment. Runner
        # positions use their own take profit / stop / time stop. RUNNER_ONLY=1 turns the
        # at-graduation entry off so only runners are traded.
        self.runner_enabled = os.getenv("RUNNER_ENABLED", "1") == "1"
        self.runner_only = os.getenv("RUNNER_ONLY", "0") == "1"
        self.runner_min_market_cap_usd = float(os.getenv("RUNNER_MIN_MARKET_CAP_USD", "400000"))
        self.runner_max_market_cap_usd = float(os.getenv("RUNNER_MAX_MARKET_CAP_USD", "4000000"))
        self.runner_watch_hours = float(os.getenv("RUNNER_WATCH_HOURS", "6"))
        self.runner_check_seconds = max(15.0, float(os.getenv("RUNNER_CHECK_SECONDS", "60")))
        self.runner_min_gain_pct = float(os.getenv("RUNNER_MIN_GAIN_PCT", "10"))
        self.runner_momentum_minutes = float(os.getenv("RUNNER_MOMENTUM_MINUTES", "15"))
        self.runner_max_watch = int(os.getenv("RUNNER_MAX_WATCH", "100"))
        self.runner_take_profit = float(os.getenv("RUNNER_TAKE_PROFIT", "1.0"))
        self.runner_stop_loss = float(os.getenv("RUNNER_STOP_LOSS", "0.30"))
        self.runner_trailing_stop = float(os.getenv("RUNNER_TRAILING_STOP", "0.25"))
        self.runner_time_stop_minutes = float(os.getenv("RUNNER_TIME_STOP_MINUTES", "240"))
        self.jupiter_price_url = os.getenv("JUPITER_PRICE_URL", "https://lite-api.jup.ag/price/v3").rstrip("/")
        # Copy mode: mirror the buys of chosen wallets. COPY_WALLETS lists their addresses; each
        # is polled every COPY_POLL_SECONDS for new signatures and every new transaction is
        # decoded from its balance changes. A buy worth at least COPY_MIN_BUY_USD is mirrored at
        # our usual position size; the price-impact and round-trip checks always run, the slow
        # holder/bundle analysis is skipped when COPY_FAST=1 (speed is the whole point). With
        # COPY_FOLLOW_SELLS=1 the position is closed when that wallet sells the token; the
        # COPY_* thresholds guard it in between. Trades older than COPY_MAX_TX_AGE_SECONDS at
        # detection are ignored, and the first poll of a wallet only takes a baseline.
        self.copy_wallets, self.copy_wallet_min_usd, self.copy_wallet_size = parse_wallet_list(os.getenv("COPY_WALLETS", ""))
        # COPY_ONLY=1 (the default once COPY_WALLETS is set) turns graduation discovery and the
        # runner watchlist off: the only entries are mirrored buys. COPY_ONLY=0 runs all lanes.
        self.copy_only = os.getenv("COPY_ONLY", "1" if self.copy_wallets else "0") == "1" and bool(self.copy_wallets)
        if self.copy_only and os.getenv("MAX_CONCURRENT_POSITIONS") is None:
            self.max_concurrent = 10
        if self.copy_only and os.getenv("ACCOUNT_FRACTION") is None:
            self.account_fraction = 0.08
        # Copy-only sizing: 8% of the whole account (free SOL plus open positions) per copy, up
        # to ten at once, and never more than MAX_DEPLOYED_FRACTION of the account in positions.
        self.max_deployed_fraction = min(1.0, max(0.1, float(os.getenv("MAX_DEPLOYED_FRACTION", "0.80"))))
        # Each open position costs one Jupiter sell quote per check; the keyless Jupiter tier
        # answers a busy loop with 429s, which delays every exit. Copy positions ride for hours,
        # so checking them every few seconds is plenty; graduation snipes keep every-cycle checks.
        self.position_check_seconds = float(os.getenv("POSITION_CHECK_SECONDS", "8" if self.copy_only else "0"))
        # Value open positions from the batched price feed (one request for all of them) and
        # only ask for a real sell quote when an exit is within PRICE_FIRST_MARGIN_PCT of firing.
        self.price_first_valuation = os.getenv("PRICE_FIRST_VALUATION", "1") == "1"
        self.price_first_margin_pct = float(os.getenv("PRICE_FIRST_MARGIN_PCT", "8"))
        self.copy_poll_seconds = max(1.0, float(os.getenv("COPY_POLL_SECONDS", "3")))
        # Followed wallets scatter $3-$10 probe buys between their real entries; mirroring a
        # probe with a full-size position would out-bet the wallet itself. $50 skips the probes.
        # $300: only a wallet's conviction buys. Its $10-$100 sprays drove 60 round trips in
        # six hours, each paying the buy and sell slippage on a coin that did not move.
        self.copy_min_buy_usd = float(os.getenv("COPY_MIN_BUY_USD", "300"))
        self.copy_follow_sells = os.getenv("COPY_FOLLOW_SELLS", "1") == "1"
        # A followed wallet selling at least this share of its stack closes our position; a
        # smaller sale trims ours by the same share (a trim worth under a dollar is skipped).
        self.copy_full_sell_fraction = float(os.getenv("COPY_FULL_SELL_FRACTION", "0.8"))
        # With every slot full, a new copied buy sells our oldest position and takes its slot
        # (COPY_ROTATE=0 skips the new buy instead), so the book is never stuck in old coins.
        # Off by default: rotating the oldest position out for every new buy sold coins at
        # whatever price they were at (-5% to -28%) to chase the next one. A full book now
        # skips the new buy; positions leave only on their own stop, target or time.
        self.copy_rotate = os.getenv("COPY_ROTATE", "0") == "1"
        # A hyperactive wallet would otherwise rotate the book every minute, paying the buy and
        # sell slippage each time; a position younger than this keeps its slot and the new buy
        # is skipped instead.
        self.copy_rotate_min_age_minutes = float(os.getenv("COPY_ROTATE_MIN_AGE_MINUTES", "20"))
        self.copy_fast = os.getenv("COPY_FAST", "1") == "1"
        self.copy_max_tx_age_seconds = float(os.getenv("COPY_MAX_TX_AGE_SECONDS", "90"))
        self.copy_take_profit = float(os.getenv("COPY_TAKE_PROFIT", "0.75"))
        self.copy_stop_loss = float(os.getenv("COPY_STOP_LOSS", "0.30"))
        self.copy_trailing_stop = float(os.getenv("COPY_TRAILING_STOP", "0.25"))
        self.copy_time_stop_minutes = float(os.getenv("COPY_TIME_STOP_MINUTES", "1440"))
        # The followed wallets' winners mostly top out between +30% and +100% from their entry,
        # so profit is phased out across that range: "1.4:40,1.8:30,3:30" sells 40% of the entry
        # tokens at +40%, 30% at +80% and the last 30% at 3x (the final rung closes the
        # position, so the moon bag still applies).
        # Empty disables the ladder and COPY_TAKE_PROFIT decides instead.
        self.copy_ladder = parse_sell_ladder(os.getenv("COPY_LADDER", "1.4:40,1.8:30,3:30"))
        # Bundle guards. A curve that fills in seconds was bought by one party (SOLL graduated 29s
        # after creation with six buyers), and a wallet holding a big slice of supply at entry is
        # the one that dumps on us (SOLL's creator held 59% at graduation). 0 disables either.
        self.min_curve_age_seconds = float(os.getenv("MIN_CURVE_AGE_SECONDS", "120"))
        self.max_top_holder_pct = float(os.getenv("MAX_TOP_HOLDER_PCT", "20"))
        # Curve activity floor. A curve that filled with a handful of transactions was bought by
        # one party: rugs seen live had 2, 9 and 15 successful curve transactions, organic
        # launches 311 to 2,293. Counted from signature metadata alone (one or two calls), so it
        # does not depend on the creation timestamp being found. 0 disables.
        self.min_curve_transactions = int(os.getenv("MIN_CURVE_TRANSACTIONS", "150"))
        # Early-dump check at entry time: a single plain wallet that has already sold this much
        # of supply into the pool since migration means the dump started before we arrived
        # (SOLL's creator sold 78% at +53s; we bought at +98s). 0 disables.
        self.max_early_sell_pct = percent_env("MAX_EARLY_SELL_PCT", 3)
        # Creator checks. Serial launchers are rug factories; a creator still holding a large
        # slice at entry is the wallet that dumps. Prior launches are counted from a bounded
        # sample of the creator's history, so a busy legitimate wallet undercounts (fails open)
        # while a factory that launches constantly is seen. 0 disables either.
        self.max_creator_prior_launches = int(os.getenv("MAX_CREATOR_PRIOR_LAUNCHES", "3"))
        self.max_creator_hold_pct = percent_env("MAX_CREATOR_HOLD_PCT", 5)
        self.creator_history_signatures = min(1000, max(20, int(os.getenv("CREATOR_HISTORY_SIGNATURES", "100"))))
        self.creator_history_decode_limit = min(200, max(10, int(os.getenv("CREATOR_HISTORY_DECODE_LIMIT", "60"))))
        # Pump.fun BOOST (live since 2026-07-21) spends ~17.6 SOL of migration proceeds on a
        # five-minute TWAP buy-and-burn immediately after migration. A +30s entry rides that
        # protocol bid; exits after +300s do not. Every position and trade row records whether
        # entry and exit fell inside the window so the subsidy can be separated from the edge.
        self.boost_window_seconds = float(os.getenv("BOOST_WINDOW_SECONDS", "300"))
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
        # A holding worth less than this at startup is a leftover (a moon bag from before the
        # restart, or dust), not a position: it is adopted as a moon bag so it neither takes a
        # slot nor blocks a fresh copy of the same coin. Default: half the minimum position.
        self.adopt_as_bag_below_usd = float(os.getenv("ADOPT_AS_BAG_BELOW_USD", str(self.min_position_usd * 0.5)))
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


def _append_row(path: Path, columns: list[str], row: dict[str, Any]) -> None:
    """Append one CSV row. A file written by an older version (a header missing columns we now
    record) is rotated to <name>.<timestamp>.csv so no metadata is silently dropped and no
    column is ever misaligned; a file with extra or reordered columns keeps its own header."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = columns
    new = not path.exists()
    if not new:
        with path.open() as fh:
            existing = fh.readline().strip().split(",")
        if existing and existing != [""]:
            if set(existing) < set(columns):
                rotated = path.with_name(f"{path.stem}.{int(now_ts())}{path.suffix}")
                os.replace(path, rotated)
                log(f"rotated {path.name} (older header) to {rotated.name}")
                new = True
            else:
                fieldnames = existing
    with path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k) for k in fieldnames})


def record_trade(row: dict[str, Any]) -> None:
    _append_row(TRADES_FILE, TRADE_COLUMNS, row)


def record_skip(mint: str, reason: str, meta: dict[str, Any] | None = None) -> None:
    """Audit trail of everything the bot passed on, why, and what was known at the time, so a
    day of skips can be scored against what the tokens did next."""
    row = {"timestamp": utc_iso(), "mint": mint, "reason": reason}
    for key, value in (meta or {}).items():
        if key in SKIP_COLUMNS and value is not None:
            row[key] = round(value, 2) if isinstance(value, float) else value
    _append_row(SKIPS_FILE, SKIP_COLUMNS, row)


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


def parse_wallet_list(spec: str) -> tuple[tuple[str, ...], dict[str, float], dict[str, float]]:
    """COPY_WALLETS entries are `address`, `address:min_usd` or `address:min_usd:size`. The
    minimum is that wallet's own smallest buy to mirror (a whale's $100 buys are pocket change
    to it); the size scales our copies of that wallet against the usual position size (`0.5`
    or `50%` halves them, to keep a riskier wallet small). Returns (wallets, minimums, sizes)."""
    wallets: list[str] = []
    minimums: dict[str, float] = {}
    sizes: dict[str, float] = {}
    for raw in spec.replace("\n", ",").replace(";", ",").split(","):
        parts = [p.strip() for p in raw.strip().split(":")]
        address = parts[0]
        if not address:
            continue
        wallets.append(address)
        minimum = parts[1] if len(parts) > 1 else ""
        size = parts[2] if len(parts) > 2 else ""
        try:
            if minimum:
                minimums[address] = float(minimum.lstrip("$").replace("_", "").replace("k", "000").replace("K", "000"))
        except ValueError:
            pass
        try:
            if size:
                value = float(size.lower().lstrip("x").rstrip("%"))
                if size.endswith("%"):
                    value /= 100.0
                if value > 0:
                    sizes[address] = value
        except ValueError:
            pass
    return tuple(wallets), minimums, sizes


def parse_sell_ladder(spec: str) -> list[dict[str, float]]:
    """"2:40,3:30,5:30" -> [{"x": 2, "pct": 40}, ...]: percent of the entry tokens to sell once
    the token price reaches x times the entry price. Sorted by multiple; bad rungs dropped."""
    rungs = []
    for part in str(spec or "").split(","):
        if ":" not in part:
            continue
        x_text, pct_text = part.split(":", 1)
        try:
            x, pct = float(x_text.strip().rstrip("xX")), float(pct_text.strip().rstrip("%"))
        except ValueError:
            continue
        if x > 1 and 0 < pct <= 100:
            rungs.append({"x": x, "pct": pct})
    return sorted(rungs, key=lambda r: r["x"])


def ladder_text(rungs: list[dict[str, float]]) -> str:
    return ",".join("{:g}x:{:g}%".format(r["x"], r["pct"]) for r in rungs) or "off"


STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6,   # USDT
}


def wallet_swap_from_transaction(tx: dict[str, Any], wallet: str) -> dict[str, Any] | None:
    """What `wallet` bought or sold in a confirmed transaction, read from balance changes:
    a token whose balance rose while the wallet's SOL (wrapped SOL, USDC or USDT) fell is a
    buy, the reverse a sell. Returns {"side", "mint", "tokens", "sol", "stable_usd"} plus
    "held_before" (a buy added to a coin the wallet already held) or "fraction" (the share of
    the wallet's stack a sell let go), or None when the wallet did not swap. Multi-hop routes
    report the token with the largest change."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    keys = rpc_account_keys(tx)
    sol_delta = 0.0
    if wallet in keys:
        idx = keys.index(wallet)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if idx < len(pre) and idx < len(post):
            sol_delta = (int(post[idx]) - int(pre[idx])) / LAMPORTS
    deltas: dict[str, int] = {}
    before: dict[str, int] = {}
    for sign, rows in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
        for row in rows:
            if str(row.get("owner")) != wallet:
                continue
            mint = str(row.get("mint"))
            try:
                amount = int((row.get("uiTokenAmount") or {}).get("amount") or 0)
            except (TypeError, ValueError):
                continue
            deltas[mint] = deltas.get(mint, 0) + sign * amount
            if sign < 0:
                before[mint] = before.get(mint, 0) + amount
    wsol_delta = deltas.pop(WSOL, 0) / LAMPORTS
    sol_delta += wsol_delta
    stable_delta = sum(deltas.pop(m, 0) / 10 ** dec for m, dec in STABLE_MINTS.items())
    deltas = {m: d for m, d in deltas.items() if d != 0}
    if not deltas:
        return None
    mint, delta = max(deltas.items(), key=lambda kv: abs(kv[1]))
    paid_sol = -sol_delta if sol_delta < 0 else 0.0
    paid_stable = -stable_delta if stable_delta < 0 else 0.0
    held = before.get(mint, 0)
    if delta > 0 and (paid_sol > 0 or paid_stable > 0):
        return {"side": "buy", "mint": mint, "tokens": delta, "sol": paid_sol, "stable_usd": paid_stable,
                "held_before": held > 0}
    if delta < 0 and (sol_delta > 0 or stable_delta > 0):
        return {"side": "sell", "mint": mint, "tokens": -delta, "sol": max(sol_delta, 0.0), "stable_usd": max(stable_delta, 0.0),
                "fraction": min(1.0, -delta / held) if held > 0 else 1.0}
    return None


def rpc_account_keys(tx: dict[str, Any]) -> list[str]:
    transaction = tx.get("transaction")
    if not isinstance(transaction, dict):
        return []
    message = transaction.get("message")
    if not isinstance(message, dict):
        return []
    keys: list[str] = []
    for item in message.get("accountKeys") or []:
        keys.append(str(item.get("pubkey")) if isinstance(item, dict) else str(item))
    return keys


def rpc_instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    transaction = tx.get("transaction")
    if not isinstance(transaction, dict):
        return []
    message = transaction.get("message")
    if not isinstance(message, dict):
        return []
    instructions = list(message.get("instructions") or [])
    meta = tx.get("meta")
    if isinstance(meta, dict):
        for group in meta.get("innerInstructions") or []:
            if isinstance(group, dict):
                instructions.extend(group.get("instructions") or [])
    return [item for item in instructions if isinstance(item, dict)]


def bonding_curve_address(mint: str) -> str:
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address(
        [b'bonding-curve', bytes(Pubkey.from_string(mint))],
        Pubkey.from_string('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'),
    )[0])


def candidate_mints_from_rpc_transaction(tx: dict[str, Any]) -> list[str]:
    """Recognize an actual Pump migration + matching PumpSwap pool creation.

    Account positions/discriminators are pinned to pump-fun/pump-public-docs IDLs.
    Token balance and initializeMint scans confuse the LP mint with the launched mint;
    ordinary transfers and repeat (idempotent) migrate calls are not new graduations.
    """
    if (tx.get('meta') or {}).get('err') is not None:
        return []
    migrations = []
    pools = set()
    keys = rpc_account_keys(tx)
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    for ix in rpc_instructions(tx):
        try:
            program = ix.get('programId') or keys[ix['programIdIndex']]
            accounts = [keys[a] if isinstance(a, int) else a for a in ix.get('accounts', [])]
            encoded = ix.get('data', '')
            n = 0
            for char in encoded:
                n = n * 58 + alphabet.index(char)
            data = b'\0' * (len(encoded) - len(encoded.lstrip('1'))) + n.to_bytes((n.bit_length() + 7) // 8, 'big')
            tag = data[:8]
            if program == '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P':
                if tag == bytes([155,234,231,146,236,158,162,30]) and len(accounts) >= 25:
                    migrations.append((accounts[2], accounts[14], accounts[9], accounts[15]))
                elif tag == bytes([187,203,18,31,206,237,254,41]) and len(accounts) >= 27:
                    migrations.append((accounts[2], accounts[3], accounts[10], accounts[15]))
            elif program == 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA':
                if tag == bytes([233,146,209,142,207,104,64,188]) and len(accounts) >= 18:
                    pools.add((accounts[3], accounts[4], accounts[0], accounts[5]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return sorted({mint for mint, quote, pool, lp in migrations
                   if mint not in KNOWN_QUOTES and quote in KNOWN_QUOTES
                   and (mint, quote, pool, lp) in pools})


def pump_creations(tx: dict[str, Any]) -> list[dict[str, Any]]:
    """Verified Pump Create/CreateV2 instructions, using the published Pump IDL.

    The creator is an instruction argument, not necessarily the transaction fee payer.
    Activity timestamps alone do not establish creation.
    """
    meta = tx.get("meta")
    if not isinstance(meta, dict) or meta.get("err") is not None:
        return []
    if tx.get("blockTime") is None or tx.get("slot") is None:
        return []
    from solders.pubkey import Pubkey
    keys = rpc_account_keys(tx)
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    creations = []
    for ix in rpc_instructions(tx):
        try:
            program = ix.get('programId') or keys[ix['programIdIndex']]
            if program != '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P':
                continue
            encoded = ix.get('data', '')
            n = 0
            for char in encoded:
                n = n * 58 + alphabet.index(char)
            data = b'\0' * (len(encoded) - len(encoded.lstrip('1'))) + n.to_bytes((n.bit_length() + 7) // 8, 'big')
            required = {bytes([24,30,200,40,5,28,7,119]): 14,
                        bytes([214,144,76,236,95,139,49,180]): 16}.get(data[:8])
            accounts = [keys[a] if isinstance(a, int) else a for a in ix.get('accounts', [])]
            if required is None or len(accounts) < required:
                continue
            mint = accounts[0]
            if accounts[2] != bonding_curve_address(mint):
                continue
            offset = 8
            for _ in range(3):  # name, symbol, URI: Borsh strings
                if offset + 4 > len(data):
                    raise ValueError('truncated create arguments')
                length = int.from_bytes(data[offset:offset + 4], 'little')
                offset += 4 + length
                if offset > len(data):
                    raise ValueError('truncated create string')
            creator = str(Pubkey.from_bytes(data[offset:offset + 32]))
            creations.append({'mint': mint, 'creator': creator,
                              'timestamp': tx['blockTime'], 'slot': tx['slot']})
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return creations


def normalize_rpc_transaction(tx: dict[str, Any], signature: str = "") -> dict[str, Any]:
    """Convert standard jsonParsed transaction data into the small normalized shape used by
    the bundle detector. This intentionally reconstructs only evidence we consume."""
    keys = rpc_account_keys(tx)
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    fee_payer = keys[0] if keys else None
    native_transfers: list[dict[str, Any]] = []
    for instruction in rpc_instructions(tx):
        parsed = instruction.get("parsed")
        # Memo and a few other programs come back with `parsed` as a bare string; treating it
        # as a dict crashed the whole bundle snapshot ('str' object has no attribute 'get').
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info")
        if not isinstance(info, dict):
            continue
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
            if not isinstance(row, dict):
                continue
            mint = row.get("mint")
            index = row.get("accountIndex")
            token = row.get("uiTokenAmount")
            if not isinstance(token, dict):
                continue
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
        "creations": pump_creations(tx),
        "type": "SWAP" if set(keys) & DEX_PROGRAMS else "TRANSFER",
        "nativeTransfers": native_transfers,
        "tokenTransfers": token_transfers,
    }


def entry_market_cap_usd(
    size_usd: float,
    out_amount_raw: int,
    supply_ui: float,
    decimals: int,
    supply_raw: int | None = None,
) -> float | None:
    """Fully-diluted value implied by the executable buy quote.

    When raw supply is available, decimals cancel because both the Jupiter output and
    mint supply use the token's base units.  Keeping this path integer-based prevents a
    bad decimal conversion or a rounded ``uiAmount`` from manufacturing a huge value.
    The UI-unit path remains for backtests and older callers.
    """
    if out_amount_raw <= 0:
        return None
    if supply_raw is not None:
        if supply_raw <= 0:
            return None
        return float(Decimal(str(size_usd)) * Decimal(supply_raw) / Decimal(out_amount_raw))
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
    *,
    curve_tx_count: int | None = None,
    creator_prior_launches: int | None = None,
    creator_hold_pct: float | None = None,
    early_sell_pct: float | None = None,
    early_seller: str | None = None,
    runner: bool = False,
    copy_trade: bool = False,
) -> str | None:
    """Reject entries that are no longer the trade the backtest models.

    Unknown inputs (None) never block: a failed metadata lookup is logged, not traded on.
    The one exception is the bundle snapshot, which fails closed when BUNDLE_FAIL_CLOSED is set.
    A runner entry (watchlist token that grew into the swing band) is late by design: it skips
    the lateness check and uses the runner market-cap band instead of the graduation one."""
    if not runner and not copy_trade:
        lateness = now - (graduated_ts + cfg.entry_delay_seconds)
        if lateness > cfg.max_entry_lateness_seconds:
            return f"stale entry: {lateness:.0f}s past target"
    if price_impact_pct is not None and price_impact_pct > cfg.max_price_impact_pct:
        return f"price impact {price_impact_pct:.1f}% > {cfg.max_price_impact_pct:.1f}% (pool too thin for our size)"
    if copy_trade:
        pass  # the copied wallet chose the cap; no band applies
    elif runner:
        if market_cap_usd is not None and market_cap_usd > cfg.runner_max_market_cap_usd > 0:
            return f"market cap ${market_cap_usd:,.0f} > ${cfg.runner_max_market_cap_usd:,.0f} (above the runner band)"
        if market_cap_usd is not None and market_cap_usd < cfg.runner_min_market_cap_usd:
            return f"market cap ${market_cap_usd:,.0f} < ${cfg.runner_min_market_cap_usd:,.0f} (fell out of the runner band)"
    elif (
        market_cap_usd is not None
        and cfg.max_entry_market_cap_usd > 0
        and market_cap_usd > cfg.max_entry_market_cap_usd
    ):
        return (
            f"market cap ${market_cap_usd:,.0f} > ${cfg.max_entry_market_cap_usd:,.0f} "
            "(already pumped far past graduation)"
        )
    elif (
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
            "(observed history does not meet minimum curve age)"
        )
    if (
        curve_tx_count is not None
        and cfg.min_curve_transactions > 0
        and curve_tx_count < cfg.min_curve_transactions
    ):
        return (
            f"curve filled with {curve_tx_count} successful transactions < {cfg.min_curve_transactions} "
            "(one-party fill)"
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
    if (
        creator_hold_pct is not None
        and cfg.max_creator_hold_pct > 0
        and creator_hold_pct > cfg.max_creator_hold_pct
    ):
        return (
            f"creator still holds {creator_hold_pct:.1f}% of supply > {cfg.max_creator_hold_pct:.0f}% "
            "(the wallet that dumps)"
        )
    if (
        creator_prior_launches is not None
        and cfg.max_creator_prior_launches > 0
        and creator_prior_launches > cfg.max_creator_prior_launches
    ):
        return (
            f"creator launched {creator_prior_launches} prior Pump.fun tokens > {cfg.max_creator_prior_launches} "
            "(launch factory)"
        )
    if (
        early_sell_pct is not None
        and cfg.max_early_sell_pct > 0
        and early_sell_pct > cfg.max_early_sell_pct
    ):
        who = f" [{early_seller}]" if early_seller else ""
        return (
            f"a wallet already sold {early_sell_pct:.1f}% of supply since migration > {cfg.max_early_sell_pct:.0f}% "
            f"(dump started before entry){who}"
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
        self._method_cooldowns: dict[tuple[str, str], float] = {}
        self._provider_lock = threading.Lock()
        self._holder_cache: dict[tuple, tuple[float, list[tuple[str, int]]]] = {}
        self._creator_by_mint: dict[str, str] = {}
        self.last_history_sample: dict[str, Any] | None = None
        # The standard holder lookup starts from token accounts, then resolves their
        # wallet owners. Keep that relationship for the entry's bundle screen: those
        # token-account histories are small and directly relevant, unlike the shared
        # bonding curve which can contain thousands of unrelated swaps.
        self._holder_token_accounts: dict[tuple, tuple[float, dict[str, list[str]]]] = {}
        self._creation_cache: dict[str, dict[str, Any]] = {}
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

    def _endpoint_order(self, methods: tuple[str, ...] = ()) -> list[tuple[int, str]]:
        total = len(self.cfg.rpc_urls)
        with self._provider_lock:
            return [(index, self.cfg.rpc_urls[index])
                    for index in [(self._active_endpoint + offset) % total for offset in range(total)]
                    if all(self._method_cooldowns.get((self.cfg.rpc_urls[index], method), 0) <= time.monotonic()
                           for method in methods)]

    def _cool_method(self, endpoint: str, method: str, exc: BaseException) -> None:
        status = getattr(getattr(exc, 'response', None), 'status_code', 0)
        delay = 900.0 if status in {401, 403} else 30.0
        with self._provider_lock:
            self._method_cooldowns[endpoint, method] = time.monotonic() + delay
        log(f"WARN RPC method={method} provider={redact_endpoint(endpoint)} "
            f"error={status or type(exc).__name__} cooldown={delay:.0f}s")

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
        for index, endpoint in self._endpoint_order((method,)):
            try:
                body = self._post(
                    endpoint,
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout,
                )
                if not isinstance(body, dict):
                    raise RuntimeError(f"malformed RPC response for {method}: {str(body)[:120]}")
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
                self._cool_method(endpoint, method, exc)
        if last_error is None:
            raise RuntimeError(f"service unavailable: all providers cooling down for {method}")
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
                if 'result' not in item and 'error' not in item:
                    raise RuntimeError(f"malformed RPC response: batch missing {method} result")
                if "error" in item:
                    raise RuntimeError(f"RPC {method}: {item['error']}")
                results.append(item.get("result"))
        return results

    def _batch_post(self, payload: list[dict[str, Any]], timeout: float | None = None) -> list[dict[str, Any]]:
        last_error: BaseException | None = None
        methods = tuple(sorted({item['method'] for item in payload}))
        for index, endpoint in self._endpoint_order(methods):
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
                for method in methods:
                    self._cool_method(endpoint, method, exc)
        if last_error is None:
            raise RuntimeError("service unavailable: all providers cooling down for batch")
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

    def token_supply_details(self, mint: str) -> tuple[float, int, int]:
        """Validated ``(UI supply, decimals, raw supply)`` for a mint."""
        value = self.call("getTokenSupply", [mint])["value"]
        raw = int(value["amount"])
        decimals = int(value["decimals"])
        ui_text = str(value["uiAmountString"])
        try:
            ui_decimal = Decimal(ui_text)
        except InvalidOperation as exc:
            raise RuntimeError("invalid token supply UI amount") from exc
        expected_ui = Decimal(raw).scaleb(-decimals)
        if ui_decimal != expected_ui:
            raise RuntimeError(
                f"inconsistent token supply: raw={raw} decimals={decimals} ui={ui_text}"
            )
        return float(ui_decimal), decimals, raw

    def token_supply(self, mint: str) -> tuple[float, int]:
        """Compatibility view: ``(supply in UI units, decimals)``."""
        supply_ui, decimals, _ = self.token_supply_details(mint)
        return supply_ui, decimals

    def mint_first_seen(self, mint: str, stop_before_ts: float, max_pages: int = 30) -> float | None:
        """Return verified creation only; never reuse a lower-bound activity timestamp.

        Retains the public method name for callers. Signature work uses the same bounded
        oldest-first path as funding lookup, starting at the quieter curve PDA.
        """
        curve = bonding_curve_address(mint)
        transactions = self.raw_transactions(curve, **{
            'sort-order': 'asc', 'limit': 25, 'max-pages': max_pages,
        })
        creations = [c for tx in transactions for c in tx.get('creations', [])
                     if c['mint'] == mint]
        if creations:
            created = min(creations, key=lambda c: c['timestamp'])
            if created.get('creator'):
                self._creator_by_mint[mint] = created['creator']
            if len(self._creation_cache) >= 1000:
                self._creation_cache.pop(next(iter(self._creation_cache)))
            self._creation_cache[mint] = dict(created)
            log(f"CURVE mint={mint} evidence=verified_creation created_ts={created['timestamp']} "
                f"creation_slot={created['slot']} creator={created.get('creator')}")
            return float(created['timestamp'])
        times = [tx['timestamp'] for tx in transactions if tx.get('timestamp') is not None]
        evidence = 'minimum_age_only' if times and min(times) <= stop_before_ts else 'unknown'
        log(f"CURVE mint={mint} evidence={evidence} creation_unverified=True")
        return None

    def mint_creator(self, mint: str) -> str | None:
        """Creator argument of the verified Create instruction, remembered by mint_first_seen."""
        return self._creator_by_mint.get(mint)

    def curve_transaction_count(self, mint: str, created_ts: float, graduated_ts: float) -> int:
        """Successful transactions on the bonding curve between creation and graduation, from
        signature metadata only (no transaction bodies). Capped at two pages of 1,000: anything
        past that is already far above any sane floor."""
        curve = bonding_curve_address(mint)
        count = 0
        before: str | None = None
        for _ in range(2):
            options: dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
            if before:
                options["before"] = before
            page = self.call("getSignaturesForAddress", [curve, options])
            if not isinstance(page, list):
                raise RuntimeError("curve signature data incomplete")
            times = []
            for row in page:
                if not isinstance(row, dict):
                    continue
                block_time = row.get("blockTime")
                if block_time is None:
                    continue
                times.append(block_time)
                if row.get("err") is None and created_ts - 2 <= block_time <= graduated_ts + 2:
                    count += 1
            if len(page) < 1000 or (times and min(times) < created_ts - 2):
                break
            before = page[-1].get("signature") if isinstance(page[-1], dict) else None
            if not before:
                break
        return count

    def creator_profile(self, creator: str, mint: str, before_ts: float) -> dict[str, Any]:
        """Prior Pump.fun launches by this creator, from a bounded sample of its history before
        this launch, cached for a day. Counts verified Create instructions only, so it never
        mistakes ordinary activity for a launch; a busy legitimate wallet undercounts."""
        cache = self.wallet_graph_cache.setdefault("creators", {})
        cached = cache.get(creator)
        if isinstance(cached, dict) and now_ts() - float(cached.get("checked_at") or 0) <= 86400:
            return cached
        page = self.call(
            "getSignaturesForAddress",
            [creator, {"limit": self.cfg.creator_history_signatures, "commitment": "confirmed"}],
        )
        if not isinstance(page, list):
            raise RuntimeError("creator signature data incomplete")
        rows = [
            r for r in page
            if isinstance(r, dict) and r.get("signature") and r.get("err") is None
            and r.get("blockTime") is not None and r["blockTime"] < before_ts
        ]
        times = [r["blockTime"] for r in rows]
        sample = rows[: self.cfg.creator_history_decode_limit]
        calls = [
            ("getTransaction", [r["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                "maxSupportedTransactionVersion": 0}])
            for r in sample
        ]
        bodies: list[Any] = []
        for start in range(0, len(calls), self.cfg.rpc_batch_size):
            bodies.extend(self.batch_call(
                calls[start:start + self.cfg.rpc_batch_size],
                timeout=self.cfg.bundle_lookup_timeout_ms / 1000 * 2,
            ))
        launches = 0
        for body in bodies:
            if isinstance(body, dict):
                launches += sum(
                    1 for c in pump_creations(body) if c["creator"] == creator and c["mint"] != mint
                )
        profile = {
            "prior_launches": launches,
            "sampled_signatures": len(rows),
            "decoded": len(sample),
            "first_seen_ts": min(times) if times else None,
            "checked_at": now_ts(),
        }
        cache[creator] = profile
        return profile

    def largest_seller_since(
        self, mint: str, since_ts: float, supply_raw: int, exclude: set[str] | None = None, limit: int = 100
    ) -> tuple[str, float] | None:
        """(wallet, percent of supply) for the plain wallet with the largest net token outflow
        since `since_ts`, read from the mint's most recent transactions. Program-owned accounts
        are ignored: the pool's vault drains on every buy and the BOOST burn empties a vault.
        None when no wallet has sold."""
        exclude = exclude or set()
        page = self.call("getSignaturesForAddress", [mint, {"limit": limit, "commitment": "confirmed"}])
        if not isinstance(page, list):
            raise RuntimeError("mint signature data incomplete")
        rows = [
            r for r in page
            if isinstance(r, dict) and r.get("signature") and r.get("err") is None
            and (r.get("blockTime") or 0) >= since_ts - 2
        ]
        if not rows:
            return None
        calls = [
            ("getTransaction", [r["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                "maxSupportedTransactionVersion": 0}])
            for r in rows
        ]
        bodies: list[Any] = []
        for start in range(0, len(calls), self.cfg.rpc_batch_size):
            bodies.extend(self.batch_call(
                calls[start:start + self.cfg.rpc_batch_size],
                timeout=self.cfg.bundle_lookup_timeout_ms / 1000 * 2,
            ))
        net: dict[str, int] = {}
        for body in bodies:
            if not isinstance(body, dict):
                continue
            meta = body.get("meta") or {}
            balances: dict[int, tuple[str, int, int]] = {}  # account index -> (owner, pre, post)
            for row in meta.get("preTokenBalances") or []:
                if row.get("mint") == mint and row.get("owner") is not None:
                    idx = int(row.get("accountIndex"))
                    amount = int(((row.get("uiTokenAmount") or {}).get("amount")) or 0)
                    balances[idx] = (row["owner"], amount, 0)
            for row in meta.get("postTokenBalances") or []:
                if row.get("mint") == mint and row.get("owner") is not None:
                    idx = int(row.get("accountIndex"))
                    amount = int(((row.get("uiTokenAmount") or {}).get("amount")) or 0)
                    owner, pre, _ = balances.get(idx, (row["owner"], 0, 0))
                    balances[idx] = (owner, pre, amount)
            for owner, pre, post in balances.values():
                net[owner] = net.get(owner, 0) + (post - pre)
        sellers = {owner: -delta for owner, delta in net.items() if delta < 0 and owner not in exclude}
        if not sellers or supply_raw <= 0:
            return None
        candidates = sorted(sellers.items(), key=lambda item: item[1], reverse=True)[:10]
        accounts = self.call(
            "getMultipleAccounts", [[owner for owner, _ in candidates], {"encoding": "base64"}]
        ).get("value") or []
        for (owner, sold), acct in zip(candidates, accounts):
            program = acct["owner"] if acct else SYSTEM_PROGRAM
            if program == SYSTEM_PROGRAM:
                return owner, sold / supply_raw * 100
        return None

    def plain_wallet_holders(
        self, mint: str, exclude: set[str] | None = None, limit: int = 20
    ) -> list[tuple[str, int]]:
        key = (mint, tuple(sorted(exclude or set())))
        cached = self._holder_cache.get(key)
        if cached is None or time.monotonic() - cached[0] >= 10:
            rows = self._plain_wallet_holders_uncached(mint, exclude, max(limit, self.cfg.bundle_max_wallets))
            # Share the fresh snapshot between top-holder and bundle checks in this entry.
            self._holder_cache = {k: v for k, v in self._holder_cache.items() if time.monotonic() - v[0] < 10}
            self._holder_token_accounts = {
                k: v for k, v in self._holder_token_accounts.items()
                if time.monotonic() - v[0] < 10
            }
            self._holder_cache[key] = (time.monotonic(), rows)
        return self._holder_cache[key][1][:limit]

    def _plain_wallet_holders_uncached(
        self, mint: str, exclude: set[str] | None = None, limit: int = 20
    ) -> list[tuple[str, int]]:
        """Largest plain-wallet holders, combining multiple token accounts per owner.

        Optional Metaplex DAS is used first because standard getTokenLargestAccounts is
        hard-capped at 20. DAS is disabled by default so unsupported extension calls do not
        burn free-provider quota; the portable top-20 path remains the default.
        """
        exclude = exclude or set()
        cache_key = (mint, tuple(sorted(exclude)))
        token_accounts_by_owner: dict[str, list[str]] = {}
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
                    address = (row.get("address") or row.get("token_account")
                               or row.get("tokenAccount"))
                    if address:
                        token_accounts_by_owner.setdefault(owner, []).append(str(address))
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
                    wallet_set = {owner for owner, _ in wallets}
                    mapped = {owner: token_accounts_by_owner.get(owner, [])
                              for owner in wallet_set if token_accounts_by_owner.get(owner)}
                    if len(mapped) == len(wallet_set):
                        self._holder_token_accounts[cache_key] = (time.monotonic(), mapped)
                        return wallets
        except Exception:
            # DAS availability varies by provider/plan; retain the standard 20-account path.
            pass
        largest = self.call("getTokenLargestAccounts", [mint]).get("value") or []
        if not largest:
            return []
        addresses = [entry["address"] for entry in largest]
        accounts = self.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}]).get("value") or []
        if len(accounts) != len(addresses):
            raise RuntimeError("holder account data incomplete")
        # A token account can close between the two calls (a holder dumping and closing during
        # a launch is normal). Tolerate a few missing entries; refuse only when most are gone.
        missing = sum(1 for a in accounts if a is None)
        if missing and missing > len(addresses) // 5:
            raise RuntimeError(f"holder account data incomplete ({missing} of {len(addresses)} accounts missing)")
        owners: list[tuple[str, str, int]] = []
        for entry, acct in zip(largest, accounts):
            if not acct:
                continue
            try:
                owner = acct["data"]["parsed"]["info"]["owner"]
            except (KeyError, TypeError):
                continue
            owners.append((entry["address"], owner, int(entry["amount"])))
        if not owners:
            return []
        owner_accounts = self.call(
            "getMultipleAccounts", [[owner for _, owner, _ in owners], {"encoding": "base64"}]
        ).get("value") or []
        if len(owner_accounts) != len(owners):
            raise RuntimeError("holder owner data incomplete")
        totals: dict[str, int] = {}
        for (address, owner, amount), acct in zip(owners, owner_accounts):
            program = acct["owner"] if acct else SYSTEM_PROGRAM  # unfunded wallet: still a wallet
            if program != SYSTEM_PROGRAM or owner in exclude:
                continue
            totals[owner] = totals.get(owner, 0) + amount
            token_accounts_by_owner.setdefault(owner, []).append(address)
        self._holder_token_accounts[cache_key] = (
            time.monotonic(), token_accounts_by_owner,
        )
        return sorted(totals.items(), key=lambda row: row[1], reverse=True)[:limit]

    def holder_history_addresses(
        self, mint: str, holders: list[tuple[str, int]], exclude: set[str] | None = None
    ) -> list[str]:
        """Token accounts whose histories cover the current plain-wallet holders."""
        key = (mint, tuple(sorted(exclude or set())))
        cached = self._holder_token_accounts.get(key)
        if cached is None or time.monotonic() - cached[0] >= 10:
            return []
        accounts = cached[1]
        if any(not accounts.get(wallet) for wallet, _ in holders):
            # Never present a partial holder-history sample as a clean screen.
            return []
        # Preserve holder rank and de-duplicate accounts shared through malformed data.
        return list(dict.fromkeys(
            address for wallet, _ in holders for address in accounts.get(wallet, [])
        ))

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
        gte = int(params.get("gte-time") or 0)
        lte = int(params.get("lte-time") or 2**63 - 1)
        # Signature metadata is cheap compared with full transaction bodies. The old
        # 40-row page cap searched only 120 signatures, including failed transactions,
        # and could never reach the curve window on an active launch.
        oldest_first = params.get('sort-order') == 'asc' and not gte
        page_size = 1000 if gte or oldest_first else 100
        options["limit"] = page_size
        # A healthy, busy launch can exceed 500 successful curve transactions in the
        # roughly two-minute observation window. Rejecting it solely because activity
        # was high starves the bundle gate of exactly the launches it should analyze.
        # One full RPC signature page remains a hard upper bound so this work cannot
        # grow without limit.
        body_limit = 1000 if gte else limit
        started = time.monotonic()
        deadline = started + 5.0
        signatures = []
        # Read backwards to the requested window instead of filtering only the latest
        # page (which silently returned an empty early-buy/funding history).
        reached_window = False
        for page_number in range(1, min(30, max(1, int(params.get('max-pages', 3)))) + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('history window unavailable: signature search time budget exhausted')
            page = self.call("getSignaturesForAddress", [address, dict(options)], timeout=remaining)
            if not isinstance(page, list):
                raise RuntimeError('history signature data incomplete')
            if any(not isinstance(r, dict) or not r.get('signature') for r in page):
                raise RuntimeError('history signature data incomplete')
            seen = {r['signature'] for r in signatures}
            fresh = [r for r in page if r['signature'] not in seen]
            if page and not fresh:
                raise RuntimeError('history window unavailable: pagination made no progress')
            signatures.extend(fresh)
            times = [r['blockTime'] for r in page if r.get('blockTime') is not None]
            eligible = [r for r in signatures if r.get('signature') and not r.get('err')
                        and r.get('blockTime') is not None and gte <= r['blockTime'] <= lte]
            if gte:
                reached_window = len(page) < page_size or bool(times and min(times) < gte)
            else:
                # Funding histories are bounded samples, not exhaustive wallet ancestry.
                # Oldest-first is meaningful only after reaching the history boundary.
                # Reversing a recent sample cannot recover its original funder.
                reached_window = len(page) < page_size or (not oldest_first and len(eligible) >= limit)
            if reached_window:
                break
            if not page or not page[-1].get('signature'):
                break
            options['before'] = page[-1]['signature']
        if not reached_window:
            raise RuntimeError(f'history window unavailable after {page_number} pages / {len(signatures)} signatures')
        # A successful transaction with no timestamp cannot be silently excluded from
        # a time-window analysis. Missing evidence is not a zero-risk observation.
        if any(not r.get('err') and r.get('blockTime') is None for r in signatures):
            raise RuntimeError('history window unavailable: successful signature missing block time')
        rows = [
            row for row in signatures
            if row.get("signature") and not row.get("err") and gte <= int(row.get("blockTime") or 0) <= lte
        ]
        self.last_history_sample = None
        if gte and len(rows) > body_limit:
            # A window busier than the decode budget is evidence of a crowd, not a reason to
            # refuse the token. Decode the earliest rows, where creation, dev buys and bundles
            # live, and report how much of the window that covered.
            self.last_history_sample = {"address": address, "total": len(rows), "decoded": body_limit}
            rows = rows[-body_limit:]  # rows are newest-first here; the earliest sit at the end
        if not gte:
            rows = rows[-limit:] if oldest_first else rows[:limit]
        sampled = f" sampled={self.last_history_sample['decoded']}/{self.last_history_sample['total']}" if self.last_history_sample else ""
        log(f"HISTORY address={address} pages={page_number} signatures={len(signatures)} "
            f"selected={len(rows)} window={'covered' if gte else 'oldest_sample' if oldest_first else 'recent_sample'}"
            f"{sampled} fetch_seconds={time.monotonic() - started:.2f}")
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
        # A covered curve window must be decoded in full, never truncated into an
        # apparently clean snapshot. Bound work between batches to protect exits.
        bodies = []
        # Keep the old eight-second budget for ordinary launches, while allowing a
        # full 1,000-row window enough time to finish its bounded RPC batches.
        batch_count = (len(calls) + self.cfg.rpc_batch_size - 1) // self.cfg.rpc_batch_size
        decode_deadline = time.monotonic() + min(20.0, max(8.0, batch_count * 0.5))
        for start in range(0, len(calls), self.cfg.rpc_batch_size):
            remaining = decode_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('history transaction decode time budget exhausted')
            chunk = calls[start:start + self.cfg.rpc_batch_size]
            bodies.extend(self.batch_call(
                chunk, timeout=min(remaining, self.cfg.bundle_lookup_timeout_ms / 1000)
            ))
        if len(bodies) != len(rows) or any(not isinstance(body, dict) for body in bodies):
            raise RuntimeError('history transaction data incomplete')
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
        """Earliest meaningful inbound SOL sender in an oldest-first pre-graduation sample."""
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
        semantics = f"oldest-v2:{self.cfg.transaction_history_mode}:{self.cfg.raw_funder_signature_limit}"
        if isinstance(cached, dict):
            age = now - float(cached.get("checked_at") or 0)
            ttl = self.cfg.wallet_graph_cache_days * 86400 if cached.get("funder") else 3600
            if (age <= ttl and cached.get('semantics') == semantics
                    and cached.get('before_ts') == before_ts):
                return cached.get("funder")
        funder = self.origin_funder(wallet, before_ts)
        entries[wallet] = {"funder": funder, "checked_at": now,
                           "semantics": semantics, "before_ts": before_ts}
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
        log(f"HOLDERS mint={mint} requested_wallets={self.cfg.bundle_max_wallets} "
            f"observed_wallets={len(holders)} source={'das_or_standard_fallback' if self.cfg.rpc_das_enabled else 'standard_top20_accounts'}")
        if len(holders) < 2:
            return {"complete": False, "error": "fewer than two plain-wallet holders returned"}
        wallet_amounts = {wallet: raw / (10 ** decimals) for wallet, raw in holders}
        holder_set = set(wallet_amounts)
        history_address = bonding_curve_address(mint) if self.cfg.transaction_history_mode == 'raw' else mint
        history_params: dict[str, Any] = {
            "sort-order": "asc",
            "gte-time": int(created_ts) - 2,
            "lte-time": int(graduated_ts + self.cfg.entry_delay_seconds) + 2,
            "limit": 100,
        }
        if self.cfg.transaction_history_mode == 'raw':
            # Creation lookup already searches this deeply. Bundle analysis must cover
            # the same curve lifetime instead of reintroducing the old 3,000-signature
            # ceiling after creation was found. raw_transactions retains its five-second
            # signature-search deadline and bounded transaction decode budget.
            history_params["max-pages"] = 30
        creation = self._creation_cache.get(mint)
        targeted_addresses = (
            self.holder_history_addresses(mint, holders, exclude)
            if self.cfg.transaction_history_mode == 'raw' and creation
            else []
        )
        if targeted_addresses:
            # A transaction can touch several sampled token accounts. Count it once in
            # the final evidence set. Token accounts are mint-specific, so their earliest
            # activity is the acquisition/distribution evidence we need; there is no value
            # in decoding every later swap on the shared curve. A one-page boundary keeps
            # this screen cheap, and raw_transactions fails closed for an exceptionally
            # active sampled account rather than silently treating a recent sample as old.
            targeted_history_params = {
                "sort-order": "asc",
                "lte-time": history_params["lte-time"],
                "limit": 25,
                "max-pages": 1,
            }
            by_signature: dict[str, dict[str, Any]] = {}
            for address in targeted_addresses:
                for tx in self.enhanced_transactions(address, **targeted_history_params):
                    signature = tx.get("signature")
                    if not signature:
                        return {"complete": False, "error": "targeted holder history missing signature"}
                    by_signature.setdefault(str(signature), tx)
            transactions = sorted(
                by_signature.values(), key=lambda tx: (int(tx.get("slot") or 0), str(tx.get("signature") or ""))
            )
            log(f"BUNDLE_HISTORY mint={mint} mode=targeted holder_accounts={len(targeted_addresses)} "
                f"transactions={len(transactions)}")
        else:
            # Compatibility fallback for enhanced history providers and callers that did
            # not perform the holder/creation lookups through this Rpc instance.
            transactions = self.enhanced_transactions(history_address, **history_params)
        if targeted_addresses:
            history_total = history_decoded = len(transactions)
        else:
            history_sample = getattr(self, "last_history_sample", None)
            history_total = history_sample["total"] if history_sample else len(transactions)
            history_decoded = history_sample["decoded"] if history_sample else len(transactions)
        buys: list[dict[str, Any]] = []
        transfer_edges: list[tuple[str, str]] = []
        creator = None
        create_slot = None
        if self.cfg.transaction_history_mode == 'raw':
            if targeted_addresses and creation:
                creator = creation.get('creator')
                create_slot = int(creation['slot'])
            else:
                creations = [c for tx in transactions for c in tx.get('creations', [])
                             if c['mint'] == mint and c['timestamp'] == created_ts]
                if not creations:
                    return {'complete': False, 'error': 'verified creation missing from curve history'}
                creator = creations[0]['creator']
                create_slot = int(creations[0]['slot'])
        for tx in transactions:
            slot = tx.get("slot")
            if slot is None:
                continue
            if self.cfg.transaction_history_mode != 'raw' and (create_slot is None or int(slot) < create_slot):
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
            "history_total": history_total,
            "history_decoded": history_decoded,
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

        self.notifications = 0
        self.failed_notifications = 0
        self.dropped_signatures = 0
        self.last_notification_ts = 0.0

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="migration-stream", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        cooldowns: dict[str, float] = {}
        failures: dict[str, int] = {}
        endpoint_index = 0
        while not self.stop_event.is_set():
            endpoints = self.cfg.rpc_ws_urls
            endpoint = None
            for offset in range(len(endpoints)):
                index = (endpoint_index + offset) % len(endpoints)
                candidate = endpoints[index]
                if cooldowns.get(candidate, 0) <= time.monotonic():
                    endpoint = candidate
                    endpoint_index = (index + 1) % len(endpoints)
                    break
            if endpoint is None:
                delay = max(0.1, min(cooldowns.values()) - time.monotonic())
                self.stop_event.wait(delay)
                continue
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
                if not isinstance(response, dict) or response.get("id") != 1 or not isinstance(response.get("result"), int):
                    raise RuntimeError("logsSubscribe did not acknowledge the subscription")
                self.connected = True
                failures[endpoint] = 0
                log(f"migration WebSocket connected: {redact_endpoint(endpoint.replace('wss://', 'https://').replace('ws://', 'http://'))}")
                while not self.stop_event.is_set():
                    try:
                        body = json.loads(conn.recv())
                    except websocket.WebSocketTimeoutException:
                        continue
                    value = (((body.get("params") or {}).get("result") or {}).get("value") or {})
                    signature = value.get("signature")
                    if signature:
                        self.notifications += 1
                        self.last_notification_ts = now_ts()
                        if value.get("err"):
                            self.failed_notifications += 1
                    if not signature or value.get("err"):
                        continue
                    try:
                        self.signatures.put_nowait(signature)
                    except queue.Full:
                        self.dropped_signatures += 1
                        # Catch-up polling is authoritative; dropping the oldest live hint is safe.
                        try:
                            self.signatures.get_nowait()
                        except queue.Empty:
                            pass
                        self.signatures.put_nowait(signature)
            except Exception as exc:
                self.connected = False
                failures[endpoint] = min(10, failures.get(endpoint, 0) + 1)
                delay = min(self.cfg.provider_backoff_max_seconds, 30.0 * 2 ** (failures[endpoint] - 1))
                cooldowns[endpoint] = time.monotonic() + delay
                host = redact_endpoint(endpoint.replace('wss://', 'https://').replace('ws://', 'http://'))
                log(f"WARN migration WebSocket {host} unavailable ({type(exc).__name__}); cooling down for {delay:.0f}s; trying another available endpoint")
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
        self._next_heartbeat = 0.0
        self._last_scan_ts = 0.0
        self._discovery_counts: dict[str, int] = {}
        self._next_position_log: dict[str, float] = {}
        # Keep pending entries in the persisted state.  A deploy should not silently forget the
        # queue and then rediscover/process an unbounded burst before checking open positions.
        self.pending: list[dict[str, Any]] = self.state.setdefault("pending", [])
        self._prune_stale_pending()
        self.last_entry_meta: dict[str, Any] = {}

    # ---- pricing helpers -------------------------------------------------
    def sol_price_usd(self) -> float:
        """SOL/USD from a 1 SOL -> USDC quote, cached for 30s: it only converts position sizes and
        P&L, and re-quoting it every 5s loop was a third of our Jupiter request budget."""
        cached = getattr(self, "_sol_price", None)
        if cached and now_ts() - cached[0] < 60:
            return cached[1]
        try:
            quote = self.jup.quote(WSOL, USDC, LAMPORTS)  # 1 SOL -> USDC (6 decimals)
        except Exception as exc:
            if cached and is_rate_limited(exc):
                # A stale SOL price only skews sizing by a few cents; a 429 here must not
                # abort the whole cycle and with it every exit check.
                self._sol_price = (now_ts() - 30, cached[1])
                return cached[1]
            raise
        price = int(quote["outAmount"]) / 1e6
        self._sol_price = (now_ts(), price)
        return price

    def spendable_usd(self, sol_price: float) -> float:
        """Free SOL above the fee reserve, the most a single buy can spend."""
        if self.cfg.mode == "paper":
            return float(self.state["paper_balance_usd"])
        spendable = max(0.0, self.rpc.sol_balance(self.wallet.pubkey) - self.cfg.min_sol_reserve)
        return spendable * sol_price

    def deployed_usd(self) -> float:
        """Current value of every open position (last mark, else its basis)."""
        return sum(float(p.get("last_value_usd") or p.get("position_usd") or 0.0) for p in self.state["positions"])

    def equity_usd(self, sol_price: float) -> float:
        """The whole account: free SOL plus open positions, so a fixed fraction of it is the
        same dollar size for the first copy and the tenth."""
        return self.spendable_usd(sol_price) + self.deployed_usd()

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

    def discovery_result(self, signature: str, outcome: str) -> None:
        self._discovery_counts[outcome] = self._discovery_counts.get(outcome, 0) + 1
        log(f"DECODE signature={signature} result={outcome}")

    def log_heartbeat(self) -> None:
        now = now_ts()
        if now < self._next_heartbeat:
            return
        self._next_heartbeat = now + 30.0
        stream = self.migration_stream
        ws = "off" if stream is None else ("connected" if stream.connected else "disconnected")
        scan_age = f"{now - self._last_scan_ts:.0f}s" if self._last_scan_ts else "never"
        event_ts = getattr(stream, "last_notification_ts", 0)
        event_age = f"{now - event_ts:.0f}s" if event_ts else "never"
        cooldown = max(0.0, float(getattr(self, "_provider_cooldown_until", 0)) - now)
        log(
            f"HEARTBEAT ws={ws} notifications={getattr(stream, 'notifications', 0)} "
            f"failed_notifications={getattr(stream, 'failed_notifications', 0)} "
            f"ws_queue={stream.signatures.qsize() if stream else 0} "
            f"dropped={getattr(stream, 'dropped_signatures', 0)} last_event={event_age} "
            f"last_scan={scan_age} pending={len(self.pending)} "
            f"positions={len(self.state['positions'])} stuck={len(self.state.get('stuck', []))} "
            f"moon_bags={len(self.state.get('moon_bags', []))} watchlist={len(self.state.get('watchlist', []))} "
            f"draining={self.state.get('draining', False)} cooldown={cooldown:.0f}s "
            f"reconcile_pending={getattr(self, '_reconcile_pending', False)} "
            f"decode_totals={json.dumps(self._discovery_counts, sort_keys=True)}"
        )

    def _queue_graduation(self, signature: str, timestamp_hint: float | None = None) -> None:
        if not signature or signature in self.state["seen_signatures"]:
            return
        log(f"DECODE fetching signature={signature}")
        tx = self.rpc.transaction(signature)
        if not tx:
            self.discovery_result(signature, "transaction_unavailable_retry_on_catchup")
            return  # confirmed data may lag briefly; periodic catch-up will retry it
        timestamp = tx.get("blockTime") or timestamp_hint
        if not timestamp:
            self.discovery_result(signature, "timestamp_unavailable_retry_on_catchup")
            return
        self.state["seen_signatures"].append(signature)
        mints = candidate_mints_from_rpc_transaction(tx)
        if len(mints) != 1:
            self.discovery_result(signature, "no_candidate_mint" if not mints else "ambiguous_mints")
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
            self.discovery_result(signature, "mint_already_pending_or_owned")
            return
        age = now_ts() - int(timestamp)
        self.watch_runner(mint, int(timestamp))
        if self.cfg.runner_only:
            self.discovery_result(signature, "watchlist_only")
            return
        if age > self.cfg.max_entry_age_seconds:
            self.discovery_result(signature, "too_old")
            self.skip(mint, f"graduation too old at detection ({age:.0f}s)")
            return
        enter_at = int(timestamp) + self.cfg.entry_delay_seconds
        self.pending.append({"mint": mint, "graduated_ts": int(timestamp), "enter_at": enter_at})
        self.discovery_result(signature, "queued")
        log(
            f"DETECTED graduation {mint} (age {age:.0f}s, "
            f"entering at +{self.cfg.entry_delay_seconds:.0f}s)"
        )

    def poll_graduations(self) -> None:
        if now_ts() < float(getattr(self, "_provider_cooldown_until", 0.0)):
            return
        signatures = self.migration_stream.drain() if self.migration_stream else []
        try:
            if signatures:
                log(f"STREAM drained={len(signatures)} unseen={sum(s not in self.state['seen_signatures'] for s in signatures)}")
            for signature in signatures:
                self._queue_graduation(signature)
            if now_ts() < self._next_discovery_catchup:
                return
            self._next_discovery_catchup = now_ts() + self.cfg.discovery_catchup_seconds
            started = time.monotonic()
            log(f"SCAN start address={self.cfg.migration_address} limit={self.cfg.discovery_poll_limit}")
            rows = self.rpc.call(
                "getSignaturesForAddress",
                [
                    self.cfg.migration_address,
                    {"limit": self.cfg.discovery_poll_limit, "commitment": "confirmed"},
                ],
                timeout=self.cfg.discovery_timeout_seconds,
            ) or []
            self._last_scan_ts = now_ts()
            newest = max((r.get('blockTime') or 0 for r in rows), default=0)
            newest_age = f"{now_ts() - newest:.0f}s" if newest else "unknown"
            log(
                f"SCAN fetched={len(rows)} unseen={sum(bool(r.get('signature')) and r['signature'] not in self.state['seen_signatures'] for r in rows)} "
                f"failed_txs={sum(bool(r.get('err')) for r in rows)} newest_age={newest_age} "
                f"provider={redact_endpoint(self.cfg.rpc_urls[self.rpc._active_endpoint])} "
                f"fetch_seconds={time.monotonic() - started:.2f}"
            )
            for row in reversed(rows):
                if not row.get("err"):
                    self._queue_graduation(row.get("signature") or "", row.get("blockTime"))
            log(f"SCAN complete pending={len(self.pending)} total_seconds={time.monotonic() - started:.2f}")
        except Exception as exc:
            if is_provider_unavailable(exc):
                delay = self.note_provider_rate_limit()
                log(f"WARN RPC discovery providers unavailable; pausing new-entry work for {delay:.0f}s")
            else:
                log(f"WARN RPC discovery failed: {describe_error(exc)}")
            return
        self._provider_backoff_seconds = 30.0

    # ---- runner watchlist ------------------------------------------------
    def watch_runner(self, mint: str, graduated_ts: int) -> None:
        """Put a graduation on the runner watchlist (newest first, bounded)."""
        cfg = self.cfg
        if not cfg.runner_enabled:
            return
        if now_ts() - graduated_ts > cfg.runner_watch_hours * 3600:
            return
        watch = self.state.setdefault("watchlist", [])
        if any(w["mint"] == mint for w in watch):
            return
        watch.insert(0, {"mint": mint, "graduated_ts": graduated_ts, "added_at": utc_iso(), "samples": [], "supply_raw": None, "decimals": None})
        del watch[cfg.runner_max_watch:]

    def token_prices(self, mints: list[str]) -> dict[str, tuple[float, int]]:
        """(usd price, decimals) per mint from Jupiter's batched price feed: one request values
        every open position, where a sell quote each would cost one request per position."""
        out: dict[str, tuple[float, int]] = {}
        for i in range(0, len(mints), 50):
            chunk = mints[i:i + 50]
            resp = self.jup.session.get(self.cfg.jupiter_price_url, params={"ids": ",".join(chunk)}, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", body) if isinstance(body, dict) else {}
            for mint in chunk:
                entry = data.get(mint) if isinstance(data, dict) else None
                if not isinstance(entry, dict):
                    continue
                try:
                    price = float(entry.get("usdPrice", entry.get("price")) or 0)
                    decimals = int(entry.get("decimals")) if entry.get("decimals") is not None else -1
                except (TypeError, ValueError):
                    continue
                if price > 0 and decimals >= 0:
                    out[mint] = (price, decimals)
        if WSOL in out:
            self._sol_price = (now_ts(), out[WSOL][0])
        return out

    def runner_prices(self, mints: list[str]) -> dict[str, float]:
        """USD price per mint from Jupiter's batched price feed; mints without a price are absent."""
        out: dict[str, float] = {}
        for i in range(0, len(mints), 50):
            chunk = mints[i:i + 50]
            resp = self.jup.session.get(self.cfg.jupiter_price_url, params={"ids": ",".join(chunk)}, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", body) if isinstance(body, dict) else {}
            for mint in chunk:
                entry = data.get(mint) if isinstance(data, dict) else None
                price = None
                if isinstance(entry, dict):
                    price = entry.get("usdPrice", entry.get("price"))
                try:
                    if price is not None and float(price) > 0:
                        out[mint] = float(price)
                except (TypeError, ValueError):
                    pass
        return out

    def manage_watchlist(self, sol_price: float) -> None:
        """Every RUNNER_CHECK_SECONDS: value every watched graduation, drop the dead and the
        expired, and enter the ones that climbed into the runner band with momentum."""
        cfg = self.cfg
        watch = self.state.get("watchlist") or []
        if not cfg.runner_enabled or not watch:
            return
        if now_ts() - float(self.state.get("watchlist_checked_ts") or 0) < cfg.runner_check_seconds:
            return
        self.state["watchlist_checked_ts"] = now_ts()
        now = now_ts()
        keep = []
        for w in watch:
            if now - float(w["graduated_ts"]) > cfg.runner_watch_hours * 3600:
                continue
            if any(p.get("mint") == w["mint"] for p in self.state["positions"]) or any(p.get("mint") == w["mint"] for p in self.pending):
                continue
            keep.append(w)
        watch[:] = keep
        if not watch:
            return
        try:
            prices = self.runner_prices([w["mint"] for w in watch])
        except Exception as exc:
            log(f"WARN runner price feed: {describe_error(exc)}")
            return
        candidates = []
        for w in watch:
            price = prices.get(w["mint"])
            if price is None:
                w["missing"] = int(w.get("missing", 0)) + 1
                continue
            w["missing"] = 0
            if w.get("supply_raw") is None:
                try:
                    _ui, decimals, raw = self.rpc.token_supply_details(w["mint"])
                    w["supply_raw"], w["decimals"] = raw, decimals
                except Exception as exc:
                    log(f"WARN runner supply {w['mint']}: {describe_error(exc)}")
                    continue
            mcap = price * int(w["supply_raw"]) / (10 ** int(w["decimals"]))
            samples = w.setdefault("samples", [])
            samples.append([round(now), round(mcap)])
            del samples[:-60]
            window = [m for t, m in samples if now - t <= cfg.runner_momentum_minutes * 60]
            low = min(window) if window else mcap
            gain_pct = (mcap / low - 1.0) * 100 if low > 0 else 0.0
            w["last_market_cap_usd"] = round(mcap)
            in_band = cfg.runner_min_market_cap_usd <= mcap <= (cfg.runner_max_market_cap_usd or float("inf"))
            if in_band and gain_pct >= cfg.runner_min_gain_pct and len(window) >= 2:
                candidates.append((gain_pct, w, mcap))
        # Watch tokens a price feed has forgotten for five checks in a row are dead.
        watch[:] = [w for w in watch if int(w.get("missing", 0)) < 5]
        if not candidates:
            return
        candidates.sort(key=lambda c: -c[0])
        for gain_pct, w, mcap in candidates[: cfg.max_entries_per_cycle]:
            log(f"RUNNER {w['mint']}: market cap ${mcap:,.0f} in band, +{gain_pct:.0f}% over {cfg.runner_momentum_minutes:.0f}m; entering")
            watch.remove(w)
            self.enter_with_retry({"mint": w["mint"], "graduated_ts": int(w["graduated_ts"]), "enter_at": now, "runner": True,
                                   "runner_market_cap_usd": round(mcap), "runner_gain_pct": round(gain_pct, 1)}, sol_price)

    def exit_cfg(self, pos: dict[str, Any]) -> "Config":
        """Exit thresholds for a position: runner and copied positions use their own settings."""
        if pos.get("copy") or (pos.get("adopted") and self.cfg.copy_only):
            prefix = "copy"          # a position picked up after a redeploy keeps the copy lane's exits
        elif pos.get("runner"):
            prefix = "runner"
        else:
            return self.cfg
        cfg = copy.copy(self.cfg)
        cfg.take_profit = getattr(self.cfg, f"{prefix}_take_profit")
        cfg.stop_loss = getattr(self.cfg, f"{prefix}_stop_loss")
        cfg.trailing_stop = getattr(self.cfg, f"{prefix}_trailing_stop")
        cfg.time_stop_minutes = getattr(self.cfg, f"{prefix}_time_stop_minutes")
        if pos.get("ladder"):
            # The ladder phases profit out; the flat take profit only backstops its top rung.
            cfg.take_profit = max(cfg.take_profit, max(r["x"] for r in pos["ladder"]) - 1.0)
        return cfg

    # ---- copy trading ----------------------------------------------------
    def poll_copy_wallets(self, sol_price: float) -> None:
        """Mirror new buys of the copied wallets and, when configured, their sells of tokens we
        hold because of them. The first look at a wallet only records a baseline."""
        cfg = self.cfg
        if not cfg.copy_wallets:
            return
        if now_ts() - float(self.state.get("copy_polled_ts") or 0) < cfg.copy_poll_seconds:
            return
        self.state["copy_polled_ts"] = now_ts()
        seen_all = self.state.setdefault("copy_seen", {})
        for wallet in cfg.copy_wallets:
            try:
                # A busy wallet (the FOMO quick-buy button fires ten or more trades a minute in
                # bursts) scrolls past a short window between polls, so read a long one; already
                # seen signatures cost nothing.
                rows = self.rpc.call("getSignaturesForAddress", [wallet, {"limit": 100, "commitment": "confirmed"}]) or []
            except Exception as exc:
                log(f"WARN copy poll {wallet[:8]}: {describe_error(exc)}")
                continue
            seen = seen_all.setdefault(wallet, [])
            baselined = self.state.setdefault("copy_baselined", [])
            if wallet not in baselined:
                # Baseline: everything older than the mirror window is history. Trades inside the
                # window (a buy made while we were restarting) are handled like any new trade.
                baselined.append(wallet)
                fresh = [r for r in rows if r.get("blockTime") and now_ts() - int(r["blockTime"]) <= cfg.copy_max_tx_age_seconds]
                seen.extend(r.get("signature") for r in rows if r.get("signature") and r not in fresh)
                log(f"COPY watching {wallet} (baseline {len(seen)} signatures, {len(fresh)} fresh; "
                    f"mirroring buys >= ${cfg.copy_wallet_min_usd.get(wallet, cfg.copy_min_buy_usd):,.0f} "
                    f"at {cfg.copy_wallet_size.get(wallet, 1.0):.0%} of the usual size)")
            for row in reversed(rows):
                sig = row.get("signature")
                if not sig or sig in seen:
                    continue
                seen.append(sig)
                del seen[:-1000]
                if row.get("err"):
                    continue
                block_time = row.get("blockTime")
                if block_time and now_ts() - int(block_time) > cfg.copy_max_tx_age_seconds:
                    log(f"COPY {wallet[:8]}: trade {sig[:12]}… is {now_ts() - int(block_time):.0f}s old; too late to mirror")
                    continue
                try:
                    tx = self.rpc.transaction(sig)
                except Exception as exc:
                    log(f"WARN copy decode {sig[:12]}…: {describe_error(exc)}")
                    continue
                swap = wallet_swap_from_transaction(tx or {}, wallet)
                if not swap:
                    continue
                usd = swap["sol"] * sol_price + swap.get("stable_usd", 0.0)
                mint = swap["mint"]
                if swap["side"] == "sell":
                    if not cfg.copy_follow_sells:
                        continue
                    # Any followed wallet selling a coin we hold is our cue to do the same, whichever
                    # lane or wallet got us in: selling most of its stack closes our position
                    # (close_position still keeps the moon bag), trimming part of it trims ours by
                    # the same share.
                    fraction = float(swap.get("fraction") or 1.0)
                    for pos in list(self.state["positions"]):
                        if pos.get("mint") != mint:
                            continue
                        how = "our copy" if pos.get("copy") == wallet else "our position"
                        try:
                            if fraction >= cfg.copy_full_sell_fraction:
                                log(f"COPY {wallet[:8]} sold {fraction:.0%} of {mint} (${usd:,.0f}); closing {how}")
                                self.close_position(pos, "copy_sell", sol_price)
                                continue
                            worth = fraction * float(pos.get("last_value_usd") or pos.get("position_usd") or 0.0)
                            if worth < 1.0:
                                log(f"COPY {wallet[:8]} trimmed {fraction:.0%} of {mint} (${usd:,.0f}); "
                                    f"the same trim of {how} is worth ${worth:.2f}, not worth the fees")
                                continue
                            log(f"COPY {wallet[:8]} trimmed {fraction:.0%} of {mint} (${usd:,.0f}); trimming {how} the same")
                            self.scale_out(pos, sol_price, frac=fraction, reason="copy_trim")
                        except Exception as exc:
                            log(f"WARN copy sell {mint}: {describe_error(exc)}")
                    continue
                minimum = cfg.copy_wallet_min_usd.get(wallet, cfg.copy_min_buy_usd)
                if usd < minimum:
                    log(f"COPY {wallet[:8]} bought {mint} for ${usd:,.0f} < ${minimum:,.0f} minimum; ignored")
                    continue
                if any(p.get("mint") == mint for p in self.state["positions"]) or any(p.get("mint") == mint for p in self.pending):
                    log(f"COPY {wallet[:8]} bought {mint} (${usd:,.0f}); already held or pending")
                    continue
                if not self.rotate_for_copy(mint, sol_price):
                    continue
                kind = "adding to a coin it holds" if swap.get("held_before") else "first buy of this coin"
                log(f"COPY {wallet[:8]} bought {mint} for ${usd:,.0f} ({kind}); mirroring")
                self.enter_with_retry({"mint": mint, "graduated_ts": int(block_time or now_ts()), "enter_at": now_ts(),
                                       "copy": wallet, "copy_buy_usd": round(usd),
                                       "copy_size": cfg.copy_wallet_size.get(wallet, 1.0)}, sol_price)

    def rotate_for_copy(self, mint: str, sol_price: float) -> bool:
        """Make room for a copied buy when every slot is taken: sell the oldest position we chose
        ourselves (adopted leftovers hold no slot) and let the new coin take its place. Returns
        False when the buy should be skipped instead (rotation off, daily loss limit reached, or
        the sale failed)."""
        cfg = self.cfg
        held = [p for p in self.state["positions"] if not p.get("adopted") or cfg.copy_only]
        if len(held) < cfg.max_concurrent:
            return True
        if not cfg.copy_rotate:
            log(f"COPY {mint}: all {cfg.max_concurrent} slots full and COPY_ROTATE=0; skipped")
            return False
        if float(self.state["daily"]["realized_pnl_usd"]) <= -cfg.daily_loss_limit_usd:
            log(f"COPY {mint}: daily loss limit reached; not rotating")
            return False
        # Adopted positions were bought before this process started, so they are the oldest.
        oldest = min(held, key=lambda p: (0 if p.get("adopted") else 1, float(p.get("opened_ts") or 0)))
        age_minutes = (now_ts() - float(oldest.get("opened_ts") or 0)) / 60
        if not oldest.get("adopted") and age_minutes < cfg.copy_rotate_min_age_minutes:
            log(f"COPY {mint}: slots full and the oldest position ({oldest['mint'][:8]}) is only {age_minutes:.0f}m old "
                f"(< {cfg.copy_rotate_min_age_minutes:.0f}m); skipped instead of rotating")
            return False
        log(f"ROTATE selling oldest {oldest['mint']} (opened {oldest.get('opened_at', '?')}) to make room for {mint}")
        try:
            self.close_position(oldest, "rotate", sol_price)
        except Exception as exc:
            log(f"WARN rotate {oldest['mint']}: {describe_error(exc)}; {mint} skipped")
            return False
        return not any(p.get("mint") == oldest["mint"] for p in self.state["positions"])

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
        record_skip(mint, reason, getattr(self, "last_entry_meta", None))
        log(f"SKIP {mint}: {reason}")

    def entry_metadata(
        self, mint: str, graduated_ts: float, size_usd: float, tokens: int
    ) -> tuple[float | None, float | None, float | None, str | None, dict[str, Any]]:
        """Reject at each metadata stage before spending on the next one.

        Returns (market cap, curve age, top holder pct, top holder wallet, bundle snapshot) for
        existing callers; everything else learned on the way (curve transaction count, creator
        profile and holding, early seller, BOOST window) lands in self.last_entry_meta so both
        the position record and the skip row carry it. Only candidates surviving every cheaper
        stage reach the mandatory bundle snapshot.
        """
        cfg = self.cfg
        meta = self.last_entry_meta
        market_cap = curve_age = top_holder_pct = None
        top_holder = None
        supply_ui = decimals = supply_raw = None
        curve_tx_count = creator_prior_launches = creator_hold_pct = early_sell_pct = None
        creator = early_seller = None
        not_analyzed = {"complete": False, "error": "earlier entry guard rejected; bundle not analyzed"}

        def rejected(bundle: dict[str, Any] | None = None) -> str | None:
            return entry_guard_reason(
                cfg, graduated_ts, now_ts(), None, market_cap, curve_age, top_holder_pct, bundle,
                curve_tx_count=curve_tx_count, creator_prior_launches=creator_prior_launches,
                creator_hold_pct=creator_hold_pct, early_sell_pct=early_sell_pct, early_seller=early_seller,
            )

        def result(bundle: dict[str, Any]):
            meta.update({
                "market_cap_usd": market_cap, "curve_age_seconds": curve_age,
                "top_holder_pct": top_holder_pct, "top_holder": top_holder,
                "curve_tx_count": curve_tx_count, "creator": creator,
                "creator_prior_launches": creator_prior_launches, "creator_hold_pct": creator_hold_pct,
                "early_sell_pct": early_sell_pct, "early_seller": early_seller,
                "bundle_confidence": bundle.get("bundle_confidence"),
                "history_total": bundle.get("history_total"), "history_decoded": bundle.get("history_decoded"),
            })
            for key in ("bundle_slot_pct", "cluster_pct", "dev_cluster_pct", "top10_wallet_pct",
                        "early_buy_pct", "funder_coverage_pct"):
                meta[key] = bundle.get(key)
            return market_cap, curve_age, top_holder_pct, top_holder, bundle

        if cfg.max_entry_market_cap_usd > 0 or cfg.min_entry_market_cap_usd > 0 or cfg.max_top_holder_pct > 0 \
                or cfg.max_creator_hold_pct > 0 or cfg.max_early_sell_pct > 0:
            try:
                supply_ui, decimals, supply_raw = self.rpc.token_supply_details(mint)
                market_cap = entry_market_cap_usd(
                    size_usd, tokens, supply_ui, decimals, supply_raw=supply_raw
                )
                tokens_ui = Decimal(tokens).scaleb(-decimals)
                log(f"VALUATION mint={mint} basis=buy_quote_raw_supply size_usd={size_usd:.8g} "
                    f"out_amount_raw={tokens} out_amount_ui={tokens_ui} supply_raw={supply_raw} "
                    f"supply_ui={supply_ui:.16g} decimals={decimals} value_usd={market_cap}")
            except Exception as exc:
                log(f"WARN {mint}: market cap check unavailable ({describe_error(exc)})")
        if rejected():
            return result(not_analyzed)
        created = None
        if cfg.min_curve_age_seconds > 0 or cfg.min_curve_transactions > 0 or cfg.max_creator_prior_launches > 0 \
                or cfg.max_creator_hold_pct > 0:
            try:
                created = self.rpc.mint_first_seen(mint, graduated_ts - cfg.min_curve_age_seconds)
                if created is not None:
                    curve_age = max(0.0, graduated_ts - created)
            except Exception as exc:
                log(f"WARN {mint}: curve age check unavailable ({describe_error(exc)})")
        if rejected():
            return result(not_analyzed)
        if cfg.min_curve_transactions > 0 and created is not None:
            try:
                curve_tx_count = int(self.rpc.curve_transaction_count(mint, created, graduated_ts))
                log(f"CURVE mint={mint} successful_transactions={curve_tx_count}")
            except Exception as exc:
                curve_tx_count = None
                log(f"WARN {mint}: curve activity check unavailable ({describe_error(exc)})")
        if rejected():
            return result(not_analyzed)
        if cfg.max_top_holder_pct > 0 and supply_ui:
            try:
                exclude = {self.wallet.pubkey} if self.wallet else set()
                holder = self.rpc.top_wallet_holder(mint, exclude)
                if holder:
                    top_holder, amount = holder
                    # Raw/raw is exact and cannot be skewed by token decimals.
                    top_holder_pct = amount / supply_raw * 100
            except Exception as exc:
                log(f"WARN {mint}: holder concentration check unavailable ({describe_error(exc)})")
        if rejected():
            return result(not_analyzed)
        if cfg.max_creator_prior_launches > 0 or cfg.max_creator_hold_pct > 0:
            try:
                creator = self.rpc.mint_creator(mint)
                if isinstance(creator, str) and creator:
                    if cfg.max_creator_hold_pct > 0 and supply_raw:
                        held = int(self.rpc.token_balance(creator, mint))
                        creator_hold_pct = held / supply_raw * 100
                    if cfg.max_creator_prior_launches > 0 and created is not None:
                        profile = self.rpc.creator_profile(creator, mint, created)
                        creator_prior_launches = int(profile["prior_launches"])
                        log(f"CREATOR mint={mint} wallet={creator} prior_launches={creator_prior_launches} "
                            f"sampled={profile.get('sampled_signatures')} holds={creator_hold_pct if creator_hold_pct is None else round(creator_hold_pct, 2)}%")
                else:
                    creator = None
            except Exception as exc:
                creator_prior_launches = creator_hold_pct = None
                log(f"WARN {mint}: creator check unavailable ({describe_error(exc)})")
        if rejected():
            return result(not_analyzed)
        if cfg.max_early_sell_pct > 0 and supply_raw:
            try:
                exclude = {self.wallet.pubkey} if self.wallet else set()
                seller = self.rpc.largest_seller_since(mint, graduated_ts, supply_raw, exclude)
                if seller:
                    early_seller, early_sell_pct = seller[0], float(seller[1])
                    log(f"EARLY-SELL mint={mint} wallet={early_seller} sold={early_sell_pct:.2f}% of supply since migration")
            except Exception as exc:
                early_sell_pct = early_seller = None
                log(f"WARN {mint}: early-dump check unavailable ({describe_error(exc)})")
        if rejected():
            return result(not_analyzed)
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
        return result(bundle)

    def try_enter(self, item: dict[str, Any], sol_price: float) -> None:
        mint = item["mint"]
        graduated_ts = float(item["graduated_ts"])
        since_graduation = now_ts() - graduated_ts
        self.last_entry_meta = {
            "seconds_after_graduation": round(since_graduation),
            "in_boost_window": since_graduation <= self.cfg.boost_window_seconds,
        }
        daily_pnl = float(self.state["daily"]["realized_pnl_usd"])
        # Adopted bags are money already in the market, not a choice we are making now, so they
        # do not take an entry slot: three $2 leftovers must not block every new graduation.
        # In copy-only mode they do count: a redeploy must not let the book grow past the slots.
        open_slots = sum(1 for p in self.state["positions"] if not p.get("adopted") or self.cfg.copy_only)
        equity = self.equity_usd(sol_price)
        size_usd = position_size_usd(self.cfg, equity, open_slots, daily_pnl)
        if size_usd <= 0:
            self.skip(mint, f"sizing guards (open={open_slots}, daily_pnl={daily_pnl:.2f})")
            return
        scale = float(item.get("copy_size") or 1.0)
        if scale != 1.0:
            # A wallet copied at a reduced size; never below the minimum position, where fees win.
            size_usd = max(self.cfg.min_position_usd, round(size_usd * scale, 2))
        deployed = self.deployed_usd()
        if deployed + size_usd > equity * self.cfg.max_deployed_fraction:
            self.skip(mint, f"deployment cap: ${deployed:,.2f} in positions + ${size_usd:,.2f} > "
                            f"{self.cfg.max_deployed_fraction:.0%} of ${equity:,.2f}")
            return
        spendable = self.spendable_usd(sol_price)
        if size_usd > spendable:
            size_usd = round(spendable, 2)
            if size_usd < self.cfg.min_position_usd:
                self.skip(mint, f"only ${spendable:,.2f} of free SOL left (< ${self.cfg.min_position_usd:,.2f} minimum)")
                return
        lamports = int(size_usd / sol_price * LAMPORTS)
        quote = self.jup.quote(WSOL, mint, lamports)
        tokens = int(quote["outAmount"])
        if tokens <= 0:
            raise RuntimeError("zero-token quote")
        impact = quote_price_impact_pct(quote)
        self.last_entry_meta["price_impact_pct"] = impact
        runner = bool(item.get("runner"))
        copied = item.get("copy") or ""
        self.last_entry_meta["runner"] = runner
        # Reject an already-invalid quote before expensive holder/history/funder queries.
        early_guard = entry_guard_reason(self.cfg, item['graduated_ts'], now_ts(), impact, runner=runner, copy_trade=bool(copied))
        if early_guard:
            self.skip(mint, early_guard)
            return
        if copied and self.cfg.copy_fast:
            # Mirroring a wallet is a race: the slow holder/bundle analysis is skipped and the
            # executable checks below (round trip, impact) are the safety net.
            market_cap, curve_age, top_holder_pct, top_holder, bundle = None, None, None, None, None
        else:
            market_cap, curve_age, top_holder_pct, top_holder, bundle = self.entry_metadata(
                mint, item["graduated_ts"], size_usd, tokens
            )
        meta = self.last_entry_meta
        extra = dict(
            curve_tx_count=meta.get("curve_tx_count"),
            creator_prior_launches=meta.get("creator_prior_launches"),
            creator_hold_pct=meta.get("creator_hold_pct"),
            early_sell_pct=meta.get("early_sell_pct"),
            early_seller=meta.get("early_seller"),
            runner=runner,
            copy_trade=bool(copied),
        )
        guard = entry_guard_reason(
            self.cfg, item["graduated_ts"], now_ts(), impact, market_cap, curve_age, top_holder_pct, **extra
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
            **extra,
        )
        bundle = bundle if bundle is not None else {"skipped": True}
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
        log(f"BUNDLE {mint}: {summary or bundle.get('error', 'skipped (copy fast entry)' if bundle.get('skipped') else 'no metrics')}")
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
        self.last_entry_meta["round_trip_pct"] = round_trip_pct
        if self.cfg.min_entry_round_trip_pct > 0 and round_trip_pct < self.cfg.min_entry_round_trip_pct:
            self.skip(
                mint,
                f"round-trip liquidity returns {round_trip_pct:.1f}% < "
                f"{self.cfg.min_entry_round_trip_pct:.1f}% of proposed buy",
            )
            return
        buy_sig = ""
        if self.cfg.mode == "live":
            # The wallet may already hold this mint (a moon bag from an earlier exit), so the
            # position's token count is what this buy delivered, never the wallet's balance.
            try:
                held_before = self.rpc.token_balance(self.wallet.pubkey, mint)
            except Exception:
                held_before = 0
            try:
                buy_sig = self.execute_swap(quote)
            except Exception as exc:
                # A swap can still land after our confirmation timeout. Check for
                # the tokens before giving up, or they become an untracked bag
                # sitting in the wallet that nothing will ever sell.
                time.sleep(15)
                landed = self.rpc.token_balance(self.wallet.pubkey, mint)
                if landed <= held_before:
                    raise
                log(f"WARN {mint}: entry reported failure ({exc}) but {landed - held_before} tokens landed; adopting position")
                buy_sig = "unconfirmed"
                tokens = landed - held_before
            else:
                tokens = self.tokens_received(buy_sig, mint) or tokens
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
                "graduated_ts": graduated_ts,
                "entry_seconds_after_graduation": round(now_ts() - graduated_ts),
                "entry_in_boost_window": (now_ts() - graduated_ts) <= self.cfg.boost_window_seconds,
                "entry_curve_tx_count": meta.get("curve_tx_count"),
                "entry_early_sell_pct": round(meta["early_sell_pct"], 2) if meta.get("early_sell_pct") is not None else None,
                "entry_creator": meta.get("creator"),
                "entry_creator_prior_launches": meta.get("creator_prior_launches"),
                "entry_creator_hold_pct": round(meta["creator_hold_pct"], 2) if meta.get("creator_hold_pct") is not None else None,
                "entry_history_total": bundle.get("history_total"),
                "entry_history_decoded": bundle.get("history_decoded"),
                "peak_usd": size_usd,
                "runner": runner,
                "runner_gain_pct": item.get("runner_gain_pct"),
                "copy": copied or None,
                "copy_buy_usd": item.get("copy_buy_usd"),
                "entry_tokens": tokens,
                "entry_basis_usd": size_usd,
                "ladder": [dict(r, done=False) for r in self.cfg.copy_ladder] if copied and self.cfg.copy_ladder else None,
            }
        )
        save_state(self.state)
        log(f"ENTER {mint} ${size_usd:.2f} ({'live ' + buy_sig[:16] + '…' if buy_sig else 'paper fill'})")

    def tokens_received(self, signature: str, mint: str) -> int:
        """Tokens of `mint` our wallet received in the confirmed swap `signature` (its balance
        change in the transaction), retried briefly while the RPC catches up. 0 if unknown."""
        for attempt in range(6):
            try:
                tx = self.rpc.transaction(signature)
            except Exception:
                tx = None
            swap = wallet_swap_from_transaction(tx or {}, str(self.wallet.pubkey)) if tx else None
            if swap and swap["side"] == "buy" and swap["mint"] == mint:
                return int(swap["tokens"])
            time.sleep(2.0)
        log(f"WARN {mint}: could not read the buy's token delta from {signature[:12]}…; using the quoted amount")
        return 0

    def sellable(self, mint: str, tracked: int) -> int:
        """How many of `tracked` tokens we can actually sell: never more than the wallet holds,
        and never the wallet's whole balance when a moon bag of the same mint sits beside the
        position."""
        if self.cfg.mode != "live":
            return int(tracked)
        held = self.rpc.token_balance(self.wallet.pubkey, mint)
        return min(int(tracked), int(held)) if held > 0 else 0

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
            amount = self.sellable(mint, int(pos["tokens"]))
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
        closed = adopted = swept = 0
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
                if 0 < value < self.cfg.dust_sweep_below_usd and self.cfg.close_empty_accounts:
                    try:
                        self.close_token_account(acct["pubkey"], acct["program"], mint, burn_amount=acct["amount"])
                        swept += 1
                        log(f"SWEPT dust {mint} worth ${value:.2f}; burned and account closed (rent reclaimed)")
                        time.sleep(1.0)
                    except Exception as exc:
                        log(f"WARN could not sweep dust {mint}: {describe_error(exc)}")
                continue
            if value < self.cfg.adopt_as_bag_below_usd:
                self.state.setdefault("moon_bags", []).append(
                    {"mint": mint, "tokens": acct["amount"], "cost_usd": round(value, 2), "kept_usd": round(value, 2),
                     "peak_usd": round(value, 2), "created_at": utc_iso(), "from_exit": "adopted"}
                )
                adopted += 1
                log(f"ADOPTED leftover {mint} worth ${value:.2f} as a moon bag (< ${self.cfg.adopt_as_bag_below_usd:.2f}); "
                    f"it takes no slot and sells at {self.cfg.moon_bag_target_x:.0f}x")
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
        if closed or adopted or swept:
            save_state(self.state)
        log(f"wallet reconciled: adopted {adopted} position(s), closed {closed} empty token account(s), swept {swept} dust holding(s)"
            + (f" (~{(closed + swept) * TOKEN_ACCOUNT_RENT_SOL:.4f} SOL rent)" if closed or swept else ""))

    def _record_close(self, pos: dict[str, Any], reason: str, exit_usd: float, sell_sig: str, sold_cost: float) -> None:
        net = exit_usd / sold_cost - 1.0 if sold_cost > 0 else 0.0
        exit_after = round(now_ts() - float(pos["graduated_ts"])) if pos.get("graduated_ts") else None
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
                "entry_seconds_after_graduation": pos.get("entry_seconds_after_graduation"),
                "entry_in_boost_window": pos.get("entry_in_boost_window"),
                "exit_seconds_after_graduation": exit_after,
                "exit_in_boost_window": (exit_after <= self.cfg.boost_window_seconds) if exit_after is not None else None,
                "entry_curve_tx_count": pos.get("entry_curve_tx_count"),
                "entry_early_sell_pct": pos.get("entry_early_sell_pct"),
                "entry_creator": pos.get("entry_creator"),
                "entry_creator_prior_launches": pos.get("entry_creator_prior_launches"),
                "entry_creator_hold_pct": pos.get("entry_creator_hold_pct"),
                "entry_history_total": pos.get("entry_history_total"),
                "entry_history_decoded": pos.get("entry_history_decoded"),
                "peak_gain_pct": round(peak_gain * 100, 1) if peak_gain is not None else None,
            }
        )
        peaked = f", peaked {peak_gain:+.1%}" if peak_gain is not None else ""
        log(f"EXIT {pos['mint']} {reason} ${exit_usd:.2f} ({net:+.1%}{peaked})")

    def position_estimates(self, positions: list[dict[str, Any]]) -> dict[str, float]:
        """Mark each position at the batched feed price. A position is only sell-quoted when this
        estimate says an exit or ladder rung is due, which keeps the Jupiter budget for the
        trades themselves. Empty when the feed is unavailable (the quote path then runs)."""
        if not positions or not self.cfg.price_first_valuation:
            return {}
        try:
            prices = self.token_prices([p["mint"] for p in positions] + [WSOL])
        except Exception as exc:
            log(f"WARN price feed: {describe_error(exc)}; falling back to sell quotes")
            return {}
        out: dict[str, float] = {}
        for pos in positions:
            hit = prices.get(pos["mint"])
            if hit:
                out[pos["mint"]] = int(pos["tokens"]) / 10 ** hit[1] * hit[0]
        return out

    def exit_due_at(self, pos: dict[str, Any], value_usd: float, panic: bool) -> bool:
        """Would a value of `value_usd` trigger any exit, rung or scale-out for this position?"""
        if panic:
            return True
        xcfg = self.exit_cfg(pos)
        margin = 1.0 + self.cfg.price_first_margin_pct / 100  # quote a little before the line
        if pos.get("ladder"):
            entry_tokens, entry_basis = int(pos.get("entry_tokens") or 0), float(pos.get("entry_basis_usd") or 0)
            pending = [r for r in pos["ladder"] if not r.get("done")]
            if pending and entry_tokens > 0 and entry_basis > 0 and int(pos["tokens"]) > 0:
                multiple = (value_usd / int(pos["tokens"])) / (entry_basis / entry_tokens)
                if multiple * margin >= pending[0]["x"]:
                    return True
        elif self.cfg.scale_out_at > 0 and not pos.get("scaled_out") and value_usd * margin >= pos["position_usd"] * (1.0 + self.cfg.scale_out_at):
            return True
        peak = max(float(pos.get("peak_usd", pos["position_usd"])), value_usd)
        return decide_exit(pos["position_usd"], value_usd / margin, pos["opened_ts"], now_ts(), xcfg, peak) is not None

    def manage_positions(self, sol_price: float, panic: bool) -> None:
        due = [pos for pos in self.state["positions"] if panic or now_ts() >= float(pos.get("next_check_ts") or 0)]
        estimates = self.position_estimates(due)
        for pos in due:
            if pos not in self.state["positions"]:
                continue
            pos["next_check_ts"] = now_ts() + self.cfg.position_check_seconds
            estimate = estimates.get(pos["mint"])
            if estimate is not None and not self.exit_due_at(pos, estimate, panic):
                # Nothing is close to firing: mark at the feed price and spend no quote.
                pos["peak_usd"] = max(float(pos.get("peak_usd", pos["position_usd"])), estimate)
                pos["last_value_usd"] = estimate
                pos["sell_failures"] = 0
                if now_ts() >= self._next_position_log.get(pos['mint'], 0):
                    self._next_position_log[pos['mint']] = now_ts() + 30.0
                    basis = float(pos['position_usd'])
                    xcfg = self.exit_cfg(pos)
                    log(f"POSITION {pos['mint']} price_value=${estimate:.2f} basis=${basis:.2f} "
                        f"tp_value=${basis * (1 + xcfg.take_profit):.2f} sl_value=${basis * (1 - xcfg.stop_loss):.2f} "
                        f"peak=${pos['peak_usd']:.2f} age={(now_ts() - pos['opened_ts']) / 60:.1f}m")
                continue
            try:
                quote = self.jup.quote(pos["mint"], WSOL, int(pos["tokens"]))
                current_usd = int(quote["outAmount"]) / LAMPORTS * sol_price
                pos["peak_usd"] = max(float(pos.get("peak_usd", pos["position_usd"])), current_usd)
                pos["last_value_usd"] = current_usd
                xcfg = self.exit_cfg(pos)
                if now_ts() >= self._next_position_log.get(pos['mint'], 0):
                    self._next_position_log[pos['mint']] = now_ts() + 30.0
                    basis = float(pos['position_usd'])
                    log(
                        f"POSITION {pos['mint']}{' (runner)' if pos.get('runner') else ''} sell_quote=${current_usd:.2f} basis=${basis:.2f} "
                        f"tp_value=${basis * (1 + xcfg.take_profit):.2f} "
                        f"sl_value=${basis * (1 - xcfg.stop_loss):.2f} "
                        f"peak_quote=${pos['peak_usd']:.2f} scaled_out={bool(pos.get('scaled_out'))} "
                        f"age={(now_ts() - pos['opened_ts']) / 60:.1f}m"
                    )
                if not panic and pos.get("ladder") and self.ladder_step(pos, current_usd, sol_price):
                    pos["next_check_ts"] = 0
                    continue  # re-quote what is left next cycle
                if (
                    not panic
                    and not pos.get("ladder")
                    and self.cfg.scale_out_at > 0
                    and not pos.get("scaled_out")
                    and current_usd >= pos["position_usd"] * (1.0 + self.cfg.scale_out_at)
                ):
                    self.scale_out(pos, sol_price)
                    pos["next_check_ts"] = 0
                    continue  # remainder is re-evaluated against a fresh quote next cycle
                reason = "panic" if panic else decide_exit(
                    pos["position_usd"], current_usd, pos["opened_ts"], now_ts(), xcfg, pos["peak_usd"]
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

    def scale_out(self, pos: dict[str, Any], sol_price: float, frac: float | None = None, reason: str = "scale_out") -> None:
        """Bank part of a winner at the first target. The remainder keeps the same TP/SL/trailing
        rules on its reduced cost basis, which leaves every threshold at the same token price.
        `frac` is the share of the current tokens to sell (default SCALE_OUT_FRACTION)."""
        mint = pos["mint"]
        frac = self.cfg.scale_out_fraction if frac is None else min(0.99, max(0.0, frac))
        amount = self.sellable(mint, int(pos["tokens"]))
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
                "exit_reason": reason,
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
        log(f"{reason.upper().replace('_', '-')} {mint}: sold {frac:.0%} for ${proceeds:.2f} ({net:+.1%}); remainder basis ${pos['position_usd']:.2f}")

    def ladder_step(self, pos: dict[str, Any], current_usd: float, sol_price: float) -> bool:
        """Copied positions phase out profit: at each rung's multiple of the entry price sell that
        rung's share of the entry tokens; the last rung closes the position (moon bag applies).
        Returns True when it acted, so the caller re-quotes next cycle."""
        rungs = pos.get("ladder") or []
        entry_tokens, entry_basis = int(pos.get("entry_tokens") or 0), float(pos.get("entry_basis_usd") or 0)
        tokens_now = int(pos["tokens"])
        if not rungs or entry_tokens <= 0 or entry_basis <= 0 or tokens_now <= 0:
            return False
        multiple = (current_usd / tokens_now) / (entry_basis / entry_tokens)
        pending = [r for r in rungs if not r.get("done")]
        if not pending or multiple < pending[0]["x"]:
            return False
        # Take every rung the price has crossed in one sale; a jump past two rungs sells both shares.
        crossed = [r for r in pending if multiple >= r["x"]]
        share_tokens = sum(entry_tokens * r["pct"] / 100 for r in crossed)
        last = crossed[-1] is rungs[-1]
        for r in crossed:
            r["done"] = True
        log(f"LADDER {pos['mint']}: {multiple:.2f}x entry, rung {crossed[-1]['x']:g}x reached")
        if last or share_tokens >= tokens_now * 0.98:
            self.close_position(pos, f"ladder_{crossed[-1]['x']:g}x", sol_price)
        else:
            self.scale_out(pos, sol_price, frac=share_tokens / tokens_now, reason=f"ladder_{crossed[-1]['x']:g}x")
        return True

    def sell_bag(self, bag: dict[str, Any], key: str, reason: str, sol_price: float, quote: dict[str, Any] | None = None) -> float:
        """Market-sell one bag from state[key] (moon bags or stuck positions), record the trade,
        reclaim its rent. Returns the USD proceeds."""
        if quote is None:
            amount = self.sellable(bag["mint"], int(bag["tokens"])) or int(bag["tokens"])
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
                if is_provider_unavailable(exc):
                    delay = self.note_provider_rate_limit(minimum_seconds=300)
                    log(f"WARN wallet reconciliation provider unavailable; retrying in {delay:.0f}s")
                else:
                    log(f"WARN deferred wallet reconciliation failed: {describe_error(exc)}")
                    self._reconcile_pending = False
        panic = PANIC_FLAG.exists()
        draining = STOP_FLAG.exists() or panic
        self.state["draining"] = draining
        self.log_heartbeat()
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
            if not self.cfg.copy_only:
                self.poll_graduations()
            due = [p for p in self.pending if now_ts() >= p["enter_at"]]
            if due:
                sol_price = sol_price or self.sol_price_usd()
                for item in due[: self.cfg.max_entries_per_cycle]:
                    self.pending.remove(item)
                    log(f"ENTRY checking mint={item['mint']} lateness={now_ts() - item['enter_at']:.0f}s")
                    self.enter_with_retry(item, sol_price)
            if self.state.get("watchlist") and not self.cfg.copy_only:
                try:
                    self.manage_watchlist(sol_price or self.sol_price_usd())
                except Exception as exc:
                    log(f"WARN runner watchlist: {describe_error(exc)}")
            if self.cfg.copy_wallets:
                try:
                    self.poll_copy_wallets(sol_price or self.sol_price_usd())
                except Exception as exc:
                    log(f"WARN copy trading: {describe_error(exc)}")
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
            f"runner={'only' if self.cfg.runner_only else ('on' if self.cfg.runner_enabled else 'off')}"
            f"(${self.cfg.runner_min_market_cap_usd:,.0f}-${self.cfg.runner_max_market_cap_usd:,.0f} "
            f"+{self.cfg.runner_min_gain_pct:.0f}%/{self.cfg.runner_momentum_minutes:.0f}m watch={self.cfg.runner_watch_hours:.0f}h "
            f"tp=+{self.cfg.runner_take_profit:.0%} sl=-{self.cfg.runner_stop_loss:.0%} trail={self.cfg.runner_trailing_stop:.0%} "
            f"time_stop={self.cfg.runner_time_stop_minutes:.0f}m) "
            f"copy={'only,' if self.cfg.copy_only else ''}{len(self.cfg.copy_wallets)}wallets(min${self.cfg.copy_min_buy_usd:,.0f} "
            f"{'fast' if self.cfg.copy_fast else 'full-checks'} {'follow-sells' if self.cfg.copy_follow_sells else 'own-exits'} "
            f"{'rotate' if self.cfg.copy_rotate else 'no-rotate'} "
            f"ladder={ladder_text(self.cfg.copy_ladder)} "
            f"tp=+{self.cfg.copy_take_profit:.0%} sl=-{self.cfg.copy_stop_loss:.0%} trail={self.cfg.copy_trailing_stop:.0%} "
            f"time_stop={self.cfg.copy_time_stop_minutes:.0f}m) "
            f"curve_age>={self.cfg.min_curve_age_seconds:.0f}s top_holder<={self.cfg.max_top_holder_pct:.0f}% "
            f"curve_txs>={self.cfg.min_curve_transactions} early_sell<={self.cfg.max_early_sell_pct:.0f}% "
            f"creator_launches<={self.cfg.max_creator_prior_launches} creator_hold<={self.cfg.max_creator_hold_pct:.0f}% "
            f"boost_window={self.cfg.boost_window_seconds:.0f}s "
            f"bundle_slot<={self.cfg.max_bundle_slot_pct:.0f}% cluster<={self.cfg.max_cluster_pct:.0f}% "
            f"ancestry<={self.cfg.max_ancestry_cluster_pct:.0f}% transfer<={self.cfg.max_transfer_cluster_pct:.0f}% "
            f"coordinated<={self.cfg.max_coordinated_buy_pct:.0f}% repeat<={self.cfg.max_repeat_cohort_pct:.0f}% "
            f"dev_cluster<={self.cfg.max_dev_cluster_pct:.0f}% top10<={self.cfg.max_top10_wallet_pct:.0f}% "
            f"early_buy<={self.cfg.max_early_buy_pct:.0f}% funder_coverage>={self.cfg.min_funder_coverage_pct:.0f}% "
            f"funder_lookup>={self.cfg.min_funder_lookup_pct:.0f}% "
            f"high_confidence>={self.cfg.high_confidence_funder_coverage_pct:.0f}% "
            f"holder_target={self.cfg.bundle_max_wallets} funders={self.cfg.bundle_funder_max_wallets} "
            f"funder_workers={self.cfg.bundle_lookup_workers} "
            f"providers={len(self.cfg.rpc_urls)} discovery={self.cfg.discovery_mode} "
            f"history={self.cfg.transaction_history_mode} das={'on' if self.cfg.rpc_das_enabled else 'off'} "
            f"bundle_mode={'log' if self.cfg.bundle_log_only else 'block'} "
            f"adopt>=${self.cfg.min_adopt_usd} "
            f"stuck_after={self.cfg.stuck_after_minutes:.0f}m "
            f"retries={self.cfg.entry_retries} daily_loss_limit=${self.cfg.daily_loss_limit_usd}")
        log("RPC endpoints: " + ", ".join(redact_endpoint(url) for url in self.cfg.rpc_urls))
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
        if self.cfg.copy_only:
            log(f"copy-only: graduation discovery and runner watchlist off; mirroring {len(self.cfg.copy_wallets)} wallet(s)")
        elif self.cfg.discovery_mode == "websocket":
            self.migration_stream = MigrationStream(self.cfg)
            self.migration_stream.start()
            log(f"graduation discovery: WebSocket stream + {self.cfg.discovery_catchup_seconds:.0f}s RPC catch-up")
        else:
            log(f"graduation discovery: RPC polling every {self.cfg.discovery_catchup_seconds:.0f}s")
        while True:
            try:
                self.run_cycle()
            except Exception as exc:
                log(f"ERROR loop: {describe_error(exc)}")
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
