import hashlib
import json

import pytest

from agent_terminal.shipping import (
    INTENT,
    NOT_SHIPPING,
    REFUSED,
    intent_digest,
    parse_intent,
    ship_stage,
)

TEXT = "Fix the thing\n\nBecause it was broken.\n"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "logs").mkdir()
    return tmp_path


def write_intent(tree, text=TEXT):
    (tree / "logs" / "ship-intent.md").write_text(text, encoding="utf-8")


def write_state(tree, stage, text=TEXT):
    digest = intent_digest(*parse_intent(text))
    state = {"stage": stage, "intent": digest}
    (tree / "logs" / "ship-state.json").write_text(json.dumps(state), encoding="utf-8")


def test_no_intent_is_not_shipping(tree):
    assert ship_stage(tree) == NOT_SHIPPING


def test_no_logs_directory_is_not_shipping(tmp_path):
    assert ship_stage(tmp_path) == NOT_SHIPPING


def test_a_written_intent_is_waiting_for_the_pass(tree):
    write_intent(tree)
    assert ship_stage(tree) == INTENT


def test_an_empty_intent_is_nothing(tree):
    write_intent(tree, "   \n")
    assert ship_stage(tree) == NOT_SHIPPING


def test_a_reaped_intent_is_gone(tree):
    (tree / "logs" / "ship-intent.shipped.md").write_text(TEXT, encoding="utf-8")
    assert ship_stage(tree) == NOT_SHIPPING


def test_words_already_shipped_are_spent(tree):
    write_intent(tree)
    write_state(tree, "shipped")
    assert ship_stage(tree) == NOT_SHIPPING


def test_a_fresh_intent_after_a_ship_is_waiting_again(tree):
    write_intent(tree, "A new change\n\nFor another reason.\n")
    write_state(tree, "shipped")
    assert ship_stage(tree) == INTENT


@pytest.mark.parametrize("stage", ["refused", "failed"])
def test_a_pass_that_could_not_ship_it(tree, stage):
    write_intent(tree)
    write_state(tree, stage)
    assert ship_stage(tree) == REFUSED


def test_a_refusal_of_older_words_says_nothing_about_these(tree):
    write_intent(tree, "Rewritten\n\nAfter the refusal.\n")
    write_state(tree, "refused")
    assert ship_stage(tree) == INTENT


def test_a_corrupt_state_file_reads_as_none(tree):
    write_intent(tree)
    (tree / "logs" / "ship-state.json").write_text("{", encoding="utf-8")
    assert ship_stage(tree) == INTENT


def test_parse_intent_drops_a_heading_mark():
    assert parse_intent("# Subject\n\nBody\n") == ("Subject", "Body")


def test_parse_intent_of_nothing():
    assert parse_intent("") == ("", "")


def test_the_digest_is_the_passs():
    # Spelled out from devkit's `Intent.digest`: a mismatch would read every shipped
    # intent as still waiting.
    expected = hashlib.sha256(b"Subject\nBody").hexdigest()[:12]
    assert intent_digest("Subject", "Body") == expected
