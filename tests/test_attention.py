import pytest

from agent_terminal.attention import (
    BLOCKED,
    FINISHED,
    IDLE,
    MAX_OSC,
    NEEDS_INPUT,
    NO_ATTENTION,
    QUERY,
    QUERY_REPLY,
    SHELL,
    STARTING,
    WORKING,
    OscScanner,
    Presence,
    advance,
    clean_title,
    is_user_input,
    parse_program_state,
    parse_title,
)

ST = "\x1b\\"


def osc(payload: str, end: str = ST) -> str:
    return f"\x1b]{payload}{end}"


class TestOscScanner:
    def test_finds_a_payload_terminated_by_st(self):
        assert OscScanner().feed(f"before{osc('7501;state=idle')}after") == ["7501;state=idle"]

    def test_finds_a_payload_terminated_by_bel(self):
        assert OscScanner().feed(osc("0;title", "\x07")) == ["0;title"]

    def test_finds_several_in_order(self):
        assert OscScanner().feed(osc("0;a") + "text" + osc("2;b")) == ["0;a", "2;b"]

    def test_plain_text_has_none(self):
        assert OscScanner().feed("hello \x1b[31mred\x1b[0m") == []

    @pytest.mark.parametrize("cut", range(1, len(osc("7501;?"))))
    def test_a_sequence_may_be_cut_anywhere(self, cut):
        scanner = OscScanner()
        whole = osc("7501;?")
        assert scanner.feed(whole[:cut]) + scanner.feed(whole[cut:]) == ["7501;?"]

    def test_one_character_at_a_time(self):
        scanner = OscScanner()
        found = [payload for char in osc("0;abc") for payload in scanner.feed(char)]
        assert found == ["0;abc"]

    def test_an_unterminated_osc_opening_another_is_abandoned(self):
        assert OscScanner().feed("\x1b]0;lost\x1b]2;kept\x07") == ["2;kept"]

    def test_an_oversized_payload_is_dropped_and_the_stream_recovers(self):
        scanner = OscScanner()
        assert scanner.feed("\x1b]1337;" + "x" * (MAX_OSC + 1)) == []
        assert scanner.feed(osc("0;next")) == ["0;next"]

    def test_an_escape_ending_a_chunk_is_held_for_the_next(self):
        scanner = OscScanner()
        assert scanner.feed("text\x1b") == []
        assert scanner.feed("]0;t\x07") == ["0;t"]


class TestParsing:
    @pytest.mark.parametrize(
        ("payload", "state"),
        [
            ("7501;state=idle:app=codex", "idle"),
            ("7501;state=blocked:app=claude-code:kind=permission:msg=YQ==", "blocked"),
            ("7501;state=clear", "clear"),
            ("7501;?", None),
            ("7501;", None),
            ("0;state=idle", None),
            ("75010;state=idle", None),
        ],
    )
    def test_program_state(self, payload, state):
        assert parse_program_state(payload) == state

    @pytest.mark.parametrize(
        ("payload", "title"),
        [("0;hello", "hello"), ("2;hello", "hello"), ("0;", ""), ("1;icon", None), ("0", None)],
    )
    def test_title(self, payload, title):
        assert parse_title(payload) == title

    def test_the_query_reply_is_what_claude_accepts(self):
        # Claude takes any OSC 7501 reply whose data starts with "?".
        assert QUERY_REPLY == f"\x1b]{QUERY}{ST}"
        assert QUERY.split(";", 1)[1].startswith("?")


class TestCleanTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Shell probe file write", "Shell probe file write"),
            ("\u2733 Fix the bug", "Fix the bug"),
            ("\u25d0 Fix the bug", "Fix the bug"),
            ("Run probe command | demo", "Run probe command"),
            ("[ ! ] Action Required | Run probe command | demo", "Run probe command"),
            ("[draft] notes", "[draft] notes"),
            ("demo", ""),
            ("claude", ""),
            ("Claude Code", ""),
            ("\u2733 Claude Code", ""),
            ("codex", ""),
            ("C:\\Program Files\\WindowsApps\\pwsh.exe", ""),
            ("", ""),
        ],
    )
    def test_keeps_only_what_describes_the_work(self, raw, expected):
        assert clean_title(raw, "demo") == expected


@pytest.mark.parametrize(
    ("data", "typed"),
    [
        ("y", True),
        ("\r", True),
        ("hello", True),
        ("\x1b[?1;2c", False),
        ("\x1b]10;rgb\x1b\\", False),
    ],
)
def test_is_user_input(data, typed):
    assert is_user_input(data) is typed


class TestAdvance:
    def test_working_clears_attention(self):
        presence = Presence(status=BLOCKED, attention=NEEDS_INPUT)
        assert advance(presence, "working") == Presence(status=WORKING)

    @pytest.mark.parametrize("state", ["blocked", "error", "failed"])
    def test_stopping_on_the_user_needs_their_input(self, state):
        assert advance(Presence(status=WORKING), state) == Presence(BLOCKED, NEEDS_INPUT, False)

    def test_finishing_a_turn_is_not_a_request_for_input(self):
        assert advance(Presence(status=WORKING), "done") == Presence(IDLE, FINISHED, False)

    def test_an_answered_question_that_ends_the_turn_reads_as_finished(self):
        assert advance(Presence(BLOCKED, NEEDS_INPUT), "done").attention == FINISHED

    def test_a_question_after_a_finish_is_input_again(self):
        assert advance(Presence(IDLE, FINISHED), "blocked").attention == NEEDS_INPUT

    def test_a_turns_second_done_keeps_what_the_first_set(self):
        assert advance(Presence(IDLE, FINISHED), "done").attention == FINISHED

    def test_startup_idle_is_not_attention(self):
        assert advance(Presence(status=STARTING), "idle") == Presence(status=IDLE)

    def test_codex_flicker_at_startup_is_not_attention(self):
        presence = Presence(status=STARTING)
        for state in ("idle", "working", "idle"):
            presence = advance(presence, state)
        assert presence == Presence(status=IDLE)

    def test_a_restored_session_settling_is_not_attention(self):
        assert advance(Presence(status=STARTING), "done") == Presence(status=IDLE)

    def test_a_new_stop_brings_a_parked_tab_back(self):
        assert advance(Presence(WORKING, parked=True), "blocked").parked is False
        assert advance(Presence(WORKING, parked=True), "done").parked is False

    def test_a_turns_second_done_leaves_a_parked_tab_alone(self):
        # Claude sends `done` again with its summary, seconds after the first.
        presence = advance(Presence(status=WORKING), "done")
        parked = Presence(presence.status, presence.attention, parked=True)
        assert advance(parked, "done") == parked

    def test_parking_survives_working(self):
        assert advance(Presence(IDLE, parked=True), "working").parked is True

    def test_idle_does_not_answer_a_waiting_agent(self):
        assert advance(Presence(IDLE, attention=NEEDS_INPUT), "idle").attention == NEEDS_INPUT

    @pytest.mark.parametrize("kind", [NEEDS_INPUT, FINISHED])
    def test_startup_working_does_not_clear_a_restored_tabs_attention(self, kind):
        # Codex reports idle, working, idle on every start, with nobody having answered.
        presence = Presence(status=STARTING, attention=kind)
        for state in ("idle", "working", "idle"):
            presence = advance(presence, state, user_engaged=False)
        assert presence == Presence(status=IDLE, attention=kind)

    def test_the_agent_leaving_ends_attention(self):
        assert advance(Presence(BLOCKED, attention=NEEDS_INPUT), "clear") == Presence(status=SHELL)

    def test_an_unknown_state_changes_nothing(self):
        presence = Presence(WORKING, attention=NO_ATTENTION, parked=True)
        assert advance(presence, "something-new") == presence
