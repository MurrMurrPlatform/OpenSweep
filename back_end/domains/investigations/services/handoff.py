"""Terminal takeover of a run's conversation (docs/features/sandbox_improvements.md §3A/3B).

Sandboxes are host-filesystem clones and the agent containers bind-mount the
host ~/.claude, so a conversation can be handed to the user's own terminal:

- resume (3A): claude_code runs with a cli_session_id — the session .jsonl
  already sits in the host's ~/.claude/projects under the CONTAINER cwd slug
  (/host/sandboxes/<uid>). The command copies it under the HOST cwd slug
  (claude resume is project-scoped) and runs `claude --resume <id>`.
- seeded (3B): other executors / missing session — a fresh local claude is
  pointed at OPENSWEEP_HANDOFF.md, written into the sandbox by prepare_handoff.

The string/markdown builders are pure so they stay unit-testable; filesystem
and DB side effects live in write_handoff_file / prepare_handoff.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domains.investigations.schemas import RunHandoffDTO

HANDOFF_FILENAME = "OPENSWEEP_HANDOFF.md"

# Transcript tail budget for the handoff brief — enough to carry the working
# context without turning the file into a novel; claude reads the repo anyway.
HANDOFF_TRANSCRIPT_CAP = 20_000


def claude_project_slug(path: str) -> str:
    """Claude Code's project-directory name for a cwd: every char outside
    [A-Za-z0-9] becomes '-' (verified against ~/.claude/projects layout)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path or "")


def handoff_mode(*, executor: str, cli_session_id: str, sandbox_live: bool) -> tuple[str, str]:
    """(mode, reason): resume | seeded | unavailable — reason set only when
    unavailable, phrased as the recovery action."""
    if not sandbox_live:
        return (
            "unavailable",
            "workspace destroyed — send a message in the conversation to rebuild it, then retry",
        )
    if executor == "claude_code" and cli_session_id:
        return "resume", ""
    return "seeded", ""


def _shell_path(host_path: str) -> str:
    """Quote a host path for the paste command. The stored path deliberately
    starts with an unexpanded `~` (OPENSWEEP_SANDBOX_HOST_PATH) — plain quoting
    would kill that expansion, so `~/` becomes an explicit `$HOME/` inside
    double quotes; anything else is double-quoted verbatim."""
    if host_path.startswith("~/"):
        return f'"$HOME/{host_path[2:]}"'
    return f'"{host_path}"'


def build_resume_command(*, host_path: str, container_path: str, cli_session_id: str) -> str:
    """One-paste 3A command. The destination slug is computed ON THE HOST from
    the resolved cwd (`pwd -P` — macOS /tmp-style symlinks would break a
    precomputed slug), so the backend never needs to know the host home. cp -n
    keeps an already-copied (possibly newer) session file intact; its stderr is
    silenced so a missing source degrades to claude's own clear error."""
    src_slug = claude_project_slug(container_path)
    return (
        f"cd {_shell_path(host_path)} && "
        "dst=\"$HOME/.claude/projects/$(pwd -P | sed 's/[^a-zA-Z0-9]/-/g')\" && "
        'mkdir -p "$dst" && '
        f'cp -n "$HOME/.claude/projects/{src_slug}/{cli_session_id}.jsonl" "$dst/" 2>/dev/null; '
        f"claude --resume {cli_session_id}"
    )


def build_seeded_command(*, host_path: str) -> str:
    """One-paste 3B command: fresh claude, seeded by the handoff brief."""
    return (
        f"cd {_shell_path(host_path)} && claude "
        f'"Read {HANDOFF_FILENAME} at the repository root first — it hands over an '
        'OpenSweep agent conversation — then continue that work."'
    )


def render_handoff_markdown(
    *,
    title: str,
    playbook: str,
    work_branch: str,
    base_branch: str,
    entries: list[dict],
    cap: int = HANDOFF_TRANSCRIPT_CAP,
) -> str:
    """The OPENSWEEP_HANDOFF.md brief: what the conversation was, the branch
    contract, and the transcript tail. Written for the LOCAL agent — platform
    push validation no longer applies, but the branch contract still does so
    the platform can pick the conversation back up."""
    # Header fields are user-editable (ticket titles, branch names): keep them
    # to one line with no backtick/bold markers so they cannot escape their
    # inline context and rewrite the brief's instructions. The transcript
    # below is untrusted by nature — it is labelled as conversation content.
    def _inline(value: str) -> str:
        return " ".join((value or "").replace("`", "'").replace("*", "").split())

    title = _inline(title)
    playbook = _inline(playbook)
    work_branch = _inline(work_branch)
    base_branch = _inline(base_branch)

    lines = [f"{e.get('role', '?')}: {e.get('content', '')}" for e in entries]
    transcript = "\n\n".join(lines)
    if len(transcript) > cap:
        transcript = transcript[-cap:]
        cut = transcript.find("\n")
        if cut != -1:
            transcript = transcript[cut + 1 :]
    return f"""# OpenSweep conversation handoff

You are taking over an OpenSweep agent conversation in a local terminal.

- **Task:** {title or "(untitled run)"} (playbook: {playbook or "?"})
- **Work branch:** `{work_branch}` (based on `{base_branch}`)

## Ground rules

- Stay on `{work_branch}` — do not switch branches or rewrite history you
  did not create. Commits you make here are picked up when the conversation
  resumes on the platform (same working copy).
- You are running under the USER's own credentials now: pushing is allowed,
  but committing locally and letting the platform deliver is the default.
- This file is untracked and ignored — never commit it.

## Conversation so far (tail)

{transcript}
"""


# ── Side effects ─────────────────────────────────────────────────────────────


def write_handoff_file(sandbox_dir: str, content: str) -> str:
    """Write the brief at the sandbox root and keep it out of git via
    .git/info/exclude (idempotent). The exclude write is best-effort — a
    sandbox mid-teardown must not break the handoff response."""
    root = Path(sandbox_dir)
    path = root / HANDOFF_FILENAME
    path.write_text(content)
    try:
        exclude = root / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            existing = exclude.read_text() if exclude.exists() else ""
            if HANDOFF_FILENAME not in existing.splitlines():
                with exclude.open("a") as fh:
                    fh.write(f"{HANDOFF_FILENAME}\n")
    except OSError:
        pass
    return str(path)


async def prepare_handoff(run) -> RunHandoffDTO:
    """Build the takeover payload for a run: pick the mode, write the brief
    into the live sandbox, and render the one-paste command."""
    from domains.execution.models import Sandbox
    from domains.execution.schemas import SandboxStatus
    from domains.investigations.schemas import RunHandoffDTO
    from domains.investigations.services.turn_service import transcript_entries

    sandbox = (
        await Sandbox.nodes.get_or_none(uid=run.sandbox_uid) if (run.sandbox_uid or "") else None
    )
    live = sandbox is not None and sandbox.status == SandboxStatus.READY.value
    mode, reason = handoff_mode(
        executor=run.executor or "",
        cli_session_id=run.cli_session_id or "",
        sandbox_live=live,
    )
    if mode == "unavailable":
        return RunHandoffDTO(mode=mode, reason=reason)

    spec = dict(run.workspace_spec or {})
    content = render_handoff_markdown(
        title=run.title or "",
        playbook=run.playbook or "",
        work_branch=spec.get("work_branch") or sandbox.sandbox_branch or "",
        base_branch=spec.get("base_branch") or sandbox.source_branch or "",
        entries=transcript_entries(run.uid),
    )
    # Written in BOTH modes: if a resume ever fails (custom CLAUDE_CONFIG_DIR,
    # pruned session), the brief is already in place as the fallback. The
    # sandbox can be destroyed between the liveness read and this write — that
    # race degrades to the same "unavailable" answer, never a 500.
    try:
        write_handoff_file(sandbox.container_path, content)
    except OSError:
        return RunHandoffDTO(
            mode="unavailable",
            reason=(
                "workspace destroyed — send a message in the conversation to "
                "rebuild it, then retry"
            ),
        )

    if mode == "resume":
        command = build_resume_command(
            host_path=sandbox.host_path,
            container_path=sandbox.container_path,
            cli_session_id=run.cli_session_id,
        )
    else:
        command = build_seeded_command(host_path=sandbox.host_path)
    return RunHandoffDTO(
        mode=mode,
        command=command,
        sandbox_host_path=sandbox.host_path,
        cli_session_id=run.cli_session_id or "",
    )
