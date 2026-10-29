# Architecture

## Layers

```
HTTP / CLI          app/api/routes.py, app/cli.py
                    thin: validate, delegate, serialise
      |
Orchestration       app/services/planner_service.py
                    picks an engine, handles fallback, caches, persists
      |
Domain services     budget.py  (the cost engine, owns all arithmetic)
                    ai/        (Anthropic client + prompts)
                    heuristic.py (template planner)
                    pdf.py     (ReportLab export)
                    repository.py (SQLAlchemy persistence)
      |
Models              trip.py   (input)
                    draft.py  (what the model fills in)
                    itinerary.py (what the user receives)
      |
Reference data      cost_data.py + data/places.json
```

The rule that keeps this honest: **if a route grows a calculation, that
calculation belongs in a service.** Routes validate, call one method, and
serialise.

## Request lifecycle

`POST /api/itineraries`

1. **Rate limit** — a sliding window, applied only to generation, because that
   is the one route that costs a model call.
2. **Validate** — `TripRequest` rejects impossible trips (return before
   departure, negative budget, 31-day trips, same origin and destination) before
   any work happens.
3. **Cache** — the brief is canonicalised and hashed; an identical brief is
   served from memory.
4. **Baseline and envelope** — reference pricing, then per-unit budget targets.
5. **Draft** — Claude if a key is configured, otherwise the template planner.
   Any model failure falls through to the template planner.
6. **Reconcile** — clamp, multiply, sum, verdict.
7. **Assemble, persist, cache, return.**

## Why the draft and the itinerary are different types

`ItineraryDraft` has no totals. That is not an oversight — it is the mechanism.

If the model could return a total, something downstream would eventually trust
it, and the one guarantee this project makes would be gone. Keeping totals out
of the schema means the compiler and the type checker enforce the rule, not a
code review.

The two types also have different *jobs*:

| | `ItineraryDraft` | `Itinerary` |
|---|---|---|
| Produced by | the model, or the template planner | the cost engine |
| Money | unit prices | reconciled party totals |
| Optional fields | none — all required, none nullable | several |
| Extra keys | forbidden (strict JSON Schema) | n/a |
| Lives in | one function call | the database, the API, the PDF |

## Two planners, one costing path

`AIPlanner.draft()` and `HeuristicPlanner.draft()` both return an
`ItineraryDraft`. `PlannerService` picks one; `BudgetEngine.reconcile()` is
indifferent to which.

This buys three things:

- The repository runs with no API key, so anyone can clone and use it.
- The test suite is free, fast, deterministic and offline — the template
  planner is also the fixture that exercises the cost engine.
- An API outage degrades to a usable plan with an honest label instead of a 500.

## The model call

One streamed request per plan:

- **Structured output** — `output_format=ItineraryDraft` pins the response to a
  JSON Schema generated from the Pydantic model, and the SDK validates the
  reply. No prose parsing anywhere.
- **Streaming** — a 30-day itinerary is a large response, and a non-streaming
  request with a high `max_tokens` risks an HTTP timeout.
- **Prompt caching** — `SYSTEM_PROMPT` is byte-identical across requests and
  sits behind an `ephemeral` cache breakpoint; the trip brief follows it.
- **Adaptive thinking** — always on for this model family; depth is set by
  `output_config.effort`, not a token budget.

Every failure is converted to a typed `AIUnavailableError`:

| Condition | Handling |
|---|---|
| `AuthenticationError` | key rejected |
| `RateLimitError` | try again shortly |
| `APITimeoutError` / `APIConnectionError` | service unreachable |
| `APIStatusError` | logged with status, request rejected |
| `stop_reason == "refusal"` | surfaced with its category |
| `stop_reason == "max_tokens"` | JSON is truncated, so it is rejected |
| `parsed_output is None` | did not match the schema |

`PlannerService` catches all of them and falls back, unless
`WTN_FALLBACK_TO_HEURISTIC=false`.

## Persistence

One table. The whole itinerary is stored as JSON in a single column, with a few
denormalised columns beside it for the listing query.

An itinerary is always read and written whole, is never queried by activity, and
its shape is already validated by a Pydantic model. Normalising it across a
dozen tables would add joins and migrations to buy nothing.

`database_url` accepts any async SQLAlchemy URL, so moving to Postgres
(`postgresql+asyncpg://…`) needs no code change.

## Cross-cutting pieces

- **Cache** — TTL + LRU, in process. Keyed on a hash of the canonicalised brief,
  so two requests differing only in field order share an entry.
- **Rate limiter** — sliding window, in process, bounded so a long-running
  process cannot accumulate one deque per IP ever seen.
- **Logging** — one JSON object per line, ASCII-escaped. Reserved `LogRecord`
  attribute names must never be used as `extra` keys; `message` in particular
  raises `KeyError` inside `logging` itself, and there is a test pinning this.
- **Errors** — every domain exception carries an HTTP status and a stable
  `code`, rendered through one handler into one envelope.

## Known limits

| Limit | When it matters | Fix |
|---|---|---|
| Cache and rate limiter are per process | More than one worker | Move both to Redis; the interfaces already fit |
| `create_all` instead of migrations | The schema starts changing | Add Alembic |
| No authentication | Multi-user deployment | Add auth and scope itineraries to a user |
| Pricing is indicative | Users book from it | Replace the two functions named in `docs/cost-model.md` |
| PDF renders synchronously | Very long trips under load | `anyio.to_thread.run_sync` |
