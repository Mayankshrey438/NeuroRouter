"""
Benchmark: sends a realistic mixed workload through NeuroRouter and reports
cache hit rate + estimated cost savings vs a naive baseline that always
calls the largest/most expensive model with no caching.

Usage:
    python scripts/benchmark.py --base-url http://localhost:8000 --api-key dev-key-123 --requests 200
"""
import argparse
import random
import statistics
import time

import httpx

SIMPLE_PROMPTS = [
    "hi there", "thanks!", "what's 2+2?", "hello", "how are you",
    "what time is it", "good morning", "ok sounds good", "yes please",
    "what's the capital of France",
]

MEDIUM_PROMPTS = [
    "Summarize the plot of Romeo and Juliet in three sentences",
    "What are the main differences between TCP and UDP",
    "Give me three tips for better sleep",
    "Explain what a REST API is",
    "What's the difference between a list and a tuple in Python",
]

COMPLEX_PROMPTS = [
    "Explain step by step, then compare and analyze the trade-offs between "
    "sliding window and token bucket rate limiting algorithms, including a "
    "proof of correctness and example code for each.",
    "Design and architect a distributed caching layer for a high-traffic "
    "e-commerce site. Analyze consistency trade-offs and optimize for p99 latency.",
    "Debug and refactor this recursive algorithm, then derive its time "
    "complexity with a formal proof and calculate the theoretical speedup "
    "from memoization.",
    "Compare and analyze microservices vs monolith architecture trade-offs, "
    "then design a migration strategy step by step with rollback plans.",
]

# Baseline pricing if EVERY request went to the largest model with NO caching
BASELINE_RATE = {"input": 0.59, "output": 0.79}  # USD per 1M tokens


def build_workload(n: int, duplicate_ratio: float = 0.35):
    """Builds a workload where some fraction of prompts are exact or
    near-duplicate repeats - representative of real traffic (retries,
    common FAQs, shared demo links, polling)."""
    pool = SIMPLE_PROMPTS + MEDIUM_PROMPTS + COMPLEX_PROMPTS
    workload = []
    seen = []

    for _ in range(n):
        if seen and random.random() < duplicate_ratio:
            base = random.choice(seen)
            workload.append(base)  # exact duplicate
        else:
            prompt = random.choice(pool)
            workload.append(prompt)
            seen.append(prompt)

    random.shuffle(workload)
    return workload


def run(base_url: str, api_key: str, n_requests: int):
    workload = build_workload(n_requests)
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    latencies = []
    results = []

    with httpx.Client(timeout=30.0) as client:
        for i, prompt in enumerate(workload):
            payload = {
                "model": "auto",
                "messages": [{"role": "user", "content": prompt}],
            }
            t0 = time.perf_counter()
            resp = client.post(f"{base_url}/v1/chat/completions", json=payload, headers=headers)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)

            if resp.status_code != 200:
                print(f"  [{i}] ERROR {resp.status_code}: {resp.text[:150]}")
                continue

            data = resp.json()
            results.append(data)

    stats_resp = httpx.get(f"{base_url}/v1/stats", headers=headers)
    server_stats = stats_resp.json()

    return workload, results, latencies, server_stats


def summarize(workload, results, latencies, server_stats):
    total = len(results)
    cache_hits = sum(1 for r in results if r["routing"]["cache_hit"])
    tier_counts = {}
    for r in results:
        tier = r["routing"]["complexity"]
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    total_actual_cost = sum(r["routing"]["estimated_cost_usd"] for r in results)

    # naive baseline: every request (including the ones that were cache hits
    # here) hits the largest model at full token cost, no caching at all
    baseline_cost = 0.0
    for r in results:
        pt = r["usage"]["prompt_tokens"]
        ct = r["usage"]["completion_tokens"]
        baseline_cost += (pt / 1_000_000) * BASELINE_RATE["input"] + (ct / 1_000_000) * BASELINE_RATE["output"]

    savings = baseline_cost - total_actual_cost
    savings_pct = (savings / baseline_cost * 100) if baseline_cost > 0 else 0

    print("\n" + "=" * 60)
    print("NEUROROUTER BENCHMARK RESULTS")
    print("=" * 60)
    print(f"Total requests sent:        {total}")
    print(f"Cache hit rate:             {cache_hits}/{total} ({cache_hits/total*100:.1f}%)")
    print(f"Tier distribution:          {tier_counts}")
    print(f"Avg latency:                {statistics.mean(latencies):.1f} ms")
    print(f"p50 latency:                {statistics.median(latencies):.1f} ms")
    print(f"p95 latency:                {sorted(latencies)[int(len(latencies)*0.95)]:.1f} ms")
    print("-" * 60)
    print(f"Actual cost (NeuroRouter):  ${total_actual_cost:.6f}")
    print(f"Baseline cost (naive):      ${baseline_cost:.6f}")
    print(f"Estimated savings:          ${savings:.6f}  ({savings_pct:.1f}% reduction)")
    print("-" * 60)
    print("Server-side cumulative stats since startup:")
    for k, v in server_stats.items():
        print(f"  {k}: {v}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default="dev-key-123")
    parser.add_argument("--requests", type=int, default=200)
    args = parser.parse_args()

    workload, results, latencies, server_stats = run(args.base_url, args.api_key, args.requests)
    summarize(workload, results, latencies, server_stats)
