"""Quotes and swaps on one chain.

Two backends: the Uniswap Trading API (UNISWAP_API_KEY set) routes through Uniswap V2/V3/V4
including the V4 pools memecoins graduate into on Robinhood Chain; without a key the lane
falls back to the chain's on-chain V3 QuoterV2/SwapRouter02 and V2 router, which covers
Base and BNB well and Robinhood Chain only for tokens with V2/V3 pools."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from eth_abi import decode as abi_decode, encode as abi_encode

from .chains import ZERO, Chain
from .rpc import Rpc, RpcError, encode_call, selector

TRADING_API = "https://trade-api.gateway.uniswap.org/v1"
ADDRESS_THIS = "0x0000000000000000000000000000000000000002"   # SwapRouter02: "send to the router"
MAX_UINT = 2**256 - 1


@dataclass
class Quote:
    kind: str                 # "api" | "v3" | "v2"
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    detail: dict[str, Any] = field(default_factory=dict)


class Router:
    def __init__(self, chain: Chain, rpc: Rpc, wallet: str, api_key: str = "", slippage_pct: float = 10.0,
                 send: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 sign_permit: Callable[[dict[str, Any]], str] | None = None) -> None:
        self.chain = chain
        self.rpc = rpc
        self.wallet = wallet
        self.api_key = api_key
        self.slippage_pct = slippage_pct
        self.send = send                      # lane-provided: sign, broadcast, wait; returns receipt
        self.sign_permit = sign_permit        # lane-provided: EIP-712 signature for Permit2 data
        self.session = requests.Session()
        self.last_api_error = ""
        self._approved: set[str] = set()

    # ---- quotes ----------------------------------------------------------
    def quote(self, token_in: str, token_out: str, amount_in: int) -> Quote | None:
        """Best available quote; `token_in`/`token_out` of ZERO mean the native coin."""
        if amount_in <= 0:
            return None
        best: Quote | None = None
        if self.api_key:
            try:
                best = self._api_quote(token_in, token_out, amount_in)
            except Exception as exc:                       # fall through to on-chain routing
                best = None
                self.last_api_error = str(exc)
        for fn in (self._v3_quote, self._v2_quote):
            try:
                q = fn(token_in, token_out, amount_in)
            except Exception:
                q = None
            if q and (best is None or q.amount_out > best.amount_out):
                best = q
        return best

    def _api_headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Content-Type": "application/json", "Accept": "application/json",
                "x-universal-router-version": "2.0",
                "x-agent-info": '{"integration_name":"mememe-copy-lane","decision_origin":"autonomous","version":"1.0"}'}

    def _api_quote(self, token_in: str, token_out: str, amount_in: int) -> Quote | None:
        body = {"swapper": self.wallet, "tokenIn": token_in, "tokenOut": token_out,
                "tokenInChainId": str(self.chain.chain_id), "tokenOutChainId": str(self.chain.chain_id),
                "amount": str(amount_in), "type": "EXACT_INPUT", "slippageTolerance": self.slippage_pct,
                "routingPreference": "CLASSIC"}
        resp = self.session.post(f"{TRADING_API}/quote", json=body, headers=self._api_headers(), timeout=15)
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            raise RpcError(f"trading api quote {resp.status_code}: {str(data.get('detail') or data)[:160]}")
        if data.get("routing") not in ("CLASSIC", "WRAP", "UNWRAP"):
            raise RpcError(f"trading api routing {data.get('routing')} unsupported here")
        out = int(data["quote"]["output"]["amount"])
        return Quote("api", token_in, token_out, amount_in, out, {"response": data})

    def _wrapped(self, token: str) -> str:
        return self.chain.wrapped_native if token == ZERO else token

    def _v3_quote(self, token_in: str, token_out: str, amount_in: int) -> Quote | None:
        if not self.chain.v3_quoter:
            return None
        tin, tout = self._wrapped(token_in), self._wrapped(token_out)
        best: Quote | None = None
        for fee in self.chain.v3_fees:
            data = encode_call("quoteExactInputSingle((address,address,uint256,uint24,uint160))",
                               ["(address,address,uint256,uint24,uint160)"], [(tin, tout, amount_in, fee, 0)])
            try:
                out = self.rpc.eth_call(self.chain.v3_quoter, data)
                amount_out = abi_decode(["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(out[2:]))[0]
            except Exception:
                continue
            if amount_out > 0 and (best is None or amount_out > best.amount_out):
                best = Quote("v3", token_in, token_out, amount_in, int(amount_out), {"fee": fee})
        return best

    def _v2_quote(self, token_in: str, token_out: str, amount_in: int) -> Quote | None:
        if not self.chain.v2_router:
            return None
        path = [self._wrapped(token_in), self._wrapped(token_out)]
        data = encode_call("getAmountsOut(uint256,address[])", ["uint256", "address[]"], [amount_in, path])
        out = self.rpc.eth_call(self.chain.v2_router, data)
        amounts = abi_decode(["uint256[]"], bytes.fromhex(out[2:]))[0]
        if len(amounts) < 2 or amounts[-1] <= 0:
            return None
        return Quote("v2", token_in, token_out, amount_in, int(amounts[-1]), {"path": path})

    def native_price_usd(self) -> float | None:
        """USD per native coin from the chain's own wrapped-native/stable pool."""
        if not self.chain.stable:
            return None
        q = None
        for fn in (self._v3_quote, self._v2_quote):
            try:
                cand = fn(ZERO, self.chain.stable, 10**18)
            except Exception:
                cand = None
            if cand and (q is None or cand.amount_out > q.amount_out):
                q = cand
        if not q:
            return None
        return q.amount_out / 10 ** self.chain.stable_decimals

    # ---- execution -------------------------------------------------------
    def min_out(self, quote: Quote) -> int:
        return int(quote.amount_out * (1 - self.slippage_pct / 100))

    def execute(self, quote: Quote) -> dict[str, Any]:
        """Send the swap for `quote`; returns the receipt. Handles the token approval a sell
        needs (Permit2 for the Trading API, the router itself on-chain)."""
        if self.send is None:
            raise RpcError("router has no sender (paper mode)")
        if quote.kind == "api":
            return self._api_execute(quote)
        if quote.kind == "v3":
            return self._v3_execute(quote)
        return self._v2_execute(quote)

    def _approve(self, token: str, spender: str, amount: int) -> None:
        key = f"{token}:{spender}"
        if key in self._approved:
            return
        if self.rpc.erc20_allowance(token, self.wallet, spender) >= amount:
            self._approved.add(key)
            return
        data = encode_call("approve(address,uint256)", ["address", "uint256"], [spender, MAX_UINT])
        self.send({"to": token, "data": data, "value": 0})
        self._approved.add(key)

    def _api_execute(self, quote: Quote) -> dict[str, Any]:
        headers = self._api_headers()
        if quote.token_in != ZERO:
            resp = self.session.post(f"{TRADING_API}/check_approval", headers=headers, timeout=15,
                                     json={"walletAddress": self.wallet, "token": quote.token_in,
                                           "amount": str(quote.amount_in), "chainId": self.chain.chain_id})
            data = resp.json() if resp.content else {}
            if resp.status_code != 200:
                raise RpcError(f"trading api check_approval {resp.status_code}: {str(data.get('detail') or data)[:160]}")
            approval = data.get("approval")
            if approval:
                self.send({"to": approval["to"], "data": approval["data"], "value": int(approval.get("value") or 0)})
        response = dict(quote.detail["response"])
        permit_data = response.pop("permitData", None)
        response.pop("permitTransaction", None)
        body: dict[str, Any] = dict(response)
        if permit_data and isinstance(permit_data, dict):
            if self.sign_permit is None:
                raise RpcError("permit signature needed but no signer configured")
            body["permitData"] = permit_data
            body["signature"] = self.sign_permit(permit_data)
        resp = self.session.post(f"{TRADING_API}/swap", json=body, headers=headers, timeout=20)
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            raise RpcError(f"trading api swap {resp.status_code}: {str(data.get('detail') or data)[:160]}")
        swap = data.get("swap") or {}
        if not swap.get("data") or swap["data"] == "0x":
            raise RpcError("trading api returned an empty swap (quote expired?)")
        tx = {"to": swap["to"], "data": swap["data"], "value": int(swap.get("value") or 0)}
        if swap.get("gasLimit"):
            tx["gas"] = int(swap["gasLimit"])
        return self.send(tx)

    def _v3_execute(self, quote: Quote) -> dict[str, Any]:
        fee = quote.detail["fee"]
        deadline = int(time.time()) + 120
        if quote.token_in == ZERO:
            params = (self.chain.wrapped_native, quote.token_out, fee, self.wallet, quote.amount_in, self.min_out(quote), 0)
            data = encode_call("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
                               ["(address,address,uint24,address,uint256,uint256,uint160)"], [params])
            return self.send({"to": self.chain.v3_router, "data": data, "value": quote.amount_in})
        self._approve(quote.token_in, self.chain.v3_router, quote.amount_in)
        params = (quote.token_in, self.chain.wrapped_native, fee, ADDRESS_THIS, quote.amount_in, self.min_out(quote), 0)
        swap = selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))") + abi_encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"], [params])
        unwrap = selector("unwrapWETH9(uint256,address)") + abi_encode(["uint256", "address"], [self.min_out(quote), self.wallet])
        data = encode_call("multicall(uint256,bytes[])", ["uint256", "bytes[]"], [deadline, [swap, unwrap]])
        return self.send({"to": self.chain.v3_router, "data": data, "value": 0})

    def _v2_execute(self, quote: Quote) -> dict[str, Any]:
        deadline = int(time.time()) + 120
        path = quote.detail["path"]
        if quote.token_in == ZERO:
            data = encode_call("swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)",
                               ["uint256", "address[]", "address", "uint256"], [self.min_out(quote), path, self.wallet, deadline])
            return self.send({"to": self.chain.v2_router, "data": data, "value": quote.amount_in})
        self._approve(quote.token_in, self.chain.v2_router, quote.amount_in)
        data = encode_call("swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)",
                           ["uint256", "uint256", "address[]", "address", "uint256"],
                           [quote.amount_in, self.min_out(quote), path, self.wallet, deadline])
        return self.send({"to": self.chain.v2_router, "data": data, "value": 0})
