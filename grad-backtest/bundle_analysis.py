"""Pure bundle / funding-cluster math shared by tests and the live executor."""
from __future__ import annotations

from typing import Any


CEX_FUNDERS: set[str] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "H8sMJSCQxfKiFTCfDR3DUMLZdecSNVf5zrJsjJukPghQ",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm",
    "5VCwKtCXgCJ6kit5FuuuY9k7X3oZwxnAk5Zts4NnC4y",
}


def cluster_wallets(wallet_to_funder: dict[str, str | None], cex: set[str] | None = None) -> list[list[str]]:
    """Union-find wallets that share a non-CEX origin funder."""
    cex = cex if cex is not None else CEX_FUNDERS
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for wallet, funder in wallet_to_funder.items():
        find(wallet)
        if funder and funder not in cex:
            union(wallet, funder)
    groups: dict[str, list[str]] = {}
    for wallet in wallet_to_funder:
        groups.setdefault(find(wallet), []).append(wallet)
    return [g for g in groups.values() if len(g) >= 2]


def cluster_supply_pct(
    clusters: list[list[str]],
    wallet_amounts: dict[str, float],
    supply: float,
) -> tuple[float, list[str]]:
    if supply <= 0:
        return 0.0, []
    best_pct = 0.0
    best: list[str] = []
    for group in clusters:
        held = sum(wallet_amounts.get(w, 0.0) for w in group)
        pct = held / supply * 100
        if pct > best_pct:
            best_pct = pct
            best = list(group)
    return best_pct, best


def related_holder_wallets(
    wallet_to_funder: dict[str, str | None],
    seed: str | None,
    cex: set[str] | None = None,
) -> list[str]:
    """Return holder wallets connected to ``seed`` through non-CEX funding edges."""
    if not seed:
        return []
    cex = cex if cex is not None else CEX_FUNDERS
    graph: dict[str, set[str]] = {}
    for wallet, funder in wallet_to_funder.items():
        graph.setdefault(wallet, set())
        if funder and funder not in cex:
            graph.setdefault(funder, set())
            graph[wallet].add(funder)
            graph[funder].add(wallet)
    seen = {seed}
    pending = [seed]
    while pending:
        node = pending.pop()
        for neighbour in graph.get(node, set()):
            if neighbour not in seen:
                seen.add(neighbour)
                pending.append(neighbour)
    return [wallet for wallet in wallet_to_funder if wallet in seen]


def wallets_supply_pct(wallets: list[str], wallet_amounts: dict[str, float], supply: float) -> float:
    if supply <= 0:
        return 0.0
    return sum(wallet_amounts.get(wallet, 0.0) for wallet in wallets) / supply * 100


def top_wallets_supply_pct(wallet_amounts: dict[str, float], supply: float, limit: int = 10) -> float:
    if supply <= 0 or limit <= 0:
        return 0.0
    held = sum(sorted(wallet_amounts.values(), reverse=True)[:limit])
    return held / supply * 100


def bundle_slot_pct(buys: list[dict[str, Any]], supply: float) -> tuple[float, int | None, int]:
    if supply <= 0 or not buys:
        return 0.0, None, 0
    by_slot: dict[int, dict[str, float]] = {}
    for row in buys:
        slot = int(row["slot"])
        by_slot.setdefault(slot, {})
        by_slot[slot][row["wallet"]] = by_slot[slot].get(row["wallet"], 0.0) + float(row["amount"])
    best_pct, best_slot, best_n = 0.0, None, 0
    for slot, wallets in by_slot.items():
        if len(wallets) < 2:
            continue
        pct = sum(wallets.values()) / supply * 100
        if pct > best_pct:
            best_pct, best_slot, best_n = pct, slot, len(wallets)
    return best_pct, best_slot, best_n


def early_buy_pct(buys: list[dict[str, Any]], supply: float, create_slot: int | None, extra_slots: int = 3) -> float:
    if supply <= 0 or not buys or create_slot is None:
        return 0.0
    cutoff = create_slot + extra_slots
    held = sum(float(b["amount"]) for b in buys if int(b["slot"]) <= cutoff)
    return held / supply * 100
