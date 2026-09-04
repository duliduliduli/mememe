#!/usr/bin/env python3
"""Bankroll and position-sizing analysis for grad-backtest results.

Takes the per-trade net returns produced by `grad_backtest.py run`
(trade_results.csv, which already includes the proportional side costs)
and answers: given a starting balance and the *fixed* per-transaction
fees on Solana (base fee + priority fee/tip, which do NOT shrink with
position size), what fraction of the account should each trade use?

Method: bootstrap Monte Carlo over the empirical trade distribution for
a grid of account fractions, reporting median outcome, downside (5th
percentile), drawdown, and risk of ruin, then recommending the fraction
with the best median log growth among fractions that keep ruin risk low.

This is research code, not investment advice.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from pathlib import Path
from typing import Any

import pandas as pd


def trade_pnl(position: float, net_return: float, fixed_fee_per_side: float) -> float:
    """Dollar PnL of one trade.

    `net_return` already includes proportional (percentage) costs on both
    sides, matching apply_costs() in grad_backtest.py. Fixed fees are charged
    once per side regardless of size, which is why small positions bleed.
    """
    return position * net_return - 2.0 * fixed_fee_per_side


def simulate_path(
    returns: list[float],
    trades: int,
    balance: float,
    fraction: float,
    fixed_fee_per_side: float,
    min_position: float,
    ruin_threshold: float,
    rng: random.Random,
) -> dict[str, float]:
    peak = balance
    max_drawdown = 0.0
    floor_hits = 0
    for _ in range(trades):
        if balance < ruin_threshold:
            return {"final": 0.0, "max_drawdown": 1.0, "ruined": 1.0, "floor_hits": floor_hits}
        position = balance * fraction
        if position < min_position:
            position = min(min_position, balance)
            floor_hits += 1
        balance += trade_pnl(position, rng.choice(returns), fixed_fee_per_side)
        peak = max(peak, balance)
        if peak > 0:
            max_drawdown = max(max_drawdown, 1.0 - balance / peak)
    ruined = 1.0 if balance < ruin_threshold else 0.0
    return {"final": max(balance, 0.0), "max_drawdown": max_drawdown, "ruined": ruined, "floor_hits": floor_hits}


def evaluate_fraction(
    returns: list[float],
    fraction: float,
    args: argparse.Namespace,
    rng: random.Random,
) -> dict[str, Any]:
    finals: list[float] = []
    drawdowns: list[float] = []
    ruins = 0
    floor_hits = 0
    for _ in range(args.paths):
        path = simulate_path(
            returns,
            args.trades_per_path,
            args.balance,
            fraction,
            args.fixed_fee_per_side,
            args.min_position,
            args.ruin_threshold,
            rng,
        )
        finals.append(path["final"])
        drawdowns.append(path["max_drawdown"])
        ruins += int(path["ruined"])
        floor_hits += int(path["floor_hits"])
    finals.sort()
    drawdowns.sort()

    def pct(values: list[float], q: float) -> float:
        return values[min(len(values) - 1, int(q * len(values)))]

    median_final = pct(finals, 0.5)
    return {
        "fraction": fraction,
        "median_final": median_final,
        "p5_final": pct(finals, 0.05),
        "p95_final": pct(finals, 0.95),
        "median_max_drawdown": pct(drawdowns, 0.5),
        "ruin_rate": ruins / args.paths,
        "median_log_growth": math.log(median_final / args.balance) if median_final > 0 else float("-inf"),
        "floor_hit_rate": floor_hits / (args.paths * args.trades_per_path),
    }


def fee_reality(args: argparse.Namespace, side_cost: float) -> dict[str, Any]:
    round_trip_pct = 1 - (1 - side_cost) / (1 + side_cost)
    rows = []
    for position in (5, 10, 20, 25, 50, 100):
        if position > args.balance:
            continue
        fixed = 2 * args.fixed_fee_per_side
        rows.append(
            {
                "position_usd": position,
                "fixed_fees_usd": fixed,
                "fixed_fees_pct": fixed / position,
                "total_round_trip_cost_pct": round_trip_pct + fixed / position,
            }
        )
    return {
        "proportional_round_trip_cost_pct": round_trip_pct,
        "breakeven_move_needed_pct_by_position": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    data_dir = os.getenv("DATA_DIR", "data")
    parser.add_argument("--input", default=os.path.join(data_dir, "trade_results.csv"))
    parser.add_argument("--balance", type=float, default=100.0, help="Starting bankroll in USD")
    parser.add_argument(
        "--fixed-fee-per-side",
        type=float,
        default=0.10,
        help="Fixed USD cost per swap (base fee + priority fee/tip), charged on entry AND exit",
    )
    parser.add_argument(
        "--side-cost",
        type=float,
        default=0.03,
        help="Proportional cost per side used in the backtest (for the fee report only; "
        "trade_results.csv net returns already include it)",
    )
    parser.add_argument("--min-position", type=float, default=10.0, help="Smallest trade worth placing")
    parser.add_argument("--ruin-threshold", type=float, default=5.0, help="Balance below this counts as busted")
    parser.add_argument("--max-ruin-rate", type=float, default=0.05, help="Reject fractions with more ruin risk")
    parser.add_argument("--fractions", default="0.02,0.05,0.10,0.15,0.20,0.25,0.33,0.50")
    parser.add_argument("--trades-per-path", type=int, default=200)
    parser.add_argument("--paths", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", default=os.path.join(data_dir, "sizing_summary.json"))
    args = parser.parse_args()

    frame = pd.read_csv(args.input)
    if "net_return" not in frame.columns:
        raise SystemExit(f"{args.input} has no net_return column; run the backtest first")
    returns = [float(r) for r in frame["net_return"].dropna()]
    if len(returns) < 20:
        raise SystemExit(
            f"Only {len(returns)} trades in {args.input}. Sizing conclusions from so few trades "
            "are noise; collect a bigger sample before trusting any fraction."
        )

    rng = random.Random(args.seed)
    grid = [float(f) for f in args.fractions.split(",")]
    table = [evaluate_fraction(returns, fraction, args, rng) for fraction in grid]

    viable = [row for row in table if row["ruin_rate"] <= args.max_ruin_rate]
    best = max(viable, key=lambda row: row["median_log_growth"]) if viable else None

    edge = sum(returns) / len(returns)
    summary = {
        "input": args.input,
        "trade_sample_size": len(returns),
        "mean_net_return_per_trade": edge,
        "median_net_return_per_trade": float(pd.Series(returns).median()),
        "win_rate": sum(r > 0 for r in returns) / len(returns),
        "starting_balance": args.balance,
        "fixed_fee_per_side_usd": args.fixed_fee_per_side,
        "fee_reality": fee_reality(args, args.side_cost),
        "grid": table,
        "recommended": best,
        "note": (
            "Recommendation maximizes median log growth subject to ruin_rate <= "
            f"{args.max_ruin_rate}. Consider using half of it live: backtests overstate edge."
        ),
    }
    if edge <= 0:
        summary["warning"] = (
            "Mean net return per trade is not positive. No position size or profit-taking level "
            "makes a negative-edge strategy profitable; smaller sizing only slows the bleed."
        )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))

    print(f"Sample: {len(returns)} trades | mean net/trade {edge:+.2%} | win rate {summary['win_rate']:.1%}")
    print(f"{'frac':>6} {'median$':>9} {'p5$':>8} {'p95$':>9} {'maxDD':>7} {'ruin':>6} {'floor':>6}")
    for row in table:
        print(
            f"{row['fraction']:>6.2f} {row['median_final']:>9.2f} {row['p5_final']:>8.2f} "
            f"{row['p95_final']:>9.2f} {row['median_max_drawdown']:>7.1%} {row['ruin_rate']:>6.1%} "
            f"{row['floor_hit_rate']:>6.1%}"
        )
    if "warning" in summary:
        print(f"\nWARNING: {summary['warning']}")
    elif best:
        print(
            f"\nRecommended fraction: {best['fraction']:.0%} of balance per trade "
            f"(median ${best['median_final']:.2f} after {args.trades_per_path} trades, "
            f"ruin {best['ruin_rate']:.1%}). Live suggestion: start at half that."
        )
    else:
        print("\nNo fraction met the ruin-rate constraint; the edge is too weak for this fee structure.")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
