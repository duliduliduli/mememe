"""The scout's promotion evidence: shadow costs that left out rent, a paper route that could
promote on a single lucky stress quote, and a rejection histogram that hid the reason wallets
actually died. All three were found while diagnosing why 27 wallets reached the live set without
anyone having read a per-wallet number."""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="grad-scout-evidence-test-"))
os.environ["GMGN_UNITS_PER_SECOND"] = "100000"

import scout


def cfg_with(**env):
    with mock.patch.dict(os.environ, {k: str(v) for k, v in env.items()}):
        return scout.ScoutConfig()


def fill(pnl_base, pnl_stress=None, wallet="w", closed=True, reason="trailing_stop"):
    return {"wallet": wallet, "opened_ts": 1_000.0, "closed_ts": 2_000.0 if closed else None,
            "reason": reason, "pnl_base": pnl_base, "pnl_stress": pnl_stress}


class Lane:
    """A stand-in exposing just enough of ScoutLane to call its real promotion logic."""
    paper_record = scout.ScoutLane.paper_record
    paper_green = scout.ScoutLane.paper_green

    def __init__(self, cfg, trades):
        self.cfg = cfg
        self.st = {"trades": list(trades)}


class RentCostTests(unittest.TestCase):
    def test_default_charges_one_token_account_rent(self):
        self.assertAlmostEqual(cfg_with().rent_usd, 0.25)

    def test_zero_restores_the_old_cost_model(self):
        self.assertEqual(cfg_with(SCOUT_RENT_USD="0").rent_usd, 0.0)

    def test_configurable(self):
        self.assertAlmostEqual(cfg_with(SCOUT_RENT_USD="0.40").rent_usd, 0.40)

    def test_rent_is_material_at_shadow_ticket_size(self):
        """The point of charging it: on a $5 shadow ticket 0.25 is 5%, which is the same order as
        the whole edge being measured."""
        rent, size = cfg_with().rent_usd, 5.0
        self.assertGreater(rent / size, 0.04)


class PaperGreenStressEvidenceTests(unittest.TestCase):
    def green(self, trades, **env):
        cfg = cfg_with(**{"PROMOTE_MIN_PAPER_FILLS": 5, **env})
        return Lane(cfg, trades).paper_green("w")

    def test_thirty_baseline_fills_and_one_lucky_stress_quote_no_longer_promotes(self):
        """The defect: net_base_usd summed all fills but net_stress_usd summed only the fills that
        had a stress quote, so one stressed winner carried an untested wallet into the live set."""
        trades = [fill(1.0) for _ in range(29)] + [fill(1.0, pnl_stress=5.0)]
        ok, why = self.green(trades)
        self.assertFalse(ok)
        self.assertIn("1 of 30", why)

    def test_no_stress_quotes_at_all_no_longer_promotes_on_the_baseline_alone(self):
        """net_stress_usd is None when nothing was stressed, so the old code skipped that clause
        entirely and promoted on baseline numbers that never priced our latency."""
        ok, why = self.green([fill(2.0) for _ in range(6)])
        self.assertFalse(ok)
        self.assertIn("0 of 6", why)

    def test_enough_stressed_fills_and_both_nets_positive_still_promotes(self):
        ok, why = self.green([fill(1.0, pnl_stress=0.5) for _ in range(6)])
        self.assertTrue(ok, why)
        self.assertIn("with 20s latency", why)

    def test_a_negative_stress_net_still_blocks(self):
        ok, why = self.green([fill(1.0, pnl_stress=-0.5) for _ in range(6)])
        self.assertFalse(ok)
        self.assertIn("with 20s latency", why)

    def test_a_negative_baseline_net_still_blocks(self):
        ok, why = self.green([fill(-1.0, pnl_stress=0.5) for _ in range(6)])
        self.assertFalse(ok)
        self.assertIn("paper net", why)

    def test_too_few_fills_outranks_the_stress_clause(self):
        ok, why = self.green([fill(1.0, pnl_stress=1.0) for _ in range(2)])
        self.assertFalse(ok)
        self.assertIn("need 5", why)

    def test_zero_disables_the_new_clause(self):
        ok, why = self.green([fill(2.0) for _ in range(6)], PROMOTE_MIN_STRESS_FILLS="0")
        self.assertTrue(ok, why)

    def test_the_threshold_is_configurable_up_to_the_strict_reading(self):
        ok, _ = self.green([fill(1.0, pnl_stress=0.5) for _ in range(6)], PROMOTE_MIN_STRESS_FILLS="6")
        self.assertTrue(ok)
        ok, why = self.green([fill(1.0, pnl_stress=0.5) for _ in range(5)] + [fill(1.0)],
                             PROMOTE_MIN_STRESS_FILLS="6")
        self.assertFalse(ok)
        self.assertIn("5 of 6", why)

    def test_missed_and_open_fills_are_not_counted_as_evidence(self):
        trades = [fill(1.0, pnl_stress=1.0) for _ in range(4)]
        trades += [fill(9.0, pnl_stress=9.0, closed=False) for _ in range(3)]
        trades += [fill(9.0, pnl_stress=9.0, reason="missed:quote_budget") for _ in range(3)]
        ok, why = self.green(trades)
        self.assertFalse(ok, "open and missed fills must not pad the counts")
        self.assertIn("4 paper fills", why)


class GateHistogramTests(unittest.TestCase):
    def report(self, candidates, **env):
        cfg = cfg_with(**{"SCOUT_FAST_TRACK": 0, **env})
        return scout.candidate_report({"candidates": candidates}, cfg, 1_800_000_000.0)

    def cand(self, state="shadow", failed=(), missing=()):
        return {"state": state, "evaluation": {"failed": list(failed), "missing": list(missing), "gates": {}}}

    def test_missing_is_counted_separately_from_failed(self):
        """`missing` rejects exactly as hard as `failed` (qualified = not failed and not
        missing_deciding), but the old histogram counted only failures -- which is how ~200 wallets
        came to read as "unqualified on merit" when the deciding gates had never been answered."""
        rep = self.report({
            "a": self.cand(failed=["profit_factor_30d"]),
            "b": self.cand(missing=["hold_time"]),
            "c": self.cand(missing=["hold_time", "leaderboard"]),
        })
        self.assertEqual(rep["gate_failures"], {"profit_factor_30d": 1})
        self.assertEqual(rep["gate_missing"], {"hold_time": 2, "leaderboard": 1})

    def test_a_wallet_with_no_evidence_at_all_is_visible(self):
        rep = self.report({"a": self.cand(missing=["sniper", "fast_exits", "hold_time", "profit_factor_30d"])})
        self.assertEqual(rep["gate_failures"], {})
        self.assertEqual(len(rep["gate_missing"]), 4)

    def test_live_and_qualified_wallets_are_excluded_from_both(self):
        rep = self.report({
            "a": self.cand(state="live", failed=["x"], missing=["y"]),
            "b": self.cand(state="qualified", failed=["x"], missing=["y"]),
            "c": self.cand(failed=["x"], missing=["y"]),
        })
        self.assertEqual(rep["gate_failures"], {"x": 1})
        self.assertEqual(rep["gate_missing"], {"y": 1})

    def test_both_histograms_are_sorted_by_frequency(self):
        rep = self.report({
            "a": self.cand(missing=["rare"]),
            "b": self.cand(missing=["common"]),
            "c": self.cand(missing=["common"]),
        })
        self.assertEqual(list(rep["gate_missing"]), ["common", "rare"])

    def test_rejected_by_is_unchanged(self):
        rep = self.report({"a": {"state": "rejected",
                                 "evaluation": {"failed": [], "missing": [],
                                                "gates": {"tags": {"status": "fail"}}}}})
        self.assertEqual(rep["rejected_by"], {"tags": 1})


if __name__ == "__main__":
    unittest.main()
