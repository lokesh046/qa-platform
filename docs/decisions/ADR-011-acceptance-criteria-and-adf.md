# ADR-011: Resolving acceptance criteria, and handling ADF

Date: 2026-11-08
Status: Accepted

## Context

The MCP tool in Step 7 must return an issue's summary **and its acceptance
criteria**. Jira Cloud has no acceptance-criteria field. Teams express them in
one of two ways:

- a custom field, whose id differs per tenant (`customfield_10040` in one
  tenant, `customfield_10127` in another), discoverable at `/rest/api/3/field`;
- a heading inside the description, usually "Acceptance Criteria".

Separately, Jira's REST API v3 returns every rich text field as **Atlassian
Document Format**: a JSON tree of typed nodes, not text. The platform stores
and displays plain text, so something must flatten it. ADF has around thirty
node types and Atlassian adds more.

The content inside that tree is written by whoever filed the ticket, which on
an internal QA board can include a customer.

## Decision

**Custom field first, description heading as fallback.**

`JIRA_ACCEPTANCE_CRITERIA_FIELD` optionally names the tenant's custom field.
When it is set and the field holds anything, that value wins. Otherwise
`adf.extract_acceptance_criteria` looks for a heading matching
`^\s*(acceptance\s+criteria|acceptance|\bac\b)\s*:?\s*$` and returns everything
from there until the next heading at the same level or shallower. When neither
yields anything, `acceptance_criteria` is `null`.

The setting is validated to look like `customfield_<digits>`, rejecting the
likely mistake of entering the field's display name.

**Section extraction works on the ADF tree, not the flattened text.** A heading
is an unambiguous node type; in plain text, "Acceptance Criteria" could be a
list item or a sentence.

**ADF flattening lives in its own module, `app/modules/jira/adf.py`, as pure
functions.** No I/O, no ORM. It takes a dict and returns a string.

**Two hard limits, because the input is untrusted:** recursion depth
(`MAX_DEPTH = 24`) and output size (`MAX_OUTPUT_CHARS = 60_000`, truncation
marked rather than silent).

**Unknown node types are descended into, not dropped.** A future ADF node will
almost certainly still carry its text in `content`.

**Only the fields the platform stores are requested from Jira.** The `fields`
query parameter lists `summary`, `description`, `issuetype`, `status`,
`updated`, plus the AC field when configured.

## Alternatives considered

**Require the custom field, with no fallback.** Precise and predictable: what
the team put in the field is what the tool returns. Rejected as the sole
mechanism because it returns `null` for every issue until somebody finds their
tenant's field id, which makes the Step 7 demo fail for a reason that looks
like a bug. The field still wins when configured, so nothing is lost.

**Parse the description only, with no custom field support.** No configuration.
Rejected: a team using a dedicated field usually has an empty or boilerplate
description, so the tool would return nothing for exactly the teams who have
been most careful.

**Ask Jira for all fields and search for one named "Acceptance Criteria".**
Removes the configuration entirely — fetch `/rest/api/3/field` once, find the
field whose name matches, remember its id. Genuinely attractive and probably
right eventually. Rejected for Phase 1 because it needs a second Jira call and
a cache for the field map, it breaks when two custom fields have similar names,
and `fields=*all` would pull comments, attachments and worklogs across the
wire, which ADR-009 says must not be stored — and the surest way not to store
something is not to ask for it.

**Use REST API v2, which returns wiki markup instead of ADF.** No ADF parser
needed. Rejected: wiki markup still needs a parser, v2 is the legacy API, and
v3 is where Atlassian adds fields.

**Store the ADF JSON and render it at read time.** Keeps full fidelity and lets
the renderer improve without a re-fetch. Rejected by ADR-009: that is the raw
payload, with everything that implies.

**Use an off-the-shelf ADF library.** Checked; the available packages are thin,
unmaintained, or render to HTML rather than text. The subset needed here is
about 200 lines and the alternative is a dependency on somebody's weekend
project for the component that parses untrusted input.

## Consequences

- The endpoint produces useful acceptance criteria on a tenant with no
  configuration at all, and exact ones once the field id is supplied. Both
  paths are verified.
- The heading regex is loose, so a heading like "Acceptance" or "AC:" matches.
  A false positive returns the wrong section rather than failing, which is the
  better failure direction but is still wrong.
- Adding a Jira field means editing `_BASE_FIELDS` in `client.py` and adding a
  column; see ADR-009 for why that friction is intentional.
- `adf.py` will meet node types it does not special-case. It descends rather
  than dropping, so the text survives even when the structure does not.
- A pathological document is truncated at 60,000 characters with a marker, and
  a document nested deeper than 24 levels loses the over-deep branch. Both are
  bounded and neither raises.
- The parser is the single most testable component in the module and currently
  has no committed test. That is the gap most worth closing first.
