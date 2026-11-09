"""Parsing and validating the markdown a model returns for a BRD.

A model's reply is untrusted input (ADR-023). It is usually right, and when it
is wrong it is wrong *confidently*: prose before the document, the whole thing
wrapped in a code fence, a requirement with no scenarios, a scenario with a
**WHEN** and no **THEN**. None of those are exceptional, so none of them are
treated as exceptions. They are parsed, found wanting, and reported.

The format is OpenSpec's, because named WHEN/THEN scenarios in a fixed shape
are good input for test generation in Phase 3 (ADR-019):

    ## ADDED Requirements

    ### Requirement: Expired password is refused
    The system SHALL refuse a login with an expired password.

    #### Scenario: Password expired yesterday
    - **WHEN** a user signs in with a password that expired
    - **THEN** an error reading "password expired" is shown

A document with no requirements but a populated ``## Open questions`` section
is **valid**. The prompt asks for exactly that when a ticket says too little,
and an honest "this ticket does not say what should happen" is more useful than
invented requirements. The caller decides what to do with it; this module only
reports what it found.

Pure functions, no I/O, which is why this is cheap to test exhaustively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

MAX_DOCUMENT_CHARS: Final = 60_000
"""Bound before parsing. ``max_tokens`` bounds a well-behaved provider; this
bounds the other kind."""

MAX_REQUIREMENTS: Final = 40
MAX_SCENARIOS_PER_REQUIREMENT: Final = 20
"""Generous next to the prompt's "one to eight" and "at most six". These are
not style rules, they are the point past which something has gone wrong."""

_REQUIREMENTS_HEADING = re.compile(r"^##\s+(?:ADDED\s+)?Requirements\s*$", re.IGNORECASE)
_OPEN_QUESTIONS_HEADING = re.compile(r"^##\s+Open\s+questions\s*:?\s*$", re.IGNORECASE)
_OTHER_H2 = re.compile(r"^##\s+\S")
_REQUIREMENT = re.compile(r"^###\s+Requirement:\s*(?P<name>.+?)\s*$", re.IGNORECASE)
_SCENARIO = re.compile(r"^####\s+Scenario:\s*(?P<name>.+?)\s*$", re.IGNORECASE)
_WHEN = re.compile(r"^\s*[-*]\s*\*{0,2}WHEN\*{0,2}\s*:?\s*(?P<text>.+?)\s*$", re.IGNORECASE)
_THEN = re.compile(r"^\s*[-*]\s*\*{0,2}THEN\*{0,2}\s*:?\s*(?P<text>.+?)\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>.+?)\s*$")
_FENCE = re.compile(r"^\s*```+[a-zA-Z0-9_-]*\s*$")


@dataclass(frozen=True, slots=True)
class Scenario:
    """One named case under a requirement."""

    name: str
    when: tuple[str, ...]
    then: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Requirement:
    """One requirement and its scenarios."""

    name: str
    statement: str
    scenarios: tuple[Scenario, ...]


@dataclass(frozen=True, slots=True)
class BrdDocument:
    """A parsed BRD.

    Attributes:
        requirements: In document order.
        open_questions: Bullet points under ``## Open questions``.
        markdown: The normalised markdown, which is what gets stored — not the
            model's raw reply, so a stored document has no code fence or
            preamble around it.
    """

    requirements: tuple[Requirement, ...]
    open_questions: tuple[str, ...]
    markdown: str

    @property
    def scenario_count(self) -> int:
        """Total scenarios across every requirement."""
        return sum(len(r.scenarios) for r in self.requirements)

    @property
    def needs_more_information(self) -> bool:
        """True when the model could only ask questions.

        Valid, and a useful answer. The caller records it differently from a
        document with requirements, because nothing can be tested from it.
        """
        return not self.requirements and bool(self.open_questions)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The outcome of validating one reply.

    Attributes:
        ok: True when the document may be stored.
        errors: Every problem found, not just the first, so one round trip
            tells a developer everything that is wrong with a prompt's output.
        document: The parsed document when ``ok``, otherwise ``None``.
    """

    ok: bool
    errors: tuple[str, ...]
    document: BrdDocument | None


def normalise(markdown: str) -> str:
    """Strip what models add around a document, without touching its content.

    Two habits, both common and both harmless to undo:

    - wrapping the whole reply in a ``` fence, which would otherwise be stored
      and rendered as one code block;
    - a line of preamble such as "Here are the requirements:" before the first
      heading.

    Everything from the first ``##`` heading onwards is kept verbatim. Text
    before it is dropped *only* if a heading exists, so a reply that is pure
    prose is left intact for the validator to reject rather than silently
    emptied.
    """
    lines = markdown.strip().splitlines()

    # A fence wrapping the document, which may itself sit between a line of
    # preamble and a line of closing chatter. Taking the outermost pair and
    # keeping it only if a heading lies inside handles all four combinations,
    # and leaves a fence that is genuinely *part* of the document alone --
    # a requirement quoting a JSON payload has no "##" between its fences.
    fences = [i for i, line in enumerate(lines) if _FENCE.match(line)]
    if len(fences) >= 2:
        inner = lines[fences[0] + 1 : fences[-1]]
        if any(line.startswith("##") for line in inner):
            lines = inner

    first_heading = next((i for i, line in enumerate(lines) if line.startswith("##")), None)
    if first_heading is not None:
        lines = lines[first_heading:]
    return "\n".join(lines).strip()


def parse(markdown: str) -> BrdDocument:
    """Parse normalised markdown into requirements, scenarios and questions.

    Lenient by design: it extracts what is there and records nothing about what
    is missing. ``validate`` decides whether what was found is enough. Keeping
    the two apart means the error messages live in one place and the parser
    does not have to guess which omissions matter.
    """
    text = normalise(markdown)
    requirements: list[Requirement] = []
    open_questions: list[str] = []

    section: str | None = None
    name: str | None = None
    statement: list[str] = []
    scenarios: list[Scenario] = []
    scenario_name: str | None = None
    when: list[str] = []
    then: list[str] = []

    def close_scenario() -> None:
        if scenario_name is not None:
            scenarios.append(Scenario(name=scenario_name, when=tuple(when), then=tuple(then)))

    def close_requirement() -> None:
        close_scenario()
        if name is not None:
            requirements.append(
                Requirement(
                    name=name,
                    statement=" ".join(s.strip() for s in statement if s.strip()).strip(),
                    scenarios=tuple(scenarios),
                )
            )

    for line in text.splitlines():
        if _REQUIREMENTS_HEADING.match(line):
            close_requirement()
            section, name, statement, scenarios = "requirements", None, [], []
            scenario_name, when, then = None, [], []
            continue
        if _OPEN_QUESTIONS_HEADING.match(line):
            close_requirement()
            section, name, statement, scenarios = "questions", None, [], []
            scenario_name, when, then = None, [], []
            continue
        if _OTHER_H2.match(line):
            # An unrecognised H2 ends the current section rather than being
            # absorbed into it. A model that invents "## Notes" should not have
            # its notes read as a requirement's statement.
            close_requirement()
            section, name, statement, scenarios = None, None, [], []
            scenario_name, when, then = None, [], []
            continue

        if section == "questions":
            bullet = _BULLET.match(line)
            if bullet:
                open_questions.append(bullet.group("text"))
            continue

        if section != "requirements":
            continue

        requirement = _REQUIREMENT.match(line)
        if requirement:
            close_requirement()
            name, statement, scenarios = requirement.group("name"), [], []
            scenario_name, when, then = None, [], []
            continue

        scenario = _SCENARIO.match(line)
        if scenario:
            close_scenario()
            scenario_name, when, then = scenario.group("name"), [], []
            continue

        if scenario_name is not None:
            # WHEN before THEN: "**THEN** ..." must not match the WHEN pattern,
            # and it cannot, because each pattern anchors on its own keyword.
            when_match = _WHEN.match(line)
            if when_match:
                when.append(when_match.group("text"))
                continue
            then_match = _THEN.match(line)
            if then_match:
                then.append(then_match.group("text"))
                continue
            continue

        if name is not None:
            statement.append(line)

    close_requirement()
    return BrdDocument(
        requirements=tuple(requirements),
        open_questions=tuple(open_questions),
        markdown=text,
    )


def validate(markdown: str) -> ValidationReport:
    """Decide whether a model's reply may be stored, and say why not.

    Returns a report rather than raising, because "the model wrote something
    unusable" is an expected outcome that the caller records and reports to a
    person — not an exception that unwinds a request.

    Args:
        markdown: The model's raw reply. Normalised internally.

    Returns:
        A report. When ``ok`` is false, ``errors`` lists every problem found.
    """
    errors: list[str] = []

    if not markdown or not markdown.strip():
        return ValidationReport(ok=False, errors=("The model returned nothing.",), document=None)

    if len(markdown) > MAX_DOCUMENT_CHARS:
        return ValidationReport(
            ok=False,
            errors=(
                f"The reply is {len(markdown)} characters; the limit is {MAX_DOCUMENT_CHARS}.",
            ),
            document=None,
        )

    document = parse(markdown)

    if not document.requirements and not document.open_questions:
        return ValidationReport(
            ok=False,
            errors=(
                "No requirements and no open questions were found. Expected a "
                "'## ADDED Requirements' section with '### Requirement:' entries, "
                "or an '## Open questions' section.",
            ),
            document=None,
        )

    if len(document.requirements) > MAX_REQUIREMENTS:
        errors.append(
            f"{len(document.requirements)} requirements were returned; "
            f"the limit is {MAX_REQUIREMENTS}."
        )

    seen: set[str] = set()
    for requirement in document.requirements:
        label = requirement.name
        if not requirement.statement:
            errors.append(f"Requirement '{label}' has no statement.")
        if not requirement.scenarios:
            # The one that matters most: a requirement without scenarios cannot
            # become a test, which is the entire reason this document exists.
            errors.append(f"Requirement '{label}' has no scenarios.")
        if len(requirement.scenarios) > MAX_SCENARIOS_PER_REQUIREMENT:
            errors.append(
                f"Requirement '{label}' has {len(requirement.scenarios)} scenarios; "
                f"the limit is {MAX_SCENARIOS_PER_REQUIREMENT}."
            )
        key = label.strip().lower()
        if key in seen:
            errors.append(f"Requirement '{label}' appears more than once.")
        seen.add(key)

        for scenario in requirement.scenarios:
            if not scenario.when:
                errors.append(f"Scenario '{scenario.name}' has no WHEN.")
            if not scenario.then:
                # A scenario with no THEN states a trigger and no expectation.
                # It reads like a test and cannot be one.
                errors.append(f"Scenario '{scenario.name}' has no THEN.")

    if errors:
        return ValidationReport(ok=False, errors=tuple(errors), document=None)
    return ValidationReport(ok=True, errors=(), document=document)
