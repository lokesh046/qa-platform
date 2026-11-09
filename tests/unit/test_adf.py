"""Tests for ``app.modules.jira.adf``.

Pure functions, so these are the cheapest tests in the project and the ones
most worth having: this module parses untrusted input (a ticket can be filed by
a customer) and every other test of the jira service depends on it behaving.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.modules.jira import adf


def paragraph(text: str) -> dict[str, Any]:
    """One ADF paragraph holding a single text node."""
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def heading(text: str, level: int = 2) -> dict[str, Any]:
    """One ADF heading at the given level."""
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def bullet_list(*items: str) -> dict[str, Any]:
    """A bullet list, one text paragraph per item."""
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [paragraph(item)]} for item in items],
    }


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    """An ADF document wrapping the given block nodes."""
    return {"type": "doc", "version": 1, "content": list(content)}


# ---------------------------------------------------------------------------
# to_text: the three shapes Jira actually returns
# ---------------------------------------------------------------------------


def test_none_stays_none():
    assert adf.to_text(None) is None


def test_plain_string_passes_through_stripped():
    # Older field types and some custom fields come back as a bare string.
    assert adf.to_text("  hello  ") == "hello"


def test_whitespace_only_string_is_none_not_empty():
    # The distinction matters: callers branch on None to mean "no value".
    assert adf.to_text("   \n ") is None


def test_empty_document_is_none():
    assert adf.to_text(doc()) is None


@pytest.mark.parametrize("value", [42, [], {"no_type": 1}, {"type": 5}, True])
def test_non_adf_values_are_none(value: object):
    # A field whose shape nobody anticipated must not raise.
    assert adf.to_text(value) is None


def test_paragraphs_are_separated_by_blank_lines():
    assert adf.to_text(doc(paragraph("One."), paragraph("Two."))) == "One.\n\nTwo."


def test_inline_marks_are_ignored_but_text_is_kept():
    # Bold, italic and links are marks on a text node. The text survives.
    node = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Click ", "marks": [{"type": "strong"}]},
            {
                "type": "text",
                "text": "here",
                "marks": [{"type": "link", "attrs": {"href": "https://example.org"}}],
            },
        ],
    }
    assert adf.to_text(doc(node)) == "Click here"


def test_hard_break_becomes_a_newline():
    node = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "a"},
            {"type": "hardBreak"},
            {"type": "text", "text": "b"},
        ],
    }
    assert adf.to_text(doc(node)) == "a\nb"


def test_mention_renders_its_display_text():
    node = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "ask "},
            {"type": "mention", "attrs": {"id": "557058:x", "text": "@Dana"}},
        ],
    }
    assert adf.to_text(doc(node)) == "ask @Dana"


def test_mention_without_text_falls_back_to_a_placeholder():
    node = {"type": "paragraph", "content": [{"type": "mention", "attrs": {"id": "557058:x"}}]}
    assert adf.to_text(doc(node)) == "@unknown"


def test_status_lozenge_is_bracketed():
    node = {"type": "paragraph", "content": [{"type": "status", "attrs": {"text": "DONE"}}]}
    assert adf.to_text(doc(node)) == "[DONE]"


def test_inline_card_renders_its_url():
    node = {
        "type": "paragraph",
        "content": [{"type": "inlineCard", "attrs": {"url": "https://example.org/a"}}],
    }
    assert adf.to_text(doc(node)) == "https://example.org/a"


def test_bullet_list_items_get_markers():
    assert adf.to_text(doc(bullet_list("first", "second"))) == "- first\n- second"


def test_ordered_list_items_are_numbered():
    ordered = {
        "type": "orderedList",
        "content": [
            {"type": "listItem", "content": [paragraph("step one")]},
            {"type": "listItem", "content": [paragraph("step two")]},
        ],
    }
    assert adf.to_text(doc(ordered)) == "1. step one\n2. step two"


def test_code_block_text_is_kept_verbatim():
    code = {
        "type": "codeBlock",
        "attrs": {"language": "python"},
        "content": [{"type": "text", "text": "assert x == 1"}],
    }
    assert adf.to_text(doc(code)) == "assert x == 1"


def test_rule_becomes_a_dash_line():
    assert adf.to_text(doc({"type": "rule"})) == "---"


def test_panel_contributes_its_children_not_itself():
    panel = {"type": "panel", "attrs": {"panelType": "warning"}, "content": [paragraph("careful")]}
    assert adf.to_text(doc(panel)) == "careful"


def test_table_row_cells_are_pipe_separated():
    table = {
        "type": "table",
        "content": [
            {
                "type": "tableRow",
                "content": [
                    {"type": "tableHeader", "content": [paragraph("Given")]},
                    {"type": "tableHeader", "content": [paragraph("Then")]},
                ],
            },
            {
                "type": "tableRow",
                "content": [
                    {"type": "tableCell", "content": [paragraph("valid password")]},
                    {"type": "tableCell", "content": [paragraph("access granted")]},
                ],
            },
        ],
    }
    assert adf.to_text(doc(table)) == "Given | Then\n\nvalid password | access granted"


def test_media_becomes_a_placeholder():
    # Attachments are never fetched or stored (ADR-009), but a reader should
    # be able to tell the ticket had one.
    media = {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "abc"}}]}
    assert adf.to_text(doc(media)) == "[attachment]"


def test_unknown_node_type_is_descended_into():
    # The forward-compatibility rule: a future ADF node will still hold its
    # text in `content`, so dropping it loses real content.
    future = {"type": "somethingNew2027", "content": [paragraph("still readable")]}
    assert adf.to_text(doc(future)) == "still readable"


def test_unknown_leaf_node_contributes_nothing():
    assert adf.to_text(doc({"type": "somethingNew2027"}, paragraph("kept"))) == "kept"


def test_malformed_children_are_skipped_not_fatal():
    node = {"type": "paragraph", "content": [{"type": "text", "text": "a"}, "not a node", 7, None]}
    assert adf.to_text(doc(node)) == "a"


def test_content_of_the_wrong_type_is_tolerated():
    assert adf.to_text({"type": "doc", "content": "not a list"}) is None


# ---------------------------------------------------------------------------
# Bounds. The input is untrusted, so both limits are part of the contract.
# ---------------------------------------------------------------------------


def test_deep_nesting_terminates_instead_of_exhausting_the_stack():
    node: dict[str, Any] = paragraph("bottom")
    for _ in range(adf.MAX_DEPTH * 3):
        node = {"type": "blockquote", "content": [node]}
    # The assertion is that this returns at all. Below MAX_DEPTH the text would
    # survive; past it the branch is abandoned, which is the intended trade.
    result = adf.to_text(doc(node))
    assert result is None or "bottom" in result


def test_oversized_output_is_truncated_and_says_so():
    long_text = "x" * 1000
    big = doc(*[paragraph(long_text) for _ in range(200)])
    result = adf.to_text(big)
    assert result is not None
    assert len(result) <= adf.MAX_OUTPUT_CHARS + len("\n[... truncated]")
    assert result.endswith("[... truncated]")


# ---------------------------------------------------------------------------
# extract_acceptance_criteria
# ---------------------------------------------------------------------------


def test_extracts_the_section_after_the_heading():
    description = doc(
        paragraph("A user cannot log in."),
        heading("Acceptance Criteria", 3),
        bullet_list("Valid password grants access", "Invalid password is refused"),
    )
    assert adf.extract_acceptance_criteria(description) == (
        "- Valid password grants access\n- Invalid password is refused"
    )


def test_text_before_the_heading_is_excluded():
    description = doc(
        paragraph("background noise"), heading("Acceptance Criteria"), paragraph("AC1")
    )
    assert adf.extract_acceptance_criteria(description) == "AC1"


@pytest.mark.parametrize(
    "label",
    [
        "Acceptance Criteria",
        "acceptance criteria",
        "Acceptance Criteria:",
        "AC",
        "ac:",
        "Acceptance",
    ],
)
def test_heading_wording_is_matched_loosely(label: str):
    # Teams write this differently and all of them mean the same thing.
    description = doc(heading(label), paragraph("AC1"))
    assert adf.extract_acceptance_criteria(description) == "AC1"


@pytest.mark.parametrize(
    "label", ["Acceptance Criteria are tricky", "Notes", "Background", "Tackle"]
)
def test_other_headings_do_not_match(label: str):
    description = doc(heading(label), paragraph("AC1"))
    assert adf.extract_acceptance_criteria(description) is None


def test_a_following_heading_at_the_same_level_ends_the_section():
    description = doc(
        heading("Acceptance Criteria", 2),
        paragraph("in scope"),
        heading("Out of scope", 2),
        paragraph("excluded"),
    )
    assert adf.extract_acceptance_criteria(description) == "in scope"


def test_a_following_shallower_heading_ends_the_section():
    description = doc(
        heading("Acceptance Criteria", 3),
        paragraph("in scope"),
        heading("Next up", 1),
        paragraph("excluded"),
    )
    assert adf.extract_acceptance_criteria(description) == "in scope"


def test_a_deeper_heading_stays_part_of_the_section():
    description = doc(
        heading("Acceptance Criteria", 2),
        heading("Happy path", 3),
        paragraph("logs in"),
    )
    result = adf.extract_acceptance_criteria(description)
    assert result is not None
    assert "Happy path" in result
    assert "logs in" in result


def test_heading_with_nothing_after_it_is_none():
    # A heading and no body is not acceptance criteria; returning "" would
    # make the caller store an empty string instead of NULL.
    assert adf.extract_acceptance_criteria(doc(heading("Acceptance Criteria"))) is None


def test_the_first_matching_heading_wins():
    description = doc(
        heading("Acceptance Criteria", 2),
        paragraph("first"),
        heading("Acceptance Criteria", 2),
        paragraph("second"),
    )
    assert adf.extract_acceptance_criteria(description) == "first"


def test_a_plain_string_description_has_no_extractable_section():
    # Working on the node tree is the point: in flat text "Acceptance
    # Criteria" could be a sentence, and guessing would be worse than nothing.
    assert adf.extract_acceptance_criteria("Acceptance Criteria\n- one") is None


@pytest.mark.parametrize("value", [None, 7, [], {"type": "doc"}])
def test_non_document_input_is_none(value: object):
    assert adf.extract_acceptance_criteria(value) is None
