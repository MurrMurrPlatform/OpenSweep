"""Escaped-markdown repair at the platform-tool ingestion boundary.

The sample below is the shape observed in run a3bffb78, where a doc-update
agent sent 27 proposed bodies with zero real newlines and hundreds of literal
`\\n` markers.
"""

import json

from domains.platform_tools.text_repair import looks_escaped, repair_args, repair_text

ESCAPED_BODY = (
    "\\# Backend — LLM Providers Domain\\n\\n`back_end/domains/llm_providers/` — "
    "provider configuration and credential management.\\n\\n## LLMProvider model\\n\\n"
    "An **LLMProvider** node stores:\\n- `kind` — provider type\\n- `name` — display "
    "name\\n- `credential_secret` — sealed by `secretbox.py` (not a boolean "
    '\\"encrypted\\" flag)\\n\\nEach org has its own set.\\n'
)


def test_repairs_escaped_markdown_body():
    repaired = repair_text(ESCAPED_BODY)
    assert repaired.startswith("# Backend — LLM Providers Domain\n\n")
    assert "\\n" not in repaired
    assert "\n## LLMProvider model\n" in repaired
    assert '(not a boolean "encrypted" flag)' in repaired
    # The em dash survives — a codecs `unicode_escape` decode would mangle it.
    assert "— provider configuration" in repaired


def test_leaves_well_formed_markdown_untouched():
    body = "# Title\n\nA paragraph about `\\n` escapes.\n" + "filler line\n" * 40
    assert repair_text(body) == body
    assert not looks_escaped(body)


def test_leaves_short_single_line_text_untouched():
    # Prose *about* escapes, not an escaped document.
    text = "Split on \\n, then join with \\n\\n before writing the file."
    assert repair_text(text) == text


def test_preserves_letter_escapes_in_repaired_text():
    text = (
        "\\# Regex notes\\n\\nUse `\\d+` for digits and `\\s` for whitespace.\\n"
        "A literal backslash-n is written `\\\\n` in source.\\n" + "\\n- filler\\n" * 20
    )
    repaired = repair_text(text)
    assert repaired.startswith("# Regex notes\n")
    assert "`\\d+`" in repaired
    assert "`\\s`" in repaired
    assert "`\\n` in source" in repaired


def test_leaves_serialized_json_payloads_untouched():
    # attach_artifact(content=…) with a trace log: its `\n`s are correct JSON
    # encoding, and decoding them would corrupt the artifact.
    payload = json.dumps(
        {"events": [{"ts": i, "msg": "line one\nline two\nline three"} for i in range(10)]}
    )
    assert not looks_escaped(payload)
    assert repair_text(payload) == payload


def test_repair_args_walks_nested_structures():
    args = {
        "slug": "backend/llm-providers",
        "proposed_body": ESCAPED_BODY,
        "watch_paths": ["back_end/domains/llm_providers/"],
        "changes": {"description": ESCAPED_BODY},
        "count": 3,
        "archived": False,
        "missing": None,
    }
    out = repair_args(args)
    assert out["proposed_body"].startswith("# Backend")
    assert out["changes"]["description"].startswith("# Backend")
    assert out["slug"] == "backend/llm-providers"
    assert out["watch_paths"] == ["back_end/domains/llm_providers/"]
    assert out["count"] == 3
    assert out["archived"] is False
    assert out["missing"] is None
