"""EVM copy lane: decode followed wallets' ERC-20 transfers into buys and sells, size and
mirror them, phase out on the ladder, rotate when full, and reuse state across restarts."""
import importlib
import os
import tempfile
import unittest
from unittest import mock

from eth_utils import keccak

SOL_PRICE_ETH = 3000.0
WALLET = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


def pad(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def transfer_log(token, frm, to, amount, block, tx):
    return {"address": token, "topics": [TRANSFER, pad(frm), pad(to)], "data": hex(amount), "blockNumber": hex(block), "transactionHash": tx}


class FakeRpc:
    def __init__(self):
        self.head = 1000
        self.logs_by_block = []            # list of logs
        self.balances = {WALLET: int(1e18)}
        self.token_balances = {}           # (owner) -> token balance; anyone else holds plenty

    def block_number(self):
        return self.head

    def balance(self, address):
        return self.balances.get(address, 0)

    def logs(self, from_block, to_block, topics, address=None):
        out = []
        for entry in self.logs_by_block:
            blk = int(entry["blockNumber"], 16)
            if not from_block <= blk <= to_block:
                continue
            ok = True
            for i, t in enumerate(topics):
                if t is not None and entry["topics"][i].lower() != t.lower():
                    ok = False
            if ok:
                out.append(entry)
        return out

    def erc20_symbol(self, token):
        return "TOK"

    def erc20_decimals(self, token):
        return 18

    def erc20_balance(self, token, owner):
        return self.token_balances.get(owner, 10**30)


class FakeRouter:
    """Prices in native wei: `price` native per token (token has 18 decimals)."""
    def __init__(self, price):
        self.price = price
        self.last_api_error = ""

    def quote(self, token_in, token_out, amount_in):
        from evm.router import Quote
        if token_in == "0x0000000000000000000000000000000000000000":
            out = int(amount_in / self.price["v"])
        else:
            out = int(amount_in * self.price["v"] * self.price.get("rt", 1.0))
        return Quote("fake", token_in, token_out, amount_in, out)

    def native_price_usd(self):
        return SOL_PRICE_ETH


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-evm-test-")
    base = {"DATA_DIR": tmp, "EXECUTOR_MODE": "paper", "EVM_COPY_WALLETS": WALLET, "EVM_CHAINS": "base",
            "PAPER_BALANCE_USD": "200", "COPY_MIN_BUY_USD": "50"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import evm.lane as lane
    importlib.reload(lane)
    return lane, patcher


def make_lane(**env):
    lane, patcher = fresh(**env)
    ln = lane.Lane(lane.Config())
    rpc = FakeRpc()
    ln.rpcs["base"] = rpc
    price = {"v": 1e-6}                    # 1 token = 0.000001 ETH  ($0.003)
    ln.routers["base"] = FakeRouter(price)
    ln._price_cache["base"] = (lane.now_ts(), SOL_PRICE_ETH)
    return lane, ln, rpc, price, patcher


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.lane, self.ln, self.rpc, self.price, p = make_lane()
        self.addCleanup(p.stop)

    def test_first_poll_scans_only_the_recent_window_then_mirrors_new_buys(self):
        # 100 tokens received long ago (block 100) and 100_000 tokens just now (block 999)
        self.rpc.logs_by_block = [transfer_log(TOKEN, OTHER, WALLET, 100 * 10**18, 100, "0xold"),
                                  transfer_log(TOKEN, OTHER, WALLET, 100_000 * 10**18, 999, "0xnew")]
        self.ln.poll_wallets()
        self.assertEqual([p["symbol"] for p in self.ln.state["positions"]], ["TOK"])
        pos = self.ln.state["positions"][0]
        self.assertEqual(pos["copy"], WALLET)
        self.assertAlmostEqual(pos["copy_buy_usd"], 300)          # 100_000 * 0.000001 ETH * $3000
        self.assertAlmostEqual(pos["position_usd"], 16.0)          # 8% of $200 paper equity
        self.assertIn("0xnew", self.ln.state["copy_seen"]["base"])
        self.assertNotIn("0xold", self.ln.state["copy_seen"]["base"])
        self.ln._last_poll = 0
        self.ln.poll_wallets()                                     # nothing new: no duplicate
        self.assertEqual(len(self.ln.state["positions"]), 1)

    def test_small_buys_and_wrapped_native_are_ignored(self):
        self.rpc.logs_by_block = [transfer_log(TOKEN, OTHER, WALLET, 1_000 * 10**18, 999, "0xsmall"),   # $3
                                  transfer_log(self.ln.chains["base"].wrapped_native, OTHER, WALLET, 10**18, 999, "0xweth")]
        self.ln.poll_wallets()
        self.assertEqual(self.ln.state["positions"], [])

    def test_followed_sell_closes_our_position(self):
        self.rpc.logs_by_block = [transfer_log(TOKEN, OTHER, WALLET, 100_000 * 10**18, 999, "0xbuy")]
        self.ln.poll_wallets()
        self.assertEqual(len(self.ln.state["positions"]), 1)
        self.rpc.head = 1005
        self.rpc.token_balances[WALLET] = 0                            # sold the whole stack
        self.rpc.logs_by_block.append(transfer_log(TOKEN, WALLET, OTHER, 100_000 * 10**18, 1004, "0xsell"))
        self.ln._last_poll = 0
        self.ln.poll_wallets()
        self.assertEqual(self.ln.state["positions"], [])
        self.assertEqual(self.ln.state["bags"][0]["symbol"], "TOK")   # moon bag kept

    def test_followed_partial_sell_trims_ours_by_the_same_share(self):
        self.rpc.logs_by_block = [transfer_log(TOKEN, OTHER, WALLET, 100_000 * 10**18, 999, "0xbuy")]
        self.ln.poll_wallets()
        pos = self.ln.state["positions"][0]
        tokens_before = int(pos["tokens"])
        pos["last_value_usd"] = 20.0
        self.rpc.head = 1005
        self.rpc.token_balances[WALLET] = 50_000 * 10**18                # half is left: a 50% trim
        self.rpc.logs_by_block.append(transfer_log(TOKEN, WALLET, OTHER, 50_000 * 10**18, 1004, "0xsell"))
        self.ln._last_poll = 0
        self.ln.poll_wallets()
        self.assertEqual(len(self.ln.state["positions"]), 1)
        self.assertEqual(int(self.ln.state["positions"][0]["tokens"]), tokens_before // 2)


class ExitTests(unittest.TestCase):
    def setUp(self):
        self.lane, self.ln, self.rpc, self.price, p = make_lane(COPY_LADDER="2:40,3:30,5:30")   # price steps assume these rungs
        self.addCleanup(p.stop)
        self.rpc.logs_by_block = [transfer_log(TOKEN, OTHER, WALLET, 100_000 * 10**18, 999, "0xbuy")]
        self.ln.poll_wallets()
        self.pos = self.ln.state["positions"][0]

    def test_ladder_phases_out_then_closes(self):
        entry_tokens = self.pos["tokens"]
        self.price["v"] *= 2.1
        self.ln._last_manage = 0
        self.ln.manage_positions()
        self.assertAlmostEqual(self.pos["tokens"], entry_tokens * 0.6, delta=entry_tokens * 1e-9)
        self.assertTrue(self.pos["ladder"][0]["done"])
        self.price["v"] *= 3                       # 6.3x: top rung closes the rest
        self.ln._last_manage = 0
        self.ln.manage_positions()
        self.assertEqual(self.ln.state["positions"], [])
        self.assertGreater(self.ln.state["daily"]["realized_pnl_usd"], 0)

    def test_stop_loss(self):
        self.price["v"] *= 0.6
        self.ln._last_manage = 0
        self.ln.manage_positions()
        self.assertEqual(self.ln.state["positions"], [])
        self.assertLess(self.ln.state["daily"]["realized_pnl_usd"], 0)

    def test_round_trip_guard_blocks_taxed_or_thin_pools(self):
        self.price["rt"] = 0.5                     # selling only returns half
        self.rpc.head = 1010
        self.rpc.logs_by_block.append(transfer_log(OTHER, WALLET, WALLET, 100_000 * 10**18, 1009, "0xbuy2"))
        self.rpc.logs_by_block[-1]["topics"][1] = pad("0x4444444444444444444444444444444444444444")
        self.ln._last_poll = 0
        self.ln.poll_wallets()
        self.assertEqual(len(self.ln.state["positions"]), 1)     # second token skipped


class RotateAndRestartTests(unittest.TestCase):
    def test_full_book_rotates_oldest_and_state_survives_restart(self):
        lane, ln, rpc, price, p = make_lane(MAX_CONCURRENT_POSITIONS="1", COPY_ROTATE="1")
        self.addCleanup(p.stop)
        rpc.logs_by_block = [transfer_log(TOKEN, OTHER, WALLET, 100_000 * 10**18, 999, "0xbuy")]
        ln.poll_wallets()
        rpc.head = 1010
        rpc.logs_by_block.append(transfer_log("0x5555555555555555555555555555555555555555", OTHER, WALLET, 100_000 * 10**18, 1009, "0xbuy2"))
        ln._last_poll = 0
        ln.poll_wallets()
        self.assertEqual([p_["token"][:6] for p_ in ln.state["positions"]], ["0x5555"])
        ln.save_state()
        again = lane.Lane(lane.Config())                            # restart: same DATA_DIR
        self.assertEqual([p_["token"][:6] for p_ in again.state["positions"]], ["0x5555"])
        self.assertEqual(again.state["last_block"]["base"], 1010)

    def test_copy_only_defaults_and_wallet_parsing(self):
        lane, p = fresh(EVM_COPY_WALLETS=f"{WALLET}:500, {OTHER}\n{TOKEN}")
        self.addCleanup(p.stop)
        cfg = lane.Config()
        self.assertEqual(len(cfg.wallets), 3)
        self.assertEqual(cfg.wallet_min_usd, {WALLET: 500.0})
        self.assertEqual(cfg.mode, "paper")
        self.assertEqual([(r["x"], r["pct"]) for r in cfg.copy_ladder], [(1.4, 40.0), (1.8, 30.0), (3.0, 30.0)])


class WalletTests(unittest.TestCase):
    def test_key_or_phrase(self):
        from evm.wallet import load_account
        phrase = "test test test test test test test test test test test junk"
        self.assertEqual(load_account(phrase).address, "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266")
        key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        self.assertEqual(load_account(key).address, "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266")
        self.assertEqual(load_account(key[2:]).address, "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266")


class EndpointTests(unittest.TestCase):
    def test_rate_limited_endpoint_hands_over_to_the_next(self):
        from evm.rpc import Rpc
        rpc = Rpc("https://a.example,https://b.example")
        calls = []

        class Resp:
            def __init__(self, status, payload):
                self.status_code, self._payload, self.content = status, payload, b"x"
            def raise_for_status(self):
                pass
            def json(self):
                return self._payload

        def post(url, json=None, timeout=None):
            calls.append(url)
            if url.startswith("https://a"):
                return Resp(429, {})
            return Resp(200, {"result": "0x10"})
        rpc.session.post = post
        self.assertEqual(rpc.call("eth_blockNumber"), "0x10")
        self.assertEqual(calls, ["https://a.example", "https://b.example"])
        self.assertEqual(rpc.call("eth_blockNumber"), "0x10")
        self.assertEqual(calls[-1], "https://b.example")           # stays on the working one

    def test_alchemy_key_from_solana_rpc_url_serves_base_and_bnb(self):
        from evm import chains
        with mock.patch.dict(os.environ, {"RPC_URL": "https://solana-mainnet.g.alchemy.com/v2/abcdefgh1234", "ALCHEMY_API_KEY": ""}):
            loaded = chains.load_chains(["robinhood", "base", "bnb"])
        self.assertTrue(loaded["base"].rpc_url.startswith("https://base-mainnet.g.alchemy.com/v2/abcdefgh1234,"))
        self.assertTrue(loaded["bnb"].rpc_url.startswith("https://bnb-mainnet.g.alchemy.com/v2/abcdefgh1234,"))
        self.assertEqual(loaded["robinhood"].rpc_url, "https://rpc.mainnet.chain.robinhood.com")
        with mock.patch.dict(os.environ, {"RPC_URL": "", "ALCHEMY_API_KEY": "", "EVM_BASE_RPC_URL": "https://my.base"}):
            self.assertEqual(chains.load_chains(["base"])["base"].rpc_url, "https://my.base")


class ReceiptTests(unittest.TestCase):
    def test_transfers_in_receipt_nets_our_balance_changes(self):
        from evm.rpc import transfers_in_receipt
        receipt = {"logs": [transfer_log(TOKEN, OTHER, WALLET, 500, 1, "0x1"), transfer_log(TOKEN, WALLET, OTHER, 200, 1, "0x1"),
                            transfer_log(OTHER, OTHER, "0x9999999999999999999999999999999999999999", 7, 1, "0x1")]}
        self.assertEqual(transfers_in_receipt(receipt, WALLET), {"0x2222222222222222222222222222222222222222": 300})


if __name__ == "__main__":
    unittest.main()
