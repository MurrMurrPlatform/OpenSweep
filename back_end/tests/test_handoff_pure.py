"""Pure tests for the terminal-handoff helpers (no Neo4j, no filesystem)."""

from domains.investigations.services.handoff import (
    HANDOFF_FILENAME,
    build_resume_command,
    build_seeded_command,
    claude_project_slug,
    handoff_mode,
    render_handoff_markdown,
)


def test_slug_replaces_every_non_alphanumeric_char():
    assert claude_project_slug("/host/sandboxes/abc123") == "-host-sandboxes-abc123"
    # Dots and underscores are NOT preserved — matches ~/.claude/projects layout.
    assert claude_project_slug("/Users/j/.opensweep/x_y") == "-Users-j--opensweep-x-y"
    assert claude_project_slug("") == ""


def test_mode_resume_needs_claude_session_and_live_sandbox():
    assert handoff_mode(executor="claude_code", cli_session_id="abc", sandbox_live=True) == (
        "resume",
        "",
    )


def test_mode_seeded_for_other_executors_or_missing_session():
    mode, _ = handoff_mode(executor="codex", cli_session_id="abc", sandbox_live=True)
    assert mode == "seeded"
    mode, _ = handoff_mode(executor="claude_code", cli_session_id="", sandbox_live=True)
    assert mode == "seeded"


def test_mode_unavailable_without_workspace_explains_recovery():
    mode, reason = handoff_mode(executor="claude_code", cli_session_id="abc", sandbox_live=False)
    assert mode == "unavailable"
    assert "rebuild" in reason


def test_resume_command_copies_session_into_host_slug_then_resumes():
    cmd = build_resume_command(
        host_path="~/.opensweep/sandboxes/abc123",
        container_path="/host/sandboxes/abc123",
        cli_session_id="11111111-2222-3333-4444-555555555555",
    )
    # Quoted with the deliberate ~ → $HOME rewrite so expansion still happens.
    assert cmd.startswith('cd "$HOME/.opensweep/sandboxes/abc123" && ')
    # Source: the slug the CONTAINER cwd produced; destination: computed on the
    # host from the resolved cwd, so it is correct for any host home/symlinks.
    assert "-host-sandboxes-abc123" in cmd
    assert "pwd -P" in cmd
    assert cmd.rstrip().endswith("claude --resume 11111111-2222-3333-4444-555555555555")


def test_shell_path_quotes_non_tilde_paths_verbatim():
    from domains.investigations.services.handoff import _shell_path

    assert _shell_path("/srv/sand boxes/x") == '"/srv/sand boxes/x"'
    assert _shell_path("~/.opensweep/sandboxes/x") == '"$HOME/.opensweep/sandboxes/x"'


def test_seeded_command_points_claude_at_the_handoff_file():
    cmd = build_seeded_command(host_path="~/.opensweep/sandboxes/abc123")
    assert cmd.startswith('cd "$HOME/.opensweep/sandboxes/abc123" && claude ')
    assert HANDOFF_FILENAME in cmd


def test_handoff_markdown_carries_context_and_guardrails():
    md = render_handoff_markdown(
        title="Fix flaky retry test",
        playbook="thread",
        work_branch="opensweep/t-1",
        base_branch="main",
        entries=[
            {"role": "user", "content": "please fix the retry test"},
            {"role": "assistant", "content": "I found the race in ..."},
        ],
    )
    assert "Fix flaky retry test" in md
    assert "opensweep/t-1" in md and "main" in md
    assert "please fix the retry test" in md
    assert "do not switch branches" in md.lower()


def test_handoff_markdown_sanitizes_user_editable_header_fields():
    md = render_handoff_markdown(
        title="evil `code` **bold**\ninjected line",
        playbook="thread",
        work_branch="feat/`tick`",
        base_branch="main",
        entries=[],
    )
    header = md.split("## Conversation")[0]
    assert "`code`" not in header and "**bold**" not in header
    assert "injected line" in header  # kept, but folded onto the same line
    assert "evil 'code' bold injected line" in header


def test_handoff_markdown_caps_transcript_keeping_the_tail():
    entries = [
        {"role": "user", "content": "x" * 30000},
        {"role": "assistant", "content": "THE-TAIL"},
    ]
    md = render_handoff_markdown(
        title="t", playbook="thread", work_branch="w", base_branch="b", entries=entries, cap=5000
    )
    assert "THE-TAIL" in md
    assert len(md) < 12000
