"""Screener rejects for the specification's reasons and logs every one of them."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from mm.config import MMConfig, USDC, WSOL
from mm.screener import Screener, load_universe
from mm.sources import depth_at_impact, impact_at_size, transfer_fee_bps

MINT = "Mint1111111111111111111111111111111111111111"
NOW = time.time()


def token(**over):
    base = {
        "id": MINT, "symbol": "MEME", "name": "Meme", "decimals": 6, "usdPrice": 0.01, "mcap": 2_000_000,
        "holderCount": 5000, "organicScore": 80, "tokenProgram": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "stats24h": {"numTraders": 1500, "numOrganicBuyers": 40, "buyVolume": 300_000, "sellVolume": 280_000},
        "audit": {"topHoldersPercentage": 25.0, "devBalancePercentage": 0.5},
        "firstPool": {"createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 90 * 86400))},
        "tags": ["meme"],
    }
    base.update(over)
    return base


def pair(liquidity=300_000, quote=WSOL, created=NOW - 90 * 86400):
    return {"chainId": "solana", "dexId": "meteora", "pairAddress": "Pool111", "baseToken": {"address": MINT},
            "quoteToken": {"address": quote, "symbol": "SOL"}, "priceUsd": "0.01", "priceNative": "0.0001",
            "liquidity": {"usd": liquidity, "base": 1, "quote": liquidity / 2 / 100}, "pairCreatedAt": created * 1000}


def dlmm_pool(tvl=250_000):
    return {"address": "DLMM1111", "tvl": tvl, "token_x": {"address": MINT}, "token_y": {"address": WSOL},
            "pool_config": {"bin_step": 100, "base_fee_pct": 1.0, "protocol_fee_pct": 10.0}, "dynamic_fee_pct": 0.2,
            "fee_tvl_ratio": {"24h": 0.02}, "volume": {"24h": 800_000}}


def ladder(impacts):
    return [{"size_usd": s, "impact_pct": i} for s, i in impacts]


def make_sources(pairs=None, pools=None, mint_info=None, ladder_rows=None):
    src = MagicMock()
    src.dexscreener.token_pairs.return_value = pairs if pairs is not None else [pair()]
    src.meteora.pools_for_mint.return_value = pools if pools is not None else [dlmm_pool()]
    src.rpc.mint_info.return_value = mint_info if mint_info is not None else {
        "program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "decimals": 6, "mint_authority": None,
        "freeze_authority": None, "extensions": {}}
    src.jupiter.exit_ladder.return_value = ladder_rows if ladder_rows is not None else ladder(
        [(10, 0.0), (40, 0.05), (200, 0.2), (800, 0.9), (4000, 4.0)])
    return src


class ScreenerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()))

    def screen(self, tok=None, **kw):
        return Screener(self.cfg, make_sources(**kw)).screen_token(tok or token(), now=NOW)

    def test_healthy_candidate_is_accepted_with_diagnostics(self):
        cand = self.screen()
        self.assertTrue(cand.accepted, cand.rejects)
        self.assertAlmostEqual(cand.age_days, 90, places=1)
        self.assertEqual(cand.dlmm_pool, "DLMM1111")
        self.assertAlmostEqual(cand.liquidity_to_market_cap, 0.15)
        self.assertIsNotNone(cand.impact_at_max_position_pct)
        self.assertGreater(cand.depth_emergency_usd, 800)

    def test_young_token_rejected(self):
        cand = self.screen(token(firstPool={"createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 5 * 86400))}),
                           pairs=[pair(created=NOW - 5 * 86400)])
        self.assertIn("token age 5.0d", cand.rejects[0][1])

    def test_thin_pool_rejected(self):
        cand = self.screen(pairs=[pair(liquidity=40_000)])
        self.assertTrue(any("best pool liquidity $40,000" in r for _, r, _, _ in cand.rejects))

    def test_unaccepted_quote_only_is_rejected(self):
        cand = self.screen(pairs=[pair(quote="Other111")])
        self.assertTrue(any("no pool against an accepted quote" in r for _, r, _, _ in cand.rejects))

    def test_live_authorities_and_token_2022_controls_rejected(self):
        info = {"program": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "decimals": 6, "mint_authority": "Auth1",
                "freeze_authority": "Auth2",
                "extensions": {"permanentDelegate": {"delegate": "Del1"},
                               "transferFeeConfig": {"newerTransferFee": {"transferFeeBasisPoints": 100}},
                               "transferHook": {"programId": "Hook1"}}}
        cand = self.screen(mint_info=info)
        reasons = [r for _, r, _, _ in cand.rejects]
        self.assertTrue(any("mint authority" in r for r in reasons))
        self.assertTrue(any("freeze authority" in r for r in reasons))
        self.assertTrue(any("permanent delegate" in r for r in reasons))
        self.assertTrue(any("transfer fee 100 bps" in r for r in reasons))
        self.assertTrue(any("transfer hook" in r for r in reasons))

    def test_holder_concentration_and_participation_rejects(self):
        cand = self.screen(token(audit={"topHoldersPercentage": 70, "devBalancePercentage": 20},
                                 stats24h={"numTraders": 50, "numOrganicBuyers": 0, "buyVolume": 95, "sellVolume": 5},
                                 organicScore=10))
        reasons = [r for _, r, _, _ in cand.rejects]
        self.assertTrue(any("top holders 70.0%" in r for r in reasons))
        self.assertTrue(any("dev balance 20.0%" in r for r in reasons))
        self.assertTrue(any("50 traders" in r for r in reasons))
        self.assertTrue(any("organic score 10" in r for r in reasons))
        self.assertTrue(any("flow imbalance 0.90" in r for r in reasons))

    def test_exit_test_rejects_when_max_inventory_moves_price(self):
        cand = self.screen(ladder_rows=ladder([(10, 0.0), (40, 0.6), (200, 2.0), (800, 5.0), (4000, 12.0)]))
        reasons = [r for _, r, _, _ in cand.rejects]
        self.assertTrue(any("impact 0.60%" in r for r in reasons), reasons)
        self.assertTrue(any("depth $" in r and "< $800" in r for r in reasons), reasons)

    def test_missing_dlmm_pool_is_a_warning_unless_required(self):
        cand = self.screen(pools=[])
        self.assertTrue(cand.accepted)
        self.assertIn("no DLMM pool; momentum-only candidate", cand.warnings)
        self.cfg.require_dlmm_pool = True
        cand = self.screen(pools=[])
        self.assertTrue(any("no Meteora DLMM pool" in r for _, r, _, _ in cand.rejects))

    def test_run_writes_universe_and_reject_log(self):
        src = make_sources()
        bad = token(id="Mint2222222222222222222222222222222222222222", symbol="RUG", organicScore=1)
        src.jupiter.top_traded.return_value = [token(), bad, {"id": WSOL, "symbol": "SOL"}, {"id": "X", "symbol": "USDT"}]
        src.jupiter.top_organic.return_value = []
        accepted, rejected = Screener(self.cfg, src).run()
        self.assertEqual([c.symbol for c in accepted], ["MEME"])
        self.assertEqual([c.symbol for c in rejected], ["RUG"])
        self.assertEqual(load_universe(self.cfg.universe_file)[0]["mint"], MINT)
        self.assertIn("organic score 1", self.cfg.rejects_file.read_text())


class LadderTests(unittest.TestCase):
    def test_depth_interpolates_to_the_band(self):
        rows = ladder([(10, 0.0), (100, 0.1), (1000, 1.0), (10000, 10.0)])
        self.assertAlmostEqual(depth_at_impact(rows, 0.25), 250.0)
        self.assertAlmostEqual(depth_at_impact(rows, 3.0), 3000.0)
        self.assertAlmostEqual(impact_at_size(rows, 550), 0.55)
        self.assertIsNone(depth_at_impact([{"size_usd": 1, "impact_pct": None}], 1))

    def test_ladder_treats_dust_quotes_as_unknown(self):
        from unittest.mock import MagicMock
        from mm.sources import Jupiter
        jup = Jupiter(MagicMock(), MagicMock(jupiter_base="http://x"))
        # 1 unit out per 1e6 raw in at $10, then a broken route that returns dust at $40
        jup.quote = MagicMock(side_effect=[{"outAmount": "1000000", "routePlan": []},
                                           {"outAmount": "1", "routePlan": []}])
        rows = jup.exit_ladder("mint", 6, 1.0, [40.0, 10.0])
        self.assertEqual([r["size_usd"] for r in rows], [10.0, 40.0])
        self.assertAlmostEqual(rows[0]["impact_pct"], 0.0)
        self.assertIsNone(rows[1]["impact_pct"])
        self.assertIsNone(impact_at_size(rows, 40.0))
        self.assertAlmostEqual(impact_at_size(rows, 10.0), 0.0)

    def test_transfer_fee_reads_newest_schedule(self):
        self.assertEqual(transfer_fee_bps({"transferFeeConfig": {"olderTransferFee": {"transferFeeBasisPoints": 50},
                                                                 "newerTransferFee": {"transferFeeBasisPoints": 75}}}), 75)
        self.assertEqual(transfer_fee_bps({}), 0)


if __name__ == "__main__":
    unittest.main()


class RecorderTests(unittest.TestCase):
    def test_source_failures_land_in_the_error_column(self):
        from mm.recorder import Recorder
        from mm.sources import HttpError
        cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()))
        src = make_sources()
        src.jupiter.token.side_effect = HttpError("429")
        src.jupiter.sol_price.side_effect = HttpError("429")
        src.meteora.pool.return_value = dlmm_pool()
        src.meteora.pool.return_value.update({"current_price": 0.0001, "token_x_amount": 1e6, "token_y_amount": 1000,
                                              "token_y": {"address": WSOL, "price": 100.0}, "token_x": {"address": MINT},
                                              "cumulative_metrics": {"volume": 1.0, "fees": 2.0}})
        rows = Recorder(cfg, src).tick([{"mint": MINT, "symbol": "MEME", "dlmm_pool": "DLMM1111"}])
        self.assertEqual(len(rows), 1)
        self.assertIn("jupiter token: 429", rows[0]["errors"])
        self.assertIn("sol price: 429", rows[0]["errors"])
        self.assertAlmostEqual(rows[0]["price_usd"], 0.01)      # derived from the DLMM pool price
        self.assertTrue((cfg.snapshots_dir / f"{MINT}.csv").exists())

    def test_snapshot_crash_still_writes_a_row(self):
        from mm.recorder import Recorder
        cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()))
        src = make_sources()
        src.jupiter.token.side_effect = RuntimeError("boom")
        src.meteora.pool.side_effect = RuntimeError("boom")
        src.jupiter.sol_price.return_value = 100.0
        rows = Recorder(cfg, src).tick([{"mint": MINT, "symbol": "MEME", "dlmm_pool": "DLMM1111"}])
        self.assertIn("boom", rows[0]["errors"])
