"""Signing wallet. EVM_PRIVATE_KEY accepts either a hex private key (what MetaMask's "Show
private key" gives) or a 12/24-word recovery phrase; the phrase derives the first account
(m/44'/60'/0'/0/0), which is the account MetaMask and Phantom show first."""
from __future__ import annotations

from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount


def load_account(secret: str) -> LocalAccount:
    secret = (secret or "").strip()
    if not secret:
        raise SystemExit("EVM_PRIVATE_KEY is empty")
    words = secret.split()
    if len(words) >= 12:
        Account.enable_unaudited_hdwallet_features()
        return Account.from_mnemonic(" ".join(words))
    if not secret.startswith("0x"):
        secret = "0x" + secret
    return Account.from_key(secret)


def sign_permit(account: LocalAccount, permit_data: dict[str, Any]) -> str:
    """EIP-712 signature for the Trading API's Permit2 `permitData` ({domain, types, values})."""
    types = {k: v for k, v in (permit_data.get("types") or {}).items() if k != "EIP712Domain"}
    signed = account.sign_typed_data(permit_data.get("domain") or {}, types, permit_data.get("values") or {})
    return "0x" + signed.signature.hex() if not signed.signature.hex().startswith("0x") else signed.signature.hex()


def sign_and_serialize(account: LocalAccount, tx: dict[str, Any]) -> bytes:
    signed = account.sign_transaction(tx)
    return bytes(signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction)
