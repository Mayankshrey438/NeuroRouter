# 🧠 NeuroRouter

**An LLM Gateway / Model Router** — a single API in front of your LLM providers that
automatically classifies request complexity, routes to the cheapest model that can
handle it, caches semantically similar requests, and fails over gracefully when a
provider goes down.

Built as a portfolio project to demonstrate backend engineering depth (rate limiting,
circuit breakers, caching, fallback chains) layered with a lightweight ML component
(a complexity classifier) — full-stack + AI, in a repo you can actually run end to end.

Runs with **zero API keys** out of the box (via a built-in `MockProvider`), and upgrades
to real LLM calls the moment you drop a free [Groq](https://console.groq.com) key into `.env`.

---

## Why this exists

Every team calling LLMs in production eventually hits the same problem: you're paying
70B-model prices for "hi" and "thanks!" messages, you're recomputing answers to
questions you've already answered, and a single provider outage takes down your whole
product. NeuroRouter is a gateway layer that sits between your app and your LLM
provider(s) and solves all three:

| Problem | NeuroRouter's answer |
|---|---|
| Paying big-model prices for trivial prompts | **Complexity classifier** routes each request to a `simple` / `medium` / `complex` tier, each mapped to a different model |
| Re-paying for duplicate or near-duplicate prompts | **Two-tier cache**: exact-match (hash) + semantic (vector similarity) |
| One provider's outage breaking your product | **Circuit breaker** per provider + **ordered fallback chain** across providers/models |
| Noisy neighbors / cost blowouts | **Sliding-window rate limiting** per API key |
| "How much are we actually spending, and saving?" | **Live cost-tracking dashboard** |

---

## Architecture

```
                         ┌─────────────────────────────────────────┐
                         │              NeuroRouter                  │
   Client ── POST ──────▶│  /v1/chat/completions                    │
   (X-API-Key header)    │                                           │
                         │  1. Auth check                           │
                         │  2. Rate limiter (Redis sliding window)  │
                         │  3. Complexity classifier                │
                         │        simple / medium / complex         │
                         │  4. Cache lookup                         │
                         │        exact (hash) → semantic (cosine)  │
                         │  5. On miss: fallback chain               │
                         │        hop 1: Groq  (small model)         │
                         │        hop 2: Groq  (large model)         │
                         │        hop 3: OpenAI (fallback, optional) │
                         │        — each hop gated by a per-provider │
                         │          circuit breaker                  │
                         │  6. Cache write-back + cost tracking      │
                         └─────────────────────────────────────────┘
                                          │
                                     Redis (state)
                          cache · rate limits · circuit
                          breaker state · cost counters
```

**Design principle:** every piece of state (cache, rate limit counters, circuit
breaker state, cost totals) lives in Redis, not in-process memory — so you can run
multiple gateway replicas behind a load balancer and they all see consistent state.

---

## Repo structure

```
neurorouter/
├── app/
│   ├── main.py                  # FastAPI app factory + startup wiring
│   ├── config.py                # env-based settings (pydantic-settings)
│   ├── models/
│   │   └── schemas.py           # request/response Pydantic models
│   ├── core/
│   │   ├── classifier.py        # complexity classifier (the "AI" component)
│   │   ├── router.py            # orchestrates classify → cache → fallback chain
│   │   ├── cache.py             # exact + semantic caching (Redis-backed)
│   │   ├── rate_limiter.py      # sliding-window rate limiter (Redis-backed)
│   │   ├── circuit_breaker.py   # per-provider circuit breaker (Redis-backed)
│   │   ├── cost_tracker.py      # spend/savings counters for the dashboard
│   │   ├── event_log.py         # structured request/event log (Redis-backed)
│   │   ├── api_keys.py          # API key create/list/revoke store (Redis-backed)
│   │   ├── runtime_config.py    # live-editable rate/cache/circuit config
│   │   └── provider_control.py  # admin enable/disable per provider
│   ├── providers/
│   │   ├── base.py              # provider interface
│   │   ├── groq_provider.py     # Groq (primary, free tier)
│   │   ├── openai_provider.py   # OpenAI (optional fallback)
│   │   └── mock_provider.py     # zero-key offline stand-in
│   └── api/
│       ├── routes.py            # /v1/chat/completions, /v1/stats, /v1/models, /health
│       ├── admin_routes.py      # /v1/admin/* — keys, config, provider control
│       └── logs_routes.py       # /v1/logs, /v1/logs/{trace_id}
├── frontend/                    # control panel UI (served by FastAPI at "/")
│   ├── index.html               # Playground
│   ├── dashboard.html           # Analytics dashboard
│   ├── providers.html           # Providers & routing strategy
│   ├── monitor.html             # Live request monitor
│   ├── logs.html                # System logs
│   ├── keys.html                # API key management
│   ├── settings.html            # Live runtime config + session settings
│   └── assets/
│       ├── style.css            # design tokens matching the Stitch design system
│       └── api.js               # shared API client, auth storage, nav, toasts
├── scripts/
│   └── benchmark.py             # sends a mixed workload, reports cost savings
├── tests/                       # 16 pytest tests (classifier, cache, rate limit, breaker)
├── Dockerfile
├── docker-compose.yml           # gateway + redis, one command
├── requirements.txt
├── .env.example
├── README.md
└── DOCUMENTATION.md             # full technical writeup: how it works, why, results
```

---

## Quick start

### Option A — Docker Compose (recommended)

```bash
cp .env.example .env
# optionally add a free key from https://console.groq.com to .env

docker compose up --build
```

Control panel (frontend): http://localhost:8000/
API: http://localhost:8000/v1/...
Docs (auto-generated OpenAPI): http://localhost:8000/docs

### Option B — Run locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# you need a local redis instance:
redis-server &

cp .env.example .env
uvicorn app.main:app --reload
```

With no `GROQ_API_KEY` set, the gateway automatically runs in **mock mode** — fully
functional, zero external calls, so you can test/demo the whole system immediately.
Open http://localhost:8000/ for the control panel, or drive it purely via curl/the API.

**Default credentials (change before deploying anywhere public):**
- API key: `dev-key-123` (auto-seeded on first startup, editable from the Keys page)
- Admin token: `admin-dev-token` (gates the admin API — key management, live config,
  provider enable/disable)

---

## The control panel (frontend)

A full web UI ships in `frontend/` and is served directly by the FastAPI backend at
`/` — no separate frontend server, no build step, just static files talking to the
same-origin API.

| Page | What it does |
|---|---|
| **Playground** (`/`) | A real chat interface against `/v1/chat/completions`. Every response shows the actual routing decision — tier, provider/model, cache hit, latency, cost — not a mock-up. |
| **Dashboard** (`/dashboard.html`) | Live cost/cache/request metrics polling `/v1/stats`, plus circuit breaker states and a recent-requests feed. |
| **Providers & Routing** (`/providers.html`) | Visualizes each tier's fallback chain and lets an admin disable/enable a provider or force-reset a tripped circuit breaker — with immediate effect on live routing. |
| **Live Monitor** (`/monitor.html`) | Auto-refreshing table of every request with a click-through trace detail modal (prompt/response preview, full routing metadata). |
| **System Logs** (`/logs.html`) | Filterable log stream over every event type: requests, provider errors, circuit trips, rate-limit rejections, auth failures. |
| **API Keys** (`/keys.html`) | Real key management — create, view usage stats, and revoke keys backed by Redis, not a static `.env` list. |
| **Settings** (`/settings.html`) | Edit rate limits, cache TTL/threshold, and circuit breaker thresholds live — changes apply to new requests within ~2 seconds, no restart. |

Admin-gated pages (Providers, Keys, Settings) need the admin token, entered once and
stored in `localStorage` for that browser. The Playground only needs the API key.

---

---

## Using the API

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-key-123" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Explain how TCP handshakes work"}]
  }'
```

Response includes a `routing` block showing exactly what the gateway decided:

```json
{
  "id": "chatcmpl-...",
  "model": "llama-3.1-8b-instant",
  "choices": [{"message": {"role": "assistant", "content": "..."}}],
  "usage": {"prompt_tokens": 9, "completion_tokens": 142, "total_tokens": 151},
  "routing": {
    "complexity": "medium",
    "complexity_score": 0.31,
    "model_used": "llama-3.1-8b-instant",
    "provider_used": "groq",
    "cache_hit": false,
    "latency_ms": 412.6,
    "estimated_cost_usd": 0.000012,
    "fallback_used": false,
    "attempts": 1
  }
}
```

Set `"model"` to `"simple"`, `"medium"`, or `"complex"` to force a tier, or to a
literal model name to force a specific model.

Other endpoints:
- `GET /v1/stats` — cumulative cost, cache hit rate, savings
- `GET /v1/models` — configured tiers + live circuit breaker state per provider (incl. admin-disabled flag)
- `GET /v1/logs` / `GET /v1/logs/{trace_id}` — structured event log (powers Monitor/Logs/Trace-detail pages)
- `GET /health` — liveness + Redis connectivity
- **Admin API** (`X-Admin-Token` header required):
  - `GET/POST /v1/admin/keys`, `DELETE /v1/admin/keys/{key}` — API key management
  - `GET/POST /v1/admin/config`, `POST /v1/admin/config/reset` — live runtime config
  - `POST /v1/admin/providers/{name}/disable|enable|reset-circuit` — provider control

---

## The complexity classifier

Rather than a black-box model, the classifier is a transparent, tunable scoring
function over cheap-to-compute features — word count, presence of code blocks,
math terminology, reasoning keywords ("explain", "compare", "optimize"), multi-step
phrasing ("step by step", "and then"), question density. It returns a 0–1 score,
bucketed into `simple` / `medium` / `complex`.

This is a deliberate choice for a *routing* decision: you want it to be instant
(no model inference, no GPU, no cold start), free, and debuggable — "this scored 0.83
because of `reasoning_score` and `code_score`" is a much better production story than
"the neural net said so." `extract_features()` returns a clean numeric feature vector,
so if you outgrow the heuristic later, swapping in a trained `sklearn` classifier is a
one-file change — the rest of the router doesn't care how the tier was decided.

---

## Caching strategy

- **Exact cache**: `sha256(normalized_prompt + tier)` → Redis string, TTL-based.
  Catches identical repeat prompts (retries, polling, shared demo links) in O(1).
- **Semantic cache**: `HashingVectorizer` (scikit-learn, stateless, no fitting/training
  or network access required) turns each prompt into a fixed-length vector; new
  prompts are compared via cosine similarity against a bounded recent-prompt window
  kept in Redis. Catches paraphrases an exact hash would miss — e.g. *"what is the
  capital of France"* vs *"tell me France's capital city"*.

This is a deliberately lightweight-first choice over pulling in a transformer sentence-
embedding model (heavy download, needs a model registry, arguably overkill for a
gateway cache). `SemanticCache` is the seam where you'd swap in a real embedding model
later without touching the router.

---

## Circuit breaker

Classic 3-state machine per provider, backed by Redis so it's consistent across
multiple gateway replicas:

- **Closed** → requests flow normally, failures counted in a rolling window
- **Open** → too many recent failures; fail fast (no network call), router moves to
  the next hop in the fallback chain immediately
- **Half-open** → after the recovery window, exactly one probe request is let through;
  success closes the circuit, failure re-opens it

## Rate limiting

Redis sorted-set sliding window per API key — more accurate than a fixed-window
counter (no boundary-burst problem), atomic, O(log N).

---

## Benchmark results

`scripts/benchmark.py` sends a mixed workload (simple/medium/complex prompts, ~35%
exact-duplicate rate to simulate realistic traffic like retries and shared FAQs) and
compares actual spend against a naive baseline that always calls the largest model
with no caching. Run against the mock provider locally:

```bash
python scripts/benchmark.py --requests 300
```

**Actual run output:**

```
Total requests sent:        300
Cache hit rate:             281/300 (93.7%)
Tier distribution:          {'simple': 256, 'medium': 44}
Avg latency:                9.6 ms
p50 latency:                2.1 ms
p95 latency:                76.8 ms
------------------------------------------------------------
Actual cost (NeuroRouter):  $0.000038
Baseline cost (naive):      $0.006184
Estimated savings:          $0.006145  (99.4% reduction)
```

Real-world savings will be lower than this synthetic worst-case-for-baseline number
(a 35% exact-duplicate rate is workload-dependent), but the mechanism — cache hits
cost nothing, and 2 of 3 complexity tiers route to the cheap model — is exactly what
you'd point to in a "we cut inference costs by X%" interview story. Point the same
script at a Groq-backed deployment with `--requests` tuned to your real traffic
patterns to get a number for your own workload.

---

## Deploying live (Railway / Vercel + Redis)

1. Push this repo to GitHub.
2. **Railway**: New Project → Deploy from GitHub → it detects the `Dockerfile`
   automatically. Add a Redis plugin from Railway's marketplace, and set
   `REDIS_URL` to the plugin's connection string plus your `GROQ_API_KEY` in the
   service's environment variables.
3. Alternatively run `docker compose up` on any VM (Fly.io, a $5 DigitalOcean
   droplet, etc.) — it's self-contained.
4. Once deployed, your live demo link is the Railway/Fly URL + `/dashboard`.

---

## Running tests

```bash
pip install -r requirements-dev.txt
redis-server &          # tests use Redis db 15
pytest tests/ -v
```

16 tests covering the classifier's tier boundaries, exact + semantic cache hits/misses,
rate limiter enforcement, and the full circuit breaker state machine (closed → open →
half-open → closed).

---

## What this project demonstrates

- **Backend engineering depth**: async FastAPI, Redis-backed distributed state
  (not in-process — correct across replicas), sliding-window rate limiting, a real
  circuit breaker state machine, ordered fallback chains.
- **A thin, well-scoped ML component**: a fast, explainable complexity classifier —
  the right tool for a routing decision, not a sledgehammer.
- **Product thinking**: a concrete "before/after" cost story, a live dashboard,
  zero-key demoability, one-command deploy.
- **Testing discipline**: the state machine and caching logic are unit tested, not
  just eyeballed.

## What it can actually do today

- Act as a drop-in gateway in front of Groq (and optionally OpenAI as fallback) for
  any app making chat-completion calls.
- Automatically pick cheaper/faster models for simple queries and reserve the
  expensive model for genuinely complex ones.
- Deduplicate cost for repeated and paraphrased prompts.
- Survive a provider outage by failing over to the next hop instead of 500ing.
- Enforce per-API-key rate limits.
- Report live cost/savings/cache-hit metrics via API or dashboard.

## Natural next steps

- Streaming responses (SSE) for `stream: true`
- Per-tenant budgets/quotas, not just rate limits
- A trained (rather than heuristic) classifier once you have labeled routing data
- Prometheus/Grafana metrics export alongside the built-in dashboard
- Horizontal scaling behind a load balancer (the Redis-backed state design already
  supports this — it's an infra config change, not a code change)
