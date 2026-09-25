"""Minimal JSON-RPC client plus the handful of ABI helpers the lane needs (no web3 dependency:
eth-account signs, eth-abi encodes)."""
from __future__ import annotations

import time
from typing import Any

import requests
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import function_signature_to_4byte_selector, keccak, to_checksum_address

TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


class RpcError(RuntimeError):
    pass


def selector(signature: str) -> bytes:
    return function_signature_to_4byte_selector(signature)


def encode_call(signature: str, types: list[str], args: list[Any]) -> str:
    return "0x" + (selector(signature) + abi_encode(types, args)).hex()


def pad_address(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def topic_address(topic: str) -> str:
    return to_checksum_address("0x" + topic[-40:])


class Rpc:
    """JSON-RPC over one or more endpoints (comma-separated). A rate-limited or failing
    endpoint hands over to the next one and stays demoted until the others fail too."""
    def __init__(self, url: str, timeout: float = 20.0) -> None:
        self.urls = [u.strip() for u in url.split(",") if u.strip()]
        self.url = self.urls[0]
        self.timeout = timeout
        self.session = requests.Session()
        self._id = 0
        self._active = 0

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        last: Exception | None = None
        attempts = max(3, len(self.urls) * 2)
        for attempt in range(attempts):
            url = self.urls[self._active % len(self.urls)]
            try:
                resp = self.session.post(url, json=body, timeout=self.timeout)
                if resp.status_code == 429:
                    raise RpcError("429 rate limited")
                resp.raise_for_status()
                data = resp.json()
                if data.get("error"):
                    err = data["error"]
                    message = str(err.get("message") or err)
                    # "logs matched by query exceeds limit of 10000" is a too-wide query, not a
                    # rate limit: it is raised to the caller, which narrows the range.
                    low = message.lower()
                    if "rate" in low or "too many requests" in low or err.get("code") == 429 or (
                            "limit" in low and "exceed" not in low and "logs" not in low):
                        raise RpcError("429 rate limited")
                    raise RpcError(f"{method}: {message}")
                return data.get("result")
            except RpcError as exc:
                if "rate limited" not in str(exc):
                    raise
                last = exc
            except (requests.RequestException, ValueError) as exc:
                last = exc
            self._active += 1                      # next endpoint (or the same one, later)
            if len(self.urls) == 1 or attempt % len(self.urls) == len(self.urls) - 1:
                time.sleep(0.5 * (attempt + 1))
        raise RpcError(f"{method} failed: {last}")

    # ---- reads -----------------------------------------------------------
    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def balance(self, address: str) -> int:
        return int(self.call("eth_getBalance", [address, "latest"]), 16)

    def eth_call(self, to: str, data: str, value: int = 0, sender: str | None = None) -> str:
        tx: dict[str, Any] = {"to": to, "data": data}
        if value:
            tx["value"] = hex(value)
        if sender:
            tx["from"] = sender
        return self.call("eth_call", [tx, "latest"])

    def erc20_balance(self, token: str, owner: str) -> int:
        out = self.eth_call(token, encode_call("balanceOf(address)", ["address"], [owner]))
        return int(out, 16) if out and out != "0x" else 0

    def erc20_decimals(self, token: str) -> int:
        out = self.eth_call(token, "0x" + selector("decimals()").hex())
        return int(out, 16) if out and out != "0x" else 18

    def erc20_symbol(self, token: str) -> str:
        try:
            out = self.eth_call(token, "0x" + selector("symbol()").hex())
            raw = bytes.fromhex(out[2:])
            try:
                return abi_decode(["string"], raw)[0][:16]
            except Exception:
                return raw.rstrip(b"\x00").decode("utf-8", "ignore")[:16] or token[:8]
        except Exception:
            return token[:8]

    def erc20_allowance(self, token: str, owner: str, spender: str) -> int:
        out = self.eth_call(token, encode_call("allowance(address,address)", ["address", "address"], [owner, spender]))
        return int(out, 16) if out and out != "0x" else 0

    def logs(self, from_block: int, to_block: int, topics: list[Any], address: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": topics}
        if address:
            params["address"] = address
        return self.call("eth_getLogs", [params]) or []

    def transaction(self, tx_hash: str) -> dict[str, Any] | None:
        return self.call("eth_getTransactionByHash", [tx_hash])

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        return self.call("eth_getTransactionReceipt", [tx_hash])

    # ---- writes ----------------------------------------------------------
    def fees(self) -> tuple[int, int]:
        """(maxFeePerGas, maxPriorityFeePerGas) with headroom so a swap is not stuck behind a
        gas spike; L2s report a tiny priority fee, BNB a flat gas price."""
        gas_price = int(self.call("eth_gasPrice"), 16)
        try:
            priority = int(self.call("eth_maxPriorityFeePerGas"), 16)
        except Exception:
            priority = min(gas_price, 10**8)
        priority = min(priority, gas_price)
        return int(gas_price * 1.5) + priority, priority

    def estimate_gas(self, tx: dict[str, Any]) -> int:
        out = self.call("eth_estimateGas", [tx])
        return int(out, 16)

    def nonce(self, address: str) -> int:
        return int(self.call("eth_getTransactionCount", [address, "pending"]), 16)

    def send_raw(self, raw: bytes | str) -> str:
        data = raw if isinstance(raw, str) else "0x" + raw.hex()
        return self.call("eth_sendRawTransaction", [data])

    def wait_receipt(self, tx_hash: str, timeout: float = 90.0, poll: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rcpt = self.receipt(tx_hash)
            if rcpt and rcpt.get("blockNumber"):
                return rcpt
            time.sleep(poll)
        raise RpcError(f"transaction {tx_hash} not confirmed within {timeout:.0f}s")


def transfers_in_receipt(receipt: dict[str, Any], wallet: str) -> dict[str, int]:
    """Net ERC-20 balance change of `wallet` per token in a receipt's Transfer logs."""
    wallet_topic = pad_address(wallet)
    deltas: dict[str, int] = {}
    for entry in receipt.get("logs") or []:
        topics = entry.get("topics") or []
        if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        token = to_checksum_address(entry["address"])
        try:
            amount = int(entry.get("data") or "0x0", 16)
        except ValueError:
            continue
        if topics[2].lower() == wallet_topic:
            deltas[token] = deltas.get(token, 0) + amount
        if topics[1].lower() == wallet_topic:
            deltas[token] = deltas.get(token, 0) - amount
    return {t: d for t, d in deltas.items() if d}
