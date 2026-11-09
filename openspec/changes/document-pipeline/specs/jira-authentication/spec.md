# Spec Delta

## Purpose

Signs a person in to their own Jira account from a desktop tool, so that the
platform never holds a shared credential and Jira itself decides what each
person is allowed to see.

## ADDED Requirements

### Requirement: Signing in happens in a browser

The first operation that needs Jira SHALL start a browser consent flow rather
than fail, and SHALL complete without the user copying any value by hand.

#### Scenario: First use

- **WHEN** a user calls a tool that needs Jira and has never signed in
- **THEN** a browser opens at Atlassian's consent page, and once the user
  approves, the original operation continues and returns its result

#### Scenario: The user declines consent

- **WHEN** a user declines at the consent page
- **THEN** the operation fails with a message saying sign-in was declined, and
  no token is stored

#### Scenario: The browser cannot be opened

- **WHEN** no browser can be launched
- **THEN** the tool prints the URL for the user to open themselves, and
  completes when they finish

### Requirement: The sign-in is for one person's own account

Authentication SHALL identify the individual user, and every Jira read SHALL
be made as that user. The tool SHALL NOT hold a shared or service credential.

#### Scenario: Two people, two accounts

- **WHEN** two people each install the tool and sign in
- **THEN** each one's Jira reads are made as themselves, and neither can read
  an issue their own Jira account cannot read

#### Scenario: An issue the user may not see

- **WHEN** a user requests an issue their Jira account has no permission for
- **THEN** the request fails with what Jira returned, and the platform applies
  no permission rule of its own

### Requirement: The token is kept where the operating system keeps secrets

A refresh token SHALL be stored in the operating system keychain. It SHALL NOT
be written to a file in a repository, a configuration file, an environment
variable, a log line, an error message or a tool result.

#### Scenario: Where the token ends up

- **WHEN** a user completes sign-in
- **THEN** the refresh token is in the keychain, and no file in the working
  directory or the tool's own directory contains it

#### Scenario: A failure during a Jira call

- **WHEN** a Jira call fails for any reason
- **THEN** no part of any token appears in the message returned or in any log
  entry

### Requirement: Renewal is silent

The tool SHALL renew an expired access token without involving the user, for
as long as the refresh token remains valid.

#### Scenario: An expired access token

- **WHEN** a user calls a tool after their access token has expired but while
  the refresh token is valid
- **THEN** the token is renewed and the call succeeds, with no browser and no
  prompt

#### Scenario: A revoked or expired refresh token

- **WHEN** renewal fails because the refresh token is no longer valid
- **THEN** the user is told they need to sign in again, and the next operation
  starts the browser flow

### Requirement: Signing out removes the credential

The tool SHALL provide a way to sign out, which removes the stored token.

#### Scenario: Signing out

- **WHEN** a user signs out
- **THEN** the stored refresh token is removed from the keychain, and the next
  operation that needs Jira starts the browser flow

### Requirement: The Jira site is discovered, not configured

After sign-in, the tool SHALL determine which Jira sites the user has granted
access to, rather than requiring them to type a site URL.

#### Scenario: One accessible site

- **WHEN** a user completes sign-in and their account has one Jira site
- **THEN** that site is used, and the user is told which one

#### Scenario: Several accessible sites

- **WHEN** a user's account has more than one Jira site
- **THEN** the user is asked to choose once, and the choice is remembered

### Requirement: Authentication state is visible

The tool SHALL report whether a user is signed in, as whom, and against which
site, without exposing any token.

#### Scenario: Checking status

- **WHEN** a user asks for authentication status
- **THEN** whether they are signed in, the account's display name and email,
  and the selected site are returned, and no token or fragment of one is
  included
