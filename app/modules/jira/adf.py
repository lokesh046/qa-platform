"""Atlassian Document Format to plain text.

Jira Cloud's REST API v3 returns rich text fields as ADF: a JSON tree of typed
nodes. The platform stores and shows plain text, so this module flattens it.

Pure functions, no I/O, no SQLAlchemy. Written that way because this is the
component most likely to meet a shape nobody anticipated, and it needs to be
testable with a literal dict.

**The input is untrusted.** It comes from Jira, but the content inside it was
written by whoever filed the ticket, and a ticket can be filed by a customer.
Two limits follow, both enforced below: recursion depth and output size. A
deeply nested document would otherwise exhaust the stack, and a pathologically
large one would exhaust memory on the way into a ``TEXT`` column.
"""

from __future__ import annotations

import re
from typing import Any

#: How deep the walker will descend before giving up on a branch. Real
#: documents nest a handful of levels; a table inside a panel inside a list
#: item is about the worst legitimate case.
MAX_DEPTH = 24

#: Hard cap on the produced text. Jira's own description limit is 32,767
#: characters, so this is generous for a legitimate document while still
#: bounding what a malformed one can cost. Truncation is marked, never silent.
MAX_OUTPUT_CHARS = 60_000

_TRUNCATION_MARKER = "\n[... truncated]"

#: Matches a heading that introduces acceptance criteria. Deliberately loose
#: about the surrounding punctuation and wording, because teams write
#: "Acceptance Criteria", "Acceptance criteria:", and "AC" interchangeably.
ACCEPTANCE_CRITERIA_HEADING = re.compile(
    r"^\s*(acceptance\s+criteria|acceptance|\bac\b)\s*:?\s*$",
    re.IGNORECASE,
)

#: Nodes whose text runs together inline, with no block separation.
_INLINE_TYPES = frozenset({"text", "mention", "emoji", "inlineCard", "status", "date", "hardBreak"})

#: Nodes that start a new block of text.
_BLOCK_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "blockquote",
        "panel",
        "codeBlock",
        "rule",
        "bulletList",
        "orderedList",
        "listItem",
        "taskList",
        "taskItem",
        "table",
        "tableRow",
        "mediaSingle",
        "mediaGroup",
        "expand",
        "nestedExpand",
    }
)


def _is_doc(value: Any) -> bool:
    """True if ``value`` looks like an ADF node or document."""
    return isinstance(value, dict) and isinstance(value.get("type"), str)


def to_text(value: Any) -> str | None:
    """Flatten an ADF document, or a plain string, to readable text.

    Accepts what Jira actually returns for a rich text field, which is one of
    three things: an ADF document, a plain string (older field types and some
    custom fields), or ``None``.

    Returns ``None`` for an absent or empty value rather than an empty string,
    so a caller can tell "the field was blank" from "the field held
    whitespace".
    """
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if not _is_doc(value):
        return None

    blocks = _render(value, depth=0)
    text = _join_blocks(blocks)
    return text or None


def extract_acceptance_criteria(description: Any) -> str | None:
    """Pull an "Acceptance Criteria" section out of an ADF description.

    Used as the fallback when no acceptance-criteria custom field is configured
    or the field is empty on an issue. Finds the first heading matching
    :data:`ACCEPTANCE_CRITERIA_HEADING` and takes everything after it until the
    next heading at the same level or higher.

    Working on the node tree rather than the flattened text matters: a heading
    is unambiguous in ADF, while in plain text "Acceptance Criteria" could be
    a line of a bulleted list or a sentence in a paragraph.

    Returns ``None`` when there is no such heading, which is the common case.
    """
    if not _is_doc(description):
        return None

    content = description.get("content")
    if not isinstance(content, list):
        return None

    collected: list[dict[str, Any]] = []
    heading_level: int | None = None

    for node in content:
        if not _is_doc(node):
            continue
        if node.get("type") == "heading":
            level = _heading_level(node)
            if heading_level is None:
                if ACCEPTANCE_CRITERIA_HEADING.match(_inline_text(node, depth=0)):
                    heading_level = level
                continue
            # A heading at the same level or shallower ends the section. A
            # deeper one is a sub-heading and stays part of it.
            if level <= heading_level:
                break
            collected.append(node)
            continue
        if heading_level is not None:
            collected.append(node)

    if heading_level is None or not collected:
        return None

    text = _join_blocks([block for node in collected for block in _render(node, depth=1)])
    return text or None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _heading_level(node: dict[str, Any]) -> int:
    """Read a heading's level, defaulting to 1 for a malformed node."""
    attrs = node.get("attrs")
    if isinstance(attrs, dict):
        level = attrs.get("level")
        if isinstance(level, int) and 1 <= level <= 6:
            return level
    return 1


def _join_blocks(blocks: list[str]) -> str:
    """Join rendered blocks with blank lines, dropping empties and truncating."""
    joined = "\n\n".join(block for block in (b.strip() for b in blocks) if block)
    if len(joined) > MAX_OUTPUT_CHARS:
        return joined[:MAX_OUTPUT_CHARS].rstrip() + _TRUNCATION_MARKER
    return joined


def _inline_text(node: dict[str, Any], *, depth: int) -> str:
    """Concatenate the inline text inside one node."""
    if depth > MAX_DEPTH:
        return ""
    node_type = node.get("type")

    if node_type == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        return _attr_str(node, "text") or "@unknown"
    if node_type == "emoji":
        return _attr_str(node, "text") or _attr_str(node, "shortName") or ""
    if node_type in {"inlineCard", "blockCard"}:
        return _attr_str(node, "url") or ""
    if node_type == "status":
        label = _attr_str(node, "text")
        return f"[{label}]" if label else ""
    if node_type == "date":
        return _attr_str(node, "timestamp") or ""

    children = node.get("content")
    if not isinstance(children, list):
        return ""
    return "".join(_inline_text(child, depth=depth + 1) for child in children if _is_doc(child))


def _attr_str(node: dict[str, Any], key: str) -> str:
    """Read one string attribute, tolerating a malformed ``attrs``."""
    attrs = node.get("attrs")
    if isinstance(attrs, dict):
        value = attrs.get(key)
        if isinstance(value, str):
            return value
    return ""


def _render(node: dict[str, Any], *, depth: int) -> list[str]:
    """Render one node into zero or more text blocks.

    Returns a list rather than a string so that list items and table rows can
    each become their own block without the caller re-splitting text.
    """
    if depth > MAX_DEPTH:
        return []

    node_type = node.get("type")
    children = node.get("content") if isinstance(node.get("content"), list) else []
    child_nodes = [child for child in children if _is_doc(child)]

    if node_type == "doc":
        return [block for child in child_nodes for block in _render(child, depth=depth + 1)]

    if node_type in {"paragraph", "heading"}:
        return [_inline_text(node, depth=depth + 1)]

    if node_type == "codeBlock":
        # Kept verbatim. A code block in a reproduction step is often the most
        # useful part of a ticket, and reflowing it destroys the meaning.
        return [_inline_text(node, depth=depth + 1)]

    if node_type == "rule":
        return ["---"]

    if node_type in {"bulletList", "taskList"}:
        return _render_list(child_nodes, depth=depth, ordered=False)

    if node_type == "orderedList":
        return _render_list(child_nodes, depth=depth, ordered=True)

    if node_type in {"listItem", "taskItem"}:
        return [block for child in child_nodes for block in _render(child, depth=depth + 1)]

    # These wrappers add no text of their own; their children carry it.
    if node_type in {"blockquote", "panel", "expand", "nestedExpand", "table"}:
        return [block for child in child_nodes for block in _render(child, depth=depth + 1)]

    if node_type == "tableRow":
        cells = [
            _join_blocks([b for child in [cell] for b in _render(child, depth=depth + 1)])
            for cell in child_nodes
        ]
        row = " | ".join(cell.replace("\n", " ").strip() for cell in cells)
        return [row] if row.strip(" |") else []

    if node_type in {"tableCell", "tableHeader"}:
        return [block for child in child_nodes for block in _render(child, depth=depth + 1)]

    if node_type in {"mediaSingle", "mediaGroup", "media"}:
        # Attachments are not fetched or stored; see ADR-009. A placeholder is
        # kept so a reader can tell the ticket had an image.
        return ["[attachment]"]

    if node_type in _INLINE_TYPES:
        return [_inline_text(node, depth=depth + 1)]

    # Unknown node type. Descend rather than drop: a future ADF node will
    # almost certainly still hold its text in `content`.
    if node_type not in _BLOCK_TYPES and child_nodes:
        return [block for child in child_nodes for block in _render(child, depth=depth + 1)]
    return []


def _render_list(items: list[dict[str, Any]], *, depth: int, ordered: bool) -> list[str]:
    """Render list items as one block, each line prefixed with a marker."""
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        blocks = [block for block in _render(item, depth=depth + 1) if block.strip()]
        if not blocks:
            continue
        marker = f"{index}." if ordered else "-"
        body = " ".join(block.strip().replace("\n", " ") for block in blocks)
        lines.append(f"{marker} {body}")
    return ["\n".join(lines)] if lines else []
