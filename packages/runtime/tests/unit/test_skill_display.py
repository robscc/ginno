"""Slash-skill display folding (history replay shows "/name request", never
the raw ``<skill name="...">`` SKILL.md injection).

Fixes the regression where a ``/todoist …`` turn rendered the entire skill
body in the user bubble (and seeded the auto-title with it). The substitution
into the persisted HumanMessage is by design (model scaffolding); only the
UI/history projection is folded here.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import HumanMessage

from ginno_runtime.server import (
    _human_ui_blocks,
    _messages_to_ui,
    parse_skill_wrap,
    skill_display_text,
)
from ginno_runtime.skills.loader import Skill, wrap_skill_body


def _skill(name: str = "todoist", body: str = "# Todoist Skill\n\nManage tasks.") -> Skill:
    return Skill(
        name=name,
        description=f"{name} skill",
        body=body,
        path=Path(f"/x/skills/{name}/SKILL.md"),
    )


# --------------------------------------------------------------------------- #
# parse_skill_wrap / skill_display_text
# --------------------------------------------------------------------------- #
def test_parse_skill_wrap_with_request():
    text = wrap_skill_body(_skill(), "添加一个明天的提醒")
    assert parse_skill_wrap(text) == ("todoist", "添加一个明天的提醒")


def test_parse_skill_wrap_bare_invocation_has_empty_request():
    text = wrap_skill_body(_skill(), "")
    assert parse_skill_wrap(text) == ("todoist", "")


def test_parse_skill_wrap_plain_text_returns_none():
    assert parse_skill_wrap("just a normal message") is None
    assert parse_skill_wrap("") is None
    assert parse_skill_wrap(None) is None


def test_parse_skill_wrap_requires_leading_tag():
    # A skill tag mid-message is not a slash invocation — leave it alone.
    assert parse_skill_wrap('look at <skill name="x">y</skill>') is None


def test_skill_display_text_with_request():
    text = wrap_skill_body(_skill(), "添加提醒")
    assert skill_display_text(text) == "/todoist 添加提醒"


def test_skill_display_text_bare():
    assert skill_display_text(wrap_skill_body(_skill(), "")) == "/todoist"


def test_skill_display_text_plain_returns_none():
    assert skill_display_text("hello world") is None


# --------------------------------------------------------------------------- #
# _human_ui_blocks (the block builder used by history replay)
# --------------------------------------------------------------------------- #
def test_human_ui_blocks_folds_skill_to_chip():
    blocks = _human_ui_blocks(wrap_skill_body(_skill(), "添加提醒"))
    assert blocks == [{"kind": "skill", "name": "todoist", "text": "添加提醒"}]


def test_human_ui_blocks_bare_skill_chip_has_no_text():
    blocks = _human_ui_blocks(wrap_skill_body(_skill(), ""))
    assert blocks == [{"kind": "skill", "name": "todoist"}]


def test_human_ui_blocks_plain_text_unchanged():
    assert _human_ui_blocks("plain") == [{"kind": "text", "text": "plain"}]


def test_human_ui_blocks_multimodal_skill_folds_text_part_only():
    content = [
        {"type": "text", "text": wrap_skill_body(_skill(), "看图")},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]
    blocks = _human_ui_blocks(content)
    assert blocks[0] == {"kind": "skill", "name": "todoist", "text": "看图"}
    assert blocks[1]["kind"] == "image"


def test_human_ui_blocks_body_containing_skill_close_tag():
    # A SKILL.md body that itself mentions </skill> must not truncate the fold.
    body = "Example:\n<skill name=\"other\">x</skill>\nReal instructions."
    blocks = _human_ui_blocks(wrap_skill_body(_skill(body=body), "req"))
    assert blocks == [{"kind": "skill", "name": "todoist", "text": "req"}]


# --------------------------------------------------------------------------- #
# _messages_to_ui end-to-end (history endpoint shape)
# --------------------------------------------------------------------------- #
def test_messages_to_ui_skill_turn_renders_user_skill_block():
    human = HumanMessage(content=wrap_skill_body(_skill(), "添加提醒"), id="h1")
    ui = _messages_to_ui([human], "dev")
    assert len(ui) == 1
    assert ui[0]["role"] == "user"
    assert ui[0]["blocks"] == [{"kind": "skill", "name": "todoist", "text": "添加提醒"}]
