"""The EVM copy lane: watch followed wallets on each configured chain, mirror their buys at
the usual position size, phase out with the copy ladder, follow their sells, and keep the
same stops as the Solana copy lane."""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eth_utils import to_checksum_address

from .chains import ZERO, Chain, load_chains
from .rpc import TRANSFER_TOPIC, Rpc, RpcError, pad_address, topic_address, transfers_in_receipt
from .router import Quote, Router

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "evm_state.json"
TRADES_FILE = DATA_DIR / "evm_trades.csv"
LOG_FILE = DATA_DIR / "evm.log"
STOP_FLAG = DATA_DIR / "evm.stop"
PANIC_FLAG = DATA_DIR / "evm.panic"


def now_ts() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_ts(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    line = f"{iso()} EVM {message}"
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def describe_error(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    return text[:240]


def parse_sell_ladder(spec: str) -> list[dict[str, float]]:
    from executor import parse_sell_ladder as _parse
    return _parse(spec)


def ladder_text(rungs: list[dict[str, float]]) -> str:
    from executor import ladder_text as _text
    return _text(rungs)


def decide_exit(entry_usd: float, current_usd: float, opened_ts: float, now: float, cfg: Any, peak_usd: float | None) -> str | None:
    from executor import decide_exit as _decide
    return _decide(entry_usd, current_usd, opened_ts, now, cfg, peak_usd)


class Config:
    def __init__(self) -> None:
        env = os.getenv
        self.mode = (env("EVM_MODE") or env("EXECUTOR_MODE", "paper")).strip().lower()
        self.secret = env("EVM_PRIVATE_KEY", "")
        self.api_key = env("UNISWAP_API_KEY", "").strip()
        self.chain_keys = [c for c in env("EVM_CHAINS", "robinhood,base,bnb").replace(" ", "").split(",") if c]
        from executor import parse_wallet_list
        raw_wallets, raw_minimums = parse_wallet_list(env("EVM_COPY_WALLETS", ""))
        self.wallets = tuple(to_checksum_address(w) for w in raw_wallets)
        self.wallet_min_usd = {to_checksum_address(w): m for w, m in raw_minimums.items()}
        # Sizing: the same rules as the Solana lane, applied to this lane's own equity.
        self.account_fraction = float(env("ACCOUNT_FRACTION", "0.08"))
        self.max_position_usd = float(env("MAX_POSITION_USD", "20"))
        self.min_position_usd = float(env("MIN_POSITION_USD", "5"))
        self.max_concurrent = int(env("MAX_CONCURRENT_POSITIONS", "10"))
        self.max_deployed_fraction = min(1.0, max(0.1, float(env("MAX_DEPLOYED_FRACTION", "0.80"))))
        self.daily_loss_limit_usd = float(env("DAILY_LOSS_LIMIT_USD", "30"))
        self.paper_balance_usd = float(env("PAPER_BALANCE_USD", "500"))
        # Copy rules and exits: shared names with the Solana lane so one setting rules both.
        self.copy_min_buy_usd = float(env("COPY_MIN_BUY_USD", "300"))
        self.copy_max_tx_age_seconds = float(env("COPY_MAX_TX_AGE_SECONDS", "90"))
        self.copy_poll_seconds = max(1.0, float(env("COPY_POLL_SECONDS", "3")))
        self.copy_follow_sells = env("COPY_FOLLOW_SELLS", "1") == "1"
        self.copy_full_sell_fraction = float(env("COPY_FULL_SELL_FRACTION", "0.8"))
        self.copy_rotate = env("COPY_ROTATE", "0") == "1"
        self.copy_ladder = parse_sell_ladder(env("COPY_LADDER", "1.4:40,1.8:30,3:30"))
        self.take_profit = float(env("COPY_TAKE_PROFIT", "0.75"))
        self.stop_loss = float(env("COPY_STOP_LOSS", "0.30"))
        self.trailing_stop = float(env("COPY_TRAILING_STOP", "0.25"))
        self.time_stop_minutes = float(env("COPY_TIME_STOP_MINUTES", "1440"))
        self.moon_bag = float(env("MOON_BAG", "0.10"))
        self.moon_bag_target_x = float(env("MOON_BAG_TARGET_X", "100"))
        self.min_moon_bag_usd = float(env("MIN_MOON_BAG_USD", "0.50"))
        self.min_round_trip_pct = float(env("MIN_ENTRY_ROUND_TRIP_PCT", "80"))
        self.slippage_pct = float(env("EVM_SLIPPAGE_PCT", "10"))
        self.check_seconds = max(2.0, float(env("EVM_CHECK_SECONDS", "5")))
        self.log_scan_blocks = int(env("EVM_LOG_SCAN_BLOCKS", "2000"))
        if self.mode not in ("paper", "live"):
            raise SystemExit("EVM_MODE must be paper or live")
        if self.mode == "live" and not self.secret:
            raise SystemExit("EVM_MODE=live requires EVM_PRIVATE_KEY (a burner wallet's private key or recovery phrase)")

    def exit_cfg(self, pos: dict[str, Any]) -> SimpleNamespace:
        tp = self.take_profit
        if pos.get("ladder"):
            tp = max(tp, max(r["x"] for r in pos["ladder"]) - 1.0)
        return SimpleNamespace(take_profit=tp, stop_loss=self.stop_loss, trailing_stop=self.trailing_stop,
                               time_stop_minutes=self.time_stop_minutes)


def roll_daily(state: dict[str, Any]) -> None:
    today = iso()[:10]
    daily = state.setdefault("daily", {})
    if daily.get("date") != today:
        state["daily"] = {"date": today, "realized_pnl_usd": 0.0, "trades": 0}


class Lane:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.chains: dict[str, Chain] = load_chains(cfg.chain_keys)
        self.rpcs: dict[str, Rpc] = {k: Rpc(c.rpc_url) for k, c in self.chains.items()}
        self.account = None
        self.address = ZERO
        if cfg.secret:
            from .wallet import load_account
            self.account = load_account(cfg.secret)
            self.address = self.account.address
        self.routers: dict[str, Router] = {}
        for key, chain in self.chains.items():
            send = (lambda tx, k=key: self.send_tx(k, tx)) if cfg.mode == "live" else None
            signer = (lambda permit: self._sign_permit(permit)) if self.account else None
            self.routers[key] = Router(chain, self.rpcs[key], self.address, cfg.api_key, cfg.slippage_pct, send, signer)
        self.state: dict[str, Any] = self.load_state()
        self._price_cache: dict[str, tuple[float, float]] = {}
        self._last_manage = 0.0
        self._last_poll = 0.0
        self._last_heartbeat = 0.0
        self._last_bags = 0.0

    # ---- state -----------------------------------------------------------
    def load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(STATE_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}
        state.setdefault("positions", [])
        state.setdefault("bags", [])
        state.setdefault("copy_seen", {})
        state.setdefault("last_block", {})
        state.setdefault("paper_balance_usd", self.cfg.paper_balance_usd)
        state.setdefault("recent", [])
        roll_daily(state)
        return state

    def save_state(self) -> None:
        self.state["updated_at"] = iso()
        self.state["mode"] = self.cfg.mode
        self.state["wallet"] = self.address
        self.state["chains"] = list(self.chains)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, default=str))
        tmp.replace(STATE_FILE)

    def note(self, kind: str, text: str) -> None:
        recent = self.state.setdefault("recent", [])
        recent.append({"ts": iso(), "kind": kind, "text": text})
        del recent[:-100]

    def record_trade(self, row: dict[str, Any]) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        new = not TRADES_FILE.exists()
        fields = ["ts", "chain", "token", "symbol", "side", "tokens", "usd", "pnl_usd", "reason", "tx", "copy"]
        with TRADES_FILE.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in fields})
        self.state["daily"]["trades"] = int(self.state["daily"].get("trades") or 0) + 1

    # ---- chain helpers ---------------------------------------------------
    def native_price(self, key: str) -> float:
        chain = self.chains[key]
        source = chain.price_chain or key
        cached = self._price_cache.get(source)
        if cached and now_ts() - cached[0] < 60:
            return cached[1]
        router = self.routers.get(source)
        if router is None:
            src_chain = load_chains([source])[source]
            router = Router(src_chain, Rpc(src_chain.rpc_url), self.address)
            self.routers[source] = router
        price = router.native_price_usd()
        if not price:
            if cached:
                return cached[1]
            raise RpcError(f"no {chain.native_symbol} price from {source}")
        self._price_cache[source] = (now_ts(), price)
        return price

    def native_balance(self, key: str) -> float:
        if self.cfg.mode != "live":
            return float(self.state["paper_balance_usd"]) / max(self.native_price(key), 1e-9) / max(len(self.chains), 1)
        return self.rpcs[key].balance(self.address) / 1e18

    def equity_usd(self) -> float:
        total = 0.0
        for key in self.chains:
            try:
                total += self.native_balance(key) * self.native_price(key)
            except Exception as exc:
                log(f"WARN {key} balance/price: {describe_error(exc)}")
        total += sum(float(p.get("last_value_usd") or p.get("position_usd") or 0) for p in self.state["positions"])
        return total

    def _sign_permit(self, permit: dict[str, Any]) -> str:
        from .wallet import sign_permit
        return sign_permit(self.account, permit)

    def send_tx(self, key: str, tx: dict[str, Any]) -> dict[str, Any]:
        """Sign, broadcast and wait for one transaction on `key`; raises on revert."""
        from .wallet import sign_and_serialize
        rpc, chain = self.rpcs[key], self.chains[key]
        full: dict[str, Any] = {"to": to_checksum_address(tx["to"]), "data": tx.get("data", "0x"), "value": int(tx.get("value") or 0),
                                "chainId": chain.chain_id, "from": self.address}
        max_fee, priority = rpc.fees()
        full["maxFeePerGas"], full["maxPriorityFeePerGas"] = max_fee, priority
        full["nonce"] = rpc.nonce(self.address)
        gas = tx.get("gas")
        if not gas:
            est = rpc.estimate_gas({"from": self.address, "to": full["to"], "data": full["data"], "value": hex(full["value"])})
            gas = int(est * 1.3) + 20_000
        full["gas"] = int(gas)
        full["type"] = 2
        raw = sign_and_serialize(self.account, full)
        tx_hash = rpc.send_raw(raw)
        receipt = rpc.wait_receipt(tx_hash)
        if int(receipt.get("status", "0x0"), 16) != 1:
            raise RpcError(f"transaction reverted {chain.explorer}{tx_hash}")
        receipt["hash"] = tx_hash
        return receipt

    def token_meta(self, key: str, token: str) -> tuple[str, int]:
        cache = self.state.setdefault("token_meta", {})
        entry = cache.get(f"{key}:{token}")
        if entry:
            return entry["symbol"], int(entry["decimals"])
        rpc = self.rpcs[key]
        symbol, decimals = rpc.erc20_symbol(token), rpc.erc20_decimals(token)
        cache[f"{key}:{token}"] = {"symbol": symbol, "decimals": decimals}
        return symbol, decimals

    def value_usd(self, key: str, token: str, tokens: int) -> tuple[float, Quote | None]:
        """What selling `tokens` of `token` for the native coin is worth right now."""
        if tokens <= 0:
            return 0.0, None
        q = self.routers[key].quote(token, ZERO, tokens)
        if not q:
            return 0.0, None
        return q.amount_out / 1e18 * self.native_price(key), q

    # ---- sizing ----------------------------------------------------------
    def position_size_usd(self, key: str) -> tuple[float, str]:
        open_positions = len(self.state["positions"])
        if open_positions >= self.cfg.max_concurrent:
            return 0.0, f"all {self.cfg.max_concurrent} slots full"
        daily = float(self.state["daily"]["realized_pnl_usd"])
        if daily <= -self.cfg.daily_loss_limit_usd:
            return 0.0, f"daily loss limit (${daily:,.2f})"
        equity = self.equity_usd()
        size = min(equity * self.cfg.account_fraction, self.cfg.max_position_usd)
        deployed = sum(float(p.get("last_value_usd") or p.get("position_usd") or 0) for p in self.state["positions"])
        if deployed + size > equity * self.cfg.max_deployed_fraction:
            return 0.0, f"deployment cap: ${deployed:,.2f} in positions + ${size:,.2f} > {self.cfg.max_deployed_fraction:.0%} of ${equity:,.2f}"
        available = (self.native_balance(key) - self.chains[key].gas_reserve_native) * self.native_price(key)
        size = min(size, available)
        if size < self.cfg.min_position_usd:
            return 0.0, f"size ${size:,.2f} < ${self.cfg.min_position_usd:,.2f} minimum (equity ${equity:,.2f}, {key} available ${available:,.2f})"
        return round(size, 2), ""

    # ---- copy watching ---------------------------------------------------
    def poll_wallets(self) -> None:
        cfg = self.cfg
        if not cfg.wallets or now_ts() - self._last_poll < cfg.copy_poll_seconds:
            return
        self._last_poll = now_ts()
        for key, chain in self.chains.items():
            try:
                self._poll_chain(key, chain)
            except Exception as exc:
                log(f"WARN {key} copy poll: {describe_error(exc)}")

    def _poll_chain(self, key: str, chain: Chain) -> None:
        cfg, rpc = self.cfg, self.rpcs[key]
        head = rpc.block_number()
        last = int(self.state["last_block"].get(key) or 0)
        window = max(1, int(cfg.copy_max_tx_age_seconds / chain.block_seconds))
        if last <= 0:
            start = max(1, head - window)
            log(f"{key}: watching {len(cfg.wallets)} wallet(s) from block {start} (head {head})")
        else:
            start = last + 1
        if start > head:
            return
        if head - start > cfg.log_scan_blocks:
            start = head - cfg.log_scan_blocks
        seen_all = self.state["copy_seen"].setdefault(key, [])
        events: dict[str, dict[str, Any]] = {}
        for wallet in cfg.wallets:
            padded = pad_address(wallet)
            for topics, side in (([TRANSFER_TOPIC, None, padded], "in"), ([TRANSFER_TOPIC, padded], "out")):
                for entry in rpc.logs(start, head, topics):
                    tx_hash = entry.get("transactionHash")
                    if not tx_hash or tx_hash in seen_all:
                        continue
                    token = to_checksum_address(entry["address"])
                    if token.lower() in (chain.wrapped_native.lower(), chain.stable.lower()):
                        continue
                    try:
                        amount = int(entry.get("data") or "0x0", 16)
                    except ValueError:
                        continue
                    ev = events.setdefault(tx_hash, {"wallet": wallet, "block": int(entry["blockNumber"], 16), "deltas": {}})
                    ev["deltas"][token] = ev["deltas"].get(token, 0) + (amount if side == "in" else -amount)
        self.state["last_block"][key] = head
        for tx_hash, ev in sorted(events.items(), key=lambda kv: kv[1]["block"]):
            seen_all.append(tx_hash)
            del seen_all[:-500]
            age = (head - ev["block"]) * chain.block_seconds
            deltas = {t: d for t, d in ev["deltas"].items() if d}
            if not deltas:
                continue
            token, delta = max(deltas.items(), key=lambda kv: abs(kv[1]))
            wallet = ev["wallet"]
            if delta < 0:
                self._follow_sell(key, wallet, token, -delta)
                continue
            if age > cfg.copy_max_tx_age_seconds:
                log(f"COPY {key} {wallet[:8]}: buy of {token[:10]} is {age:.0f}s old; too late to mirror")
                continue
            self._mirror_buy(key, wallet, token, delta, tx_hash)

    def _follow_sell(self, key: str, wallet: str, token: str, tokens_sold: int = 0) -> None:
        """The followed wallet sold `tokens_sold`: selling most of its stack closes our position,
        a partial sale trims ours by the same share (its stack before = balance now + sold)."""
        if not self.cfg.copy_follow_sells:
            return
        ours = [p for p in self.state["positions"] if p["chain"] == key and p["token"].lower() == token.lower()]
        if not ours:
            return
        fraction = 1.0
        if tokens_sold > 0:
            try:
                left = self.rpcs[key].erc20_balance(token, wallet)
                fraction = min(1.0, tokens_sold / (left + tokens_sold)) if left + tokens_sold > 0 else 1.0
            except Exception as exc:
                log(f"WARN {key} copy sell: cannot read {wallet[:8]}'s balance ({describe_error(exc)}); treating as a full sale")
        for pos in ours:
            try:
                if fraction >= self.cfg.copy_full_sell_fraction:
                    log(f"COPY {key} {wallet[:8]} sold {fraction:.0%} of {pos['symbol']}; closing our position")
                    self.close_position(pos, "copy_sell")
                    continue
                worth = fraction * float(pos.get("last_value_usd") or pos.get("position_usd") or 0.0)
                if worth < 1.0:
                    log(f"COPY {key} {wallet[:8]} trimmed {fraction:.0%} of {pos['symbol']}; our trim would be ${worth:.2f}, skipped")
                    continue
                log(f"COPY {key} {wallet[:8]} trimmed {fraction:.0%} of {pos['symbol']}; trimming ours the same")
                self.scale_out(pos, fraction, "copy_trim")
            except Exception as exc:
                log(f"WARN copy sell {pos['symbol']}: {describe_error(exc)}")

    def _mirror_buy(self, key: str, wallet: str, token: str, tokens_bought: int, tx_hash: str) -> None:
        cfg = self.cfg
        symbol, decimals = self.token_meta(key, token)
        try:
            usd, _ = self.value_usd(key, token, tokens_bought)
        except Exception as exc:
            log(f"COPY {key} {wallet[:8]} bought {symbol}; cannot value it ({describe_error(exc)}); ignored")
            return
        if usd <= 0:
            log(f"COPY {key} {wallet[:8]} received {symbol} ({token[:10]}) with no sell route; ignored")
            return
        minimum = cfg.wallet_min_usd.get(wallet, cfg.copy_min_buy_usd)
        if usd < minimum:
            log(f"COPY {key} {wallet[:8]} bought {symbol} for ~${usd:,.0f} < ${minimum:,.0f} minimum; ignored")
            return
        if any(p["chain"] == key and p["token"].lower() == token.lower() for p in self.state["positions"]):
            log(f"COPY {key} {wallet[:8]} bought {symbol} (~${usd:,.0f}); already held")
            return
        if not self.rotate_for_copy(key, symbol):
            return
        log(f"COPY {key} {wallet[:8]} bought {symbol} for ~${usd:,.0f}; mirroring")
        try:
            self.enter(key, token, symbol, decimals, wallet, usd)
        except Exception as exc:
            log(f"WARN entry {key} {symbol}: {describe_error(exc)}")
            self.note("error", f"{key} {symbol} entry failed: {describe_error(exc)}")

    def rotate_for_copy(self, key: str, symbol: str) -> bool:
        cfg = self.cfg
        held = self.state["positions"]
        if len(held) < cfg.max_concurrent:
            return True
        if not cfg.copy_rotate:
            log(f"COPY {symbol}: all {cfg.max_concurrent} slots full and COPY_ROTATE=0; skipped")
            return False
        if float(self.state["daily"]["realized_pnl_usd"]) <= -cfg.daily_loss_limit_usd:
            log(f"COPY {symbol}: daily loss limit reached; not rotating")
            return False
        oldest = min(held, key=lambda p: float(p.get("opened_ts") or 0))
        log(f"ROTATE selling oldest {oldest['chain']} {oldest['symbol']} (opened {oldest.get('opened_at', '?')}) to make room for {symbol}")
        try:
            self.close_position(oldest, "rotate")
        except Exception as exc:
            log(f"WARN rotate {oldest['symbol']}: {describe_error(exc)}; {symbol} skipped")
            return False
        return oldest not in self.state["positions"]

    # ---- entries and exits -----------------------------------------------
    def enter(self, key: str, token: str, symbol: str, decimals: int, wallet: str, copy_buy_usd: float) -> None:
        cfg, chain, router = self.cfg, self.chains[key], self.routers[key]
        size_usd, why = self.position_size_usd(key)
        if size_usd <= 0:
            log(f"SKIP {key} {symbol}: {why}")
            return
        price = self.native_price(key)
        amount_in = int(size_usd / price * 1e18)
        buy = router.quote(ZERO, token, amount_in)
        if not buy or buy.amount_out <= 0:
            log(f"SKIP {key} {symbol}: no buy route ({router.last_api_error or 'no pool'})")
            return
        back = router.quote(token, ZERO, buy.amount_out)
        round_trip = (back.amount_out / amount_in * 100) if back else 0.0
        if round_trip < cfg.min_round_trip_pct:
            log(f"SKIP {key} {symbol}: round trip {round_trip:.0f}% < {cfg.min_round_trip_pct:.0f}% (thin pool or tax)")
            return
        tokens = buy.amount_out
        tx_hash = ""
        if cfg.mode == "live":
            receipt = router.execute(buy)
            tx_hash = receipt.get("hash", "")
            got = transfers_in_receipt(receipt, self.address).get(token)
            if got and got > 0:
                tokens = got
        else:
            self.state["paper_balance_usd"] = float(self.state["paper_balance_usd"]) - size_usd
        pos = {"chain": key, "token": token, "symbol": symbol, "decimals": decimals, "tokens": int(tokens),
               "position_usd": size_usd, "entry_tokens": int(tokens), "entry_basis_usd": size_usd,
               "opened_ts": now_ts(), "opened_at": iso(), "peak_usd": size_usd, "last_value_usd": size_usd,
               "copy": wallet, "copy_buy_usd": round(copy_buy_usd), "buy_tx": tx_hash, "route": buy.kind,
               "ladder": [dict(r, done=False) for r in cfg.copy_ladder]}
        self.state["positions"].append(pos)
        self.record_trade({"ts": iso(), "chain": key, "token": token, "symbol": symbol, "side": "buy", "tokens": tokens,
                           "usd": size_usd, "pnl_usd": "", "reason": "copy", "tx": tx_hash, "copy": wallet})
        log(f"ENTER {key} {symbol} ${size_usd:,.2f} via {buy.kind} round_trip={round_trip:.0f}% ({chain.explorer}{tx_hash if tx_hash else 'paper'})")
        self.note("enter", f"{key} {symbol} ${size_usd:,.2f} copying {wallet[:8]}")
        self.save_state()

    def sell_tokens(self, key: str, token: str, tokens: int) -> tuple[float, str]:
        """Sell `tokens` for the native coin; returns (usd proceeds, tx hash)."""
        router = self.routers[key]
        if self.cfg.mode == "live":
            held = self.rpcs[key].erc20_balance(token, self.address)
            tokens = min(tokens, held) if held > 0 else tokens
        q = router.quote(token, ZERO, tokens)
        if not q or q.amount_out <= 0:
            raise RpcError("no sell route")
        price = self.native_price(key)
        if self.cfg.mode != "live":
            usd = q.amount_out / 1e18 * price
            self.state["paper_balance_usd"] = float(self.state["paper_balance_usd"]) + usd
            return usd, ""
        before = self.rpcs[key].balance(self.address)
        receipt = router.execute(q)
        after = self.rpcs[key].balance(self.address)
        gas_cost = int(receipt.get("gasUsed", "0x0"), 16) * int(receipt.get("effectiveGasPrice", "0x0"), 16)
        native = (after - before + gas_cost) / 1e18
        if native <= 0:
            native = q.amount_out / 1e18
        return native * price, receipt.get("hash", "")

    def close_position(self, pos: dict[str, Any], reason: str) -> None:
        cfg = self.cfg
        tokens = int(pos["tokens"])
        last_value = float(pos.get("last_value_usd") or pos["position_usd"])
        mb = 0.0 if reason == "panic" else cfg.moon_bag
        if mb > 0 and last_value * mb < cfg.min_moon_bag_usd:
            mb = 0.0
        keep = int(tokens * mb)
        sell = tokens - keep
        usd, tx_hash = self.sell_tokens(pos["chain"], pos["token"], sell)
        basis = float(pos["position_usd"]) * (sell / tokens if tokens else 1.0)
        pnl = usd - basis
        self.state["daily"]["realized_pnl_usd"] = float(self.state["daily"]["realized_pnl_usd"]) + pnl
        if pos in self.state["positions"]:
            self.state["positions"].remove(pos)
        if keep > 0:
            self.state["bags"].append({"chain": pos["chain"], "token": pos["token"], "symbol": pos["symbol"], "tokens": keep,
                                       "cost_usd": float(pos["position_usd"]) - basis, "opened_at": pos.get("opened_at"),
                                       "basis_value_usd": last_value * mb, "created_at": iso()})
        self.record_trade({"ts": iso(), "chain": pos["chain"], "token": pos["token"], "symbol": pos["symbol"], "side": "sell",
                           "tokens": sell, "usd": usd, "pnl_usd": round(pnl, 4), "reason": reason, "tx": tx_hash, "copy": pos.get("copy", "")})
        log(f"EXIT {pos['chain']} {pos['symbol']} {reason} ${usd:,.2f} ({pnl:+.2f}) moon_bag={keep} tokens {tx_hash}")
        self.note("exit", f"{pos['chain']} {pos['symbol']} {reason} {pnl:+.2f}")
        self.save_state()

    def scale_out(self, pos: dict[str, Any], frac: float, reason: str) -> None:
        tokens = int(pos["tokens"])
        sell = int(tokens * frac)
        if sell <= 0:
            return
        usd, tx_hash = self.sell_tokens(pos["chain"], pos["token"], sell)
        basis = float(pos["position_usd"]) * frac
        pnl = usd - basis
        pos["tokens"] = tokens - sell
        pos["position_usd"] = float(pos["position_usd"]) - basis
        pos["peak_usd"] = float(pos.get("peak_usd") or 0) * (1 - frac)
        pos["last_value_usd"] = float(pos.get("last_value_usd") or 0) * (1 - frac)
        self.state["daily"]["realized_pnl_usd"] = float(self.state["daily"]["realized_pnl_usd"]) + pnl
        self.record_trade({"ts": iso(), "chain": pos["chain"], "token": pos["token"], "symbol": pos["symbol"], "side": "sell",
                           "tokens": sell, "usd": usd, "pnl_usd": round(pnl, 4), "reason": reason, "tx": tx_hash, "copy": pos.get("copy", "")})
        log(f"SCALE-OUT {pos['chain']} {pos['symbol']} {reason} {frac:.0%} ${usd:,.2f} ({pnl:+.2f}) {tx_hash}")
        self.save_state()

    def ladder_step(self, pos: dict[str, Any], current_usd: float) -> bool:
        rungs = pos.get("ladder") or []
        entry_tokens, entry_basis, tokens_now = int(pos.get("entry_tokens") or 0), float(pos.get("entry_basis_usd") or 0), int(pos["tokens"])
        if not rungs or entry_tokens <= 0 or entry_basis <= 0 or tokens_now <= 0:
            return False
        multiple = (current_usd / tokens_now) / (entry_basis / entry_tokens)
        pending = [r for r in rungs if not r.get("done")]
        if not pending or multiple < pending[0]["x"]:
            return False
        crossed = [r for r in pending if multiple >= r["x"]]
        share = sum(entry_tokens * r["pct"] / 100 for r in crossed)
        last = crossed[-1] is rungs[-1]
        for r in crossed:
            r["done"] = True
        log(f"LADDER {pos['chain']} {pos['symbol']}: {multiple:.2f}x entry, rung {crossed[-1]['x']:g}x reached")
        if last or share >= tokens_now * 0.98:
            self.close_position(pos, f"ladder_{crossed[-1]['x']:g}x")
        else:
            self.scale_out(pos, share / tokens_now, f"ladder_{crossed[-1]['x']:g}x")
        return True

    def manage_positions(self, panic: bool = False) -> None:
        if not self.state["positions"] or (not panic and now_ts() - self._last_manage < self.cfg.check_seconds):
            return
        self._last_manage = now_ts()
        for pos in list(self.state["positions"]):
            try:
                current, _ = self.value_usd(pos["chain"], pos["token"], int(pos["tokens"]))
            except Exception as exc:
                log(f"WARN value {pos['chain']} {pos['symbol']}: {describe_error(exc)}")
                continue
            if current <= 0:
                log(f"WARN {pos['chain']} {pos['symbol']}: no sell quote; leaving it alone this cycle")
                continue
            pos["last_value_usd"] = current
            pos["peak_usd"] = max(float(pos.get("peak_usd") or 0), current)
            if panic:
                self.close_position(pos, "panic")
                continue
            try:
                if self.ladder_step(pos, current):
                    continue
                reason = decide_exit(float(pos["position_usd"]), current, float(pos["opened_ts"]), now_ts(), self.cfg.exit_cfg(pos), pos["peak_usd"])
                if reason:
                    self.close_position(pos, reason)
            except Exception as exc:
                log(f"WARN exit {pos['chain']} {pos['symbol']}: {describe_error(exc)}")

    def manage_bags(self, panic: bool = False) -> None:
        if not self.state["bags"] or (not panic and now_ts() - self._last_bags < 60):
            return
        self._last_bags = now_ts()
        for bag in list(self.state["bags"]):
            try:
                value, _ = self.value_usd(bag["chain"], bag["token"], int(bag["tokens"]))
                bag["last_value_usd"] = value
                target = float(bag.get("basis_value_usd") or 0) * self.cfg.moon_bag_target_x
                if panic or (target > 0 and value >= target):
                    usd, tx_hash = self.sell_tokens(bag["chain"], bag["token"], int(bag["tokens"]))
                    self.state["bags"].remove(bag)
                    pnl = usd - float(bag.get("cost_usd") or 0)
                    self.state["daily"]["realized_pnl_usd"] = float(self.state["daily"]["realized_pnl_usd"]) + pnl
                    self.record_trade({"ts": iso(), "chain": bag["chain"], "token": bag["token"], "symbol": bag["symbol"], "side": "sell",
                                       "tokens": bag["tokens"], "usd": usd, "pnl_usd": round(pnl, 4),
                                       "reason": "panic" if panic else "moon_bag_target", "tx": tx_hash})
                    log(f"MOON BAG {bag['chain']} {bag['symbol']} sold ${usd:,.2f} ({pnl:+.2f})")
            except Exception as exc:
                log(f"WARN bag {bag['chain']} {bag['symbol']}: {describe_error(exc)}")

    # ---- loop ------------------------------------------------------------
    def heartbeat(self) -> None:
        if now_ts() - self._last_heartbeat < 30:
            return
        self._last_heartbeat = now_ts()
        log(f"HEARTBEAT positions={len(self.state['positions'])} bags={len(self.state['bags'])} "
            f"daily_pnl=${float(self.state['daily']['realized_pnl_usd']):+.2f} "
            f"blocks={ {k: v for k, v in self.state['last_block'].items()} }")
        self.save_state()

    def run_cycle(self) -> None:
        roll_daily(self.state)
        panic = PANIC_FLAG.exists()
        draining = STOP_FLAG.exists() or panic
        self.state["draining"] = draining
        self.manage_positions(panic)
        self.manage_bags(panic)
        if panic and not self.state["positions"] and not self.state["bags"]:
            PANIC_FLAG.unlink(missing_ok=True)
            STOP_FLAG.touch()
            log("panic complete: everything sold, lane draining")
        if not draining:
            self.poll_wallets()
        self.heartbeat()

    def startup(self) -> None:
        cfg = self.cfg
        log(f"lane starting: mode={cfg.mode} chains={','.join(self.chains)} wallets={len(cfg.wallets)} "
            f"router={'uniswap-api+onchain' if cfg.api_key else 'onchain-only'} fraction={cfg.account_fraction} "
            f"max_pos=${cfg.max_position_usd} slots={cfg.max_concurrent} min_buy=${cfg.copy_min_buy_usd:,.0f} "
            f"ladder={ladder_text(cfg.copy_ladder)} tp=+{cfg.take_profit:.0%} sl=-{cfg.stop_loss:.0%} "
            f"trail={cfg.trailing_stop:.0%} time_stop={cfg.time_stop_minutes:.0f}m moon_bag={cfg.moon_bag:.0%} "
            f"slippage={cfg.slippage_pct:.0f}% round_trip>={cfg.min_round_trip_pct:.0f}% "
            f"{'rotate' if cfg.copy_rotate else 'no-rotate'} {'follow-sells' if cfg.copy_follow_sells else 'own-exits'} "
            f"daily_loss_limit=${cfg.daily_loss_limit_usd}")
        if cfg.mode == "live":
            log(f"live wallet: {self.address} (burner only!)")
        for key, chain in self.chains.items():
            try:
                bal = self.native_balance(key)
                price = self.native_price(key)
                log(f"{key}: chain {chain.chain_id} balance {bal:.5f} {chain.native_symbol} (${bal * price:,.2f}, "
                    f"{chain.native_symbol} ${price:,.0f}, {chain.gas_reserve_native} reserved for gas)")
            except Exception as exc:
                log(f"WARN {key}: {describe_error(exc)}")
        if self.state["positions"]:
            log(f"resuming {len(self.state['positions'])} open position(s) from state: "
                + ", ".join(f"{p['chain']} {p['symbol']}" for p in self.state["positions"]))
        if cfg.mode == "live" and not cfg.api_key:
            log("NOTE: UNISWAP_API_KEY not set; Robinhood Chain tokens in Uniswap V4 pools (most launchpad graduates) cannot be routed")
        self.save_state()

    def run(self) -> None:
        self.startup()
        while True:
            try:
                self.run_cycle()
            except Exception as exc:
                log(f"WARN cycle: {describe_error(exc)}")
            if STOP_FLAG.exists() and not self.state["positions"] and not PANIC_FLAG.exists():
                log("stop flag set and nothing open; lane exiting")
                self.save_state()
                return
            time.sleep(1.0)


def main() -> None:
    cfg = Config()
    if not cfg.wallets:
        log("EVM_COPY_WALLETS is empty; nothing to follow. Exiting.")
        sys.exit(0)
    Lane(cfg).run()
