import json
import tempfile
import unittest
from pathlib import Path
import score_skips


class ScoreSkipsTests(unittest.TestCase):
    def test_buckets_and_outcomes(self):
        tmp = Path(tempfile.mkdtemp(prefix="grad-skips-"))
        cache = tmp / "cache"
        cache.mkdir()
        (cache / "AAA.json").write_text(json.dumps({"minute_path": [{"timestamp": 1060, "open": 1.0}, {"timestamp": 2800, "open": 2.0}]}))
        self.assertEqual(score_skips.reason_bucket("price impact 5.3% > 5.0%"), "price impact")
        self.assertEqual(score_skips.reason_bucket("market cap $450 < $25,000"), "market cap floor")
        prices = score_skips.snapshots(cache, "AAA", 1000)
        self.assertEqual(prices["plus_1m"], 1.0)
        self.assertEqual(prices["plus_30m"], 2.0)


if __name__ == "__main__":
    unittest.main()
