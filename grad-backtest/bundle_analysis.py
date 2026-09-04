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


def ancestry_clusters(
    wallet_to_ancestors: dict[str, list[str]], cex: set[str] | None = None
) -> list[list[str]]:
    """Group holders that share any non-CEX ancestor within the inspected depth.

    A ring commonly funds each buyer from a different one-use wallet. Direct-funder
    clustering misses that shape; the shared parent one hop farther back does not.
    """
    cex = cex if cex is not None else CEX_FUNDERS
    ancestor_to_wallets: dict[str, list[str]] = {}
    for wallet, ancestors in wallet_to_ancestors.items():
        for ancestor in set(ancestors):
            if ancestor and ancestor not in cex:
                ancestor_to_wallets.setdefault(ancestor, []).append(wallet)

    parent = {wallet: wallet for wallet in wallet_to_ancestors}

    def find(wallet: str) -> str:
        while parent[wallet] != wallet:
            parent[wallet] = parent[parent[wallet]]
            wallet = parent[wallet]
        return wallet

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for wallets in ancestor_to_wallets.values():
        for wallet in wallets[1:]:
            union(wallets[0], wallet)
    groups: dict[str, list[str]] = {}
    for wallet in wallet_to_ancestors:
        groups.setdefault(find(wallet), []).append(wallet)
    return [group for group in groups.values() if len(group) >= 2]


def transfer_clusters(edges: list[tuple[str, str]], holder_wallets: set[str]) -> list[list[str]]:
    """Connected current holders linked by pre-entry wallet-to-wallet token transfers.

    Non-holder distributor wallets remain in the graph as connectors, while only
    current holders are returned and therefore counted toward supply concentration.
    """
    graph: dict[str, set[str]] = {}
    for sender, recipient in edges:
        if not sender or not recipient or sender == recipient:
            continue
        graph.setdefault(sender, set()).add(recipient)
        graph.setdefault(recipient, set()).add(sender)
    seen: set[str] = set()
    groups: list[list[str]] = []
    for holder in holder_wallets:
        if holder in seen:
            continue
        pending = [holder]
        component: set[str] = set()
        while pending:
            node = pending.pop()
            if node in component:
                continue
            component.add(node)
            pending.extend(graph.get(node, set()) - component)
        seen.update(component & holder_wallets)
        members = sorted(component & holder_wallets)
        if len(members) >= 2:
            groups.append(members)
    return groups


def coordinated_buy_pct(
    buys: list[dict[str, Any]],
    supply: float,
    window_slots: int = 12,
    min_wallets: int = 3,
) -> tuple[float, int | None, int]:
    """Largest multi-wallet acquisition burst in a short rolling slot window."""
    if supply <= 0 or not buys or window_slots < 0:
        return 0.0, None, 0
    slots = sorted({int(row["slot"]) for row in buys})
    best_pct, best_slot, best_n = 0.0, None, 0
    for start in slots:
        wallets: dict[str, float] = {}
        for row in buys:
            slot = int(row["slot"])
            if start <= slot <= start + window_slots:
                wallet = str(row["wallet"])
                wallets[wallet] = wallets.get(wallet, 0.0) + float(row["amount"])
        if len(wallets) < min_wallets:
            continue
        pct = sum(wallets.values()) / supply * 100
        if pct > best_pct:
            best_pct, best_slot, best_n = pct, start, len(wallets)
    return best_pct, best_slot, best_n


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
