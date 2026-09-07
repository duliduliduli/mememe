"""Token + pool screener: builds the established-meme universe and logs every reject reason.

The unit of selection is token + pool + execution route + inventory size, so a candidate
carries the best pool, the DLMM pool (when one exists), the executable exit ladder and the
diagnostics the specification asks for. A reject is a row in mm_rejects.csv, never silence."""
from __future__ import annotations

import csv
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import EXCLUDED_SYMBOLS, EXCLUDED_TAGS, QUOTE_MINTS, MMConfig
from .sources import Sources, depth_at_impact, impact_at_size, parse_iso, transfer_fee_bps

UNIVERSE_COLUMNS = [
    "screened_at", "mint", "symbol", "name", "token_program", "age_days", "market_cap_usd", "price_usd",
    "total_liquidity_usd", "best_pool", "best_pool_dex", "best_pool_quote", "best_pool_liquidity_usd",
    "quote_reserve_usd", "dlmm_pool", "dlmm_tvl_usd", "dlmm_bin_step", "dlmm_base_fee_pct",
    "dlmm_dynamic_fee_pct", "dlmm_protocol_fee_pct", "dlmm_fee_tvl_24h", "dlmm_volume_24h",
    "volume_24h_usd", "volume_to_tvl", "liquidity_to_market_cap", "traders_24h", "organic_buyers_24h",
    "organic_score", "flow_imbalance_24h", "top_holders_pct", "dev_balance_pct", "holder_count",
    "impact_at_max_position_pct", "depth_routine_usd", "depth_emergency_usd", "warnings",
]
REJECT_COLUMNS = ["screened_at", "mint", "symbol", "reason", "stage", "value", "limit"]


@dataclass
class Candidate:
    mint: str
    symbol: str
    name: str = ""
    decimals: int = 6
    token_program: str = ""
    age_days: float | None = None
    market_cap_usd: float | None = None
    price_usd: float | None = None
    total_liquidity_usd: float = 0.0
    best_pool: str = ""
    best_pool_dex: str = ""
    best_pool_quote: str = ""
    best_pool_liquidity_usd: float = 0.0
    quote_reserve_usd: float = 0.0
    dlmm_pool: str = ""
    dlmm_tvl_usd: float = 0.0
    dlmm_bin_step: int = 0
    dlmm_base_fee_pct: float = 0.0
    dlmm_dynamic_fee_pct: float = 0.0
    dlmm_protocol_fee_pct: float = 0.0
    dlmm_fee_tvl_24h: float = 0.0
    dlmm_volume_24h: float = 0.0
    volume_24h_usd: float = 0.0
    volume_to_tvl: float | None = None
    liquidity_to_market_cap: float | None = None
    traders_24h: int = 0
    organic_buyers_24h: int = 0
    organic_score: float = 0.0
    flow_imbalance_24h: float | None = None
    top_holders_pct: float | None = None
    dev_balance_pct: float | None = None
    holder_count: int = 0
    impact_at_max_position_pct: float | None = None
    depth_routine_usd: float | None = None
    depth_emergency_usd: float | None = None
    ladder: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejects: list[tuple[str, str, Any, Any]] = field(default_factory=list)  # (stage, reason, value, limit)

    @property
    def accepted(self) -> bool:
        return not self.rejects

    def reject(self, stage: str, reason: str, value: Any = None, limit: Any = None) -> None:
        self.rejects.append((stage, reason, value, limit))

    def row(self, screened_at: str) -> dict[str, Any]:
        data = asdict(self)
        data.pop("ladder"); data.pop("rejects"); data.pop("decimals")
        data["warnings"] = "; ".join(self.warnings)
        data["screened_at"] = screened_at
        return {k: data.get(k) for k in UNIVERSE_COLUMNS}


def utc_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def append_rows(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def write_rows(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class Screener:
    def __init__(self, cfg: MMConfig, sources: Sources) -> None:
        self.cfg = cfg
        self.src = sources

    # -- universe -------------------------------------------------------------
    def seed_universe(self) -> list[dict[str, Any]]:
        """Candidate tokens from Jupiter's traded and organic leaderboards, de-duplicated,
        majors and stables removed. Screening decides everything else."""
        seen: dict[str, dict[str, Any]] = {}
        for rows in (self.src.jupiter.top_traded("24h", self.cfg.universe_size * 2),
                     self.src.jupiter.top_organic("24h", self.cfg.universe_size * 2)):
            for row in rows:
                mint = row.get("id")
                if not mint or mint in seen or mint in QUOTE_MINTS:
                    continue
                symbol = (row.get("symbol") or "").upper()
                tags = {t.lower() for t in row.get("tags") or []}
                if symbol in EXCLUDED_SYMBOLS or tags & EXCLUDED_TAGS:
                    continue
                seen[mint] = row
        return list(seen.values())[: self.cfg.universe_size]

    # -- per-token screening ------------------------------------------------------
    def screen_token(self, token: dict[str, Any], now: float | None = None) -> Candidate:
        cfg = self.cfg
        now = now or time.time()
        cand = Candidate(mint=token["id"], symbol=token.get("symbol") or "", name=token.get("name") or "",
                         decimals=int(token.get("decimals") or 6), token_program=token.get("tokenProgram") or "")
        cand.price_usd = float(token.get("usdPrice") or 0) or None
        cand.market_cap_usd = float(token.get("mcap") or token.get("fdv") or 0) or None
        cand.holder_count = int(token.get("holderCount") or 0)
        cand.organic_score = float(token.get("organicScore") or 0)
        stats = token.get("stats24h") or {}
        cand.traders_24h = int(stats.get("numTraders") or 0)
        cand.organic_buyers_24h = int(stats.get("numOrganicBuyers") or 0)
        buy, sell = float(stats.get("buyVolume") or 0), float(stats.get("sellVolume") or 0)
        cand.volume_24h_usd = buy + sell
        cand.flow_imbalance_24h = abs(buy - sell) / (buy + sell) if buy + sell > 0 else None
        audit = token.get("audit") or {}
        cand.top_holders_pct = audit.get("topHoldersPercentage")
        cand.dev_balance_pct = audit.get("devBalancePercentage")

        # Age: first pool creation from Jupiter, oldest DexScreener pair as a fallback.
        created = parse_iso((token.get("firstPool") or {}).get("createdAt"))
        pairs = self.src.dexscreener.token_pairs(cand.mint)
        pair_created = [p.get("pairCreatedAt") for p in pairs if p.get("pairCreatedAt")]
        if pair_created:
            oldest = min(pair_created) / 1000
            created = min(created, oldest) if created else oldest
        cand.age_days = (now - created) / 86400 if created else None
        if cand.age_days is None:
            cand.reject("age", "token age unknown")
        elif cand.age_days < cfg.min_token_age_days:
            cand.reject("age", f"token age {cand.age_days:.1f}d < {cfg.min_token_age_days:.0f}d",
                        round(cand.age_days, 1), cfg.min_token_age_days)

        # Liquidity and best pool (any DEX) against an accepted quote.
        quote_pairs = [p for p in pairs if (p.get("quoteToken") or {}).get("address") in cfg.quotes
                       and (p.get("baseToken") or {}).get("address") == cand.mint]
        cand.total_liquidity_usd = sum(float((p.get("liquidity") or {}).get("usd") or 0) for p in quote_pairs)
        if quote_pairs:
            best = max(quote_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            liq = best.get("liquidity") or {}
            cand.best_pool = best.get("pairAddress") or ""
            cand.best_pool_dex = best.get("dexId") or ""
            cand.best_pool_quote = (best.get("quoteToken") or {}).get("symbol") or ""
            cand.best_pool_liquidity_usd = float(liq.get("usd") or 0)
            quote_units = float(liq.get("quote") or 0)
            quote_price = float(best.get("priceUsd") or 0) / float(best.get("priceNative") or 1) if best.get("priceNative") else 0
            cand.quote_reserve_usd = quote_units * quote_price if quote_price else cand.best_pool_liquidity_usd / 2
            if not cand.price_usd:
                cand.price_usd = float(best.get("priceUsd") or 0) or None
        else:
            cand.reject("liquidity", "no pool against an accepted quote")
        if cand.best_pool_liquidity_usd < cfg.min_pool_liquidity_usd:
            cand.reject("liquidity", f"best pool liquidity ${cand.best_pool_liquidity_usd:,.0f} < ${cfg.min_pool_liquidity_usd:,.0f}",
                        round(cand.best_pool_liquidity_usd), cfg.min_pool_liquidity_usd)
        elif cand.best_pool_liquidity_usd < cfg.preferred_pool_liquidity_usd:
            cand.warnings.append("liquidity below preferred band")
        if cand.market_cap_usd and cand.total_liquidity_usd:
            cand.liquidity_to_market_cap = cand.total_liquidity_usd / cand.market_cap_usd
        if cand.total_liquidity_usd:
            cand.volume_to_tvl = cand.volume_24h_usd / cand.total_liquidity_usd

        # Meteora DLMM pool for the LP strategies.
        dlmm = self.src.meteora.pools_for_mint(cand.mint, cand.symbol, cfg.quotes)
        if dlmm:
            pool = dlmm[0]
            config = pool.get("pool_config") or {}
            cand.dlmm_pool = pool.get("address") or ""
            cand.dlmm_tvl_usd = float(pool.get("tvl") or 0)
            cand.dlmm_bin_step = int(config.get("bin_step") or 0)
            cand.dlmm_base_fee_pct = float(config.get("base_fee_pct") or 0)
            cand.dlmm_dynamic_fee_pct = float(pool.get("dynamic_fee_pct") or 0)
            cand.dlmm_protocol_fee_pct = float(config.get("protocol_fee_pct") or 0)
            cand.dlmm_fee_tvl_24h = float((pool.get("fee_tvl_ratio") or {}).get("24h") or 0)
            cand.dlmm_volume_24h = float((pool.get("volume") or {}).get("24h") or 0)
            if cand.dlmm_tvl_usd < cfg.min_pool_liquidity_usd:
                cand.warnings.append(f"DLMM pool TVL ${cand.dlmm_tvl_usd:,.0f} below minimum; LP strategies ineligible")
        elif cfg.require_dlmm_pool:
            cand.reject("pool", "no Meteora DLMM pool against an accepted quote")
        else:
            cand.warnings.append("no DLMM pool; momentum-only candidate")

        # Token controls.
        info = None
        try:
            info = self.src.rpc.mint_info(cand.mint)
        except Exception as exc:  # noqa: BLE001
            cand.reject("authority", f"mint account unreadable: {exc}")
        if info:
            if not cand.token_program:
                cand.token_program = info.get("program") or ""
            if info.get("decimals") is not None:
                cand.decimals = int(info["decimals"])
            if info.get("mint_authority"):
                cand.reject("authority", "mint authority still active", info["mint_authority"])
            if info.get("freeze_authority"):
                cand.reject("authority", "freeze authority still active", info["freeze_authority"])
            ext = info.get("extensions") or {}
            if "permanentDelegate" in ext:
                cand.reject("authority", "Token-2022 permanent delegate", (ext["permanentDelegate"] or {}).get("delegate"))
            if "transferHook" in ext and (ext["transferHook"] or {}).get("programId"):
                cand.reject("authority", "Token-2022 transfer hook", ext["transferHook"].get("programId"))
            fee_bps = transfer_fee_bps(ext)
            if fee_bps > cfg.max_transfer_fee_bps:
                cand.reject("authority", f"Token-2022 transfer fee {fee_bps} bps > {cfg.max_transfer_fee_bps}", fee_bps,
                            cfg.max_transfer_fee_bps)
            if "confidentialTransferMint" in ext:
                cand.warnings.append("confidential transfer extension present")

        # Holder distribution (Jupiter audit; public RPCs refuse largest-account queries).
        if cand.top_holders_pct is None:
            cand.warnings.append("top-holder share unavailable")
        elif cand.top_holders_pct > cfg.max_top_holders_pct:
            cand.reject("holders", f"top holders {cand.top_holders_pct:.1f}% > {cfg.max_top_holders_pct:.0f}%",
                        round(cand.top_holders_pct, 1), cfg.max_top_holders_pct)
        if cand.dev_balance_pct is not None and cand.dev_balance_pct > cfg.max_dev_balance_pct:
            cand.reject("holders", f"dev balance {cand.dev_balance_pct:.1f}% > {cfg.max_dev_balance_pct:.0f}%",
                        round(cand.dev_balance_pct, 1), cfg.max_dev_balance_pct)

        # Participation and flow quality.
        if cand.traders_24h < cfg.min_traders_24h:
            cand.reject("participation", f"{cand.traders_24h} traders in 24h < {cfg.min_traders_24h}",
                        cand.traders_24h, cfg.min_traders_24h)
        if cand.organic_score < cfg.min_organic_score:
            cand.reject("participation", f"organic score {cand.organic_score:.0f} < {cfg.min_organic_score:.0f}",
                        round(cand.organic_score), cfg.min_organic_score)
        if cand.flow_imbalance_24h is not None and cand.flow_imbalance_24h > cfg.max_flow_imbalance:
            cand.reject("participation", f"24h flow imbalance {cand.flow_imbalance_24h:.2f} > {cfg.max_flow_imbalance:.2f}",
                        round(cand.flow_imbalance_24h, 2), cfg.max_flow_imbalance)
        if cand.organic_buyers_24h == 0:
            cand.warnings.append("no organic buyers in 24h")

        # Exit test on executable quotes: the full maximum inventory must be liquidatable
        # inside the routine band, and the emergency band must hold many multiples of it.
        if cand.price_usd and cand.accepted:
            sizes = [max(1.0, cfg.max_position_usd / 4), cfg.max_position_usd, cfg.max_position_usd * 5,
                     cfg.max_position_usd * cfg.exit_depth_multiple, cfg.max_position_usd * cfg.exit_depth_multiple * 5]
            cand.ladder = self.src.jupiter.exit_ladder(cand.mint, cand.decimals, cand.price_usd, sizes)
            cand.impact_at_max_position_pct = impact_at_size(cand.ladder, cfg.max_position_usd)
            cand.depth_routine_usd = depth_at_impact(cand.ladder, cfg.routine_exit_impact_pct)
            cand.depth_emergency_usd = depth_at_impact(cand.ladder, cfg.emergency_exit_impact_pct)
            if cand.impact_at_max_position_pct is None:
                cand.reject("exit", "no executable sell quote")
            elif cand.impact_at_max_position_pct > cfg.routine_exit_impact_pct:
                cand.reject("exit", f"impact {cand.impact_at_max_position_pct:.2f}% at ${cfg.max_position_usd:.0f} > routine {cfg.routine_exit_impact_pct:.2f}%",
                            round(cand.impact_at_max_position_pct, 3), cfg.routine_exit_impact_pct)
            needed = cfg.max_position_usd * cfg.exit_depth_multiple
            if cand.depth_emergency_usd is not None and cand.depth_emergency_usd < needed:
                cand.reject("exit", f"depth ${cand.depth_emergency_usd:,.0f} at {cfg.emergency_exit_impact_pct:.0f}% impact < ${needed:,.0f}",
                            round(cand.depth_emergency_usd), needed)
        return cand

    def run(self, write: bool = True) -> tuple[list[Candidate], list[Candidate]]:
        screened_at = utc_iso()
        accepted, rejected = [], []
        for token in self.seed_universe():
            try:
                cand = self.screen_token(token)
            except Exception as exc:  # noqa: BLE001 - one bad token must not stop the screen
                cand = Candidate(mint=token.get("id", "?"), symbol=token.get("symbol") or "")
                cand.reject("error", f"screen failed: {exc}")
            (accepted if cand.accepted else rejected).append(cand)
        if write:
            write_rows(self.cfg.universe_file, UNIVERSE_COLUMNS, [c.row(screened_at) for c in accepted])
            append_rows(self.cfg.rejects_file, REJECT_COLUMNS, [
                {"screened_at": screened_at, "mint": c.mint, "symbol": c.symbol, "reason": reason,
                 "stage": stage, "value": value, "limit": limit}
                for c in rejected for stage, reason, value, limit in c.rejects
            ])
        return accepted, rejected


def load_universe(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))
