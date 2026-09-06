"""The cost models must reproduce the specification's illustrative tables exactly."""
import math
import unittest

from mm import costs


class RoundTripTests(unittest.TestCase):
    def test_break_even_table_matches_specification(self):
        q, d, c = 50.0, 0.001, 0.02
        expected = {
            0.0025: (0.0050188, 0.0074347, 0.1274),
            0.0050: (0.0100755, 0.0125035, -0.1237),
            0.0100: (0.0203041, 0.0227567, -0.6239),
            0.0120: (0.0244390, 0.0269016, -0.8233),
        }
        for fee, (be_fee, be_all, pnl) in expected.items():
            k_fee = costs.round_trip_multiplier(fee, fee)
            k_all = costs.round_trip_multiplier(fee, fee, d, d)
            self.assertAlmostEqual(costs.break_even_gain(q, k_fee), be_fee, places=7)
            self.assertAlmostEqual(costs.break_even_gain(q, k_all, c), be_all, places=7)
            self.assertAlmostEqual(costs.net_pnl(q, 0.01, k_all, c), pnl, places=4)

    def test_final_proceeds_identity(self):
        k = costs.round_trip_multiplier(0.01, 0.01)
        self.assertAlmostEqual(costs.final_proceeds(100, 0.05, k, 1) - 100, costs.net_pnl(100, 0.05, k, 1))


class CpmmTests(unittest.TestCase):
    def test_exit_impact_table(self):
        for fraction, impact in [(0.001, 0.000999), (0.0025, 0.002494), (0.005, 0.004975), (0.05, 0.047619), (0.10, 0.090909)]:
            self.assertAlmostEqual(costs.cpmm_exit(1.0, fraction)[1], impact, places=6)

    def test_full_range_value_table(self):
        for change, value in [(0.5, 1224.74), (0.0, 1000.0), (-0.2, 894.43), (-0.5, 707.11), (-0.9, 316.23)]:
            self.assertAlmostEqual(costs.full_range_lp_value(1000, 1 + change), value, places=2)
            hold = costs.hold_value(1000, 1 + change)
            self.assertLessEqual(costs.full_range_lp_value(1000, 1 + change), hold + 1e-9)

    def test_lp_gross_fee_example(self):
        self.assertAlmostEqual(costs.lp_gross_fee_income(1000, 500_000, 1_000_000, 0.002), 4.0)


class RangePositionTests(unittest.TestCase):
    def test_open_splits_value_at_midpoint(self):
        pos = costs.RangePosition.open(100, 1.0, 0.8, 1.25)
        self.assertAlmostEqual(pos.value(1.0), 100.0)
        self.assertAlmostEqual(pos.token_fraction(1.0), 0.5, places=6)

    def test_becomes_one_sided_outside_range(self):
        pos = costs.RangePosition.open(100, 1.0, 0.8, 1.25)
        self.assertEqual(pos.amounts(0.7)[1], 0.0)   # all token below range
        self.assertEqual(pos.amounts(1.3)[0], 0.0)   # all quote above range
        self.assertLess(pos.value(0.8), 100)
        self.assertGreater(pos.value(1.25), 100)
        self.assertFalse(pos.in_range(0.8))

    def test_capital_efficiency_grows_as_range_narrows(self):
        wide = costs.RangePosition(1, *costs.symmetric_range(1.0, 0.25))
        narrow = costs.RangePosition(1, *costs.symmetric_range(1.0, 0.05))
        self.assertGreater(narrow.capital_efficiency(), wide.capital_efficiency())

    def test_in_range_probability_bounds(self):
        self.assertAlmostEqual(costs.in_range_probability(0.30, 0.02, 6), 1.0, places=5)
        self.assertLess(costs.in_range_probability(0.01, 0.05, 6), 0.05)
        # z = 1 for T = 1: sum_k (-1)^k [Phi(2k+1) - Phi(2k-1)] = 0.3708.
        self.assertAlmostEqual(costs.in_range_probability(math.e - 1, 1.0, 1.0), 0.3708, places=3)


class ExpectancyTests(unittest.TestCase):
    def test_ninety_percent_win_rate_can_lose(self):
        self.assertAlmostEqual(costs.expected_pnl(0.9, 1, 15), -0.6)

    def test_fixed_cost_scale(self):
        self.assertAlmostEqual(costs.required_return_for_fixed_cost(30, 100), 0.30)
        self.assertAlmostEqual(costs.required_return_for_fixed_cost(30, 10_000), 0.003)


if __name__ == "__main__":
    unittest.main()
