"""Brokers: the only place strategy intents become fills.

PaperBroker reproduces the idealized fills the replay uses. LiveBroker executes: Jupiter
swaps signed with WALLET_PRIVATE_KEY and Meteora DLMM positions through the Node sidecar
(mm/sidecar/server.js). Strategies never touch either directly, so replay, paper and live
share one accounting path."""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import MMConfig, WSOL
from .sources import HttpError, Sources

LAMPORTS = 1_000_000_000


def _f(row: dict[str, Any], key: str, default: float | None = None) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class LpFill:
    position_key: str
    lower_usd: float
    upper_usd: float
    deployed_usd: float          # value that went into the range (after opening swap)
    cost_usd: float              # swap fee + impact + transaction fees paid on the way in
    rent_sol: float = 0.0        # refundable position rent locked on chain
    token_amount: float = 0.0
    quote_usd: float = 0.0
    signatures: list[str] = field(default_factory=list)


@dataclass
class CloseFill:
    proceeds_usd: float          # what came back to the wallet in USD, after everything
    fees_usd: float              # swap fees claimed from the pool (already inside proceeds)
    cost_usd: float
    signatures: list[str] = field(default_factory=list)


@dataclass
class SpotFill:
    tokens: float
    value_usd: float             # paid (buy) or received (sell), after costs
    cost_usd: float
    signature: str = ""


class Broker:
    name = "base"

    def open_lp(self, row: dict[str, Any], size_usd: float, half_width: float, lower: float, upper: float) -> LpFill:
        raise NotImplementedError

    def close_lp(self, row: dict[str, Any], position: Any, price: float) -> CloseFill:
        raise NotImplementedError

    def buy(self, row: dict[str, Any], size_usd: float) -> SpotFill:
        raise NotImplementedError

    def sell(self, row: dict[str, Any], tokens: float, price: float) -> SpotFill:
        raise NotImplementedError

    def deployable_usd(self, requested: float) -> float:
        return requested


class PaperBroker(Broker):
    """Idealized fills at the snapshot's price with the snapshot's measured costs."""
    name = "paper"

    def __init__(self, cfg: MMConfig) -> None:
        self.cfg = cfg

    def _swap_fee_pct(self, row: dict[str, Any]) -> float:
        return _f(row, "dlmm_base_fee_pct") or self.cfg.momentum_fee_pct_each_side

    def _exit_cost_pct(self, row: dict[str, Any], size_usd: float) -> float:
        impact = _f(row, "impact_at_max_position_pct")
        if impact is None:
            return self.cfg.emergency_exit_impact_pct
        return impact * size_usd / self.cfg.max_position_usd + self._swap_fee_pct(row)

    def open_lp(self, row, size_usd, half_width, lower, upper):
        cfg = self.cfg
        setup = cfg.tx_fee_usd * cfg.lp_setup_transactions + size_usd * 0.5 * self._swap_fee_pct(row) / 100
        return LpFill("paper", lower, upper, size_usd - setup, setup)

    def close_lp(self, row, position, price):
        cfg = self.cfg
        token_value = position.rng.value(price) * position.token_fraction(price)
        cost = cfg.tx_fee_usd * 2 + token_value * self._exit_cost_pct(row, token_value) / 100
        return CloseFill(position.value(price) - cost, position.fees_earned, cost)

    def buy(self, row, size_usd):
        price = _f(row, "price_usd") or 0.0
        fee = size_usd * (self._swap_fee_pct(row) + (_f(row, "impact_at_max_position_pct") or 0)) / 100 + self.cfg.tx_fee_usd
        return SpotFill((size_usd - fee) / price if price else 0.0, size_usd, fee)

    def sell(self, row, tokens, price):
        gross = tokens * price
        fee = gross * self._exit_cost_pct(row, gross) / 100 + self.cfg.tx_fee_usd
        return SpotFill(tokens, gross - fee, fee)


class Signer:
    """solders-based signer for Jupiter's versioned transactions; constructed only in live mode."""

    def __init__(self, private_key_b58: str) -> None:
        from solders.keypair import Keypair
        self.keypair = Keypair.from_base58_string(private_key_b58)
        self.pubkey = str(self.keypair.pubkey())

    def sign(self, raw_tx: bytes) -> bytes:
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(raw_tx)
        return bytes(VersionedTransaction(tx.message, [self.keypair]))


class Sidecar:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _check(self, resp: requests.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError as exc:
            raise HttpError(f"sidecar returned non-JSON ({resp.status_code})") from exc
        if resp.status_code != 200 or body.get("error"):
            raise HttpError(f"sidecar: {body.get('error') or resp.status_code}")
        return body

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self._check(self.session.get(f"{self.base}{path}", params=params, timeout=self.timeout))

    def post(self, path: str, **payload: Any) -> dict[str, Any]:
        return self._check(self.session.post(f"{self.base}{path}", json=payload, timeout=self.timeout))

    def health(self) -> dict[str, Any]:
        return self.get("/health")


class LiveBroker(Broker):
    """Real fills. Buy/sell go through Jupiter; ranges go through the DLMM sidecar.

    Every method returns what actually happened (amounts read back from quotes, positions and
    balances), never what the model expected."""
    name = "live"

    def __init__(self, cfg: MMConfig, sources: Sources, signer: Signer, sidecar: Sidecar,
                 rpc_urls: tuple[str, ...] | None = None, log=print) -> None:
        self.cfg = cfg
        self.src = sources
        self.signer = signer
        self.sidecar = sidecar
        self.rpc_urls = list(rpc_urls or cfg.rpc_urls)
        self.log = log
        self.deployed_usd = 0.0
        self.draining = False            # set by the engine from the mm.stop flag: no new entries
        self._decimals: dict[str, int] = {}

    # -- chain helpers ------------------------------------------------------------
    def rpc(self, method: str, params: list[Any]) -> Any:
        last: Exception | None = None
        for url in self.rpc_urls:
            try:
                body = self.src.http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue
            if body.get("error"):
                last = HttpError(f"{method}: {body['error']}")
                continue
            return body.get("result")
        raise HttpError(f"{method} failed on every endpoint: {last}")

    def sol_balance(self) -> float:
        return int(self.rpc("getBalance", [self.signer.pubkey])["value"]) / LAMPORTS

    def token_balance_raw(self, mint: str) -> int:
        result = self.rpc("getTokenAccountsByOwner", [self.signer.pubkey, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0
        for acct in (result or {}).get("value") or []:
            info = acct["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
        return total

    def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            token = self.src.jupiter.token(mint)
            if not token or token.get("decimals") is None:
                info = self.src.rpc.mint_info(mint)
                if not info or info.get("decimals") is None:
                    raise HttpError(f"decimals unknown for {mint}")
                self._decimals[mint] = int(info["decimals"])
            else:
                self._decimals[mint] = int(token["decimals"])
        return self._decimals[mint]

    def sol_price(self) -> float:
        price = self.src.jupiter.sol_price()
        if not price:
            raise HttpError("SOL price unavailable")
        return price

    def send_and_confirm(self, signed: bytes, timeout_s: float = 90.0) -> str:
        signature = self.rpc("sendTransaction", [base64.b64encode(signed).decode(),
                                                  {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            statuses = self.rpc("getSignatureStatuses", [[signature]])["value"]
            status = statuses[0] if statuses else None
            if status:
                if status.get("err"):
                    raise HttpError(f"transaction {signature} failed on-chain: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return signature
            time.sleep(2)
        raise HttpError(f"transaction {signature} not confirmed within {timeout_s:.0f}s")

    def swap(self, input_mint: str, output_mint: str, amount_raw: int) -> tuple[int, str]:
        """Execute an ExactIn swap; returns (quoted out amount raw, signature)."""
        quote = self.src.jupiter.quote(input_mint, output_mint, amount_raw, slippage_bps=self.cfg.live_slippage_bps)
        if not quote or not quote.get("outAmount"):
            raise HttpError(f"no route for {input_mint[:6]} -> {output_mint[:6]}")
        for attempt in range(2):
            resp = self.src.http.session.post(
                f"{self.cfg.jupiter_base}/swap/v1/swap",
                json={"quoteResponse": quote, "userPublicKey": self.signer.pubkey, "wrapAndUnwrapSol": True,
                      "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto"},
                timeout=30,
            )
            if resp.status_code != 200:
                raise HttpError(f"swap build failed: {resp.status_code} {resp.text[:160]}")
            raw = base64.b64decode(resp.json()["swapTransaction"])
            try:
                signature = self.send_and_confirm(self.signer.sign(raw))
                return int(quote["outAmount"]), signature
            except HttpError as exc:
                if attempt == 0 and "BlockhashNotFound" in str(exc):
                    time.sleep(1)
                    continue
                raise
        raise HttpError("swap failed")  # pragma: no cover

    # -- guards --------------------------------------------------------------------
    def deployable_usd(self, requested: float) -> float:
        if self.draining:
            return 0.0
        remaining = self.cfg.bankroll_usd - self.deployed_usd
        try:
            balance_usd = (self.sol_balance() - self.cfg.gas_reserve_sol - self.cfg.lp_position_rent_sol) * self.sol_price()
        except Exception as exc:  # noqa: BLE001 - any doubt about the balance means no new exposure
            self.log(f"[live] balance check failed, refusing to deploy: {exc}")
            return 0.0
        return max(0.0, min(requested, remaining, balance_usd))

    # -- fills ---------------------------------------------------------------------
    def open_lp(self, row, size_usd, half_width, lower, upper):
        mint, pool = row["mint"], row.get("dlmm_pool")
        if not pool:
            raise HttpError("no DLMM pool")
        sol_price = self.sol_price()
        decimals = self.decimals(mint)
        half_lamports = int(size_usd / 2 / sol_price * LAMPORTS)
        # 1) buy the token half so the range can be seeded 50/50 around the active bin
        tokens_raw, buy_sig = self.swap(WSOL, mint, half_lamports)
        tokens_raw = min(tokens_raw, self.token_balance_raw(mint))
        # 2) open the position through the sidecar around the live active bin
        result = self.sidecar.post("/open", pool=pool, halfWidth=half_width, tokenAmountRaw=str(tokens_raw),
                                   quoteAmountRaw=str(half_lamports), slippagePct=self.cfg.live_slippage_bps / 100)
        swap_fee = size_usd / 2 * (_f(row, "dlmm_base_fee_pct") or self.cfg.momentum_fee_pct_each_side) / 100
        cost = swap_fee + self.cfg.tx_fee_usd * self.cfg.lp_setup_transactions
        self.deployed_usd += size_usd
        self.log(f"[live] OPEN {mint[:8]} pool={pool[:8]} position={result['position']} "
                 f"tokens_raw={tokens_raw} quote_lamports={half_lamports} bins=[{result['minBinId']},{result['maxBinId']}] "
                 f"sig_buy={buy_sig} sig_open={result['signature']}")
        return LpFill(result["position"], float(result["lowerPrice"]) * sol_price, float(result["upperPrice"]) * sol_price,
                      size_usd - cost, cost, float(result.get("rentSol") or 0.0), tokens_raw / 10 ** decimals,
                      half_lamports / LAMPORTS * sol_price, [buy_sig, result["signature"]])

    def close_lp(self, row, position, price):
        mint, pool = position.mint, row.get("dlmm_pool") or position.pool
        result = self.sidecar.post("/close", pool=pool, position=position.position_key)
        decimals = self.decimals(mint)
        sol_price = self.sol_price()
        tokens_raw = self.token_balance_raw(mint)
        sell_sig, sol_out = "", 0
        if tokens_raw > 0:
            sol_out, sell_sig = self.swap(mint, WSOL, tokens_raw)
        quote_back = (int(result.get("quoteAmountRaw") or 0) + int(result.get("feeQuoteRaw") or 0)) / LAMPORTS
        fee_token_usd = int(result.get("feeTokenRaw") or 0) / 10 ** decimals * price
        fees_usd = int(result.get("feeQuoteRaw") or 0) / LAMPORTS * sol_price + fee_token_usd
        proceeds = (quote_back + sol_out / LAMPORTS) * sol_price
        cost = self.cfg.tx_fee_usd * (len(result.get("signatures") or []) + 1)
        self.deployed_usd = max(0.0, self.deployed_usd - position.entry_value)
        self.log(f"[live] CLOSE {mint[:8]} position={position.position_key} quote_back={quote_back:.6f} SOL "
                 f"tokens_sold_raw={tokens_raw} sol_from_sell={sol_out / LAMPORTS:.6f} sigs={result.get('signatures')} sell={sell_sig}")
        return CloseFill(proceeds, fees_usd, cost, [*(result.get("signatures") or []), sell_sig])

    def buy(self, row, size_usd):
        mint = row["mint"]
        sol_price = self.sol_price()
        lamports = int(size_usd / sol_price * LAMPORTS)
        tokens_raw, sig = self.swap(WSOL, mint, lamports)
        tokens = tokens_raw / 10 ** self.decimals(mint)
        self.deployed_usd += size_usd
        cost = size_usd * self.cfg.momentum_fee_pct_each_side / 100 + self.cfg.tx_fee_usd
        self.log(f"[live] BUY {mint[:8]} ${size_usd:.2f} -> {tokens:.6g} tokens sig={sig}")
        return SpotFill(tokens, size_usd, cost, sig)

    def sell(self, row, tokens, price):
        mint = row["mint"]
        tokens_raw = self.token_balance_raw(mint)
        if tokens_raw <= 0:
            raise HttpError(f"no {mint[:8]} balance to sell")
        sol_out, sig = self.swap(mint, WSOL, tokens_raw)
        proceeds = sol_out / LAMPORTS * self.sol_price()
        self.deployed_usd = max(0.0, self.deployed_usd - tokens * price)
        cost = proceeds * self.cfg.momentum_fee_pct_each_side / 100 + self.cfg.tx_fee_usd
        self.log(f"[live] SELL {mint[:8]} {tokens_raw} raw -> ${proceeds:.2f} sig={sig}")
        return SpotFill(tokens_raw / 10 ** self.decimals(mint), proceeds, cost, sig)
