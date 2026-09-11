"""Tests for claude/.claude/scripts/marker.sh."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import textwrap
import time

import pytest
from helpers import (
    CANARY_CONTENT,
    HOOKS_DIR,
    SCRIPTS_DIR,
    TRAVERSAL_SESSION_ID,
    agent_input,
    bare_remote_with_default_branch,
    bash_input,
    edit_input,
    git_toplevel,
    head_sha,
    plan_review_marker_path,
    plant_traversal_canary,
    push_conflicting_edit_to_origin,
    read_input,
    run_hook,
    skill_review_marker_path,
    staged_diff_hash,
    staged_diff_hash_at_base,
    write_marker,
    write_plan_review_marker,
    write_skill_review_marker,
)

from .conftest import _seed_session, assert_cap_engaged

MARKER_SCRIPT = SCRIPTS_DIR / "marker.sh"
PR_DIFF_SCRIPT = SCRIPTS_DIR / "pr-diff-against-base.sh"


def _ready_for_review_marker_path(home, repo, session_id: str):
    """No shared helper exists for this marker kind (helpers.py covers
    code-review, skill-review, and plan-review) -- same repo-hash recipe as
    marker_path/plan_review_marker_path above, just the ready-for-review dir."""
    repo_hash = hashlib.sha256(git_toplevel(repo).encode()).hexdigest()
    return home / ".claude" / "ready-for-review-markers" / f"{repo_hash}.{session_id}"


def _cumulative_review_marker_path(home, repo, session_id: str):
    """Same repo-hash recipe as _ready_for_review_marker_path above, just the
    cumulative-review dir."""
    repo_hash = hashlib.sha256(git_toplevel(repo).encode()).hexdigest()
    return home / ".claude" / "cumulative-review-markers" / f"{repo_hash}.{session_id}"


def _cumulative_review_subject_path(home, repo, session_id: str):
    """The recorded-subject path pr-diff-against-base.sh --record writes and
    `write cumulative-review` reads. Keyed by repo hash and session id, the
    same <repo-hash>.<session-id> convention every completion marker kind
    uses, so two sessions recording in the same worktree can't clobber or
    consume each other's subject."""
    repo_hash = hashlib.sha256(git_toplevel(repo).encode()).hexdigest()
    return home / ".claude" / "cumulative-review-subject-markers" / f"{repo_hash}.{session_id}"


def _cumulative_review_diff_artifact_path(home, repo, session_id: str):
    """The step-4 diff-file artifact path pr-diff-against-base.sh --diff-file
    writes. Same <repo-hash>.<session-id> keying as
    _cumulative_review_subject_path above, in its own directory since the
    two artifacts have independent lifecycles (marker.sh's `write
    cumulative-review` consumes the subject; nothing consumes this one --
    only `deactivate ready-for-review` removes it)."""
    repo_hash = hashlib.sha256(git_toplevel(repo).encode()).hexdigest()
    return home / ".claude" / "cumulative-review-diff-markers" / f"{repo_hash}.{session_id}"


def _record_subject(
    repo, home, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """Runs `pr-diff-against-base.sh --record` -- step 3's own diff command
    with the recording flag -- so a test's setup mirrors what step 3 actually
    does before `write cumulative-review` runs."""
    env = {**os.environ, "HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(PR_DIFF_SCRIPT), "--record"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def _write_diff_artifact(
    repo, home, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """Runs `pr-diff-against-base.sh --diff-file` -- step 4's own diff
    command -- with stdout discarded, mirroring step 4's `> /dev/null`
    invocation. Distinct from _record_subject above: neither helper produces
    or locates the other's artifact."""
    env = {**os.environ, "HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(PR_DIFF_SCRIPT), "--diff-file"],
        cwd=repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _gh_shim_source(base_ref: str | None) -> str:
    """Return source for a gh shim answering `gh pr view --json baseRefName
    --jq .baseRefName`, duplicated from
    scripts/tests/test_pr_diff_against_base.py's helper of the same name
    rather than cross-imported -- conftest.py's _dead_pid documents the same
    neither-test-tree-imports-the-other convention.

    base_ref=None models `gh pr view` failing (no PR open, or gh not
    authenticated), exercising pr-diff-against-base.sh's fallback to a
    repo's own refs/remotes/origin/HEAD symref.
    """
    body = "sys.exit(1)" if base_ref is None else f"print({base_ref!r})"
    return textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        args = sys.argv[1:]
        if args[:2] == ["pr", "view"] and "--json" in args and "baseRefName" in args:
            {body}
        else:
            sys.exit(1)
    """)


def _env_with_gh_shim(tmp_path, base_ref: str | None) -> dict:
    """Build a PATH-prepending env dict with a gh shim reporting base_ref --
    guarantees marker.sh's `cumulative-review` arm never makes a real gh
    network call in these tests, regardless of the host's own gh state."""
    shim_dir = tmp_path / "gh_shim"
    shim_dir.mkdir()
    gh_shim = shim_dir / "gh"
    gh_shim.write_text(_gh_shim_source(base_ref))
    gh_shim.chmod(0o755)
    return {"PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def _arm_default_branch_ref(repo) -> None:
    """Add refs/remotes/origin/main (+ origin/HEAD symref) at the repo's
    current HEAD -- the minimum pr-diff-against-base.sh needs to resolve a
    base ref at all, shared by both the non-empty-diff and zero-diff arming
    below.

    Applied on demand to the specific tests exercising `write
    cumulative-review`/`status`, never to the shared git_repo fixture
    itself: require-plugin-version-bump.sh's BASE resolution reads the
    identical refs/remotes/origin/HEAD symref, and stops degrading to
    BASE=HEAD once it is present -- test_require_plugin_version_bump.py's
    TestRequirePluginVersionBump class is built on that degraded-mode
    assumption (see its own comment), so arming it repo-wide via git_repo
    breaks every test in that class.
    """
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
    )


def _arm_default_branch_ref_and_second_commit(repo) -> None:
    """_arm_default_branch_ref, then a second commit on a new file -- the
    minimum pr-diff-against-base.sh needs to resolve a non-empty committed
    diff.

    Touches a new file (second.txt), not file.txt, so file.txt's committed
    content is unaffected -- but the commit is made via `git commit` without
    `-a`, so it also folds in git_repo's own pre-staged file.txt change
    (still sitting in the index at this point) alongside second.txt.
    """
    _arm_default_branch_ref(repo)
    (repo / "second.txt").write_text("second file\n")
    subprocess.run(["git", "add", "second.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add second file"], cwd=repo, check=True)


@pytest.fixture
def cumulative_diff_repo(git_repo):
    """git_repo, armed via _arm_default_branch_ref_and_second_commit so
    pr-diff-against-base.sh resolves a non-empty committed diff. Local to
    this file (not conftest.py) since only TestMarkerScriptCumulativeReview
    needs it -- see that helper's docstring for why the arming must not
    reach the shared git_repo fixture itself."""
    _arm_default_branch_ref_and_second_commit(git_repo)
    return git_repo


@pytest.fixture
def zero_diff_repo(git_repo):
    """git_repo, armed via _arm_default_branch_ref only (no second commit)
    so refs/remotes/origin/main points at the same commit as HEAD --
    pr-diff-against-base.sh resolves a merge-base equal to HEAD, producing
    a genuinely empty `git diff`. git_repo's own pre-staged file.txt change
    sits uncommitted in the index, so it never reaches the committed diff
    pr-diff-against-base.sh reads."""
    _arm_default_branch_ref(git_repo)
    return git_repo


def _advance_origin_main_and_rebase(repo, commit_fn) -> None:
    """Move refs/remotes/origin/main forward by one commit built by
    commit_fn (which mutates and stages the working tree; this function
    commits it), then rebase the checked-out branch back onto the new
    origin/main -- reproduces a conflict-free rebase onto a moved default
    branch, always producing a new HEAD SHA regardless of whether the
    cumulative diff bytes actually changed (docs/design-decisions.md
    §44's motivating scenario)."""
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "refs/remotes/origin/main"], cwd=repo, check=True)
    commit_fn(repo)
    new_tip = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", new_tip], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", branch], cwd=repo, check=True)
    subprocess.run(["git", "rebase", "-q", "origin/main"], cwd=repo, check=True)


def _commit_unrelated_file(repo) -> None:
    """Add a file the PR commit never touches -- an origin/main advance
    with zero overlap with the reviewed diff."""
    (repo / "unrelated.txt").write_text("unrelated base change\n")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "unrelated base change"], cwd=repo, check=True)


CONTEXT_DRIFT_LINE_COUNT = 10


def _shared_txt_lines(extra: str = "") -> str:
    """N numbered lines plus optional extra content -- shared by
    context_diff_repo's setup and _insert_adjacent_line below, so both
    sides build shared.txt from the identical line set."""
    return "\n".join(f"L{i}" for i in range(1, CONTEXT_DRIFT_LINE_COUNT + 1)) + "\n" + extra


def _insert_adjacent_line(repo) -> None:
    """Insert a line inside shared.txt's last few lines -- close enough to
    the PR commit's own appended line (see context_diff_repo) to fall
    inside git diff's default 3-line context window, so a conflict-free
    rebase still shifts that hunk's context lines."""
    lines = [f"L{i}" for i in range(1, CONTEXT_DRIFT_LINE_COUNT)]
    lines.append(f"L{CONTEXT_DRIFT_LINE_COUNT - 1}.5")
    lines.append(f"L{CONTEXT_DRIFT_LINE_COUNT}")
    (repo / "shared.txt").write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "insert adjacent line"], cwd=repo, check=True)


@pytest.fixture
def context_diff_repo(git_repo):
    """git_repo plus a committed multi-line shared.txt, origin/main pointed
    at that commit, and a PR commit that appends one line to shared.txt's
    end -- unlike cumulative_diff_repo's brand-new second.txt, this gives
    the PR's diff hunk real context lines that a base-branch edit can
    later shift. Local to this file for the same reason as
    cumulative_diff_repo above."""
    repo = git_repo
    (repo / "shared.txt").write_text(_shared_txt_lines())
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add shared.txt"], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
    )
    (repo / "shared.txt").write_text(
        _shared_txt_lines(f"L{CONTEXT_DRIFT_LINE_COUNT + 1}\n")
    )
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"append L{CONTEXT_DRIFT_LINE_COUNT + 1}"], cwd=repo, check=True)
    return repo


# Every write/activate/deactivate/status subcommand marker.sh dispatches.
# Shared by TestMarkerScriptSessionMissing and TestMarkerScriptSessionIdValidation
# so a subcommand added to the dispatch but only one of the two parametrize
# lists can't silently narrow coverage on the other guard.
ALL_MARKER_SUBCOMMAND_ARGS = [
    ["write", "code-review"],
    ["write", "skill-review"],
    ["write", "plan-review"],
    ["write", "ready-for-review"],
    ["write", "cumulative-review"],
    ["activate", "plan-review"],
    ["activate", "ready-for-review"],
    ["activate", "respond-pr"],
    ["activate", "memory-skill"],
    ["activate", "handoff"],
    ["deactivate", "plan-review"],
    ["deactivate", "ready-for-review"],
    ["deactivate", "respond-pr"],
    ["deactivate", "memory-skill"],
    ["deactivate", "handoff"],
    ["status"],
]


def _run(
    args: list[str], cwd, home, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(MARKER_SCRIPT)] + args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


class TestMarkerScriptSessionMissing:
    """When the session file ($HOME/.claude/sessions/$PPID) does not exist,
    every write/activate/deactivate subcommand must exit 2 and write nothing.
    This guards against the silent-empty-suffix regression: without explicit
    || exit 2 on the command-substitution call sites, the return 2 inside
    _resolve_session_id() only sets the subshell exit status — the parent
    continues and writes a malformed marker with an empty session-id
    suffix."""

    @pytest.mark.parametrize("args", ALL_MARKER_SUBCOMMAND_ARGS)
    def test_exits_2_when_session_file_missing(self, isolated_home, git_repo, args):
        result = _run(args, cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, (
            f"marker.sh {' '.join(args)} should exit 2 when session file is absent, "
            f"got {result.returncode}. stderr: {result.stderr!r}"
        )

    def test_no_marker_written_when_session_file_missing(self, isolated_home, git_repo):
        _run(["write", "code-review"], cwd=git_repo, home=isolated_home)
        marker_dir = isolated_home / ".claude" / "code-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], (
            f"marker.sh wrote a stray marker when session file was absent: {stray}"
        )


class TestMarkerScriptSessionIdValidation:
    """The session file's CONTENT — not just its presence — feeds every
    write/activate/deactivate path as a filesystem path component. A session
    file holding a value like '../canary' (e.g. corrupted, or a hostile
    subagent racing to write its own sessions/<pid> entry) must be rejected
    by the same _resolve_session_id() chokepoint that handles a missing
    session file, rather than flowing into `rm -f`/`>` against a path outside
    the marker/active directories."""

    @pytest.mark.parametrize("args", ALL_MARKER_SUBCOMMAND_ARGS)
    def test_exits_2_when_session_id_is_path_escaping(self, isolated_home, git_repo, args):
        _seed_session(isolated_home, TRAVERSAL_SESSION_ID)
        result = _run(args, cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, (
            f"marker.sh {' '.join(args)} should exit 2 for a path-escaping "
            f"session id, got {result.returncode}. stderr: {result.stderr!r}"
        )

    def test_no_marker_written_for_path_escaping_session_id(self, isolated_home, git_repo):
        _seed_session(isolated_home, TRAVERSAL_SESSION_ID)
        canary = plant_traversal_canary(isolated_home)

        _run(["write", "code-review"], cwd=git_repo, home=isolated_home)

        marker_dir = isolated_home / ".claude" / "code-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], (
            f"marker.sh wrote a stray marker for a path-escaping session id: {stray}"
        )
        assert canary.read_text() == CANARY_CONTENT, (
            "a path-escaping session id must not let 'write' touch a file "
            "outside the markers directory"
        )

    def test_no_active_marker_written_for_path_escaping_session_id(
        self, isolated_home, git_repo
    ):
        _seed_session(isolated_home, TRAVERSAL_SESSION_ID)
        canary = plant_traversal_canary(isolated_home)

        _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)

        active_dir = isolated_home / ".claude" / ".plan-review-active.d"
        stray = list(active_dir.iterdir()) if active_dir.exists() else []
        assert stray == [], (
            f"marker.sh wrote a stray active marker for a path-escaping session id: {stray}"
        )
        assert canary.read_text() == CANARY_CONTENT


class TestMarkerScriptHappyPath:
    """Smoke-test that each subcommand writes/removes the expected file when
    the session file is present."""

    SID = "test-session-abc"

    def test_write_code_review_creates_marker(self, isolated_home, git_repo):
        sid = self.SID
        _seed_session(isolated_home, sid)
        result = _run(["write", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        marker_dir = isolated_home / ".claude" / "code-review-markers"
        files = list(marker_dir.iterdir())
        assert len(files) == 1
        assert files[0].name.endswith(f".{sid}")

    def test_write_plan_review_stores_hash_not_literal(self, isolated_home, git_repo):
        """write plan-review must store _lib_active_plan_hash's output (a
        sha256 hex digest of the active plan set), not the legacy literal
        'reviewed' existence-only sentinel."""
        _seed_session(isolated_home, self.SID)
        plans_dir = git_repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "p.md").write_text("# plan\n")
        result = _run(["write", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        marker_dir = isolated_home / ".claude" / "plan-review-markers"
        files = list(marker_dir.iterdir())
        assert len(files) == 1
        content = files[0].read_text().strip()
        assert content != "reviewed"
        assert re.fullmatch(r"[0-9a-f]{64}", content), (
            f"expected a sha256 hex digest, got {content!r}"
        )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_write_plan_review_aborts_without_clobbering_marker(self, isolated_home, git_repo):
        """An unhashable active plan must abort the write with a non-zero
        status naming the file -- and must leave any pre-existing marker
        byte-identical. Redirecting the helper's output straight into the
        marker path would truncate it before the failure surfaced, so a
        failed attempt would destroy a good marker as a side effect."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        plans_dir = git_repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        plan = plans_dir / "p.md"
        plan.write_text("# plan\n")

        assert _run(["write", "plan-review"], cwd=git_repo, home=isolated_home).returncode == 0
        marker = isolated_home / ".claude" / "plan-review-markers" / next(
            f.name for f in (isolated_home / ".claude" / "plan-review-markers").iterdir()
        )
        good_content = marker.read_text()
        assert marker.name.endswith(f".{sid}")

        plan.chmod(0o000)
        try:
            result = _run(["write", "plan-review"], cwd=git_repo, home=isolated_home)
        finally:
            plan.chmod(0o644)

        assert result.returncode == 2, result.stderr
        assert "p.md" in result.stderr, (
            f"stderr must name the offending plan file, got {result.stderr!r}"
        )
        assert marker.read_text() == good_content, (
            "a failed write must not truncate or alter the existing marker"
        )

    def test_activate_creates_active_marker_with_pid(self, isolated_home, git_repo):
        """activate must write the Claude session PID to the active.d file body
        so hooks can check liveness with kill -0."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        active_file = isolated_home / ".claude" / ".plan-review-active.d" / sid
        assert active_file.exists()
        content = active_file.read_text().strip()
        assert content.isdigit(), (
            f"activate must write a numeric PID to the active marker, got: {content!r}"
        )
        stored_pid = int(content)
        assert stored_pid > 0, f"stored PID must be positive, got: {stored_pid}"

    def test_activate_ready_for_review_writes_pid(self, isolated_home, git_repo):
        """activate ready-for-review writes the Claude session PID (same as
        other skills) — hook now uses PID liveness, not epoch timestamp."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        result = _run(["activate", "ready-for-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        active_file = isolated_home / ".claude" / ".ready-for-review-active.d" / sid
        assert active_file.exists()
        content = active_file.read_text().strip()
        assert content.isdigit(), (
            f"activate ready-for-review must write a numeric PID, got: {content!r}"
        )

    def test_deactivate_removes_active_marker(self, isolated_home, git_repo):
        sid = self.SID
        _seed_session(isolated_home, sid)
        active_dir = isolated_home / ".claude" / ".plan-review-active.d"
        active_dir.mkdir(parents=True)
        (active_dir / sid).touch()
        result = _run(["deactivate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert not (active_dir / sid).exists()

    def test_activate_memory_skill_creates_active_marker(self, isolated_home, git_repo):
        sid = self.SID
        _seed_session(isolated_home, sid)
        result = _run(["activate", "memory-skill"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        active_file = isolated_home / ".claude" / ".memory-skill-active.d" / sid
        assert active_file.exists()

    def test_deactivate_memory_skill_removes_active_marker(self, isolated_home, git_repo):
        sid = self.SID
        _seed_session(isolated_home, sid)
        active_dir = isolated_home / ".claude" / ".memory-skill-active.d"
        active_dir.mkdir(parents=True)
        (active_dir / sid).touch()
        result = _run(["deactivate", "memory-skill"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert not (active_dir / sid).exists()

    def test_activate_handoff_creates_active_marker(self, isolated_home, git_repo):
        sid = self.SID
        _seed_session(isolated_home, sid)
        result = _run(["activate", "handoff"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        active_file = isolated_home / ".claude" / ".handoff-active.d" / sid
        assert active_file.exists()

    def test_deactivate_handoff_removes_active_marker(self, isolated_home, git_repo):
        sid = self.SID
        _seed_session(isolated_home, sid)
        active_dir = isolated_home / ".claude" / ".handoff-active.d"
        active_dir.mkdir(parents=True)
        (active_dir / sid).touch()
        result = _run(["deactivate", "handoff"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert not (active_dir / sid).exists()

    def test_help_exits_0(self, isolated_home, git_repo):
        result = _run(["--help"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0


class TestMarkerScriptClearStale:
    """Tests for `marker.sh clear-stale [--dry-run]`."""

    def _make_active_dir(self, home, skill):
        d = home / ".claude" / f".{skill}-active.d"
        d.mkdir(parents=True)
        return d

    def test_clear_stale_removes_dead_pid_entry(self, isolated_home, git_repo):
        """clear-stale evicts entries whose stored PID is dead."""
        d = self._make_active_dir(isolated_home, "plan-review")
        orphan = d / "orphan-session"
        orphan.write_text("99999999")
        result = _run(["clear-stale"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert not orphan.exists(), "clear-stale must remove dead-PID marker"
        assert "evict" in result.stdout

    def test_clear_stale_keeps_alive_pid_entry(self, isolated_home, git_repo):
        """clear-stale preserves entries whose stored PID is alive."""
        d = self._make_active_dir(isolated_home, "respond-pr")
        alive = d / "live-session"
        alive.write_text(str(os.getpid()))
        result = _run(["clear-stale"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert alive.exists(), "clear-stale must not remove alive-PID marker"

    def test_clear_stale_evicts_idle_expired_alive_pid_entry(self, isolated_home, git_repo):
        """clear-stale must apply the same idle-window check as the gate
        hooks' own liveness predicate: an alive PID whose mtime has aged past
        60 minutes is stale everywhere else too. Reporting it as "keep" here
        would leave a marker in place that every other surface already
        treats as evicted, misleading an operator running this tool's own
        recovery procedure."""
        d = self._make_active_dir(isolated_home, "plan-review")
        idle = d / "idle-session"
        idle.write_text(str(os.getpid()))
        stale_time = time.time() - 3700  # just past the 60-minute idle window
        os.utime(idle, (stale_time, stale_time))
        result = _run(["clear-stale"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert not idle.exists(), "clear-stale must evict an idle-expired alive-PID marker"
        assert "idle timeout" in result.stdout

    def test_clear_stale_dry_run_does_not_remove(self, isolated_home, git_repo):
        """--dry-run reports would-evict entries without removing them."""
        d = self._make_active_dir(isolated_home, "memory-skill")
        orphan = d / "dry-run-session"
        orphan.write_text("99999999")
        result = _run(["clear-stale", "--dry-run"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert orphan.exists(), "--dry-run must not remove markers"
        assert "dry-run" in result.stdout or "would evict" in result.stdout

    def test_clear_stale_no_active_dirs_exits_0(self, isolated_home, git_repo):
        """When no active.d directories exist, exits 0 with zero evictions."""
        result = _run(["clear-stale"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0
        assert "evicted 0" in result.stdout

    def test_clear_stale_invalid_extra_arg_exits_2(self, isolated_home, git_repo):
        result = _run(["clear-stale", "--unknown-flag"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2


class TestMarkerScriptEmptyStagedGuard:
    """Guard added to hash-based marker writes (code-review, skill-review):
    exit 2 when the staged diff for the relevant pathspec is empty but
    unstaged tracked changes exist — prevents recording an empty-hash marker
    before `git add` is run, which would cause a require-* hook mismatch at
    commit time."""

    SID = "test-session-guard"

    # ── code-review (whole-tree pathspec) ─────────────────────────────────

    def test_code_review_staged_and_unstaged_writes_marker(
        self, isolated_home, git_repo
    ):
        """Guard must NOT fire when something is staged — even with unstaged
        changes alongside it."""
        _seed_session(isolated_home, self.SID)
        # Fixture has file.txt staged with "first\nsecond\n". Write an
        # additional working-tree change without staging it — index has a
        # staged change, working tree has a further unstaged change.
        (git_repo / "file.txt").write_text("first\nsecond\nthird\n")
        result = _run(["write", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        marker_dir = isolated_home / ".claude" / "code-review-markers"
        assert len(list(marker_dir.iterdir())) == 1

    def test_code_review_empty_staged_unstaged_tracked_exits_2(
        self, isolated_home, git_repo
    ):
        """Guard fires: staged diff is empty, unstaged tracked changes exist."""
        _seed_session(isolated_home, self.SID)
        # Unstage the fixture's pre-staged change, leaving it as an unstaged
        # tracked modification.
        subprocess.run(["git", "reset", "HEAD", "--", "file.txt"], cwd=git_repo, check=True)
        result = _run(["write", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2
        assert "git add" in result.stderr
        assert "/code-review" in result.stderr
        marker_dir = isolated_home / ".claude" / "code-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"guard should not write a marker: {stray}"

    def test_code_review_empty_staged_no_unstaged_exits_0_without_marker(
        self, isolated_home, git_repo
    ):
        """Guard must NOT fire when staged is empty AND there are no unstaged
        changes — the review-of-nothing escape hatch must stay open. A
        genuinely clean tree makes `git diff --cached` produce zero bytes.
        The write arm must still exit 0 without writing a marker. It must
        not record sha256("")'s fixed-width digest as though it hashed real
        content."""
        _seed_session(isolated_home, self.SID)
        # Unstage then discard the fixture's change so both index and working
        # tree are clean. Order matters: reset the index first (HEAD → index),
        # then checkout to align the working tree with the now-clean index.
        subprocess.run(["git", "reset", "HEAD", "--", "file.txt"], cwd=git_repo, check=True)
        subprocess.run(["git", "checkout", "--", "file.txt"], cwd=git_repo, check=True)
        result = _run(["write", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "staged diff is empty" in result.stderr
        marker_dir = isolated_home / ".claude" / "code-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"an empty staged diff must not write a marker: {stray}"

    # ── skill-review (path-scoped to SKILL.md) ────────────────────────────

    def _make_skill_md(self, repo):
        """Create a tracked SKILL.md inside the repo at the expected pathspec."""
        skill_dir = repo / "claude-skills" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("# test skill\n")
        subprocess.run(["git", "add", str(skill_md)], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add test skill"],
            cwd=repo,
            check=True,
        )
        return skill_md

    def test_skill_review_out_of_scope_unstaged_does_not_fire(
        self, isolated_home, git_repo
    ):
        """Unstaged change outside the SKILL.md pathspec with nothing staged
        must NOT trigger the guard — the guard is pathspec-scoped. But the
        SKILL.md-scoped diff is genuinely empty here (no SKILL.md exists at
        all), so the write arm exits 0 without writing a marker, not the
        guard firing."""
        _seed_session(isolated_home, self.SID)
        # file.txt is staged from the fixture; reset it so staged is empty for
        # the whole tree. The unstaged file.txt change is outside the SKILL.md
        # pathspec — guard must pass through.
        subprocess.run(["git", "reset", "HEAD", "--", "file.txt"], cwd=git_repo, check=True)
        result = _run(["write", "skill-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        marker_dir = isolated_home / ".claude" / "skill-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"an empty SKILL.md-scoped diff must not write a marker: {stray}"

    def test_skill_review_unstaged_skill_md_exits_2(self, isolated_home, git_repo):
        """Guard fires when staged SKILL.md diff is empty but an unstaged
        SKILL.md change exists."""
        _seed_session(isolated_home, self.SID)
        skill_md = self._make_skill_md(git_repo)
        # Modify SKILL.md without staging it.
        skill_md.write_text("# test skill\nmodified\n")
        result = _run(["write", "skill-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2
        assert "git add" in result.stderr
        assert "/skill-review" in result.stderr
        marker_dir = isolated_home / ".claude" / "skill-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"guard should not write a marker: {stray}"

    def test_skill_review_staged_skill_md_writes_marker(self, isolated_home, git_repo):
        """Guard must NOT fire when SKILL.md change is staged."""
        _seed_session(isolated_home, self.SID)
        skill_md = self._make_skill_md(git_repo)
        skill_md.write_text("# test skill\nupdated\n")
        subprocess.run(["git", "add", str(skill_md)], cwd=git_repo, check=True)
        result = _run(["write", "skill-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        marker_dir = isolated_home / ".claude" / "skill-review-markers"
        assert len(list(marker_dir.iterdir())) == 1

    def _make_routing_md_like_file(self, repo, skill_name="other-skill"):
        """Create a tracked ROUTING.md outside plan-review — same filename,
        different skill directory, so it is out of scope for the hardcoded
        `claude-skills/skills/plan-review/ROUTING.md` pathspec."""
        skill_dir = repo / "claude-skills" / "skills" / skill_name
        skill_dir.mkdir(parents=True)
        routing_md = skill_dir / "ROUTING.md"
        routing_md.write_text("# not the gated ROUTING.md\n")
        subprocess.run(["git", "add", str(routing_md)], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add out-of-scope ROUTING.md"],
            cwd=repo,
            check=True,
        )
        return routing_md

    def _make_plan_review_routing_md(self, repo):
        """Create a tracked plan-review/ROUTING.md — the exact hardcoded pathspec."""
        routing_dir = repo / "claude-skills" / "skills" / "plan-review"
        routing_dir.mkdir(parents=True, exist_ok=True)
        routing_md = routing_dir / "ROUTING.md"
        routing_md.write_text("# test routing\n")
        subprocess.run(["git", "add", str(routing_md)], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add test routing"],
            cwd=repo,
            check=True,
        )
        return routing_md

    def test_skill_review_out_of_scope_routing_md_like_file_does_not_fire(
        self, isolated_home, git_repo
    ):
        """An unstaged change to a ROUTING.md-*named* file outside
        plan-review is out of scope for the hardcoded
        `claude-skills/skills/plan-review/ROUTING.md` pathspec — the guard
        must not fire, proving the pathspec is the exact path, not a
        generic `**/ROUTING.md` glob. But the pathspec-scoped diff is
        genuinely empty here, so the write arm exits 0 without writing a
        marker, not the guard firing."""
        _seed_session(isolated_home, self.SID)
        # file.txt is staged from the fixture; reset it so staged is empty for
        # the whole tree, matching the sibling out-of-scope test's setup.
        subprocess.run(["git", "reset", "HEAD", "--", "file.txt"], cwd=git_repo, check=True)
        routing_md = self._make_routing_md_like_file(git_repo)
        # Unstaged, out-of-scope modification.
        routing_md.write_text("# not the gated ROUTING.md\nmodified\n")
        result = _run(["write", "skill-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        marker_dir = isolated_home / ".claude" / "skill-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"an empty pathspec-scoped diff must not write a marker: {stray}"

    def test_skill_review_unstaged_routing_md_exits_2(self, isolated_home, git_repo):
        """Guard fires when the staged plan-review/ROUTING.md diff is empty
        but an unstaged ROUTING.md change exists."""
        _seed_session(isolated_home, self.SID)
        routing_md = self._make_plan_review_routing_md(git_repo)
        # Modify ROUTING.md without staging it.
        routing_md.write_text("# test routing\nmodified\n")
        result = _run(["write", "skill-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2
        assert "git add" in result.stderr
        assert "/skill-review" in result.stderr
        marker_dir = isolated_home / ".claude" / "skill-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"guard should not write a marker: {stray}"


_HASH_STAGED_DIFF_FIXTURE_START = "# MARKER_TEST_FIXTURE: hash-staged-diff — start\n"
_HASH_STAGED_DIFF_FIXTURE_END = "# MARKER_TEST_FIXTURE: hash-staged-diff — end"


def _extract_hash_staged_diff_block() -> str:
    """Return _hash_staged_diff's two readonly constants plus its own body
    from marker.sh, delimited by explicit marker comments rather than
    located by matching shell syntax -- same rationale as
    test_install_sh_transcript_config_dirs.py's _extract_block: marker.sh's
    own CLI dispatch runs unconditionally when sourced (no BASH_SOURCE
    guard), so sourcing the whole file would hit its subcommand `case` and
    exit before a test ever got to call the function directly."""
    marker_text = MARKER_SCRIPT.read_text()
    start = marker_text.find(_HASH_STAGED_DIFF_FIXTURE_START)
    assert start != -1, f"{_HASH_STAGED_DIFF_FIXTURE_START!r} not found in {MARKER_SCRIPT}"
    end = marker_text.find(_HASH_STAGED_DIFF_FIXTURE_END, start)
    assert end != -1, f"{_HASH_STAGED_DIFF_FIXTURE_END!r} not found after start marker in {MARKER_SCRIPT}"
    block = marker_text[start + len(_HASH_STAGED_DIFF_FIXTURE_START) : end]
    assert "_hash_staged_diff()" in block, (
        f"extracted block is missing the function definition; markers in "
        f"{MARKER_SCRIPT} are probably misplaced. Got: {block!r}"
    )
    return block


def _run_hash_staged_diff(
    repo_root: str, cap_mode: str = "uncapped", *pathspecs: str, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """Runs _hash_staged_diff directly, uncapped by default so no `_lib.sh`
    source (for `_lib_capped`) is needed -- cap-mode behavior itself is
    already covered by test_code_review_value_computation_times_out_gracefully."""
    env = {**os.environ, **extra_env} if extra_env else dict(os.environ)
    script = _extract_hash_staged_diff_block() + '\n_hash_staged_diff "$@"\n'
    return subprocess.run(
        ["bash", "-c", script, "bash", cap_mode, repo_root, *pathspecs],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


class TestHashStagedDiff:
    """_hash_staged_diff's three return states -- 0 (content, hash on
    stdout), $_HASH_STAGED_DIFF_RC_EMPTY (empty diff, nothing on stdout),
    and the bare 1 (git itself could not be trusted, nothing on stdout) --
    classified from the hashed digest itself rather than a separate probe.
    The row-9 regression coverage: the four non-empty-diff categories
    (mode-only change, rename, binary file, a no-op GIT_EXTERNAL_DIFF) must
    still classify as content, not empty."""

    _RC_EMPTY = 3

    def _init_repo(self, repo) -> None:
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "f.txt").write_text("first\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    def test_content_when_something_is_staged(self, tmp_path) -> None:
        repo = tmp_path / "content-repo"
        self._init_repo(repo)
        (repo / "f.txt").write_text("first\nsecond\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        result = _run_hash_staged_diff(str(repo))
        assert result.returncode == 0, result.stderr
        assert result.stdout == hashlib.sha256(
            subprocess.run(
                ["git", "-C", str(repo), "diff", "--cached"], capture_output=True, check=True
            ).stdout
        ).hexdigest()

    def test_empty_return_code_when_nothing_is_staged(self, tmp_path) -> None:
        repo = tmp_path / "empty-repo"
        self._init_repo(repo)
        result = _run_hash_staged_diff(str(repo))
        assert result.returncode == self._RC_EMPTY, result.stderr
        assert result.stdout == ""

    def test_bare_failure_return_code_for_a_non_repo_root(self, tmp_path) -> None:
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        result = _run_hash_staged_diff(str(not_a_repo))
        assert result.returncode == 1, result.stderr
        assert result.stdout == ""

    def test_mode_only_staged_change_classifies_as_content(self, tmp_path) -> None:
        """A mode-only staged change (chmod +x, no content change) still
        hashes non-empty `git diff --cached` output, so this must return 0,
        not the empty rc."""
        repo = tmp_path / "mode-only-repo"
        self._init_repo(repo)
        os.chmod(repo / "f.txt", 0o755)
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        result = _run_hash_staged_diff(str(repo))
        assert result.returncode == 0, result.stderr
        assert result.stdout != ""

    def test_staged_rename_classifies_as_content(self, tmp_path) -> None:
        repo = tmp_path / "rename-repo"
        self._init_repo(repo)
        subprocess.run(["git", "mv", "f.txt", "renamed.txt"], cwd=repo, check=True)
        result = _run_hash_staged_diff(str(repo))
        assert result.returncode == 0, result.stderr
        assert result.stdout != ""

    def test_staged_binary_file_classifies_as_content(self, tmp_path) -> None:
        repo = tmp_path / "binary-repo"
        self._init_repo(repo)
        (repo / "binary.dat").write_bytes(bytes(range(256)))
        subprocess.run(["git", "add", "binary.dat"], cwd=repo, check=True)
        result = _run_hash_staged_diff(str(repo))
        assert result.returncode == 0, result.stderr
        assert result.stdout != ""

    def test_git_external_diff_noop_misclassifies_staged_content_as_empty_rc(self, tmp_path) -> None:
        """A GIT_EXTERNAL_DIFF tool that exits 0 without writing to stdout
        makes `git diff --cached` (no `--quiet`) itself hash empty bytes for
        a genuinely staged change. `git diff --cached --quiet` never invokes
        an external-diff driver at all, so `_hash_staged_diff`'s own hashed
        call is exactly the one that GIT_EXTERNAL_DIFF can affect. A real
        external-diff helper always writes its own diff text to stdout,
        precisely so tools that hash or display it see real content. This
        test asserts against a deliberately broken no-op helper, not a
        realistic one."""
        repo = tmp_path / "external-diff-repo"
        self._init_repo(repo)
        (repo / "f.txt").write_text("first\nsecond\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        noop_script = tmp_path / "noop-external-diff.sh"
        noop_script.write_text("#!/bin/bash\nexit 0\n")
        noop_script.chmod(0o755)
        result = _run_hash_staged_diff(
            str(repo), "uncapped", extra_env={"GIT_EXTERNAL_DIFF": str(noop_script)}
        )
        assert result.returncode == self._RC_EMPTY, (
            "a no-op GIT_EXTERNAL_DIFF helper makes git diff --cached itself "
            "produce zero bytes for genuinely staged content -- this IS a "
            "known, accepted residual (see the plan's row-9 discussion), not "
            "a bug this test is pinning as correct; if this assertion ever "
            "flips to 0, the accepted-residual writeup needs revisiting, not "
            "just this test"
        )


class TestMarkerScriptStagedDiffStateClassification:
    """_hash_staged_diff's failure outcome (bare rc 1, "git itself could
    not be trusted") and the status arm's degradation to absent/historical
    on that outcome are exercised here through marker.sh's write and
    status arms. _hash_staged_diff's own three-way return contract
    (content/empty/failure), including the four non-empty-diff categories
    (mode-only change, rename, binary file, a no-op GIT_EXTERNAL_DIFF), is
    covered directly against its own return value by this file's
    TestHashStagedDiff. test_write_code_review_creates_marker in
    TestMarkerScriptHappyPath already confirms the write arm wires a
    "content" classification through to a written marker. No separate
    wiring check for the other content-yielding shapes is kept here."""

    SID = "test-session-diff-state"

    # ── the probe's own cap, exercised on the write arm ─────────────────

    def test_write_code_review_aborts_when_diff_state_is_unknown(
        self, isolated_home, git_repo, tmp_path
    ):
        """The write arm now calls _hash_staged_diff uncapped directly, with
        no separate probe call ahead of it -- a sleeping stub would hang the
        write arm forever instead of exercising the failure path, so a
        nonzero exit is required here, not a timeout. _guard_staged_vs_unstaged
        runs its own `--quiet`-shaped diff calls first, at a different
        argument count, so the stub only intercepts the load-bearing 4-arg
        (`-C <repo> diff --cached`, no pathspec) hash call the write arm
        actually makes."""
        real_git = shutil.which("git")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text(
            '#!/bin/bash\n'
            'if [ "$3" = "diff" ] && [ "$4" = "--cached" ] && [ "$#" -eq 4 ]; then\n'
            '  exit 1\n'
            'fi\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        _seed_session(isolated_home, self.SID)
        result = _run(
            ["write", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )

        assert result.returncode == 2, result.stderr
        marker_dir = isolated_home / ".claude" / "code-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"an unanswerable diff state must not write a marker: {stray}"

    def test_write_skill_review_aborts_when_diff_state_is_unknown(
        self, isolated_home, git_repo, tmp_path
    ):
        """Same as the code-review case above, retargeted at the
        skill-review arm's pathspec-scoped (9-arg, 4 pathspecs) hash call."""
        real_git = shutil.which("git")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text(
            '#!/bin/bash\n'
            'if [ "$3" = "diff" ] && [ "$4" = "--cached" ] && [ "$#" -eq 9 ]; then\n'
            '  exit 1\n'
            'fi\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        _seed_session(isolated_home, self.SID)
        result = _run(
            ["write", "skill-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )

        assert result.returncode == 2, result.stderr
        marker_dir = isolated_home / ".claude" / "skill-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"an unanswerable diff state must not write a marker: {stray}"

    # ── status degrades to absent/historical, not a crash ───────────────

    def test_status_code_review_falls_through_to_absent_when_diff_state_is_unknown(
        self, isolated_home, git_repo, tmp_path, gh_timeout_shim
    ):
        """status calls _hash_staged_diff capped directly -- no separate
        probe call remains to intercept. Uses the same nonzero-exit stub
        shape as the write-arm tests above, for consistency between the
        two arms' tests, even though this capped call could also tolerate
        a sleep+cap stub; cap-engagement itself is already covered
        generically by test_code_review_value_computation_times_out_gracefully.

        status also computes a cumulative-review line via
        _lib_cumulative_diff_hash, which shells out to `gh pr view`; the
        gh_timeout_shim stub keeps that call off the real, network- and
        auth-dependent `gh`."""
        real_git = shutil.which("git")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text(
            '#!/bin/bash\n'
            'if [ "$3" = "diff" ] && [ "$4" = "--cached" ] && [ "$#" -eq 4 ]; then\n'
            '  exit 1\n'
            'fi\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        _seed_session(isolated_home, self.SID)
        env = gh_timeout_shim(
            '[ "$1" = "pr" ] && [ "$2" = "view" ]', fake_output="main", sleep_seconds=0
        )
        env["PATH"] = f"{stub_dir}:{env['PATH']}"
        result = _run(
            ["status"],
            cwd=git_repo,
            home=isolated_home,
            extra_env=env,
        )

        assert result.returncode == 0, result.stderr
        assert "code-review: absent" in result.stdout

    def test_status_skill_review_falls_through_to_absent_when_diff_state_is_unknown(
        self, isolated_home, git_repo, tmp_path, gh_timeout_shim
    ):
        """Same as the code-review case above, but matches the
        skill-review line's 4-pathspec (9-arg) hash call shape
        specifically, so it doesn't also fail the code-review line's own
        hash call.

        status also computes a cumulative-review line via
        _lib_cumulative_diff_hash, which shells out to `gh pr view`; the
        gh_timeout_shim stub keeps that call off the real, network- and
        auth-dependent `gh`."""
        real_git = shutil.which("git")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text(
            '#!/bin/bash\n'
            'if [ "$3" = "diff" ] && [ "$4" = "--cached" ] && [ "$#" -eq 9 ]; then\n'
            '  exit 1\n'
            'fi\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        _seed_session(isolated_home, self.SID)
        env = gh_timeout_shim(
            '[ "$1" = "pr" ] && [ "$2" = "view" ]', fake_output="main", sleep_seconds=0
        )
        env["PATH"] = f"{stub_dir}:{env['PATH']}"
        result = _run(
            ["status"],
            cwd=git_repo,
            home=isolated_home,
            extra_env=env,
        )

        assert result.returncode == 0, result.stderr
        assert "skill-review: absent" in result.stdout

    # ── a stray empty-digest marker must not read live ──────────────────

    def test_status_empty_digest_marker_is_not_live_on_clean_tree(
        self, isolated_home, git_repo
    ):
        """A completion marker seeded with sha256("")'s digest must not read
        `live` once the tree is actually clean -- `status` computes no hash
        at all for the "empty" state, so it can never match this value."""
        _seed_session(isolated_home, self.SID)
        subprocess.run(["git", "reset", "HEAD", "--", "file.txt"], cwd=git_repo, check=True)
        subprocess.run(["git", "checkout", "--", "file.txt"], cwd=git_repo, check=True)
        empty_digest = hashlib.sha256(b"").hexdigest()
        write_marker(isolated_home, git_repo, empty_digest, session_id=self.SID)
        skill_marker = skill_review_marker_path(isolated_home, git_repo, session_id=self.SID)
        skill_marker.parent.mkdir(parents=True, exist_ok=True)
        skill_marker.write_text(empty_digest + "\n")

        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "code-review: live" not in result.stdout
        assert "skill-review: live" not in result.stdout


class TestMarkerScriptStalePidLookup:
    """Regression guard: `activate` must stamp the live Claude PID into the
    active-bypass marker even when ~/.claude/sessions/ holds stale entries
    from prior (crashed) sessions. The PID is resolved from the process
    ancestor walk, not by content-scanning the sessions directory, so stale
    files cannot mislead it."""

    SID = "test-session-stale"

    def test_activate_ignores_stale_session_entry(self, isolated_home, git_repo):
        """A stale sessions/ entry whose filename sorts lexically before the
        live PID — what the old reverse-lookup directory scan would have
        picked first — must not end up in the active marker."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        sessions_dir = isolated_home / ".claude" / "sessions"
        # Leading-zero filename: capture-session-id.sh only ever writes bare
        # PIDs, so a "0"-prefixed name is never a real entry, and it sorts
        # lexically before every str(os.getpid()) (which never starts with
        # "0"). The pre-fix directory scan would have stamped this into the
        # marker; the ancestor walk never reads it.
        stale = sessions_dir / "08888888"
        stale.write_text(sid)
        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        active_file = isolated_home / ".claude" / ".plan-review-active.d" / sid
        assert active_file.exists()
        content = active_file.read_text().strip()
        assert content == str(os.getpid()), (
            f"activate must stamp the live ancestor PID, not the stale "
            f"sessions entry; got {content!r}"
        )

    def test_write_arm_still_resolves_session_id(self, isolated_home, git_repo):
        """The refactored _resolve_session_id keeps its signature: a `write`
        arm (which needs only the session id) resolves and writes its
        marker with the correct session-id suffix."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        result = _run(["write", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        marker_dir = isolated_home / ".claude" / "code-review-markers"
        files = list(marker_dir.iterdir())
        assert len(files) == 1
        assert files[0].name.endswith(f".{sid}")


class TestMarkerDirectoryNamingConvention:
    """`write <skill>` must land its marker in ~/.claude/<skill>-markers/.

    A marker directory whose name does not derive mechanically from the skill
    name is unguessable: a session debugging a blocked commit infers the
    directory from the skill it just ran, looks in a path that does not exist,
    and reads the absent directory as a failed marker write.

    These assert on the directory marker.sh actually creates, not on the path
    literal in its source. Source-scanning would pass on a shadowed duplicate
    `case` arm (bash dispatches on the first match; a later arm carrying the
    right literal would satisfy a text scan while the live arm stayed broken)
    and would fail on a refactor to a shared "${SKILL}-markers" expansion that
    preserves the invariant.
    """

    WRITE_SKILLS = ("code-review", "skill-review", "plan-review", "ready-for-review", "cumulative-review")

    SID = "test-session-naming"

    @pytest.mark.parametrize("skill", WRITE_SKILLS)
    def test_write_lands_in_skill_derived_directory(
        self, skill, isolated_home, git_repo, tmp_path
    ):
        sid = self.SID
        _seed_session(isolated_home, sid)
        # plan-review hashes the active plan set; seeding one unconditionally
        # gives every arm the same preconditions so the loop stays uniform.
        plans_dir = git_repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / "p.md").write_text("# plan\n")
        if skill == "skill-review":
            # A staged SKILL.md-matching path: otherwise the pathspec-scoped
            # diff is empty and the write arm exits 0 without writing a
            # marker, which this test isn't exercising.
            skill_dir = git_repo / "claude-skills" / "skills" / "naming-convention-test-skill"
            skill_dir.mkdir(parents=True)
            skill_md = skill_dir / "SKILL.md"
            skill_md.write_text("# test skill\n")
            subprocess.run(["git", "add", str(skill_md)], cwd=git_repo, check=True)
        extra_env = None
        if skill == "cumulative-review":
            # cumulative-review's own diff recipe needs a resolvable default
            # branch -- see _arm_default_branch_ref_and_second_commit's
            # docstring for why this is armed per-test rather than on the
            # shared git_repo fixture. The write arm now reads a recorded
            # subject rather than resolving the diff itself, so record one
            # first, mirroring step 3's own routine.
            _arm_default_branch_ref_and_second_commit(git_repo)
            extra_env = _env_with_gh_shim(tmp_path, None)
            assert _record_subject(git_repo, isolated_home, extra_env).returncode == 0

        result = _run(["write", skill], cwd=git_repo, home=isolated_home, extra_env=extra_env)
        assert result.returncode == 0, result.stderr

        expected_dir = isolated_home / ".claude" / f"{skill}-markers"
        assert expected_dir.is_dir(), (
            f"`marker.sh write {skill}` must create ~/.claude/{skill}-markers/ "
            f"so a session can derive the directory from the skill name"
        )
        written = [f.name for f in expected_dir.iterdir()]
        assert len(written) == 1 and written[0].endswith(f".{sid}"), (
            f"expected one marker keyed <repo-hash>.{sid} in "
            f"{expected_dir.name}/, found {written}"
        )

    @pytest.mark.parametrize("skill", WRITE_SKILLS)
    def test_write_touches_no_other_marker_directory(
        self, skill, isolated_home, git_repo, tmp_path
    ):
        """The inverse guard: an arm that writes some other skill's directory
        (a copy-paste slip between adjacent `case` arms) would leave the
        assertion above green, since that arm's own directory is created too."""
        _seed_session(isolated_home, self.SID)
        plans_dir = git_repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / "p.md").write_text("# plan\n")
        extra_env = None
        if skill == "cumulative-review":
            _arm_default_branch_ref_and_second_commit(git_repo)
            extra_env = _env_with_gh_shim(tmp_path, None)
            assert _record_subject(git_repo, isolated_home, extra_env).returncode == 0

        assert (
            _run(["write", skill], cwd=git_repo, home=isolated_home, extra_env=extra_env).returncode == 0
        )

        # isolated_home pre-creates code-review-markers/, so presence alone
        # proves nothing — a stray is a *non-empty* foreign marker directory.
        strays = sorted(
            d.name
            for d in (isolated_home / ".claude").iterdir()
            if d.is_dir()
            and d.name.endswith("-markers")
            and d.name != f"{skill}-markers"
            and any(d.iterdir())
        )
        assert strays == [], (
            f"`marker.sh write {skill}` also wrote into {strays}; each write "
            f"arm must touch only its own marker directory"
        )

    def test_write_roster_is_closed_and_self_reported(self, isolated_home, git_repo):
        """Pin WRITE_SKILLS by execution rather than by reading marker.sh's
        source. Rejecting an unlisted skill proves the roster is closed — the
        parametrized guards above only prove each listed skill is accepted,
        which stays green if the dispatch grows an extra arm. Reading the
        roster back off the rejection message keeps the guards iterating the
        same set the script advertises at runtime."""
        result = _run(["write", "not-a-real-skill"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, (
            "`marker.sh write` must reject a skill outside its roster; got "
            f"exit {result.returncode}"
        )

        advertised = re.search(r"'write' supports: (.+?)\s*$", result.stderr, re.M)
        assert advertised, (
            f"rejection message no longer advertises the write roster: {result.stderr!r}"
        )
        assert tuple(s.strip() for s in advertised.group(1).split(",")) == self.WRITE_SKILLS, (
            f"marker.sh advertises write skills {advertised.group(1)!r}; the "
            f"convention guards iterate {self.WRITE_SKILLS} — reconcile the two"
        )

        strays = [
            d.name
            for d in (isolated_home / ".claude").iterdir()
            if d.is_dir() and d.name.endswith("-markers") and any(d.iterdir())
        ]
        assert strays == [], f"a rejected write still created markers in {strays}"


class TestMarkerWriteSatisfiesTheGate:
    """marker.sh (write side) and require-code-review.sh (read side) each
    build the marker path independently. Prove they agree by execution rather
    than by inspection: the rest of the hook suite seeds markers through a
    Python-side path helper, which only shows that the helper and the hook
    agree with each other — both could drift from marker.sh in lockstep and
    stay green."""

    def test_write_code_review_marker_opens_the_commit_gate(
        self, isolated_home, git_repo
    ):
        sid = "test-session-roundtrip"
        _seed_session(isolated_home, sid)

        result = _run(["write", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr

        assert (
            run_hook(
                HOOKS_DIR / "require-code-review.sh",
                bash_input("git commit -m roundtrip", session_id=sid),
                cwd=git_repo,
            )
            == "allow"
        )


def _build_conflicted_merge_via_origin(tmp_path):
    """Local copy of test_require_code_review.py's fixture of the same name
    (DAMP test code): a conflicted merge whose MERGE_HEAD is trusted via the
    origin/<default> anchor, resolved and staged."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    (clone / "f").write_text("ours-edit\n")
    subprocess.run(["git", "add", "f"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "ours edits f"], cwd=clone, check=True)
    push_conflicting_edit_to_origin(tmp_path, bare, "f", "origin-edit\n")
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    result = subprocess.run(
        ["git", "merge", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (clone / ".git" / "MERGE_HEAD").exists()
    (clone / "f").write_text("resolved\n")
    subprocess.run(["git", "add", "f"], cwd=clone, check=True)
    return clone


def _merge_tree_base(repo):
    """Local copy of test_require_code_review.py's helper of the same name:
    independently computes the reference tree _lib_gate_diff_base's merge
    row computes. The literal MERGE_HEAD OID (not the ref name) is passed --
    git embeds a merge-tree argument's own textual form into the conflict
    marker label, so the ref name would compute a byte-different tree than
    production's `merge-tree --write-tree HEAD "$state_oid"`."""
    merge_head_oid = (repo / ".git" / "MERGE_HEAD").read_text().strip()
    out = subprocess.run(
        ["git", "merge-tree", "--write-tree", "HEAD", merge_head_oid],
        cwd=repo, capture_output=True, text=True, check=False,
    ).stdout
    return out.strip().splitlines()[0]


class TestMarkerScriptMergeAwareBase:
    """marker.sh's `write code-review` and `status` arms thread
    _lib_gate_diff_base's resolved base through _lib_staged_diff_hash, the
    same base require-code-review.sh's read side resolves -- write and read
    must agree byte-for-byte or a marker written mid-merge can never match."""

    def test_write_code_review_marker_value_matches_independent_oracle(
        self, isolated_home, tmp_path
    ):
        repo = _build_conflicted_merge_via_origin(tmp_path)
        sid = "test-session-merge-write"
        _seed_session(isolated_home, sid)
        result = _run(["write", "code-review"], cwd=repo, home=isolated_home)
        assert result.returncode == 0, result.stderr

        marker_dir = isolated_home / ".claude" / "code-review-markers"
        files = list(marker_dir.iterdir())
        assert len(files) == 1
        base = _merge_tree_base(repo)
        expected = staged_diff_hash_at_base(repo, base)
        assert files[0].read_text().strip() == expected

    def test_write_code_review_marker_opens_the_commit_gate_mid_merge(
        self, isolated_home, tmp_path
    ):
        """Same round-trip proof as TestMarkerWriteSatisfiesTheGate above,
        against a mid-merge repo rather than a plain one -- write and read
        must agree on the base-relative recipe, not just the HEAD-relative
        one."""
        repo = _build_conflicted_merge_via_origin(tmp_path)
        sid = "test-session-merge-roundtrip"
        _seed_session(isolated_home, sid)

        result = _run(["write", "code-review"], cwd=repo, home=isolated_home)
        assert result.returncode == 0, result.stderr

        assert (
            run_hook(
                HOOKS_DIR / "require-code-review.sh",
                bash_input("git commit -m roundtrip", session_id=sid),
                cwd=repo,
            )
            == "allow"
        )

    def test_status_reports_live_mid_merge_for_base_relative_marker(
        self, isolated_home, tmp_path
    ):
        repo = _build_conflicted_merge_via_origin(tmp_path)
        sid = "test-session-merge-status"
        _seed_session(isolated_home, sid)
        base = _merge_tree_base(repo)
        write_marker(
            isolated_home, repo, staged_diff_hash_at_base(repo, base), session_id=sid
        )
        result = _run(["status"], cwd=repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "code-review: live" in result.stdout

    def test_status_reports_historical_mid_merge_for_old_head_relative_marker(
        self, isolated_home, tmp_path
    ):
        """A marker holding the plain HEAD-relative preimage must not read
        as live mid-merge -- it covers upstream's whole contribution, not
        just the resolution."""
        repo = _build_conflicted_merge_via_origin(tmp_path)
        sid = "test-session-merge-status-stale"
        _seed_session(isolated_home, sid)
        write_marker(isolated_home, repo, staged_diff_hash(repo), session_id=sid)
        result = _run(["status"], cwd=repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "code-review: historical" in result.stdout


def _build_conflicted_merge_via_origin_with_local_plan_edit(tmp_path):
    """Merge fixture with a plan file edited only locally, post-merge and
    unstaged -- upstream never touches it, so its merge-tree base content
    equals HEAD's, and diffing the local edit against either base exercises
    _lib_active_plan_hash's ordinary (non-empty active set) branch."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    plans_dir = clone / ".claude" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "p.md").write_text("base plan\n")
    subprocess.run(["git", "add", ".claude/plans/p.md"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "seed plan"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)

    (clone / "f").write_text("ours-edit\n")
    subprocess.run(["git", "add", "f"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "ours edits f"], cwd=clone, check=True)
    push_conflicting_edit_to_origin(tmp_path, bare, "f", "origin-edit\n")
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    result = subprocess.run(
        ["git", "merge", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (clone / ".git" / "MERGE_HEAD").exists()
    (clone / "f").write_text("resolved\n")
    subprocess.run(["git", "add", "f"], cwd=clone, check=True)
    (plans_dir / "p.md").write_text("locally edited plan\n")
    return clone


def _build_conflicted_merge_via_origin_with_upstream_plan_edit(tmp_path):
    """Merge fixture with a plan file edited only upstream, before the
    merge -- it auto-merges into the local worktree unchanged, so it reads
    as active relative to plain HEAD (upstream's whole contribution) but not
    relative to the trusted merge-tree base (already-reviewed content
    excluded). Used to prove a HEAD-relative marker goes stale mid-merge."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    plans_dir = clone / ".claude" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "p.md").write_text("base plan\n")
    subprocess.run(["git", "add", ".claude/plans/p.md"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "seed plan"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)

    (clone / "f").write_text("ours-edit\n")
    subprocess.run(["git", "add", "f"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "ours edits f"], cwd=clone, check=True)

    push_clone = tmp_path / "_push_upstream_plan_edit"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(push_clone)], check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=push_clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=push_clone, check=True)
    (push_clone / "f").write_text("origin-edit\n")
    (push_clone / ".claude" / "plans" / "p.md").write_text("upstream edited plan\n")
    subprocess.run(["git", "add", "f", ".claude/plans/p.md"], cwd=push_clone, check=True)
    subprocess.run(["git", "commit", "-qm", "origin edits f and p.md"], cwd=push_clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=push_clone, check=True)

    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    result = subprocess.run(
        ["git", "merge", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (clone / ".git" / "MERGE_HEAD").exists()
    (clone / "f").write_text("resolved\n")
    subprocess.run(["git", "add", "f"], cwd=clone, check=True)
    return clone


def _active_plan_hash_oracle(repo, active_files):
    """Independent, non-shell reimplementation of _lib_active_plan_hash's
    digest recipe (path, newline, per-file sha256, newline, concatenated
    across active files and re-hashed) for a caller-supplied active file
    list -- not derived by calling the function under test."""
    combined = ""
    for f in active_files:
        file_hash = hashlib.sha256((repo / f).read_bytes()).hexdigest()
        combined += f + "\n" + file_hash + "\n"
    return hashlib.sha256(combined.encode()).hexdigest()


class TestMarkerScriptMergeAwarePlanReviewBase:
    """marker.sh's `write plan-review` and `status` arms thread
    _lib_gate_diff_base's resolved base into _lib_active_plan_hash, the same
    base require-plan-review.sh's read side resolves -- write and read must
    agree byte-for-byte, or a plan-review marker written mid-merge can never
    match. Mirrors TestMarkerScriptMergeAwareBase above for the plan-review
    marker kind."""

    def test_write_plan_review_marker_value_matches_independent_oracle(
        self, isolated_home, tmp_path
    ):
        repo = _build_conflicted_merge_via_origin_with_local_plan_edit(tmp_path)
        sid = "test-session-merge-plan-write"
        _seed_session(isolated_home, sid)
        result = _run(["write", "plan-review"], cwd=repo, home=isolated_home)
        assert result.returncode == 0, result.stderr

        marker_dir = isolated_home / ".claude" / "plan-review-markers"
        files = list(marker_dir.iterdir())
        assert len(files) == 1
        expected = _active_plan_hash_oracle(repo, [".claude/plans/p.md"])
        assert files[0].read_text().strip() == expected

    def test_write_plan_review_marker_opens_the_write_gate_mid_merge(
        self, isolated_home, tmp_path
    ):
        """Same round-trip proof as TestMarkerScriptMergeAwareBase above,
        against the plan-review marker kind: write and read must agree on
        the base-relative recipe, not just the HEAD-relative one."""
        repo = _build_conflicted_merge_via_origin_with_local_plan_edit(tmp_path)
        sid = "test-session-merge-plan-roundtrip"
        _seed_session(isolated_home, sid)

        result = _run(["write", "plan-review"], cwd=repo, home=isolated_home)
        assert result.returncode == 0, result.stderr

        assert (
            run_hook(
                HOOKS_DIR / "require-plan-review.sh",
                {**edit_input(str(repo / "other.txt")), "session_id": sid},
                cwd=repo,
            )
            == "allow"
        )

    def test_status_reports_live_mid_merge_for_base_relative_plan_marker(
        self, isolated_home, tmp_path
    ):
        repo = _build_conflicted_merge_via_origin_with_local_plan_edit(tmp_path)
        sid = "test-session-merge-plan-status"
        _seed_session(isolated_home, sid)
        base = _merge_tree_base(repo)
        write_plan_review_marker(isolated_home, repo, sid, base=base)
        result = _run(["status"], cwd=repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "plan-review: live" in result.stdout

    def test_status_reports_historical_mid_merge_for_old_head_relative_plan_marker(
        self, isolated_home, tmp_path
    ):
        """A marker holding the plain HEAD-relative preimage must not read
        as live mid-merge -- it covers upstream's whole contribution to the
        plan file, not just the local resolution."""
        repo = _build_conflicted_merge_via_origin_with_upstream_plan_edit(tmp_path)
        sid = "test-session-merge-plan-status-stale"
        _seed_session(isolated_home, sid)
        write_plan_review_marker(isolated_home, repo, sid)
        result = _run(["status"], cwd=repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "plan-review: historical" in result.stdout


class TestMarkerScriptHonorsConfigDir:
    """CLAUDE_CONFIG_DIR relocates every marker path marker.sh writes, not
    just $HOME/.claude -- closes the cross-account bypass this plan targets."""

    def test_write_code_review_lands_under_config_dir(
        self, isolated_home, git_repo, tmp_path
    ):
        config_dir = tmp_path / "custom-config-dir"
        sessions_dir = config_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        sid = "test-session-config-dir"
        # Two-line format capture-session-id.sh writes: session id, then this
        # PID's TZ=UTC LC_ALL=C ps -o lstart= start time (see conftest.py's
        # _seed_session, which can't be reused here -- it targets the
        # standard $HOME/.claude/sessions path, not an arbitrary CONFIG_DIR).
        start_time = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())],
            env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip("\n")
        (sessions_dir / str(os.getpid())).write_text(f"{sid}\n{start_time}\n")

        result = _run(
            ["write", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"CLAUDE_CONFIG_DIR": str(config_dir)},
        )
        assert result.returncode == 0, result.stderr

        marker_dir = config_dir / "code-review-markers"
        files = list(marker_dir.iterdir())
        assert len(files) == 1
        assert files[0].name.endswith(f".{sid}")

        home_marker_dir = isolated_home / ".claude" / "code-review-markers"
        assert list(home_marker_dir.iterdir()) == [], (
            "marker.sh must not also write under $HOME/.claude when "
            "CLAUDE_CONFIG_DIR is set"
        )

    def test_write_aborts_when_config_dir_unresolvable(self, isolated_home, git_repo):
        """Fail closed (ledger row 11): a relative CLAUDE_CONFIG_DIR must
        abort the write rather than silently falling through to a
        root-anchored path."""
        result = _run(
            ["write", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"CLAUDE_CONFIG_DIR": "relative/path"},
        )
        assert result.returncode == 2, result.stderr
        assert "config directory" in result.stderr
        marker_dir = isolated_home / ".claude" / "code-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"a resolver failure must not write a marker: {stray}"

    def test_status_reads_completion_marker_from_config_dir_not_home(
        self, isolated_home, git_repo, tmp_path
    ):
        """Same cross-account bypass this class guards against, for the read
        side: status must resolve markers via CLAUDE_CONFIG_DIR, not fall
        back to $HOME/.claude. The stale marker seeded under the default
        home dir would report "historical" if status read it instead of the
        custom dir's live one -- proving CLAUDE_CONFIG_DIR was honored."""
        config_dir = tmp_path / "custom-config-dir"
        sessions_dir = config_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        sid = "test-session-status-config-dir"
        start_time = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())],
            env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip("\n")
        (sessions_dir / str(os.getpid())).write_text(f"{sid}\n{start_time}\n")

        write_marker(
            isolated_home, git_repo, staged_diff_hash(git_repo), session_id=sid, config_dir=config_dir
        )
        write_marker(isolated_home, git_repo, "0" * 64, session_id=sid)

        result = _run(
            ["status"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"CLAUDE_CONFIG_DIR": str(config_dir)},
        )
        assert result.returncode == 0, result.stderr
        assert "code-review: live" in result.stdout


class TestSessionStartTimeResolution:
    """_walk_session's PID-reuse guard: a sessions/<pid> entry is trusted
    only when its recorded start time matches the live process's current
    `ps -o lstart=` start time. Named invariant (see
    .claude/plans/retire-sessionend-destructors.md): any entry that doesn't
    satisfy this — mismatched, missing the second line, or empty — is
    untrusted and must fall through to the next ancestor exactly as if the
    file were absent, never resolve to a session id."""

    def _write_raw_session_file(self, home, content: str) -> None:
        sessions_dir = home / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / str(os.getpid())).write_text(content)

    def test_mismatched_start_time_does_not_resolve(self, isolated_home, git_repo):
        """A start time that doesn't match the live PID's actual lstart --
        what a reused PID's stale entry looks like -- must not authorize an
        activate."""
        self._write_raw_session_file(
            isolated_home, "test-session-mismatch\nMon Jan  1 00:00:00 1970\n"
        )
        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, (
            f"a mismatched start time must not resolve a session id, got "
            f"{result.returncode}. stderr: {result.stderr!r}"
        )
        active_dir = isolated_home / ".claude" / ".plan-review-active.d"
        stray = list(active_dir.iterdir()) if active_dir.exists() else []
        assert stray == [], f"a mismatched-start-time entry must not write a marker: {stray}"

    def test_old_format_single_line_leftover_resolves_as_absent(
        self, isolated_home, git_repo
    ):
        """Every sessions/<pid> entry on disk at merge time is this shape:
        written by the pre-fix capture-session-id.sh, one line, no recorded
        start time. It must resolve exactly as if the file were absent, not
        crash and not false-match."""
        self._write_raw_session_file(isolated_home, "test-session-old-format\n")
        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, (
            f"an old-format single-line entry must resolve as absent, got "
            f"{result.returncode}. stderr: {result.stderr!r}"
        )

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("", id="empty_file"),
            pytest.param("\nMon Jan  1 00:00:00 1970\n", id="empty_first_line"),
        ],
    )
    def test_empty_session_or_empty_first_line_resolves_as_absent(
        self, isolated_home, git_repo, content
    ):
        self._write_raw_session_file(isolated_home, content)
        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, (
            f"content {content!r} must resolve as absent, got "
            f"{result.returncode}. stderr: {result.stderr!r}"
        )

    def test_writer_reader_round_trip(self, isolated_home, git_repo):
        """Run capture-session-id.sh for real, then marker.sh activate --
        pins the two-script format as a tested contract instead of two
        independent implementations that happen to agree today."""
        sid = "roundtrip-session"
        run_hook(
            HOOKS_DIR / "capture-session-id.sh",
            {"session_id": sid},
            home=isolated_home,
        )
        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        active_file = isolated_home / ".claude" / ".plan-review-active.d" / sid
        assert active_file.exists()

    @pytest.mark.parametrize(
        "invalid_content",
        [
            pytest.param("skipped-mismatch\nMon Jan  1 00:00:00 1970\n", id="mismatch"),
            pytest.param("skipped-old-format\n", id="old_format"),
            pytest.param("", id="empty"),
        ],
    )
    def test_skips_invalid_immediate_ancestor_and_resolves_deeper_one(
        self, isolated_home, git_repo, invalid_content
    ):
        """The walk's fall-through must not just reject a bad entry — it
        must keep walking and resolve a valid entry at a deeper ancestor.
        Every existing deny-path test seeds exactly one entry (at the
        process marker.sh sees as its immediate ancestor) and asserts
        returncode == 2, which can't distinguish 'correctly skipped and
        kept walking, found nothing further up' from a regression that
        aborts the walk on the first bad entry. This test seeds an invalid
        entry at the immediate ancestor AND a valid one at a real deeper
        ancestor (this test process's own parent), so only the
        skip-and-continue behavior can pass it."""
        self._write_raw_session_file(isolated_home, invalid_content)
        sid = "deeper-ancestor-session"
        _seed_session(isolated_home, sid, pid=os.getppid())

        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)

        assert result.returncode == 0, (
            f"must skip the invalid immediate-ancestor entry and resolve the "
            f"valid deeper one, got {result.returncode}. stderr: {result.stderr!r}"
        )
        active_file = isolated_home / ".claude" / ".plan-review-active.d" / sid
        assert active_file.exists(), (
            "the active marker must be written under the deeper ancestor's "
            "session id, proving the walk actually reached it"
        )

    def test_differing_writer_reader_locale_still_resolves(self, isolated_home, git_repo):
        """Regression guard for the TZ=UTC LC_ALL=C pin itself: run the
        writer under one ambient TZ/LC_ALL and the reader under another,
        assert resolution still succeeds. Without the pin, an ambient
        divergence between the SessionStart hook's shell and the Bash
        tool's shell would make every entry mismatch forever."""
        sid = "locale-mismatch-session"
        run_hook(
            HOOKS_DIR / "capture-session-id.sh",
            {"session_id": sid},
            home=isolated_home,
            extra_env={"TZ": "America/New_York", "LC_ALL": "fr_FR.UTF-8"},
        )
        result = _run(
            ["activate", "plan-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"TZ": "UTC", "LC_ALL": "C"},
        )
        assert result.returncode == 0, result.stderr
        active_file = isolated_home / ".claude" / ".plan-review-active.d" / sid
        assert active_file.exists()


class TestMarkerScriptResolveSessionId:
    """`marker.sh resolve-session-id` exposes _resolve_session_id's result to
    a caller that needs the canonically-resolved session id without
    performing a write -- see SKILL.md's declare-planmode-path recipe, which
    uses this instead of a hand-rolled, non-liveness-checked lookup."""

    SID = "test-session-resolve"

    def test_prints_the_resolved_session_id(self, isolated_home, git_repo):
        sid = self.SID
        _seed_session(isolated_home, sid)
        result = _run(["resolve-session-id"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert result.stdout == sid

    def test_exits_2_with_empty_stdout_when_session_file_missing(self, isolated_home, git_repo):
        result = _run(["resolve-session-id"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""

    def test_rejects_stale_reused_pid_entry(self, isolated_home, git_repo):
        """A sessions/<pid> entry whose recorded start time doesn't match the
        live process's actual lstart -- the shape a reused PID's stale entry
        takes -- must not resolve. Proves this subcommand delegates to the
        canonical liveness-matched walk rather than a naive lookup, matching
        TestSessionStartTimeResolution's coverage of the same guard for the
        write/activate arms."""
        sessions_dir = isolated_home / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / str(os.getpid())).write_text(
            "test-session-stale-resolve\nMon Jan  1 00:00:00 1970\n"
        )
        result = _run(["resolve-session-id"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""


class TestMarkerScriptRoutingReadBackfill:
    """`activate plan-review` backfills routing-read credit from
    log-routing-read.sh's pending-read record when its mtime is within the
    bounded backfill window -- closes the ordering race where a Read landing
    just before activate previously earned no credit at all."""

    SID = "test-session-backfill"

    def _pending_read_path(self, home, sid=SID):
        return home / ".claude" / ".plan-review-pending-read.d" / sid

    def _routing_read_path(self, home, sid=SID):
        return home / ".claude" / ".plan-review-routing-read.d" / sid

    def _seed_pending_read(self, home, sid, age_seconds):
        pending = self._pending_read_path(home, sid)
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.touch()
        stamp = time.time() - age_seconds
        os.utime(pending, (stamp, stamp))

    def test_activate_backfills_when_pending_read_is_within_window(
        self, isolated_home, git_repo
    ):
        sid = self.SID
        _seed_session(isolated_home, sid)
        self._seed_pending_read(isolated_home, sid, age_seconds=4 * 60)

        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)

        assert result.returncode == 0, result.stderr
        assert self._routing_read_path(isolated_home, sid).exists()

    def test_activate_does_not_backfill_when_pending_read_is_outside_window(
        self, isolated_home, git_repo
    ):
        sid = self.SID
        _seed_session(isolated_home, sid)
        self._seed_pending_read(isolated_home, sid, age_seconds=6 * 60)

        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)

        assert result.returncode == 0, result.stderr
        assert not self._routing_read_path(isolated_home, sid).exists()

    def test_read_before_activate_now_credits_intentional_widening(
        self, isolated_home, git_repo
    ):
        """Pins the deliberate behavior change: a ROUTING.md Read that
        happens before marker.sh activate now grants routing-read credit,
        where test_log_routing_read.py's
        test_read_routing_md_without_active_marker_writes_pending_read_only
        shows the routing-read marker itself is still not written directly
        at Read time. Exercises the real hook + script pair, not seeded
        fixtures, so it proves the two mechanisms actually agree with each
        other."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        run_hook(
            HOOKS_DIR / "log-routing-read.sh",
            read_input("/home/user/.claude/skills/plan-review/ROUTING.md", session_id=sid),
            home=isolated_home,
        )
        assert not self._routing_read_path(isolated_home, sid).exists(), (
            "sanity check: no active marker existed yet, so the Read must not "
            "have written the routing-read marker directly"
        )

        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)

        assert result.returncode == 0, result.stderr
        assert self._routing_read_path(isolated_home, sid).exists(), (
            "activate must backfill routing-read credit from the pending-read "
            "record left by the earlier Read"
        )

    def test_read_for_unrelated_task_still_credits_a_later_activate_within_window(
        self, isolated_home, git_repo
    ):
        """Pins the accepted trade-off ciso-reviewer named: the pending-read
        record carries no signal about WHY ROUTING.md was read. A Read
        performed for a wholly unrelated task (no plan-review session
        active, several minutes of other work following it) still credits
        a plan-review `activate` as long as it lands inside the 5-minute
        window -- a deliberate, disclosed trade-off, not an oversight to
        fix here."""
        sid = self.SID
        _seed_session(isolated_home, sid)

        # No plan-review session is active when this Read happens -- e.g.
        # the agent opened ROUTING.md while editing the skill itself,
        # unrelated to any imminent plan-review.
        run_hook(
            HOOKS_DIR / "log-routing-read.sh",
            read_input("/home/user/.claude/skills/plan-review/ROUTING.md", session_id=sid),
            home=isolated_home,
        )
        assert not self._routing_read_path(isolated_home, sid).exists(), (
            "sanity check: no active marker existed yet, so the Read must not "
            "have written the routing-read marker directly"
        )

        # Several minutes of unrelated work follow, still inside the window.
        pending = self._pending_read_path(isolated_home, sid)
        stamp = time.time() - 4 * 60
        os.utime(pending, (stamp, stamp))

        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)

        assert result.returncode == 0, result.stderr
        assert self._routing_read_path(isolated_home, sid).exists(), (
            "activate must credit the earlier ROUTING.md Read even though it "
            "was performed for an unrelated task, per the accepted "
            "intent-decoupled trade-off"
        )

    def test_read_outside_window_then_activate_still_denies_agent_spawn(
        self, isolated_home, git_repo
    ):
        """End-to-end deny-path pin, mirroring the rigor already given to
        the allow-path above: chains the three real hooks -- the pending
        write (log-routing-read.sh, Read outside the 5-minute window),
        marker.sh activate, and require-routing-read.sh (Agent spawn) --
        and asserts the spawn is still denied."""
        sid = self.SID
        _seed_session(isolated_home, sid)

        run_hook(
            HOOKS_DIR / "log-routing-read.sh",
            read_input("/home/user/.claude/skills/plan-review/ROUTING.md", session_id=sid),
            home=isolated_home,
        )
        pending = self._pending_read_path(isolated_home, sid)
        stale = time.time() - 6 * 60
        os.utime(pending, (stale, stale))

        activate_result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert activate_result.returncode == 0, activate_result.stderr
        assert not self._routing_read_path(isolated_home, sid).exists()

        assert run_hook(
            HOOKS_DIR / "require-routing-read.sh",
            agent_input(session_id=sid),
            home=isolated_home,
        ) == "deny"

    def test_deactivate_clears_pending_read_marker_too(self, isolated_home, git_repo):
        sid = self.SID
        _seed_session(isolated_home, sid)
        active_dir = isolated_home / ".claude" / ".plan-review-active.d"
        active_dir.mkdir(parents=True)
        (active_dir / sid).touch()
        routing_read_dir = isolated_home / ".claude" / ".plan-review-routing-read.d"
        routing_read_dir.mkdir(parents=True)
        (routing_read_dir / sid).touch()
        self._seed_pending_read(isolated_home, sid, age_seconds=10)

        result = _run(["deactivate", "plan-review"], cwd=git_repo, home=isolated_home)

        assert result.returncode == 0, result.stderr
        assert not (active_dir / sid).exists()
        assert not (routing_read_dir / sid).exists()
        assert not self._pending_read_path(isolated_home, sid).exists()


class TestMarkerScriptPlanModeSibling:
    """`write plan-review` prioritizes a `.planmode-path` sibling file over
    _lib_active_plan_hash when one is present -- see require-plan-review.sh's
    ExitPlanMode branch, which is the read-side counterpart this write must
    agree with byte-for-byte."""

    SID = "test-session-planmode"

    def _sibling_path(self, home, sid=SID):
        return home / ".claude" / ".plan-review-active.d" / f"{sid}.planmode-path"

    def _declare_sibling(self, home, target_path, sid=SID):
        sibling = self._sibling_path(home, sid)
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_text(str(target_path))
        return sibling

    def test_valid_sibling_stores_fresh_hash_of_target_not_repo_relative_hash(
        self, isolated_home, git_repo, tmp_path
    ):
        """The stored marker must be the plan-mode target's own hash, not
        _lib_active_plan_hash's repo-relative result -- even when an active
        repo-relative plan set also exists, so the two hashes would differ if
        the write arm picked the wrong source."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        plans_dir = git_repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "p.md").write_text("# repo-relative plan\n")

        plan_mode_file = tmp_path / "planmode.md"
        plan_mode_file.write_text("# plan-mode content\n")
        self._declare_sibling(isolated_home, plan_mode_file, sid)

        result = _run(["write", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr

        marker = isolated_home / ".claude" / "plan-review-markers" / next(
            f.name for f in (isolated_home / ".claude" / "plan-review-markers").iterdir()
        )
        stored_hash = marker.read_text().strip()
        expected_digest = subprocess.run(
            ["sha256sum", str(plan_mode_file)], capture_output=True, text=True, check=True
        ).stdout.split()[0]
        assert stored_hash == expected_digest, (
            f"expected the plan-mode target's own hash {expected_digest!r}, got "
            f"{stored_hash!r} (looks like the repo-relative hash was used instead)"
        )

    def test_edit_between_declare_and_write_changes_stored_hash(
        self, isolated_home, git_repo, tmp_path
    ):
        """Freshness, not a cached value: the target is hashed at write time,
        so an edit after the sibling was declared is still caught."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        plan_mode_file = tmp_path / "planmode.md"
        plan_mode_file.write_text("# version one\n")
        self._declare_sibling(isolated_home, plan_mode_file, sid)

        assert _run(["write", "plan-review"], cwd=git_repo, home=isolated_home).returncode == 0
        marker_dir = isolated_home / ".claude" / "plan-review-markers"
        marker = marker_dir / next(f.name for f in marker_dir.iterdir())
        first_hash = marker.read_text().strip()

        plan_mode_file.write_text("# version two\n")
        assert _run(["write", "plan-review"], cwd=git_repo, home=isolated_home).returncode == 0
        second_hash = marker.read_text().strip()

        assert first_hash != second_hash, "editing the target must change the stored hash"

    def test_sibling_absent_falls_back_to_repo_relative_hash_unchanged(
        self, isolated_home, git_repo
    ):
        sid = self.SID
        _seed_session(isolated_home, sid)
        plans_dir = git_repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "p.md").write_text("# repo-relative plan\n")
        assert not self._sibling_path(isolated_home, sid).exists()

        result = _run(["write", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        marker_dir = isolated_home / ".claude" / "plan-review-markers"
        stored_hash = (marker_dir / next(f.name for f in marker_dir.iterdir())).read_text().strip()
        assert re.fullmatch(r"[0-9a-f]{64}", stored_hash)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_sibling_present_but_target_unreadable_aborts_without_writing(
        self, isolated_home, git_repo, tmp_path
    ):
        """Matches _lib_active_plan_hash's own abort contract: falling back
        to the repo-relative hash here would silently write a completion
        marker that doesn't cover what was actually reviewed."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        plan_mode_file = tmp_path / "unreadable-planmode.md"
        plan_mode_file.write_text("# secret\n")
        plan_mode_file.chmod(0o000)
        self._declare_sibling(isolated_home, plan_mode_file, sid)

        try:
            result = _run(["write", "plan-review"], cwd=git_repo, home=isolated_home)
        finally:
            plan_mode_file.chmod(0o644)

        assert result.returncode == 2, result.stderr
        marker_dir = isolated_home / ".claude" / "plan-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"an aborted write must not write a marker: {stray}"

    @pytest.mark.timing
    def test_sibling_target_read_timeout_aborts_within_budget(
        self, isolated_home, git_repo, tmp_path
    ):
        """_lib_capped caps the sha256sum call at 5s; a stalled read on the
        sibling's declared target (e.g. a dead network mount) must abort the
        write within that budget, not hang -- mirrors
        test_planfilepath_target_read_timeout_denies_within_budget in
        test_require_plan_review.py for the read side of this same hash.

        The stub only sleeps when called with a filename argument -- this
        `write plan-review` case also computes REPO_HASH via
        _marker_lib_repo_hash beforehand, which pipes a short string through
        `sha256sum` with no filename argument at all; sleeping unconditionally
        would stall that unrelated, uncapped call too (it hashes an in-memory
        path string, not file/network I/O, so it carries no real timeout risk)
        and inflate this test's budget by that call's full sleep on top of the
        one actually under test."""
        if not shutil.which("timeout"):
            pytest.skip("timeout(1) not available — BSD/macOS without coreutils")
        real_sha256sum = shutil.which("sha256sum")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "sha256sum"
        stub.write_text(f'#!/bin/bash\nif [ "$#" -gt 0 ]; then sleep 10; fi\nexec {real_sha256sum} "$@"\n')
        stub.chmod(0o755)

        sid = self.SID
        _seed_session(isolated_home, sid)
        plan_mode_file = tmp_path / "slow-planmode.md"
        plan_mode_file.write_text("# plan\n")
        self._declare_sibling(isolated_home, plan_mode_file, sid)

        start = time.monotonic()
        result = _run(
            ["write", "plan-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )
        elapsed = time.monotonic() - start

        assert result.returncode != 0, result.stderr
        assert elapsed < 9.5, (
            f"expected the 5s _lib_capped timeout to fire (stub sleeps 10s if "
            f"it does not), took {elapsed:.1f}s"
        )
        marker_dir = isolated_home / ".claude" / "plan-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"a timed-out write must not write a marker: {stray}"

    def test_deactivate_removes_the_sibling(self, isolated_home, git_repo, tmp_path):
        sid = self.SID
        _seed_session(isolated_home, sid)
        sibling = self._declare_sibling(isolated_home, tmp_path / "planmode.md", sid)
        assert sibling.exists()

        result = _run(["deactivate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert not sibling.exists()

    @pytest.mark.parametrize(
        "adjacent_pid",
        [
            pytest.param(None, id="no_adjacent_pid_file"),
            pytest.param("live", id="live_pid_adjacent"),
            pytest.param("dead", id="dead_pid_adjacent"),
        ],
    )
    def test_clear_stale_does_not_evict_a_live_sibling(
        self, isolated_home, git_repo, tmp_path, adjacent_pid
    ):
        """The sibling holds a path, never a PID, so clear-stale's
        ^[0-9]+$ liveness test would always misread it as a dead marker
        without the name-based exemption. Runs regardless of the adjacent
        PID file's liveness state -- the sibling's survival does not depend
        on it."""
        sid = self.SID
        sibling = self._declare_sibling(isolated_home, tmp_path / "planmode.md", sid)

        if adjacent_pid is not None:
            active_dir = isolated_home / ".claude" / ".plan-review-active.d"
            active_dir.mkdir(parents=True, exist_ok=True)
            stored_pid = str(os.getpid()) if adjacent_pid == "live" else "99999999"
            (active_dir / sid).write_text(stored_pid)

        result = _run(["clear-stale"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert sibling.exists(), "clear-stale must not evict a live .planmode-path sibling"

    def test_activate_does_not_create_a_sibling_file(self, isolated_home, git_repo):
        """The sibling is written by the skill's Step 0 declaration, not by
        `marker.sh activate` -- activate's own behavior is unchanged."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        result = _run(["activate", "plan-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert not self._sibling_path(isolated_home, sid).exists()


class TestWalkSessionDelegatesToLib:
    """_walk_session must actually call _lib.sh's _lib_resolve_claude_pid,
    not just reproduce its output via a second, independent ancestor walk —
    matching output alone (every other test in this file) doesn't prove
    that. Proven by overriding _lib_resolve_claude_pid in the exact _lib.sh
    copy marker.sh's own `dirname "$0"`-relative sourcing resolves, and
    observing that `resolve-session-id`'s output changes to the override's
    sentinel — output a real ancestor walk could never independently
    produce."""

    def test_resolve_session_id_reflects_lib_resolve_claude_pid_override(self, tmp_path, isolated_home):
        scripts_dir = tmp_path / "scripts"
        hooks_dir = tmp_path / "hooks"
        scripts_dir.mkdir()
        hooks_dir.mkdir()
        (scripts_dir / "marker.sh").symlink_to(MARKER_SCRIPT)
        instrumented_lib = (HOOKS_DIR / "_lib.sh").read_text() + (
            "\n_lib_resolve_claude_pid() {\n"
            '  printf "spy-session-id spy-pid"\n'
            "  return 0\n"
            "}\n"
        )
        (hooks_dir / "_lib.sh").write_text(instrumented_lib)

        result = subprocess.run(
            ["bash", str(scripts_dir / "marker.sh"), "resolve-session-id"],
            env={**os.environ, "HOME": str(isolated_home)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "spy-session-id"


class TestMarkerScriptStatusCompletionMarkers:
    """`marker.sh status` reports each completion marker (code-review,
    skill-review, plan-review, ready-for-review) as live, historical, or
    absent, recomputing the current expected value with the same recipe
    `write` uses."""

    SID = "test-session-status"

    def _make_skill_md(self, repo):
        skill_dir = repo / "claude-skills" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("# test skill\n")
        subprocess.run(["git", "add", str(skill_md)], cwd=repo, check=True)
        return skill_md

    def _make_repo_root_skill_md(self, repo):
        """Repo-root plugin layout (skills/<name>/SKILL.md), used when a
        marketplace declares "source": "./"."""
        skill_dir = repo / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("# test repo-root skill\n")
        subprocess.run(["git", "add", str(skill_md)], cwd=repo, check=True)
        return skill_md

    # ── code-review ────────────────────────────────────────────────────

    def test_code_review_live_when_hash_matches_staged_diff(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "code-review: live" in result.stdout

    def test_code_review_historical_when_marker_hash_is_stale(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        write_marker(isolated_home, git_repo, "0" * 64, session_id=self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "code-review: historical" in result.stdout

    def test_code_review_absent_when_no_marker_exists(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "code-review: absent" in result.stdout

    @pytest.mark.timing
    def test_code_review_value_computation_times_out_gracefully(
        self, isolated_home, git_repo, tmp_path
    ):
        """The code-review diff+hash is capped via _lib_capped -- a stalled
        `git diff --cached` must not hang the whole status report, and the
        empty value a killed process yields must fall through cleanly to
        _status_report_completion_marker's absent path, not crash. The stub
        only sleeps for the exact bare `-C <repo> diff --cached` invocation
        (no pathspec) so the skill-review, plan-review, and ready-for-review
        git calls later in the same run are unaffected."""
        if not shutil.which("timeout"):
            pytest.skip("timeout(1) not available — BSD/macOS without coreutils")
        real_git = shutil.which("git")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text(
            '#!/bin/bash\n'
            'if [ "$1" = "-C" ] && [ "$3" = "diff" ] && [ "$4" = "--cached" ] && [ "$#" -eq 4 ]; then\n'
            '  sleep 10\n'
            'fi\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        _seed_session(isolated_home, self.SID)
        with assert_cap_engaged():
            result = _run(
                ["status"],
                cwd=git_repo,
                home=isolated_home,
                extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
            )

        assert result.returncode == 0, result.stderr
        assert "code-review: absent" in result.stdout

    # ── skill-review ───────────────────────────────────────────────────

    def test_skill_review_live_when_hash_matches_staged_skill_md_diff(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        self._make_skill_md(git_repo)
        write_skill_review_marker(isolated_home, git_repo, session_id=self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "skill-review: live" in result.stdout

    def test_skill_review_live_when_hash_matches_staged_repo_root_skill_md_diff(
        self, isolated_home, git_repo
    ):
        """Repo-root-layout SKILL.md diffs (skills/<name>/SKILL.md) report the same
        live state as the stowed layout — the status arm's pathspec array must cover
        both."""
        _seed_session(isolated_home, self.SID)
        self._make_repo_root_skill_md(git_repo)
        write_skill_review_marker(isolated_home, git_repo, session_id=self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "skill-review: live" in result.stdout

    def test_skill_review_historical_when_marker_hash_is_stale(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        marker = skill_review_marker_path(isolated_home, git_repo, session_id=self.SID)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("0" * 64 + "\n")
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "skill-review: historical" in result.stdout

    def test_skill_review_absent_when_no_marker_exists(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "skill-review: absent" in result.stdout

    # ── plan-review ────────────────────────────────────────────────────

    def test_plan_review_live_when_hash_matches_active_plan_set(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        plans_dir = git_repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "p.md").write_text("# plan\n")
        write_plan_review_marker(isolated_home, git_repo, self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "plan-review: live" in result.stdout

    def test_plan_review_historical_when_marker_hash_is_stale(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        marker = plan_review_marker_path(isolated_home, git_repo, self.SID)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("0" * 64 + "\n")
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "plan-review: historical" in result.stdout

    def test_plan_review_absent_when_no_marker_exists(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "plan-review: absent" in result.stdout

    # ── plan-review: plan-mode sibling priority ──────────────────────────

    def _sibling_path(self, home, sid):
        return home / ".claude" / ".plan-review-active.d" / f"{sid}.planmode-path"

    def _declare_sibling(self, home, target_path, sid):
        sibling = self._sibling_path(home, sid)
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_text(str(target_path))
        return sibling

    def test_plan_review_live_when_marker_matches_sibling_target_not_repo_relative_hash(
        self, isolated_home, git_repo, tmp_path
    ):
        """A `.planmode-path` sibling takes priority over the repo-relative
        active plan set -- status must hash the sibling's target, matching
        `write plan-review`'s own priority (TestMarkerScriptPlanModeSibling).
        The repo-relative plan set below has a different hash than the
        sibling target, so a live result here proves the sibling was used."""
        _seed_session(isolated_home, self.SID)
        plans_dir = git_repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "p.md").write_text("# repo-relative plan\n")

        plan_mode_file = tmp_path / "planmode.md"
        plan_mode_file.write_text("# plan-mode content\n")
        self._declare_sibling(isolated_home, plan_mode_file, self.SID)

        expected_digest = subprocess.run(
            ["sha256sum", str(plan_mode_file)], capture_output=True, text=True, check=True
        ).stdout.split()[0]
        marker = plan_review_marker_path(isolated_home, git_repo, self.SID)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(expected_digest + "\n")

        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "plan-review: live" in result.stdout

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_plan_review_reports_absent_when_sibling_target_unreadable(
        self, isolated_home, git_repo, tmp_path
    ):
        """Diverges from `write plan-review`'s abort-on-unreadable-sibling
        behavior (test_sibling_present_but_target_unreadable_aborts_without_writing):
        status is a report, not a write, so an unreadable sibling target must
        not crash the whole command -- it degrades to the empty-hash path,
        which _status_report_completion_marker already reports as absent."""
        _seed_session(isolated_home, self.SID)
        plan_mode_file = tmp_path / "unreadable-planmode.md"
        plan_mode_file.write_text("# secret\n")
        plan_mode_file.chmod(0o000)
        self._declare_sibling(isolated_home, plan_mode_file, self.SID)

        try:
            result = _run(["status"], cwd=git_repo, home=isolated_home)
        finally:
            plan_mode_file.chmod(0o644)

        assert result.returncode == 0, result.stderr
        assert "plan-review: absent" in result.stdout

    # ── ready-for-review ───────────────────────────────────────────────

    def test_ready_for_review_live_when_hash_matches_head(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        marker = _ready_for_review_marker_path(isolated_home, git_repo, self.SID)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(git_repo) + "\n")
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "ready-for-review: live" in result.stdout

    def test_ready_for_review_historical_when_marker_hash_is_stale(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        marker = _ready_for_review_marker_path(isolated_home, git_repo, self.SID)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("0" * 40 + "\n")
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "ready-for-review: historical" in result.stdout

    def test_ready_for_review_absent_when_no_marker_exists(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "ready-for-review: absent" in result.stdout

    def test_ready_for_review_absent_cleanly_on_zero_commit_repo(self, isolated_home, tmp_path):
        """git_repo always seeds a commit, so build a genuinely commit-less
        repo here -- `git rev-parse HEAD` has nothing to resolve, and status
        must report 'absent' cleanly rather than erroring."""
        repo = tmp_path / "zero-commit-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        _seed_session(isolated_home, self.SID)
        result = _run(["status"], cwd=repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "ready-for-review: absent" in result.stdout

    # ── unreadable marker ──────────────────────────────────────────────

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_unreadable_marker_reported_as_not_matching_not_a_crash(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        marker = write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        marker.chmod(0o000)
        try:
            result = _run(["status"], cwd=git_repo, home=isolated_home)
        finally:
            marker.chmod(0o644)
        assert result.returncode == 0, result.stderr
        # _lib_marker_value_present's grep swallows an unreadable marker's
        # content and reports no match -- the file's mere presence still
        # reads as historical, not live, and the script must not crash.
        assert "code-review: historical" in result.stdout

    # ── cross-repo isolation ───────────────────────────────────────────

    def test_status_never_leaks_another_repos_marker(self, isolated_home, git_repo, tmp_path):
        other_repo = tmp_path / "other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=other_repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=other_repo, check=True)
        (other_repo / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=other_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=other_repo, check=True)

        _seed_session(isolated_home, self.SID)
        other_session = "other-repo-session"
        other_marker = write_marker(
            isolated_home, other_repo, staged_diff_hash(other_repo), session_id=other_session
        )
        other_repo_hash = other_marker.name.split(".")[0]

        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert other_marker.name not in result.stdout
        assert other_session not in result.stdout
        assert other_repo_hash not in result.stdout


class TestMarkerScriptCumulativeReview:
    """`write cumulative-review` reads and consumes the subject
    `pr-diff-against-base.sh --record` already captured, rather than calling
    pr-diff-against-base.sh itself -- guarded against an absent, empty, or
    unreadable subject on the write side. `status` still hashes
    pr-diff-against-base.sh's output live via the shared
    _lib_cumulative_diff_hash helper, degrading to absent rather than
    aborting on a failed, empty, or hung diff -- unchanged by this class's
    write-side tests above."""

    SID = "test-session-cumulative-review"
    # Lower bound for proving _lib_cumulative_diff_hash's 15s cap fired,
    # not just any cap. Above the shared 5s _lib_capped default (so a
    # silent regression back to that cap still fails this assertion) and
    # safely under 15s (so cap-plus-overhead reliably clears it).
    CUMULATIVE_DIFF_CAP_FLOOR_SECONDS = 12

    def test_write_creates_marker_with_diff_hash(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        sid = self.SID
        _seed_session(isolated_home, sid)
        env = _env_with_gh_shim(tmp_path, None)
        record_result = _record_subject(cumulative_diff_repo, isolated_home, env)
        assert record_result.returncode == 0, record_result.stderr
        result = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert result.returncode == 0, result.stderr
        marker_dir = isolated_home / ".claude" / "cumulative-review-markers"
        files = list(marker_dir.iterdir())
        assert len(files) == 1
        assert files[0].name.endswith(f".{sid}")
        content = files[0].read_text().strip()
        assert re.fullmatch(r"[0-9a-f]{64}", content), (
            f"expected a sha256 hex digest, got {content!r}"
        )

    def test_write_refuses_when_no_subject_recorded(
        self, isolated_home, cumulative_diff_repo
    ):
        """No pr-diff-against-base.sh --record has ever run for this repo --
        the write arm must refuse rather than falling back to recomputing
        the diff itself, which is exactly the bug this design closes."""
        _seed_session(isolated_home, self.SID)
        result = _run(
            ["write", "cumulative-review"], cwd=cumulative_diff_repo, home=isolated_home
        )
        assert result.returncode == 2
        assert "no recorded" in result.stderr.lower()
        marker_dir = isolated_home / ".claude" / "cumulative-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"a missing subject must not write a marker: {stray}"

    def test_write_refuses_when_subject_is_empty(
        self, isolated_home, cumulative_diff_repo
    ):
        """A recorded-but-empty subject (e.g. a truncated write) must refuse
        with a message distinct from the no-subject-at-all case above."""
        _seed_session(isolated_home, self.SID)
        subject_path = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, self.SID)
        subject_path.parent.mkdir(parents=True, exist_ok=True)
        subject_path.write_text("")
        result = _run(
            ["write", "cumulative-review"], cwd=cumulative_diff_repo, home=isolated_home
        )
        assert result.returncode == 2
        assert "empty" in result.stderr.lower()
        marker_dir = isolated_home / ".claude" / "cumulative-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"an empty subject must not write a marker: {stray}"

    def test_write_refuses_when_subject_is_a_lone_trailing_newline(
        self, isolated_home, cumulative_diff_repo
    ):
        """A subject file holding only a trailing newline is non-empty by raw
        byte count but canonically empty once command substitution strips it.
        That's the one input distinguishing today's canonicalized-text check
        from the [ -s ] byte-count check it replaced -- a regression back to
        [ -s ]-style checking would let this pass."""
        _seed_session(isolated_home, self.SID)
        subject_path = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, self.SID)
        subject_path.parent.mkdir(parents=True, exist_ok=True)
        subject_path.write_text("\n")
        result = _run(
            ["write", "cumulative-review"], cwd=cumulative_diff_repo, home=isolated_home
        )
        assert result.returncode == 2
        assert "empty" in result.stderr.lower()
        marker_dir = isolated_home / ".claude" / "cumulative-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"a lone-trailing-newline subject must not write a marker: {stray}"

    def test_write_refuses_when_recorded_subject_is_a_genuinely_empty_diff(
        self, isolated_home, zero_diff_repo, tmp_path
    ):
        """End-to-end counterpart to test_write_refuses_when_subject_is_empty
        above: drives the real `pr-diff-against-base.sh --record` against a
        repo with a truly empty merge-base...HEAD diff, rather than
        hand-writing a 0-byte subject file. Guards against the producer
        itself manufacturing a non-empty subject file for an empty diff
        (e.g. a stray trailing newline), which write_text("") above cannot
        detect since it never calls the real producer."""
        _seed_session(isolated_home, self.SID)
        env = _env_with_gh_shim(tmp_path, "main")
        record_result = _record_subject(zero_diff_repo, isolated_home, env)
        assert record_result.returncode == 0, record_result.stderr
        result = _run(
            ["write", "cumulative-review"],
            cwd=zero_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert result.returncode == 2
        assert "empty" in result.stderr.lower()
        marker_dir = isolated_home / ".claude" / "cumulative-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"an empty subject must not write a marker: {stray}"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_write_refuses_when_subject_is_unreadable(
        self, isolated_home, cumulative_diff_repo
    ):
        """A permission-denied subject must refuse with a message distinct
        from both the absent and the empty cases above -- the existence
        check (`[ ! -e ]`) needs no read permission, so a 0000-mode file
        still passes it, and this exercises the later `cat` failure
        specifically."""
        _seed_session(isolated_home, self.SID)
        subject_path = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, self.SID)
        subject_path.parent.mkdir(parents=True, exist_ok=True)
        subject_path.write_text("some recorded diff\n")
        subject_path.chmod(0o000)
        try:
            result = _run(
                ["write", "cumulative-review"], cwd=cumulative_diff_repo, home=isolated_home
            )
        finally:
            subject_path.chmod(0o644)
        assert result.returncode == 2
        assert "could not read" in result.stderr.lower()
        marker_dir = isolated_home / ".claude" / "cumulative-review-markers"
        stray = list(marker_dir.iterdir()) if marker_dir.exists() else []
        assert stray == [], f"an unreadable subject must not write a marker: {stray}"

    def test_write_consumes_the_subject(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """A successful write rm -f's the subject, bounding a
        recorded-but-unreviewed subject to authorizing at most one write --
        a second write with no fresh recording must refuse."""
        _seed_session(isolated_home, self.SID)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        subject_path = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, self.SID)
        assert subject_path.exists()

        first = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert first.returncode == 0, first.stderr
        assert not subject_path.exists(), (
            "a successful write must consume (rm -f) the recorded subject"
        )

        second = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert second.returncode == 2, "a second write with no fresh subject must refuse"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_write_failure_does_not_consume_the_subject(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """mkdir -p failing to create cumulative-review-markers/ must not
        rm -f the recorded subject -- a retry needs the subject still on
        disk. Mirrors test_record_path_failure_leaves_prior_subject_intact's
        chmod technique in scripts/tests/test_pr_diff_against_base.py."""
        _seed_session(isolated_home, self.SID)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        subject_path = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, self.SID)
        assert subject_path.exists()

        config_dir = isolated_home / ".claude"
        config_dir.chmod(0o555)
        try:
            result = _run(
                ["write", "cumulative-review"],
                cwd=cumulative_diff_repo,
                home=isolated_home,
                extra_env=env,
            )
        finally:
            config_dir.chmod(0o755)

        assert result.returncode != 0, (
            "a failed mkdir -p must not report success"
        )
        assert subject_path.exists(), (
            "a failed marker write must leave the subject for a retry to consume"
        )

    def test_two_sessions_record_and_write_independently(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """The subject file is keyed by <repo-hash>.<session-id>, the same
        write-side key every completion marker kind uses. Recording under a
        second session id must not clobber the first session's still-pending
        subject, and each session's write must consume only its own subject.
        Reseeding the session file between sequences models a session change
        the same way a resume does (docs/hooks.md's "a resumed session gets
        a new session id")."""
        sid_a, sid_b = "test-session-subject-isolation-a", "test-session-subject-isolation-b"
        env = _env_with_gh_shim(tmp_path, None)

        _seed_session(isolated_home, sid_a)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        subject_a = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, sid_a)
        assert subject_a.exists()

        _seed_session(isolated_home, sid_b)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        subject_b = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, sid_b)
        assert subject_b.exists()
        assert subject_a.exists(), (
            "recording session B's subject must not clobber session A's still-pending one"
        )

        write_b = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert write_b.returncode == 0, write_b.stderr
        assert not subject_b.exists(), "session B's write must consume its own subject"
        assert subject_a.exists(), (
            "session B's write must not consume session A's still-pending subject"
        )

        _seed_session(isolated_home, sid_a)
        write_a = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert write_a.returncode == 0, write_a.stderr
        assert not subject_a.exists(), "session A's write must consume its own subject"

        marker_dir = isolated_home / ".claude" / "cumulative-review-markers"
        marker_names = {f.name for f in marker_dir.iterdir()}
        assert any(name.endswith(f".{sid_a}") for name in marker_names)
        assert any(name.endswith(f".{sid_b}") for name in marker_names)

    def test_write_stores_hash_of_recorded_subject_not_diff_at_write_time(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """The marker's value is a hash of the text recorded at --record
        time, not one recomputed from the diff as it exists when `write`
        runs -- the direct value-level counterpart to
        test_status_historical_when_commit_lands_between_record_and_write's
        end-to-end status assertion."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        subject_path = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, self.SID)
        recorded_text = subject_path.read_bytes().rstrip(b"\n")

        (cumulative_diff_repo / "third.txt").write_text("third file\n")
        subprocess.run(["git", "add", "third.txt"], cwd=cumulative_diff_repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "late fix commit"],
            cwd=cumulative_diff_repo,
            check=True,
        )

        write_result = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert write_result.returncode == 0, write_result.stderr
        marker = _cumulative_review_marker_path(isolated_home, cumulative_diff_repo, sid)
        stored_value = marker.read_text().strip()
        assert stored_value == hashlib.sha256(recorded_text).hexdigest()

    def test_status_live_when_hash_matches_current_diff(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        sid = self.SID
        _seed_session(isolated_home, sid)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        write_result = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert write_result.returncode == 0, write_result.stderr
        result = _run(["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=env)
        assert result.returncode == 0, result.stderr
        assert "cumulative-review: live" in result.stdout

    def test_status_historical_when_marker_hash_is_stale(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        sid = self.SID
        _seed_session(isolated_home, sid)
        marker = _cumulative_review_marker_path(isolated_home, cumulative_diff_repo, sid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("0" * 64 + "\n")
        env = _env_with_gh_shim(tmp_path, None)
        result = _run(["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=env)
        assert result.returncode == 0, result.stderr
        assert "cumulative-review: historical" in result.stdout

    def test_status_absent_when_no_marker_exists(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        _seed_session(isolated_home, self.SID)
        env = _env_with_gh_shim(tmp_path, None)
        result = _run(["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=env)
        assert result.returncode == 0, result.stderr
        assert "cumulative-review: absent" in result.stdout

    @pytest.mark.timing
    def test_status_degrades_to_absent_on_timeout_rather_than_erroring(
        self, isolated_home, cumulative_diff_repo, gh_timeout_shim
    ):
        """status is a report, not a write -- a hung `gh pr view` must
        degrade the cumulative-review line to absent rather than crashing
        the whole status report (or hanging it past the 15s cap).
        sleep_seconds=20: past the 15s cap, unlike the 10s
        TIMEOUT_SHIM_SLEEP_SECONDS default calibrated for the shared 5s
        _lib_capped cap elsewhere."""
        _seed_session(isolated_home, self.SID)
        env = gh_timeout_shim('[ "$1" = "pr" ] && [ "$2" = "view" ]', sleep_seconds=20)
        with assert_cap_engaged(floor=self.CUMULATIVE_DIFF_CAP_FLOOR_SECONDS):
            result = _run(["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=env)
        assert result.returncode == 0, result.stderr
        assert "cumulative-review: absent" in result.stdout

    @pytest.mark.timing
    def test_status_reports_could_not_verify_when_marker_exists_but_hash_fails(
        self, isolated_home, cumulative_diff_repo, tmp_path, gh_timeout_shim
    ):
        """The CUMULATIVE_DIFF_HASH_FAILED stderr branch requires BOTH a
        cumulative-review marker already on disk AND _lib_cumulative_diff_hash
        failing on this status call -- distinct from
        test_status_degrades_to_absent_on_timeout_rather_than_erroring above,
        which covers the no-marker case and never reaches this branch. Write
        a marker with a working gh shim first, then re-run status with a
        `gh pr view` hung past the 15s cap."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        write_env = _env_with_gh_shim(tmp_path, None)
        assert (
            _record_subject(cumulative_diff_repo, isolated_home, write_env).returncode == 0
        )
        write_result = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=write_env,
        )
        assert write_result.returncode == 0, write_result.stderr

        timeout_env = gh_timeout_shim('[ "$1" = "pr" ] && [ "$2" = "view" ]', sleep_seconds=20)
        with assert_cap_engaged(floor=self.CUMULATIVE_DIFF_CAP_FLOOR_SECONDS):
            result = _run(
                ["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=timeout_env
            )
        assert result.returncode == 0, result.stderr
        assert "cumulative-review:" in result.stdout
        assert (
            "cumulative-review: could not verify (pr-diff-against-base.sh failed, "
            "the diff is empty -- often because this branch already has a merged "
            "PR -- or resolution timed out -- the state above reflects marker "
            "presence only, not a confirmed hash comparison)"
        ) in result.stderr

    def test_write_and_status_hash_recipes_agree_byte_for_byte(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """marker.sh write and status both call the shared
        _lib_cumulative_diff_hash helper, but that only proves the two call
        sites agree with EACH OTHER. Prove the stored value also matches an
        independently-computed sha256 of pr-diff-against-base.sh's own
        stdout, so a shared-helper bug that hashes the wrong bytes on both
        sides couldn't hide behind the two sides merely agreeing.

        Trailing newlines are stripped from the independently-captured
        stdout before hashing, mirroring bash command substitution's own
        trailing-newline stripping inside _lib_cumulative_diff_hash --
        without it this comparison would fail on the newline boundary alone,
        not on a real recipe mismatch."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        write_result = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert write_result.returncode == 0, write_result.stderr
        marker = _cumulative_review_marker_path(isolated_home, cumulative_diff_repo, sid)
        stored_value = marker.read_text().strip()

        diff_output = subprocess.run(
            [str(PR_DIFF_SCRIPT)],
            cwd=cumulative_diff_repo,
            env={**os.environ, **env},
            capture_output=True,
            check=True,
        ).stdout.rstrip(b"\n")
        assert stored_value == hashlib.sha256(diff_output).hexdigest()

        status_result = _run(
            ["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=env
        )
        assert status_result.returncode == 0, status_result.stderr
        assert "cumulative-review: live" in status_result.stdout

    def test_status_stays_live_after_conflict_free_rebase_onto_moved_base(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """The cache's own motivating scenario: origin/main advances with a
        commit the PR's diff never touches, the PR commit is rebased onto
        it conflict-free (a new HEAD SHA every time), and the cumulative
        diff bytes come out identical -- the marker hashed before the move
        must still read live after it."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        write_result = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert write_result.returncode == 0, write_result.stderr

        _advance_origin_main_and_rebase(cumulative_diff_repo, _commit_unrelated_file)

        result = _run(["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=env)
        assert result.returncode == 0, result.stderr
        assert "cumulative-review: live" in result.stdout

    def test_status_historical_when_commit_lands_between_record_and_write(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """The central regression test for this marker kind's own design:
        `write cumulative-review` must stamp the diff pr-diff-against-base.sh
        --record captured at step-3 entry, not one recomputed at write time.
        A fix commit landing after the record and before the write -- e.g. a
        fix whose only review was a narrow staged-diff /code-review -- must
        therefore leave the marker stale immediately, not live. Fails against
        a write arm that recomputes its own diff, which is structurally
        incapable of producing anything but live."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        env = _env_with_gh_shim(tmp_path, None)
        record_result = _record_subject(cumulative_diff_repo, isolated_home, env)
        assert record_result.returncode == 0, record_result.stderr

        (cumulative_diff_repo / "third.txt").write_text("third file\n")
        subprocess.run(["git", "add", "third.txt"], cwd=cumulative_diff_repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "late fix commit"],
            cwd=cumulative_diff_repo,
            check=True,
        )

        write_result = _run(
            ["write", "cumulative-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert write_result.returncode == 0, write_result.stderr

        status_result = _run(
            ["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=env
        )
        assert status_result.returncode == 0, status_result.stderr
        assert "cumulative-review: historical" in status_result.stdout

    def test_status_historical_after_base_move_shifts_diff_context(
        self, isolated_home, context_diff_repo, tmp_path
    ):
        """A base-branch commit that edits content merely adjacent to the
        PR's own change -- not the changed line itself -- can still shift
        the diff hunk's context lines after a conflict-free rebase. The
        cache must not paper over that with a false live: this is
        docs/design-decisions.md §44's named residual (a byte-identical
        rebase is not guaranteed for every conflict-free rebase, only the
        motivating case where nothing near the diff moved)."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(context_diff_repo, isolated_home, env).returncode == 0
        write_result = _run(
            ["write", "cumulative-review"],
            cwd=context_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert write_result.returncode == 0, write_result.stderr

        _advance_origin_main_and_rebase(context_diff_repo, _insert_adjacent_line)

        result = _run(["status"], cwd=context_diff_repo, home=isolated_home, extra_env=env)
        assert result.returncode == 0, result.stderr
        assert "cumulative-review: historical" in result.stdout
        assert "could not verify" not in result.stderr, (
            "historical must reflect a genuine hash mismatch, not a masked "
            "_lib_cumulative_diff_hash computation failure"
        )

    def test_status_ignores_the_subject_directory(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """status recomputes the cumulative diff live via
        _lib_cumulative_diff_hash, unchanged by this class's write-side
        tests above -- a recorded subject sitting on disk must not change
        what status reports."""
        _seed_session(isolated_home, self.SID)
        env = _env_with_gh_shim(tmp_path, None)
        before = _run(["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=env)
        assert before.returncode == 0, before.stderr

        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0

        after = _run(["status"], cwd=cumulative_diff_repo, home=isolated_home, extra_env=env)
        assert after.returncode == 0, after.stderr
        assert before.stdout == after.stdout

    def test_deactivate_ready_for_review_removes_the_subject(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        _seed_session(isolated_home, self.SID)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        subject_path = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, self.SID)
        assert subject_path.exists()

        result = _run(
            ["deactivate", "ready-for-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert result.returncode == 0, result.stderr
        assert not subject_path.exists()

    def test_deactivate_does_not_remove_another_sessions_subject(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """The cleanup is session-scoped: session B's own `deactivate
        ready-for-review` must not delete session A's still-pending
        recorded subject."""
        sid_a, sid_b = "test-session-deactivate-a", "test-session-deactivate-b"
        env = _env_with_gh_shim(tmp_path, None)

        _seed_session(isolated_home, sid_a)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        subject_a = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, sid_a)
        assert subject_a.exists()

        _seed_session(isolated_home, sid_b)
        result = _run(
            ["deactivate", "ready-for-review"], cwd=cumulative_diff_repo, home=isolated_home
        )
        assert result.returncode == 0, result.stderr
        assert subject_a.exists(), (
            "session B's deactivate must not remove session A's still-pending subject"
        )

    def test_deactivate_ready_for_review_does_not_abort_when_repo_root_unresolvable(
        self, isolated_home, tmp_path
    ):
        """The active-bypass marker removal is session-scoped and unrelated
        to repo state, so it must still run even when this arm's best-effort
        subject cleanup can't resolve a repo root -- run from outside any
        git repository, so _resolve_repo_root fails."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        active_marker = isolated_home / ".claude" / ".ready-for-review-active.d" / sid
        active_marker.parent.mkdir(parents=True, exist_ok=True)
        active_marker.write_text("12345\n")

        result = _run(["deactivate", "ready-for-review"], cwd=tmp_path, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert not active_marker.exists()

    def test_deactivate_ready_for_review_removes_the_diff_artifact(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """The step-4 diff-file artifact's own mirror of
        test_deactivate_ready_for_review_removes_the_subject above -- a
        distinct code path, so a mirror that never seeds this artifact
        would pass vacuously against an unimplemented cleanup line."""
        _seed_session(isolated_home, self.SID)
        env = _env_with_gh_shim(tmp_path, None)
        assert _write_diff_artifact(cumulative_diff_repo, isolated_home, env).returncode == 0
        artifact_path = _cumulative_review_diff_artifact_path(
            isolated_home, cumulative_diff_repo, self.SID
        )
        assert artifact_path.exists()

        result = _run(
            ["deactivate", "ready-for-review"],
            cwd=cumulative_diff_repo,
            home=isolated_home,
            extra_env=env,
        )
        assert result.returncode == 0, result.stderr
        assert not artifact_path.exists()

    def test_deactivate_does_not_remove_another_sessions_diff_artifact(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        """The cleanup is session-scoped: session B's own `deactivate
        ready-for-review` must not delete session A's still-present diff
        artifact -- the diff-artifact mirror of
        test_deactivate_does_not_remove_another_sessions_subject."""
        sid_a, sid_b = "test-session-deactivate-diff-a", "test-session-deactivate-diff-b"
        env = _env_with_gh_shim(tmp_path, None)

        _seed_session(isolated_home, sid_a)
        assert _write_diff_artifact(cumulative_diff_repo, isolated_home, env).returncode == 0
        artifact_a = _cumulative_review_diff_artifact_path(isolated_home, cumulative_diff_repo, sid_a)
        assert artifact_a.exists()

        _seed_session(isolated_home, sid_b)
        result = _run(
            ["deactivate", "ready-for-review"], cwd=cumulative_diff_repo, home=isolated_home
        )
        assert result.returncode == 0, result.stderr
        assert artifact_a.exists(), (
            "session B's deactivate must not remove session A's still-present diff artifact"
        )

    def test_deactivate_ready_for_review_names_both_artifacts_when_repo_root_unresolvable(
        self, isolated_home, tmp_path
    ):
        """The diff-artifact mirror of
        test_deactivate_ready_for_review_does_not_abort_when_repo_root_unresolvable,
        additionally pinning that the skip message names both artifacts
        now that the arm removes two, not one."""
        sid = self.SID
        _seed_session(isolated_home, sid)
        active_marker = isolated_home / ".claude" / ".ready-for-review-active.d" / sid
        active_marker.parent.mkdir(parents=True, exist_ok=True)
        active_marker.write_text("12345\n")

        result = _run(["deactivate", "ready-for-review"], cwd=tmp_path, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert not active_marker.exists()
        assert "skipping cumulative-review subject and diff-file cleanup" in result.stderr

    def test_clear_stale_dry_run_ignores_the_subject_directory(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        _seed_session(isolated_home, self.SID)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        subject_path = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, self.SID)

        result = _run(["clear-stale", "--dry-run"], cwd=cumulative_diff_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "cumulative-review-subject-markers" not in result.stdout
        assert subject_path.exists()

    def test_clear_stale_does_not_evict_the_subject(
        self, isolated_home, cumulative_diff_repo, tmp_path
    ):
        _seed_session(isolated_home, self.SID)
        env = _env_with_gh_shim(tmp_path, None)
        assert _record_subject(cumulative_diff_repo, isolated_home, env).returncode == 0
        subject_path = _cumulative_review_subject_path(isolated_home, cumulative_diff_repo, self.SID)

        result = _run(["clear-stale"], cwd=cumulative_diff_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert subject_path.exists()


class TestMarkerScriptStatusDiffBaseInvocationCount:
    """`marker.sh status` resolves GATE_DIFF_BASE once and threads it into
    both the code-review and plan-review values below it -- a resolution per
    value would multiply the merge-tree cost for the same result (see the
    `status)` case's own comment above GATE_DIFF_BASE's assignment)."""

    SID = "test-session-status-diff-base-count"

    def test_gate_diff_base_resolved_once_across_code_review_and_plan_review(
        self, isolated_home, git_repo, tmp_path
    ):
        """_lib_gate_diff_base's own first git call --
        `rev-parse --absolute-git-dir` -- is the proxy counted here: it runs
        unconditionally on every _lib_gate_diff_base invocation, before any
        state-dependent branching, so counting it counts calls to the
        function itself."""
        _seed_session(isolated_home, self.SID)
        real_git = shutil.which("git")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        invocation_log = tmp_path / "git-absolute-git-dir-invocations"
        stub.write_text(
            '#!/bin/bash\n'
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "--absolute-git-dir" ]; then\n'
            f'    echo "$@" >> "{invocation_log}"\n'
            '  fi\n'
            'done\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        result = _run(
            ["status"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )
        assert result.returncode == 0, result.stderr
        invocations = invocation_log.read_text().splitlines() if invocation_log.exists() else []
        assert len(invocations) == 1, (
            f"expected exactly one `--absolute-git-dir`-shaped git invocation "
            f"(_lib_gate_diff_base resolved once, not once per value that "
            f"consumes it), got: {invocations}"
        )


class TestMarkerScriptStatusActiveBypass:
    """`marker.sh status` reports each active-bypass marker (plan-review,
    ready-for-review, respond-pr, memory-skill, handoff) for this session as
    live, stale, or absent."""

    SID = "test-session-status-bypass"

    ACTIVE_BYPASS_KINDS = [
        ("plan-review", ".plan-review-active.d"),
        ("ready-for-review", ".ready-for-review-active.d"),
        ("respond-pr", ".respond-pr-active.d"),
        ("memory-skill", ".memory-skill-active.d"),
        ("handoff", ".handoff-active.d"),
    ]

    @pytest.mark.parametrize("label,dir_name", ACTIVE_BYPASS_KINDS)
    def test_live_when_pid_is_alive(self, isolated_home, git_repo, label, dir_name):
        _seed_session(isolated_home, self.SID)
        active_dir = isolated_home / ".claude" / dir_name
        active_dir.mkdir(parents=True)
        (active_dir / self.SID).write_text(str(os.getpid()))
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert f"{label}: live" in result.stdout

    @pytest.mark.parametrize("label,dir_name", ACTIVE_BYPASS_KINDS)
    def test_stale_when_pid_is_dead_and_the_marker_is_evicted(
        self, isolated_home, git_repo, label, dir_name
    ):
        _seed_session(isolated_home, self.SID)
        active_dir = isolated_home / ".claude" / dir_name
        active_dir.mkdir(parents=True)
        marker = active_dir / self.SID
        marker.write_text("99999999")
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert f"{label}: stale" in result.stdout
        assert not marker.exists(), (
            "a stale active-bypass marker must be evicted, not just labeled"
        )

    @pytest.mark.parametrize("label,dir_name", ACTIVE_BYPASS_KINDS)
    def test_stale_when_pid_is_alive_but_mtime_is_idle_expired(
        self, isolated_home, git_repo, label, dir_name
    ):
        """Pins the idle-timeout half of status's staleness definition
        through its own CLI surface -- a reverted call site that special-cased
        the dead-PID path would pass every other test in this class."""
        _seed_session(isolated_home, self.SID)
        active_dir = isolated_home / ".claude" / dir_name
        active_dir.mkdir(parents=True)
        marker = active_dir / self.SID
        marker.write_text(str(os.getpid()))
        idle_time = time.time() - 3700  # just past the 60-minute idle window
        os.utime(marker, (idle_time, idle_time))
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        # status's stale message doesn't distinguish dead-PID from idle-timeout
        # (unlike clear-stale's), so the discriminating assertion here is that
        # an *alive*-PID marker still evicts -- the label alone can't prove it.
        assert f"{label}: stale" in result.stdout
        assert not marker.exists(), (
            "an idle-expired active-bypass marker must be evicted, not just labeled"
        )

    @pytest.mark.parametrize("label,dir_name", ACTIVE_BYPASS_KINDS)
    def test_absent_when_no_marker_exists(self, isolated_home, git_repo, label, dir_name):
        _seed_session(isolated_home, self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert f"{label}: absent" in result.stdout


class TestMarkerScriptStatusReconciliationFlag:
    """The reconciliation flag applies only to code-review and skill-review
    (hash-of-a-real-pathspec markers) -- never to ready-for-review
    (HEAD-keyed, no pathspec) or plan-review (its own live/historical
    distinction already covers this)."""

    SID = "test-session-status-reconcile"

    def test_code_review_flag_fires_on_unstaged_change_overlapping_the_whole_repo(
        self, isolated_home, git_repo
    ):
        _seed_session(isolated_home, self.SID)
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        # Additional unstaged change on top of the fixture's staged one.
        (git_repo / "file.txt").write_text("first\nsecond\nthird\n")
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "code-review: live" in result.stdout
        assert "code-review reconciliation flag" in result.stdout

    def test_code_review_flag_absent_when_working_tree_is_clean(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "code-review: live" in result.stdout
        assert "code-review reconciliation flag" not in result.stdout

    def test_skill_review_flag_fires_on_unstaged_skill_md_change(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        skill_dir = git_repo / "claude-skills" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("# test skill\n")
        subprocess.run(["git", "add", str(skill_md)], cwd=git_repo, check=True)
        write_skill_review_marker(isolated_home, git_repo, session_id=self.SID)
        # Unstaged, overlapping the SKILL.md pathspec.
        skill_md.write_text("# test skill\nmodified\n")
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "skill-review: live" in result.stdout
        assert "skill-review reconciliation flag" in result.stdout

    def test_skill_review_flag_does_not_fire_for_out_of_scope_unstaged_change(
        self, isolated_home, git_repo
    ):
        """An unstaged change outside the SKILL.md pathspec must not fire the
        skill-review flag -- mirrors TestMarkerScriptEmptyStagedGuard's
        pathspec discipline for the write-side guard."""
        _seed_session(isolated_home, self.SID)
        skill_dir = git_repo / "claude-skills" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("# test skill\n")
        subprocess.run(["git", "add", str(skill_md)], cwd=git_repo, check=True)
        write_skill_review_marker(isolated_home, git_repo, session_id=self.SID)
        # Unstaged change to file.txt, outside the SKILL.md pathspec.
        (git_repo / "file.txt").write_text("first\nsecond\nthird\n")
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "skill-review: live" in result.stdout
        assert "skill-review reconciliation flag" not in result.stdout

    def test_ready_for_review_never_carries_a_reconciliation_flag(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        marker = _ready_for_review_marker_path(isolated_home, git_repo, self.SID)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(git_repo) + "\n")
        (git_repo / "file.txt").write_text("first\nsecond\nthird\n")
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "ready-for-review: live" in result.stdout
        assert "reconciliation flag" not in result.stdout

    def test_plan_review_never_carries_a_reconciliation_flag(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        plans_dir = git_repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "p.md").write_text("# plan\n")
        write_plan_review_marker(isolated_home, git_repo, self.SID)
        result = _run(["status"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "plan-review: live" in result.stdout
        assert "reconciliation flag" not in result.stdout


class TestMarkerScriptCheck:
    """`marker.sh check code-review` reports, without writing anything,
    whether an existing code-review marker already matches the current
    staged diff -- the short-circuit `/code-review`'s Step 0.1 consults
    before dispatching its specialist panel."""

    SID = "test-session-check"

    def test_match_when_marker_hash_equals_current_staged_diff(self, isolated_home, git_repo):
        _seed_session(isolated_home, self.SID)
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().startswith("match")

    def test_no_match_when_no_marker_exists(self, isolated_home, git_repo):
        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")

    def test_no_match_when_marker_hash_is_stale(self, isolated_home, git_repo):
        write_marker(isolated_home, git_repo, "0" * 64, session_id=self.SID)
        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")

    def test_no_match_when_hash_matches_but_marker_is_older_than_the_age_bound(
        self, isolated_home, git_repo
    ):
        """A marker past the default 24h freshness bound must read as
        no-match even though its hash still matches the current staged diff
        -- an old marker doesn't prove the diff was reviewed recently enough
        to trust as a skip-review signal (docs/design-decisions.md)."""
        marker = write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        stale_time = time.time() - 86400 - 60  # just past the 24h default
        os.utime(marker, (stale_time, stale_time))
        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")

    def test_check_max_age_env_override_narrows_the_freshness_bound(
        self, isolated_home, git_repo
    ):
        """CODE_REVIEW_CHECK_MAX_AGE_SECONDS overrides the 24h default -- a
        marker within the default window but past the lowered override must
        still read as no-match."""
        marker = write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        old_time = time.time() - 120
        os.utime(marker, (old_time, old_time))
        result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"CODE_REVIEW_CHECK_MAX_AGE_SECONDS": "60"},
        )
        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")

    def test_freshness_bound_is_a_strict_less_than_not_at_or_under(
        self, isolated_home, git_repo
    ):
        """`_code_review_marker_fresh_age` uses `age -lt max_age_seconds` --
        a marker at or past the bound must read as stale (no-match), while
        comfortably under it must still read as fresh (match). Uses an
        overridden bound with a slack margin wide enough to absorb the
        subprocess-invocation latency between `os.utime` and marker.sh's own
        `date +%s` read (a 1-second margin against the wall clock is not
        reliable across a real subprocess call). This mirrors
        nudge-long-turn-subagent.sh's own exact-boundary threshold test for
        its sibling malformed-value guard shape."""
        bound_seconds = 120
        under_bound_marker = write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        under_time = time.time() - bound_seconds + 30  # comfortably under the bound
        os.utime(under_bound_marker, (under_time, under_time))
        under_result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"CODE_REVIEW_CHECK_MAX_AGE_SECONDS": str(bound_seconds)},
        )
        assert under_result.returncode == 0, under_result.stderr
        assert under_result.stdout.strip().startswith("match")

        at_bound_marker = write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        at_time = time.time() - bound_seconds  # age >= bound_seconds by check time
        os.utime(at_bound_marker, (at_time, at_time))
        at_result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"CODE_REVIEW_CHECK_MAX_AGE_SECONDS": str(bound_seconds)},
        )
        assert at_result.returncode == 1
        assert at_result.stdout.strip().startswith("no-match")

    def test_max_age_override_can_widen_the_bound_not_only_narrow_it(
        self, isolated_home, git_repo
    ):
        """CODE_REVIEW_CHECK_MAX_AGE_SECONDS must be read on the passing
        side too, not only to narrow the bound (the narrowing direction is
        already covered above) -- a marker past the 24h default that's
        still within a widened override must match."""
        marker = write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        thirty_hours_ago = time.time() - (30 * 3600)
        os.utime(marker, (thirty_hours_ago, thirty_hours_ago))
        result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"CODE_REVIEW_CHECK_MAX_AGE_SECONDS": str(40 * 3600)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().startswith("match")

    def test_freshness_scan_finds_a_fresh_marker_behind_a_stale_one(
        self, isolated_home, git_repo
    ):
        """Two markers can share the same repo-hash prefix and the same
        hash matching the current staged diff -- one from an earlier
        session past the freshness bound, one from a later session still
        within it. _code_review_marker_fresh_age's candidate loop must keep
        scanning past the stale match rather than stopping at the first
        candidate it encounters. The stale session id sorts before the
        fresh one so a premature-return bug would surface here as a false
        no-match."""
        diff_hash = staged_diff_hash(git_repo)
        stale_marker = write_marker(isolated_home, git_repo, diff_hash, session_id="aaa-stale-session")
        stale_time = time.time() - 86400 - 60  # just past the 24h default
        os.utime(stale_marker, (stale_time, stale_time))

        fresh_marker = write_marker(isolated_home, git_repo, diff_hash, session_id="zzz-fresh-session")
        fresh_time = time.time() - 120
        os.utime(fresh_marker, (fresh_time, fresh_time))

        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().startswith("match")
        age = int(result.stdout.strip().split("age_seconds=")[1])
        assert age < 130, f"expected the fresh marker's age (~120s), got {age}"

    @pytest.mark.parametrize("malformed_value", ["", "0", "not-a-number", "0340", "999999999"])
    def test_malformed_max_age_override_falls_back_to_the_24h_default(
        self, isolated_home, git_repo, malformed_value
    ):
        """A malformed CODE_REVIEW_CHECK_MAX_AGE_SECONDS (empty, literal
        zero, non-digit, zero-padded, or 9+ digits) must fall back to the
        24h default rather than degrading toward 0 (every marker reads
        stale) or an unbounded value (every marker reads fresh) -- pinned
        by checking both sides of the real 24h boundary under the same
        malformed override."""
        fresh_marker = write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        fresh_time = time.time() - 86400 + 120  # just under the 24h default
        os.utime(fresh_marker, (fresh_time, fresh_time))
        fresh_result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"CODE_REVIEW_CHECK_MAX_AGE_SECONDS": malformed_value},
        )
        assert fresh_result.returncode == 0, fresh_result.stderr
        assert fresh_result.stdout.strip().startswith("match")

        stale_marker = write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        stale_time = time.time() - 86400 - 120  # just over the 24h default
        os.utime(stale_marker, (stale_time, stale_time))
        stale_result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"CODE_REVIEW_CHECK_MAX_AGE_SECONDS": malformed_value},
        )
        assert stale_result.returncode == 1
        assert stale_result.stdout.strip().startswith("no-match")

    def test_no_match_after_staged_diff_changes_past_the_marker(self, isolated_home, git_repo):
        """A marker recorded for the diff at write time must not still read
        as a match once the staged diff has moved on -- a false match here
        would silently skip a real review."""
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        (git_repo / "file.txt").write_text("first\nsecond\nthird\n")
        subprocess.run(["git", "add", "file.txt"], cwd=git_repo, check=True)
        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")

    def test_date_failure_reads_as_no_match_not_maximally_fresh(
        self, isolated_home, git_repo, tmp_path
    ):
        """If `date +%s` fails, `_code_review_marker_fresh_age` must return
        1 (no fresh marker) rather than let an empty $now turn the age
        arithmetic into a large negative number that trivially passes the
        freshness bound -- a fail-open here would silently skip a real
        review whenever `date` is unavailable or errors."""
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "date"
        stub.write_text("#!/bin/bash\nexit 1\n")
        stub.chmod(0o755)
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )
        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")

    def test_future_mtime_age_is_clamped_to_zero_not_negative(self, isolated_home, git_repo):
        """A marker whose mtime is in the future (clock skew, restore
        tooling) must clamp age to 0 and still report a match rather than
        surface a nonsensical negative age_seconds -- the clamp must not
        itself turn into a reject path that masks a hash match."""
        marker = write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        future_time = time.time() + 3600
        os.utime(marker, (future_time, future_time))
        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().startswith("match")
        assert "age_seconds=-" not in result.stdout

    def test_no_match_after_the_reviewed_change_is_committed(self, isolated_home, git_repo):
        """Committing advances HEAD and empties the index, so the staged
        diff `check` recomputes no longer equals what the marker recorded --
        a stale marker from before a commit must not read as a match."""
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        subprocess.run(["git", "commit", "-q", "-m", "reviewed change"], cwd=git_repo, check=True)
        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")

    @pytest.mark.timing
    def test_hash_computation_times_out_to_no_match(self, isolated_home, git_repo, tmp_path):
        """The `_hash_staged_diff` call `check` makes is capped via
        _lib_capped -- a stalled `git diff --cached` must not hang `check`
        indefinitely, and a killed call must fall through to no-match,
        never a false match. Mirrors `status`'s own
        test_code_review_value_computation_times_out_gracefully for the
        same underlying call."""
        if not shutil.which("timeout"):
            pytest.skip("timeout(1) not available — BSD/macOS without coreutils")
        real_git = shutil.which("git")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text(
            '#!/bin/bash\n'
            'if [ "$1" = "-C" ] && [ "$3" = "diff" ] && [ "$4" = "--cached" ] && [ "$#" -eq 4 ]; then\n'
            '  sleep 10\n'
            'fi\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        start = time.monotonic()
        result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")
        assert elapsed < 9.5, (
            f"expected the 5s _lib_capped timeout to fire (stub sleeps 10s if "
            f"it does not), took {elapsed:.1f}s"
        )

    @pytest.mark.timing
    def test_stat_stall_reads_as_no_match_not_a_hang(self, isolated_home, git_repo, tmp_path):
        """`_marker_mtime_epoch`'s own `stat -c%Y`/`stat -f%m` calls are each
        individually capped via `_lib_capped_for(5)` -- a stalled `stat` must
        not hang `check` indefinitely, and a killed call must fall through to
        no-match, never a false match on an unresolvable mtime."""
        if not shutil.which("timeout"):
            pytest.skip("timeout(1) not available — BSD/macOS without coreutils")
        real_stat = shutil.which("stat")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "stat"
        stub.write_text(f'#!/bin/bash\nsleep 10\nexec {real_stat} "$@"\n')
        stub.chmod(0o755)
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)

        start = time.monotonic()
        result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")
        # Both the GNU and BSD stat forms are individually capped at 5s and
        # tried in sequence, so the bounded worst case is ~10s, not a hang.
        assert elapsed < 14.5, (
            f"expected both 5s _lib_capped_for stat timeouts to fire (stub "
            f"sleeps 10s per call if they do not), took {elapsed:.1f}s"
        )

    @pytest.mark.timing
    def test_grep_stall_reads_as_no_match_not_a_hang(self, isolated_home, git_repo, tmp_path):
        """`_code_review_marker_fresh_age`'s own `grep -qFx` call is capped
        via `_lib_capped_for(5)` -- a stalled `grep` must not hang `check`
        indefinitely, and a killed call must fall through to no-match, never
        a false match on an unverified marker value. `_lib_marker_value_present`
        makes an earlier, identically-shaped `grep -qFx` call on the same
        marker file (the cheap common-case hash check `check` runs first), so
        the stub only stalls from the second `-qFx` invocation onward --
        isolating `_code_review_marker_fresh_age`'s own call."""
        if not shutil.which("timeout"):
            pytest.skip("timeout(1) not available — BSD/macOS without coreutils")
        real_grep = shutil.which("grep")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "grep"
        invocation_counter = tmp_path / "grep-qFx-invocation-count"
        stub.write_text(
            '#!/bin/bash\n'
            'if [ "$1" = "-qFx" ]; then\n'
            f'  count=$(( $(cat {invocation_counter} 2>/dev/null || echo 0) + 1 ))\n'
            f'  printf "%s" "$count" > {invocation_counter}\n'
            '  if [ "$count" -ge 2 ]; then\n'
            '    sleep 10\n'
            '  fi\n'
            'fi\n'
            f'exec {real_grep} "$@"\n'
        )
        stub.chmod(0o755)
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)

        start = time.monotonic()
        result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")
        assert elapsed < 9.5, (
            f"expected the 5s _lib_capped_for grep timeout to fire (stub "
            f"sleeps 10s if it does not), took {elapsed:.1f}s"
        )

    def test_only_one_git_diff_invocation_on_the_check_path(
        self, isolated_home, git_repo, tmp_path
    ):
        """`check code-review` makes exactly one git call: `_hash_staged_diff`'s
        `git diff --cached`. A second, separately-timed git call would
        reintroduce a window where the two calls could observe different
        index states under contention and produce a false match. A stub that
        would hang on a `--quiet`-shaped invocation but answer normally
        otherwise proves no `--quiet` call is made on this path. The
        invocation log's single `diff --cached`-shaped line proves
        `_hash_staged_diff` is only called once. A marker matching the real
        staged diff is written first so the run also exercises the match
        branch through this single-call path, rather than passing vacuously
        the way an unconditional no-match would."""
        if not shutil.which("timeout"):
            pytest.skip("timeout(1) not available — BSD/macOS without coreutils")
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        real_git = shutil.which("git")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        invocation_log = tmp_path / "git-invocation-args"
        stub.write_text(
            '#!/bin/bash\n'
            f'echo "$@" >> {invocation_log}\n'
            'if [ "$1" = "-C" ] && [ "$3" = "diff" ] && [ "$4" = "--cached" ] && [ "$5" = "--quiet" ]; then\n'
            '  sleep 10\n'
            'fi\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        result = _run(
            ["check", "code-review"],
            cwd=git_repo,
            home=isolated_home,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().startswith("match")
        invocations = invocation_log.read_text().splitlines() if invocation_log.exists() else []
        assert not any("--quiet" in line for line in invocations), (
            f"expected no --quiet-shaped git invocation on the check path, got: {invocations}"
        )
        diff_cached_invocations = [
            line
            for line in invocations
            if re.match(r"^-C \S+ diff --cached$", line)
        ]
        assert len(diff_cached_invocations) == 1, (
            f"expected exactly one `-C <root> diff --cached`-shaped git "
            f"invocation on the check path, got: {invocations}"
        )

    def test_no_match_on_empty_staged_diff_even_if_marker_matches_empty_hash(
        self, isolated_home, git_repo
    ):
        """Empty staged diff must never read as a match. `ready-for-review`'s
        step 3 invokes `/code-review` with nothing staged (it reviews the
        cumulative PR diff instead), and a marker some earlier empty-diff
        review left behind must not falsely short-circuit that review."""
        subprocess.run(
            ["git", "commit", "-q", "-m", "land the fixture's staged change"],
            cwd=git_repo,
            check=True,
        )
        empty_diff_hash = hashlib.sha256(b"").hexdigest()
        write_marker(isolated_home, git_repo, empty_diff_hash, session_id=self.SID)
        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 1
        assert result.stdout.strip().startswith("no-match")

    def test_check_requires_no_session_file(self, isolated_home, git_repo):
        """check is read-only and never resolves a session id, unlike
        write/activate/deactivate -- it must not exit 2 for a missing
        session file the way TestMarkerScriptSessionMissing pins for those."""
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=self.SID)
        result = _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 0, result.stderr

    def test_check_writes_no_marker(self, isolated_home, git_repo):
        marker_dir = isolated_home / ".claude" / "code-review-markers"
        _run(["check", "code-review"], cwd=git_repo, home=isolated_home)
        assert list(marker_dir.iterdir()) == []

    def test_check_missing_skill_argument_exits_2(self, isolated_home, git_repo):
        result = _run(["check"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2

    def test_check_unsupported_skill_exits_2(self, isolated_home, git_repo):
        result = _run(["check", "skill-review"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2
        assert "code-review" in result.stderr

    def test_check_code_review_extra_arg_exits_2(self, isolated_home, git_repo):
        result = _run(["check", "code-review", "extra"], cwd=git_repo, home=isolated_home)
        assert result.returncode == 2


class TestMarkerScriptArgumentGrammarIsPositional:
    """Regression guard for an invariant enforce-marker-script-shape.sh's
    gate-release-authority arm depends on: marker.sh's argument grammar is
    strictly positional (`marker.sh <subcommand> [<skill>]`). Only
    -h/--help is accepted in $1 as an exception to that grammar. A future
    value-taking flag ahead of the subcommand would let a command like
    `marker.sh --config x write code-review` evade both of that hook's
    detectors. It evades the raw-substring check because `marker.sh` and
    `write` are no longer text-adjacent. It evades the command-word check
    because the flag's value would occupy the subcommand-word slot."""

    @pytest.mark.parametrize(
        "flag_args",
        [
            pytest.param(["--bogus"], id="bare_flag"),
            pytest.param(["--config", "x"], id="flag_with_value"),
        ],
    )
    def test_flag_shaped_first_arg_rejected_as_unknown_subcommand(
        self, isolated_home, git_repo, flag_args
    ):
        result = _run(flag_args, cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, (
            f"marker.sh {' '.join(flag_args)} must be rejected -- a "
            f"flag-shaped $1 is not a recognized subcommand, got "
            f"{result.returncode}. stderr: {result.stderr!r}"
        )
        assert f"unknown subcommand '{flag_args[0]}'" in result.stderr, (
            f"marker.sh must reject {flag_args[0]!r} via the same "
            f"unrecognized-subcommand path as any other bogus $1, not "
            f"consume it as a global flag; stderr: {result.stderr!r}"
        )

    def test_four_word_flag_and_subcommand_shape_hits_usage_not_dispatch(
        self, isolated_home, git_repo
    ):
        """This is the attack shape named in the class docstring:
        `marker.sh --config x write code-review`. Because it has 4 args, it
        hits marker.sh's `$# -gt 2` arg-count cap and usage() -- not the
        unknown-subcommand path the 1-2-word cases above hit."""
        flag_args = ["--config", "x", "write", "code-review"]
        result = _run(flag_args, cwd=git_repo, home=isolated_home)
        assert result.returncode == 2, (
            f"marker.sh {' '.join(flag_args)} must be rejected -- 4 args "
            f"exceeds marker.sh's 2-arg cap, got {result.returncode}. "
            f"stderr: {result.stderr!r}"
        )
        assert "Usage:" in result.stderr, (
            f"marker.sh {' '.join(flag_args)} must hit the usage() path "
            f"via the arg-count cap, not any subcommand-dispatch path; "
            f"stderr: {result.stderr!r}"
        )
