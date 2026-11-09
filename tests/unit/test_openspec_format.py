"""Tests for ``app.llm.openspec_format``.

Pure functions over a model's reply, so this is the cheapest test file in the
project and one of the most load-bearing: it is the only thing standing between
a confidently wrong answer and a stored document.

The cases are the real failure modes, not invented ones. A fence around the
whole reply, a line of preamble, a requirement with no scenarios, a scenario
with a WHEN and no THEN, an apology instead of a document — every one of these
is something a model does.
"""

from __future__ import annotations

import pytest

from app.llm.openspec_format import (
    MAX_DOCUMENT_CHARS,
    MAX_REQUIREMENTS,
    MAX_SCENARIOS_PER_REQUIREMENT,
    normalise,
    parse,
    validate,
)

SCENARIO = """#### Scenario: Password expired yesterday
- **WHEN** a user signs in with an expired password
- **THEN** an error reading "password expired" is shown
"""

REQUIREMENT = f"""### Requirement: Expired password is refused
The system SHALL refuse a login with an expired password.

{SCENARIO}"""

GOOD = f"""## ADDED Requirements

{REQUIREMENT}"""


def document(body: str = GOOD) -> str:
    """A reply body, as a model would return it."""
    return body


# ---------------------------------------------------------------------------
# normalise: undoing what models add around a document
# ---------------------------------------------------------------------------


def test_a_clean_document_is_unchanged():
    assert normalise(GOOD) == GOOD.strip()


def test_a_fence_around_the_whole_reply_is_removed():
    fenced = f"```markdown\n{GOOD}\n```"
    assert normalise(fenced).startswith("## ADDED Requirements")
    assert "```" not in normalise(fenced)


def test_a_fence_without_a_language_is_removed():
    assert "```" not in normalise(f"```\n{GOOD}\n```")


def test_preamble_before_the_first_heading_is_dropped():
    assert normalise(f"Here are the requirements:\n\n{GOOD}") == GOOD.strip()


def test_both_a_fence_and_preamble_are_handled():
    reply = f"Sure! Here you go:\n\n```markdown\n{GOOD}\n```\n\nLet me know if you need changes."
    result = normalise(reply)
    assert result.startswith("## ADDED Requirements")
    assert "Let me know" not in result


def test_a_code_fence_inside_the_document_survives():
    # A requirement may legitimately quote a payload or a log line.
    body = f'## ADDED Requirements\n\n{REQUIREMENT}\n```json\n{{"a": 1}}\n```\n'
    assert '{"a": 1}' in normalise(body)


def test_prose_with_no_heading_is_left_alone_not_emptied():
    # So the validator rejects it with a useful message rather than being
    # handed an empty string and reporting "the model returned nothing".
    assert normalise("I cannot help with that.") == "I cannot help with that."


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_one_requirement_with_one_scenario_is_parsed():
    doc = parse(GOOD)
    assert len(doc.requirements) == 1
    requirement = doc.requirements[0]
    assert requirement.name == "Expired password is refused"
    assert requirement.statement.startswith("The system SHALL refuse")
    assert len(requirement.scenarios) == 1
    assert requirement.scenarios[0].name == "Password expired yesterday"


def test_when_and_then_text_is_captured_without_the_markers():
    scenario = parse(GOOD).requirements[0].scenarios[0]
    assert scenario.when == ("a user signs in with an expired password",)
    assert scenario.then == ('an error reading "password expired" is shown',)


def test_several_whens_and_thens_are_all_captured():
    body = """## ADDED Requirements

### Requirement: A
The system SHALL x.

#### Scenario: S
- **WHEN** the first condition
- **WHEN** the second condition
- **THEN** the first outcome
- **THEN** the second outcome
"""
    scenario = parse(body).requirements[0].scenarios[0]
    assert len(scenario.when) == 2
    assert len(scenario.then) == 2


def test_requirements_keep_document_order():
    body = "## ADDED Requirements\n\n" + "\n".join(
        f"### Requirement: R{n}\nThe system SHALL do {n}.\n\n{SCENARIO}" for n in (1, 2, 3)
    )
    assert [r.name for r in parse(body).requirements] == ["R1", "R2", "R3"]


def test_scenario_count_totals_across_requirements():
    body = "## ADDED Requirements\n\n" + "\n".join(
        f"### Requirement: R{n}\nSHALL.\n\n{SCENARIO}{SCENARIO}" for n in (1, 2)
    )
    assert parse(body).scenario_count == 4


def test_open_questions_are_collected():
    body = f"{GOOD}\n## Open questions\n\n- How long is the lockout?\n- Who can unlock?\n"
    doc = parse(body)
    assert doc.open_questions == ("How long is the lockout?", "Who can unlock?")


@pytest.mark.parametrize(
    "heading", ["## Requirements", "## ADDED Requirements", "## added requirements"]
)
def test_the_requirements_heading_is_matched_loosely(heading: str):
    assert len(parse(f"{heading}\n\n{REQUIREMENT}").requirements) == 1


@pytest.mark.parametrize("marker", ["- **WHEN**", "- WHEN", "* **WHEN**", "- **when**", "- WHEN:"])
def test_the_when_marker_is_matched_loosely(marker: str):
    body = (
        "## ADDED Requirements\n\n### Requirement: A\nSHALL.\n\n"
        f"#### Scenario: S\n{marker} a thing\n- **THEN** an outcome\n"
    )
    assert parse(body).requirements[0].scenarios[0].when == ("a thing",)


def test_an_unrecognised_h2_ends_the_section():
    # A model that invents "## Notes" must not have its notes absorbed into the
    # previous requirement's statement.
    body = f"{GOOD}\n## Notes\n\nThis was hard to write.\n"
    doc = parse(body)
    assert len(doc.requirements) == 1
    assert "hard to write" not in doc.requirements[0].statement


def test_content_before_any_recognised_heading_is_ignored():
    assert parse("### Requirement: Orphan\nSHALL.\n").requirements == ()


def test_the_stored_markdown_is_the_normalised_form():
    # What gets stored has no fence and no preamble, so a reader of the stored
    # document sees the document.
    doc = parse(f"Here:\n\n```markdown\n{GOOD}\n```")
    assert doc.markdown.startswith("## ADDED Requirements")


# ---------------------------------------------------------------------------
# validate: the happy path
# ---------------------------------------------------------------------------


def test_a_good_document_is_accepted():
    report = validate(GOOD)
    assert report.ok is True
    assert report.errors == ()
    assert report.document is not None


def test_a_realistic_reply_with_fence_and_preamble_is_accepted():
    report = validate(f"Here are the requirements:\n\n```markdown\n{GOOD}\n```")
    assert report.ok is True


def test_open_questions_alone_are_accepted():
    # The prompt asks for exactly this when a ticket says too little. An honest
    # "this does not say what should happen" beats invented requirements.
    report = validate("## Open questions\n\n- What should happen on expiry?\n")
    assert report.ok is True
    assert report.document is not None
    assert report.document.needs_more_information is True


def test_a_document_with_requirements_does_not_need_more_information():
    report = validate(f"{GOOD}\n## Open questions\n\n- A minor question?\n")
    assert report.ok is True
    assert report.document is not None
    assert report.document.needs_more_information is False


# ---------------------------------------------------------------------------
# validate: the refusals. Each is something a model actually returns.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["", "   ", "\n\n"], ids=["empty", "spaces", "newlines"])
def test_an_empty_reply_is_refused(reply: str):
    report = validate(reply)
    assert report.ok is False
    assert report.document is None
    assert "nothing" in report.errors[0]


@pytest.mark.parametrize(
    "reply",
    [
        "I cannot help with that request.",
        "I'd be happy to help! Could you tell me more about the issue?",
        "The ticket SCRUM-1 is about login. It needs tests.",
    ],
    ids=["refusal", "question-back", "prose-summary"],
)
def test_prose_instead_of_a_document_is_refused(reply: str):
    report = validate(reply)
    assert report.ok is False
    assert "No requirements and no open questions" in report.errors[0]


def test_a_requirement_with_no_scenarios_is_refused():
    # The one that matters most: it cannot become a test, which is the whole
    # reason the document exists.
    report = validate("## ADDED Requirements\n\n### Requirement: A\nThe system SHALL x.\n")
    assert report.ok is False
    assert any("no scenarios" in e for e in report.errors)


def test_a_scenario_with_no_then_is_refused():
    body = (
        "## ADDED Requirements\n\n### Requirement: A\nSHALL.\n\n"
        "#### Scenario: S\n- **WHEN** a thing\n"
    )
    report = validate(body)
    assert report.ok is False
    assert any("no THEN" in e for e in report.errors)


def test_a_scenario_with_no_when_is_refused():
    body = (
        "## ADDED Requirements\n\n### Requirement: A\nSHALL.\n\n"
        "#### Scenario: S\n- **THEN** an outcome\n"
    )
    report = validate(body)
    assert report.ok is False
    assert any("no WHEN" in e for e in report.errors)


def test_a_requirement_with_no_statement_is_refused():
    report = validate(f"## ADDED Requirements\n\n### Requirement: A\n\n{SCENARIO}")
    assert report.ok is False
    assert any("no statement" in e for e in report.errors)


def test_a_duplicated_requirement_name_is_refused():
    # Two requirements with one name make the document ambiguous to anything
    # that later refers to a requirement by name.
    body = f"## ADDED Requirements\n\n{REQUIREMENT}\n{REQUIREMENT}"
    report = validate(body)
    assert report.ok is False
    assert any("more than once" in e for e in report.errors)


def test_duplicate_detection_ignores_case():
    body = (
        "## ADDED Requirements\n\n"
        f"### Requirement: Expired password\nSHALL.\n\n{SCENARIO}\n"
        f"### Requirement: EXPIRED PASSWORD\nSHALL.\n\n{SCENARIO}"
    )
    assert validate(body).ok is False


def test_every_problem_is_reported_not_just_the_first():
    # One round trip should tell a developer everything wrong with a prompt's
    # output, otherwise tuning it takes one generation per mistake.
    body = (
        "## ADDED Requirements\n\n"
        "### Requirement: A\n\n#### Scenario: S1\n- **WHEN** w\n\n"
        "### Requirement: B\nSHALL.\n"
    )
    report = validate(body)
    assert report.ok is False
    assert len(report.errors) >= 3


def test_an_oversized_reply_is_refused_without_parsing():
    report = validate("## ADDED Requirements\n" + "x" * MAX_DOCUMENT_CHARS)
    assert report.ok is False
    assert "the limit is" in report.errors[0]


def test_too_many_requirements_is_refused():
    body = "## ADDED Requirements\n\n" + "\n".join(
        f"### Requirement: R{n}\nSHALL.\n\n{SCENARIO}" for n in range(MAX_REQUIREMENTS + 1)
    )
    report = validate(body)
    assert report.ok is False
    assert any("requirements were returned" in e for e in report.errors)


def test_too_many_scenarios_on_one_requirement_is_refused():
    body = "## ADDED Requirements\n\n### Requirement: A\nSHALL.\n\n" + SCENARIO * (
        MAX_SCENARIOS_PER_REQUIREMENT + 1
    )
    report = validate(body)
    assert report.ok is False
    assert any("scenarios; the limit is" in e for e in report.errors)


def test_a_failed_report_carries_no_document():
    # So a caller cannot store the document from a report it did not check.
    report = validate("## ADDED Requirements\n\n### Requirement: A\nSHALL.\n")
    assert report.ok is False
    assert report.document is None
