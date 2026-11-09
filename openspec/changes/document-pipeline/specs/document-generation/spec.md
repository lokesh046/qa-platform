# Spec Delta

## Purpose

Supplies the client with everything a stage needs — the source content and the
shape the answer must take — then checks what comes back and stores it with
its lineage, so that each link of the chain is written by the model already in
the conversation rather than by a model this tool pays for.

## ADDED Requirements

### Requirement: Starting a stage returns its context and its shape

The system SHALL provide an operation that, given a stage and its source,
returns the source content and the instructions describing the shape the
result must take. It SHALL NOT itself produce the document.

#### Scenario: Starting the BRD stage

- **WHEN** a user starts the BRD stage naming a stored brief
- **THEN** the brief's text, any attached Jira issue content, and the
  instructions for writing a BRD are returned, together with a draft
  identifier

#### Scenario: Starting a later stage

- **WHEN** a user starts the FSD stage naming a verified BRD
- **THEN** the BRD's current text and the instructions for writing an FSD are
  returned

#### Scenario: Nothing is written by starting

- **WHEN** a stage is started
- **THEN** no document exists yet, and starting the same stage again returns
  the same context rather than accumulating anything

### Requirement: The chain's stages are fixed

The supported stages SHALL be: brief to BRD, BRD to FSD, FSD to step
procedure, and step procedure to story breakdown. No other pairing is offered.

#### Scenario: Requesting an unsupported pairing

- **WHEN** a user attempts to start a story breakdown directly from a BRD
- **THEN** the request is refused, naming the stage that must come first

#### Scenario: Starting a stage from the wrong kind of source

- **WHEN** a user attempts to start the step procedure stage naming a brief
- **THEN** the request is refused, naming the kind of source that stage
  requires

### Requirement: Only a verified, current source may be used

The system SHALL refuse to start a stage whose source has not been verified by
a person, or whose source has changed since. A brief needs no verification,
because a person wrote it.

#### Scenario: An unverified source

- **WHEN** a user starts the FSD stage from a BRD nobody has verified
- **THEN** the request is refused, stating that the source must be verified
  first

#### Scenario: A rejected source

- **WHEN** a user starts a stage from a rejected document
- **THEN** the request is refused, stating that the source was rejected

#### Scenario: A stale source

- **WHEN** a user starts a stage from a verified document whose own source has
  since been corrected
- **THEN** the request is refused, stating that the source changed and must be
  verified again

### Requirement: A drafted document is checked before it is stored

The system SHALL provide an operation that accepts drafted text for a started
stage, check it against the shape that stage requires, and store it only if it
passes.

#### Scenario: A well-formed draft

- **WHEN** a client submits a draft matching the shape the stage requires
- **THEN** the document is stored, awaiting verification, and its identifier
  is returned

#### Scenario: A draft of the wrong shape

- **WHEN** a client submits prose instead of a document of the stage's shape
- **THEN** nothing is stored, and the response lists what is wrong

#### Scenario: An empty draft

- **WHEN** a client submits empty or whitespace-only text
- **THEN** nothing is stored and the submission is refused

### Requirement: A rejected draft says what to fix

Where a draft fails its shape check, the response SHALL list every problem
found rather than only the first, so the client can correct the draft and
submit again without a further round trip per mistake.

#### Scenario: Several problems at once

- **WHEN** a draft has a requirement with no scenarios and a scenario with no
  expected outcome
- **THEN** both problems are listed in one response

#### Scenario: Correcting and resubmitting

- **WHEN** a client submits a corrected draft for the same stage after a
  failure
- **THEN** it is checked afresh and stored if it now passes

### Requirement: A document records where it came from

Every stored document SHALL record the stage that produced it, the source it
was written from, and the exact revision of that source, so its lineage back
to the brief is recoverable.

#### Scenario: Tracing a document

- **WHEN** a user asks where an FSD came from
- **THEN** the BRD it was written from is named, together with the revision of
  that BRD that was current at the time

### Requirement: The delivery phase label travels down the chain

Where a source document marks content with a delivery phase of the product
being specified, the instructions for the next stage SHALL require that phase
to be carried onto the content derived from it, and the shape check SHALL
confirm it was.

#### Scenario: A phase-marked requirement produces phase-marked stories

- **WHEN** an FSD marks a requirement as Phase 2, and a step procedure and
  story breakdown are written from it
- **THEN** the steps and stories derived from that requirement are also marked
  Phase 2

#### Scenario: Content with no phase

- **WHEN** a source marks no phase on some content
- **THEN** the derived content carries no phase, rather than a guessed one

#### Scenario: A phase that cannot be read

- **WHEN** a draft marks a phase in a form the shape check cannot read
- **THEN** the draft is refused naming the expected form, rather than the
  phase being silently dropped

### Requirement: A story breakdown proposes work without creating it

A story breakdown SHALL be a document describing proposed stories, each with
its tasks and subtasks. The system SHALL NOT create, modify or delete anything
in Jira.

#### Scenario: Writing a story breakdown

- **WHEN** a story breakdown is stored for a verified step procedure
- **THEN** it lists stories with their tasks and subtasks, and nothing in Jira
  has been created or altered

### Requirement: Writing a stage twice is deliberate

Where a document already exists for a stage, starting that stage again SHALL
report the existing document rather than inviting a replacement, unless the
caller asks to write a new one. Writing a new one SHALL keep the previous.

#### Scenario: Starting a stage that is already done

- **WHEN** a user starts the BRD stage for a brief that already has a BRD
- **THEN** the existing document is reported, and no new draft is invited

#### Scenario: Deliberately writing it again

- **WHEN** a user starts the stage again and asks for a new document
- **THEN** a draft is invited, and storing it leaves the previous document in
  place

### Requirement: Source content is treated as data

Content taken from a brief, an earlier document or a Jira issue SHALL be
presented to the client as data rather than as instructions, and SHALL NOT be
written into any log.

#### Scenario: A source containing instructions aimed at a model

- **WHEN** a source document contains text instructing the model to ignore its
  instructions
- **THEN** that text is presented as content inside delimiters, and whatever
  comes back is still checked for shape before it is stored

#### Scenario: A failure while a stage is in progress

- **WHEN** any part of starting a stage or storing a draft fails
- **THEN** no source content and no drafted text appears in any log entry

### Requirement: What produced a document is recorded as reported

The system SHALL record which client and model a draft claims to have come
from, and SHALL present it as a claim rather than a verified fact.

#### Scenario: A client that names itself

- **WHEN** a client submits a draft and names the model that wrote it
- **THEN** that name is stored with the document and shown as reported by the
  client

#### Scenario: A client that names nothing

- **WHEN** a client submits a draft without naming a model
- **THEN** the document is stored with no model recorded, rather than a
  guessed one
