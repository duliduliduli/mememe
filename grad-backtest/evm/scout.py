"""Wallet scout for an EVM chain (Robinhood Chain by default): find the wallets that made money
on the chain's recent runners, straight from on-chain transfer logs, and hand the best of them
to the copy lane.

Each cycle (EVM_SCOUT_CYCLE_MINUTES):
1. Runners: GeckoTerminal's trending and newest pools on the chain, created at most
   EVM_SCOUT_MAX_AGE_HOURS ago, market cap at least EVM_SCOUT_MIN_MCAP_USD, up at least
   EVM_SCOUT_MIN_GAIN_PCT over 24 h. One pool per token (the largest cap).
2. Each runner not scanned in the last EVM_SCOUT_RESCAN_HOURS: every ERC-20 Transfer of the
   token since its pool was created (eth_getLogs, split whenever the node's result cap is hit),
   priced with the pool's GeckoTerminal candles. Addresses that trade with many counterparties
   (pools, the V4 PoolManager, routers) are venues; tokens a wallet receives from a venue are a
   buy, tokens it sends to one are a sell. Per wallet: USD spent, USD realized on what it sold
   (average cost), minutes after launch it first bought, seconds it held before its first sell.
   Launchpad bonding curves are venues too, so their early buyers count.
3. Ranking across every runner scanned in the last EVM_SCOUT_KEEP_DAYS: runners traded, runners
   won, realized profit, median hold. A wallet qualifies with at least EVM_SCOUT_MIN_RUNNERS
   runners, EVM_SCOUT_MIN_WINS of them in profit and at least half won, EVM_SCOUT_MIN_REALIZED_USD
   realized in total, and a median hold of at least EVM_SCOUT_MIN_HOLD_SECONDS (a sniper we
   would only buy the exit of). Contracts (bots, routers) are dropped; plain and EIP-7702
   accounts stay. Bundlers and insiders are not filtered: getting in early with a consistently
   profitable one is the point.
4. The best EVM_SCOUT_LIVE_MAX qualified wallets are copied by the lane on this chain.

State and the full ranking are kept in DATA_DIR/evm_scout.json (served at /api/evm/scout).
"""
from __future__ import annotations

import bisect
import json
import os
import statistics
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from eth_utils import to_checksum_address

from .rpc import TRANSFER_TOPIC, Rpc, RpcError

GECKO_API = "https://api.geckoterminal.com/api/v2"
GECKO_NETWORK = {"robinhood": "robinhood", "base": "base", "bnb": "bsc"}
ZERO_TOPIC_ADDR = "0x" + "0" * 40
MAJORS_SKIP = {"WETH", "ETH", "USDG", "USDC", "USDT", "WBNB", "BNB"}


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ScoutConfig:
    def __init__(self) -> None:
        self.enabled = _env("EVM_SCOUT", "1") == "1"
        self.chain = _env("EVM_SCOUT_CHAIN", "robinhood").strip().lower()
        self.cycle_minutes = _f(_env("EVM_SCOUT_CYCLE_MINUTES", "60"), 60)
        self.min_mcap_usd = _f(_env("EVM_SCOUT_MIN_MCAP_USD", "150000"), 150000)
        self.min_gain_pct = _f(_env("EVM_SCOUT_MIN_GAIN_PCT", "200"), 200)
        self.max_age_hours = _f(_env("EVM_SCOUT_MAX_AGE_HOURS", "72"), 72)
        self.runners_per_cycle = int(_f(_env("EVM_SCOUT_RUNNERS", "12"), 12))
        self.rescan_hours = _f(_env("EVM_SCOUT_RESCAN_HOURS", "6"), 6)
        self.keep_days = _f(_env("EVM_SCOUT_KEEP_DAYS", "7"), 7)
        self.venue_degree = int(_f(_env("EVM_SCOUT_VENUE_DEGREE", "25"), 25))
        self.min_cost_usd = _f(_env("EVM_SCOUT_MIN_COST_USD", "50"), 50)          # a smaller position is noise
        self.max_logs_per_token = int(_f(_env("EVM_SCOUT_MAX_LOGS_PER_TOKEN", "300000"), 300000))
        self.min_runners = int(_f(_env("EVM_SCOUT_MIN_RUNNERS", "2"), 2))
        self.min_wins = int(_f(_env("EVM_SCOUT_MIN_WINS", "2"), 2))
        self.min_realized_usd = _f(_env("EVM_SCOUT_MIN_REALIZED_USD", "1000"), 1000)
        self.min_hold_seconds = _f(_env("EVM_SCOUT_MIN_HOLD_SECONDS", "60"), 60)
        self.live_max = int(_f(_env("EVM_SCOUT_LIVE_MAX", "10"), 10))
        # A scouted wallet's buys are mirrored from this size (their buys are smaller than the
        # Solana whales'); EVM_COPY_WALLETS keep COPY_MIN_BUY_USD or their own address:min.
        self.min_buy_usd = _f(_env("EVM_SCOUT_MIN_BUY_USD", "100"), 100)
        self.gecko_pause_seconds = _f(_env("EVM_SCOUT_GECKO_PAUSE_SECONDS", "2"), 2)

    def describe(self) -> str:
        if not self.enabled:
            return "off (EVM_SCOUT=0)"
        return (f"{self.chain}: runners <= {self.max_age_hours:.0f}h old, >= ${self.min_mcap_usd:,.0f} cap, >= +{self.min_gain_pct:.0f}% 24h; "
                f"qualify: {self.min_runners}+ runners, {self.min_wins}+ won, >= ${self.min_realized_usd:,.0f} realized, "
                f"median hold >= {self.min_hold_seconds:.0f}s; copy top {self.live_max} from ${self.min_buy_usd:,.0f} buys; "
                f"every {self.cycle_minutes:.0f}m")


def _default_http_get(url: str) -> dict[str, Any]:
    import requests
    for attempt in range(3):
        resp = requests.get(url, headers={"Accept": "application/json"}, timeout=20)
        if resp.status_code == 429 and attempt < 2:
            time.sleep(6 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    return {}


def _iso_ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def fetch_runners(cfg: ScoutConfig, now: float, http_get: Callable[[str], dict[str, Any]],
                  pause: float = 0.0) -> tuple[list[dict[str, Any]], list[str]]:
    """Recent runners on the scout chain, biggest cap first. Returns (runners, errors)."""
    network = GECKO_NETWORK.get(cfg.chain, cfg.chain)
    urls = [f"{GECKO_API}/networks/{network}/trending_pools?duration={d}&page=1" for d in ("1h", "6h", "24h")]
    urls += [f"{GECKO_API}/networks/{network}/new_pools?page={n}" for n in (1, 2)]
    best: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for i, url in enumerate(urls):
        if pause and i:
            time.sleep(pause)
        try:
            rows = (http_get(url) or {}).get("data") or []
        except Exception as exc:
            errors.append(f"geckoterminal {url.split('/networks/')[-1]}: {exc}")
            continue
        for row in rows:
            attrs = row.get("attributes") or {}
            rel = row.get("relationships") or {}
            base = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
            token = base.split("_", 1)[-1] if base else ""
            created = _iso_ts(attrs.get("pool_created_at"))
            mcap = _f(attrs.get("market_cap_usd")) or _f(attrs.get("fdv_usd"))
            gain = _f((attrs.get("price_change_percentage") or {}).get("h24"))
            symbol = str(attrs.get("name") or "").split("/")[0].strip()
            if not token.startswith("0x") or len(token) != 42 or token == ZERO_TOPIC_ADDR or created is None:
                continue
            if symbol.upper() in MAJORS_SKIP:
                continue
            if now - created > cfg.max_age_hours * 3600 or mcap < cfg.min_mcap_usd or gain < cfg.min_gain_pct:
                continue
            key = token.lower()
            if key not in best or mcap > best[key]["mcap_usd"]:
                best[key] = {"token": key, "pool": str(attrs.get("address") or "").lower(), "symbol": symbol,
                             "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"), "created_ts": created,
                             "mcap_usd": round(mcap), "gain_24h_pct": round(gain)}
    return sorted(best.values(), key=lambda r: -r["mcap_usd"])[: cfg.runners_per_cycle], errors


def fetch_logs(rpc: Rpc, token: str, start: int, end: int, cap: int, out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every Transfer log of `token` in [start, end], halving the range whenever the node
    refuses a query for returning too many logs. Stops adding past `cap` logs."""
    out = [] if out is None else out
    stack = [(start, end)]
    while stack and len(out) < cap:
        a, b = stack.pop()
        try:
            rows = rpc.logs(a, b, [TRANSFER_TOPIC], address=token)
        except RpcError as exc:
            if b > a and ("limit" in str(exc).lower() or "too many" in str(exc).lower() or "range" in str(exc).lower()):
                mid = (a + b) // 2
                stack.append((mid + 1, b))           # popped second: keeps block order
                stack.append((a, mid))
                continue
            raise
        out.extend(rows)
    return out


def price_at(candles: list[list[float]], ts: float) -> float:
    """Close of the last candle at or before `ts` (the first candle's close before it)."""
    if not candles:
        return 0.0
    i = bisect.bisect_right([c[0] for c in candles], ts) - 1
    return float(candles[max(i, 0)][4])


def wallet_rows(transfers: list[tuple[int, str, str, float]], ts_of: Callable[[int], float], candles: list[list[float]],
                venue_degree: int, min_cost_usd: float) -> dict[str, dict[str, Any]]:
    """Per-wallet results on one token from its transfers (block, from, to, tokens)."""
    peers: dict[str, set[str]] = defaultdict(set)
    for _, frm, to, _ in transfers:
        peers[frm].add(to)
        peers[to].add(frm)
    venues = {a for a, s in peers.items() if len(s) >= venue_degree}
    launch = ts_of(min(t[0] for t in transfers)) if transfers else 0.0
    acc: dict[str, dict[str, Any]] = defaultdict(lambda: {"bt": 0.0, "bu": 0.0, "st": 0.0, "su": 0.0, "fb": None, "fs": None, "buys": 0})
    for block, frm, to, amount in transfers:
        if frm == ZERO_TOPIC_ADDR or to == ZERO_TOPIC_ADDR or amount <= 0:
            continue
        t = ts_of(block)
        if frm in venues and to not in venues:
            x = acc[to]
            x["bt"] += amount
            x["bu"] += amount * price_at(candles, t)
            x["buys"] += 1
            x["fb"] = x["fb"] if x["fb"] is not None else t
        elif to in venues and frm not in venues:
            x = acc[frm]
            x["st"] += amount
            x["su"] += amount * price_at(candles, t)
            x["fs"] = x["fs"] if x["fs"] is not None else t
    rows: dict[str, dict[str, Any]] = {}
    for wallet, x in acc.items():
        if x["bt"] <= 0 or x["bu"] < min_cost_usd:
            continue
        sold = min(x["st"], x["bt"])
        cost_of_sold = x["bu"] * sold / x["bt"]
        rows[wallet] = {"cost_usd": round(x["bu"], 2), "sold_usd": round(x["su"], 2),
                        "realized_usd": round(x["su"] - cost_of_sold, 2) if sold > 0 else 0.0,
                        "sold_fraction": round(sold / x["bt"], 3), "buys": x["buys"],
                        "entry_minutes": round((x["fb"] - launch) / 60, 1),
                        "hold_seconds": round(x["fs"] - x["fb"]) if x["fs"] is not None and x["fs"] >= x["fb"] else None}
    return rows


def rank(tokens: dict[str, dict[str, Any]], cfg: ScoutConfig, kinds: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Wallets across every stored runner, qualified ones first, then by realized profit."""
    kinds = kinds or {}
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"runners": [], "realized_usd": 0.0, "cost_usd": 0.0, "wins": 0,
                                                          "holds": [], "entries": []})
    for token, info in tokens.items():
        for wallet, row in (info.get("rows") or {}).items():
            g = agg[wallet]
            g["runners"].append(info.get("symbol") or token[:8])
            g["realized_usd"] += _f(row.get("realized_usd"))
            g["cost_usd"] += _f(row.get("cost_usd"))
            g["wins"] += 1 if _f(row.get("realized_usd")) > 0 else 0
            if row.get("hold_seconds") is not None:
                g["holds"].append(float(row["hold_seconds"]))
            g["entries"].append(_f(row.get("entry_minutes")))
    out = []
    for wallet, g in agg.items():
        hits = len(g["runners"])
        hold = statistics.median(g["holds"]) if g["holds"] else None
        kind = kinds.get(wallet, "unknown")
        why = []
        if hits < cfg.min_runners:
            why.append(f"{hits} runner(s) < {cfg.min_runners}")
        if g["wins"] < cfg.min_wins or g["wins"] * 2 < hits:
            why.append(f"won {g['wins']} of {hits}")
        if g["realized_usd"] < cfg.min_realized_usd:
            why.append(f"realized ${g['realized_usd']:,.0f} < ${cfg.min_realized_usd:,.0f}")
        if hold is not None and hold < cfg.min_hold_seconds:
            why.append(f"median hold {hold:.0f}s < {cfg.min_hold_seconds:.0f}s (sniper)")
        if kind == "contract":
            why.append("contract (bot or router)")
        out.append({"wallet": wallet, "runners": hits, "wins": g["wins"], "realized_usd": round(g["realized_usd"], 2),
                    "cost_usd": round(g["cost_usd"], 2), "median_hold_seconds": None if hold is None else round(hold),
                    "median_entry_minutes": round(statistics.median(g["entries"]), 1) if g["entries"] else None,
                    "tokens": sorted(g["runners"]), "kind": kind, "qualified": not why, "why_not": "; ".join(why)})
    out.sort(key=lambda r: (not r["qualified"], -r["realized_usd"]))
    return out


class Scout:
    """Runs one cycle at a time on a worker thread; the lane reads `live_wallets()`."""

    def __init__(self, cfg: ScoutConfig, rpc: Rpc, state_path: Path, log: Callable[[str], None] = print,
                 http_get: Callable[[str], dict[str, Any]] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.rpc = rpc
        self.path = state_path
        self.log = log
        self.http_get = http_get or _default_http_get
        self.clock = clock
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        try:
            self.state: dict[str, Any] = json.loads(state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.state = {}
        for key, default in (("tokens", {}), ("kinds", {}), ("ranked", []), ("live", []), ("errors", [])):
            self.state.setdefault(key, default)

    # ---- the lane's view ------------------------------------------------------------------
    def live_wallets(self) -> list[str]:
        with self._lock:
            return list(self.state.get("live") or [])

    def maybe_run(self) -> None:
        if not self.cfg.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        if self.clock() - _f(self.state.get("last_cycle_ts")) < self.cfg.cycle_minutes * 60:
            return
        self.state["last_cycle_ts"] = self.clock()
        self._thread = threading.Thread(target=self._safe_cycle, name="evm-scout", daemon=True)
        self._thread.start()

    def _safe_cycle(self) -> None:
        try:
            self.run_cycle()
        except Exception as exc:
            self.log(f"SCOUT WARN cycle failed: {str(exc)[:240]}")
            self._error(f"cycle: {exc}")

    def _error(self, text: str) -> None:
        with self._lock:
            self.state["errors"] = (self.state.get("errors") or [])[-49:] + [{"ts": self.clock(), "error": text[:300]}]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with self._lock:
                tmp.write_text(json.dumps(self.state, separators=(",", ":")))
            tmp.replace(self.path)
        except OSError:
            pass

    # ---- one cycle -------------------------------------------------------------------------
    def block_clock(self) -> tuple[int, Callable[[int], float], float]:
        """The head block and a block -> unix time estimate from two real blocks."""
        head = self.rpc.block_number()
        old = max(1, head - 200_000)
        hb = self.rpc.call("eth_getBlockByNumber", [hex(head), False]) or {}
        ob = self.rpc.call("eth_getBlockByNumber", [hex(old), False]) or {}
        head_ts = int(hb.get("timestamp", "0x0"), 16) or self.clock()
        old_ts = int(ob.get("timestamp", "0x0"), 16) or head_ts - (head - old)
        spb = max(0.01, (head_ts - old_ts) / max(1, head - old))
        return head, (lambda b: head_ts - (head - b) * spb), spb

    def candles(self, pool: str, age_hours: float) -> list[list[float]]:
        network = GECKO_NETWORK.get(self.cfg.chain, self.cfg.chain)
        tf, agg = ("minute", 1) if age_hours <= 16 else (("minute", 5) if age_hours <= 80 else ("hour", 1))
        url = f"{GECKO_API}/networks/{network}/pools/{pool}/ohlcv/{tf}?aggregate={agg}&limit=1000&currency=usd&token=base"
        rows = (((self.http_get(url) or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        return sorted([[float(c[0]), 0, 0, 0, float(c[4])] for c in rows if len(c) >= 5])

    def scan(self, runner: dict[str, Any], head: int, ts_of: Callable[[int], float], spb: float) -> dict[str, dict[str, Any]]:
        token = runner["token"]
        now = self.clock()
        start = max(1, head - int((now - runner["created_ts"] + 600) / spb))
        logs = fetch_logs(self.rpc, token, start, head, self.cfg.max_logs_per_token)
        decimals = self.rpc.erc20_decimals(token)
        transfers = []
        for entry in logs:
            topics = entry.get("topics") or []
            if len(topics) < 3:
                continue
            try:
                amount = int(entry.get("data") or "0x0", 16) / 10 ** decimals
            except ValueError:
                continue
            transfers.append((int(entry["blockNumber"], 16), "0x" + topics[1][-40:].lower(), "0x" + topics[2][-40:].lower(), amount))
        time.sleep(self.cfg.gecko_pause_seconds)
        candles = self.candles(runner["pool"], (now - runner["created_ts"]) / 3600)
        rows = wallet_rows(transfers, ts_of, candles, self.cfg.venue_degree, self.cfg.min_cost_usd)
        self.log(f"SCOUT {runner['symbol']} ({token[:10]}, +{runner['gain_24h_pct']}% 24h, ${runner['mcap_usd']:,} cap): "
                 f"{len(logs)} transfers, {len(rows)} wallets with >= ${self.cfg.min_cost_usd:.0f} in")
        return rows

    def kind_of(self, wallet: str) -> str:
        kinds = self.state["kinds"]
        if wallet not in kinds:
            code = self.rpc.call("eth_getCode", [wallet, "latest"]) or "0x"
            kinds[wallet] = "eoa" if code in ("0x", "0x0") or code.lower().startswith("0xef0100") else "contract"
        return kinds[wallet]

    def run_cycle(self) -> dict[str, Any]:
        cfg, now = self.cfg, self.clock()
        runners, errors = fetch_runners(cfg, now, self.http_get, pause=cfg.gecko_pause_seconds if self.http_get is _default_http_get else 0.0)
        for e in errors:
            self._error(e)
        head, ts_of, spb = self.block_clock()
        tokens = self.state["tokens"]
        scanned = 0
        for runner in runners:
            prev = tokens.get(runner["token"])
            if prev and now - _f(prev.get("ts")) < cfg.rescan_hours * 3600:
                continue
            try:
                rows = self.scan(runner, head, ts_of, spb)
            except Exception as exc:
                self._error(f"scan {runner['symbol']}: {exc}")
                self.log(f"SCOUT WARN scan {runner['symbol']}: {str(exc)[:200]}")
                continue
            with self._lock:
                tokens[runner["token"]] = {**runner, "ts": now, "rows": rows}
            scanned += 1
        with self._lock:
            for token in [t for t, info in tokens.items() if now - _f(info.get("ts")) > cfg.keep_days * 86400]:
                tokens.pop(token, None)
        ranked = rank(tokens, cfg, self.state["kinds"])
        # Contract checks only where they decide something: wallets that pass every other gate.
        for row in ranked:
            if row["kind"] == "unknown" and row["qualified"]:
                try:
                    self.kind_of(row["wallet"])
                except Exception as exc:
                    self._error(f"getCode {row['wallet'][:10]}: {exc}")
        ranked = rank(tokens, cfg, self.state["kinds"])
        live = [to_checksum_address(r["wallet"]) for r in ranked if r["qualified"] and r["kind"] == "eoa"][: cfg.live_max]
        with self._lock:
            added = [w for w in live if w not in self.state.get("live", [])]
            dropped = [w for w in self.state.get("live", []) if w not in live]
            self.state["ranked"] = ranked[:300]
            self.state["live"] = live
            self.state["last_cycle"] = {"ts": now, "runners": [r["symbol"] for r in runners], "scanned": scanned,
                                        "tracked_runners": len(tokens), "wallets": len(ranked),
                                        "qualified": sum(1 for r in ranked if r["qualified"] and r["kind"] == "eoa")}
        self.save()
        self.log(f"SCOUT cycle: {len(runners)} runner(s) listed, {scanned} scanned, {len(tokens)} kept; "
                 f"{self.state['last_cycle']['qualified']} wallet(s) qualify; copying {len(live)}"
                 + (f"; added {', '.join(w[:10] for w in added)}" if added else "")
                 + (f"; dropped {', '.join(w[:10] for w in dropped)}" if dropped else ""))
        return self.state["last_cycle"]

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {"config": self.cfg.describe(), "live": list(self.state.get("live") or []),
                    "last_cycle": self.state.get("last_cycle"), "ranked": list(self.state.get("ranked") or [])[:100],
                    "runners": [{k: v for k, v in info.items() if k != "rows"} | {"wallets": len(info.get("rows") or {})}
                                for info in self.state.get("tokens", {}).values()],
                    "errors": list(self.state.get("errors") or [])[-10:]}
