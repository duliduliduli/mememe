"""Thin, rate-limit-aware clients for the public data sources the research track uses.

Every client returns plain dicts and never raises on a missing record; callers decide
whether missing data is a reject reason. All HTTP goes through one session with
exponential backoff on 429 and 5xx responses."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import requests

from .config import MMConfig, QUOTE_MINTS, TOKEN_2022_PROGRAM, WSOL

BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)
MAX_CREDIBLE_IMPACT_PCT = 90.0  # ladder impacts above this are treated as unreadable quotes


class HttpError(RuntimeError):
    pass


class Http:
    def __init__(self, cfg: MMConfig) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "mememe-mm-research/1.0"
        self.calls = 0
        self.min_interval = {"lite-api.jup.ag": cfg.jupiter_min_interval}
        self._last_call: dict[str, float] = {}

    def _throttle(self, url: str) -> None:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        wait = self.min_interval.get(host, 0.0)
        if wait:
            elapsed = time.monotonic() - self._last_call.get(host, 0.0)
            if elapsed < wait:
                time.sleep(wait - elapsed)
        self._last_call[host] = time.monotonic()

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        last: Exception | None = None
        for attempt, delay in enumerate((0.0, *BACKOFF)):
            if delay:
                time.sleep(delay)
            try:
                self._throttle(url)
                self.calls += 1
                resp = self.session.get(url, params=params, timeout=self.cfg.request_timeout)
            except requests.RequestException as exc:
                last = exc
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last = HttpError(f"{resp.status_code} from {url}")
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise HttpError(f"{resp.status_code} from {url}: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise HttpError(f"non-JSON body from {url}") from exc
        raise HttpError(f"gave up on {url}: {last}")

    def post_json(self, url: str, payload: Any) -> Any:
        last: Exception | None = None
        for delay in (0.0, *BACKOFF):
            if delay:
                time.sleep(delay)
            try:
                self.calls += 1
                resp = self.session.post(url, json=payload, timeout=self.cfg.request_timeout)
            except requests.RequestException as exc:
                last = exc
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last = HttpError(f"{resp.status_code} from {url}")
                continue
            resp.raise_for_status()
            return resp.json()
        raise HttpError(f"gave up on {url}: {last}")


def parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Meteora:
    """Meteora DLMM data API (dlmm.datapi.meteora.ag)."""

    def __init__(self, http: Http, cfg: MMConfig) -> None:
        self.http = http
        self.base = cfg.meteora_base

    def pools(self, page: int = 1, limit: int = 50, query: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"page": page, "limit": limit}
        if query:
            params["query"] = query
        body = self.http.get_json(f"{self.base}/pools", params) or {}
        return list(body.get("data") or [])

    def pool(self, address: str) -> dict[str, Any] | None:
        return self.http.get_json(f"{self.base}/pools/{address}")

    def pools_for_mint(self, mint: str, symbol: str, quotes: frozenset[str]) -> list[dict[str, Any]]:
        """DLMM pools pairing `mint` with an accepted quote, best TVL first.

        The API only searches by name, so the symbol is the lookup key and the mint is the
        filter; a token whose symbol collides with a bigger one still resolves correctly."""
        rows = []
        for pool in self.pools(limit=50, query=symbol) if symbol else []:
            x, y = pool.get("token_x") or {}, pool.get("token_y") or {}
            pair = {x.get("address"), y.get("address")}
            if mint in pair and (pair & quotes) and not pool.get("is_blacklisted"):
                rows.append(pool)
        rows.sort(key=lambda p: float(p.get("tvl") or 0), reverse=True)
        return rows


class Jupiter:
    """Jupiter lite-api: token facts (v2), prices (v3) and executable quotes (swap v1)."""

    def __init__(self, http: Http, cfg: MMConfig) -> None:
        self.http = http
        self.base = cfg.jupiter_base
        self._token_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def top_traded(self, interval: str = "24h", limit: int = 100) -> list[dict[str, Any]]:
        return list(self.http.get_json(f"{self.base}/tokens/v2/toptraded/{interval}", {"limit": limit}) or [])

    def top_organic(self, interval: str = "24h", limit: int = 100) -> list[dict[str, Any]]:
        return list(self.http.get_json(f"{self.base}/tokens/v2/toporganicscore/{interval}", {"limit": limit}) or [])

    def token(self, mint: str, max_age: float = 60.0) -> dict[str, Any] | None:
        cached = self._token_cache.get(mint)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        rows = self.http.get_json(f"{self.base}/tokens/v2/search", {"query": mint}) or []
        for row in rows:
            if row.get("id") == mint:
                self._token_cache[mint] = (time.monotonic(), row)
                return row
        return None

    def prices(self, mints: list[str]) -> dict[str, float]:
        body = self.http.get_json(f"{self.base}/price/v3", {"ids": ",".join(mints)}) or {}
        out = {}
        for mint, row in body.items():
            price = (row or {}).get("usdPrice")
            if price:
                out[mint] = float(price)
        return out

    def sol_price(self) -> float | None:
        return self.prices([WSOL]).get(WSOL)

    def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int = 100) -> dict[str, Any] | None:
        try:
            return self.http.get_json(
                f"{self.base}/swap/v1/quote",
                {"inputMint": input_mint, "outputMint": output_mint, "amount": int(amount_raw),
                 "slippageBps": slippage_bps},
            )
        except HttpError:
            return None

    def exit_ladder(self, mint: str, decimals: int, price_usd: float, sizes_usd: list[float],
                    quote_mint: str = WSOL) -> list[dict[str, Any]]:
        """Executable sell quotes for `sizes_usd` of the token, each with realized impact.

        Impact is measured against the smallest quote's unit price, i.e. against the
        contemporaneous executable price rather than a displayed spot price."""
        if price_usd <= 0 or not sizes_usd:
            return []
        ladder = []
        reference: float | None = None
        for size in sorted(sizes_usd):
            amount_raw = int(size / price_usd * 10 ** decimals)
            if amount_raw <= 0:
                continue
            quote = self.quote(mint, quote_mint, amount_raw)
            if not quote or not quote.get("outAmount"):
                ladder.append({"size_usd": size, "impact_pct": None, "out_raw": None, "unit_out": None})
                continue
            unit = int(quote["outAmount"]) / amount_raw
            if reference is None:
                reference = unit
            impact = max(0.0, (1 - unit / reference) * 100) if reference else None
            if impact is not None and impact > MAX_CREDIBLE_IMPACT_PCT:
                # A route that returns almost nothing is a broken quote (stale route,
                # dust output), not a measurement: report it as unknown rather than
                # let it read as a 100% exit cost.
                impact = None
            ladder.append({"size_usd": size, "impact_pct": impact, "out_raw": int(quote["outAmount"]),
                           "unit_out": unit, "routes": [p.get("swapInfo", {}).get("label") for p in quote.get("routePlan", [])]})
        return ladder


def depth_at_impact(ladder: list[dict[str, Any]], impact_pct: float) -> float | None:
    """Largest ladder size whose measured impact stays at or under `impact_pct`, linearly
    interpolated to the first rung that exceeds it. None when nothing on the ladder is usable."""
    rows = [r for r in ladder if r.get("impact_pct") is not None]
    if not rows:
        return None
    best: float | None = None
    prev: dict[str, Any] | None = None
    for row in rows:
        if row["impact_pct"] <= impact_pct:
            best = row["size_usd"]
            prev = row
            continue
        if prev is not None and row["impact_pct"] > prev["impact_pct"]:
            frac = (impact_pct - prev["impact_pct"]) / (row["impact_pct"] - prev["impact_pct"])
            best = prev["size_usd"] + frac * (row["size_usd"] - prev["size_usd"])
        break
    return best


def impact_at_size(ladder: list[dict[str, Any]], size_usd: float) -> float | None:
    """Measured impact at `size_usd`, interpolated between rungs. An unreadable rung at or
    below the size makes the answer unknown rather than an extrapolation from smaller rungs."""
    if any(r.get("impact_pct") is None and r["size_usd"] <= size_usd for r in ladder):
        return None
    rows = [r for r in ladder if r.get("impact_pct") is not None]
    if not rows:
        return None
    prev = None
    for row in rows:
        if row["size_usd"] >= size_usd:
            if prev is None or row["size_usd"] == size_usd:
                return row["impact_pct"]
            frac = (size_usd - prev["size_usd"]) / (row["size_usd"] - prev["size_usd"])
            return prev["impact_pct"] + frac * (row["impact_pct"] - prev["impact_pct"])
        prev = row
    return rows[-1]["impact_pct"] * (size_usd / rows[-1]["size_usd"])  # extrapolate linearly


class DexScreener:
    def __init__(self, http: Http, cfg: MMConfig) -> None:
        self.http = http
        self.base = cfg.dexscreener_base

    def token_pairs(self, mint: str) -> list[dict[str, Any]]:
        rows = self.http.get_json(f"{self.base}/token-pairs/v1/solana/{mint}") or []
        return [r for r in rows if r.get("chainId") == "solana"]


class SolanaRpc:
    """Only the read calls the screener needs; rotates endpoints on failure."""

    def __init__(self, http: Http, cfg: MMConfig) -> None:
        self.http = http
        self.urls = list(cfg.rpc_urls)

    def call(self, method: str, params: list[Any]) -> Any:
        last: Exception | None = None
        for url in self.urls:
            try:
                body = self.http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            except Exception as exc:  # noqa: BLE001 - try the next endpoint
                last = exc
                continue
            if body.get("error"):
                last = HttpError(f"{method}: {body['error']}")
                continue
            return body.get("result")
        raise HttpError(f"{method} failed on every endpoint: {last}")

    def mint_info(self, mint: str) -> dict[str, Any] | None:
        result = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        value = (result or {}).get("value")
        if not value:
            return None
        parsed = ((value.get("data") or {}).get("parsed") or {})
        info = parsed.get("info") or {}
        extensions = {}
        for ext in info.get("extensions") or []:
            extensions[ext.get("extension")] = ext.get("state") or {}
        return {
            "program": value.get("owner"),
            "token_2022": value.get("owner") == TOKEN_2022_PROGRAM,
            "decimals": info.get("decimals"),
            "supply_raw": int(info.get("supply") or 0),
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "extensions": extensions,
        }

    def largest_holders(self, mint: str) -> list[dict[str, Any]] | None:
        """Best effort: several public endpoints refuse getTokenLargestAccounts."""
        try:
            result = self.call("getTokenLargestAccounts", [mint])
        except HttpError:
            return None
        return list((result or {}).get("value") or [])


def transfer_fee_bps(extensions: dict[str, Any]) -> int:
    state = extensions.get("transferFeeConfig") or {}
    newest = state.get("newerTransferFee") or state.get("olderTransferFee") or {}
    return int(newest.get("transferFeeBasisPoints") or 0)


class Sources:
    """One object bundling every client so callers and tests can swap it wholesale."""

    def __init__(self, cfg: MMConfig) -> None:
        self.cfg = cfg
        self.http = Http(cfg)
        self.meteora = Meteora(self.http, cfg)
        self.jupiter = Jupiter(self.http, cfg)
        self.dexscreener = DexScreener(self.http, cfg)
        self.rpc = SolanaRpc(self.http, cfg)


__all__ = [
    "DexScreener", "Http", "HttpError", "Jupiter", "Meteora", "SolanaRpc", "Sources",
    "QUOTE_MINTS", "depth_at_impact", "impact_at_size", "parse_iso", "transfer_fee_bps",
]
