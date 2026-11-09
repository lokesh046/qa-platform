# Spec Delta

## Purpose

Produces each document in the chain from the one before it, so that a brief
becomes a BRD, a BRD becomes an FSD, an FSD becomes a step procedure, and a
step procedure becomes a proposed story breakdown — with each result checked
for shape before anyone is shown it.

## ADDED Requirements

### Requirement: A document is generated from one source

The system SHALL generate a document from exactly one source: a brief, or the
document immediately before it in the chain. The generated document records
which source produced it, so any document's lineage back to its brief is
recoverable.

#### Scenario: Generating a BRD from a brief

- **WHEN** a user generates a BRD naming a stored brief
- **THEN** a BRD document is produced from that brief and records it as its
  source

#### Scenario: Generating an FSD from a BRD

- **WHEN** a user generates an FSD naming a BRD
- **THEN** an FSD is produced and records that BRD as its source

#### Scenario: Generating from the wrong kind of source

- **WHEN** a user attempts to generate a step procedure naming a brief rather
  than an FSD
- **THEN** the request is refused, naming the kind of source that stage
  requires

### Requirement: The chain's stages are fixed

The supported stages SHALL be: brief to BRD, BRD to FSD, FSD to step
procedure, and step procedure to story breakdown. No other pairing is
generated.

#### Scenario: Requesting an unsupported pairing

- **WHEN** a user attempts to generate a story breakdown directly from a BRD
- **THEN** the request is refused, naming the stage that must come first

### Requirement: Only a verified source may be used

The system SHALL refuse to generate from a document that a person has not yet
verified. A brief needs no verification, because a person wrote it.

#### Scenario: Generating from an unverified document

- **WHEN** a user attempts to generate an FSD from a BRD that no person has
  verified
- **THEN** the request is refused, stating that the source must be verified
  first, and nothing is generated

#### Scenario: Generating from a verified document

- **WHEN** a user generates an FSD from a BRD a person has verified
- **THEN** generation proceeds

### Requirement: Access is decided before anything is generated

The system SHALL confirm the caller is a registered, active user holding a
grant for the chain's project before it sends any content to a model.

#### Scenario: A caller without a grant

- **WHEN** a user with no grant for the chain's project requests generation
- **THEN** the request is refused, no content is sent to any model, and the
  refusal is indistinguishable from the chain not existing

#### Scenario: A deactivated account

- **WHEN** a deactivated account requests generation
- **THEN** the request is refused and no content is sent to any model

### Requirement: The result must have the shape its type requires

The system SHALL check a generated document against the structure required for
its type and SHALL NOT store a result that fails. A document that does not
validate is recorded as a failed attempt with a reason.

#### Scenario: A result that does not validate

- **WHEN** a model returns prose instead of a document of the requested type
- **THEN** no document is stored, the attempt is recorded as failed with a
  reason, and the caller is told the result was unusable

#### Scenario: A result cut off before it finished

- **WHEN** a model's reply stops because it reached its output limit
- **THEN** the result is treated as a failure rather than as a short document,
  because an incomplete document reads as a complete one

### Requirement: A story breakdown proposes work without creating it

A story breakdown SHALL be a document describing proposed stories, each with
its tasks and subtasks. The system SHALL NOT create, modify or delete anything
in Jira.

#### Scenario: Generating a story breakdown

- **WHEN** a user generates a story breakdown from a verified step procedure
- **THEN** a document is produced listing stories with their tasks and
  subtasks, and nothing is written to Jira

#### Scenario: Confirming Jira is untouched

- **WHEN** a story breakdown has been generated
- **THEN** no issue in Jira has been created or altered by the platform

### Requirement: The delivery phase label travels down the chain

Where a source document marks content with a delivery phase of the product
being specified, the generated document SHALL carry that phase onto the
content derived from it.

#### Scenario: A phase-marked requirement produces phase-marked stories

- **WHEN** an FSD marks a requirement as Phase 2 and a step procedure and
  story breakdown are generated from it
- **THEN** the steps and stories derived from that requirement are also marked
  Phase 2

#### Scenario: Content with no phase

- **WHEN** a source marks no phase on some content
- **THEN** the derived content carries no phase, rather than a guessed one

### Requirement: Generating is explicit and does not repeat by accident

Generation SHALL happen only because a caller asked for it. Asking again for a
document that already exists SHALL return the existing one unless the caller
explicitly requests a new generation.

#### Scenario: Asking twice

- **WHEN** a user requests a BRD for a brief that already has one, without
  asking to regenerate
- **THEN** the existing document is returned, and no model is called

#### Scenario: Explicitly regenerating

- **WHEN** a user requests generation and asks for a new one
- **THEN** a new document is produced, and the previous one is kept

### Requirement: Generation is optional and says so when absent

Where no model is configured, the system SHALL continue to serve everything
else and SHALL refuse generation with a message naming what is missing.

#### Scenario: No model configured

- **WHEN** a user requests generation on a server with no model configured
- **THEN** the request is refused with a message naming the missing
  configuration, and reading existing documents still works

#### Scenario: Access outranks configuration

- **WHEN** a user with no grant for the project requests generation on a
  server with no model configured
- **THEN** the refusal is the access refusal, so an unauthorised caller learns
  nothing about how the server is configured

### Requirement: What an attempt cost is recorded

The system SHALL record, for every generation attempt including a failed one,
which prompt version and which model produced it and what it consumed.

#### Scenario: A failed attempt that was still charged for

- **WHEN** a model returns an unusable result
- **THEN** the failed attempt records the model and the usage it reported, so
  cost is attributable even when nothing was produced

### Requirement: Source content is treated as data

Content taken from a brief, an earlier document or a Jira issue SHALL be
placed into a prompt as data rather than as instructions, and SHALL NOT be
written into any log.

#### Scenario: A source containing instructions aimed at a model

- **WHEN** a source document contains text instructing the model to ignore its
  instructions
- **THEN** generation treats that text as content, and the result is still
  checked for shape before it is stored

#### Scenario: A generation that fails

- **WHEN** a generation attempt fails for any reason
- **THEN** no part of the prompt or the source content appears in any log
  entry
