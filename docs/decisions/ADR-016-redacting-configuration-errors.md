# ADR-016: Configuration errors are redacted before they are printed

Date: 2026-11-08
Status: Accepted

## Context

Both processes validate their configuration at startup with a Pydantic
settings model, and both report a failure to the operator.

A test written in Step 8 asserted that a rejected `DATABASE_URL` does not
appear in the resulting error. It failed. The reason is that Pydantic appends
the rejected input to every error in a `ValidationError`:

```
1 validation error for Settings
database_url
  Value error, DATABASE_URL must use the 'postgresql+psycopg' scheme, got
  'postgresql' [type=value_error, input_value='postgresql://qa:sup3rs3cret@db/qa',
  input_type=str]
```

Every validator in `app/core/config.py` had been written carefully to report
only the *shape* of the problem — the URL scheme, the token's length — and the
wrapper undid that work. The field's type does not help either: the token is a
`SecretStr`, whose `repr` is `**********`, but `input_value` carries the raw
string the environment supplied.

Two startup paths printed it:

- `mcp_server/server.py`'s `main()` wrote `str(exc)` to stderr by design, so a
  short or malformed `INTERNAL_API_TOKEN` was printed in full.
- The backend's `app = create_app()` runs at import, so a `ValidationError`
  reached uvicorn, which printed the traceback — including `DATABASE_URL` with
  its password, `JIRA_API_TOKEN` and `INTERNAL_API_TOKEN`. In a container that
  output goes wherever container logs go, which is usually a log aggregator
  with a long retention period and a wider audience than the database
  password's.

The second is the serious one. It needs no attacker and no unusual conditions:
a typo in `.env` on the day somebody deploys is enough.

## Decision

Nothing prints a `ValidationError` from a settings model. Both startup paths
render it through `app.core.config.format_validation_error`, which rebuilds the
message from the field name and the rule and never includes a value:

```
mcp server configuration error:
  INTERNAL_API_TOKEN: is 15 characters; at least 32 are required. Generate one
  with python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Specifically:

- `format_validation_error(exc)` iterates
  `exc.errors(include_input=False, include_url=False)` and emits one
  `NAME: message` line per error, upper-casing the field name so it matches the
  environment variable an operator has to edit.
- `create_app` catches `ValidationError` and raises `RuntimeError` with that
  text, using `raise ... from None`. The `from None` matters as much as the
  rest: without it the original exception stays in the chain and the traceback
  prints its `repr`, which is what we are trying to avoid.
- `mcp_server.server.main` writes the same text to stderr and returns exit
  code 2, as before.

A consequence recorded in the function's own docstring: a validator's message
must not quote a secret either, because that message *is* passed through. The
existing ones report a scheme, a length or a field id.

This is the same rule `app/core/errors.py` already follows for a rejected HTTP
request, where `validation_error_handler` builds `location`, `message` and
`type` explicitly rather than serialising the errors wholesale. Step 8 simply
extends it to startup.

## Alternatives

- **Trust `SecretStr` to mask the value.** This was the implicit assumption
  before, and it is wrong for validation errors: the masking applies to the
  field's `repr`, not to the raw input recorded alongside the error.
- **Catch nothing and let the traceback print.** Simplest, and what most
  projects do. Rejected because of what this particular model holds. For a
  settings model, the input *is* the credential.
- **Log the error and exit zero.** Rejected: a process that cannot read its
  configuration must fail loudly, and exit code 2 is what tells Compose and
  Kubernetes it did.
- **Put the helper in a new shared module.** It lives in `app/core/config.py`
  instead, which is where every other validator already is. `mcp_server`
  imports it, which is a widening of the boundary described in
  `mcp_server/config.py` — but importing that module constructs nothing and
  reads no environment variable, so the gatekeeper still cannot obtain a
  database URL by accident. The import carries a comment saying so.
- **Redact by pattern matching the output**, e.g. masking anything that looks
  like a URL password. Rejected: a denylist of secret shapes fails the first
  time a secret has a new shape. Building the message from the field and the
  rule cannot leak a value because it never has one.

## Consequences

- An operator's error message is shorter and more useful than Pydantic's: it
  names the environment variable and what to do, not the value they can
  already see in their own `.env`.
- The `type=value_error` marker and the Pydantic documentation URL are gone.
  Nobody looking at "INTERNAL_API_TOKEN is 15 characters" needs either.
- Any new startup path that builds a settings object must use the helper. There
  is no mechanism forcing that; it is a review point, and the two existing call
  sites are the examples. A test in `tests/unit/test_validation.py` asserts the
  helper's behaviour, but cannot assert that a future caller uses it.
- `tests/unit/test_validation.py` keeps a test asserting that the *raw*
  `ValidationError` does echo the value. It documents why the helper exists, so
  nobody deletes it as redundant.
