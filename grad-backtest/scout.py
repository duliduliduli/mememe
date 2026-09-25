"""GMGN wallet scouting for the copy lane: discover candidate wallets, qualify them on their
history, watch their first buys in shadow with executable quotes, and promote only what
passes every gate, and only when the live switch is on.

Nothing here places an order. The lane's only effects on production are (1) the wallets it
reports as `live` being added to the followed set (SCOUT_LIVE=1 and a per-wallet loss budget
are both required) and (2) the elite-only entry gate (SCOUT_ELITE_ONLY, on by default: a
configured wallet's buys are mirrored only while it is qualified or live). Everything else
is bookkeeping in state["scout"], including the convergence signal (several independent
tracked wallets entering one coin inside a short window), which is only ever shadow traded.

What the GMGN OpenAPI can and cannot evidence (verified against the live API, Sep 2026):
  * wallet_stats: realized profit, buy/sell counts, tokens traded, win rate and average hold
    over 1d / 7d / 30d / all (there is no 90d window), plus the profile block: tags, twitter
    handle and follower counts, the first funding address, created-token count, and a
    `tag_rank` that has read 0 for every wallet looked at.
  * wallet_activity: every buy and sell with USD paid or received and gas, 20 a page with a
    cursor. Episodes, hold times, profit factor and the outlier test are built from it.
  * wallet_holdings: per token realized and unrealized profit and the inventory that arrived
    by transfer, which is how allocation-derived profit is told apart from copyable buys
    (one page of up to 100 tokens; the response is {"list": [...], "next": cursor}). This
    endpoint needs GMGN's signed auth (a GMGN_PRIVATE_KEY); with a read-only key it answers
    "missing signature", holdings are recorded as unavailable and the open-inventory gate
    stays `missing`.
  * smart-money and KOL trade feeds and a token's top traders: discovery sources. A token's
    top-trader position is a token-specific rank, never a global one.
  * There is no wallet leaderboard endpoint. The leaderboard gate therefore stays
    `insufficient evidence` until GMGN exposes ranks (tag_rank > 0) on enough daily
    snapshots; it is never inferred from popularity or feed appearances.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import random
import statistics
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POLICY_VERSION = "2026-09-25.1"
# The pseudo-wallet that owns convergence shadow positions (several tracked wallets entering
# one coin together); never a real address, never polled, never promoted.
CONVERGENCE = "convergence"
# Every threshold that decides a gate. `policy_key` fingerprints them, so changing one (in
# code or by variable) re-scores the stored candidates instead of leaving stale verdicts.
POLICY_FIELDS = (
    "fast_track", "fast_track_min_history_days", "fast_track_dense_episodes", "fast_track_min_episodes_30d",
    "fast_track_min_tokens_30d", "fast_track_recent_days", "min_history_days", "min_episodes_30d",
    "min_tokens_30d", "min_active_days_30d", "min_profit_factor", "max_best_token_share", "max_drawdown",
    "min_median_hold_minutes", "max_fast_exit_fraction", "leaderboard_top_fraction", "leaderboard_top_n",
    "leaderboard_min_snapshots", "leaderboard_span_days", "shadow_min_days", "shadow_min_trades",
    "shadow_min_tokens", "shadow_min_active_days", "shadow_min_profit_factor", "shadow_max_drawdown",
    "sniper_median_hold_seconds", "promote_min_paper_fills", "demote_idle_days",
)


def rate_limited(exc: BaseException) -> bool:
    """GMGN's rate-limit and IP-ban responses. Hitting one means the whole cycle must stop:
    more requests only extend the ban, and every later call in it would fail anyway."""
    text = str(exc).lower()
    return "rate limit" in text or "temporarily banned" in text or "too many requests" in text or "429" in text


def policy_key(cfg: "ScoutConfig") -> str:
    raw = "|".join(f"{f}={getattr(cfg, f, '')}" for f in POLICY_FIELDS)
    return f"{POLICY_VERSION}+{hashlib.sha1(raw.encode()).hexdigest()[:8]}"
STATES = ("discovered", "research", "shadow", "qualified", "live", "paused", "rejected")
# Tags that reject a wallet: wash and MEV flow is not copyable profit. Bundlers and insiders
# ("rat_trader") are not here: when they are consistently profitable, being in early with them is the point.
BAD_TAGS = ("wash_trader", "sandwich_bot", "mev_bot")
WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
DAY = 86400.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class ScoutConfig:
    """Every knob, read once. Thresholds are the configured starting policy from the handoff,
    not empirically optimized values; POLICY_VERSION is recorded on every evaluation."""

    def __init__(self) -> None:
        self.mode = os.getenv("SCOUT_MODE", "shadow").strip().lower()          # off | shadow
        self.live = os.getenv("SCOUT_LIVE", "1") == "1"                        # promotion switch
        # Off by default again since the copy funnel: requiring a qualified scout verdict for
        # every buy left the lane with zero wallets. Wallets now earn and lose the live set on
        # our own fills instead (promotion on paper results, demotion when red for us), and the
        # wallets that already failed are watch-only (COPY_WATCH_ONLY). SCOUT_ELITE_ONLY=1
        # still gates every buy on a fresh qualified/live verdict.
        self.elite_only = os.getenv("SCOUT_ELITE_ONLY", "0") == "1"
        # ---- the copy funnel: watchlist -> live set -> one position per coin ----
        # Watchlist: tracked wallets whose buys are recorded and paper traded but never bought
        # on their own. Polled on-chain in rotation, SCOUT_WATCH_POLL_WALLETS per poll, so a
        # few hundred wallets cost a steady trickle of RPC calls instead of a burst.
        self.watchlist_max = _env_int("WATCHLIST_MAX", 400)
        self.watch_poll_wallets = max(1, _env_int("SCOUT_WATCH_POLL_WALLETS", 8))
        # Live set: configured plus promoted wallets whose first buys are copied on their own.
        self.live_copy_max = _env_int("LIVE_COPY_MAX", 40)
        # Promotion on paper results: this many closed paper trades (baseline and 20 s stress
        # quotes), net positive at both, and a trade inside DEMOTE_IDLE_DAYS.
        self.promote_min_paper_fills = _env_int("PROMOTE_MIN_PAPER_FILLS", 5)
        # Demotion on our own results: after this many of our closed positions from a wallet
        # with a negative net, or no source trade in DEMOTE_IDLE_DAYS, it leaves the live set.
        self.demote_after_live_fills = _env_int("DEMOTE_AFTER_LIVE_FILLS", 10)
        self.demote_idle_days = _env_float("DEMOTE_IDLE_DAYS", 7)
        # Configured wallets are the owner's picks and are always copied; this optional list
        # (addresses or prefixes, empty by default) is the owner's own switch to make one of
        # them watch-only: its buys then feed confluence and paper data but are not copied.
        self.copy_watch_only = tuple(w.strip() for w in os.getenv("COPY_WATCH_ONLY", "").split(",") if w.strip())
        # Sniper farms: a median hold under this many seconds rejects the wallet outright (not
        # copyable at our latency, not worth paper trading). Hold time above it is a score and
        # the SCOUT_MIN_MEDIAN_HOLD_MINUTES history gate, never a veto.
        self.sniper_median_hold_seconds = _env_float("SCOUT_SNIPER_MEDIAN_HOLD_SEC", 60)
        # The hard filter on a buy: a source wallet whose measured median hold is under this
        # many seconds is never copied, whatever set it is in.
        self.sniper_max_hold_seconds = _env_float("SNIPER_MAX_HOLD_SEC", 10)
        # Runner overlap discovery: coins that ran (pool at most SCOUT_OVERLAP_MAX_AGE_HOURS old
        # with a market cap of at least SCOUT_OVERLAP_MIN_MCAP_USD, about 3x a pump.fun
        # graduation), their top traders, wallets ranked by how many of those runners they hit.
        self.overlap = os.getenv("SCOUT_OVERLAP", "1") == "1"
        self.overlap_max_age_hours = _env_float("SCOUT_OVERLAP_MAX_AGE_HOURS", 72)
        self.overlap_min_mcap_usd = _env_float("SCOUT_OVERLAP_MIN_MCAP_USD", 200_000)
        self.overlap_runners = _env_int("SCOUT_OVERLAP_RUNNERS", 25)
        self.overlap_top = _env_int("SCOUT_OVERLAP_TOP", 30)
        self.overlap_min_entry_delay_seconds = _env_float("SCOUT_OVERLAP_MIN_ENTRY_DELAY_SEC", 5)
        self.overlap_traders_per_runner = _env_int("SCOUT_OVERLAP_TRADERS_PER_RUNNER", 50)
        self.gecko_pause_seconds = _env_float("SCOUT_GECKO_PAUSE_SECONDS", 1.5)   # GeckoTerminal's keyless tier answers bursts with 429
        self.discovery_hours = _env_float("SCOUT_DISCOVERY_HOURS", 1)
        self.refresh_hours = _env_float("SCOUT_REFRESH_HOURS", 24)
        self.max_qualification_age_hours = _env_float("SCOUT_MAX_QUALIFICATION_AGE_HOURS", 48)
        self.max_live = _env_int("SCOUT_MAX_LIVE", 40)                         # scouted wallets live at once (LIVE_COPY_MAX caps the whole set)
        self.live_size = _env_float("SCOUT_LIVE_SIZE", 1.0)                    # of the usual size; 0.25 falls under the $5 minimum on a small account
        self.max_open_positions = _env_int("SCOUT_MAX_OPEN_POSITIONS", 0)      # open positions from scouted wallets at once (0: no cap beyond MAX_CONCURRENT_POSITIONS)
        self.live_loss_budget_usd = _env_float("SCOUT_LIVE_LOSS_BUDGET_USD", 25)  # 0: promotion refused
        # Fast track: qualify on fetched history alone, waiving the gates GMGN cannot evidence
        # (leaderboard, holdings) and the 14-day shadow sample. Shadow still runs as the tripwire.
        self.fast_track = os.getenv("SCOUT_FAST_TRACK", "1") == "1"
        self.fast_track_min_history_days = _env_float("SCOUT_FAST_TRACK_MIN_HISTORY_DAYS", 15)
        # A wallet trading hundreds of times a day fills the activity page cap in days, not
        # weeks, so a short window is not thin evidence: this many closed episodes inside it
        # count instead of the calendar requirement.
        self.fast_track_dense_episodes = _env_int("SCOUT_FAST_TRACK_DENSE_EPISODES", 30)
        self.fast_track_min_episodes_30d = _env_int("SCOUT_FAST_TRACK_MIN_EPISODES_30D", 10)
        self.fast_track_min_tokens_30d = _env_int("SCOUT_FAST_TRACK_MIN_TOKENS_30D", 6)
        # Fast track asks only that the wallet still trades: some activity in this many days.
        # Consistency is carried by the 7d/30d/all realized-profit, profit-factor and outlier gates.
        self.fast_track_recent_days = _env_float("SCOUT_FAST_TRACK_RECENT_DAYS", 7)
        self.tripwire_trades = _env_int("SCOUT_TRIPWIRE_TRADES", 10)              # live wallet paused when shadow net < 0 after this many
        self.max_shadow = _env_int("SCOUT_MAX_SHADOW", 25)                     # wallets polled on-chain
        self.max_candidates = _env_int("SCOUT_MAX_CANDIDATES", _env_int("WATCHLIST_MAX", 400))
        self.poll_seconds = _env_float("SCOUT_POLL_SECONDS", 5)
        self.decode_budget = _env_int("SCOUT_DECODE_BUDGET", 20)               # transactions per poll, all shadow wallets
        self.quote_budget = _env_int("SCOUT_QUOTE_BUDGET", 6)                  # Jupiter quotes per poll
        self.stress_seconds = _env_float("SCOUT_STRESS_SECONDS", 20)
        self.fee_usd = _env_float("SCOUT_FEE_USD", 0.05)                       # network + priority fee per swap; quotes carry route fees and impact
        self.enrich_per_cycle = _env_int("SCOUT_ENRICH_PER_CYCLE", 30)
        # Quick screen before the full history pull: one 30d wallet_stats call (3 units instead
        # of about 90). A new wallet with less realized profit than this over 30 days, or an
        # average hold under SCOUT_SNIPER_MEDIAN_HOLD_SEC, is rejected without the deep dive.
        self.screen_per_cycle = _env_int("SCOUT_SCREEN_PER_CYCLE", 80)
        self.screen_min_profit_usd = _env_float("SCOUT_SCREEN_MIN_PROFIT_USD", 500)
        # A rejected wallet leaves the tracked set for newer finds and is not looked at again
        # for this many days.
        self.reject_memory_days = _env_float("SCOUT_REJECT_MEMORY_DAYS", 7)
        self.activity_pages = _env_int("SCOUT_ACTIVITY_PAGES", 25)             # 20 events a page
        self.token_sample = _env_int("SCOUT_TOKEN_SAMPLE", 12)
        self.gmgn_units_per_cycle = _env_int("SCOUT_GMGN_UNITS_PER_CYCLE", 3600)
        self.pause_seconds = _env_float("SCOUT_PAUSE_SECONDS", 2.0)          # between GMGN calls; the free tier bans an IP that sustains too much
        # GMGN bans last at most 5 minutes; the pause is a margin over that, not a penalty.
        self.rate_limit_cooldown_hours = _env_float("SCOUT_RATE_LIMIT_COOLDOWN_HOURS", 0.25)
        self.requalify_hours = _env_float("SCOUT_REQUALIFY_HOURS", 24)
        # Historical qualification (30d window unless named otherwise)
        self.min_history_days = _env_float("SCOUT_MIN_HISTORY_DAYS", 60)
        self.min_episodes_30d = _env_int("SCOUT_MIN_EPISODES_30D", 30)
        self.min_tokens_30d = _env_int("SCOUT_MIN_TOKENS_30D", 20)
        self.min_active_days_30d = _env_int("SCOUT_MIN_ACTIVE_DAYS_30D", 10)
        self.min_profit_factor = _env_float("SCOUT_MIN_PROFIT_FACTOR", 1.5)
        # Memecoin profit is lumpy: one coin carrying most of a month is normal for a real
        # winner. The outlier gate (still net positive without the best coin) stays.
        self.max_best_token_share = _env_float("SCOUT_MAX_BEST_TOKEN_SHARE", 0.6)
        # A red last week on a green month is not disqualifying unless this is 1.
        self.require_pnl_7d = os.getenv("SCOUT_REQUIRE_PNL_7D", "0") == "1"
        self.max_drawdown = _env_float("SCOUT_MAX_DRAWDOWN", 0.25)
        self.min_median_hold_minutes = _env_float("SCOUT_MIN_MEDIAN_HOLD_MINUTES", 2)
        self.max_fast_exit_fraction = _env_float("SCOUT_MAX_FAST_EXIT_FRACTION", 0.25)
        # Leaderboard gate
        self.leaderboard_top_fraction = _env_float("SCOUT_LEADERBOARD_TOP_FRACTION", 0.05)
        self.leaderboard_top_n = _env_int("SCOUT_LEADERBOARD_TOP_N", 100)
        self.leaderboard_min_snapshots = _env_int("SCOUT_LEADERBOARD_MIN_SNAPSHOTS", 3)
        self.leaderboard_span_days = _env_float("SCOUT_LEADERBOARD_SPAN_DAYS", 7)
        # Shadow evaluation
        self.shadow_min_days = _env_float("SCOUT_SHADOW_MIN_DAYS", 14)
        self.shadow_min_trades = _env_int("SCOUT_SHADOW_MIN_TRADES", 30)
        self.shadow_min_tokens = _env_int("SCOUT_SHADOW_MIN_TOKENS", 15)
        self.shadow_min_active_days = _env_int("SCOUT_SHADOW_MIN_ACTIVE_DAYS", 7)
        self.shadow_min_profit_factor = _env_float("SCOUT_SHADOW_MIN_PROFIT_FACTOR", 1.3)
        self.shadow_max_drawdown = _env_float("SCOUT_SHADOW_MAX_DRAWDOWN", 0.15)
        # Convergence: several tracked wallets, from different relationship clusters, buying
        # the same coin inside a short window is treated as one signal of its own and shadow
        # traded like a wallet's first buy. Reported separately, so it can be judged against
        # single-wallet copying before it is ever given real money.
        self.convergence = os.getenv("SCOUT_CONVERGENCE", "1") == "1"
        self.convergence_min_wallets = max(2, _env_int("CONFLUENCE_K", _env_int("SCOUT_CONVERGENCE_MIN_WALLETS", 2)))
        self.convergence_window_seconds = (_env_float("CONFLUENCE_WINDOW_SEC", 180) if os.getenv("SCOUT_CONVERGENCE_WINDOW_MINUTES") is None
                                           else _env_float("SCOUT_CONVERGENCE_WINDOW_MINUTES", 3) * 60)
        # A smaller buy still counts toward confluence (a wallet's $100 entry is evidence when
        # a second wallet confirms it); the lone-wallet copy keeps COPY_MIN_SOURCE_USD.
        self.confluence_min_source_usd = _env_float("CONFLUENCE_MIN_SOURCE_USD", 100)
        # How old the newest converging buy may be when the live buy is placed. Watchlist
        # wallets are polled in rotation, so this is looser than COPY_MAX_TX_AGE_SECONDS.
        self.confluence_max_age_seconds = _env_float("CONFLUENCE_MAX_AGE_SEC", 150)
        self.convergence_cooldown_hours = _env_float("SCOUT_CONVERGENCE_COOLDOWN_HOURS", 6)   # one event per coin per this long
        # Live convergence: a convergence event also buys for real, at its own reduced size,
        # with its own cap on open positions and its own loss budget. Once realized P&L of
        # these positions reaches -budget, live convergence stops (shadow keeps recording).
        # The shadow tripwire stops it too: after this many closed shadow convergence trades
        # with a negative baseline net. SCOUT_CONVERGENCE_LIVE=0 turns the real buys off.
        self.convergence_live = os.getenv("SCOUT_CONVERGENCE_LIVE", "1") == "1"
        self.convergence_live_size = _env_float("SCOUT_CONVERGENCE_LIVE_SIZE", 1.0)
        self.convergence_max_open = _env_int("SCOUT_CONVERGENCE_MAX_OPEN", 3)
        self.convergence_loss_budget_usd = _env_float("SCOUT_CONVERGENCE_LOSS_BUDGET_USD", 15)
        self.convergence_tripwire_trades = _env_int("SCOUT_CONVERGENCE_TRIPWIRE_TRADES", 10)

        self.gmgn_key_set = bool(os.getenv("GMGN_API_KEY", "").strip())

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and self.gmgn_key_set


# ---- pure analytics -----------------------------------------------------------------------
def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def episodes_from_activity(events: list[dict[str, Any]], dust_ratio: float = 0.02) -> dict[str, Any]:
    """Turn GMGN activity rows into closed and open position episodes per token. An episode
    opens with a buy from an empty (or dust) stack and closes when the stack is back to dust.
    A sell with no open episode is inventory whose cost we never saw (transfer, allocation,
    history before our window): it is counted, not scored."""
    by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        token = (e.get("token") or {}).get("address") if isinstance(e.get("token"), dict) else e.get("token_address")
        if not token or e.get("event_type") not in ("buy", "sell"):
            continue
        by_token[str(token)].append(e)
    episodes: list[dict[str, Any]] = []
    unmatched_sells = 0
    unmatched_usd = 0.0
    for token, rows in by_token.items():
        rows.sort(key=lambda r: int(r.get("timestamp") or 0))
        balance, peak = 0.0, 0.0
        ep: dict[str, Any] | None = None
        for r in rows:
            amount = _f(r.get("token_amount"))
            usd = _f(r.get("cost_usd"))
            gas = _f(r.get("gas_usd"))
            ts = int(r.get("timestamp") or 0)
            if r["event_type"] == "buy":
                if ep is None:
                    ep = {"token": token, "opened_ts": ts, "first_buy_usd": usd, "cost_usd": 0.0, "proceeds_usd": 0.0,
                          "fees_usd": 0.0, "adds": 0, "sells": 0, "first_material_sell_ts": None, "closed_ts": None, "closed": False}
                    balance, peak = 0.0, 0.0
                else:
                    ep["adds"] += 1
                ep["cost_usd"] += usd
                ep["fees_usd"] += gas
                balance += amount
                peak = max(peak, balance)
            else:
                if ep is None:
                    unmatched_sells += 1
                    unmatched_usd += usd
                    continue
                ep["proceeds_usd"] += usd
                ep["fees_usd"] += gas
                ep["sells"] += 1
                if ep["first_material_sell_ts"] is None and peak > 0 and amount >= 0.2 * peak:
                    ep["first_material_sell_ts"] = ts
                balance -= amount
                if balance <= peak * dust_ratio:
                    ep["closed_ts"], ep["closed"] = ts, True
                    ep["pnl_usd"] = ep["proceeds_usd"] - ep["cost_usd"] - ep["fees_usd"]
                    episodes.append(ep)
                    ep, balance, peak = None, 0.0, 0.0
        if ep is not None:
            ep["pnl_usd"] = None
            episodes.append(ep)
    return {"episodes": episodes, "unmatched_sells": unmatched_sells, "unmatched_sell_usd": round(unmatched_usd, 2)}


PROFIT_FACTOR_CAP = 99.0     # "no losses at all" is reported as this finite value, never infinity (JSON-safe)


def profit_factor(gross_profit: float, gross_loss: float) -> float | None:
    if gross_loss > 0:
        return round(min(PROFIT_FACTOR_CAP, gross_profit / gross_loss), 3)
    return PROFIT_FACTOR_CAP if gross_profit > 0 else None


def max_drawdown(series: list[float]) -> float:
    peak, worst = 0.0, 0.0
    for v in series:
        peak = max(peak, v)
        worst = max(worst, peak - v)
    return worst


def history_metrics(episodes: list[dict[str, Any]], now: float, window_days: int = 30, min_first_buy_usd: float = 300.0,
                    excluded_tokens: set[str] | None = None, capital_base_usd: float | None = None) -> dict[str, Any]:
    """Consistency and copyability numbers over the closed episodes inside the window. Tokens
    in `excluded_tokens` (transferred-in inventory, unknown cost basis) are left out of
    profitability and counted as excluded."""
    excluded_tokens = excluded_tokens or set()
    since = now - window_days * DAY
    closed = [e for e in episodes if e.get("closed") and e.get("closed_ts") and e["closed_ts"] >= since]
    excluded = [e for e in closed if e["token"] in excluded_tokens]
    scored = [e for e in closed if e["token"] not in excluded_tokens]
    gross_pos = sum(e["pnl_usd"] for e in scored if e["pnl_usd"] > 0)
    gross_neg = -sum(e["pnl_usd"] for e in scored if e["pnl_usd"] < 0)
    net = gross_pos - gross_neg
    by_token: dict[str, float] = defaultdict(float)
    for e in scored:
        by_token[e["token"]] += e["pnl_usd"]
    best_token, best_pnl = (max(by_token.items(), key=lambda kv: kv[1]) if by_token else (None, 0.0))
    qualifying = [e for e in scored if e["first_buy_usd"] >= min_first_buy_usd]
    holds = [((e["first_material_sell_ts"] or e["closed_ts"]) - e["opened_ts"]) / 60.0 for e in qualifying]
    fast = sum(1 for h in holds if h * 60 <= 60)
    profitable = [e for e in scored if e["pnl_usd"] > 0]
    ordered = sorted(scored, key=lambda e: e["closed_ts"])
    equity, series = 0.0, []
    for e in ordered:
        equity += e["pnl_usd"]
        series.append(equity)
    dd_usd = max_drawdown(series)
    base = capital_base_usd if capital_base_usd else sum(e["cost_usd"] for e in scored)
    active_days = {day_of(e["closed_ts"]) for e in scored} | {day_of(e["opened_ts"]) for e in scored}
    return {
        "window_days": window_days,
        "closed_episodes": len(scored),
        "excluded_episodes": len(excluded),
        "distinct_tokens": len({e["token"] for e in scored}),
        "active_days": len(active_days),
        "net_pnl_usd": round(net, 2),
        "gross_profit_usd": round(gross_pos, 2),
        "gross_loss_usd": round(gross_neg, 2),
        "profit_factor": profit_factor(gross_pos, gross_neg),
        "episode_win_rate": round(len(profitable) / len(scored), 4) if scored else None,
        "episode_win_count": len(profitable),
        "best_token": best_token,
        "best_token_pnl_usd": round(best_pnl, 2),
        "best_token_share": round(best_pnl / gross_pos, 4) if gross_pos > 0 and best_pnl > 0 else 0.0,
        "net_without_best_token_usd": round(net - max(best_pnl, 0.0), 2),
        "qualifying_first_buys": len(qualifying),
        "median_hold_minutes": round(statistics.median(holds), 1) if holds else None,
        "fast_exit_fraction": round(fast / len(holds), 4) if holds else None,
        "profit_needing_adds_share": round(sum(e["pnl_usd"] for e in profitable if e["adds"] > 0) / gross_pos, 4) if gross_pos > 0 else 0.0,
        "drawdown_usd": round(dd_usd, 2),
        "drawdown_fraction": round(dd_usd / base, 4) if base and base > 0 else None,
        "drawdown_basis": "realized episode equity, open positions not marked",
    }


def cluster_bootstrap_lower_bound(clusters: list[list[float]], iterations: int = 2000, alpha: float = 0.05,
                                  seed: int = 7) -> float | None:
    """95% one-sided lower bound for the mean trade return, resampling whole clusters (all the
    trades of one token, or of one day) so correlated trades do not pose as independent."""
    clusters = [c for c in clusters if c]
    if len(clusters) < 2 or sum(len(c) for c in clusters) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        picked = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        flat = [v for c in picked for v in c]
        means.append(sum(flat) / len(flat))
    means.sort()
    return means[max(0, min(len(means) - 1, int(alpha * len(means))))]


def shadow_metrics(trades: list[dict[str, Any]], now: float, first_signal_ts: float | None = None) -> dict[str, Any]:
    """Forward results of one wallet's shadow trades: baseline and 20s-stress net P&L, profit
    factor, drawdown against the capital the trades actually tied up, the outlier test and
    the clustered bootstrap bound on the mean baseline return."""
    closed = [t for t in trades if t.get("closed_ts")]
    if not closed:
        return {"trades": 0, "distinct_tokens": 0, "active_days": 0, "calendar_days": round((now - first_signal_ts) / DAY, 1) if first_signal_ts else 0}
    net_base = sum(t["pnl_base"] for t in closed)
    stress_known = [t for t in closed if t.get("pnl_stress") is not None]
    net_stress = sum(t["pnl_stress"] for t in stress_known)
    gross_pos = sum(t["pnl_base"] for t in closed if t["pnl_base"] > 0)
    gross_neg = -sum(t["pnl_base"] for t in closed if t["pnl_base"] < 0)
    by_token: dict[str, float] = defaultdict(float)
    for t in closed:
        by_token[t["mint"]] += t["pnl_base"]
    best_pnl = max(by_token.values()) if by_token else 0.0
    ordered = sorted(closed, key=lambda t: t["closed_ts"])
    equity, series = 0.0, []
    for t in ordered:
        equity += t["pnl_base"]
        series.append(equity)
    # Capital base: the most cost these trades had open at once.
    edges = sorted([(t["opened_ts"], t["cost_usd"]) for t in closed] + [(t["closed_ts"], -t["cost_usd"]) for t in closed])
    running, peak_capital = 0.0, 0.0
    for _, delta in edges:
        running += delta
        peak_capital = max(peak_capital, running)
    dd_usd = max_drawdown(series)
    by_token_ret: dict[str, list[float]] = defaultdict(list)
    by_day_ret: dict[str, list[float]] = defaultdict(list)
    for t in closed:
        r = t["pnl_base"] / t["cost_usd"] if t["cost_usd"] else 0.0
        by_token_ret[t["mint"]].append(r)
        by_day_ret[day_of(t["opened_ts"])].append(r)
    lb_token = cluster_bootstrap_lower_bound(list(by_token_ret.values()))
    lb_day = cluster_bootstrap_lower_bound(list(by_day_ret.values()))
    bounds = [b for b in (lb_token, lb_day) if b is not None]
    admitted = [t for t in closed if t.get("portfolio_admitted")]
    first = first_signal_ts or min(t["opened_ts"] for t in closed)
    return {
        "trades": len(closed),
        "distinct_tokens": len(by_token),
        "active_days": len({day_of(t["opened_ts"]) for t in closed}),
        "calendar_days": round((now - first) / DAY, 1),
        "net_base_usd": round(net_base, 2),
        "net_stress_usd": round(net_stress, 2) if stress_known else None,
        "stress_coverage": round(len(stress_known) / len(closed), 3),
        "mean_return_base": round(sum(t["pnl_base"] / t["cost_usd"] for t in closed if t["cost_usd"]) / len(closed), 4),
        "profit_factor": profit_factor(gross_pos, gross_neg),
        "win_rate": round(sum(1 for t in closed if t["pnl_base"] > 0) / len(closed), 4),
        "drawdown_usd": round(dd_usd, 2),
        "drawdown_fraction": round(dd_usd / peak_capital, 4) if peak_capital > 0 else None,
        "net_without_best_token_usd": round(net_base - max(best_pnl, 0.0), 2),
        "bootstrap_lower_bound_token_clusters": round(lb_token, 4) if lb_token is not None else None,
        "bootstrap_lower_bound_day_clusters": round(lb_day, 4) if lb_day is not None else None,
        "bootstrap_lower_bound": round(min(bounds), 4) if bounds else None,
        "missed": {"no_slot": sum(1 for t in trades if t.get("reason") == "missed:no_slot"),
                   "unpriceable": sum(1 for t in trades if t.get("reason") == "missed:unpriceable")},
        "portfolio_admitted_trades": len(admitted),
        "portfolio_admitted_net_usd": round(sum(t["pnl_base"] for t in admitted), 2),
    }


def leaderboard_status(snapshots: list[dict[str, Any]], cfg: ScoutConfig, now: float) -> dict[str, Any]:
    """A wallet is leaderboard-qualified only on global rank snapshots (never a token's
    top-trader list) that clear the bar on enough distinct days spanning the required period."""
    global_snaps = [s for s in snapshots if s.get("scope") == "global" and int(s.get("rank") or 0) > 0]
    good = []
    for s in global_snaps:
        pop = int(s.get("population") or 0)
        rank = int(s["rank"])
        ok = rank <= cfg.leaderboard_top_n if pop <= 0 else rank <= max(1, math.floor(pop * cfg.leaderboard_top_fraction))
        if ok:
            good.append(s)
    days = sorted({day_of(s["ts"]) for s in good})
    span = (max(s["ts"] for s in good) - min(s["ts"] for s in good)) / DAY if good else 0.0
    passed = len(days) >= cfg.leaderboard_min_snapshots and span >= cfg.leaderboard_span_days
    token_only = sum(1 for s in snapshots if s.get("scope") == "token")
    return {"status": "pass" if passed else ("fail" if global_snaps else "missing"),
            "qualifying_days": len(days), "span_days": round(span, 1), "global_snapshots": len(global_snaps),
            "token_list_snapshots": token_only,
            "detail": ("global rank evidence on %d day(s) spanning %.1f days" % (len(days), span)) if global_snaps
            else "no global leaderboard rank available from GMGN (tag_rank is 0); token top-trader lists do not count"}


def evaluate_candidate(cand: dict[str, Any], cfg: ScoutConfig, now: float, min_first_buy_usd: float) -> dict[str, Any]:
    """Every mandatory gate with pass / fail / missing and the reason. A score is computed only
    for the record; it never overrides a gate."""
    gates: dict[str, dict[str, Any]] = {}
    missing: list[str] = []

    def gate(name: str, status: str, detail: str) -> None:
        gates[name] = {"status": status, "detail": detail}
        if status == "missing":
            missing.append(name)

    stats = cand.get("stats") or {}
    s7, s30, sall = stats.get("7d") or {}, stats.get("30d") or {}, stats.get("all") or {}
    hist = cand.get("history") or {}
    m30 = hist.get("metrics_30d") or {}
    flags = list(cand.get("risk_flags") or [])
    tags = list(cand.get("tags") or [])
    screen = cand.get("screen") or {}
    if screen.get("ok") is False:
        gate("screen", "fail", screen.get("why") or "failed the quick screen")
    bad = [t for t in tags if t in BAD_TAGS]
    if bad:
        gate("tags", "fail", "GMGN tags " + ", ".join(bad))
    else:
        gate("tags", "pass", "no wash / bundler / MEV tags")
    for name, row in (("pnl_7d", s7), ("pnl_30d", s30), ("pnl_all", sall)):
        if not row:
            gate(name, "missing", "no wallet_stats row")
        else:
            realized = _f(row.get("realized_profit"))
            gate(name, "pass" if realized > 0 else "fail", f"realized {realized:+,.0f} USD")
    coverage_days = hist.get("coverage_days")
    if coverage_days is None:
        gate("history_days", "missing", "activity history not fetched")
    elif hist.get("truncated") and coverage_days < cfg.min_history_days:
        gate("history_days", "missing", f"only {coverage_days:.0f} days of activity fetched before the page cap; need {cfg.min_history_days:.0f}")
    else:
        gate("history_days", "pass" if coverage_days >= cfg.min_history_days else "fail", f"{coverage_days:.0f} days observed")
    dense = int((m30 or {}).get("closed_episodes") or 0)
    if coverage_days is None:
        gate("history_days_fast", "missing", "activity history not fetched")
    elif coverage_days >= cfg.fast_track_min_history_days:
        gate("history_days_fast", "pass", f"{coverage_days:.1f} days of activity fetched")
    elif hist.get("truncated") and dense >= cfg.fast_track_dense_episodes:
        # The page cap cut the window short, not the wallet's record: this many closed
        # episodes inside those days is denser evidence than a quiet wallet's fortnight.
        gate("history_days_fast", "pass",
             f"{coverage_days:.1f} days fetched before the page cap, holding {dense} closed episodes "
             f"(dense evidence counts from {cfg.fast_track_dense_episodes})")
    elif hist.get("truncated"):
        gate("history_days_fast", "missing",
             f"only {coverage_days:.1f} days fetched before the page cap, holding {dense} closed episodes; "
             f"need {cfg.fast_track_min_history_days:.0f} days or {cfg.fast_track_dense_episodes} episodes")
    else:
        gate("history_days_fast", "fail",
             f"{coverage_days:.1f} days of activity (fast track needs {cfg.fast_track_min_history_days:.0f})")
    min_eps = cfg.fast_track_min_episodes_30d if cfg.fast_track else cfg.min_episodes_30d
    min_tok = cfg.fast_track_min_tokens_30d if cfg.fast_track else cfg.min_tokens_30d
    if not m30:
        for name in ("episodes_30d", "tokens_30d", "active_days_30d", "profit_factor_30d", "outlier_30d", "best_token_share", "drawdown", "hold_time", "fast_exits"):
            gate(name, "missing", "no episode metrics")
    else:
        gate("episodes_30d", "pass" if m30["closed_episodes"] >= min_eps else "fail", f"{m30['closed_episodes']} closed episodes (need {min_eps})")
        gate("tokens_30d", "pass" if m30["distinct_tokens"] >= min_tok else "fail", f"{m30['distinct_tokens']} tokens (need {min_tok})")
        if cfg.fast_track:
            last_seen = max([float(e.get("closed_ts") or e.get("opened_ts") or 0) for e in hist.get("episodes") or []]
                            + [_f(row.get("last_timestamp")) / (1000.0 if _f(row.get("last_timestamp")) > 1e11 else 1.0)
                               for row in (s7, s30, sall) if row] + [0.0])
            days_ago = (now - last_seen) / DAY if last_seen > 0 else None
            recent = days_ago is not None and days_ago <= cfg.fast_track_recent_days
            gate("active_days_30d", "pass" if recent else "fail",
                 (f"last trade {days_ago:.1f} days ago" if days_ago is not None else "no trade timestamp")
                 + f" (needs one within {cfg.fast_track_recent_days:.0f})")
        else:
            gate("active_days_30d", "pass" if m30["active_days"] >= cfg.min_active_days_30d else "fail",
                 f"{m30['active_days']} active days (need {cfg.min_active_days_30d})")
        pf = m30.get("profit_factor")
        if pf is None:
            gate("profit_factor_30d", "missing", "no scored losses or profits")
        else:
            gate("profit_factor_30d", "pass" if pf >= cfg.min_profit_factor else "fail", f"profit factor {pf:.2f} (need {cfg.min_profit_factor})")
        gate("outlier_30d", "pass" if m30["net_without_best_token_usd"] > 0 else "fail", f"net without best token {m30['net_without_best_token_usd']:+,.0f} USD")
        gate("best_token_share", "pass" if m30["best_token_share"] <= cfg.max_best_token_share else "fail", f"best token {m30['best_token_share']:.0%} of gross profit (max {cfg.max_best_token_share:.0%})")
        dd = m30.get("drawdown_fraction")
        if dd is None:
            gate("drawdown", "missing", "no capital base for the drawdown")
        else:
            gate("drawdown", "pass" if dd <= cfg.max_drawdown else "fail", f"{dd:.0%} drawdown of realized episode equity (max {cfg.max_drawdown:.0%}; open positions not marked)")
        hold = m30.get("median_hold_minutes")
        if hold is None:
            gate("hold_time", "missing", "no qualifying first buys to time")
        else:
            gate("hold_time", "pass" if hold >= cfg.min_median_hold_minutes else "fail", f"median {hold:.1f} min to first material sell (need {cfg.min_median_hold_minutes:.0f})")
        fe = m30.get("fast_exit_fraction")
        if fe is None:
            gate("fast_exits", "missing", "no qualifying first buys")
        else:
            gate("fast_exits", "pass" if fe <= cfg.max_fast_exit_fraction else "fail", f"{fe:.0%} of first buys exited within 60s (max {cfg.max_fast_exit_fraction:.0%})")
    hold = (m30 or {}).get("median_hold_minutes")
    if hold is None:
        gate("sniper", "missing", "no qualifying first buys to time")
    else:
        sniper = hold * 60 < cfg.sniper_median_hold_seconds
        gate("sniper", "fail" if sniper else "pass",
             f"median hold {hold * 60:.0f}s " + ("< " if sniper else ">= ") + f"{cfg.sniper_median_hold_seconds:.0f}s (sniper farm below)")
    open_loss = _f((cand.get("holdings") or {}).get("open_loss_usd"))
    if cand.get("holdings") is None:
        gate("open_inventory", "missing", "holdings not fetched")
    else:
        realized30 = _f(s30.get("realized_profit"))
        concealed = realized30 > 0 and open_loss > realized30
        gate("open_inventory", "fail" if concealed else "pass", f"open unrealized losses {open_loss:,.0f} USD vs 30d realized {realized30:+,.0f}")
    lb = leaderboard_status(cand.get("rank_snapshots") or [], cfg, now)
    gate("leaderboard", lb["status"], lb["detail"])
    hard = [f for f in flags if f.get("severity") == "hard"]
    gate("risk_flags", "fail" if hard else "pass", "; ".join(f["flag"] for f in hard) if hard else f"{len(flags)} soft flag(s)")
    sh = cand.get("shadow") or {}
    n = sh.get("trades", 0)
    enough = (n >= cfg.shadow_min_trades and sh.get("distinct_tokens", 0) >= cfg.shadow_min_tokens
              and sh.get("active_days", 0) >= cfg.shadow_min_active_days and sh.get("calendar_days", 0) >= cfg.shadow_min_days)
    if not enough:
        gate("shadow_sample", "missing", f"{n} trades / {sh.get('distinct_tokens', 0)} tokens / {sh.get('active_days', 0)} active days / {sh.get('calendar_days', 0)} days; need "
             f"{cfg.shadow_min_trades} / {cfg.shadow_min_tokens} / {cfg.shadow_min_active_days} / {cfg.shadow_min_days:.0f}")
        for name in ("shadow_baseline", "shadow_stress", "shadow_profit_factor", "shadow_drawdown", "shadow_outlier", "shadow_bootstrap"):
            gate(name, "missing", "shadow sample too small")
    else:
        gate("shadow_sample", "pass", f"{n} trades over {sh['calendar_days']} days")
        gate("shadow_baseline", "pass" if sh["net_base_usd"] > 0 else "fail", f"baseline net {sh['net_base_usd']:+,.2f} USD")
        if sh.get("net_stress_usd") is None or sh.get("stress_coverage", 0) < 0.8:
            gate("shadow_stress", "missing", f"stress quotes cover {sh.get('stress_coverage', 0):.0%} of trades")
        else:
            gate("shadow_stress", "pass" if sh["net_stress_usd"] > 0 else "fail", f"20s-latency net {sh['net_stress_usd']:+,.2f} USD")
        pf = sh.get("profit_factor")
        gate("shadow_profit_factor", "missing" if pf is None else ("pass" if pf >= cfg.shadow_min_profit_factor else "fail"),
             f"profit factor {pf}" if pf is not None else "no losses and no profits")
        dd = sh.get("drawdown_fraction")
        gate("shadow_drawdown", "missing" if dd is None else ("pass" if dd <= cfg.shadow_max_drawdown else "fail"),
             f"{dd:.0%} of peak shadow capital" if dd is not None else "no capital base")
        gate("shadow_outlier", "pass" if sh["net_without_best_token_usd"] > 0 else "fail", f"net without best token {sh['net_without_best_token_usd']:+,.2f} USD")
        lbb = sh.get("bootstrap_lower_bound")
        gate("shadow_bootstrap", "missing" if lbb is None else ("pass" if lbb > 0 else "fail"),
             f"95% lower bound of mean return {lbb:+.4f} (token and day clusters)" if lbb is not None else "too few clusters")
    # Which gates decide. The full policy needs every gate; the fast track waives what GMGN
    # cannot evidence (leaderboard, holdings), the 60-day history and the shadow sample, and
    # judges on the fetched history alone. `history_days_fast` only counts on the fast track.
    if cfg.fast_track:
        waived = ["leaderboard", "open_inventory", "history_days"] + [k for k in gates if k.startswith("shadow_")]
    else:
        waived = ["history_days_fast"]
    if not cfg.require_pnl_7d:
        waived.append("pnl_7d")
    deciding = {k: g for k, g in gates.items() if k not in waived}
    failed = [k for k, g in deciding.items() if g["status"] == "fail"]
    missing_deciding = [k for k, g in deciding.items() if g["status"] == "missing"]
    qualified = not failed and not missing_deciding
    score = score_candidate(cand, m30, sh, lb)
    return {"policy_version": policy_key(cfg), "evaluated_at": now, "gates": gates, "failed": failed, "missing": missing_deciding,
            "waived": waived, "track": "fast" if cfg.fast_track else "full", "qualified": qualified, "score": score}


def score_candidate(cand: dict[str, Any], m30: dict[str, Any], sh: dict[str, Any], lb: dict[str, Any]) -> dict[str, Any]:
    """Documented 0-100 score for ranking wallets that passed every gate. Each component is
    normalized to 0-1 against a stated ceiling; missing exposure earns zero."""
    def clamp(x: float) -> float:
        return max(0.0, min(1.0, x))
    fwd = clamp((sh.get("mean_return_base") or 0.0) / 0.20) if sh.get("trades") else 0.0          # +20% mean return = full marks
    consistency = 0.0
    if m30:
        pf = m30.get("profit_factor") or 0.0
        consistency = clamp((min(pf, 3.0) - 1.0) / 2.0) * 0.6 + clamp((m30.get("episode_win_rate") or 0.0) / 0.6) * 0.4
    copyability = 0.0
    if m30 and m30.get("median_hold_minutes") is not None:
        copyability = clamp((m30["median_hold_minutes"] or 0.0) / 120.0) * 0.5 + clamp(1.0 - (m30.get("fast_exit_fraction") or 0.0) / 0.1) * 0.5
    rank = 1.0 if lb.get("status") == "pass" else 0.0
    exposure = cand.get("exposure") or {}
    expo = 0.0
    if exposure.get("known"):
        expo = clamp(math.log10(max(1.0, _f(exposure.get("followers")))) / 6.0) * 0.6 + (0.4 if exposure.get("verified") else 0.0)
    diversification = clamp((m30.get("distinct_tokens") or 0) / 40.0) if m30 else 0.0
    weights = {"forward_copy_performance": 35, "historical_consistency": 25, "execution_copyability": 20,
               "persistent_leaderboard_rank": 10, "verified_public_exposure": 5, "portfolio_diversification": 5}
    components = {"forward_copy_performance": fwd, "historical_consistency": consistency, "execution_copyability": copyability,
                  "persistent_leaderboard_rank": rank, "verified_public_exposure": expo, "portfolio_diversification": diversification}
    total = sum(weights[k] * components[k] for k in weights)
    return {"total": round(total, 1), "components": {k: round(v, 3) for k, v in components.items()}, "weights": weights}


def relationship_clusters(candidates: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group wallets that look commonly controlled: the same first funder (low confidence,
    exchanges fund many strangers) or three or more tokens bought within two minutes of each
    other (medium). Returns {address: {"cluster": id, "confidence": ..., "evidence": [...]}}."""
    addrs = list(candidates)
    parent = {a: a for a in addrs}
    evidence: dict[str, list[str]] = defaultdict(list)
    conf: dict[str, str] = {}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str, level: str, why: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
        evidence[a].append(why)
        evidence[b].append(why)
        for x in (a, b):
            if level == "medium" or conf.get(x) != "medium":
                conf[x] = level

    funders: dict[str, list[str]] = defaultdict(list)
    for a, c in candidates.items():
        f = (c.get("profile") or {}).get("fund_from_address")
        if f:
            funders[f].append(a)
    for f, group in funders.items():
        for i in range(1, len(group)):
            union(group[0], group[i], "low", f"shared first funder {f[:8]}")
    by_token: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for a, c in candidates.items():
        for e in (c.get("history") or {}).get("episodes", []):
            if e.get("opened_ts") and e.get("token"):
                by_token[e["token"]].append((int(e["opened_ts"]), a))
    pair_tokens: dict[tuple[str, str], set[str]] = defaultdict(set)
    for token, rows in by_token.items():
        rows.sort()
        for i, (ts, a) in enumerate(rows):
            for ts2, b in rows[i + 1:]:
                if ts2 - ts > 120:
                    break
                if a != b:
                    pair_tokens[(min(a, b), max(a, b))].add(token)
    for (a, b), toks in pair_tokens.items():
        if len(toks) >= 3:
            union(a, b, "medium", f"{len(toks)} synchronized entries between {a[:8]} and {b[:8]}")
    out = {}
    for a in addrs:
        root = find(a)
        members = [x for x in addrs if find(x) == root]
        out[a] = {"cluster": root[:8] if len(members) > 1 else None, "size": len(members),
                  "confidence": conf.get(a, "none") if len(members) > 1 else "none", "evidence": evidence.get(a, [])}
    return out


# ---- the lane ---------------------------------------------------------------------------------
class ScoutLane:
    """Owns state["scout"]. `tick()` is called once per executor cycle after every exit check;
    every step inside is budgeted and every failure is logged, never raised."""

    def __init__(self, ex: Any) -> None:
        self.ex = ex
        self.cfg = ScoutConfig()
        self.mod = sys.modules[type(ex).__module__]          # the executor module: helpers, clocks, logging
        self.st: dict[str, Any] = ex.state.setdefault("scout", {})
        for key, default in (("candidates", {}), ("positions", []), ("trades", []), ("signals", []), ("seen", {}),
                             ("baselined", []), ("inbox", {}), ("unresolved", {}), ("failed", []), ("token_sample", []),
                             ("errors", []), ("live", {}), ("recent_buys", {}), ("convergence_events", [])):
            self.st.setdefault(key, default if not isinstance(default, (dict, list)) else type(default)())
        self.st.setdefault("policy_version", policy_key(self.cfg))
        self._thread: threading.Thread | None = None
        self._result: tuple[str, Any] | None = None
        self._poll_ts = 0.0

    # ---- small helpers -------------------------------------------------------------------
    def now(self) -> float:
        return self.mod.now_ts()

    def log(self, message: str) -> None:
        self.mod.log("SCOUT " + message)

    def data_dir(self) -> Path:
        return Path(getattr(self.mod, "DATA_DIR", Path(os.getenv("DATA_DIR", "data"))))

    def _csv(self, name: str, columns: list[str], row: dict[str, Any]) -> None:
        try:
            self.mod._append_row(self.data_dir() / name, columns, {c: row.get(c, "") for c in columns})
        except Exception as exc:                                                          # bookkeeping never breaks trading
            self.log(f"WARN could not write {name}: {exc}")

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def candidate(self, address: str) -> dict[str, Any] | None:
        return self.st["candidates"].get(address)

    def state_of(self, address: str) -> str:
        c = self.candidate(address)
        return c["state"] if c else "unknown"

    def transition(self, cand: dict[str, Any], new_state: str, reason: str) -> None:
        old = cand.get("state")
        if old == new_state:
            return
        cand["state"] = new_state
        cand["state_since"] = self.now()
        cand.setdefault("history", {})
        cand.setdefault("lifecycle", []).append({"ts": self.now(), "from": old, "to": new_state, "reason": reason})
        del cand["lifecycle"][:-50]
        self.log(f"{cand['address'][:8]} {old} -> {new_state}: {reason}")

    def live_wallets(self) -> dict[str, dict[str, Any]]:
        """Scouted wallets the production copy lane must follow for new buys: only `live`
        state, only with the switch on. Their size multiplier and minimum ride along."""
        if not self.cfg.live:
            return {}
        out = {}
        for a, c in self.st["candidates"].items():
            if c.get("state") == "live":
                out[a] = {"size": self.cfg.live_size, "min_usd": float(self.ex.cfg.copy_min_buy_usd)}
        return out

    def qualification_fresh(self, cand: dict[str, Any]) -> bool:
        ev = cand.get("evaluation") or {}
        return bool(ev) and self.now() - float(ev.get("evaluated_at") or 0) <= self.cfg.max_qualification_age_hours * 3600

    # ---- the copy funnel: who may trigger a copy on its own ------------------------------
    def watch_only(self, wallet: str) -> bool:
        """A configured wallet listed in COPY_WATCH_ONLY (address or prefix): its buys feed
        confluence and paper data but are never copied on their own."""
        return any(wallet == w or (len(w) < 32 and wallet.startswith(w)) for w in self.cfg.copy_watch_only)

    def book(self, wallet: str) -> dict[str, Any]:
        """Our own record of copying `wallet`: realized P&L, closed fills, and a demotion."""
        return self.st.setdefault("live", {}).setdefault(wallet, {"realized_pnl_usd": 0.0, "fills": 0})

    def median_hold_seconds(self, wallet: str) -> float | None:
        hold = (((self.candidate(wallet) or {}).get("history") or {}).get("metrics_30d") or {}).get("median_hold_minutes")
        return float(hold) * 60 if hold is not None else None

    def configured_live(self) -> list[str]:
        """Configured wallets currently in the live set: not watch-only, not demoted."""
        return [w for w in self.ex.cfg.copy_wallets if not self.watch_only(w)]

    def entry_allowed(self, wallet: str) -> tuple[bool, str]:
        """May a new buy by `wallet` be copied on its own? Sells and open positions are never
        touched. In order: watch-only, demoted on our own fills, a sniper by its measured
        median hold, and (only with SCOUT_ELITE_ONLY=1 and GMGN evidence) a fresh qualified or
        live verdict. The first three apply with or without GMGN scouting."""
        if wallet in self.ex.cfg.copy_wallets:
            # The owner's own wallets are always copied: the scout's rules (promotion,
            # demotion, sniper filter, elite gate) are only for wallets it found itself. The
            # hard execution filters (size, impact, round trip, source exit, daily stop) still
            # apply to every buy.
            if self.watch_only(wallet):
                return False, "watch_only: in COPY_WATCH_ONLY, its buys only count toward confluence"
            return True, ""
        demoted = (self.st.get("live") or {}).get(wallet, {})
        if demoted.get("demoted_ts"):
            return False, f"demoted: {demoted.get('demoted_reason') or 'red for us'}"
        hold = self.median_hold_seconds(wallet)
        if hold is not None and hold < self.cfg.sniper_max_hold_seconds:
            return False, f"sniper: median hold {hold:.0f}s < SNIPER_MAX_HOLD_SEC {self.cfg.sniper_max_hold_seconds:.0f}s"
        if not self.cfg.elite_only or not self.enabled:
            return True, ""          # without GMGN scouting there is no evidence to gate on
        cand = self.candidate(wallet)
        if cand is None:
            return False, "elite_only: wallet not evaluated"
        if cand.get("state") not in ("qualified", "live"):
            return False, f"elite_only: wallet is {cand.get('state')}"
        if not self.qualification_fresh(cand):
            return False, "elite_only: qualification stale"
        return True, ""

    def note_live_pnl(self, wallet: str, pnl: float, closed: bool = False) -> None:
        """Realized P&L of production positions copied from `wallet` (a closed position counts
        as one of our fills). Two rules act on it: a scouted wallet's loss budget pauses it,
        and any wallet, configured or scouted, that is net red for us after
        DEMOTE_AFTER_LIVE_FILLS fills leaves the live set (its sells are still followed)."""
        entry = self.book(wallet)
        entry["realized_pnl_usd"] = round(float(entry.get("realized_pnl_usd") or 0.0) + pnl, 4)
        if closed:
            entry["fills"] = int(entry.get("fills") or 0) + 1
        cand = self.candidate(wallet)
        if cand and cand.get("state") == "live" and self.cfg.live_loss_budget_usd > 0 and entry["realized_pnl_usd"] <= -self.cfg.live_loss_budget_usd:
            self.transition(cand, "paused", f"live loss budget breached ({entry['realized_pnl_usd']:+.2f} USD)")
        if (wallet != CONVERGENCE and wallet not in self.ex.cfg.copy_wallets and self.cfg.demote_after_live_fills > 0
                and not entry.get("demoted_ts")
                and int(entry.get("fills") or 0) >= self.cfg.demote_after_live_fills and entry["realized_pnl_usd"] < 0):
            self.demote(wallet, f"red for us: {entry['realized_pnl_usd']:+.2f} USD over {entry['fills']} fills")

    def demote(self, wallet: str, reason: str) -> None:
        """Live set -> watchlist. The wallet keeps being watched and paper traded; getting back
        in takes PROMOTE_MIN_PAPER_FILLS new paper fills, net positive, after this moment."""
        entry = self.book(wallet)
        entry["demoted_ts"] = self.now()
        entry["demoted_reason"] = reason
        cand = self.candidate(wallet)
        if cand is not None:
            cand["demoted_ts"] = self.now()
            if cand.get("state") in ("live", "qualified"):
                self.transition(cand, "shadow", f"demoted to the watchlist: {reason}")
        self.log(f"{wallet[:8]} demoted from the live set: {reason}")

    def note_source_trade(self, wallet: str, ts: float | None = None) -> None:
        """Any buy or sell seen from a watched or followed wallet: the idle clock."""
        seen = self.st.setdefault("last_trade", {})
        ts = float(ts) if ts else self.now()
        if ts > float(seen.get(wallet) or 0):
            seen[wallet] = ts

    def last_trade_ts(self, wallet: str) -> float:
        """Newest evidence the wallet traded: our own on-chain observations, its fetched
        history and GMGN's last-activity field. The first time a wallet is looked at counts,
        so a fresh deploy never demotes a wallet it has not had the chance to watch."""
        cand = self.candidate(wallet) or {}
        hist = cand.get("history") or {}
        stamps = [float((self.st.get("last_trade") or {}).get(wallet) or 0)]
        stamps += [float(e.get("closed_ts") or e.get("opened_ts") or 0) for e in (hist.get("episodes") or [])[-50:]]
        for row in ((cand.get("stats") or {}).get(k) or {} for k in ("7d", "30d", "all")):
            t = _f(row.get("last_timestamp"))
            stamps.append(t / 1000.0 if t > 1e11 else t)
        first = self.st.setdefault("first_watched", {})
        stamps.append(float(first.setdefault(wallet, self.now())))
        return max(stamps)

    def idle(self, wallet: str) -> bool:
        return self.cfg.demote_idle_days > 0 and self.now() - self.last_trade_ts(wallet) > self.cfg.demote_idle_days * DAY

    def paper_record(self, wallet: str, since: float | None = None) -> dict[str, Any]:
        """Closed paper trades of a wallet (optionally only those opened after `since`):
        count, baseline net, and stress net over the trades that have a stress quote."""
        rows = [t for t in self.st["trades"] if t.get("wallet") == wallet and t.get("kind") != "convergence"
                and t.get("closed_ts") and not str(t.get("reason") or "").startswith("missed")
                and (since is None or float(t.get("opened_ts") or 0) > since)]
        stressed = [t for t in rows if t.get("pnl_stress") not in (None, "")]
        return {"fills": len(rows), "net_base_usd": round(sum(_f(t.get("pnl_base")) for t in rows), 4),
                "net_stress_usd": round(sum(_f(t.get("pnl_stress")) for t in stressed), 4) if stressed else None,
                "stress_fills": len(stressed)}

    def paper_green(self, wallet: str, since: float | None = None) -> tuple[bool, str]:
        """The paper route into the live set: enough closed paper fills, net positive at the
        baseline quote and at the 20 s stress quote (when stress quotes exist)."""
        rec = self.paper_record(wallet, since)
        if rec["fills"] < self.cfg.promote_min_paper_fills:
            return False, f"{rec['fills']} paper fills (need {self.cfg.promote_min_paper_fills})"
        if rec["net_base_usd"] <= 0:
            return False, f"paper net {rec['net_base_usd']:+.2f} USD over {rec['fills']} fills"
        if rec["net_stress_usd"] is not None and rec["net_stress_usd"] <= 0:
            return False, f"paper net {rec['net_base_usd']:+.2f} USD but {rec['net_stress_usd']:+.2f} with 20s latency"
        return True, f"paper net {rec['net_base_usd']:+.2f} USD over {rec['fills']} fills" + (
            f" ({rec['net_stress_usd']:+.2f} with 20s latency)" if rec["net_stress_usd"] is not None else "")

    def review_live_set(self) -> None:
        """Configured wallets are never demoted (the owner's picks are always copied); this only
        clears a demotion an earlier version stored for one."""
        for wallet in self.ex.cfg.copy_wallets:
            entry = (self.st.get("live") or {}).get(wallet) or {}
            if entry.get("demoted_ts"):
                entry.pop("demoted_ts", None)
                entry.pop("demoted_reason", None)
                self.log(f"{wallet[:8]} is a configured wallet: always copied, demotion cleared")

    # ---- per-cycle entry point ------------------------------------------------------------
    def tick(self, sol_price: float) -> None:
        if not self.enabled:
            return
        key = policy_key(self.cfg)
        if self.st.get("policy_version") != key and self.st["candidates"]:
            # A deploy changed the policy: re-score what is already known now, not at the
            # next discovery cycle hours away.
            self.log(f"policy {self.st.get('policy_version')} -> {key}: re-evaluating {len(self.st['candidates'])} candidate(s)")
            self.evaluate_all()
        self.st["policy_version"] = key
        self.apply_discovery()
        self.maybe_discover()
        if self.now() - float(self.st.get("reviewed_ts") or 0) >= 600:
            self.st["reviewed_ts"] = self.now()
            self.review_live_set()
        if self.now() - self._poll_ts >= self.cfg.poll_seconds:
            self._poll_ts = self.now()
            self.poll_shadow_wallets(sol_price)
            self.manage_shadow_positions(sol_price)

    # ---- discovery worker -----------------------------------------------------------------
    def maybe_discover(self, force: bool = False) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        due = self.now() - float(self.st.get("discovered_ts") or 0) >= self.cfg.discovery_hours * 3600
        # A cycle that enriched nothing (every lookup failed) is not a cycle: try again at the
        # retry interval instead of waiting out the whole discovery period.
        if self.st["candidates"] and not any(c.get("last_refresh") for c in self.st["candidates"].values()):
            due = True
        retry_ok = self.now() - float(self.st.get("attempt_ts") or 0) >= 1800
        # GMGN bans an IP that keeps pushing after a rate-limit warning, so a cycle that hit
        # one buys silence rather than retrying into the ban.
        # A pause stored under an older, longer setting is cut back to the current one.
        until = float(self.st.get("rate_limited_until") or 0)
        if until > self.now() + self.cfg.rate_limit_cooldown_hours * 3600:
            until = self.st["rate_limited_until"] = self.now() + self.cfg.rate_limit_cooldown_hours * 3600
        cooling = self.now() < until
        if cooling and not force:
            return
        if not force and not (due and retry_ok):
            return
        self.st["attempt_ts"] = self.now()
        wallets_known = dict(self.st["candidates"])
        # Recently rejected wallets count as known (and rejected) so the worker spends nothing on them.
        for address in list((self.st.get("rejected_seen") or {})):
            if address not in wallets_known and self.recently_rejected(address):
                wallets_known[address] = {"state": "rejected", "last_refresh": self.st["rejected_seen"][address]}
        configured = list(self.ex.cfg.copy_wallets)
        token_sample = self.token_sample()
        min_buy = float(self.ex.cfg.copy_min_buy_usd)
        cfg, now = self.cfg, self.now()
        # New finds are only worth enriching if they can be tracked: free slots plus the
        # enriched wallets that would give theirs up.
        room = max(0, cfg.max_candidates - len(wallets_known)) + len(self.evictable())

        def work() -> None:
            try:
                self._result = ("ok", discover_and_enrich(self.gmgn_client(), cfg, wallets_known, configured, token_sample, now, min_buy,
                                                          room=room))
            except Exception as exc:
                self._result = ("error", self.mod.describe_error(exc))

        self._thread = threading.Thread(target=work, name="scout-discovery", daemon=True)
        self._thread.start()

    def gmgn_client(self) -> Any:
        import gmgn
        client = gmgn.Gmgn()
        client.pause_seconds = self.cfg.pause_seconds   # scouting is the low-priority user of the shared GMGN budget
        return client

    def token_sample(self) -> list[str]:
        """Tokens whose top traders are worth a look: what the followed wallets bought, what we
        traded (winners and losers alike), plus GMGN's trending list at discovery time."""
        mints: list[str] = []
        for t in list(self.ex.state.get("positions") or []) + list(self.st.get("positions") or []):
            if t.get("mint"):
                mints.append(t["mint"])
        try:
            rows = list(csv.DictReader(open(self.data_dir() / "copy_signals.csv")))[-200:]
            mints.extend(r.get("mint", "") for r in rows if r.get("kind") == "first")
        except Exception:
            pass
        try:
            rows = list(csv.DictReader(open(self.data_dir() / "live_trades.csv")))[-100:]
            mints.extend(r.get("mint", "") for r in rows)
        except Exception:
            pass
        seen, out = set(), []
        for m in reversed(mints):
            if m and m not in seen and m != WSOL:
                seen.add(m)
                out.append(m)
        return out

    def apply_discovery(self) -> None:
        if self._result is None:
            return
        status, payload = self._result
        self._result = None
        if status != "ok":
            self.st.setdefault("errors", []).append({"ts": self.now(), "what": "discovery", "why": payload})
            del self.st["errors"][:-50]
            self.log(f"WARN discovery failed: {payload}; retrying in 30m")
            return
        if payload.get("rate_limited"):
            # Not a finished cycle: pick the backlog back up after the pause (and the 30m
            # retry spacing) instead of waiting out a whole discovery period.
            until = self.now() + self.cfg.rate_limit_cooldown_hours * 3600
            self.st["rate_limited_until"] = until
            self.log(f"WARN GMGN rate-limited this cycle; pausing discovery for "
                     f"{self.cfg.rate_limit_cooldown_hours * 60:.0f}m and resuming at a slower pace")
        else:
            self.st["discovered_ts"] = self.now()
            self.st.pop("rate_limited_until", None)
        self.st["token_sample"] = payload.get("token_sample", [])
        if payload.get("overlap") is not None and (payload.get("overlap") or payload.get("runners")):
            self.st["overlap"] = {"ts": self.now(), "runners": payload.get("runners") or [], "wallets": payload.get("overlap") or []}
            for row in payload.get("overlap") or []:
                cand = self.st["candidates"].get(row["address"])
                if cand is not None:
                    cand["overlap_hits"] = row["hits"]
        self.st["last_units"] = payload.get("units", 0)
        for err in payload.get("errors", []):
            self.st.setdefault("errors", []).append({"ts": self.now(), "what": "enrich", "why": err})
        del self.st["errors"][:-50]
        cands = self.st["candidates"]
        for address, update in payload.get("candidates", {}).items():
            cand = cands.get(address)
            if cand is None:
                if self.recently_rejected(address):
                    continue
                if len(cands) >= self.cfg.max_candidates and address not in self.ex.cfg.copy_wallets:
                    worst = self.evictable()
                    if not worst:
                        continue
                    self.evict(worst[0])
                cand = {"address": address, "state": "discovered", "state_since": self.now(), "discovered_at": self.now(),
                        "sources": [], "rank_snapshots": [], "lifecycle": []}
                cands[address] = cand
            for src in update.get("sources", []):
                if not any(s["source"] == src["source"] for s in cand["sources"]):
                    cand["sources"].append(src)
            cand["source_kinds"] = sorted({s["source"].split(":")[0] for s in cand["sources"]})
            for snap in update.get("rank_snapshots", []):
                cand["rank_snapshots"].append(snap)
            del cand["rank_snapshots"][:-200]
            for key in ("screen", "tags", "profile", "exposure", "stats", "holdings", "holdings_error", "history", "risk_flags", "last_refresh", "refresh_error"):
                if key in update:
                    cand[key] = update[key]
        for row in payload.get("overlap") or []:
            if row["address"] in cands:
                cands[row["address"]]["overlap_hits"] = row["hits"]
        clusters = relationship_clusters(cands)
        for a, c in cands.items():
            c["relationship"] = clusters.get(a)
        self.evaluate_all()
        self.log(f"discovery applied: {len(payload.get('candidates', {}))} wallet(s) touched, {payload.get('screened', 0)} screened, "
                 f"{payload.get('enriched', 0)} fully enriched, {len(cands)} tracked, {payload.get('units', 0)} GMGN units")

    # ---- evaluation and lifecycle ---------------------------------------------------------
    def evaluate_all(self) -> None:
        now = self.now()
        cands = self.st["candidates"]
        by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in self.st["trades"]:
            if t.get("kind") != "convergence":                # the group signal is judged on its own
                by_wallet[t["wallet"]].append(t)
        first_signal: dict[str, float] = {}
        for s in self.st["signals"]:
            first_signal[s["wallet"]] = min(first_signal.get(s["wallet"], s["ts"]), s["ts"])
        for address, cand in cands.items():
            cand["shadow"] = shadow_metrics(by_wallet.get(address, []), now, first_signal.get(address))
            ev = evaluate_candidate(cand, self.cfg, now, float(self.ex.cfg.copy_wallet_min_usd.get(address, self.ex.cfg.copy_min_buy_usd)))
            cand["evaluation"] = ev
            self.apply_lifecycle(cand, ev)
        self.select_live()

    def apply_lifecycle(self, cand: dict[str, Any], ev: dict[str, Any]) -> None:
        """discovered -> research -> shadow (the watchlist) -> qualified -> live, and back.
        Into the live set by either route: every history gate (the fast track, profit factor
        included), or paper-green (PROMOTE_MIN_PAPER_FILLS closed paper fills since any
        demotion, net positive at baseline and 20 s stress). Out of it when red for us after
        DEMOTE_AFTER_LIVE_FILLS fills (note_live_pnl), idle for DEMOTE_IDLE_DAYS, or when
        neither route holds any more. Bad tags, hard risk flags or a sniper-farm hold time
        reject the wallet from the watchlist altogether."""
        state = cand.get("state")
        gates = ev["gates"]
        for name in ("screen", "tags", "risk_flags", "sniper"):
            if (gates.get(name) or {}).get("status") == "fail":
                if state != "rejected":
                    self.transition(cand, "rejected", gates[name]["detail"])
                return
        if state == "rejected":
            self.transition(cand, "research", "flags cleared on refresh")
            state = "research"
        if state == "discovered" and cand.get("last_refresh"):
            self.transition(cand, "research", "enriched")
            state = "research"
        historical_failed = [k for k in ev["failed"] if not k.startswith("shadow")]
        if state == "research" and cand.get("last_refresh"):
            self.transition(cand, "shadow", "on the watchlist: first buys paper traded with executable quotes; " + (
                f"open gates: {', '.join(historical_failed)}" if historical_failed else "history gates pass so far"))
            state = "shadow"
        demoted_ts = cand.get("demoted_ts")
        paper_ok, paper_detail = self.paper_green(cand["address"], since=float(demoted_ts) if demoted_ts else None)
        route = "history" if ev["qualified"] else ("paper" if paper_ok else "")
        cand["route"] = route
        cand["paper"] = {"ok": paper_ok, "detail": paper_detail, **self.paper_record(cand["address"])}
        idle = self.idle(cand["address"])
        if state == "shadow" and route and not idle:
            if demoted_ts and self.now() - float(demoted_ts) < self.cfg.requalify_hours * 3600:
                return
            self.transition(cand, "qualified", "every history gate passed with fresh evidence" if route == "history" else paper_detail)
            return
        sh = cand.get("shadow") or {}
        if state == "live" and sh.get("trades", 0) >= self.cfg.tripwire_trades and _f(sh.get("net_base_usd")) < 0 and not paper_ok:
            cand["demoted_ts"] = self.now()
            self.transition(cand, "paused", f"tripwire: shadow net {sh['net_base_usd']:+.2f} USD after {sh['trades']} trades")
            return
        if state in ("qualified", "live") and idle:
            self.demote(cand["address"], f"no trade in {self.cfg.demote_idle_days:.0f} days")
            return
        if state in ("qualified", "live") and not route:
            why = ", ".join(ev["failed"]) or ("missing " + ", ".join(ev["missing"]))
            target = "paused" if state == "live" else "shadow"
            cand["demoted_ts"] = self.now()
            self.transition(cand, target, f"no route into the live set any more: history ({why}); {paper_detail}")
            return
        if state == "paused" and route:
            if demoted_ts and self.now() - float(demoted_ts) < self.cfg.requalify_hours * 3600:
                return
            budget = (self.st.get("live") or {}).get(cand["address"], {}).get("realized_pnl_usd", 0.0)
            if self.cfg.live_loss_budget_usd > 0 and budget <= -self.cfg.live_loss_budget_usd:
                return                                             # budget breached: needs a reset by the operator
            self.transition(cand, "qualified", "requalified with fresh evidence after the cooldown")

    def select_live(self) -> dict[str, Any]:
        """Promote qualified wallets into the live set, best score first, one per evidenced
        cluster, only with the live switch on and a positive per-wallet loss budget. The live
        set (configured wallets still live plus scouted live wallets) never exceeds
        LIVE_COPY_MAX, and scouted wallets never exceed SCOUT_MAX_LIVE."""
        cands = self.st["candidates"]
        configured = set(self.ex.cfg.copy_wallets)
        qualified = [c for c in cands.values() if c.get("state") == "qualified" and c["address"] not in configured
                     and (c.get("route") == "paper" or self.qualification_fresh(c))]
        live = [c for c in cands.values() if c.get("state") == "live"]
        cap = min(self.cfg.max_live, max(0, self.cfg.live_copy_max - len(self.configured_live())))
        decision: dict[str, Any] = {"ts": self.now(), "promoted": [], "reason": "", "cap": cap}
        if not qualified:
            decision["reason"] = "no wallet on the watchlist has a route into the live set yet" + (" (live switch off)" if not self.cfg.live else "")
        elif not self.cfg.live:
            decision["reason"] = f"{len(qualified)} qualified; SCOUT_LIVE=0 so none is promoted"
        elif self.cfg.live_loss_budget_usd <= 0:
            decision["reason"] = "SCOUT_LIVE_LOSS_BUDGET_USD must be set above 0 before any promotion"
        else:
            taken_clusters = {(c.get("relationship") or {}).get("cluster") for c in live if (c.get("relationship") or {}).get("cluster")}
            for c in sorted(qualified, key=lambda c: (c.get("route") != "paper", -(c.get("evaluation") or {}).get("score", {}).get("total", 0))):
                if len(live) >= cap:
                    decision["reason"] = f"live set full ({len(live)} scouted, cap {cap})"
                    break
                rel = c.get("relationship") or {}
                if rel.get("cluster") and rel.get("confidence") in ("medium", "high") and rel["cluster"] in taken_clusters:
                    continue
                self.book(c["address"]).pop("demoted_ts", None)
                self.transition(c, "live", f"promoted ({c.get('route') or 'history'} route) at {self.cfg.live_size:.0%} size, "
                                           f"budget {self.cfg.live_loss_budget_usd:.0f} USD")
                live.append(c)
                decision["promoted"].append(c["address"])
                if rel.get("cluster"):
                    taken_clusters.add(rel["cluster"])
            decision["reason"] = decision["reason"] or f"{len(decision['promoted'])} promoted"
        self.st["last_selection"] = decision
        return decision

    # ---- shadow: watch first buys with executable quotes ----------------------------------
    def recently_rejected(self, address: str) -> bool:
        ts = (self.st.get("rejected_seen") or {}).get(address)
        return ts is not None and self.now() - float(ts) < self.cfg.reject_memory_days * DAY

    def evict(self, address: str) -> None:
        cand = self.st["candidates"].pop(address, None)
        if cand is not None and cand.get("state") == "rejected":
            seen = self.st.setdefault("rejected_seen", {})
            seen[address] = self.now()
            if len(seen) > 20000:
                for a in sorted(seen, key=seen.get)[: len(seen) - 20000]:
                    seen.pop(a, None)

    def evictable(self) -> list[str]:
        """Tracked wallets that may give up their slot to a new find, weakest first: already
        enriched and evaluated with a failed history gate, not configured, not qualified or
        live, not among the wallets polled on-chain, and with no open shadow position. A wallet
        never enriched keeps its slot until it has had its turn, and rejected wallets stay so
        they are not enriched again."""
        cands = self.st["candidates"]
        # Protected: the best SCOUT_MAX_SHADOW of the watchlist and any wallet with an open
        # paper position. (Reading the rotation here would move it.)
        polled = set(self.watchlist()[: self.cfg.max_shadow]) | {p["wallet"] for p in self.st["positions"]}
        configured = set(self.ex.cfg.copy_wallets)
        out, rejected = [], []
        for address, c in cands.items():
            ev = c.get("evaluation") or {}
            if address in configured or address in polled:
                continue
            if c.get("state") == "rejected":
                rejected.append(address)          # remembered in rejected_seen once evicted
            elif (c.get("state") in ("discovered", "research", "shadow") and c.get("last_refresh")
                    and any(not k.startswith("shadow") for k in ev.get("failed") or [])):
                out.append(address)
        rejected.sort(key=lambda a: float(cands[a].get("state_since") or 0))
        out.sort(key=lambda a: ((cands[a].get("evaluation") or {}).get("score", {}).get("total", 0), float(cands[a].get("last_refresh") or 0)))
        return rejected + out

    def watchlist(self) -> list[str]:
        """Every wallet on the watchlist: enriched, not rejected, best score first, capped at
        WATCHLIST_MAX."""
        cands = self.st["candidates"]
        watch = [a for a, c in cands.items() if c.get("state") in ("shadow", "qualified", "live", "paused")]
        watch.sort(key=lambda a: -((cands[a].get("evaluation") or {}).get("score", {}).get("total", 0)))
        return watch[: self.cfg.watchlist_max]

    def shadow_wallets(self) -> list[str]:
        """Wallets polled on-chain this round: first those with an open paper position (their
        sells close it), then the next SCOUT_WATCH_POLL_WALLETS of the watchlist in rotation,
        so a few hundred wallets are each visited every few minutes at a steady RPC cost."""
        watch = self.watchlist()
        per = self.cfg.watch_poll_wallets
        out: list[str] = []
        for p in self.st["positions"]:
            if p["wallet"] not in out and p["wallet"] != CONVERGENCE and len(out) < per:
                out.append(p["wallet"])
        if watch:
            cursor = int(self.st.get("watch_cursor") or 0) % len(watch)
            ring = watch[cursor:] + watch[:cursor]
            picked = [w for w in ring if w not in out][:per]
            out.extend(picked)
            self.st["watch_cursor"] = (cursor + len(picked)) % len(watch)
        return out

    def poll_shadow_wallets(self, sol_price: float) -> None:
        wallets = self.shadow_wallets()
        if not wallets:
            return
        budget = self.cfg.decode_budget
        for wallet in wallets:
            if budget <= 0:
                break
            seen = self.st["seen"].setdefault(wallet, [])
            baselined = self.st["baselined"]
            try:
                rows = self.ex.copy_fetch_rows(wallet, seen, wallet in baselined)
            except Exception as exc:
                self.log(f"WARN poll {wallet[:8]}: {self.mod.describe_error(exc)}")
                continue
            if wallet not in baselined:
                baselined.append(wallet)
                fresh = [r for r in rows if r.get("blockTime") and self.now() - int(r["blockTime"]) <= self.ex.cfg.copy_max_tx_age_seconds]
                seen.extend(r.get("signature") for r in rows if r.get("signature") and r not in fresh)
            inbox = self.st["inbox"].setdefault(wallet, [])
            queued = {e["signature"] for e in inbox}
            for row in reversed(rows):
                sig = row.get("signature")
                if not sig or sig in seen or sig in queued or row.get("err"):
                    if sig and sig not in seen:
                        seen.append(sig)
                    continue
                seen.append(sig)
                inbox.append({"signature": sig, "blockTime": row.get("blockTime")})
                queued.add(sig)
            del seen[:-1000]
            inbox.sort(key=lambda e: int(e.get("blockTime") or 0))
            deferred = []
            while inbox and budget > 0:
                entry = inbox.pop(0)
                if float(entry.get("retry_ts") or 0) > self.now():
                    deferred.append(entry)
                    continue
                budget -= 1
                self.shadow_fetch_and_handle(wallet, entry["signature"], entry.get("blockTime"), sol_price)
            inbox.extend(deferred)

    def shadow_fetch_and_handle(self, wallet: str, sig: str, block_time: Any, sol_price: float) -> None:
        try:
            tx = self.ex.rpc.transaction(sig)
        except Exception as exc:
            tx, why = None, self.mod.describe_error(exc)
        else:
            why = "no transaction returned"
        if not tx:
            unresolved = self.st["unresolved"]
            entry = unresolved.get(sig) or {"wallet": wallet, "block_time": block_time, "attempts": 0}
            entry["attempts"] += 1
            if entry["attempts"] >= 3:
                unresolved.pop(sig, None)
                self.st["failed"].append({"signature": sig, "wallet": wallet, "why": why, "ts": self.now()})
                del self.st["failed"][:-200]
            else:
                unresolved[sig] = entry
                self.st["inbox"].setdefault(wallet, []).append({"signature": sig, "blockTime": block_time,
                                                                "retry_ts": self.now() + 30.0 * entry["attempts"]})
            return
        self.st["unresolved"].pop(sig, None)
        swap = self.mod.wallet_swap_from_transaction(tx, wallet)
        if not swap:
            return
        self.note_source_trade(wallet, block_time)
        usd = swap["sol"] * sol_price + swap.get("stable_usd", 0.0)
        age = self.now() - int(block_time) if block_time else 0.0
        if swap["side"] == "sell":
            self.shadow_follow_sell(wallet, swap["mint"], float(swap.get("fraction") or 1.0), sol_price, sig, block_time)
            return
        self.shadow_handle_buy(wallet, sig, block_time, swap, usd, age, sol_price)

    def record_signal(self, row: dict[str, Any]) -> None:
        row = {"ts": self.now(), **row}
        self.st["signals"].append(row)
        del self.st["signals"][:-2000]
        self._csv("scout_signals.csv", ["ts", "wallet", "mint", "signature", "source_usd", "status", "reason", "size_usd", "portfolio_admitted"],
                  {**row, "ts": self.mod.utc_iso(row["ts"])})

    def shadow_handle_buy(self, wallet: str, sig: str, block_time: Any, swap: dict[str, Any], usd: float, age: float, sol_price: float) -> None:
        ex, cfg = self.ex, self.ex.cfg
        mint = swap["mint"]
        if self.state_of(wallet) == "rejected":
            # A rejected wallet (sniper farm, bad tags) is still polled while a paper position of
            # its own is open, so that position can follow its sells; it opens nothing new and
            # never counts toward confluence.
            return
        base = {"wallet": wallet, "mint": mint, "signature": sig, "source_usd": round(usd)}
        minimum = float(cfg.copy_wallet_min_usd.get(wallet, cfg.copy_min_buy_usd))
        if usd < minimum:
            # Below the lone-copy minimum: no paper position of its own, but a first buy of at
            # least CONFLUENCE_MIN_SOURCE_USD still counts toward confluence.
            self.note_confluence_buy(wallet, sig, block_time, swap, usd, sol_price)
            return
        if age > cfg.copy_max_tx_age_seconds:
            self.record_signal({**base, "status": "blocked", "reason": "too_late"})
            return
        pre, bought = int(swap.get("pre_tokens") or 0), int(swap.get("tokens") or 0)
        if cfg.copy_first_buy_only and bought > 0 and pre > bought * cfg.copy_add_dust_ratio:
            self.record_signal({**base, "status": "blocked", "reason": "add"})
            return
        # Every qualifying first buy feeds the convergence detector, whether or not this
        # wallet's own shadow position can be opened (budget, already held).
        self.note_first_buy(wallet, mint, sig, block_time, usd, bought)
        if any(p["wallet"] == wallet and p["mint"] == mint for p in self.st["positions"]):
            self.record_signal({**base, "status": "blocked", "reason": "already_held"})
            self.check_convergence(mint, sol_price)
            return
        self.open_shadow_position(base, wallet, mint, sig, block_time, usd, age, sol_price)
        self.check_convergence(mint, sol_price)

    def open_shadow_position(self, base: dict[str, Any], wallet: str, mint: str, sig: str, block_time: Any, usd: float,
                             age: float, sol_price: float, size_multiplier: float | None = None,
                             extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Quote and open one shadow position under production's sizing and entry guards.
        `wallet` is the source wallet, or the CONVERGENCE label for a multi-wallet signal.
        Returns the position, or None with the reason recorded as a signal."""
        ex, cfg = self.ex, self.ex.cfg
        if self._quotes_used >= self.cfg.quote_budget:
            self.record_signal({**base, "status": "failed", "reason": "missed:quote_budget"})
            return None
        # Hypothetical size: what production would trade for this wallet, under the same guards.
        daily_pnl = float(ex.state["daily"]["realized_pnl_usd"])
        open_prod = sum(1 for p in ex.state["positions"] if not p.get("adopted") or cfg.copy_only)
        open_shadow = sum(1 for p in self.st["positions"] if p.get("portfolio_admitted"))
        try:
            equity = ex.equity_usd(sol_price)
        except Exception as exc:
            self.record_signal({**base, "status": "failed", "reason": f"missed:equity ({self.mod.describe_error(exc)})"})
            return
        size = self.mod.position_size_usd(cfg, equity, 0, daily_pnl)
        if size <= 0:
            size = max(cfg.min_position_usd, min(equity * cfg.account_fraction, cfg.max_position_usd))
        multiplier = float(cfg.copy_wallet_size.get(wallet, 1.0)) if size_multiplier is None else float(size_multiplier)
        size = round(size * multiplier, 2)
        admitted = (open_prod + open_shadow < cfg.max_concurrent
                    and ex.deployed_usd() + sum(p["size_usd"] for p in self.st["positions"] if p.get("portfolio_admitted")) + size
                    <= equity * cfg.max_deployed_fraction)
        lamports = int(size / sol_price * LAMPORTS)
        self._quotes_used += 2
        try:
            quote = ex.jup.quote(WSOL, mint, lamports)
            tokens = int(quote["outAmount"])
            impact = self.mod.quote_price_impact_pct(quote)
            reverse = ex.jup.quote(mint, WSOL, tokens)
            round_trip = int(reverse["outAmount"]) / lamports * 100 if lamports else 0.0
        except Exception as exc:
            self.record_signal({**base, "status": "failed", "reason": "missed:unpriceable", "size_usd": size, "portfolio_admitted": admitted})
            self.st["trades"].append({**base, "opened_ts": self.now(), "closed_ts": None, "cost_usd": size, "reason": "missed:unpriceable",
                                      "why": self.mod.describe_error(exc), "portfolio_admitted": admitted, **(extra or {})})
            return None
        if tokens <= 0 or (impact is not None and impact > cfg.max_price_impact_pct) or round_trip < cfg.min_entry_round_trip_pct:
            why = f"impact {impact}% round_trip {round_trip:.0f}%"
            self.record_signal({**base, "status": "failed", "reason": "missed:guards", "size_usd": size, "portfolio_admitted": admitted})
            self.st["trades"].append({**base, "opened_ts": self.now(), "closed_ts": None, "cost_usd": size, "reason": "missed:guards",
                                      "why": why, "portfolio_admitted": admitted, **(extra or {})})
            return None
        pos = {"id": f"{wallet[:8]}:{sig[:16]}", "wallet": wallet, "mint": mint, "signature": sig, "source_usd": round(usd),
               "opened_ts": self.now(), "source_ts": int(block_time) if block_time else None, "latency_s": round(age, 1),
               "size_usd": size, "cost_usd": round(size + self.cfg.fee_usd, 4), "tokens": tokens, "entry_tokens": tokens,
               "entry_basis_usd": size, "position_usd": size, "tokens_stress": None, "stress_due_ts": self.now() + self.cfg.stress_seconds,
               "stress_attempts": 0, "entry_price_impact_pct": impact, "entry_round_trip_pct": round(round_trip, 1),
               "ladder": [dict(r) for r in cfg.copy_ladder], "peak_usd": size, "last_value_usd": size, "realized_usd": 0.0,
               "realized_stress_usd": 0.0, "copy": wallet, "portfolio_admitted": admitted, "next_check_ts": 0, **(extra or {})}
        self.st["positions"].append(pos)
        self.record_signal({**base, "status": "shadow_opened", "reason": "shadow", "size_usd": size, "portfolio_admitted": admitted})
        who = "convergence" if wallet == CONVERGENCE else f"{wallet[:8]} bought"
        self.log(f"{who} {mint} for ${usd:,.0f}; shadow buy ${size:.2f} -> {tokens} tokens "
                 f"(impact {impact}%, round trip {round_trip:.0f}%, {age:.0f}s after source, {'in' if admitted else 'outside'} portfolio)")
        return pos

    # ---- convergence: several independent tracked wallets entering the same coin ------------
    def note_confluence_buy(self, wallet: str, sig: str, block_time: Any, swap: dict[str, Any], usd: float, sol_price: float) -> None:
        """A first buy by any watched or followed wallet, for confluence: at least
        CONFLUENCE_MIN_SOURCE_USD, a first buy (not an add), then check whether it completes a
        confluence on its coin. Idempotent per signature."""
        if not self.enabled or usd < self.cfg.confluence_min_source_usd:
            return
        pre, bought = int(swap.get("pre_tokens") or 0), int(swap.get("tokens") or 0)
        if bought > 0 and pre > bought * self.ex.cfg.copy_add_dust_ratio:
            return
        self.note_first_buy(wallet, swap["mint"], sig, block_time, usd, bought)
        self.check_convergence(swap["mint"], sol_price)

    def note_first_buy(self, wallet: str, mint: str, sig: str, block_time: Any, usd: float, tokens: int = 0) -> None:
        ts = float(block_time) if block_time else self.now()
        recent = self.st.setdefault("recent_buys", {})
        rows = recent.setdefault(mint, [])
        if any(r.get("signature") == sig for r in rows):
            return
        rows.append({"wallet": wallet, "ts": ts, "signature": sig, "usd": round(usd), "tokens": int(tokens or 0)})
        self.prune_recent_buys()

    def prune_recent_buys(self) -> None:
        horizon = self.now() - max(self.cfg.convergence_window_seconds, self.cfg.convergence_cooldown_hours * 3600)
        recent = self.st.setdefault("recent_buys", {})
        for mint in list(recent):
            recent[mint] = [r for r in recent[mint] if float(r.get("ts") or 0) >= horizon]
            if not recent[mint]:
                del recent[mint]

    def independent_groups(self, wallets: list[str]) -> list[list[str]]:
        """Wallets grouped by relationship cluster: a medium/high-confidence cluster counts as
        one participant however many of its members bought, so two addresses that look
        commonly controlled never make a convergence on their own."""
        groups: dict[str, list[str]] = {}
        for w in wallets:
            rel = (self.candidate(w) or {}).get("relationship") or {}
            key = rel.get("cluster") if rel.get("cluster") and rel.get("confidence") in ("medium", "high") else w
            groups.setdefault(key, []).append(w)
        return list(groups.values())

    def check_convergence(self, mint: str, sol_price: float) -> None:
        """Open one convergence shadow position for `mint` when at least
        SCOUT_CONVERGENCE_MIN_WALLETS independent tracked wallets bought it within the window,
        at most once per coin per cooldown. The event is recorded whether or not the shadow
        position could be opened, so the frequency of the signal is known either way."""
        if not self.cfg.convergence:
            return
        rows = (self.st.get("recent_buys") or {}).get(mint) or []
        if len(rows) < self.cfg.convergence_min_wallets:
            return
        newest = max(float(r["ts"]) for r in rows)
        window = [r for r in rows if newest - float(r["ts"]) <= self.cfg.convergence_window_seconds]
        wallets = list(dict.fromkeys(r["wallet"] for r in window))
        groups = self.independent_groups(wallets)
        if len(groups) < self.cfg.convergence_min_wallets:
            return
        events = self.st.setdefault("convergence_events", [])
        cooldown = self.cfg.convergence_cooldown_hours * 3600
        if any(e.get("mint") == mint and self.now() - float(e.get("ts") or 0) < cooldown for e in events):
            return
        if any(p["wallet"] == CONVERGENCE and p["mint"] == mint for p in self.st["positions"]):
            return
        first_ts, last_ts = min(float(r["ts"]) for r in window), newest
        source_usd = sum(float(r.get("usd") or 0) for r in window)
        overlap = self.pair_overlap(wallets)
        sig = f"conv:{mint[:12]}:{int(last_ts)}"
        event = {"ts": self.now(), "mint": mint, "wallets": wallets, "independent": len(groups), "first_ts": first_ts,
                 "last_ts": last_ts, "spread_s": round(last_ts - first_ts, 1), "source_usd": round(source_usd),
                 "pair_overlap": overlap, "signature": sig}
        events.append(event)
        del events[:-2000]
        self.log(f"CONVERGENCE {mint}: {len(groups)} independent tracked wallet(s) bought within {event['spread_s']:.0f}s "
                 f"({', '.join(w[:8] for w in wallets)}; ${source_usd:,.0f} combined; usual overlap {overlap:.0%})")
        self._csv("scout_convergence.csv", ["ts", "mint", "wallets", "independent", "spread_s", "source_usd", "pair_overlap", "signature"],
                  {**event, "ts": self.mod.utc_iso(event["ts"]), "wallets": " ".join(wallets)})
        base = {"wallet": CONVERGENCE, "mint": mint, "signature": sig, "source_usd": round(source_usd)}
        age = self.now() - last_ts
        pos = self.open_shadow_position(base, CONVERGENCE, mint, sig, int(last_ts), source_usd, age, sol_price, size_multiplier=1.0,
                                        extra={"kind": "convergence", "wallets": wallets, "independent": len(groups),
                                               "spread_s": event["spread_s"], "pair_overlap": overlap})
        event["shadow_opened"] = pos is not None
        group_tokens = sum(int(r.get("tokens") or 0) for r in window)
        event["live"] = self.enter_convergence_live(mint, sig, last_ts, source_usd, wallets, sol_price, group_tokens)

    def convergence_live_block(self) -> str:
        """Why live convergence may not buy right now, or "" when it may."""
        if not self.cfg.convergence_live:
            return "SCOUT_CONVERGENCE_LIVE=0"
        realized = float((self.st.get("live") or {}).get(CONVERGENCE, {}).get("realized_pnl_usd") or 0.0)
        if self.cfg.convergence_loss_budget_usd > 0 and realized <= -self.cfg.convergence_loss_budget_usd:
            return f"loss budget spent ({realized:+.2f} USD <= -{self.cfg.convergence_loss_budget_usd:.0f})"
        closed = [t for t in self.st["trades"] if t.get("kind") == "convergence" and t.get("closed_ts")]
        if self.cfg.convergence_tripwire_trades > 0 and len(closed) >= self.cfg.convergence_tripwire_trades:
            net = sum(float(t.get("pnl_base") or 0.0) for t in closed)
            if net < 0:
                return f"shadow tripwire: {len(closed)} closed shadow trades net {net:+.2f} USD"
        open_live = sum(1 for p in self.ex.state["positions"] if p.get("convergence"))
        if self.cfg.convergence_max_open > 0 and open_live >= self.cfg.convergence_max_open:
            return f"{open_live} live convergence position(s) open (SCOUT_CONVERGENCE_MAX_OPEN={self.cfg.convergence_max_open})"
        return ""

    def enter_convergence_live(self, mint: str, sig: str, last_ts: float, source_usd: float, wallets: list[str],
                               sol_price: float, group_tokens: int = 0) -> str:
        """Buy the converging coin for real through production's entry path (every sizing,
        impact and round-trip guard applies). Returns what happened, for the event record."""
        ex = self.ex
        why = self.convergence_live_block()
        if not why and ex.state.get("draining"):
            why = "draining"
        if not why and self.now() - last_ts > self.cfg.confluence_max_age_seconds:
            why = f"last buy {self.now() - last_ts:.0f}s old > CONFLUENCE_MAX_AGE_SEC {self.cfg.confluence_max_age_seconds:.0f}s"
        if not why and (any(p.get("mint") == mint for p in ex.state["positions"]) or any(p.get("mint") == mint for p in ex.pending)):
            why = "already held"
        if why:
            self.log(f"CONVERGENCE {mint}: not bought live ({why})")
            return f"skipped: {why}"
        self.log(f"CONVERGENCE {mint}: buying live at {self.cfg.convergence_live_size:.0%} size")
        ex.last_skip = None
        ex.enter_with_retry({"mint": mint, "graduated_ts": int(last_ts), "enter_at": self.now(), "copy": CONVERGENCE,
                             "copy_buy_usd": round(source_usd), "copy_signature": sig, "copy_size": self.cfg.convergence_live_size,
                             # The whole group leaving at once is the exit to survive: the
                             # source-exit guard quotes the sell of everything they just bought.
                             "copy_tokens": group_tokens, "convergence_wallets": wallets,
                             "convergence_spread_s": round(max(0.0, last_ts - min(float(r["ts"]) for r in (self.st.get("recent_buys") or {}).get(mint, [{"ts": last_ts}]))), 1)},
                            sol_price)
        pos = next((p for p in ex.state["positions"] if p.get("copy_signature") == sig), None)
        if pos is None:
            skip = ex.last_skip[1] if ex.last_skip and ex.last_skip[0] == mint else "entry failed"
            return f"failed: {skip}"
        pos["convergence"] = True
        pos["convergence_wallets"] = wallets
        self.mod.save_state(ex.state)
        return "bought"

    def pair_overlap(self, wallets: list[str]) -> float:
        """How often these wallets already buy the same coins: shared coins over the smaller
        wallet's coin count, averaged over pairs, from the recent-buy record. 0 when unknown.
        A high value means the group converging is their habit, not news."""
        coins: dict[str, set[str]] = defaultdict(set)
        for mint, rows in (self.st.get("recent_buys") or {}).items():
            for r in rows:
                coins[r["wallet"]].add(mint)
        pairs, total = 0, 0.0
        for i, a in enumerate(wallets):
            for b in wallets[i + 1:]:
                smaller = min(len(coins[a]), len(coins[b]))
                if smaller:
                    total += len(coins[a] & coins[b]) / smaller
                    pairs += 1
        return round(total / pairs, 3) if pairs else 0.0

    def convergence_summary(self) -> dict[str, Any]:
        return convergence_report(self.st, self.cfg)

    def shadow_follow_sell(self, wallet: str, mint: str, fraction: float, sol_price: float, sig: str,
                           block_time: Any = None) -> None:
        cfg = self.ex.cfg

        def predates(pos: dict[str, Any], opened_key: str) -> bool:
            # A sale made before we entered belongs to the member's earlier episode (it
            # bought and flipped while we were still deciding); it does not end our position.
            return bool(block_time) and int(block_time) < float(pos.get(opened_key) or 0) - 30

        # A live convergence position exits on the first member's full sell, like its shadow.
        # Its members are scout wallets the production copy poll does not watch, so this is
        # the only place their sells are seen.
        if fraction >= cfg.copy_full_sell_fraction:
            for pos in list(self.ex.state["positions"]):
                if pos.get("convergence") and pos.get("mint") == mint and wallet in (pos.get("convergence_wallets") or []):
                    if predates(pos, "opened_ts"):
                        self.log(f"CONVERGENCE {mint}: member {wallet[:8]} sold before our entry; own exits apply")
                        continue
                    self.log(f"CONVERGENCE {mint}: member {wallet[:8]} sold {fraction:.0%}; closing the live position")
                    self.ex.copy_execute_exit(pos, wallet, sig, fraction, 0, sol_price)
        for pos in list(self.st["positions"]):
            if pos["mint"] != mint:
                continue
            if pos["wallet"] == CONVERGENCE:
                # The group's first full exit ends the convergence: the same rule production
                # applies to any followed wallet selling a coin it holds.
                if wallet in (pos.get("wallets") or []) and fraction >= cfg.copy_full_sell_fraction and not predates(pos, "opened_ts"):
                    pos["exit_wallet"] = wallet
                    self.shadow_exit(pos, 1.0, "convergence_member_sell", sol_price)
                continue
            if pos["wallet"] != wallet:
                continue
            if fraction >= cfg.copy_full_sell_fraction:
                self.shadow_exit(pos, 1.0, "copy_sell", sol_price)
            else:
                worth = fraction * float(pos.get("last_value_usd") or pos["size_usd"])
                if worth >= 1.0:
                    self.shadow_exit(pos, fraction, "copy_trim", sol_price)

    def manage_shadow_positions(self, sol_price: float) -> None:
        positions = self.st["positions"]
        if not positions:
            return
        # Stress quotes: the same buy quoted again 20s after the baseline.
        for pos in positions:
            if pos.get("tokens_stress") is None and pos.get("stress_attempts", 0) < 3 and self.now() >= float(pos.get("stress_due_ts") or 0) \
                    and self._quotes_used < self.cfg.quote_budget:
                self._quotes_used += 1
                pos["stress_attempts"] += 1
                try:
                    q = self.ex.jup.quote(WSOL, pos["mint"], int(pos["size_usd"] / sol_price * LAMPORTS))
                    pos["tokens_stress"] = int(q["outAmount"])
                except Exception as exc:
                    pos["stress_error"] = self.mod.describe_error(exc)
        # Exits that could not be quoted last time (budget or a quote failure) go first.
        for pos in list(positions):
            pend = pos.get("pending_exit")
            if pend and self._quotes_used < self.cfg.quote_budget:
                self.shadow_exit(pos, float(pend["fraction"]), pend["reason"], sol_price)
        # Marks from the batched price feed; a sell quote only when an exit or rung looks due.
        try:
            estimates = self.ex.position_estimates([p for p in positions if not p.get("pending_exit")])
        except Exception:
            estimates = {}
        for pos in list(positions):
            if pos.get("pending_exit") or pos not in positions:
                continue
            est = estimates.get(pos["mint"])
            if est is not None:
                pos["peak_usd"] = max(float(pos["peak_usd"]), est)
                pos["last_value_usd"] = est
                if not self.ex.exit_due_at(pos, est, False):
                    continue
            if self._quotes_used >= self.cfg.quote_budget:
                continue
            self._quotes_used += 1
            try:
                quote = self.ex.jup.quote(pos["mint"], WSOL, int(pos["tokens"]), slippage_bps=self.ex.cfg.sell_slippage_bps)
                current = int(quote["outAmount"]) / LAMPORTS * sol_price
            except Exception as exc:
                pos["quote_failures"] = int(pos.get("quote_failures", 0)) + 1
                pos["last_quote_error"] = self.mod.describe_error(exc)
                if pos["quote_failures"] >= 20 and self.now() - pos["opened_ts"] > self.ex.cfg.copy_time_stop_minutes * 60:
                    self.shadow_close(pos, 0.0, "unpriceable", None)
                continue
            pos["quote_failures"] = 0
            pos["peak_usd"] = max(float(pos["peak_usd"]), current)
            pos["last_value_usd"] = current
            xcfg = self.ex.exit_cfg(pos)
            rung = self.shadow_ladder(pos, current)
            if rung:
                continue
            reason = self.mod.decide_exit(float(pos["position_usd"]), current, float(pos["opened_ts"]), self.now(), xcfg, float(pos["peak_usd"]))
            if reason:
                self.shadow_exit(pos, 1.0, reason, sol_price, current)

    def shadow_ladder(self, pos: dict[str, Any], current_usd: float) -> bool:
        rungs = pos.get("ladder") or []
        entry_tokens, entry_basis, tokens_now = int(pos["entry_tokens"]), float(pos["entry_basis_usd"]), int(pos["tokens"])
        if not rungs or entry_tokens <= 0 or entry_basis <= 0 or tokens_now <= 0:
            return False
        multiple = (current_usd / tokens_now) / (entry_basis / entry_tokens)
        pending = [r for r in rungs if not r.get("done")]
        if not pending or multiple < pending[0]["x"]:
            return False
        crossed = [r for r in pending if multiple >= r["x"]]
        share_tokens = sum(entry_tokens * r["pct"] / 100 for r in crossed)
        for r in crossed:
            r["done"] = True
        last = crossed[-1] is rungs[-1]
        runner = float(getattr(self.ex.cfg, "copy_runner_trail", 0.0) or 0.0) > 0   # same rule as the live ladder
        if (last and not runner) or share_tokens >= tokens_now * 0.98:
            self.shadow_exit(pos, 1.0, f"ladder_{crossed[-1]['x']:g}x", None, current_usd)
        else:
            self.shadow_exit(pos, share_tokens / tokens_now, f"ladder_{crossed[-1]['x']:g}x", None, current_usd)
        return True

    def shadow_exit(self, pos: dict[str, Any], fraction: float, reason: str, sol_price: float | None, current_usd: float | None = None) -> None:
        """Sell `fraction` of the shadow stack at the executable quote (fetched here when the
        caller has none). The whole position closes on fraction 1.0."""
        if current_usd is None:
            if self._quotes_used >= self.cfg.quote_budget:
                pos["pending_exit"] = {"fraction": fraction, "reason": reason}
                return
            self._quotes_used += 1
            try:
                quote = self.ex.jup.quote(pos["mint"], WSOL, int(pos["tokens"]), slippage_bps=self.ex.cfg.sell_slippage_bps)
                current_usd = int(quote["outAmount"]) / LAMPORTS * (sol_price or self.ex.sol_price_usd())
            except Exception as exc:
                pos["pending_exit"] = {"fraction": fraction, "reason": reason, "why": self.mod.describe_error(exc)}
                return
        pos.pop("pending_exit", None)
        if fraction >= 0.999:
            self.shadow_close(pos, current_usd, reason, sol_price)
            return
        proceeds = current_usd * fraction - self.cfg.fee_usd
        sold_tokens = int(int(pos["tokens"]) * fraction)
        pos["tokens"] = int(pos["tokens"]) - sold_tokens
        pos["realized_usd"] += proceeds
        pos["realized_stress_usd"] += proceeds * self.stress_ratio(pos)
        pos["position_usd"] = round(float(pos["position_usd"]) * (1.0 - fraction), 4)
        pos["peak_usd"] = float(pos["peak_usd"]) * (1.0 - fraction)
        pos["last_value_usd"] = current_usd * (1.0 - fraction)
        pos.setdefault("partials", []).append({"ts": self.now(), "reason": reason, "fraction": round(fraction, 4), "proceeds_usd": round(proceeds, 4)})
        self.log(f"{pos['wallet'][:8]} {pos['mint']}: shadow {reason} sold {fraction:.0%} for ${proceeds:.2f}")

    def stress_ratio(self, pos: dict[str, Any]) -> float:
        ts = pos.get("tokens_stress")
        return (ts / pos["entry_tokens"]) if ts and pos.get("entry_tokens") else 1.0

    def shadow_close(self, pos: dict[str, Any], current_usd: float, reason: str, sol_price: float | None) -> None:
        cfg = self.ex.cfg
        keep = cfg.moon_bag if (cfg.moon_bag > 0 and current_usd > float(pos["position_usd"]) and current_usd * cfg.moon_bag >= cfg.min_moon_bag_usd) else 0.0
        proceeds = current_usd * (1.0 - keep) - (self.cfg.fee_usd if current_usd > 0 else 0.0)
        total = pos["realized_usd"] + proceeds
        stress_known = pos.get("tokens_stress") is not None
        total_stress = pos["realized_stress_usd"] + proceeds * self.stress_ratio(pos)
        trade = {"wallet": pos["wallet"], "mint": pos["mint"], "signature": pos["signature"], "source_usd": pos["source_usd"],
                 "opened_ts": pos["opened_ts"], "closed_ts": self.now(), "latency_s": pos.get("latency_s"), "cost_usd": pos["cost_usd"],
                 "proceeds_base_usd": round(total, 4), "pnl_base": round(total - pos["cost_usd"], 4),
                 "proceeds_stress_usd": round(total_stress, 4) if stress_known else None,
                 "pnl_stress": round(total_stress - pos["cost_usd"], 4) if stress_known else None,
                 "return_base": round((total - pos["cost_usd"]) / pos["cost_usd"], 4) if pos["cost_usd"] else 0.0,
                 "reason": reason, "moon_bag_written_off": round(current_usd * keep, 4), "portfolio_admitted": pos.get("portfolio_admitted", False),
                 "entry_price_impact_pct": pos.get("entry_price_impact_pct"), "entry_round_trip_pct": pos.get("entry_round_trip_pct")}
        if pos.get("kind") == "convergence":
            trade.update({"kind": "convergence", "wallets": pos.get("wallets"), "independent": pos.get("independent"),
                          "spread_s": pos.get("spread_s"), "pair_overlap": pos.get("pair_overlap"), "exit_wallet": pos.get("exit_wallet")})
        self.st["trades"].append(trade)
        del self.st["trades"][:-5000]
        self.st["positions"].remove(pos)
        stamped = {**trade, "opened_at": self.mod.utc_iso(trade["opened_ts"]), "closed_at": self.mod.utc_iso(trade["closed_ts"])}
        if pos.get("kind") == "convergence":
            self._csv("scout_convergence_trades.csv",
                      ["opened_at", "closed_at", "mint", "wallets", "independent", "spread_s", "pair_overlap", "source_usd", "cost_usd",
                       "proceeds_base_usd", "pnl_base", "proceeds_stress_usd", "pnl_stress", "reason", "exit_wallet", "latency_s"],
                      {**stamped, "wallets": " ".join(pos.get("wallets") or [])})
        else:
            self._csv("scout_shadow_trades.csv", ["opened_at", "closed_at", "wallet", "mint", "source_usd", "cost_usd", "proceeds_base_usd", "pnl_base",
                                                  "proceeds_stress_usd", "pnl_stress", "reason", "portfolio_admitted", "latency_s", "signature"],
                      stamped)
        self.log(f"{pos['wallet'][:8]} {pos['mint']}: shadow {reason} ${total:.2f} on ${pos['cost_usd']:.2f} "
                 f"({trade['return_base']:+.1%}{', stress ' + format(trade['pnl_stress'], '+.2f') if stress_known else ''})")

    _quotes_used = 0

    def reset_quote_budget(self) -> None:
        self._quotes_used = 0

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATES}
        for c in self.st["candidates"].values():
            out[c.get("state", "discovered")] = out.get(c.get("state", "discovered"), 0) + 1
        return out

    def heartbeat(self) -> str:
        """One token for the executor heartbeat line."""
        if not self.enabled:
            return "off" if self.cfg.mode == "off" else "no-key"
        n = self.counts()
        age = self.now() - float(self.st.get("discovered_ts") or 0) if self.st.get("discovered_ts") else None
        conv_open = sum(1 for p in self.st["positions"] if p.get("kind") == "convergence")
        conv_closed = sum(1 for t in self.st["trades"] if t.get("kind") == "convergence" and t.get("closed_ts"))
        return (f"{self.cfg.mode}{'+live' if self.cfg.live else ''}:cand={len(self.st['candidates'])},shadow={n['shadow']},"
                f"qualified={n['qualified']},live={n['live']},paused={n['paused']},rejected={n['rejected']},"
                f"shadow_pos={len(self.st['positions'])},shadow_trades={len(self.st['trades'])},"
                f"conv={len(self.st.get('convergence_events') or [])}/{conv_open}/{conv_closed},"
                f"watch={len(self.watchlist())},live_set={len(self.configured_live()) + n['live']}/{self.cfg.live_copy_max},"
                f"overlap={len((self.st.get('overlap') or {}).get('wallets') or [])},"
                f"discovery={'never' if age is None else f'{age / 3600:.1f}h'}")

    def startup_line(self) -> str:
        c = self.cfg
        if not self.enabled:
            why = "SCOUT_MODE=off" if c.mode == "off" else "GMGN_API_KEY not set"
            return f"wallet scouting off ({why})"
        return (f"wallet scouting mode={c.mode} track={'fast' if c.fast_track else 'full'} live_promotion={'ON' if c.live else 'off'} elite_only={'on' if c.elite_only else 'off'} "
                f"discovery={c.discovery_hours:.0f}h refresh={c.refresh_hours:.0f}h max_live={c.max_live} live_size={c.live_size:.0%} "
                f"max_open_scouted={c.max_open_positions} "
                f"live_loss_budget=${c.live_loss_budget_usd:,.0f}{' (promotion refused until set)' if c.live_loss_budget_usd <= 0 else ''} "
                f"shadow>={c.shadow_min_days}d/{c.shadow_min_trades}trades/{c.shadow_min_tokens}tokens PF>={c.shadow_min_profit_factor} "
                f"DD<={c.shadow_max_drawdown:.0%} history>={c.min_history_days}d PF>={c.min_profit_factor} "
                f"tracked={len(self.st['candidates'])} policy={policy_key(c)} | funnel: watchlist<={c.watchlist_max} "
                f"({c.watch_poll_wallets}/poll every {c.poll_seconds:.0f}s) live_set<={c.live_copy_max} "
                f"confluence K={c.convergence_min_wallets} in {c.convergence_window_seconds:.0f}s (buys>=${c.confluence_min_source_usd:.0f}) "
                f"promote={c.promote_min_paper_fills} green paper fills demote={c.demote_after_live_fills} red fills or {c.demote_idle_days:.0f}d idle "
                f"hold>={c.min_median_hold_minutes:.0f}m sniper<{c.sniper_median_hold_seconds:.0f}s rejected "
                f"watch_only={','.join(c.copy_watch_only) or 'none'} overlap={'on' if c.overlap else 'off'}")

    # ---- report --------------------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        return {**candidate_report(self.st, self.cfg, self.now()),
                "funnel": funnel_report(self.st, self.cfg, self.now(), list(self.ex.cfg.copy_wallets), self.ex.state.get("jev"))}


def candidate_report(st: dict[str, Any], cfg: ScoutConfig, now: float) -> dict[str, Any]:
    rows = []
    for address, c in (st.get("candidates") or {}).items():
        ev = c.get("evaluation") or {}
        stats = c.get("stats") or {}
        m30 = (c.get("history") or {}).get("metrics_30d") or {}
        sh = c.get("shadow") or {}
        rows.append({
            "address": address, "state": c.get("state"), "state_since": c.get("state_since"),
            "discovery_sources": [s["source"] for s in c.get("sources", [])],
            "leaderboard": leaderboard_status(c.get("rank_snapshots") or [], cfg, now),
            "verified_exposure": c.get("exposure"),
            "history_coverage": {k: (c.get("history") or {}).get(k) for k in ("coverage_days", "events", "truncated", "unmatched_sells")},
            "net_pnl": {w: (stats.get(w) or {}).get("realized_profit") for w in ("7d", "30d", "all")},
            "closed_episodes_30d": m30.get("closed_episodes"), "distinct_tokens_30d": m30.get("distinct_tokens"),
            "profit_factor_30d": m30.get("profit_factor"),
            "win_rate": {"token_level_30d": (stats.get("30d") or {}).get("winrate"), "episode_level_30d": m30.get("episode_win_rate"),
                         "episodes": m30.get("closed_episodes")},
            "drawdown": {k: m30.get(k) for k in ("drawdown_usd", "drawdown_fraction", "drawdown_basis")},
            "best_token_profit_concentration": m30.get("best_token_share"),
            "first_buy_only_results": {k: m30.get(k) for k in ("qualifying_first_buys", "profit_needing_adds_share", "net_without_best_token_usd")},
            "hold_time_distribution": {k: m30.get(k) for k in ("median_hold_minutes", "fast_exit_fraction")},
            "relationship": c.get("relationship"), "risk_flags": c.get("risk_flags"),
            "shadow_sample_size": {k: sh.get(k) for k in ("trades", "distinct_tokens", "active_days", "calendar_days")},
            "shadow_net_pnl": {"baseline": sh.get("net_base_usd"), "stress": sh.get("net_stress_usd"), "stress_coverage": sh.get("stress_coverage")},
            "shadow_expectancy_confidence_interval": {k: sh.get(k) for k in ("bootstrap_lower_bound", "bootstrap_lower_bound_token_clusters", "bootstrap_lower_bound_day_clusters")},
            "combined_portfolio_results": {k: sh.get(k) for k in ("portfolio_admitted_trades", "portfolio_admitted_net_usd", "missed")},
            "score_components": ev.get("score"), "missing_evidence": ev.get("missing"),
            "qualification_or_rejection_reasons": {"failed": ev.get("failed"), "gates": ev.get("gates"),
                                                   "last_transition": (c.get("lifecycle") or [{}])[-1]},
            "last_successful_refresh": c.get("last_refresh"), "refresh_error": c.get("refresh_error"),
            "policy_version": ev.get("policy_version"),
            "route_to_live": c.get("route") or None, "paper": c.get("paper"), "overlap_hits": c.get("overlap_hits"),
        })
    order = {s: i for i, s in enumerate(("live", "qualified", "shadow", "research", "discovered", "paused", "rejected"))}
    rows.sort(key=lambda r: (order.get(r["state"], 9), -((r.get("score_components") or {}).get("total") or 0)))
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["state"]] += 1
    # Which filter keeps wallets out: every deciding gate that failed, counted over the
    # candidates not in the live set, plus what rejected the rejected ones.
    gate_failures: dict[str, int] = defaultdict(int)
    rejected_by: dict[str, int] = defaultdict(int)
    for c in (st.get("candidates") or {}).values():
        ev = c.get("evaluation") or {}
        if c.get("state") in ("live", "qualified"):
            continue
        for name in ev.get("failed") or []:
            gate_failures[name] += 1
        if c.get("state") == "rejected":
            gates = ev.get("gates") or {}
            name = next((n for n in ("tags", "risk_flags", "sniper") if (gates.get(n) or {}).get("status") == "fail"), "other")
            rejected_by[name] += 1
    return {"gate_failures": dict(sorted(gate_failures.items(), key=lambda kv: -kv[1])), "rejected_by": dict(rejected_by),
            "policy_version": policy_key(cfg), "mode": cfg.mode, "live_switch": cfg.live, "elite_only": cfg.elite_only,
            "track": "fast" if cfg.fast_track else "full", "live_loss_budget_usd": cfg.live_loss_budget_usd,
            "counts": dict(counts), "candidates": rows, "shadow_positions": st.get("positions", []),
            "convergence": convergence_report(st, cfg),
            "last_selection": st.get("last_selection"), "discovered_at": st.get("discovered_ts"), "errors": (st.get("errors") or [])[-10:],
            "token_sample": st.get("token_sample", []), "gmgn_units_last_cycle": st.get("last_units")}


def configured_wallets_from_env() -> list[str]:
    """COPY_WALLETS as the executor reads it (address, or address:min[:size]), for read-only
    reports that do not construct an executor."""
    out: list[str] = []
    for w in os.getenv("COPY_WALLETS", "").split(","):
        address = w.split(":")[0].strip()
        if address and address not in out:
            out.append(address)
    return out


def funnel_report(st: dict[str, Any], cfg: ScoutConfig, now: float, configured: list[str] | None = None,
                  jev: dict[str, Any] | None = None) -> dict[str, Any]:
    """The copy funnel at a glance: watchlist, live set, paper P&L by wallet, confluence
    today, runner-overlap candidates and the Jev gate's counts."""
    configured = list(configured if configured is not None else configured_wallets_from_env())
    cands = st.get("candidates") or {}
    book = st.get("live") or {}

    def watch_only(w: str) -> bool:
        return any(w == x or (len(x) < 32 and w.startswith(x)) for x in cfg.copy_watch_only)

    watch = [a for a, c in cands.items() if c.get("state") in ("shadow", "qualified", "live", "paused")]
    configured_rows = [{"wallet": w, "status": "watch_only" if watch_only(w) else "always_copied",
                        "our_fills": (book.get(w) or {}).get("fills", 0), "our_net_usd": (book.get(w) or {}).get("realized_pnl_usd", 0.0),
                        "last_trade_ts": (st.get("last_trade") or {}).get(w)} for w in configured]
    scouted_live = [{"wallet": a, "route": c.get("route"), "our_fills": (book.get(a) or {}).get("fills", 0),
                     "our_net_usd": (book.get(a) or {}).get("realized_pnl_usd", 0.0)}
                    for a, c in cands.items() if c.get("state") == "live"]
    paper: dict[str, dict[str, Any]] = {}
    for t in st.get("trades") or []:
        if t.get("kind") == "convergence" or not t.get("closed_ts") or str(t.get("reason") or "").startswith("missed"):
            continue
        row = paper.setdefault(t["wallet"], {"wallet": t["wallet"], "fills": 0, "net_base_usd": 0.0, "net_stress_usd": 0.0})
        row["fills"] += 1
        row["net_base_usd"] = round(row["net_base_usd"] + _f(t.get("pnl_base")), 4)
        row["net_stress_usd"] = round(row["net_stress_usd"] + _f(t.get("pnl_stress")), 4)
    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    events_today = [e for e in st.get("convergence_events") or []
                    if datetime.fromtimestamp(float(e.get("ts") or 0), timezone.utc).strftime("%Y-%m-%d") == today]
    return {
        "watchlist": {"size": min(len(watch), cfg.watchlist_max), "max": cfg.watchlist_max, "tracked": len(cands),
                      "poll_wallets_per_round": cfg.watch_poll_wallets, "poll_seconds": cfg.poll_seconds},
        "live_set": {"max": cfg.live_copy_max, "configured": configured_rows, "scouted": scouted_live,
                     "size": sum(1 for r in configured_rows if r["status"] == "always_copied") + len(scouted_live)},
        "promotion": {"paper_fills": cfg.promote_min_paper_fills, "demote_after_live_fills": cfg.demote_after_live_fills,
                      "demote_idle_days": cfg.demote_idle_days, "sniper_max_hold_sec": cfg.sniper_max_hold_seconds,
                      "sniper_median_hold_sec_rejects": cfg.sniper_median_hold_seconds},
        "paper_pnl_by_wallet": sorted(paper.values(), key=lambda r: -r["net_base_usd"])[:100],
        "confluence_today": {"events": len(events_today), "bought_live": sum(1 for e in events_today if e.get("live") == "bought"),
                             "k": cfg.convergence_min_wallets, "window_sec": cfg.convergence_window_seconds},
        "overlap_candidates": (st.get("overlap") or {}).get("wallets") or [],
        "overlap_runners": (st.get("overlap") or {}).get("runners") or [],
        "overlap_at": (st.get("overlap") or {}).get("ts"),
        "jev": jev or {},
    }


def convergence_report(st: dict[str, Any], cfg: ScoutConfig) -> dict[str, Any]:
    """The convergence signal on its own, beside single-wallet shadow copying: how often it
    fires, and what its closed shadow trades made at baseline and under stress."""
    events = st.get("convergence_events") or []
    closed = [t for t in (st.get("trades") or []) if t.get("kind") == "convergence" and t.get("closed_ts")]
    single = [t for t in (st.get("trades") or []) if t.get("kind") != "convergence" and t.get("closed_ts")]

    def summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
        pnl = [float(t.get("pnl_base") or 0.0) for t in trades]
        stress = [float(t["pnl_stress"]) for t in trades if t.get("pnl_stress") is not None]
        wins = [p for p in pnl if p > 0]
        losses = [-p for p in pnl if p < 0]
        return {"trades": len(trades), "net_base_usd": round(sum(pnl), 4), "net_stress_usd": round(sum(stress), 4) if stress else None,
                "win_rate": round(len(wins) / len(pnl), 3) if pnl else None,
                "avg_return": round(sum(float(t.get("return_base") or 0.0) for t in trades) / len(trades), 4) if trades else None,
                "profit_factor": profit_factor(sum(wins), sum(losses)) if pnl else None}

    return {"enabled": cfg.convergence, "min_wallets": cfg.convergence_min_wallets,
            "live": {"enabled": cfg.convergence_live, "size": cfg.convergence_live_size, "max_open": cfg.convergence_max_open,
                     "loss_budget_usd": cfg.convergence_loss_budget_usd,
                     "realized_pnl_usd": (st.get("live") or {}).get(CONVERGENCE, {}).get("realized_pnl_usd", 0.0),
                     "bought": sum(1 for e in events if e.get("live") == "bought")},
            "window_minutes": cfg.convergence_window_seconds / 60, "events": len(events),
            "open_positions": sum(1 for p in st.get("positions") or [] if p.get("kind") == "convergence"),
            "shadow": summary(closed), "single_wallet_shadow": summary(single),
            "recent_events": events[-20:], "recent_trades": closed[-20:]}


# ---- discovery + enrichment (runs on the worker thread, touches no executor state) --------------
GECKO_API = "https://api.geckoterminal.com/api/v2"
SNIPER_TAGS = ("sniper", "mev_bot", "sandwich_bot", "wash_trader")


def _iso_ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def fetch_runners(cfg: ScoutConfig, now: float, http_get: Any = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Coins that ran recently, from GeckoTerminal (keyless): Solana pools on the trending
    lists (1h, 6h, 24h) and the newest-pools pages, created at most
    SCOUT_OVERLAP_MAX_AGE_HOURS ago, with a market cap of at least SCOUT_OVERLAP_MIN_MCAP_USD
    (about 3x a pump.fun graduation). One row per base mint, largest cap first, at most
    SCOUT_OVERLAP_RUNNERS. Returns (runners, errors); a failed page is an error, not a stop."""
    pause = 0.0
    if http_get is None:
        import requests
        pause = cfg.gecko_pause_seconds

        def http_get(url: str) -> dict[str, Any]:
            resp = requests.get(url, headers={"Accept": "application/json"}, timeout=15)
            resp.raise_for_status()
            return resp.json()
    urls = [f"{GECKO_API}/networks/solana/trending_pools?duration={d}&page=1" for d in ("1h", "6h", "24h")]
    urls += [f"{GECKO_API}/networks/solana/new_pools?page={n}" for n in (1, 2, 3)]
    best: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for i, url in enumerate(urls):
        if pause and i:
            time.sleep(pause)
        rows = None
        for attempt in (0, 1):
            try:
                rows = (http_get(url) or {}).get("data") or []
                break
            except Exception as exc:
                if attempt == 0 and "429" in str(exc):
                    time.sleep(max(pause, 0.0) * 4)    # one polite retry after a rate limit
                    continue
                errors.append(f"geckoterminal {url.split('/networks/solana/')[-1]}: {exc}")
        if rows is None:
            continue
        for row in rows:
            attrs = row.get("attributes") or {}
            base = (((row.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
            mint = base.split("_", 1)[-1] if base else ""
            created = _iso_ts(attrs.get("pool_created_at"))
            mcap = _f(attrs.get("market_cap_usd") or attrs.get("fdv_usd"))
            if not mint or mint == WSOL or created is None:
                continue
            if now - created > cfg.overlap_max_age_hours * 3600 or mcap < cfg.overlap_min_mcap_usd:
                continue
            if mint not in best or mcap > best[mint]["mcap_usd"]:
                best[mint] = {"mint": mint, "mcap_usd": round(mcap), "pool_created_ts": created, "name": attrs.get("name"),
                              "dex": (((row.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id")}
    runners = sorted(best.values(), key=lambda r: -r["mcap_usd"])[: cfg.overlap_runners]
    return runners, errors


def _first_seconds(row: dict[str, Any]) -> float | None:
    """How many seconds after the token's creation this trader first bought, when GMGN's row
    carries both timestamps; None otherwise."""
    first = next((_f(row.get(k)) for k in ("start_holding_at", "first_buy_time", "first_buy_timestamp") if row.get(k)), 0.0)
    created = next((_f(row.get(k)) for k in ("token_created_at", "created_timestamp", "open_timestamp") if row.get(k)), 0.0)
    if first <= 0 or created <= 0:
        return None
    return first - created


def overlap_rank(runner_traders: dict[str, list[dict[str, Any]]], cfg: ScoutConfig) -> list[dict[str, Any]]:
    """Wallets ranked by how many runners they were a top trader of. Transfer-in inventory,
    GMGN sniper / MEV / wash tags (bundlers and insiders do count) and entries in the first SCOUT_OVERLAP_MIN_ENTRY_DELAY_SEC
    seconds of a token do not count: a style that repeats across runners is the signal, a
    block-0 fill is not copyable. Top SCOUT_OVERLAP_TOP."""
    import gmgn
    hits: dict[str, set[str]] = defaultdict(set)
    tags_of: dict[str, set[str]] = defaultdict(set)
    for mint, rows in runner_traders.items():
        for r in rows:
            address = gmgn.wallet_of(r)
            tags = set(gmgn._tags(r))
            if not address or r.get("transfer_in") or r.get("is_suspicious") or tags & set(SNIPER_TAGS):
                continue
            early = _first_seconds(r)
            if early is not None and early < cfg.overlap_min_entry_delay_seconds:
                continue
            hits[address].add(mint)
            tags_of[address] |= tags
    ranked = sorted(hits, key=lambda a: (-len(hits[a]), a))[: cfg.overlap_top]
    return [{"address": a, "hits": len(hits[a]), "runners": sorted(hits[a]), "tags": sorted(tags_of[a])} for a in ranked]


def discover_and_enrich(client: Any, cfg: ScoutConfig, known: dict[str, dict[str, Any]], configured: list[str],
                        token_sample: list[str], now: float, min_first_buy_usd: float, room: int | None = None) -> dict[str, Any]:
    """`room`: how many new wallets can still be tracked (None: no limit). Enriching a find that
    cannot be tracked would spend the budget on a wallet that is then dropped."""
    import gmgn
    units = 0
    errors: list[str] = []
    found: dict[str, dict[str, Any]] = defaultdict(lambda: {"sources": [], "rank_snapshots": []})

    def note(address: str, source: str, tags: list[str]) -> None:
        if not address or address in configured:
            return
        entry = found[address]
        if not any(s["source"] == source for s in entry["sources"]):
            entry["sources"].append({"source": source, "ts": now})
        if tags:
            entry.setdefault("feed_tags", [])
            entry["feed_tags"] = sorted(set(entry["feed_tags"]) | set(tags))

    for name, fn in (("smartmoney", client.smart_money), ("kol", client.kol)):
        try:
            rows = fn("sol", 100)
            units += 1
            for r in rows:
                note(gmgn.wallet_of(r), name, gmgn._tags(r) or list((r.get("maker_info") or {}).get("tags") or []))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            if rate_limited(exc):
                return {"candidates": dict(found), "token_sample": [], "units": units, "errors": errors, "rate_limited": True}
        time.sleep(client.pause_seconds)
    tokens = list(token_sample)
    try:
        trending = client.market_rank("sol", 50)
        units += 3
        tokens.extend(str(r.get("address")) for r in trending if r.get("address"))
    except Exception as exc:
        errors.append(f"market_rank: {exc}")
        if rate_limited(exc):
            return {"candidates": dict(found), "token_sample": [], "units": units, "errors": errors, "rate_limited": True}
    seen_tokens: list[str] = []
    for t in tokens:
        if t and t not in seen_tokens:
            seen_tokens.append(t)
    rotation = int(now // (cfg.discovery_hours * 3600))
    if len(seen_tokens) > cfg.token_sample:
        start = (rotation * cfg.token_sample) % len(seen_tokens)
        seen_tokens = (seen_tokens[start:] + seen_tokens[:start])[: cfg.token_sample]
    for token in seen_tokens:
        if units >= cfg.gmgn_units_per_cycle:
            break
        try:
            time.sleep(client.pause_seconds)
            rows = client.top_traders("sol", token, limit=20)
            units += 5
        except Exception as exc:
            errors.append(f"top_traders {token[:8]}: {exc}")
            if rate_limited(exc):
                return {"candidates": dict(found), "token_sample": seen_tokens, "units": units, "errors": errors, "rate_limited": True}
            continue
        for rank, r in enumerate(rows, 1):
            address = gmgn.wallet_of(r)
            if r.get("transfer_in") or r.get("is_suspicious"):
                continue                                  # inventory that arrived by transfer is not a copyable buy
            note(address, f"top_traders:{token[:8]}", gmgn._tags(r))
            found[address]["rank_snapshots"].append({"ts": now, "scope": "token", "list": f"top_traders:{token[:8]}", "rank": rank, "population": len(rows)})
    overlap: list[dict[str, Any]] = []
    runners: list[dict[str, Any]] = []
    if cfg.overlap:
        runners, gecko_errors = fetch_runners(cfg, now)
        errors.extend(gecko_errors)
        traders: dict[str, list[dict[str, Any]]] = {}
        for runner in runners:
            if units >= cfg.gmgn_units_per_cycle:
                break
            try:
                time.sleep(client.pause_seconds)
                traders[runner["mint"]] = client.top_traders("sol", runner["mint"], limit=cfg.overlap_traders_per_runner)
                units += 5
            except Exception as exc:
                errors.append(f"overlap top_traders {runner['mint'][:8]}: {exc}")
                if rate_limited(exc):
                    break
        overlap = overlap_rank(traders, cfg)
        for row in overlap:
            note(row["address"], f"overlap:{row['hits']}", row["tags"])
    # Enrichment order: configured wallets due a refresh, then the backlog (tracked, never
    # enriched), then this cycle's new finds that fit the free room, then the stalest. Backlog and stale are drawn from everything tracked,
    # not only from what the feeds surfaced this cycle: a wallet discovered once and never
    # enriched would otherwise wait for a feed to mention it again, which may never happen.
    def refreshed(address: str) -> float:
        return float((known.get(address) or {}).get("last_refresh") or 0)

    def worth_enriching(address: str) -> bool:
        return address not in configured and (known.get(address) or {}).get("state") != "rejected"

    due = [a for a in configured if now - refreshed(a) >= cfg.refresh_hours * 3600]
    # Runner-overlap finds jump the queue: a wallet that was a top trader of several runners
    # is the best lead a cycle produces.
    overlap_first = [r["address"] for r in overlap if r["address"] not in configured and not refreshed(r["address"])
                     and (known.get(r["address"]) or {}).get("state") != "rejected"]
    fresh_new = [a for a in found if a not in known and a not in overlap_first]
    if room is not None:
        fresh_new = fresh_new[:max(0, room)]
    backlog = sorted((a for a in known if worth_enriching(a) and not refreshed(a)),
                     key=lambda a: float((known[a].get("discovered_at") or 0)))
    stale = sorted((a for a in known if worth_enriching(a) and refreshed(a)
                    and now - refreshed(a) >= cfg.refresh_hours * 3600), key=refreshed)
    queue, seen_queue = [], set()
    if room is not None:
        overlap_first = [a for a in overlap_first if a in known] + [a for a in overlap_first if a not in known][:max(0, room)]
        fresh_new = fresh_new[:max(0, room - len([a for a in overlap_first if a not in known]))]
    for address in due + overlap_first + backlog + fresh_new + stale:
        if address and address not in seen_queue:
            seen_queue.add(address)
            queue.append(address)
    enriched = screened = 0
    for address in queue:
        if enriched >= cfg.enrich_per_cycle or units >= cfg.gmgn_units_per_cycle:
            break
        entry = found[address] if address in found else found.setdefault(address, {"sources": [], "rank_snapshots": []})
        if address in configured and not any(s["source"] == "configured" for s in entry["sources"]):
            entry["sources"].append({"source": "configured", "ts": now})
        passed_before = ((known.get(address) or {}).get("screen") or {}).get("ok") is True
        if address not in configured and not refreshed(address) and not passed_before and cfg.screen_per_cycle > 0:
            # A wallet never looked at gets the cheap screen first; only a pass earns the
            # full history pull.
            if screened >= cfg.screen_per_cycle:
                continue
            try:
                verdict, used = screen_wallet(client, cfg, address)
                units += used
                screened += 1
            except Exception as exc:
                entry["refresh_error"] = str(exc)
                errors.append(f"screen {address[:8]}: {exc}")
                if rate_limited(exc):
                    break
                continue
            entry["screen"] = {"ok": verdict["ok"], "why": verdict["why"], "ts": now}
            if not verdict["ok"]:
                entry["stats"] = {"30d": verdict["stats_30d"]}
                entry["last_refresh"] = now
                entry["refresh_error"] = None
                continue
        try:
            enrich, used = enrich_wallet(client, cfg, address, now, min_first_buy_usd, cfg.gmgn_units_per_cycle - units)
            units += used
            entry["rank_snapshots"] = entry.get("rank_snapshots", []) + enrich.pop("rank_snapshots", [])   # token lists + global ranks
            entry.update(enrich)
            entry["last_refresh"] = now
            entry["refresh_error"] = None
            enriched += 1
        except Exception as exc:
            entry["refresh_error"] = str(exc)
            errors.append(f"enrich {address[:8]}: {exc}")
            if rate_limited(exc):
                break
    for address, entry in found.items():
        entry.setdefault("tags", sorted(set(entry.get("feed_tags") or []) | set(entry.get("tags") or [])))
        entry.pop("feed_tags", None)
    return {"candidates": dict(found), "token_sample": seen_tokens, "units": units, "errors": errors,
            "overlap": overlap, "runners": runners, "screened": screened, "enriched": enriched,
            "rate_limited": any(rate_limited(Exception(e)) for e in errors)}


def stat_row(row: dict[str, Any]) -> dict[str, Any]:
    """One wallet_stats row reduced to what the gates read, plus GMGN's per-token return
    buckets: `jackpot_rate` is the share of traded tokens that returned 2x or more."""
    import gmgn
    stat = row.get("pnl_stat") if isinstance(row.get("pnl_stat"), dict) else {}
    tokens = int(gmgn._num(stat, "token_num"))
    x2_5 = int(gmgn._num(stat, "pnl_2x_5x_num"))
    x5 = int(gmgn._num(stat, "pnl_gt_5x_num"))
    return {"realized_profit": round(gmgn._num(row, "realized_profit"), 2), "pnl": round(gmgn._num(row, "realized_profit_pnl"), 4),
            "buys": int(gmgn._num(row, "buy")), "sells": int(gmgn._num(row, "sell")), "bought_cost": round(gmgn._num(row, "bought_cost"), 2),
            "sold_income": round(gmgn._num(row, "sold_income"), 2), "token_num": tokens,
            "winrate": round(gmgn._num(stat, "winrate"), 4), "avg_hold_hours": round(gmgn._num(stat, "avg_holding_period") / 3600, 1),
            "avg_hold_seconds": round(gmgn._num(stat, "avg_holding_period"), 1),
            "tokens_2x_5x": x2_5, "tokens_gt_5x": x5, "jackpot_rate": round((x2_5 + x5) / tokens, 4) if tokens else None,
            "last_timestamp": int(gmgn._num(row, "last_timestamp"))}


def screen_wallet(client: Any, cfg: ScoutConfig, address: str) -> tuple[dict[str, Any], int]:
    """The quick screen: one 30d wallet_stats call. ({"ok", "why", "stats_30d"}, units)."""
    time.sleep(client.pause_seconds)
    rows = client.wallet_stats("sol", [address], "30d")
    s30 = stat_row(rows[0] if rows else {})
    realized, hold = s30["realized_profit"], s30["avg_hold_seconds"]
    jackpot = "" if s30["jackpot_rate"] is None else f", {s30['jackpot_rate']:.0%} of tokens 2x+"
    if realized < cfg.screen_min_profit_usd:
        why = f"quick screen: realized {realized:+,.0f} USD in 30d (need {cfg.screen_min_profit_usd:,.0f}){jackpot}"
        return {"ok": False, "why": why, "stats_30d": s30}, 3
    if 0 < hold < cfg.sniper_median_hold_seconds:
        why = f"quick screen: average hold {hold:.0f}s < {cfg.sniper_median_hold_seconds:.0f}s (sniper farm)"
        return {"ok": False, "why": why, "stats_30d": s30}, 3
    return {"ok": True, "why": f"quick screen: realized {realized:+,.0f} USD in 30d, avg hold {s30['avg_hold_hours']:.1f}h{jackpot}",
            "stats_30d": s30}, 3


def enrich_wallet(client: Any, cfg: ScoutConfig, address: str, now: float, min_first_buy_usd: float, unit_budget: int) -> tuple[dict[str, Any], int]:
    """All the evidence GMGN can give on one wallet, and the request units it cost."""
    import gmgn
    units = 0
    out: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    profile: dict[str, Any] = {}
    for period in ("7d", "30d", "all"):
        time.sleep(client.pause_seconds)
        rows = client.wallet_stats("sol", [address], period)
        units += 3
        row = rows[0] if rows else {}
        stats[period] = stat_row(row)
        common = row.get("common") if isinstance(row.get("common"), dict) else {}
        if common and not profile:
            profile = {"tags": [str(t) for t in (common.get("tags") or [])], "tag_rank": common.get("tag_rank") or {},
                       "twitter_username": common.get("twitter_username") or "", "is_blue_verified": bool(common.get("is_blue_verified")),
                       "followers_count": int(gmgn._num(common, "followers_count", "twitter_fans_num")), "created_at": common.get("created_at"),
                       "fund_from_address": common.get("fund_from_address") or "", "created_token_count": int(gmgn._num(common, "created_token_count"))}
    out["stats"] = stats
    out["profile"] = profile
    out["tags"] = list(profile.get("tags") or [])
    known_exposure = bool(profile)
    out["exposure"] = {"known": known_exposure, "followers": profile.get("followers_count", 0), "verified": profile.get("is_blue_verified", False),
                       "twitter": profile.get("twitter_username", ""), "tags": [t for t in profile.get("tags", []) if t in ("kol", "top_followed", "renowned")],
                       "provenance": "gmgn wallet_stats.common", "ts": now}
    snapshots = []
    for name, rank in (profile.get("tag_rank") or {}).items():
        if int(rank or 0) > 0:
            snapshots.append({"ts": now, "scope": "global", "list": f"gmgn:{name}", "rank": int(rank), "population": 0})
    out["rank_snapshots"] = snapshots
    # Holdings: transferred-in inventory and open losses. GMGN serves this endpoint only with
    # its signed ("critical") auth, which a read-only key cannot do; then holdings are recorded
    # as unavailable, the open-inventory gate stays `missing`, and enrichment carries on.
    holdings: list[dict[str, Any]] | None = None
    holdings_error = ""
    if not getattr(client, "holdings_unavailable", False):
        time.sleep(client.pause_seconds)
        try:
            holdings = client.wallet_holdings("sol", address, limit=100)
        except Exception as exc:
            holdings_error = str(exc)
            if "signature" in holdings_error.lower():
                client.holdings_unavailable = True          # every wallet would fail the same way this cycle
        units += 2
    else:
        holdings_error = "GMGN wallet_holdings needs signed auth (GMGN_PRIVATE_KEY); skipped this cycle"
    transfer_tokens: set[str] = set()
    open_loss = 0.0
    transfer_cost = 0.0
    for h in holdings or []:
        token = (h.get("token") or {}).get("token_address") if isinstance(h.get("token"), dict) else h.get("token_address")
        tin = gmgn._num(h, "history_transfer_in_amount")
        bought = gmgn._num(h, "history_bought_amount")
        if token and tin > 0 and tin >= 0.1 * max(bought, 1e-9):
            transfer_tokens.add(str(token))
            transfer_cost += gmgn._num(h, "history_transfer_in_cost")
        unreal = gmgn._num(h, "unrealized_profit")
        if unreal < 0:
            open_loss += -unreal
    if holdings is None:
        out["holdings"] = None
        out["holdings_error"] = holdings_error
    else:
        out["holdings"] = {"tokens": len(holdings), "transfer_in_tokens": len(transfer_tokens), "transfer_in_cost_usd": round(transfer_cost, 2),
                           "open_loss_usd": round(open_loss, 2)}
    # Activity: page back until the history window is covered or the page cap is hit.
    events: list[dict[str, Any]] = []
    cursor = None
    pages = 0
    truncated = False
    since = now - cfg.min_history_days * DAY
    while pages < cfg.activity_pages:
        if units + 3 > unit_budget:
            truncated = True
            break
        time.sleep(client.pause_seconds)
        rows, cursor = client.wallet_activity("sol", address, 20, cursor)
        units += 3
        pages += 1
        for r in rows:
            token = r.get("token") if isinstance(r.get("token"), dict) else {}
            events.append({"timestamp": int(gmgn._num(r, "timestamp")), "event_type": r.get("event_type"), "token": {"address": token.get("address")},
                           "token_amount": gmgn._num(r, "token_amount"), "cost_usd": gmgn._num(r, "cost_usd"), "gas_usd": gmgn._num(r, "gas_usd")})
        if not rows or not cursor:
            break
        if min(e["timestamp"] for e in events) <= since:
            break
    else:
        truncated = True
    if cursor and pages >= cfg.activity_pages:
        truncated = True
    built = episodes_from_activity(events, 0.02)
    episodes = built["episodes"]
    m30 = history_metrics(episodes, now, 30, min_first_buy_usd, transfer_tokens, stats["30d"]["bought_cost"] or None)
    m7 = history_metrics(episodes, now, 7, min_first_buy_usd, transfer_tokens, stats["7d"]["bought_cost"] or None)
    earliest = min((e["timestamp"] for e in events), default=None)
    out["history"] = {"events": len(events), "pages": pages, "truncated": truncated,
                      "coverage_days": round((now - earliest) / DAY, 1) if earliest else 0.0,
                      "unmatched_sells": built["unmatched_sells"], "unmatched_sell_usd": built["unmatched_sell_usd"],
                      "metrics_30d": m30, "metrics_7d": m7,
                      "episodes": [{"token": e["token"], "opened_ts": e["opened_ts"], "closed_ts": e.get("closed_ts"), "pnl_usd": e.get("pnl_usd"),
                                    "first_buy_usd": round(e["first_buy_usd"], 2), "adds": e["adds"]} for e in episodes[-400:]]}
    flags: list[dict[str, Any]] = []
    bad = [t for t in out["tags"] if t in BAD_TAGS]
    if bad:
        flags.append({"flag": "gmgn tags " + ", ".join(bad), "severity": "hard", "confidence": "provider"})
    if transfer_tokens:
        flags.append({"flag": f"{len(transfer_tokens)} token(s) with transferred-in inventory (excluded from profitability)",
                      "severity": "soft", "confidence": "high"})
    if holdings is None:
        flags.append({"flag": "holdings unavailable (GMGN signed auth required): transferred-in inventory and open losses not verified",
                      "severity": "soft", "confidence": "provider"})
    if built["unmatched_sells"] > 0:
        flags.append({"flag": f"{built['unmatched_sells']} sell(s) of inventory with no observed buy", "severity": "soft", "confidence": "medium"})
    if profile.get("created_token_count", 0) > 0:
        flags.append({"flag": f"created {profile['created_token_count']} token(s); creator-linked trading possible", "severity": "soft", "confidence": "low"})
    buys30 = stats["30d"]["buys"]
    if buys30 > 0 and m30["qualifying_first_buys"] < 0.1 * buys30 and buys30 >= 100:
        flags.append({"flag": f"high turnover: {buys30} buys in 30d but only {m30['qualifying_first_buys']} qualifying first buys", "severity": "soft", "confidence": "high"})
    if m30.get("profit_needing_adds_share", 0) > 0.5:
        flags.append({"flag": f"{m30['profit_needing_adds_share']:.0%} of 30d profit came from episodes that needed adds our bot skips", "severity": "soft", "confidence": "high"})
    out["risk_flags"] = flags
    return out, units
