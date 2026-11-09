# ADR-020: Anthropic Claude Sonnet 5.5, configured not hardcoded

Date: 2026-11-09
Status: Accepted (the specific provider is reopened; see ADR-021)

## Context

Phase 2 needs a model to turn a Jira ticket into structured requirements. That
is the first time this platform spends money per request, calls a service whose
output is non-deterministic, and depends on a vendor for a core feature. Four
things had to be settled before any of it is written: which provider, which
model, where the key lives, and how tests avoid paying for a test run.

## Decision

1. **Anthropic, `claude-sonnet-5-5` by default.** Fast and inexpensive, and
   strong enough for turning a ticket into requirements. Phase 2 will be run
   dozens of times while the prompt is tuned, and that is the usage pattern the
   default should suit.
2. **The model is configuration, not a constant.** `LLM_MODEL` in the
   environment, validated at startup like everything else. Changing to
   `claude-opus-5-5` is one line in `.env` and a container restart, so a thin
   ticket that needs a stronger model is not a code change.
3. **`ANTHROPIC_API_KEY` is a `SecretStr` in `Settings`**, read from the
   environment, covered by the existing placeholder detection so the startup
   warning reports it like every other credential. Never logged, never in a
   document, never in a commit.
4. **Prompts are files under `app/llm/prompts/`, with a version string.** The
   version is recorded on every generated document, so "which prompt produced
   this BRD" is answerable six months later. A prompt is behaviour; it belongs
   in a diff and in review, not in a Python string literal.
5. **Tests mock the provider at the HTTP boundary** with `respx`, exactly as
   Jira is mocked (ADR-018). No test in `tests/unit` may reach
   `api.anthropic.com`. A test suite that costs money is a test suite people
   stop running.
6. **Generation is always explicit.** It happens because somebody called the
   tool, never as a side effect of reading an issue. The input is the cached
   row in `jira_issues`, so generating a BRD never triggers a Jira call either.

## Alternatives

- **`claude-opus-5-5` as the default.** Better at inferring unstated
  requirements from a thin ticket, which is exactly the hard case here.
  Rejected as a *default* only, on cost during prompt iteration; point 2 makes
  it an env var away, and Phase 3 may well set it for generation while leaving
  Sonnet for cheaper summarisation work.
- **A per-request model override in the API.** Useful for comparing output on
  one ticket. Deferred: it is a parameter on an endpoint nobody has used yet,
  and it needs a policy on who may choose an expensive model. Add it when
  there is a reason.
- **Another provider, or several behind an abstraction.** Rejected for Phase 2.
  One provider, one client, no premature interface. The client lives in
  `app/llm/` precisely so that a second one would have one place to go.
- **A self-hosted open-weights model.** Rejected for Phase 2: it moves the cost
  from per-call to GPU capacity and operations, and the quality bar for
  requirements writing is high. Worth revisiting if ticket volume makes the
  per-call cost material, or if data residency rules forbid sending ticket text
  to a third party — which is a question for whoever owns the real Jira, not
  for this ADR.

## Consequences

- **Ticket text leaves the network.** A Jira description goes to Anthropic.
  That is a new data-handling fact about this platform, not a technical detail,
  and it belongs in whatever review this platform gets before it reads a real
  tenant's tickets. ADR-009 already limits what we *store*; this is about what
  we *send*.
- **One more long-lived secret in `.env`.** Rotation applies to it as it does
  to `JIRA_API_TOKEN`.
- **Generation can fail in ways a database read cannot**: a rate limit, a
  timeout, a refusal, or valid markdown that is wrong. The failure must be
  clean — no half-written document stored, a clear message to the caller, and
  an `error` row in the audit trail with the reason.
- **Output is non-deterministic**, so no test can assert the exact text of a
  generated BRD. Tests assert structure, the gates around generation, and what
  is recorded. Judging quality is a human review step, which is what the
  `approvals` module exists for in Phase 3.
- **Cost is now a property of a request.** The audit detail records the model
  and the prompt version; token counts are recorded on the document row so
  spend is attributable per actor and per issue rather than appearing as one
  monthly figure.
