# Spec Delta

## Purpose

Holds one person's briefs, documents, revisions and verification state on
their own machine, so the tool works with nothing running and nothing
deployed.

## ADDED Requirements

### Requirement: The store is local and single-user

The store SHALL live in a single file on the user's machine, outside any
working repository, and SHALL hold the data of exactly one person.

#### Scenario: Where the store lives

- **WHEN** the tool runs for the first time
- **THEN** it creates its store in the tool's own directory under the user's
  home, not in the repository the editor happens to have open

#### Scenario: Two repositories, one store

- **WHEN** a user runs the tool from two different repositories
- **THEN** both see the same briefs and documents, because the store belongs
  to the person rather than to a checkout

#### Scenario: Nothing is shared

- **WHEN** two people use the tool on different machines
- **THEN** neither can read the other's briefs, documents or revisions, and no
  network service holds a copy

### Requirement: The store is created and upgraded without the user acting

The tool SHALL create its store on first use and bring an existing store up to
the current shape when it starts, without asking the user to run anything.

#### Scenario: First run

- **WHEN** the tool runs and no store exists
- **THEN** one is created and the operation proceeds

#### Scenario: A store from an older version

- **WHEN** the tool starts against a store written by an earlier version
- **THEN** it is upgraded in place, existing briefs, documents and revisions
  are preserved, and the operation proceeds

#### Scenario: A store from a newer version

- **WHEN** the tool starts against a store written by a later version than
  itself
- **THEN** it refuses to proceed and says so, rather than writing data an
  older shape cannot represent

### Requirement: The store keeps the chain intact

The store SHALL preserve, for every document, which brief it belongs to, which
document and which revision it was derived from, and every revision in order.

#### Scenario: Tracing a document back

- **WHEN** a user asks where a story breakdown came from
- **THEN** the step procedure, FSD, BRD and brief it descends from are
  identifiable, each naming the revision that was used

#### Scenario: A revision is never lost

- **WHEN** a document has been corrected several times
- **THEN** every revision remains readable in order with its author and time

### Requirement: Nothing is deleted

The store SHALL provide no operation that deletes a brief, a document or a
revision.

#### Scenario: Attempting to delete

- **WHEN** any caller attempts to delete stored content
- **THEN** no such operation exists, and the stored history is unchanged

### Requirement: No credential is stored in it

The store SHALL hold no Jira token, no client secret and no password.
Credentials belong in the operating system keychain.

#### Scenario: Inspecting the store file

- **WHEN** the store file is read directly
- **THEN** it contains no token, no client secret and no password

### Requirement: A damaged store fails loudly

Where the store is unreadable or corrupt, the tool SHALL say so and SHALL NOT
silently start a new empty one.

#### Scenario: A corrupt store

- **WHEN** the store file exists but cannot be read
- **THEN** the tool reports which file is unreadable and stops, so a user does
  not conclude their documents were lost when they were only unreachable

### Requirement: The store is portable by copying

Where a user copies the store file to another machine running the same
version, the tool SHALL read it unchanged.

#### Scenario: Moving to a new machine

- **WHEN** a user copies their store file to another machine and runs the same
  version of the tool
- **THEN** their briefs, documents and revisions are all present, and they are
  asked to sign in to Jira again because no credential travelled with it
