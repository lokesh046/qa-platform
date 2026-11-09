# Step 11: The model client, and validating what it returns

Date: 2026-11-09
Phase: 2

## Goal

Give the platform a way to call a model, and a way to tell whether what came
back is usable. These are the two halves of BRD generation that have nothing to
do with Jira, the database or the MCP server, so they are built and tested on
their own before anything is wired together.

## What was done

| Path | Why |
| --- | --- |
| `docs/decisions/ADR-023-…` | The provider decision: one OpenAI-compatible client, base URL and model as configuration, defaulting to GitHub Models. Settles what ADR-021 reopened. |
| `app/core/config.py` | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, timeout, max tokens and max retries. A `generation_available` property, and `LLM_API_KEY` added to the placeholder-secret detection. |
| `app/llm/client.py` | The only code in `app/` that talks to a model. Pooled `httpx` client, bounded retries, one error type per failure class, token usage reported. |
| `app/llm/openspec_format.py` | Parse and validate a reply against OpenSpec's Requirement/Scenario shape. Pure functions. |
| `app/llm/prompt.py`, `prompts/brd_v1_*.md` | Committed in the previous pass; unchanged by this one, which is the point of having kept prompt assembly free of any client. |
| `app/main.py` | The model provider's connection pool is closed on shutdown, alongside the database, Redis and Jira pools. |
| `tests/unit/test_llm_client.py` | 54 tests. One case per status, every malformed-reply shape, the retry schedule, and secret hygiene. |
| `tests/unit/test_openspec_format.py` | 45 tests, written against the failure modes models actually produce. |
| `tests/conftest.py` | `make_llm_settings`, and **14 more names in the environment-scrub list**, so a developer's real `LLM_API_KEY` cannot reach a test. |
| `.env.example`, `README.md` | The new variables, with which-is-which comments: three GitHub-adjacent secrets now exist. |

## How it works

### Generation is optional

`LLM_MODEL` has no default, because GitHub Models names models by publisher and
the catalogue changes — a default would be a model id this project guessed.
Unset, **the backend still starts** and Phase 1 keeps working; only generation
is unavailable, and it says so in a sentence naming the variable:

```
Document generation is not configured on this server. Missing: LLM_MODEL,
LLM_API_KEY. List available models with GET https://models.github.ai/catalog/models
```

That check happens **before** any network call, so an unconfigured server does
not produce a 401 from the provider that reads like the provider's fault.

### The client is a transport, not a policy

It maps HTTP outcomes onto this project's error types and returns what the
provider said — including a reply that was cut off. Deciding whether a reply is
*acceptable* belongs to the service that asked for it. That is the same split
as `modules/jira/client.py` against `modules/jira/service.py`.

Two mappings worth knowing:

- **404 blames the model name**, not the URL. On an OpenAI-compatible endpoint
  that is nearly always what it means, and "not found" would send someone to
  check the path.
- **429 is its own error type**, not a generic upstream failure. GitHub Models'
  free tier is rate limited per model, so this will happen, and the fix is a
  quota decision rather than a bug hunt.

Retries cover a timeout, a connection failure, a 429 and a 5xx, with backoff of
0.5s then 1.0s. Never a 4xx: a request this code built wrongly will be built
wrongly again.

### Truncation is not shortness

`finish_reason == "length"` means the reply was cut off. `Completion.was_truncated`
exposes it, because a truncated requirements document looks complete —
well-formed requirements, just fewer of them — and would otherwise pass
validation and be stored as finished work.

### What validation is for

A model's reply is untrusted input, and when it is wrong it is wrong
confidently. The validator handles what models really do:

| Reply | Outcome |
| --- | --- |
| The document, clean | accepted |
| The document inside a ``` fence, after "Here are the requirements:", with "Let me know if you need changes." after it | accepted; fence, preamble and trailing chatter stripped |
| A requirement with no scenarios | **refused** — it cannot become a test, which is the only reason the document exists |
| A scenario with a **WHEN** and no **THEN** | **refused** — a trigger with no expectation reads like a test and cannot be one |
| "I'd be happy to help! Could you tell me more?" | **refused** |
| Only `## Open questions` | **accepted**, flagged `needs_more_information` |

The last row is deliberate. The prompt asks for exactly that when a ticket says
too little, and an honest "this ticket does not say what should happen" is more
useful than invented requirements.

`validate` returns a report rather than raising, and reports **every** problem
rather than the first — tuning a prompt one error per generation is slow. A
failed report carries no document, so a caller cannot store the result of a
check it did not read.

## How to run / verify

```bash
uv run pytest -q             # 568 passed
uv run ruff check .          # All checks passed!
uv run ruff format --check . # 143 files already formatted
```

The suite needs nothing running, and ignores whatever is in your environment:

```bash
# A real key exported must not reach a test, and respx must still intercept.
env LLM_API_KEY=real LLM_MODEL=real/model uv run pytest -q   # 568 passed

docker compose down
mv .env .env.hidden && uv run pytest -q; mv .env.hidden .env  # 568 passed
```

Confirm generation is off until configured, without starting anything:

```bash
uv run python -c "
from app.core.config import get_settings
print('generation available:', get_settings().generation_available)"
```

### The first live call

Not done yet, and it needs two things in `.env`:

```bash
LLM_MODEL=<a name from the catalogue>
LLM_API_KEY=<a GitHub fine-grained token scoped models:read>
```

List the catalogue first, because the name must be exact:

```bash
curl -s -H "Authorization: Bearer $LLM_API_KEY" \
     https://models.github.ai/catalog/models | python3 -m json.tool | head -40
```

A wrong name produces a 404, which this client reports as "does not recognise
the model '<name>'" rather than as a missing endpoint.

## Decisions and trade-offs

**GitHub Models as the default, not Anthropic.** ADR-023 in full. Short version:
it is free, server-side, and adds no vendor for anyone to approve, which
matters more in Phase 2 than output quality does. ADR-020's preference for
Claude is one `.env` change away and is the obvious upgrade once prompt quality
rather than plumbing is the bottleneck.

**One client with a configurable base URL, not a provider abstraction.** Azure,
OpenAI, most gateways and Anthropic's compatibility endpoint all speak this
shape. A second implementation should appear when a provider that does not is
actually needed, not before.

**Parsing and validating are separate functions.** The parser extracts what is
there and records nothing about what is missing; the validator decides whether
that is enough. Keeping them apart puts every error message in one place and
means the parser does not have to guess which omissions matter.

**The limits are not style rules.** `MAX_REQUIREMENTS = 40` next to a prompt
asking for one to eight; `MAX_SCENARIOS_PER_REQUIREMENT = 20` next to "at most
six". They are the point past which something has gone wrong, not an opinion
about document length.

**`normalise` keeps an inner code fence.** A requirement may legitimately quote
a JSON payload. Taking the outermost fence pair and keeping it only if a `##`
heading lies between them handles the wrapper case without eating a fence that
is part of the document. This was found by a failing test: the first version
handled a fence *or* a preamble but not both, and left the closing fence and
the trailing "Let me know if you need changes" in the stored document.

## Security notes

- **The prompt is never logged.** It contains the ticket's text, which is
  somebody else's content and may contain anything. ADR-009's rule about what
  the platform stores applies to log lines too, and a test asserts it: a
  generation that times out must not put the prompt in the log.
- **`LLM_API_KEY` appears in no log line, no error message and no URL.**
  Asserted across six statuses, not assumed. The credential lives on the
  pooled client, so no call site handles it.
- **A rejected credential names the variable, not the value.** "Check
  LLM_API_KEY has the models:read scope" — an operator knows what to fix and
  the log holds nothing worth stealing.
- **The reply is bounded twice.** `max_tokens` bounds a well-behaved provider;
  `MAX_RESPONSE_BYTES` (2 MB) bounds the other kind, and is checked before the
  body is parsed. `MAX_DOCUMENT_CHARS` (60 000) bounds what reaches the parser.
- **14 variables were added to the test environment scrub list.** Without them,
  a developer with `LLM_API_KEY` exported would have had it reach a test, and
  `respx` does not intercept a call to a host it was not told about — so the
  suite could have made a real, billable request. Verified by running the suite
  with a fake key exported.
- **`.env.example` gained two secrets as empty values**, never as examples.
  Comments say which is which, because `LLM_API_KEY`, `GITHUB_CLIENT_SECRET`
  and `JIRA_API_TOKEN` are all GitHub- or Atlassian-issued strings and putting
  the wrong one in the wrong place is a realistic mistake.
- **Ticket text will leave the network when generation runs.** Narrower than
  ADR-020's position, since GitHub is not a new vendor here, but still a fact
  about where ticket content travels. It belongs in whatever review precedes a
  real tenant, and it is recorded in ADR-023 rather than discovered later.

## Known issues / TODO

- **Nothing calls either module yet.** `app/llm/` is complete and unused:
  no endpoint, no MCP tool, no database row. That is Step 12 (the
  `brd_documents` migration and the `brd` module) and Step 13 (the
  `generate_brd` tool).
- **No live call has been made.** The suite mocks the provider, so the first
  real generation is still unverified — the same posture Step 6 took before
  Jira was connected, and it found real problems then.
- **Truncation is detected and nothing acts on it.** `was_truncated` has no
  consumer until the service exists; the service must refuse to store a
  truncated document.
- **The validator does not check that a BRD is about the right issue.** A model
  can return a well-formed document describing something else entirely.
  Detecting that needs the issue to compare against, so it belongs in the
  service, and it is not obvious how to do it without being brittle.
- **Prompt quality is unmeasured.** There is no way to tell whether `brd_v1`
  produces good requirements, because judging that needs a person. The
  `approvals` module in Phase 3 is where that becomes a workflow step.
- **Still no integration tests and still no CI**, now with a third reason:
  nothing automatically checks that generated documents validate.
