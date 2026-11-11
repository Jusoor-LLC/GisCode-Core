"""Provider-neutral usage accounting.

A paid provider call must be visible: how many requests were made, how many
tokens the provider reported, and - only when the user configured current
prices - an estimate of what those tokens cost.  GisCode does not ship price
tables for providers, because published prices change and a stale table would
turn an estimate into a false claim.

Records contain no prompt or completion text, no API key and no endpoint
credentials.  They can be kept in memory and, when a path is configured,
appended as JSON Lines for the user to inspect.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _as_nonnegative_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


class UsageLedger:
    """Thread-safe counter for one provider's billable calls."""

    def __init__(self, path: Optional[Path | str] = None, pricing: Optional[dict] = None) -> None:
        self._path = Path(path) if path is not None else None
        self._pricing: dict = {}
        self._lock = threading.Lock()
        self._requests = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._estimated_cost = 0.0
        self._priced_requests = 0
        self.set_pricing(pricing)

    def set_pricing(self, pricing: Optional[dict]) -> None:
        clean: dict = {}
        if isinstance(pricing, dict):
            for key in ("input_per_million", "output_per_million"):
                value = _as_nonnegative_number(pricing.get(key))
                if value is not None:
                    clean[key] = value
        with self._lock:
            self._pricing = clean

    @property
    def pricing_configured(self) -> bool:
        with self._lock:
            return (
                "input_per_million" in self._pricing
                and "output_per_million" in self._pricing
            )

    def record(
        self,
        *,
        provider: str,
        model: str,
        request_id: str = "",
        usage: Optional[dict] = None,
        finish_reason: str = "",
    ) -> dict:
        block = usage if isinstance(usage, dict) else {}
        prompt = int(block.get("prompt_tokens") or block.get("input_tokens") or 0)
        completion = int(block.get("completion_tokens") or block.get("output_tokens") or 0)
        total = int(block.get("total_tokens") or (prompt + completion))
        with self._lock:
            cost: Optional[float] = None
            if "input_per_million" in self._pricing and "output_per_million" in self._pricing:
                cost = (
                    prompt * self._pricing["input_per_million"]
                    + completion * self._pricing["output_per_million"]
                ) / 1_000_000.0
                self._estimated_cost += cost
                self._priced_requests += 1
            self._requests += 1
            self._prompt_tokens += max(0, prompt)
            self._completion_tokens += max(0, completion)
            self._total_tokens += max(0, total)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "provider": provider,
                "model": model,
                "request_id": request_id,
                "prompt_tokens": max(0, prompt),
                "completion_tokens": max(0, completion),
                "total_tokens": max(0, total),
                "finish_reason": finish_reason,
                "estimated_cost_usd": round(cost, 8) if cost is not None else None,
            }
        self._append(entry)
        return entry

    def _append(self, entry: dict) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # Accounting must never break a request that already succeeded.
            return

    def snapshot(self) -> dict:
        with self._lock:
            priced = self._priced_requests == self._requests and self._requests > 0
            pricing_configured = (
                "input_per_million" in self._pricing
                and "output_per_million" in self._pricing
            )
            return {
                "requests": self._requests,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": self._total_tokens,
                "pricing_configured": pricing_configured,
                "priced_requests": self._priced_requests,
                "estimated_cost_usd": (
                    round(self._estimated_cost, 8) if priced else None
                ),
            }

    def reset(self) -> None:
        with self._lock:
            self._requests = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._total_tokens = 0
            self._estimated_cost = 0.0
            self._priced_requests = 0

    def describe(self) -> str:
        data = self.snapshot()
        if not data["requests"]:
            return "no OpenAI API requests yet"
        text = (
            f"{data['requests']} request(s), {data['prompt_tokens']} input tokens, "
            f"{data['completion_tokens']} output tokens"
        )
        if data["estimated_cost_usd"] is None:
            text += "; cost not configured"
        else:
            text += f"; estimated ${data['estimated_cost_usd']:.4f} at the configured prices"
        return text
