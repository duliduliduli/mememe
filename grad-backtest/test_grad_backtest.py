import unittest

from grad_backtest import Candle, apply_costs, simulate_trade


class SimulationTests(unittest.TestCase):
    def test_costs_flat_price_are_negative(self):
        gross, net = apply_costs(100.0, 100.0, 0.03)
        self.assertEqual(gross, 0.0)
        self.assertAlmostEqual(net, 0.97 / 1.03 - 1.0)

    def test_take_profit(self):
        candles = [Candle(100, 100, 180, 99, 170, 1)]
        result = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03)
        self.assertEqual(result.exit_reason, "take_profit")
        self.assertEqual(result.exit_price, 175)

    def test_same_candle_uses_conservative_stop(self):
        candles = [Candle(100, 100, 180, 60, 120, 1)]
        result = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03)
        self.assertEqual(result.exit_reason, "sl_ambiguous")
        self.assertEqual(result.exit_price, 70)
        self.assertTrue(result.both_barriers_same_candle)


if __name__ == "__main__":
    unittest.main()

