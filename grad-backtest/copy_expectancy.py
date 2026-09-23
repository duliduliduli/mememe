"""Per-wallet expectancy of the copy lane from the files the executor already writes.

Joins live_trades.csv (our fills and exits) to copy_signals.csv (which followed wallet's
buy each fill mirrored) through the buy signature, and reads the scout's shadow and
convergence trades, so one call answers: after costs, which wallets are worth real money,
which are not, and whether the convergence signal beats single-wallet copying.

    python copy_expectancy.py            # prints the report for DATA_DIR
    GET /api/copy/expectancy             # the same as JSON from the dashboard
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def summarize(returns: list[float], pnls: list[float]) -> dict[str, Any]:
    """Expectancy of one group of closed trades: count, win rate, mean return, net P&L,
    profit factor and the mean winner and loser, so the break-even winner size is visible."""
    n = len(pnls)
    if n == 0:
        return {"trades": 0}
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    win_rate = len(wins) / n
    avg_win = sum(r for r in returns if r > 0) / len([r for r in returns if r > 0]) if any(r > 0 for r in returns) else 0.0
    avg_loss = sum(r for r in returns if r <= 0) / len([r for r in returns if r <= 0]) if any(r <= 0 for r in returns) else 0.0
    breakeven_win = (-avg_loss * (1 - win_rate) / win_rate) if win_rate > 0 else None
    return {
        "trades": n,
        "win_rate": round(win_rate, 3),
        "avg_return": round(sum(returns) / n, 4),
        "avg_winner_return": round(avg_win, 4),
        "avg_loser_return": round(avg_loss, 4),
        "net_pnl_usd": round(sum(pnls), 2),
        "expectancy_usd_per_trade": round(sum(pnls) / n, 4),
        "profit_factor": round(sum(wins) / sum(losses), 3) if losses else None,   # no losers yet: undefined, not infinite
        # The average winner the current win rate needs for the group to break even.
        "breakeven_avg_winner_return": round(breakeven_win, 4) if breakeven_win is not None else None,
    }


def expectancy(data_dir: Path) -> dict[str, Any]:
    trades = _rows(data_dir / "live_trades.csv")
    signals = _rows(data_dir / "copy_signals.csv")
    shadow = _rows(data_dir / "scout_shadow_trades.csv")
    convergence = _rows(data_dir / "scout_convergence_trades.csv")

    wallet_by_fill = {s["fill_signature"]: s["wallet"] for s in signals if s.get("fill_signature")}
    source_by_fill = {s["fill_signature"]: _f(s.get("source_usd")) for s in signals if s.get("fill_signature")}

    per_wallet: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"returns": [], "pnls": [], "source": []})
    unattributed = {"returns": [], "pnls": []}
    by_reason: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        cost, exit_usd = _f(t.get("position_usd")), _f(t.get("exit_usd"))
        ret = _f(t.get("net_return"), exit_usd / cost - 1.0 if cost else 0.0)
        pnl = exit_usd - cost
        by_reason[t.get("exit_reason") or "?"].append(pnl)
        wallet = wallet_by_fill.get(t.get("buy_signature") or "")
        if wallet:
            per_wallet[wallet]["returns"].append(ret)
            per_wallet[wallet]["pnls"].append(pnl)
            per_wallet[wallet]["source"].append(source_by_fill.get(t.get("buy_signature") or "", 0.0))
        else:
            unattributed["returns"].append(ret)
            unattributed["pnls"].append(pnl)

    live_rows = []
    for wallet, g in per_wallet.items():
        row = {"wallet": wallet, **summarize(g["returns"], g["pnls"])}
        row["avg_source_buy_usd"] = round(sum(g["source"]) / len(g["source"]), 0) if g["source"] else None
        blocked = [s for s in signals if s.get("wallet") == wallet and s.get("status") == "blocked"]
        row["signals_blocked"] = len(blocked)
        row["signals_mirrored"] = sum(1 for s in signals if s.get("wallet") == wallet and s.get("status") == "filled")
        live_rows.append(row)
    live_rows.sort(key=lambda r: r.get("net_pnl_usd") or 0.0)

    shadow_by_wallet: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"returns": [], "pnls": [], "stress": []})
    for t in shadow:
        if not t.get("closed_at"):
            continue
        cost, pnl = _f(t.get("cost_usd")), _f(t.get("pnl_base"))
        g = shadow_by_wallet[t.get("wallet") or "?"]
        g["returns"].append(pnl / cost if cost else 0.0)
        g["pnls"].append(pnl)
        if t.get("pnl_stress") not in (None, ""):
            g["stress"].append(_f(t.get("pnl_stress")))
    shadow_rows = []
    for wallet, g in shadow_by_wallet.items():
        row = {"wallet": wallet, **summarize(g["returns"], g["pnls"])}
        row["net_pnl_stress_usd"] = round(sum(g["stress"]), 2) if g["stress"] else None
        shadow_rows.append(row)
    shadow_rows.sort(key=lambda r: -(r.get("net_pnl_usd") or 0.0))

    conv_returns, conv_pnls, conv_stress = [], [], []
    for t in convergence:
        cost, pnl = _f(t.get("cost_usd")), _f(t.get("pnl_base"))
        conv_returns.append(pnl / cost if cost else 0.0)
        conv_pnls.append(pnl)
        if t.get("pnl_stress") not in (None, ""):
            conv_stress.append(_f(t.get("pnl_stress")))
    all_shadow_returns = [r for g in shadow_by_wallet.values() for r in g["returns"]]
    all_shadow_pnls = [p for g in shadow_by_wallet.values() for p in g["pnls"]]

    all_returns = [r for g in per_wallet.values() for r in g["returns"]] + unattributed["returns"]
    all_pnls = [p for g in per_wallet.values() for p in g["pnls"]] + unattributed["pnls"]
    return {
        "live": {"overall": summarize(all_returns, all_pnls), "by_wallet": live_rows,
                 "unattributed": summarize(unattributed["returns"], unattributed["pnls"]),
                 "by_exit_reason": {k: {"trades": len(v), "net_pnl_usd": round(sum(v), 2)} for k, v in sorted(by_reason.items())}},
        "shadow": {"overall": summarize(all_shadow_returns, all_shadow_pnls), "by_wallet": shadow_rows},
        "convergence": {**summarize(conv_returns, conv_pnls),
                        "net_pnl_stress_usd": round(sum(conv_stress), 2) if conv_stress else None},
        "files": {name: (data_dir / name).exists() for name in
                  ("live_trades.csv", "copy_signals.csv", "scout_shadow_trades.csv", "scout_convergence_trades.csv")},
    }


def main() -> None:
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    report = expectancy(data_dir)
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2, default=str))
        return
    live = report["live"]
    print(f"Live copy lane ({data_dir}): {live['overall']}")
    for row in live["by_wallet"]:
        print(f"  {row['wallet'][:8]}  trades={row['trades']} win={row.get('win_rate')} avg={row.get('avg_return')} "
              f"net=${row.get('net_pnl_usd')} PF={row.get('profit_factor')} needs_avg_winner={row.get('breakeven_avg_winner_return')}")
    if live["unattributed"].get("trades"):
        print(f"  (unattributed: {live['unattributed']})")
    print(f"Exit reasons: {live['by_exit_reason']}")
    print(f"Shadow (single wallet): {report['shadow']['overall']}")
    for row in report["shadow"]["by_wallet"][:15]:
        print(f"  {row['wallet'][:8]}  trades={row['trades']} win={row.get('win_rate')} net=${row.get('net_pnl_usd')} "
              f"stress=${row.get('net_pnl_stress_usd')} PF={row.get('profit_factor')}")
    print(f"Convergence: {report['convergence']}")


if __name__ == "__main__":
    main()
