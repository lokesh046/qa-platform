"""Assemble the prompt that generates a BRD from a cached Jira issue.

Pure functions over data: no network, no database, no settings object. This
module builds a prompt and names its version; **who executes it is not its
concern**. That separation is why it survived the reversal in ADR-021, where
the plan to run generation on the caller's own model through MCP sampling was
dropped once sampling turned out to be deprecated and supported by two clients
out of twenty-four. The provider is settled in ADR-022.

Prompt text lives in ``prompts/`` as files rather than string literals, so a
change to it appears in a diff and can be reviewed. The version is recorded on
every document this prompt produces, which is what makes "why does this BRD
look like that" answerable later.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.modules.jira.schemas import IssueRead

PROMPT_VERSION = "brd_v1"
"""Identifies the prompt pair below. Bump it when either file changes."""

_PROMPT_DIR = Path(__file__).parent / "prompts"

DEFAULT_MAX_TOKENS = 4096
"""Enough for eight requirements with scenarios, and a ceiling on a runaway reply."""

MAX_FIELD_CHARS = 8000
"""Per-field bound on quoted issue text.

A Jira description can hold a pasted log file. Without a bound, one ticket
could fill the model's context and leave no room for the instructions, which
fails in the confusing way: a reply that ignores the format because the format
scrolled out of attention.
"""

_TRUNCATION_NOTE = "\n[... truncated for length]"

_ABSENT = "(the issue does not provide this)"


@dataclass(frozen=True, slots=True)
class BrdPrompt:
    """A prompt ready to be handed to a client's model.

    Attributes:
        version: The value of ``PROMPT_VERSION`` that produced it, stored
            alongside the generated document.
        system: Instructions describing the task and the output format.
        user: The issue itself, quoted inside delimiters.
        max_tokens: The ceiling to pass to the sampling request.
    """

    version: str
    system: str
    user: str
    max_tokens: int


@lru_cache(maxsize=4)
def _read_prompt_file(name: str) -> str:
    """Read a prompt file once and keep it in memory.

    Cached because these files never change while the process runs, and the
    alternative is a disk read on every request.
    """
    path = _PROMPT_DIR / name
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt file {name} is empty")
    return text


def _bounded(value: str | None) -> str:
    """Return issue text trimmed to ``MAX_FIELD_CHARS``, or a stand-in if absent.

    An explicit "the issue does not provide this" beats an empty space: it tells
    the model the field was genuinely missing rather than that the prompt was
    built wrongly, and it keeps the delimiters balanced.
    """
    if value is None or not value.strip():
        return _ABSENT
    text = value.strip()
    if len(text) <= MAX_FIELD_CHARS:
        return text
    return text[:MAX_FIELD_CHARS] + _TRUNCATION_NOTE


def build_brd_prompt(issue: IssueRead, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> BrdPrompt:
    """Build the BRD prompt for one issue.

    Deterministic: the same issue always produces the same prompt, which is
    what lets a test assert on it and what makes ``prompt_version`` meaningful.

    The issue's own text is quoted inside ``<<<ISSUE_DESCRIPTION`` delimiters
    and labelled as data. A ticket can be filed by anyone, so its text is
    untrusted input being placed into a prompt — the delimiters and the
    system prompt's instruction to ignore embedded commands are the mitigation.
    They are a mitigation and not a guarantee; nothing a model is *told* is a
    security boundary, which is why generated output is validated before it is
    stored rather than trusted.

    Args:
        issue: The cached issue, already flattened from ADF to text.
        max_tokens: Ceiling for the reply. Lower it for a cheap first pass.

    Returns:
        The assembled prompt and the version that produced it.

    Raises:
        ValueError: If ``max_tokens`` is not positive, or a prompt file is
            missing or empty.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    # str.replace, not str.format. The substituted values are issue text, and a
    # description containing a brace -- a JSON snippet, a code sample, a log
    # line -- makes format() raise KeyError or ValueError. Replacement treats
    # the issue's own braces as the literal characters they are.
    user = _read_prompt_file(f"{PROMPT_VERSION}_user.md")
    for placeholder, value in (
        ("{issue_key}", issue.issue_key),
        ("{project_key}", issue.project_key),
        ("{issue_type}", issue.issue_type or _ABSENT),
        ("{status}", issue.status or _ABSENT),
        ("{summary}", issue.summary.strip() or _ABSENT),
        ("{description}", _bounded(issue.description)),
        ("{acceptance_criteria}", _bounded(issue.acceptance_criteria)),
    ):
        user = user.replace(placeholder, value)
    return BrdPrompt(
        version=PROMPT_VERSION,
        system=_read_prompt_file(f"{PROMPT_VERSION}_system.md"),
        user=user,
        max_tokens=max_tokens,
    )
