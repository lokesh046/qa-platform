# Spec Delta

## Purpose

Gives every document the same life: a person reads it, verifies or rejects it,
corrects it where it is wrong, and each correction becomes a version nobody
can lose. It is the single review loop every stage of the chain shares.

## ADDED Requirements

### Requirement: A document has a verification state

Every document SHALL be in exactly one state: awaiting verification, verified,
or rejected. A newly stored document is awaiting verification.

#### Scenario: A document is stored

- **WHEN** a drafted document passes its shape check and is stored
- **THEN** it is awaiting verification, and no stage may be started from it

#### Scenario: Reading a document's state

- **WHEN** a user reads a document
- **THEN** its state, when that state last changed, and the note given with it
  are returned

### Requirement: A person verifies by editing a review file

Verification SHALL happen by the user editing a review file that the tool
writes and watches, outside any repository. Changing its status line to
verified or rejected and saving SHALL apply that state to the document.

#### Scenario: Verifying

- **WHEN** a user sets the review file's status to verified and saves it
- **THEN** the document becomes verified, recording when, and a stage may be
  started from it

#### Scenario: Rejecting with a reason

- **WHEN** a user sets the status to rejected and writes a reason in the file
- **THEN** the document becomes rejected, the reason is stored, and no stage
  may be started from it

#### Scenario: An unreadable status

- **WHEN** the status line is changed to something that is neither verified
  nor rejected
- **THEN** the document's state is unchanged and the file reports what the
  accepted values are

#### Scenario: The document's text is also edited

- **WHEN** a user edits the document's text in the review file as well as its
  status
- **THEN** the edited text is stored as a new revision before the state is
  applied, so a person verifies what they actually read

### Requirement: Verification is not available as a tool

The system SHALL NOT expose verification or rejection as an operation a
connected client can call. Only the review file applies those states.

#### Scenario: Looking for a verify tool

- **WHEN** a client lists the tools the server offers
- **THEN** none of them verifies, rejects, or otherwise approves a document

#### Scenario: An agent asked to approve

- **WHEN** a user asks an agent to verify a document for them
- **THEN** the agent has no operation that does so, and the document stays
  awaiting verification

### Requirement: Nothing verifies itself

The system SHALL NOT place a document into the verified state on its own,
however well-formed it was.

#### Scenario: A draft that passes every check

- **WHEN** a drafted document passes its shape check completely
- **THEN** it is awaiting verification, because verification is a person's act
  and not a property of the text

### Requirement: A correction creates a revision

When a person corrects a document, the corrected text SHALL be stored as a new
revision rather than replacing the previous text. Every revision records when
it was made and the note given with it.

#### Scenario: Correcting a document

- **WHEN** a user saves corrected text for a document
- **THEN** a new revision is stored, the previous revision remains readable,
  and the document's current text is the new revision

#### Scenario: Nothing is overwritten

- **WHEN** a document has been corrected several times
- **THEN** every earlier revision is still readable in order, with its time

### Requirement: A corrected document returns for verification

Correcting a document SHALL return it to awaiting verification, because the
text a person approved is no longer the text the document holds.

#### Scenario: Correcting a verified document

- **WHEN** a user corrects a document that was already verified
- **THEN** it is awaiting verification again, and no stage may be started from
  it until the new revision is verified

### Requirement: The current version is unambiguous

A document's current version SHALL be its most recent revision, and every
reader SHALL see the same one.

#### Scenario: Reading after a correction

- **WHEN** a user reads a document that was corrected after it was stored
- **THEN** the text returned is the most recent revision, and the revision is
  identified so a reader can tell which one they have

### Requirement: A document whose source changed is marked stale

Where a document was written from a source that has since gained a new
revision, the system SHALL mark the derived document stale and SHALL say so
when it is read. A stale document is not deleted and its verification is not
withdrawn.

#### Scenario: The source is corrected after deriving from it

- **WHEN** a BRD is corrected after an FSD was written from it
- **THEN** the FSD is marked stale, naming the source revision it was written
  from and the newer one that now exists

#### Scenario: A stale document is still readable

- **WHEN** a user reads a stale document
- **THEN** its text and state are returned together with the fact that it is
  stale, rather than the read being refused

#### Scenario: Writing it again clears staleness

- **WHEN** a document is written again from the current revision of its source
- **THEN** the new document is not stale

### Requirement: The history of a document is readable

The system SHALL return a document's full history: every revision and every
state change, in order, with the time of each.

#### Scenario: Reviewing what happened

- **WHEN** a user requests a document's history
- **THEN** every revision and state change is returned in order with its time
  and any note

### Requirement: Lifecycle actions are recorded locally

The system SHALL keep a local record of every verification, rejection and
correction, with the time and any note. The record is for the person using the
tool; it is not a shared audit trail.

#### Scenario: A verification

- **WHEN** a user verifies a document
- **THEN** a local record notes which document was verified and when,
  independently of the state stored on the document itself
