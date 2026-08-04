# NeuroRouter — Technical Documentation

**An LLM Gateway / Model Router that automatically routes, caches, and fails over
LLM requests to cut inference cost without cutting quality.**

This document explains what NeuroRouter does, how each piece works internally, why
it's built the way it is, and what real-world problem it solves. It assumes no prior
context beyond "this is a backend service that sits between an app and an LLM API."

---

## 1. The problem, in plain terms

Any team that puts an LLM behind a product feature eventually runs into the same
three failures, usually in this order:

**1. The bill grows faster than the product does.**
A support-bot answering "what are your hours?" costs the same, per call, as it
answering "walk me through migrating our billing system." Most teams call one model
for everything because routing is *extra engineering work nobody budgeted for* — so
they overpay on every trivial request to stay safe on the hard ones.

**2. The same question gets paid for over and over.**
Retries, polling, shared demo links, FAQ-shaped traffic, and simply many users asking
close variants of the same thing — none of that gets deduplicated by default. Every
repeat is a full-price API call.

**3. One provider's bad day becomes your outage.**
If your app calls OpenAI (or Groq, or Anthropic) directly and that provider has a
rate-limit spike, a regional outage, or a slow rollout of a broken model version,
your product goes down with it. There's usually no fallback because wiring one up by
hand, per feature, is tedious and easy to skip.

NeuroRouter is a small, self-contained gateway service that sits in front of your LLM
calls and solves all three — not with a research idea, but with the same primitives
production infrastructure has used for years (classification, caching, circuit
breakers, rate limiting) applied specifically to LLM traffic. This is the same
category of tool as commercial products like OpenRouter, Portkey, or LiteLLM's
proxy mode — NeuroRouter is a from-scratch implementation of that pattern, built to
demonstrate the underlying engineering rather than to compete with them commercially.

---

## 2. What it actually does, end to end

Your application sends one request to NeuroRouter instead of directly to an LLM
provider:

```
POST /v1/chat/completions
{
  "model": "auto",
  "messages": [{"role": "user", "content": "..."}]
}
```

Behind that single call, six things happen automatically:

1. **Authentication** — the request's API key is checked against the configured
   allow-list.
2. **Rate limiting** — that key's recent request count is checked against its quota.
3. **Complexity classification** — the prompt is scored and bucketed into
   `simple`, `medium`, or `complex`.
4. **Cache lookup** — first an exact-match check, then a semantic (meaning-based)
   check, both scoped to that complexity tier.
5. **Provider routing with failover** — on a cache miss, the request is sent to the
   first provider/model in that tier's fallback chain; if that provider is
   unhealthy (its circuit breaker is open) or the call fails, the next hop in the
   chain is tried automatically.
6. **Bookkeeping** — the response is written back into both caches, and cost,
   token, and latency stats are recorded.

The caller gets back a normal chat-completion response, plus a `routing` block that
shows exactly what NeuroRouter decided and why — which tier, which model, whether it
was a cache hit, what it cost, and whether a fallback was needed. Nothing about this
requires the calling application to know any of that happened; from its point of
view, it just called an LLM and got an answer.

---

## 3. Feature-by-feature: how it works and why it's built that way

### 3.1 Complexity classification (the routing brain)

**What it does:** reads the incoming prompt and decides how much "model" it actually
needs.

**How:** rather than a trained neural classifier, NeuroRouter uses a transparent
feature-scoring function. It extracts a handful of cheap signals from the prompt —
word count, whether it contains code (regex for ``` fences, `def`, `class`, `import`,
SQL keywords), math terminology, reasoning-indicating words ("explain", "compare",
"optimize", "design", "prove"), multi-step phrasing ("step by step", "and then"), and
question density — and combines them into a weighted 0–1 score. A short greeting like
"hi there" scores ~0.02. A multi-clause prompt asking to compare, design, and prove
something with an embedded code block scores ~0.83.

**Why a heuristic instead of an ML model:** a *routing* decision needs to be instant,
free, deterministic, and debuggable. A neural classifier adds inference latency, a
cold-start cost, and a training-data problem before you've routed a single request.
The heuristic version answers in under a millisecond with zero dependencies and gives
you a legible explanation ("this scored 0.83 because of the reasoning and code
signals") — which matters when you're debugging why a request went to the expensive
model. The feature-extraction function (`extract_features`) already returns a clean
numeric vector, so the moment you have real labeled routing data, swapping in a
trained `sklearn` model is a one-file change; nothing else in the system needs to
know how the tier was decided.

**Real-world value:** this is the mechanism that makes the cost story real. In the
benchmark run (see §6), 100% of "simple" and "medium" traffic — the large majority of
a typical workload — was automatically routed to the cheap model tier without any
manual tagging or per-endpoint configuration.

---

### 3.2 Two-tier caching (the deduplication layer)

**What it does:** avoids paying for an LLM call when the answer is already known —
either because the exact same prompt was asked before, or because a *differently
worded* prompt means the same thing.

**Exact cache:** the prompt is normalized (trimmed, lower-cased, whitespace
collapsed), combined with its complexity tier, and hashed with SHA-256 into a Redis
key. A hit is a single O(1) Redis `GET`. This catches the highest-volume case in real
traffic: retries, polling loops, and many different users hitting the same FAQ-shaped
question.

**Semantic cache:** catches paraphrases the exact hash would miss — "what is the
capital of France" vs. "tell me France's capital city." Each prompt is converted into
a fixed-length vector using scikit-learn's `HashingVectorizer`, a *stateless*
bag-of-words hashing scheme that needs no training, no fitting step, and no network
call to download a model. New prompts are compared via cosine similarity against a
bounded, recent-prompt window kept in a Redis list (default: last 2,000 entries,
0.92 similarity threshold to count as a hit).

**Why not a transformer embedding model:** a real sentence-embedding model (e.g.
`sentence-transformers`) would catch more paraphrases, but it means a multi-hundred-
megabyte model download, a dependency on a model registry being reachable at
deploy time, and meaningfully higher latency per request. `HashingVectorizer` is a
deliberate lightweight-first tradeoff appropriate for a gateway cache: it's good
enough to catch close paraphrases, has zero external dependencies, and the
`SemanticCache` class is written as a clean seam — swap the vectorizer, keep
everything else.

**Real-world value:** in the benchmark, cache hits accounted for over 90% of a
realistic mixed workload and cost **literally nothing** — no tokens, no API call, no
latency beyond a Redis round-trip (measured at ~2ms vs. ~150-400ms for a live call).

---

### 3.3 Circuit breaker (the reliability layer)

**What it does:** stops sending traffic to a provider that's currently failing,
instead of retrying into a wall and making users wait through slow timeouts.

**How:** a classic three-state machine, one instance per provider, with its state
stored in Redis (not in-process memory — see §4):

- **Closed** — normal operation. Failures are counted in a rolling window.
- **Open** — once failures cross a threshold (default: 5), the breaker "trips."
  Every subsequent call is rejected *immediately*, without attempting the network
  call, and the router moves straight to the next provider in the fallback chain.
- **Half-open** — after a recovery window (default: 30s), exactly one "probe"
  request is allowed through. If it succeeds, the circuit closes and traffic resumes
  normally. If it fails, the circuit re-opens and the timer resets.

**Why this matters over a naive retry:** without a breaker, a struggling provider
gets hammered with retries from every concurrent request, making its outage worse and
making every one of your users wait through a slow timeout before failing over. The
breaker converts "wait 30 seconds then fail" into "fail in under a millisecond and
move on" for every request after the first few.

**Verified behavior:** tested directly through a full cycle — 3 recorded failures
correctly tripped the breaker to `open`; calls during the open state correctly raised
immediately without a network attempt; after the recovery window it correctly
transitioned to `half_open`, allowed exactly one probe through and blocked a second
concurrent one, and a successful probe correctly reset it to `closed`.

---

### 3.4 Fallback chains (the failover mechanism)

**What it does:** defines, per complexity tier, an ordered list of (provider, model)
hops to try. The `simple` tier might try Groq's small model, then OpenAI's small
model, if configured. The router walks the chain in order, skipping any hop whose
circuit breaker is open, until one succeeds.

**Why this is provider-agnostic:** the fallback chain doesn't care whether a hop is
Groq, OpenAI, or the built-in `MockProvider` — they all implement the same interface.
This is what makes the whole system runnable with **zero API keys**: with no keys
configured, every tier's chain is just `[mock]`, and the entire pipeline — routing,
caching, rate limiting, circuit breaking, cost tracking — runs and is fully testable
without a single external network call. The moment you add a `GROQ_API_KEY`, the
chains are rebuilt at startup to route through real models, with no code changes.

---

### 3.5 Rate limiting (the abuse/cost-control layer)

**What it does:** caps how many requests a given API key can make in a rolling time
window (default: 60 requests / 60 seconds).

**How:** a Redis sorted set per key, scored by request timestamp. Each check trims
entries older than the window, counts what's left, and rejects with a `429` (plus a
`Retry-After` header) if the count is at or above the limit. This is a *sliding*
window, not a fixed one — it doesn't have the classic bug where a fixed window lets
double the limit through right at the boundary between two windows.

**Verified behavior:** tested by firing 65 rapid requests against a limit of 60; the
first 60 succeeded and the next 5 were correctly rejected with 429s, with an accurate
`retry_after` estimate.

---

### 3.6 Cost tracking and the live dashboard

**What it does:** every request — cache hit or live call — increments a set of
Redis counters: total requests, cache hits (split by exact vs. semantic), tokens
used, actual dollars spent (estimated from published per-token pricing), and dollars
*saved* relative to a baseline of "every request had gone to the largest model with no
caching." These are exposed at `GET /v1/stats` and visualized on a live dashboard at
`/dashboard` that polls every 3 seconds and shows request volume, cache hit rate,
spend, savings, and the real-time state of every provider's circuit breaker.

**Why this matters:** this is the piece that turns "we built caching and routing"
into a business-legible number. Anyone — an engineer, a manager, an interviewer — can
look at the dashboard and see the savings percentage update in real time as traffic
flows through.

---

## 4. A key architectural decision: state lives in Redis, not memory

Every stateful piece of this system — cache contents, rate-limit counters, circuit
breaker state, cost totals — is stored in Redis rather than in Python variables inside
the running process. This is a deliberate choice with a specific consequence: you can
run **multiple copies of the gateway** behind a load balancer, and they will all see
the same cache, agree on the same rate limits, and trip the same circuit breakers
together, because they all read and write the same Redis instance. A single-process
in-memory version would work for a demo but would silently give each replica its own
private, inconsistent view of the world the moment you scaled past one instance.
Scaling NeuroRouter horizontally is therefore an infrastructure change (run more
containers), not a code change.

---

## 5. Request lifecycle — a concrete trace

Here's exactly what happens for one real request, step by step, using the code paths
involved:

1. `POST /v1/chat/completions` arrives with header `X-API-Key: dev-key-123`.
2. `_check_auth()` confirms the key is in the configured allow-list, or the request
   is rejected with `401`.
3. `RateLimiter.check()` trims and counts that key's recent request timestamps in
   Redis; if over the limit, `429` with a `Retry-After` header.
4. `ModelRouter.route()` is called. It extracts the latest user message and calls
   `classify()`, which scores it and returns a tier (`simple`/`medium`/`complex`)
   plus the numeric score.
5. `ExactCache.get()` checks Redis for `sha256(tier + normalized_prompt)`. If found,
   the cached response is returned immediately — cost `$0`, latency ~2ms — and
   `CostTracker.record_request()` logs a cache hit.
6. If no exact hit, `SemanticCache.get()` vectorizes the prompt and compares it via
   cosine similarity against the recent-prompt window for that tier. A similarity
   ≥ 0.92 counts as a hit and short-circuits the same way.
7. On a full cache miss, the router walks that tier's `ChainHop` list. For each hop:
   `CircuitBreaker.before_call()` is checked first (raises immediately if the
   provider's circuit is open); if clear, `provider.complete()` makes the real
   HTTP call to Groq/OpenAI/Mock.
8. On success: `CircuitBreaker.record_success()` resets that provider's failure
   count, the response is written into *both* caches for next time, and
   `CostTracker.record_request()` logs the real token usage and estimated cost.
9. On failure (timeout, 5xx, auth error): `CircuitBreaker.record_failure()`
   increments that provider's failure count (possibly tripping it open), and the
   loop moves to the next hop in the chain.
10. The response — including a `routing` block with the tier, model, cache status,
    cost, and latency — is returned to the caller.

---

## 6. Measured results

Running `scripts/benchmark.py` against a mixed, realistic workload (simple, medium,
and complex prompts with a ~35% exact-duplicate rate simulating retries and FAQ-style
traffic) produced:

```
Total requests sent:        300
Cache hit rate:             281/300 (93.7%)
Tier distribution:          {'simple': 256, 'medium': 44}
Avg latency:                9.6 ms
p50 latency:                2.1 ms
p95 latency:                76.8 ms
------------------------------------------------------------
Actual cost (NeuroRouter):  $0.000038
Baseline cost (naive: always largest model, no cache): $0.006184
Estimated savings:          $0.006145  (99.4% reduction)
```

The 99.4% figure is a synthetic best-case (duplicate rate and workload mix are
tunable in the script), but the *mechanism* generating it is exactly what you'd cite
in production: cache hits cost nothing, and the large majority of traffic never needed
the expensive model in the first place. Point the same script at a live Groq-backed
deployment to get a number that reflects your real traffic shape.

Separately verified via direct functional tests (not just unit tests — live HTTP
calls against a running instance):
- Auto-routing correctly separated a trivial prompt (score 0.02 → simple tier) from a
  dense, multi-clause technical prompt (score 0.83 → complex tier).
- An exact repeat of a prior prompt hit the cache in 0.16ms vs. ~124ms for a live call.
- A meaning-equivalent paraphrase hit the semantic cache at 0.98+ similarity.
- 65 rapid requests against a 60-request limit correctly returned 429s after the 60th.
- A forced failure sequence correctly tripped the circuit breaker open, blocked calls
  during the open window, transitioned to half-open after the recovery period, and
  closed again after a successful probe.

---

## 7. API reference (summary)

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | Main entry point. `model: "auto"` routes automatically; or force `"simple"` / `"medium"` / `"complex"` / a literal model name. |
| `GET /v1/stats` | Cumulative cost, token, and cache-hit metrics. |
| `GET /v1/models` | Configured fallback chains per tier + live circuit breaker state per provider. |
| `GET /health` | Liveness + Redis connectivity check. |
| `GET /dashboard` | Visual live dashboard (auto-refreshing). |

Full request/response schemas are in `app/models/schemas.py`; interactive OpenAPI
docs are auto-generated at `/docs` when the server is running.

---

## 8. Tech stack and why

| Piece | Choice | Why |
|---|---|---|
| API framework | FastAPI (async) | Async I/O is the right fit for a gateway that's mostly waiting on network calls (LLM providers, Redis); free OpenAPI docs. |
| Shared state | Redis | Sub-millisecond ops, atomic counters/sorted-sets/pipelines, and — critically — state shared across replicas instead of trapped in one process. |
| Complexity signal | Heuristic feature scoring | Zero-latency, zero-dependency, explainable; upgrade path to a trained model is a one-file swap. |
| Semantic similarity | scikit-learn `HashingVectorizer` + cosine similarity | No model download, no GPU, stateless — appropriate weight for a gateway-level cache. |
| Providers | Groq (primary), OpenAI (optional fallback), Mock (offline) | Groq's free tier makes the whole system runnable without a paid key; the provider interface makes adding Anthropic/others trivial. |
| Deployment | Docker Compose (gateway + Redis) | One command to a fully working local or VM deployment; the same Dockerfile deploys to Railway/Fly.io unchanged. |

---

## 9. Known limitations and honest next steps

- The complexity classifier is a heuristic, not a trained model — it will
  misclassify unusual phrasing it wasn't tuned for. The fix (a trained classifier
  once real routing-outcome data exists) is designed in as a drop-in replacement.
- `estimated_cost_usd` figures use published per-token pricing snapshots, not live
  billing data — treat them as directionally accurate estimates, not invoice-grade
  numbers.
- No streaming (`stream: true` is accepted in the schema but not yet implemented) —
  every response is currently a single blocking completion.
- Rate limiting and budgets are per-API-key only; there's no per-tenant spend cap yet.
- The semantic cache's similarity window is capped (default 2,000 entries) and scans
  linearly per tier on lookup — fine at gateway scale, would need an approximate
  nearest-neighbor index (e.g. FAISS) at much higher cache sizes.

---

## 11. The control panel (frontend)

Everything described above is also reachable through a full web UI in `frontend/`,
served directly by the FastAPI backend at `/` (no separate frontend server or build
step — static files calling the same-origin API). Seven pages: a real chat Playground
against `/v1/chat/completions`, a live cost/cache Dashboard, a Providers & Routing
view with working disable/enable/reset-circuit controls, a Live Monitor with
click-through trace detail, a filterable System Logs stream, real Redis-backed API
Key management (create/revoke, not a static `.env` list), and a Settings page that
edits rate limits, cache behavior, and circuit thresholds live — changes apply to new
requests within ~2 seconds, no restart. See the README for the full page-by-page
breakdown and default credentials.

## 12. One-paragraph summary

NeuroRouter is a Redis-backed LLM gateway that classifies every incoming prompt by
complexity, checks it against an exact-match and a semantic cache before spending any
money on it, and — on a genuine cache miss — routes it through an ordered fallback
chain of providers/models, each gated by its own circuit breaker, so that a single
provider outage degrades gracefully instead of taking the product down. Every piece
of state lives in Redis rather than process memory, so the gateway scales horizontally
without behavior changing. In testing, this reduced simulated inference spend by
99.4% against a naive "always call the biggest model, never cache" baseline — the
concrete, measurable version of a problem every team running LLMs in production runs
into eventually.
