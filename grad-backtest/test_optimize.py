import json
import os
import tempfile
import unittest
from pathlib import Path

from grad_backtest import Candle
from optimize import evaluate, load_dataset, parse_grid


def make_cache(cache_dir: Path, mint: str, candles: list[Candle], entry: list[Candle]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{mint}.json").write_text(json.dumps({
        "pool_address": "p", "dex_id": "pump",
        "minute_path": [vars(c) for c in candles],
        "entry_candles": [vars(c) for c in entry],
    }))


class OptimizeTests(unittest.TestCase):
    def test_parse_grid(self):
        self.assertEqual(parse_grid("0.4,0.75, 1.0"), [0.4, 0.75, 1.0])

    def test_load_and_evaluate(self):
        tmp = Path(tempfile.mkdtemp(prefix="grad-opt-test-"))
        cache = tmp / "cache"
        # winner: entry 100, runs to 200 -> TP; loser: entry 100, drops to 60 -> SL
        make_cache(cache, "winner", [Candle(1030, 100, 200, 100, 190, 1)], [Candle(1030, 100, 101, 99, 100, 1)])
        make_cache(cache, "loser", [Candle(2030, 100, 105, 60, 62, 1)], [Candle(2030, 100, 101, 99, 100, 1)])
        csv_path = tmp / "graduations.csv"
        csv_path.write_text(
            "mint_address,graduation_timestamp,extraction_status\n"
            "winner,1000,confirmed\nloser,2000,confirmed\nuncached,3000,confirmed\n"
        )
        dataset = load_dataset(csv_path, cache, entry_delay=30)
        self.assertEqual([d["mint"] for d in dataset], ["winner", "loser"])  # uncached dropped, order kept

        combo = {"take_profit": 0.75, "stop_loss": 0.30, "time_stop_minutes": 30, "trailing_stop": 0.0, "moon_bag": 0.0}
        stats = evaluate(dataset, combo, side_cost=0.03)
        self.assertEqual(stats["trades"], 2)
        self.assertEqual(stats["win_rate"], 0.5)

    def test_evaluate_empty(self):
        stats = evaluate([], {"take_profit": 0.75, "stop_loss": 0.3, "time_stop_minutes": 30,
                              "trailing_stop": 0.0, "moon_bag": 0.0}, 0.03)
        self.assertEqual(stats["trades"], 0)
        self.assertIsNone(stats["median"])


if __name__ == "__main__":
    unittest.main()
