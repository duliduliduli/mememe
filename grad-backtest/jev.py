"""Jev, TypeSafe's decision model, as a second vote on copy entries (and optionally exits).

Jev does not generate text: POST /v1/systemone takes a `state` (the facts) and named
`questions` (choice, noul = yes/no probability, score) and answers each by name with
probabilities. Served by OpenRouter (POST https://openrouter.ai/api/v1/systemone,
OPENROUTER_API_KEY) and by TypeSafe directly (POST https://api.typesafe.ai/v1/systemone,
TYPESAFE_API_KEY); the second is the fallback when both keys are set.

Where it sits: after every hard filter (sizing, daily stop, impact, round trip, the source
wallet's exit impact), just before the swap. It can only veto a buy, never force one, and it
is never asked about sells the bot makes for its own reasons: a followed wallet's sell, the
stop, the ladder and the daily stop never wait on it. A timeout or error is no decision; a
buy then goes ahead only with JEV_FAIL_OPEN=1. Without a key the gate is off and says so.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Any, Callable

OPENROUTER_URL = "https://openrouter.ai/api/v1/systemone"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"

ENTRY_QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": ("Should this small Solana copy bot buy this memecoin now? The bot is late vs the source wallet. "
                         "Skip snipes, thin pools, and likely dumps."),
        "criteria": {
            "buy": "Copyable entry with acceptable dump risk for an $8 ticket",
            "watch": "Not enough evidence, do not buy",
            "skip": "Bad entry, crowded, thin, sniper, or dump risk",
        },
    },
    "dump_risk": {
        "type": "noul",
        "instructions": "Is this likely to dump hard on copy-traders in the next few minutes?",
    },
}

EXIT_QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "We already hold this memecoin from a copy. Hold or sell now?",
        "criteria": {"hold": "Still reasonable to keep", "sell": "Dumping or the trade is done"},
    },
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class JevConfig:
    def __init__(self) -> None:
        self.enabled = os.getenv("JEV_ENABLED", "1") == "1"
        self.fail_open = os.getenv("JEV_FAIL_OPEN", "0") == "1"
        self.provider = os.getenv("JEV_PROVIDER", "openrouter").strip().lower()
        self.model = os.getenv("JEV_MODEL", "jev-1.13").strip()
        self.typesafe_model = os.getenv("JEV_TYPESAFE_MODEL", "jev-1.13.0").strip()
        self.buy_min_p = _env_float("JEV_BUY_MIN_P", 0.62)
        self.sell_min_p = _env_float("JEV_SELL_MIN_P", 0.70)
        self.dump_max = _env_float("JEV_DUMP_MAX", 0.65)
        self.timeout_seconds = max(0.1, _env_float("JEV_TIMEOUT_MS", 600) / 1000.0)
        self.max_calls_per_min = int(_env_float("JEV_MAX_CALLS_PER_MIN", 30))
        self.cache_seconds = _env_float("JEV_CACHE_SECONDS", 15)
        # The exit vote sells a real position on the model's say-so, so it is opt-in; when on,
        # each copied position is asked at most every JEV_EXIT_CHECK_SECONDS.
        self.exit_enabled = os.getenv("JEV_EXIT_ENABLED", "0") == "1"
        self.exit_check_seconds = _env_float("JEV_EXIT_CHECK_SECONDS", 20)
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        self.typesafe_key = os.getenv("TYPESAFE_API_KEY", "").strip()

    def endpoints(self) -> list[tuple[str, str, str, str]]:
        """(name, url, key, model) in the order they are tried."""
        routes = []
        if self.openrouter_key:
            routes.append(("openrouter", OPENROUTER_URL, self.openrouter_key, self.model))
        if self.typesafe_key:
            routes.append(("typesafe", TYPESAFE_URL, self.typesafe_key, self.typesafe_model))
        if self.provider == "typesafe":
            routes.sort(key=lambda r: r[0] != "typesafe")
        return routes

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.endpoints())

    def describe(self) -> str:
        if not self.enabled:
            return "off (JEV_ENABLED=0)"
        if not self.endpoints():
            return "off: JEV_ENABLED=1 but neither OPENROUTER_API_KEY nor TYPESAFE_API_KEY is set"
        return (f"on via {', '.join(r[0] for r in self.endpoints())}: buy if choice=buy with p>={self.buy_min_p} and "
                f"dump_risk<{self.dump_max}; timeout {self.timeout_seconds * 1000:.0f}ms; "
                f"{'fail open' if self.fail_open else 'fail closed'}; exits {'on' if self.exit_enabled else 'off'}")


def _default_post(url: str, key: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    import requests
    resp = requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                         data=json.dumps(body), timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


class Jev:
    """One client per executor. `stats` is a dict the caller persists (executor state
    "jev"): call counts, decisions and the last calls, for /api/scout."""

    def __init__(self, cfg: JevConfig | None = None, stats: dict[str, Any] | None = None,
                 post: Callable[[str, str, dict[str, Any], float], dict[str, Any]] | None = None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] | None = None,
                 record: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.cfg = cfg or JevConfig()
        self.stats = stats if stats is not None else {}
        self.post = post or _default_post
        self.clock = clock
        self.log = log or (lambda m: None)
        self.record = record or (lambda row: None)
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._calls: deque[float] = deque()

    def _count(self, key: str) -> None:
        self.stats[key] = int(self.stats.get(key) or 0) + 1

    def ask(self, state: dict[str, Any], questions: dict[str, Any], cache_key: str) -> dict[str, Any] | None:
        """Answers keyed by question name, or None for no decision (timeout, error, rate cap).
        The same cache key inside JEV_CACHE_SECONDS reuses the last result: two wallets hitting
        one coin cost one call."""
        now = self.clock()
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] <= self.cfg.cache_seconds:
            self._count("cache_hits")
            return cached[1]
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if self.cfg.max_calls_per_min > 0 and len(self._calls) >= self.cfg.max_calls_per_min:
            self._count("rate_capped")
            self.log(f"JEV {cache_key}: {self.cfg.max_calls_per_min} calls in the last minute; no decision")
            return None
        answers, error, route_used, usage = None, "", "", {}
        started = time.monotonic()
        for name, url, key, model in self.cfg.endpoints():
            self._calls.append(now)
            self._count("calls")
            try:
                body = self.post(url, key, {"model": model, "state": state, "questions": questions}, self.cfg.timeout_seconds)
                answers = body.get("answers") if isinstance(body.get("answers"), dict) else None
                if not answers:
                    raise RuntimeError(f"no answers in response: {json.dumps(body)[:200]}")
                route_used, usage = name, body.get("usage") or {}
                break
            except Exception as exc:
                error = f"{name}: {exc}"
                self._count("timeouts" if "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower() else "errors")
                self.stats["last_error"] = {"ts": now, "error": error[:300]}
        latency_ms = round((time.monotonic() - started) * 1000)
        self._cache[cache_key] = (now, answers)
        for k in [k for k, (t, _) in self._cache.items() if now - t > self.cfg.cache_seconds]:
            self._cache.pop(k, None)
        row = {"ts": now, "key": cache_key, "questions": ",".join(questions), "route": route_used or "none",
               "latency_ms": latency_ms, "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
               "answers": json.dumps(answers, separators=(",", ":")) if answers else "", "error": error if not answers else ""}
        recent = self.stats.setdefault("recent", [])
        recent.append(row)
        del recent[:-30]
        self.record(row)
        if answers is None:
            self._count("no_decision")
        return answers

    def entry(self, state: dict[str, Any], mint: str) -> tuple[str, str]:
        """("buy" | "skip" | "no_decision", detail). "skip" covers watch and skip choices and a
        buy below the probability or above the dump-risk threshold."""
        answers = self.ask(state, ENTRY_QUESTIONS, f"{mint}:buy")
        if answers is None:
            return "no_decision", "no decision (timeout, error or rate cap)"
        action = answers.get("action") or {}
        choice = str(action.get("choice") or "")
        p_buy = float((action.get("probabilities") or {}).get("buy") or 0.0)
        dump = (answers.get("dump_risk") or {}).get("noul")
        dump = float(dump) if dump is not None else None
        detail = f"choice={choice} p_buy={p_buy:.2f} dump_risk={'?' if dump is None else f'{dump:.2f}'}"
        if choice == "buy" and p_buy >= self.cfg.buy_min_p and dump is not None and dump < self.cfg.dump_max:
            self._count("buy")
            return "buy", detail
        self._count("skip")
        return "skip", detail

    def exit(self, state: dict[str, Any], mint: str) -> tuple[str, str]:
        """("sell" | "hold" | "no_decision", detail)."""
        answers = self.ask(state, EXIT_QUESTIONS, f"{mint}:sell")
        if answers is None:
            return "no_decision", "no decision"
        action = answers.get("action") or {}
        choice = str(action.get("choice") or "")
        p_sell = float((action.get("probabilities") or {}).get("sell") or 0.0)
        detail = f"choice={choice} p_sell={p_sell:.2f}"
        if choice == "sell" and p_sell >= self.cfg.sell_min_p:
            self._count("sell")
            return "sell", detail
        self._count("hold")
        return "hold", detail
