from domains.executors.claude_code import (
    _CONTINUATION_NUDGE,
    build_continuation_argv,
    soft_wall,
    with_turn_cap,
)

ARGV = ["claude", "-p", "the instruction", "--append-system-prompt", "sys", "--output-format", "stream-json"]


def test_continuation_swaps_instruction_and_appends_resume():
    out = build_continuation_argv(
        ARGV, instruction="the instruction", nudge=_CONTINUATION_NUDGE, session_id="sid-1"
    )
    assert out is not None
    assert out[out.index("-p") + 1] == _CONTINUATION_NUDGE
    assert out[-2:] == ["--resume", "sid-1"]
    assert "the instruction" not in out


def test_no_session_id_means_no_continuation():
    assert build_continuation_argv(ARGV, instruction="the instruction", nudge="n", session_id="") is None


def test_unfindable_instruction_means_no_continuation():
    assert build_continuation_argv(ARGV, instruction="not present", nudge="n", session_id="sid") is None


def test_soft_wall_reserves_winddown_share():
    assert soft_wall(3600) == 3240  # 90%
    assert soft_wall(None) is None
    assert soft_wall(60) == 54


# Regression: Finding 1 — with_turn_cap always embeds --max-turns, so the old
# guard `"--max-turns" not in " ".join(pass_argv)` was always False for cap-set
# argv, causing real CLI failures to be re-tried up to max_extra_passes times.
# Confirm: a normal capped argv always contains --max-turns, and the check that
# was removed (not in) would therefore have evaluated to False (no break).
def test_with_turn_cap_always_embeds_flag_so_old_guard_was_wrong():
    capped = with_turn_cap(ARGV, 200)
    assert "--max-turns" in capped
    # The old broken guard: `"--max-turns" not in " ".join(capped)` → False
    # → failure would NOT break → CLI re-invoked on real crash. Fixed: plain
    # `exit_code not in (0, None)` breaks unconditionally.
    old_guard_would_have_allowed_retry = "--max-turns" not in " ".join(capped)
    assert not old_guard_would_have_allowed_retry  # proves the old logic was wrong
