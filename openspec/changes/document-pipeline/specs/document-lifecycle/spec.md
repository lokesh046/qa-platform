# Spec Delta

## Purpose

Gives every generated document the same life: a person reads it, verifies or
rejects it, corrects it where it is wrong, and each correction becomes a
version nobody can lose. It is the single review loop every stage of the chain
shares.

## ADDED Requirements

### Requirement: A document has a verification state

Every document SHALL be in exactly one state: awaiting verification, verified,
or rejected. A newly generated document is awaiting verification.

#### Scenario: A document is generated

- **WHEN** a document is generated
- **THEN** it is awaiting verification, and no stage may be generated from it

#### Scenario: Reading a document's state

- **WHEN** a user with access reads a document
- **THEN** its state, who last changed that state, and when, are returned

### Requirement: A person verifies or rejects a document

A user holding a grant for the document's project SHALL be able to verify or
reject it, and SHALL be able to record a note saying why. The system itself
SHALL NOT verify a document.

#### Scenario: Verifying

- **WHEN** a user with access verifies a document awaiting verification
- **THEN** it becomes verified, recording who verified it and when, and it may
  be used as the source for the next stage

#### Scenario: Rejecting with a reason

- **WHEN** a user with access rejects a document and gives a reason
- **THEN** it becomes rejected, the reason is stored, and it may not be used
  as the source for any stage

#### Scenario: A user without access

- **WHEN** a user with no grant for the document's project attempts to verify
  it
- **THEN** the attempt is refused and the state is unchanged

#### Scenario: Nothing verifies itself

- **WHEN** a document is generated, however successfully it validated
- **THEN** it is not verified, because verification is a person's act

### Requirement: An edit creates a revision

When a person corrects a document, the system SHALL store the corrected text
as a new revision rather than replacing the previous text. Every revision
records who made it, when, and the note they gave.

#### Scenario: Correcting a document

- **WHEN** a user with access submits corrected text for a document
- **THEN** a new revision is stored, the previous revision remains readable,
  and the document's current text is the new revision

#### Scenario: Nothing is overwritten

- **WHEN** a document has been corrected several times
- **THEN** every earlier revision is still readable in order, with its author
  and time

#### Scenario: Correcting requires access

- **WHEN** a user with no grant for the document's project submits corrected
  text
- **THEN** the attempt is refused and no revision is created

### Requirement: An edited document returns for verification

Correcting a document SHALL return it to awaiting verification, because the
text a person approved is no longer the text the document holds.

#### Scenario: Editing a verified document

- **WHEN** a user corrects a document that was already verified
- **THEN** the document is awaiting verification again, and the next stage may
  not be generated from it until somebody verifies the new revision

### Requirement: The current version is unambiguous

The system SHALL define a document's current version as its most recent
revision, and every reader SHALL see the same one.

#### Scenario: Reading after an edit

- **WHEN** a user reads a document that was corrected after it was generated
- **THEN** the text returned is the most recent revision, and the revision is
  identified so a reader can tell which one they have

### Requirement: A document whose source changed is marked stale

Where a document was generated from a source that has since gained a new
revision, the system SHALL mark the derived document stale and SHALL say so
when it is read. A stale document is not deleted and its verification is not
withdrawn.

#### Scenario: The source is corrected after deriving from it

- **WHEN** a BRD is corrected after an FSD was generated from it
- **THEN** the FSD is marked stale, naming the source revision it was built
  from and the newer one that now exists

#### Scenario: A stale document is still readable

- **WHEN** a user reads a stale document
- **THEN** its text and state are returned together with the fact that it is
  stale, rather than the read being refused

#### Scenario: Regenerating clears staleness

- **WHEN** a document is regenerated from the current revision of its source
- **THEN** the new document is not stale

### Requirement: Only verified documents feed the next stage

The system SHALL permit generation from a document only while that document is
verified and not stale.

#### Scenario: Deriving from a rejected document

- **WHEN** a user attempts to generate a step procedure from a rejected FSD
- **THEN** the request is refused, stating that the source was rejected

#### Scenario: Deriving from a stale document

- **WHEN** a user attempts to generate from a verified but stale document
- **THEN** the request is refused, stating that the source changed and must be
  verified again

### Requirement: The history of a document is readable

A user with access SHALL be able to read a document's full history: every
revision, every state change, who did each and when, in order.

#### Scenario: Reviewing what happened

- **WHEN** a user with access requests a document's history
- **THEN** every revision and state change is returned in order with its actor
  and time

#### Scenario: History requires access

- **WHEN** a user with no grant for the document's project requests its
  history
- **THEN** the request is refused with the same response as a document that
  does not exist

### Requirement: Nothing in a chain is deleted

The system SHALL NOT provide a way to delete a brief, a document or a
revision. A document that is wrong is rejected; text that is wrong is
superseded by a new revision.

#### Scenario: Attempting to delete

- **WHEN** any caller attempts to delete a document or a revision
- **THEN** no such operation exists, and the stored history is unchanged

### Requirement: Every lifecycle action is recorded

The system SHALL record an audit entry for every verification, rejection,
correction and history read, whether it succeeded or was refused, naming the
actor, the document and the reason for a refusal.

#### Scenario: A refused verification

- **WHEN** a user without access attempts to verify a document
- **THEN** an audit entry records the actor, the action, the refusal and its
  reason

#### Scenario: A verification that succeeded

- **WHEN** a user verifies a document
- **THEN** an audit entry records who verified which document and when,
  independently of the state stored on the document itself
