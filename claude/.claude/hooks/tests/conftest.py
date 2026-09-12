"""Shared pytest fixtures auto-discovered across the hook test suite.

Only fixtures used by two or more test files live here. Class-local
fixtures stay with their class in the per-hook test file. `_seed_session`
is a plain helper function rather than a fixture (matching
scripts/tests/conftest.py's convention) since callers need to pass a
per-test session id, not a fixed injected value; import it directly with
`from .conftest import _seed_session`.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from helpers import HOOKS_DIR

# Shim sleep duration for the git/gh-timeout regression tests below: long
# enough that a broken (uncapped) call site never returns before the
# test's own timeout.
TIMEOUT_SHIM_SLEEP_SECONDS = 10
# Lower bound for asserting the 5s _lib_capped cap actually engaged.
# Below the 5s cap, so cap-plus-overhead reliably clears it. Well above 0,
# so a no-op shim (never invoked, or invoked without sleeping) can't pass
# by accident.
CAP_ENGAGED_FLOOR_SECONDS = 4


def _seed_session(home: Path, session_id: str, pid: int | None = None) -> None:
    """Write $HOME/.claude/sessions/<pid> in the two-line format
    capture-session-id.sh writes: the session id, then that pid's
    `TZ=UTC LC_ALL=C ps -o lstart=` start time. The start time is captured
    the same way the writer's `$(...)` does — trailing newline stripped,
    other trailing whitespace preserved — so a seeded entry round-trips
    through marker.sh's _walk_session comparison exactly like a real
    capture-session-id.sh write would.

    pid defaults to this test process's own pid: marker.sh resolves its
    session id by walking process ancestors, and when it runs as a
    subprocess of pytest, that walk reaches the pytest process itself.
    """
    target_pid = os.getpid() if pid is None else pid
    sessions_dir = home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    start_time = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(target_pid)],
        env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.rstrip("\n")
    (sessions_dir / str(target_pid)).write_text(f"{session_id}\n{start_time}\n")


def _dead_pid() -> int:
    """Return a pid that is guaranteed not to be running. Mirrors
    scripts/tests/conftest.py's helper of the same name, duplicated rather
    than shared so neither test tree's conftest imports the other's —
    spawns and reaps a real process so the returned pid is a genuine,
    just-exited one rather than a guessed high number that might collide
    with something still alive."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.fixture
def live_pid():
    """Yield the pid of a real process this fixture owns for the test's
    duration, terminated on teardown. A safe stand-in for "some other live
    session" in a foreign-live-lock test — unlike os.getppid(), it doesn't
    depend on this test process's own ancestry staying stable for the
    assertion window, which a detached or reparented test-runner process
    supervision setup can't guarantee (see _dead_pid's analogous
    genuine-process-over-guessed-number rationale)."""
    proc = subprocess.Popen(["sleep", "3600"])
    try:
        yield proc.pid
    finally:
        proc.terminate()
        proc.wait()


def _worktree_lock_reason(worktree: Path) -> str | None:
    """Return the `locked <reason>` porcelain line for `worktree`, or None
    when it is unlocked. Test-only companion to _lib_worktree_lock_pid
    (_lib.sh), which parses the same `git worktree list --porcelain` text
    inside the hook; kept independent (not shelling out to the library
    function) so a test using this to assert lock state isn't checking the
    function under test against itself."""
    porcelain = subprocess.run(
        ["git", "-C", str(worktree), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    in_target = False
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            in_target = line[len("worktree "):] == str(worktree)
        elif line.startswith("locked") and in_target:
            return line
    return None


def _write_conditional_sleep_shim(
    bin_dir: Path,
    binary_name: str,
    real_binary: str,
    match_condition: str,
    fake_output: str | None = None,
    sleep_seconds: int = TIMEOUT_SHIM_SLEEP_SECONDS,
) -> None:
    """Write a fake `binary_name` under bin_dir that sleeps sleep_seconds
    past the cap under test when `match_condition` matches, and execs
    `real_binary` otherwise. Shared conditional-sleep logic behind
    git_timeout_shim and gh_timeout_shim. sleep_seconds defaults to
    TIMEOUT_SHIM_SLEEP_SECONDS (calibrated for the shared 5s _lib_capped
    cap); a caller testing a wider cap (e.g. _lib_cumulative_diff_hash's 15s)
    must pass a larger value or the shim returns before that cap fires.

    When `fake_output` is set, it replaces the exec-real-binary fallback
    after the sleep completes, so a broken cap is observably distinct from
    a working one instead of both converging on the real binary's result."""
    post_sleep = (
        f"  echo {shlex.quote(fake_output)}\n  exit 0\n" if fake_output is not None else ""
    )
    fake_binary = bin_dir / binary_name
    fake_binary.write_text(
        f"#!/bin/bash\n"
        f"if {match_condition}; then\n"
        f"  sleep {sleep_seconds}\n"
        f"{post_sleep}"
        f"fi\n"
        f'exec {real_binary} "$@"\n'
    )
    fake_binary.chmod(0o755)


@pytest.fixture
def git_timeout_shim(tmp_path):
    """`install(match_condition)` writes a `git` shim that sleeps past the 5s
    _lib_capped cap when `match_condition` matches, execs the real binary
    otherwise, and returns a PATH-override dict. Shared by
    test_deny_pii_in_commits.py and test_require_ready_for_review.py.

    `match_condition` is a `[ ... ]`/`[[ ... ]]` test expression, e.g.
    `[ "$1" = "diff" ]` or `[ "$1" = "rev-parse" ] && [ "$2" = "HEAD" ]`. A
    `$N`-pinned predicate is coupled to the target call site's argv shape
    (e.g. a `-C <dir>` prefix shifts every position) and must be re-audited
    whenever that call site's argv shape changes.

    Skips when `git` is absent, or when neither `timeout(1)` nor
    `gtimeout(1)` is available (stock macOS ships neither without Homebrew
    coreutils).

    The PATH dict is built inside `install`, not at fixture setup, so it
    reads `os.environ["PATH"]` at the test's own call time. A caller that
    prepends its own bin dir via `monkeypatch.setenv` beforehand (e.g.
    fake_gh_pr_exists) must call `monkeypatch.setenv` before calling
    `install`, so that ordering is preserved.

    `install`'s optional `fake_output` passes through to
    _write_conditional_sleep_shim, for a call site whose real, uncapped
    result would otherwise coincidentally match the timed-out result.

    `install`'s optional `sleep_seconds` also passes through, for a call
    site testing a cap wider than the default TIMEOUT_SHIM_SLEEP_SECONDS.
    """
    real_git = shutil.which("git")
    if not real_git:
        pytest.skip("git not found in PATH")
    if not shutil.which("timeout") and not shutil.which("gtimeout"):
        pytest.skip("neither timeout(1) nor gtimeout(1) available — BSD/macOS without coreutils")

    def install(
        match_condition: str,
        fake_output: str | None = None,
        sleep_seconds: int = TIMEOUT_SHIM_SLEEP_SECONDS,
    ) -> dict[str, str]:
        _write_conditional_sleep_shim(
            tmp_path, "git", real_git, match_condition, fake_output, sleep_seconds
        )
        return {"PATH": f"{tmp_path}:{os.environ['PATH']}"}

    return install


@pytest.fixture
def gh_timeout_shim(tmp_path):
    """`install(match_condition)` writes a `gh` shim with the same
    conditional-sleep contract as git_timeout_shim, for regression tests
    against require-ready-for-review.sh's `gh pr view` call. Same skip
    conditions as git_timeout_shim, checking `gh` in place of `git`.

    `install`'s optional `fake_output` passes through to
    _write_conditional_sleep_shim, for a call site whose real, uncapped
    result would otherwise coincidentally match the timed-out result.

    `install`'s optional `sleep_seconds` also passes through, for a call
    site testing a cap wider than the default TIMEOUT_SHIM_SLEEP_SECONDS.
    """
    real_gh = shutil.which("gh")
    if not real_gh:
        pytest.skip("gh not found in PATH")
    if not shutil.which("timeout") and not shutil.which("gtimeout"):
        pytest.skip("neither timeout(1) nor gtimeout(1) available — BSD/macOS without coreutils")

    def install(
        match_condition: str,
        fake_output: str | None = None,
        sleep_seconds: int = TIMEOUT_SHIM_SLEEP_SECONDS,
    ) -> dict[str, str]:
        _write_conditional_sleep_shim(
            tmp_path, "gh", real_gh, match_condition, fake_output, sleep_seconds
        )
        return {"PATH": f"{tmp_path}:{os.environ['PATH']}"}

    return install


@contextmanager
def assert_cap_engaged(floor: float = CAP_ENGAGED_FLOOR_SECONDS):
    """Time the wrapped block and assert it took longer than `floor` --
    evidence some _lib_capped/_lib_capped_for timeout fired rather than,
    say, the shim never being invoked at all. Deliberately no upper bound:
    under `-n auto` parallel load, a passing run can take arbitrarily
    longer than the shim's own sleep duration without the cap having
    failed to engage.

    `floor` defaults to CAP_ENGAGED_FLOOR_SECONDS (calibrated for the
    shared 5s _lib_capped cap); a caller testing a wider cap (e.g.
    _lib_cumulative_diff_hash's 15s) must pass a higher floor, or a
    regression to the shared 5s cap would still clear the default floor
    and pass undetected."""
    start = time.monotonic()
    yield
    elapsed = time.monotonic() - start
    assert elapsed > floor, (
        f"expected a capped timeout to fire (the installed shim sleeps past "
        f"it if the cap does not), took only {elapsed:.1f}s"
    )


@pytest.fixture(autouse=True)
def _clear_claude_pid_env(monkeypatch):
    """capture-session-id.sh accepts $CLAUDE_PID from its own environment. A
    real Claude Code session running this suite exports it, and the test
    runners below inherit the parent environment wholesale — without this,
    that real value would silently satisfy every test exercising the $PPID
    fallback, masking it. Absent on CI; raising=False matches the
    isolated_home precedent below for a var that may not be set.
    """
    monkeypatch.delenv("CLAUDE_PID", raising=False)


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Sandbox $HOME so the hooks' marker files don't collide with real state."""
    home = tmp_path / "home"
    (home / ".claude" / "code-review-markers").mkdir(parents=True)
    hooks_dir = home / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "_lib.sh").symlink_to(HOOKS_DIR / "_lib.sh")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


@pytest.fixture
def git_repo(tmp_path):
    """Fresh git repo with one committed file and one staged change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "file.txt").write_text("first\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / "file.txt").write_text("first\nsecond\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    return repo


@pytest.fixture
def opted_in_repo(tmp_path):
    """Git repo with .claude/worktree-required committed (opted into
    worktree enforcement)."""
    repo = tmp_path / "opted-in"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "worktree-required").write_text("# sentinel\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def stray_marker_repo(tmp_path):
    """Git repo with .claude/worktree-required present but NOT committed or
    staged — the GH-427 scenario. Enforcement still activates (existence-based
    check, unchanged), but the deny message should carry the stray-marker
    hint since a tracked-marker repo would not."""
    repo = tmp_path / "stray-marker"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "worktree-required").write_text("# stray, untracked\n")
    return repo


@pytest.fixture
def staged_marker_repo(tmp_path):
    """Git repo with .claude/worktree-required staged (git add) but not yet
    committed. _lib_stray_marker_hint uses `git ls-files --error-unmatch`,
    which succeeds for staged-not-committed files, not just committed ones —
    this fixture exercises that middle state so the hint's actual gate
    (index-tracked, not HEAD-committed) is what tests pin down."""
    repo = tmp_path / "staged-marker"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "worktree-required").write_text("# staged, not committed\n")
    subprocess.run(["git", "add", ".claude/worktree-required"], cwd=repo, check=True)
    return repo


@pytest.fixture
def non_opted_repo(tmp_path):
    """Git repo without the sentinel — enforcement should be a no-op."""
    repo = tmp_path / "non-opted"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def user_marker_home(isolated_home):
    """Sandboxed $HOME with ~/.claude/worktree-required present.
    Builds on isolated_home so $HOME is set to a temp dir; this fixture
    adds the machine-level marker file. The assertion verifies the write
    succeeded so a typo in the path cannot yield a silently-inert marker.
    """
    marker = isolated_home / ".claude" / "worktree-required"
    marker.write_text("# machine-level sentinel\n")
    assert marker.exists(), f"user_marker_home: marker not written at {marker}"
    return isolated_home


@pytest.fixture
def repo_with_optout(tmp_path):
    """Git repo with .claude/worktree-optout present (but no .claude/worktree-required).
    Used to verify opt-out is an inert modulator, not a trigger."""
    repo = tmp_path / "optout-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "worktree-optout").write_text("# opt-out\n")
    return repo


@pytest.fixture
def opted_in_with_worktree(opted_in_repo, tmp_path, isolated_home):
    """Opted-in repo with a linked worktree at a path that does NOT contain
    '/worktrees/' — verifies the hook's worktree check reads git-dir rather
    than pattern-matching the working-tree path. Seeds a session file so
    _lib_worktree_collision_guard can resolve this test process's own PID as
    the lock owner. Every write into this worktree runs the guard. A read
    runs it only when the worktree's lock is already present."""
    wt_path = tmp_path / "feature-tree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature", str(wt_path)],
        cwd=opted_in_repo,
        check=True,
    )
    _seed_session(isolated_home, "opted-in-with-worktree-session")
    return opted_in_repo, wt_path
