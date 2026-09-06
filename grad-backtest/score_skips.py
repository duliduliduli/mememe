#!/usr/bin/env python3
"""Score skips.csv against cached OHLCV so filters are judged on skipped coins, not survivors."""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any


DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

REASON_PREFIXES = [
    ("price impact", "price impact"),
    ("market cap", "market cap"),
    ("graduated", "curve age"),
    ("top wallet", "top wallet"),
    ("top10 wallets", "top10"),
    ("bundle slot", "bundle slot"),
    ("funding cluster", "funding cluster"),
    ("dev cluster", "dev cluster"),
    ("stale", "stale"),
    ("too old", "too old"),
    ("no sell route", "no sell route"),
    ("sizing", "sizing"),
    ("graduation cluster", "heat"),
]


def reason_bucket(reason: str) -> str:
    text = (reason or "").lower()
    for needle, label in (
        ("one-party fill", "curve activity floor"),
        ("dump started before entry", "early dump"),
        ("launch factory", "creator launches"),
        ("creator still holds", "creator holding"),
    ):
        if needle in text:
            return label
    for needle, label in REASON_PREFIXES:
        if needle in text:
            if needle == "market cap" and "<" in text:
                return "market cap floor"
            if needle == "market cap" and ">" in text:
                return "market cap ceiling"
            return label
    return "other"


def _candle_ts_price(candle: Any):
    if isinstance(candle, dict):
        ts = candle.get("timestamp") or candle.get("t")
        price = candle.get("open") or candle.get("close") or candle.get("price")
        if ts is None or price is None:
            return None
        return float(ts), float(price)
    if isinstance(candle, (list, tuple)) and len(candle) >= 2:
        return float(candle[0]), float(candle[1])
    return None


def price_near(candles, target: float, tolerance: float = 180):
    best = None
    best_delta = None
    for candle in candles:
        parsed = _candle_ts_price(candle)
        if parsed is None:
            continue
        ts, price = parsed
        delta = abs(ts - target)
        if delta <= tolerance and (best_delta is None or delta < best_delta):
            best, best_delta = price, delta
    return best


def snapshots(cache_dir: Path, mint: str, graduated_ts):
    empty = {"plus_1m": None, "plus_5m": None, "plus_30m": None}
    path = cache_dir / f"{mint}.json"
    if not path.exists() or graduated_ts is None:
        return empty
    raw = json.loads(path.read_text())
    candles = raw.get("minute_path") or raw.get("candles") or raw.get("ohlcv") or []
    return {
        "plus_1m": price_near(candles, graduated_ts + 60),
        "plus_5m": price_near(candles, graduated_ts + 300),
        "plus_30m": price_near(candles, graduated_ts + 1800),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skips", default=str(DATA_DIR / "skips.csv"))
    parser.add_argument("--cache-dir", default=str(DATA_DIR / "cache"))
    parser.add_argument("--output-dir", default=str(DATA_DIR))
    args = parser.parse_args()
    skips_path = Path(args.skips)
    cache_dir = Path(args.cache_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not skips_path.exists():
        raise SystemExit(f"no skips file at {skips_path}")
    rows = list(csv.DictReader(skips_path.open()))
    buckets = defaultdict(list)
    for row in rows:
        bucket = reason_bucket(row.get("reason") or "")
        try:
            graduated_ts = float(row["graduated_ts"]) if row.get("graduated_ts") else None
        except (TypeError, ValueError):
            graduated_ts = None
        prices = snapshots(cache_dir, row.get("mint") or "", graduated_ts)
        entry, later = prices.get("plus_1m"), prices.get("plus_30m")
        net = later / entry - 1.0 if entry and later and entry > 0 else None
        buckets[bucket].append({**row, "bucket": bucket, **prices, "net_30m": net})
    summary = []
    for bucket, items in sorted(buckets.items()):
        nets = [i["net_30m"] for i in items if i["net_30m"] is not None]
        known = len(nets)
        win = sum(1 for x in nets if x >= 0.5)
        lose = sum(1 for x in nets if x <= -0.3)
        mid = sorted(nets)[len(nets) // 2] if nets else None
        summary.append({
            "bucket": bucket,
            "n": len(items),
            "with_prices": known,
            "pct_plus_50_at_30m": round(win / known, 3) if known else None,
            "pct_minus_30_at_30m": round(lose / known, 3) if known else None,
            "median_30m_net": round(mid, 4) if mid is not None else None,
        })
    (out_dir / "skip_outcomes.json").write_text(json.dumps({"summary": summary, "n_skips": len(rows)}, indent=2))
    with (out_dir / "skip_outcomes.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["bucket", "n", "with_prices", "pct_plus_50_at_30m", "pct_minus_30_at_30m", "median_30m_net"])
        writer.writeheader()
        writer.writerows(summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
