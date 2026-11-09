# ADR-023: One OpenAI-compatible client, defaulting to GitHub Models

Date: 2026-11-09
Status: Accepted
Supersedes: the provider and model choice in ADR-020, which ADR-021 reopened

## Context

ADR-020 chose Anthropic with an `ANTHROPIC_API_KEY`. ADR-021 reopened the
question when the plan to use the caller's own model through MCP sampling was
rejected — sampling is deprecated in the current protocol and implemented by
roughly two of twenty-four clients.

So generation runs server-side, and the question is which provider. Three
constraints, in the order they matter:

1. **It must work from every client.** The tools have to be usable from any AI
   chat the team can reach, which means the model is this platform's
   responsibility and not the client's.
2. **It must work unattended.** Phases 3 to 5 run LangGraph workflows and
   Playwright execution from the `jobs` table, with nobody watching.
3. **Adding a vendor has a cost that is not the invoice.** Sending ticket text
   to a new processor is a question for whoever owns the real Jira. The team
   already uses GitHub.

## Decision

**One client, speaking the OpenAI chat-completions shape, with the base URL,
model and key as configuration. It defaults to GitHub Models.**

1. **`app/llm/client.py` is the only code that talks to a model.** An async
   `httpx` client posting to `{LLM_BASE_URL}/chat/completions`. Nothing else in
   `app/` opens a model connection, so a second provider is one module.
2. **GitHub Models by default.** `LLM_BASE_URL=https://models.github.ai/inference`,
   authenticated with a fine-grained GitHub token scoped `models:read`. Every
   GitHub account has rate-limited access at no cost, with a paid opt-in for
   higher limits. No new vendor, no invoice to approve, and it runs server-side
   so constraints 1 and 2 hold.
3. **`LLM_MODEL` has no default, and generation is an optional feature.**
   GitHub Models names models by publisher, such as `openai/...` or
   `meta/...`, and the catalogue changes, so a default here would be a model id
   this project had guessed.

   Unset, the backend **still starts** and everything from Phase 1 keeps
   working; only BRD generation is unavailable, and it says so in one sentence
   naming the variable and pointing at `GET
   https://models.github.ai/catalog/models`. Startup logs once that generation
   is off. Refusing to boot would mean an unset optional feature taking down
   issue reads, which is a worse failure than the one it prevents.
4. **The OpenAI shape is the interface, not a provider.** Azure OpenAI, OpenAI
   itself, most gateways and Anthropic's compatibility endpoint all speak it. So
   "which provider" becomes a `.env` change rather than a code change, and
   ADR-020's preference for Claude on quality can be acted on without touching
   this module.
5. **Generation is bounded on three axes**: a request timeout, `max_tokens` on
   the reply, and a cap on retries. Retries are only for a timeout, a
   connection failure, a 429 or a 5xx — never for a 4xx, which will fail again.
6. **The reply is untrusted input.** It is validated by
   `app/llm/openspec_format.py` before anything is stored. A model that returns
   prose, a code fence, an apology or a well-formed document about the wrong
   issue is a normal occurrence, not an exception.
7. **Usage is recorded, not just logged.** The prompt and completion token
   counts the provider reports are stored on the generated document, so spend
   is attributable per actor and per issue rather than appearing as one monthly
   figure.

## Alternatives

- **Anthropic directly, as ADR-020 chose.** Better at writing requirements from
  a thin ticket, which is the hard case. Rejected as the *default* because it
  needs a paid key and a new data processor to do a job GitHub already does for
  free. Point 4 keeps it one `.env` change away, and it is the obvious upgrade
  once prompt quality rather than plumbing is the bottleneck.
- **A provider abstraction with an implementation per vendor.** Rejected as
  premature: one interface with a configurable base URL already covers every
  provider under consideration. A second implementation should appear when a
  provider that does not speak this shape is actually needed.
- **Copilot's model through MCP sampling.** Rejected in ADR-021, with evidence.
- **A self-hosted open-weights model.** Rejected for Phase 2: it trades a
  per-call cost for GPU capacity and operations, and requirements writing has a
  high quality bar. Worth revisiting if data residency rules forbid sending
  ticket text anywhere, which is a question for whoever owns the real Jira.

## Consequences

- **A GitHub token with `models:read` is a new credential in `.env`**, covered
  by the startup placeholder warning like every other. It is not the OAuth
  app's client secret and not the Jira token; three GitHub-adjacent secrets now
  exist and the names must stay distinguishable.
- **The free tier is a prototyping tier.** Limits vary by model and by Copilot
  plan, and a workflow in Phase 3 that generates for fifty tickets will meet
  them. The client treats 429 as retryable with backoff and surfaces it
  clearly rather than failing as "upstream error", because the fix is a quota
  decision rather than a bug.
- **Ticket text goes to GitHub.** Narrower than ADR-020's position — no new
  vendor — but still a statement about where ticket content travels, and it
  belongs in whatever review precedes a real tenant.
- **Output quality is now a configuration property.** Two deployments with
  different `LLM_MODEL` values produce different BRDs from the same ticket. The
  model is recorded on every document, so a bad batch can be attributed.
- **Nothing is testable against the real provider in the suite.** The HTTP
  boundary is mocked with respx, as Jira is (ADR-018). The first live call is a
  manual verification step in the step log, which is the same posture Step 6
  took with Jira.
