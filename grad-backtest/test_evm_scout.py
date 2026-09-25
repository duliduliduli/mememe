"""EVM wallet scout (evm/scout.py): runners, per-wallet P&L from transfer logs, ranking, the
cycle, and the lane copying what it finds."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_evm
from evm import scout
from evm.rpc import RpcError

POOL = "0x" + "a" * 40
ROUTER = "0x" + "b" * 40
TOKEN = "0x" + "c" * 40
W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40
SNIPER = "0x" + "5" * 40
BOT = "0x" + "6" * 40


def cfg(**env):
    base = {"EVM_SCOUT_MIN_REALIZED_USD": "100", "EVM_SCOUT_MIN_COST_USD": "10", "EVM_SCOUT_VENUE_DEGREE": "3"}
    base.update(env)
    with mock.patch.dict(os.environ, base):
        return scout.ScoutConfig()


def gecko_pool(token, symbol, mcap, gain, age_h, now, pool=POOL):
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_h * 3600))
    return {"attributes": {"address": pool, "name": f"{symbol} / WETH", "pool_created_at": created, "market_cap_usd": str(mcap),
                           "price_change_percentage": {"h24": str(gain)}},
            "relationships": {"base_token": {"data": {"id": f"robinhood_{token}"}}, "dex": {"data": {"id": "uniswap-v4-robinhood"}}}}


class RunnerTests(unittest.TestCase):
    def test_runners_are_young_big_and_up(self):
        now = time.time()
        rows = [gecko_pool(TOKEN, "MOON", 900_000, 5000, 10, now),
                gecko_pool("0x" + "d" * 40, "OLD", 900_000, 5000, 200, now),       # too old
                gecko_pool("0x" + "e" * 40, "TINY", 50_000, 5000, 10, now),        # too small
                gecko_pool("0x" + "f" * 40, "FLAT", 900_000, 20, 10, now),         # did not run
                gecko_pool("0x" + "0" * 39 + "9", "WETH", 9e9, 900, 1, now),       # a major
                gecko_pool(TOKEN, "MOON", 400_000, 5000, 10, now, pool="0x" + "7" * 40)]   # smaller pool of the same token
        runners, errors = scout.fetch_runners(cfg(), now, lambda url: {"data": rows})
        self.assertEqual(errors, [])
        self.assertEqual([(r["symbol"], r["pool"]) for r in runners], [("MOON", POOL)])
        _, errors = scout.fetch_runners(cfg(), now, lambda url: (_ for _ in ()).throw(RuntimeError("429")))
        self.assertEqual(len(errors), 5)


class WalletRowTests(unittest.TestCase):
    def test_buys_and_sells_against_venues_priced_by_candles(self):
        # Price 1.0 until t=1000, then 5.0. Block n happens at time n.
        candles = [[0, 0, 0, 0, 1.0], [1000, 0, 0, 0, 5.0]]
        others = [("0x" + "8" * 39 + str(i), 100 + i) for i in range(4)]     # give the pool and router enough peers
        transfers = [(10, scout.ZERO_TOPIC_ADDR, POOL, 1e6)]                  # mint: ignored
        transfers += [(100, POOL, W1, 100.0),                               # W1 buys 100 at $1
                      (200, POOL, SNIPER, 50.0), (230, SNIPER, POOL, 50.0),  # SNIPER in and out in 30 s at $1
                      (1500, W1, ROUTER, 60.0),                             # W1 sells 60 at $5 through the router
                      (300, W1, W2, 5.0)]                                   # wallet-to-wallet: neither buy nor sell
        transfers += [(n, POOL, a, 1.0) for a, n in others] + [(n + 1, a, ROUTER, 1.0) for a, n in others]
        rows = scout.wallet_rows(transfers, float, candles, venue_degree=4, min_cost_usd=10)   # W1 has 3 peers: not a venue
        self.assertEqual(rows[W1]["cost_usd"], 100.0)
        self.assertEqual(rows[W1]["realized_usd"], 240.0)                    # 60 sold for $300 minus $60 cost
        self.assertEqual(rows[W1]["hold_seconds"], 1400)
        self.assertEqual(rows[SNIPER]["hold_seconds"], 30)
        self.assertNotIn(W2, rows)                                           # received by transfer, not bought
        self.assertNotIn(others[0][0], rows)                                 # $1 in: under the $10 floor

    def test_logs_split_when_the_node_caps_results(self):
        class Rpc:
            def __init__(self):
                self.calls = []
            def logs(self, a, b, topics, address=None):
                self.calls.append((a, b))
                if b - a > 10:
                    raise RpcError("eth_getLogs: logs matched by query exceeds limit of 10000")
                return [{"blockNumber": hex(a)}]
        rpc = Rpc()
        out = scout.fetch_logs(rpc, TOKEN, 0, 40, cap=1000)
        self.assertEqual([int(x["blockNumber"], 16) for x in out], sorted(int(x["blockNumber"], 16) for x in out))
        self.assertGreater(len(out), 2)
        rpc2 = Rpc()
        rpc2.logs = lambda a, b, topics, address=None: (_ for _ in ()).throw(RpcError("eth_getLogs: execution reverted"))
        with self.assertRaises(RpcError):
            scout.fetch_logs(rpc2, TOKEN, 0, 40, cap=1000)


class RankTests(unittest.TestCase):
    def tokens(self):
        def row(realized, hold, cost=100.0):
            return {"realized_usd": realized, "cost_usd": cost, "hold_seconds": hold, "entry_minutes": 5.0}
        return {"t1": {"symbol": "A", "rows": {W1: row(500, 3600), W2: row(900, 600), SNIPER: row(800, 20), BOT: row(5000, 900)}},
                "t2": {"symbol": "B", "rows": {W1: row(300, 1800), W2: row(-50, 700), SNIPER: row(700, 15), BOT: row(4000, 900)}},
                "t3": {"symbol": "C", "rows": {W1: row(-20, 100), W2: row(-40, 500)}}}

    def test_qualifies_repeat_winners_not_snipers_contracts_or_one_offs(self):
        ranked = scout.rank(self.tokens(), cfg(), kinds={W1: "eoa", BOT: "contract"})
        by = {r["wallet"]: r for r in ranked}
        self.assertTrue(by[W1]["qualified"])                                  # 3 runners, won 2, +$780
        self.assertEqual((by[W1]["runners"], by[W1]["wins"], by[W1]["realized_usd"]), (3, 2, 780.0))
        self.assertFalse(by[W2]["qualified"])                                 # won 1 of 3
        self.assertIn("won 1 of 3", by[W2]["why_not"])
        self.assertIn("sniper", by[SNIPER]["why_not"])
        self.assertIn("contract", by[BOT]["why_not"])
        self.assertEqual(ranked[0]["wallet"], W1)


class CycleTests(unittest.TestCase):
    def test_cycle_scans_runners_checks_contracts_and_picks_live_wallets(self):
        now = time.time()

        class Rpc:
            def block_number(self):
                return 10_000

            def call(self, method, params=None):
                if method == "eth_getBlockByNumber":
                    n = int(params[0], 16)
                    return {"timestamp": hex(int(now - (10_000 - n) * 0.1))}
                if method == "eth_getCode":
                    return "0x6080" if params[0] == BOT else "0x"
                raise AssertionError(method)

            def erc20_decimals(self, token):
                return 0

            def logs(self, a, b, topics, address=None):
                def log(blk, frm, to, amount):
                    return {"blockNumber": hex(blk), "topics": [scout.TRANSFER_TOPIC, "0x" + frm[2:].rjust(64, "0"),
                                                               "0x" + to[2:].rjust(64, "0")], "data": hex(amount)}
                peers = [("0x" + "8" * 39 + str(i)) for i in range(3)]
                rows = [log(9_000, POOL, W1, 100), log(9_900, W1, POOL, 100), log(9_000, POOL, BOT, 100), log(9_900, BOT, POOL, 100)]
                rows += [log(9_001, POOL, p, 1) for p in peers]
                return [r for r in rows if a <= int(r["blockNumber"], 16) <= b]

        def http_get(url):
            if "ohlcv" in url:
                # $1 at the buys (block 9_000, 100 s ago), $3 at the sells (block 9_900, 10 s ago)
                return {"data": {"attributes": {"ohlcv_list": [[now - 1000, 0, 0, 0, 1.0], [now - 60, 0, 0, 0, 3.0]]}}}
            return {"data": [gecko_pool(TOKEN, "MOON", 900_000, 5000, 1, now), gecko_pool("0x" + "d" * 40, "MOON2", 800_000, 900, 1, now,
                                                                                          pool="0x" + "4" * 40)]}

        tmp = Path(tempfile.mkdtemp(prefix="evm-scout-"))
        c = cfg(EVM_SCOUT_MIN_RUNNERS="2", EVM_SCOUT_MIN_WINS="2", EVM_SCOUT_GECKO_PAUSE_SECONDS="0")
        s = scout.Scout(c, Rpc(), tmp / "evm_scout.json", log=lambda m: None, http_get=http_get, clock=lambda: now)
        summary = s.run_cycle()
        self.assertEqual(summary["scanned"], 2)                              # both runners (the fake serves the same logs)
        self.assertEqual(s.live_wallets(), [scout.to_checksum_address(W1)])  # BOT is a contract
        self.assertEqual(s.state["kinds"][BOT], "contract")
        self.assertTrue((tmp / "evm_scout.json").exists())
        self.assertEqual(s.run_cycle()["scanned"], 0)                        # rescanned only after EVM_SCOUT_RESCAN_HOURS
        again = scout.Scout(c, Rpc(), tmp / "evm_scout.json", log=lambda m: None, http_get=http_get, clock=lambda: now)
        self.assertEqual(again.live_wallets(), [scout.to_checksum_address(W1)])   # survives a restart
        self.assertIn("MOON", str(again.report()["runners"]))


class LaneTests(unittest.TestCase):
    def test_lane_copies_scouted_wallets_on_the_scout_chain_from_their_own_minimum(self):
        lane, ln, rpc, price, p = test_evm.make_lane(EVM_SCOUT_CHAIN="base", EVM_SCOUT_MIN_BUY_USD="100", COPY_MIN_BUY_USD="300")
        self.addCleanup(p.stop)
        self.assertIsNotNone(ln.scout)
        found = scout.to_checksum_address(W2)
        ln.scout.state["live"] = [found]
        self.assertEqual(ln.wallets_for("base"), [test_evm.WALLET, found])
        self.assertEqual(ln.min_buy_usd(found), 100.0)
        self.assertEqual(ln.min_buy_usd(test_evm.WALLET), 300.0)
        # A $150 first buy by the scouted wallet is mirrored; the configured wallet's would not be.
        rpc.logs_by_block = [test_evm.transfer_log(test_evm.TOKEN, test_evm.OTHER, found, 50_000 * 10**18, 999, "0xscouted")]
        ln.poll_wallets()
        self.assertEqual([pos["copy"] for pos in ln.state["positions"]], [found])

    def test_scout_off_or_on_another_chain_changes_nothing(self):
        lane, ln, rpc, price, p = test_evm.make_lane(EVM_SCOUT="0")
        self.addCleanup(p.stop)
        self.assertIsNone(ln.scout)
        self.assertEqual(ln.wallets_for("base"), [test_evm.WALLET])
        lane2, ln2, _, _, p2 = test_evm.make_lane(EVM_SCOUT_CHAIN="robinhood")      # lane runs base only
        self.addCleanup(p2.stop)
        self.assertIsNone(ln2.scout)

    def test_robinhood_block_time_and_evm_sizing_defaults(self):
        from evm import chains
        self.assertEqual(chains.DEFAULTS["robinhood"].block_seconds, 0.1)
        lane, ln, rpc, price, p = test_evm.make_lane()
        self.addCleanup(p.stop)
        self.assertEqual((ln.cfg.max_position_usd, ln.cfg.daily_loss_limit_usd), (5.0, 25.0))


class V4RouterTests(unittest.TestCase):
    TOK = "0x" + "c" * 40
    HOOK = "0x" + "e" * 40

    def router(self, quotes):
        """A Robinhood router over a fake RPC: two native pools for TOK; the quoter answers
        from `quotes` keyed by fee (None: that pool reverts)."""
        from eth_abi import decode, encode
        from evm import chains, router as rmod
        chain = chains.load_chains(["robinhood"])["robinhood"]
        test = self

        class Rpc:
            def __init__(self):
                self.calls, self.log_queries = [], []
            def block_number(self):
                return 50_000_000
            def logs(self, a, b, topics, address=None):
                self.log_queries.append((a, b, topics, address))
                def init(fee, spacing, hooks):
                    return {"data": "0x" + encode(["uint24", "int24", "address", "uint160", "int24"], [fee, spacing, hooks, 2**96, 0]).hex()}
                return [init(0, 200, test.HOOK), init(3000, 60, "0x" + "0" * 40)]
            def eth_call(self, to, data, value=0, sender=None):
                self.calls.append((to, data))
                if to == chain.v4_quoter:
                    key, zf1, amount, _ = decode(["(" + rmod.V4_POOL_KEY + ",bool,uint128,bytes)"], bytes.fromhex(data[10:]))[0]
                    out = quotes.get(key[2])
                    if out is None:
                        raise RpcError("execution reverted")
                    return "0x" + encode(["uint256", "uint256"], [out, 0]).hex()
                if to == chain.permit2:
                    return "0x" + encode(["uint160", "uint48", "uint48"], [0, 0, 0]).hex()
                raise RpcError("no route")
            def erc20_allowance(self, token, owner, spender):
                return 0
            def call(self, method, params=None):
                self.calls.append((method, params))
                return "0x"
        rpc = Rpc()
        sent = []
        r = rmod.Router(chain, rpc, "0x" + "9" * 40, send=lambda tx: sent.append(tx) or {"status": "0x1"})
        return r, rpc, sent, chain, rmod

    def test_v4_quote_picks_the_best_native_pool_and_caches_pools(self):
        r, rpc, sent, chain, rmod = self.router({0: 5_000, 3000: 7_000})
        q = r.quote(scout.ZERO_TOPIC_ADDR, self.TOK, 10**15)
        self.assertEqual((q.kind, q.amount_out, q.detail["key"][2], q.detail["zero_for_one"]), ("v4", 7_000, 3000, True))
        a, b, topics, address = rpc.log_queries[0]
        self.assertEqual(address, chain.v4_pool_manager)
        self.assertEqual(topics[2], "0x" + "0" * 64)                       # native ETH is always currency0
        self.assertEqual(b - a, 10_000_000)
        r.quote(scout.ZERO_TOPIC_ADDR, self.TOK, 10**15)
        self.assertEqual(len(rpc.log_queries), 1)                          # pools cached
        sell = r.quote(self.TOK, scout.ZERO_TOPIC_ADDR, 7_000)
        self.assertFalse(sell.detail["zero_for_one"])
        r2, *_ = self.router({0: None, 3000: None})                        # every pool reverts: no route
        self.assertIsNone(r2.quote(scout.ZERO_TOPIC_ADDR, self.TOK, 10**15))

    def test_v4_buy_is_simulated_then_sent_with_the_eth_attached(self):
        from eth_abi import decode
        r, rpc, sent, chain, rmod = self.router({0: 10_000, 3000: 1})
        q = r.quote(scout.ZERO_TOPIC_ADDR, self.TOK, 10**15)
        r.execute(q)
        self.assertEqual([c[0] for c in rpc.calls if c[0] == "eth_call"], ["eth_call"])   # simulated once
        self.assertEqual(len(sent), 1)
        tx = sent[0]
        self.assertEqual((tx["to"], tx["value"]), (chain.universal_router, 10**15))
        commands, inputs, _ = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(tx["data"][10:]))
        self.assertEqual(commands, bytes([rmod.UR_V4_SWAP]))
        actions, params = decode(["bytes", "bytes[]"], inputs[0])
        self.assertEqual(actions, bytes([rmod.V4_SWAP_EXACT_IN_SINGLE, rmod.V4_SETTLE_ALL, rmod.V4_TAKE_ALL]))
        swap = decode(["(" + rmod.V4_POOL_KEY + ",bool,uint128,uint128,bytes)"], params[0])[0]
        self.assertEqual((swap[0][4].lower(), swap[1], swap[2], swap[3]), (self.HOOK, True, 10**15, 9_000))   # 10% slippage

    def test_v4_sell_approves_permit2_and_the_router_first(self):
        r, rpc, sent, chain, rmod = self.router({0: 10_000, 3000: 1})
        q = r.quote(self.TOK, scout.ZERO_TOPIC_ADDR, 5_000)
        r.execute(q)
        self.assertEqual([tx["to"] for tx in sent], [self.TOK, chain.permit2, chain.universal_router])
        self.assertEqual(sent[-1]["value"], 0)

    def test_a_failed_simulation_sends_nothing(self):
        r, rpc, sent, chain, rmod = self.router({0: 10_000, 3000: 1})
        rpc.call = lambda method, params=None: (_ for _ in ()).throw(RpcError("execution reverted: hook"))
        q = r.quote(scout.ZERO_TOPIC_ADDR, self.TOK, 10**15)
        with self.assertRaises(RpcError):
            r.execute(q)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
