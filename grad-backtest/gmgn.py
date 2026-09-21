"""GMGN OpenAPI client for the read-only endpoints the bot uses: wallet statistics (to screen
the wallets we copy), a token's top traders (to find wallets worth copying) and the Smart
Money trade feed. Auth is the API key plus a timestamp and a fresh client id on every
request; the private key GMGN issues is only needed for trading and is never used here.

GMGN_API_KEY turns the integration on. Rate limits are per plan (Free 5/5); a 429 waits
for the reset once and then gives up, so a screening never blocks the trading loop."""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

import requests

HOST = "https://openapi.gmgn.ai"
USER_AGENT = "mememe-copy-lane/1.0"


class GmgnError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("GMGN_API_KEY", "").strip())


class Gmgn:
    def __init__(self, api_key: str | None = None, host: str = HOST, timeout: float = 20.0) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("GMGN_API_KEY", "")).strip()
        if not self.api_key:
            raise GmgnError("GMGN_API_KEY is not set")
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.pause_seconds = 0.7            # free tier: 5 units/s, wallet_stats weighs 3
        self.session = requests.Session()

    # ---- transport ------------------------------------------------------
    def request(self, method: str, path: str, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
        params: list[tuple[str, str]] = []
        for key, value in (query or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                params.extend((key, str(v)) for v in value)
            else:
                params.append((key, str(value)))
        headers = {"X-APIKEY": self.api_key, "Content-Type": "application/json", "User-Agent": USER_AGENT}
        for attempt in (1, 2):
            params_now = params + [("timestamp", str(int(time.time()))), ("client_id", str(uuid.uuid4()))]
            resp = self.session.request(method, f"{self.host}{path}", params=params_now, json=body, headers=headers, timeout=self.timeout)
            try:
                data = resp.json()
            except ValueError as exc:
                raise GmgnError(f"{method} {path}: HTTP {resp.status_code} (non-JSON response)") from exc
            if data.get("code") == 0:
                return data.get("data")
            message = str(data.get("message") or data.get("error") or data)
            if resp.status_code == 429 and attempt == 1:
                reset = resp.headers.get("x-ratelimit-reset")
                wait = 2.0
                try:
                    wait = min(30.0, max(1.0, float(reset) - time.time() + 1.0)) if reset else 2.0
                except ValueError:
                    pass
                time.sleep(wait)
                continue
            upgrade = data.get("upgrade_url")
            raise GmgnError(f"{method} {path}: {message}" + (f" (upgrade: {upgrade})" if upgrade else ""))
        raise GmgnError(f"{method} {path}: rate limited")

    # ---- endpoints ---------------------------------------------------------
    def wallet_stats(self, chain: str, wallets: list[str], period: str = "30d") -> list[dict[str, Any]]:
        """Per-wallet realized profit, win rate, trade counts, tokens traded and average hold
        over 7d or 30d. The endpoint answers for one wallet at a time (extra addresses are
        ignored), so this asks once per wallet, spaced for the free tier's rate limit."""
        rows: list[dict[str, Any]] = []
        for i, wallet in enumerate(wallets):
            if i:
                time.sleep(self.pause_seconds)
            data = self.request("GET", "/v1/user/wallet_stats", {"chain": chain, "wallet_address": wallet, "period": period})
            for row in _rows(data):
                row.setdefault("wallet_address", wallet)
                rows.append(row)
        return rows

    def wallet_profits(self, chain: str, wallets: list[str], period: str = "30d") -> list[dict[str, Any]]:
        data = self.request("POST", "/v1/user/wallet_profits", {}, {"chain": chain, "period": period, "wallet_addresses": list(wallets)})
        return _rows(data)

    def top_traders(self, chain: str, token: str, tag: str | None = None, order_by: str = "profit",
                    direction: str = "desc", limit: int = 20) -> list[dict[str, Any]]:
        """The wallets that made the most on a token, with GMGN's tags (smart_degen, renowned,
        sniper, bundler, rat_trader...). `tag` narrows to one kind."""
        query: dict[str, Any] = {"chain": chain, "address": token, "order_by": order_by, "direction": direction, "limit": limit}
        if tag:
            query["tag"] = tag
        return _rows(self.request("GET", "/v1/market/token_top_traders", query))

    def smart_money(self, chain: str = "sol", limit: int = 100) -> list[dict[str, Any]]:
        """Recent trades by GMGN's smart-money wallets (newest first)."""
        return _rows(self.request("GET", "/v1/user/smartmoney", {"chain": chain, "limit": limit}))


def _rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("list", "rows", "items", "data"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
        return [data]
    return []


def wallet_of(row: dict[str, Any]) -> str:
    for key in ("wallet_address", "address", "wallet", "maker"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    info = row.get("maker_info") or {}
    return str(info.get("address") or "")


def _num(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _tags(row: dict[str, Any]) -> list[str]:
    tags = row.get("tags")
    if not tags:
        tags = (row.get("common") or {}).get("tags") if isinstance(row.get("common"), dict) else None
    return [str(t) for t in tags] if isinstance(tags, list) else []


# ---- screening --------------------------------------------------------------
SPAM_BUYS_PER_DAY = 50        # more buys a day than this is a quick-buy-button spammer
THIN_TRADES = 5               # fewer buys than this and the record says nothing


def screen_rows(rows: list[dict[str, Any]], period_days: int = 30) -> list[dict[str, Any]]:
    """Turn wallet_stats rows into a verdict per wallet: copy / skip / thin, with the reason."""
    out = []
    for row in rows:
        stat = row.get("pnl_stat") if isinstance(row.get("pnl_stat"), dict) else {}
        realized = _num(row, "realized_profit", "realized_pnl_usd")
        winrate = _num(stat, "winrate") or _num(row, "winrate", "win_rate")
        buys = int(_num(row, "buy", "buy_count", "history_total_buys"))
        sells = int(_num(row, "sell", "sell_count", "history_total_sells"))
        pnl = _num(row, "realized_profit_pnl", "pnl", "realized_pnl", "total_profit_pnl")
        cost = _num(row, "bought_cost", "total_cost", "cost")
        tokens = int(_num(stat, "token_num"))
        hold_hours = _num(stat, "avg_holding_period") / 3600.0
        tags = _tags(row)
        per_day = buys / max(1, period_days)
        if buys < THIN_TRADES:
            verdict, why = "thin", f"only {buys} buys in {period_days}d; no read"
        elif realized < 0:
            verdict, why = "skip", f"lost ${-realized:,.0f} in {period_days}d"
        elif per_day > SPAM_BUYS_PER_DAY:
            verdict, why = "skip", f"{per_day:.0f} buys a day; quick-buy spam, adds not first buys"
        elif any(t in ("wash_trader", "sandwich_bot", "mev_bot", "bundler", "rat_trader") for t in tags):
            verdict, why = "skip", "tagged " + ", ".join(t for t in tags if t in ("wash_trader", "sandwich_bot", "mev_bot", "bundler", "rat_trader"))
        else:
            verdict, why = "copy", (f"+${realized:,.0f} realized ({pnl:+.0%}), {winrate:.0%} of tokens won, "
                                    f"{buys} buys / {sells} sells over {tokens} tokens, avg hold {hold_hours:.0f}h")
        out.append({"wallet": wallet_of(row), "verdict": verdict, "why": why, "realized_profit": round(realized, 2),
                    "pnl": round(pnl, 4), "winrate": round(winrate, 4), "buys": buys, "sells": sells, "tokens": tokens,
                    "hold_hours": round(hold_hours, 1), "total_cost": round(cost, 2), "tags": tags, "period_days": period_days})
    return out


def screen(client: Gmgn, wallets: list[str], chain: str = "sol", period: str = "30d") -> list[dict[str, Any]]:
    days = 7 if period == "7d" else 30
    rows = client.wallet_stats(chain, wallets, period)
    by_wallet = {wallet_of(r): r for r in rows}
    ordered = [by_wallet[w] for w in wallets if w in by_wallet] + [r for w, r in by_wallet.items() if w not in wallets]
    results = screen_rows(ordered, days)
    seen = {r["wallet"] for r in results}
    for w in wallets:
        if w not in seen:
            results.append({"wallet": w, "verdict": "unknown", "why": "GMGN returned nothing for this wallet", "tags": [], "period_days": days})
    return results


def screen_lines(results: list[dict[str, Any]]) -> list[str]:
    marks = {"copy": "OK  ", "skip": "SKIP", "thin": "THIN", "unknown": "??  "}
    return [f"GMGN {marks.get(r['verdict'], '    ')} {r['wallet']}: {r['why']}" + (f" [{', '.join(r['tags'])}]" if r.get("tags") else "")
            for r in results]


def traders_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fields that matter when picking wallets from a token's top traders."""
    out = []
    for row in rows:
        out.append({"wallet": wallet_of(row), "realized_profit": round(_num(row, "realized_profit", "profit"), 2),
                    "profit_change": round(_num(row, "profit_change", "realized_pnl"), 4),
                    "bought_usd": round(_num(row, "history_bought_cost", "buy_volume_cur"), 2),
                    "tags": _tags(row) + [str(t) for t in (row.get("maker_token_tags") or [])],
                    "name": row.get("name") or row.get("twitter_username") or ""})
    return out
