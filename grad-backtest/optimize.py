#!/usr/bin/env python3
"""Parameter sweep ("training") over cached backtest data, with a
train/validation split to keep the result honest.

Requires a prior `grad_backtest.py run` so the OHLCV cache exists — the sweep
then re-simulates every parameter combination from cache with zero API calls.

Method:
  1. Load every cached token; order chronologically by graduation.
  2. Split: the first (1 - validation_fraction) is the TRAIN set the sweep is
     allowed to fit; the last part is VALIDATION it never optimizes on.
  3. Evaluate the full grid on train, rank by median net return (mean as
     tiebreak), then report the top combos' performance on validation.

If a combo looks great on train and mediocre on validation, it was fitting
noise — that gap is the overfitting alarm, printed, not hidden. Screenshots
of individual charts are not a substitute for this; tokens you noticed are
the ones that moved.

This is research software, not investment advice.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from grad_backtest import Candle, parse_timestamp, price_at_or_after, simulate_trade


def parse_grid(text: str) -> list[float]:
    return [float(x) for x in text.split(",") if x.strip() != ""]


def load_dataset(input_csv: Path, cache_dir: Path, entry_delay: int) -> list[dict[str, Any]]:
    frame = pd.read_csv(input_csv)
    if "extraction_status" in frame:
        frame = frame[frame["extraction_status"].fillna("confirmed") == "confirmed"]
    frame = frame.sort_values("graduation_timestamp")
    dataset: list[dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        mint = str(row.mint_address)
        cache_file = cache_dir / f"{mint}.json"
        if not cache_file.exists():
            continue
        try:
            cached = json.loads(cache_file.read_text())
            graduation_ts = parse_timestamp(row.graduation_timestamp)
            minute_path = [Candle(**item) for item in cached["minute_path"]]
            entry_candles = [Candle(**item) for item in cached["entry_candles"]]
            entry_price, entry_ts = price_at_or_after(entry_candles, graduation_ts + entry_delay, tolerance=90)
        except Exception:
            continue
        dataset.append(
            {
                "mint": mint,
                "graduation_ts": graduation_ts,
                "entry_price": entry_price,
                "entry_ts": entry_ts,
                "minute_path": minute_path,
            }
        )
    return dataset


def evaluate(dataset: list[dict[str, Any]], combo: dict[str, float], side_cost: float) -> dict[str, Any]:
    nets: list[float] = []
    for item in dataset:
        try:
            result = simulate_trade(
                item["mint"], item["graduation_ts"], "", "", item["entry_price"], item["entry_ts"],
                item["minute_path"],
                combo["take_profit"], combo["stop_loss"], int(combo["time_stop_minutes"]),
                side_cost, combo["trailing_stop"], combo["moon_bag"],
            )
            nets.append(result.net_return)
        except ValueError:
            continue
    if not nets:
        return {**combo, "trades": 0, "median": None, "mean": None, "win_rate": None}
    series = pd.Series(nets)
    return {
        **combo,
        "trades": len(nets),
        "median": float(series.median()),
        "mean": float(series.mean()),
        "win_rate": float((series > 0).mean()),
    }


def main() -> None:
    data_dir = os.getenv("DATA_DIR", "data")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=os.path.join(data_dir, "graduations.csv"))
    parser.add_argument("--cache-dir", default=os.path.join(data_dir, "cache"))
    parser.add_argument("--output", default=os.path.join(data_dir, "optimize_summary.json"))
    parser.add_argument("--entry-delay-seconds", type=int, default=30)
    parser.add_argument("--side-cost", type=float, default=0.03)
    parser.add_argument("--validation-fraction", type=float, default=0.3)
    parser.add_argument("--take-profits", default="0.4,0.5,0.75,1.0,1.5")
    parser.add_argument("--stop-losses", default="0.2,0.3,0.4")
    parser.add_argument("--time-stops", default="15,30,60")
    parser.add_argument("--trailing-stops", default="0,0.2,0.3")
    parser.add_argument("--moon-bags", default="0,0.15")
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()

    dataset = load_dataset(Path(args.input), Path(args.cache_dir), args.entry_delay_seconds)
    if len(dataset) < 30:
        raise SystemExit(
            f"Only {len(dataset)} cached tokens. Run `grad_backtest.py run` on a bigger sample first; "
            "sweeping parameters on a tiny sample just memorizes noise."
        )
    split = int(len(dataset) * (1.0 - args.validation_fraction))
    train, val = dataset[:split], dataset[split:]
    print(f"Dataset: {len(dataset)} tokens -> train {len(train)}, validation {len(val)} (chronological split)")
    if len(train) < 50:
        print("WARNING: train set under 50 tokens; treat every number below as provisional.")

    grid = [
        {"take_profit": tp, "stop_loss": sl, "time_stop_minutes": ts, "trailing_stop": tr, "moon_bag": mb}
        for tp, sl, ts, tr, mb in itertools.product(
            parse_grid(args.take_profits), parse_grid(args.stop_losses), parse_grid(args.time_stops),
            parse_grid(args.trailing_stops), parse_grid(args.moon_bags),
        )
    ]
    print(f"Sweeping {len(grid)} parameter combinations from cache (no API calls)...")

    results = []
    for combo in grid:
        row = evaluate(train, combo, args.side_cost)
        if row["median"] is not None:
            results.append(row)
    results.sort(key=lambda r: (r["median"], r["mean"]), reverse=True)

    baseline = {"take_profit": 0.75, "stop_loss": 0.30, "time_stop_minutes": 30, "trailing_stop": 0.0, "moon_bag": 0.0}
    baseline_val = evaluate(val, baseline, args.side_cost)

    print(f"\n{'tp':>5} {'sl':>5} {'time':>5} {'trail':>6} {'moon':>5} | {'train_med':>9} {'train_win':>9} | {'val_med':>8} {'val_win':>7}")
    top_rows = []
    for row in results[: args.top]:
        v = evaluate(val, {k: row[k] for k in baseline}, args.side_cost)
        top_rows.append({"train": row, "validation": v})
        print(
            f"{row['take_profit']:>5.2f} {row['stop_loss']:>5.2f} {row['time_stop_minutes']:>5.0f} "
            f"{row['trailing_stop']:>6.2f} {row['moon_bag']:>5.2f} | {row['median']:>9.2%} {row['win_rate']:>9.1%} | "
            f"{(v['median'] if v['median'] is not None else float('nan')):>8.2%} "
            f"{(v['win_rate'] if v['win_rate'] is not None else float('nan')):>7.1%}"
        )
    print(
        f"\nBaseline (current defaults) on validation: median "
        f"{baseline_val['median']:.2%} | win rate {baseline_val['win_rate']:.1%}"
        if baseline_val["median"] is not None else "\nBaseline produced no valid validation trades."
    )

    best = top_rows[0] if top_rows else None
    verdict = None
    if best and best["validation"]["median"] is not None:
        gap = best["train"]["median"] - best["validation"]["median"]
        verdict = (
            "validation broadly confirms the train winner"
            if gap < 0.05
            else f"OVERFITTING WARNING: train median exceeds validation by {gap:.1%} — do not trust the train numbers"
        )
        print(f"\n{verdict}")
        print("Use the VALIDATION column to judge a combo; the train column is what the sweep was allowed to fit.")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {
            "dataset_size": len(dataset), "train_size": len(train), "validation_size": len(val),
            "grid_size": len(grid), "top": top_rows, "baseline_validation": baseline_val, "verdict": verdict,
        },
        indent=2,
    ))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
