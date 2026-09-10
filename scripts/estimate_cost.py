#!/usr/bin/env python3
"""Offline planning estimate; no model calls, keys, or third-party dependencies.

Tokens are PER MODEL REQUEST, not per uploaded file. Each agent/tool turn sends
another request. Output includes billed reasoning tokens. Rates are a dated
snapshot, not a billing guarantee. See docs/model-providers.md.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal


AS_OF = "2026-09-10"
SOURCES = {
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing/",
    "qwen": "https://www.alibabacloud.com/help/en/model-studio/model-pricing",
}
# USD per million tokens. DeepSeek uses peak prices by default.
DEEPSEEK = {"deepseek-v4-flash": ("0.44", "0.014", "1.32"),
            "deepseek-v4-pro": ("1.32", "0.044", "3.96"),
            "deepseek-v4-flash-vision-exp": ("0.44", "0.014", "1.32")}
# Explicit deployment scope; no implicit currency conversion or cache discount.
QWEN = {
    "qwen3-coder-flash-cn": [("0.144", "0.574"), ("0.216", "0.861"), ("0.359", "1.434"), ("0.717", "3.584")],
    "qwen3-coder-plus-cn": [("0.574", "2.294"), ("0.861", "3.441"), ("1.434", "5.735"), ("2.868", "28.671")],
    "qwen3-coder-flash-intl": [("0.3", "1.5"), ("0.5", "2.5"), ("0.8", "4"), ("1.6", "9.6")],
    "qwen3-coder-plus-intl": [("1", "5"), ("1.8", "9"), ("3", "15"), ("6", "60")],
}


def rates(model: str, input_tokens: int, off_peak: bool = False) -> tuple[Decimal, Decimal, Decimal]:
    if model in DEEPSEEK:
        divisor = Decimal(2 if off_peak else 1)
        return tuple(Decimal(rate) / divisor for rate in DEEPSEEK[model])
    if off_peak:
        raise ValueError("--off-peak applies only to DeepSeek")
    if model not in QWEN:
        raise ValueError("unknown model pricing profile")
    # Conservative K=1000 boundaries: may overestimate near binary-K boundaries.
    for limit, (input_rate, output_rate) in zip((32000, 128000, 256000, 1000000), QWEN[model]):
        if input_tokens <= limit:
            return Decimal(input_rate), Decimal(input_rate), Decimal(output_rate)
    raise ValueError("Qwen input exceeds the largest priced tier")


def estimate(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0,
             agents: int = 3, turns: int = 10, growth: int = 0, off_peak: bool = False,
             safety_factor: Decimal = Decimal("1.5"), minutes: Decimal = Decimal(10),
             sandbox_hourly: Decimal = Decimal(0)) -> dict:
    counts = (input_tokens, output_tokens, cached_tokens, agents, turns, growth)
    if any(type(n) is not int or n < 0 for n in counts) or agents < 1 or turns < 1:
        raise ValueError("token counts must be nonnegative integers; agents/turns must be positive")
    if cached_tokens > input_tokens:
        raise ValueError("cached tokens are part of input, not additional input")
    if any(not n.is_finite() or n < 0 for n in (safety_factor, minutes, sandbox_hourly)) or safety_factor < 1:
        raise ValueError("finite nonnegative amounts and safety factor >=1 required")
    if agents * turns > 100000:
        raise ValueError("planning request count exceeds 100000")
    cost = Decimal(0)
    total_input = 0
    for turn in range(turns):
        prompt = input_tokens + turn * growth
        input_rate, cache_rate, output_rate = rates(model, prompt, off_peak)
        cost += agents * ((prompt - cached_tokens) * input_rate + cached_tokens * cache_rate + output_tokens * output_rate) / 1000000
        total_input += agents * prompt
    infrastructure = sandbox_hourly * minutes / 60
    buffered = (cost + infrastructure) * safety_factor
    return {
        "model_pricing_profile": model, "rates_checked": AS_OF,
        "source": SOURCES["deepseek" if model in DEEPSEEK else "qwen"],
        "agents_including_main": agents, "requests_per_agent": turns, "total_requests": agents * turns,
        "total_input_tokens": total_input, "total_output_tokens_including_reasoning": agents * turns * output_tokens,
        "total_cached_input_tokens": agents * turns * cached_tokens,
        "model_cost_usd": float(cost), "sandbox_cost_usd": float(infrastructure),
        "planning_reservation_usd": float(buffered), "safety_factor": float(safety_factor),
        "estimated_jobs_within_10_usd": int(Decimal(10) // buffered) if buffered > 0 else None,
        "notes": ["Estimate only; not a spend cap or measured analysis quality.",
                  "Count main agent, every child, retries, compaction and repeated tool turns.",
                  "Image tokens must be included; text-only models do not gain vision from a PDF skill.",
                  "Sandbox cost excluded unless --sandbox-hourly-usd is supplied.",
                  "Qwen cache discount not assumed; CN and international prices differ.",
                  "DeepSeek peak prices unless --off-peak; calls spanning periods need separate estimates."],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=sorted(DEEPSEEK | QWEN), default="deepseek-v4-flash")
    parser.add_argument("--input-tokens", type=int, default=15000, help="First request input per agent, including cached input")
    parser.add_argument("--output-tokens", type=int, default=1500, help="Output per request, INCLUDING reasoning")
    parser.add_argument("--cached-input-tokens", type=int, default=0, help="Cached portion of each request input")
    parser.add_argument("--agents", type=int, default=3, help="Total agents INCLUDING the main agent")
    parser.add_argument("--turns", type=int, default=10, help="Model requests per agent, not minutes")
    parser.add_argument("--input-growth-per-turn", type=int, default=0, help="Additional input tokens each successive request")
    parser.add_argument("--off-peak", action="store_true")
    parser.add_argument("--safety-factor", type=Decimal, default=Decimal("1.5"))
    parser.add_argument("--minutes", type=Decimal, default=Decimal(10), help="One job VM's wall time, for infrastructure cost only")
    parser.add_argument("--sandbox-hourly-usd", type=Decimal, default=Decimal(0))
    args = parser.parse_args()
    try:
        result = estimate(args.model, args.input_tokens, args.output_tokens, args.cached_input_tokens,
                          args.agents, args.turns, args.input_growth_per_turn, args.off_peak,
                          args.safety_factor, args.minutes, args.sandbox_hourly_usd)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
