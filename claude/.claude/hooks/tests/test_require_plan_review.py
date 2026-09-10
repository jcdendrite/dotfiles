"""Tests for require-plan-review.sh."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from helpers import (
    CLAUDE_DIR,
    HOOKS_DIR,
    SCRIPTS_DIR,
    SKILLS_DIR,
    assert_gate_handles_traversal_session_id,
    bare_remote_with_default_branch,
    bash_input,
    build_conflicted_revert,
    edit_input,
    exitplanmode_input,
    extract_skill_command,
    multiedit_input,
    plan_review_marker_path,
    push_conflicting_edit_to_origin,
    run_hook,
    run_hook_reason,
    run_skill_command,
    write_input,
    write_plan_review_marker,
)

from .conftest import _seed_session

PLAN_REVIEW_SKILL = SKILLS_DIR / "plan-review" / "SKILL.md"

REQUIRE_PLAN_REVIEW_HOOK = HOOKS_DIR / "require-plan-review.sh"

# Forces _lib_realpath_m's manual fallback branch by shadowing both native
# `realpath -m` and `grealpath` on PATH -- mirrors test_lib.py's
# _FORCED_FALLBACK_REALPATH_SHIM, prepended rather than substituted for PATH
# so jq/git/sha256sum/timeout stay resolvable for the rest of the hook.
_FORCED_FALLBACK_REALPATH_SHIM = textwrap.dedent("""\
    #!/bin/bash
    if [ "$1" = "-m" ]; then
      echo "realpath: illegal option -- m" >&2
      exit 1
    fi
    exec /bin/realpath "$@"
""")
_FORCED_FALLBACK_GREALPATH_SHIM = textwrap.dedent("""\
    #!/bin/bash
    exit 1
""")


@pytest.fixture
def plan_review_repo(tmp_path):
    """Git repo with .claude/plans/ populated.

    The gate is globally applied — no opt-in required.
    """
    repo = tmp_path / "plan-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    plans_dir = repo / ".claude" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "impl-plan.md").write_text("# Implementation plan\n\nStep 1...\n")
    return repo


@pytest.fixture
def plan_review_home(isolated_home):
    """Isolated $HOME with the plan-review-markers directory pre-created."""
    (isolated_home / ".claude" / "plan-review-markers").mkdir(parents=True, exist_ok=True)
    return isolated_home


class TestRequirePlanReview:
    def test_plan_exists_no_marker_denies_write(self, plan_review_repo, plan_review_home):
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "test-session-prt"},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_plan_exists_no_marker_denies_edit(self, plan_review_repo, plan_review_home):
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**edit_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "test-session-prt"},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_plan_exists_with_marker_allows_write(self, plan_review_repo, plan_review_home):
        """Target must be in-repo: an out-of-repo path like /tmp/foo.py would
        also allow via the scope filter's out-of-repo exemption regardless of
        whether the marker's hash matched, masking the marker check."""
        sid = "test-session-prt-allowed"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_plan_exists_with_marker_allows_edit(self, plan_review_repo, plan_review_home):
        """In-repo target for the same reason as the Write case above."""
        sid = "test-session-prt-allowed-edit"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**edit_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_other_sessions_marker_authorizes_at_matching_plan_hash(
        self, plan_review_repo, plan_review_home
    ):
        """A marker written by session A releases session B's gate at the same plan hash.

        The stored hash is the authorization: it proves the review covered
        exactly this plan state. The filename's session suffix only keeps
        parallel writers from clobbering each other. Reading it as a predicate
        is what made a resumed session (new session_id) re-arm a gate with no
        review actually missing.
        """
        write_plan_review_marker(plan_review_home, plan_review_repo, "session-A")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "session-B"},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_other_sessions_marker_does_not_authorize_a_changed_plan(
        self, plan_review_repo, plan_review_home
    ):
        """The negative half: cross-session acceptance is by hash, not by existence.

        Session A's marker must not release session B once the plan text has
        changed — otherwise dropping the session key would degrade the gate to
        an existence check.
        """
        write_plan_review_marker(plan_review_home, plan_review_repo, "session-A")
        (plan_review_repo / ".claude" / "plans" / "impl-plan.md").write_text("edited after review\n")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "session-B"},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_no_plans_dir_allows(self, tmp_path):
        """No .claude/plans/ directory → gate is inactive."""
        repo = tmp_path / "no-plans"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                write_input("/tmp/foo.py"),
                cwd=repo,
            )
            == "allow"
        )

    def test_empty_plans_dir_allows(self, tmp_path):
        """Empty .claude/plans/ directory → no plans present, gate inactive."""
        repo = tmp_path / "empty-plans"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / ".claude" / "plans").mkdir(parents=True)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                write_input("/tmp/foo.py"),
                cwd=repo,
            )
            == "allow"
        )

    def test_historical_plans_allows_and_skips_the_realpath_fast_path(self, tmp_path):
        """.claude/plans/ holding only committed, unmodified (historical)
        content and zero active files is this repo's own steady state --
        every plan file here is committed and unmodified (`git status
        --porcelain -- .claude/plans/` is empty). The guard must skip the
        realpath-resolution block for this population, not just for an
        absent or empty plans directory (test_no_plans_dir_allows /
        test_empty_plans_dir_allows above). Proven by shimming `realpath`
        to record its own invocation: the block calls _lib_realpath_m,
        which tries native `realpath -m` first, so any call to the shim
        means the block ran."""
        repo = tmp_path / "historical-plans"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        plans_dir = repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "shipped-plan.md").write_text("# Shipped plan\n\nAlready reviewed and merged.\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed historical plan"], cwd=repo, check=True)

        real_realpath = shutil.which("realpath")
        assert real_realpath, "test host must have a real realpath binary on PATH"
        call_marker = tmp_path / "realpath-was-called"
        shim_dir = tmp_path / "call_counting_shim"
        shim_dir.mkdir()
        shim_script = textwrap.dedent(f"""\
            #!/bin/bash
            touch "{call_marker}"
            exec "{real_realpath}" "$@"
        """)
        (shim_dir / "realpath").write_text(shim_script)
        (shim_dir / "realpath").chmod(0o755)

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                write_input(str(repo / "src" / "foo.py")),
                cwd=repo,
                extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
            )
            == "allow"
        )
        assert not call_marker.exists(), (
            "realpath was invoked even though .claude/plans/ holds only historical "
            "content -- the fast-path guard failed to skip the target-scope block"
        )

    def test_historical_plans_calls_active_plan_files_only_once(self, tmp_path):
        """The fast-path guard's own _lib_active_plan_files call must not be
        followed by a second, redundant call from the hash computation below
        it -- the guard must exit 0 immediately once it has already learned
        nothing is active, rather than falling through and re-deriving the
        identical empty result via a second enumeration. Proven by shimming
        `git` to log every `ls-files --others` invocation, the call
        _lib_active_plan_files makes exactly once per call to itself, and
        asserting it appears exactly once for this single hook invocation."""
        repo = tmp_path / "historical-plans-call-count"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        plans_dir = repo / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "shipped-plan.md").write_text("# Shipped plan\n\nAlready reviewed and merged.\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed historical plan"], cwd=repo, check=True)

        real_git = shutil.which("git")
        assert real_git, "test host must have a real git binary on PATH"
        call_log = tmp_path / "ls-files-others-call-log"
        stub_dir = tmp_path / "call_counting_git_stub"
        stub_dir.mkdir()
        stub_script = textwrap.dedent(f"""\
            #!/bin/bash
            has_ls_files=0
            has_others=0
            for arg in "$@"; do
              [ "$arg" = "ls-files" ] && has_ls_files=1
              [ "$arg" = "--others" ] && has_others=1
            done
            if [ "$has_ls_files" = 1 ] && [ "$has_others" = 1 ]; then
              echo call >> "{call_log}"
            fi
            exec "{real_git}" "$@"
        """)
        (stub_dir / "git").write_text(stub_script)
        (stub_dir / "git").chmod(0o755)

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                write_input(str(repo / "src" / "foo.py")),
                cwd=repo,
                extra_env={"PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}"},
            )
            == "allow"
        )
        call_count = len(call_log.read_text().splitlines()) if call_log.exists() else 0
        assert call_count == 1, (
            f"expected _lib_active_plan_files' ls-files --others call exactly once, "
            f"got {call_count} -- the fast-path guard fell through to a redundant "
            "second call instead of short-circuiting on 'nothing active'"
        )

    def test_bash_tool_allows_always(self, plan_review_repo):
        """Bash tool calls are not gated — only Write and Edit."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                bash_input("git commit -m foo"),
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_agent_reviews_path_allows_without_marker(self, plan_review_repo, plan_review_home):
        """Writes to agent-reviews/ are exempt from the plan-review gate.

        Reviewer agents write findings to agent-reviews/ while a plan is in flight
        (e.g. code-review runs after plan-it). Blocking them forces a full-inline
        fallback that defeats the context-saving canary. The exemption is scoped to
        an exact prefix match — only paths under <repo>/agent-reviews/ pass through.
        """
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / "agent-reviews" / "staff-backend-engineer-123.md")),
                    "session_id": "test-session-canary-exempt",
                },
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_adjacent_path_not_exempt(self, plan_review_repo, plan_review_home):
        """Paths adjacent to agent-reviews/ are NOT exempt — only exact prefix match.

        The exemption comment states: "Exact prefix match only: foo-agent-reviews/
        does not satisfy this." Verify that invariant holds — a sibling directory
        whose name merely contains 'agent-reviews' still requires a plan-review marker.
        """
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / "foo-agent-reviews" / "test.md")),
                    "session_id": "test-session-adjacent-deny",
                },
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_agent_reviews_nested_path_not_exempt(self, plan_review_repo, plan_review_home):
        """A path with agent-reviews/ as a non-root-level component is NOT exempt.

        The exemption pattern is `<repo>/agent-reviews/*` — only paths whose first
        component after the repo root is literally `agent-reviews` match. A path like
        `<repo>/src/agent-reviews/file.md` does not match, preventing a glob-widening
        refactor from accidentally exempting arbitrary deep paths.
        """
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / "src" / "agent-reviews" / "file.md")),
                    "session_id": "test-session-nested-deny",
                },
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_agent_reviews_directory_itself_not_exempt(self, plan_review_repo, plan_review_home):
        """A write to the agent-reviews/ directory itself (no filename) is NOT exempt.

        The hook glob pattern is `agent-reviews/*` — a bare directory path with no
        trailing filename component does not match. The write falls through to the
        in-repo boundary check and is denied. This pins the exemption to file paths
        *under* the directory, not the directory path itself, guarding against a
        future glob-widening refactor that might accidentally widen the exemption.
        """
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / "agent-reviews")),
                    "session_id": "test-session-dir-itself-deny",
                },
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_agent_reviews_path_edit_allows_without_marker(self, plan_review_repo, plan_review_home):
        """Edit tool writes to agent-reviews/ are also exempt, not just Write.

        The exemption fires before the Write/Edit/MultiEdit gate — it applies to all
        three tool types equally. This test pins the exemption against a refactor that
        moves the check inside the Write-only branch.
        """
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **edit_input(str(plan_review_repo / "agent-reviews" / "staff-sdet-123.md")),
                    "session_id": "test-session-canary-exempt-edit",
                },
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_agent_reviews_path_multiedit_allows_without_marker(self, plan_review_repo, plan_review_home):
        """MultiEdit tool writes to agent-reviews/ are also exempt, not just Write.

        Mirrors test_agent_reviews_path_edit_allows_without_marker for the MultiEdit
        tool variant, completing the three-way Write/Edit/MultiEdit coverage established
        by the rest of this test suite.
        """
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **multiedit_input(str(plan_review_repo / "agent-reviews" / "ciso-reviewer-456.md")),
                    "session_id": "test-session-canary-exempt-multiedit",
                },
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_outside_git_repo_allows(self, tmp_path):
        """Outside a git repo, the hook cannot key a marker — allow through."""
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                write_input("/tmp/foo.py"),
                cwd=non_repo,
            )
            == "allow"
        )

    def test_no_session_id_in_input_reads_completion_marker(
        self, plan_review_repo, plan_review_home
    ):
        """A payload with no session_id still finds a marker covering this plan state.

        session_id is required only for the active-bypass marker, which asserts
        a per-process property ("a review is running right now"). The completion
        marker asserts a property of the plan state itself, so a payload that
        cannot be session-keyed is not thereby unreviewed.
        """
        write_plan_review_marker(plan_review_home, plan_review_repo, "some-other-session")
        # write_input() uses no session_id field.
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                write_input(str(plan_review_repo / "src" / "foo.py")),
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_no_session_id_and_no_matching_marker_denies(
        self, plan_review_repo, plan_review_home
    ):
        """Fail-closed still holds: no session_id and no covering review → deny.

        The gate now turns on the marker's content rather than on session
        identity, so this is the assertion that pins "unreviewed denies" —
        a missing session_id must not become an allow path.
        """
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                write_input(str(plan_review_repo / "src" / "foo.py")),
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_missing_file_path_denies(self, plan_review_repo, plan_review_home):
        """Missing file_path leaves TARGET_PATH empty, causing the scope-filter guard
        (`if [ -n "$TARGET_PATH" ]`) to be false — the hook skips both the
        agent-reviews/ exemption and the out-of-repo pass-through and falls through
        to the default emit_deny. The scope filter only ever adds allows (narrows the
        deny set), so skipping it when the path is unparseable can never wrongly
        allow — an empty or missing path fails closed. Parallel to
        test_no_session_id_in_input_denies: security controls must not silently allow
        on parse failure."""
        payload = {"tool_name": "Write", "tool_input": {}, "session_id": "session-no-path"}
        assert (
            run_hook(REQUIRE_PLAN_REVIEW_HOOK, payload, cwd=plan_review_repo) == "deny"
        )

    def test_deny_message_mentions_plan_review(self, plan_review_repo, plan_review_home):
        """Deny reason must reference /plan-review so the agent knows what to run."""
        reason = run_hook_reason(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "session-for-reason"},
            cwd=plan_review_repo,
        )
        assert reason is not None
        assert "/plan-review" in reason
        assert "plan-review-markers" in reason

    # -- Committed-clean plan suppression -----------------------------------
    # A plan file that is tracked and unmodified vs HEAD is historical —
    # its review shipped with the PR that committed it. It must not re-arm
    # the gate in a new session. Only untracked or modified plan files count
    # as active work.

    def test_committed_clean_plan_does_not_arm_gate(self, plan_review_repo, plan_review_home):
        """A plan file that is committed and unmodified vs HEAD does not arm the gate."""
        subprocess.run(["git", "add", ".claude/plans/impl-plan.md"], cwd=plan_review_repo, check=True)
        subprocess.run(["git", "commit", "-m", "plan"], cwd=plan_review_repo, check=True)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "session-committed-clean"},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_modified_committed_plan_arms_gate(self, plan_review_repo, plan_review_home):
        """A plan file that is committed but modified on disk still arms the gate."""
        subprocess.run(["git", "add", ".claude/plans/impl-plan.md"], cwd=plan_review_repo, check=True)
        subprocess.run(["git", "commit", "-m", "plan"], cwd=plan_review_repo, check=True)
        (plan_review_repo / ".claude" / "plans" / "impl-plan.md").write_text(
            "# Implementation plan\n\nStep 1...\n\nStep 2 (added)...\n"
        )
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "session-modified-committed"},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_one_committed_one_uncommitted_plan_arms_gate(self, plan_review_repo, plan_review_home):
        """If any plan file is untracked, the gate arms even if another is committed-clean."""
        subprocess.run(["git", "add", ".claude/plans/impl-plan.md"], cwd=plan_review_repo, check=True)
        subprocess.run(["git", "commit", "-m", "plan"], cwd=plan_review_repo, check=True)
        (plan_review_repo / ".claude" / "plans" / "new-plan.md").write_text("# New plan\n")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "session-one-committed-one-not"},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    # -- Active-marker bypass -----------------------------------------------
    # Marker layout: ~/.claude/.plan-review-active.d/<session_id>.
    # The /plan-review skill writes one file per session at Step 0 and
    # removes it at the deactivation step. While fresh (<60 min), the hook
    # bypasses the gate so the skill's own Write/Edit calls don't self-deny.

    def test_fresh_active_marker_allows_write(self, plan_review_repo, plan_review_home):
        """Active marker with alive PID bypasses the gate for Write tool calls."""
        sid = "session-active-write"
        marker_dir = plan_review_home / ".claude" / ".plan-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / sid).write_text(str(os.getpid()))
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input("/tmp/foo.py"), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_active_marker_hit_advances_mtime(self, plan_review_repo, plan_review_home):
        """The hook is wired to the touch-refreshing wrapper, not the bare
        liveness predicate -- a live-but-idle-window-aged marker's mtime must
        advance on a gate hit, or a reverted call site would pass every
        allow/deny assertion in this file silently."""
        sid = "session-active-touch"
        marker_dir = plan_review_home / ".claude" / ".plan-review-active.d"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / sid
        marker.write_text(str(os.getpid()))
        old_time = time.time() - 300  # in-window, but old enough to detect a refresh
        os.utime(marker, (old_time, old_time))
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                # In-repo target, unlike the /tmp/foo.py shape other allow tests in
                # this file use -- an out-of-repo target disarms the gate before it
                # ever reaches the marker check, allowing without touching the marker.
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )
        assert marker.stat().st_mtime > old_time + 1, (
            "a gate hit against a live marker must refresh its mtime"
        )

    def test_fresh_active_marker_allows_edit(self, plan_review_repo, plan_review_home):
        sid = "session-active-edit"
        marker_dir = plan_review_home / ".claude" / ".plan-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / sid).write_text(str(os.getpid()))
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**edit_input("/tmp/foo.py"), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_fresh_active_marker_allows_multiedit(self, plan_review_repo, plan_review_home):
        sid = "session-active-multiedit"
        marker_dir = plan_review_home / ".claude" / ".plan-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / sid).write_text(str(os.getpid()))
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**multiedit_input("/tmp/foo.py"), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_plan_exists_no_marker_denies_multiedit(
        self, plan_review_repo, plan_review_home
    ):
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**multiedit_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "test-session-prt"},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_alive_pid_active_marker_bypasses(
        self, plan_review_repo, plan_review_home
    ):
        """Active marker whose stored PID is alive bypasses the gate."""
        sid = "session-alive-pid"
        marker_dir = plan_review_home / ".claude" / ".plan-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / sid).write_text(str(os.getpid()))
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_dead_pid_active_marker_evicts_and_denies(
        self, plan_review_repo, plan_review_home
    ):
        """Active marker whose stored PID is dead is evicted; gate denies."""
        sid = "session-dead-pid"
        marker_dir = plan_review_home / ".claude" / ".plan-review-active.d"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / sid
        marker.write_text("99999999")  # PID outside Linux/macOS max range → always dead
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        )
        assert not marker.exists(), "hook must evict the orphan marker on dead PID"

    def test_other_sessions_active_marker_does_not_bypass(
        self, plan_review_repo, plan_review_home
    ):
        """Per-session keying: session A's active marker must NOT bypass session B."""
        marker_dir = plan_review_home / ".claude" / ".plan-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / "session-A").touch()
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "session-B"},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    # -- Hostile session_id ---------------------------------------------------

    def test_traversal_session_id_denies_and_does_not_touch_marker_dir(
        self, plan_review_repo, plan_review_home
    ):
        """A session_id of '../canary' must not read through the traversal:
        ACTIVE_MARKER concatenates it into .plan-review-active.d/../canary,
        which resolves to a file one level up ($HOME/.claude/canary). The
        invalid id must skip the active-marker bypass entirely and fall
        through to the completion-marker check, which finds no match here
        and denies."""
        assert_gate_handles_traversal_session_id(
            REQUIRE_PLAN_REVIEW_HOOK,
            lambda sid: {
                **write_input(str(plan_review_repo / "src" / "foo.py")),
                "session_id": sid,
            },
            plan_review_home,
            expected_decision="deny",
            cwd=plan_review_repo,
        )

    # -- SKILL.md fixture alignment -----------------------------------------

    def test_skill_activate_command_creates_bypass_marker(
        self, plan_review_repo, plan_review_home
    ):
        """Run the SKILL.md activate-gate recipe; verify the resulting marker
        authorizes a previously-gated Write."""
        sid = "session-skill-activate"
        _seed_session(plan_review_home, sid)

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        ), "precondition: Write must be gated before activation"

        run_skill_command(
            extract_skill_command(PLAN_REVIEW_SKILL, "activate-gate"),
            cwd=plan_review_repo,
            isolated_home=plan_review_home,
        )

        marker = plan_review_home / ".claude" / ".plan-review-active.d" / sid
        assert marker.exists(), (
            "SKILL.md activate-gate recipe ran but no marker landed at the path "
            "the hook checks — skill and hook disagree on layout."
        )
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input("/tmp/foo.py"), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_skill_deactivate_command_removes_bypass_marker(
        self, plan_review_repo, plan_review_home
    ):
        """Run activate then deactivate from SKILL.md; verify deactivate removes
        the marker and the hook re-gates."""
        sid = "session-skill-deactivate"
        _seed_session(plan_review_home, sid)

        run_skill_command(
            extract_skill_command(PLAN_REVIEW_SKILL, "activate-gate"),
            cwd=plan_review_repo,
            isolated_home=plan_review_home,
        )
        marker = plan_review_home / ".claude" / ".plan-review-active.d" / sid
        assert marker.exists(), "activate-gate setup did not create the marker"

        run_skill_command(
            extract_skill_command(PLAN_REVIEW_SKILL, "deactivate-gate"),
            cwd=plan_review_repo,
            isolated_home=plan_review_home,
        )
        assert not marker.exists(), (
            "SKILL.md deactivate-gate recipe ran but marker is still present — "
            "skill and hook disagree on the marker path."
        )
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_skill_completion_command_writes_completion_marker(
        self, plan_review_repo, plan_review_home
    ):
        """Run the SKILL.md record-completion recipe; verify the resulting marker
        authorizes a previously-gated Write via the completion path."""
        sid = "session-skill-completion"
        _seed_session(plan_review_home, sid)

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        ), "precondition: Write must be gated before completion marker is written"

        run_skill_command(
            extract_skill_command(PLAN_REVIEW_SKILL, "record-completion"),
            cwd=plan_review_repo,
            isolated_home=plan_review_home,
        )

        expected_marker = plan_review_marker_path(plan_review_home, plan_review_repo, sid)
        assert expected_marker.exists(), (
            "SKILL.md record-completion recipe ran but no marker landed at the path "
            "the hook checks — skill and hook disagree on layout."
        )
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input("/tmp/foo.py"), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    # -- Write-target scope filter ------------------------------------------
    # The gate applies only to writes inside the current repo's working tree.
    # User-home and other out-of-repo writes pass through so that plan-mode
    # can author its scratch plan file at ~/.claude/plans/<slug>.md without
    # needing a bypass marker.

    def test_home_dir_plan_write_passes_through(
        self, plan_review_repo, plan_review_home
    ):
        """Write to ~/.claude/plans/ passes through even when gate is armed.
        Mirrors the plan-mode case where the harness directs the plan file to
        ~/.claude/plans/<session-slug>.md."""
        home_plan_path = str(plan_review_home / ".claude" / "plans" / "session-plan.md")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(home_plan_path), "session_id": "session-scope-home"},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_outside_repo_write_passes_through(self, plan_review_repo, plan_review_home):
        """Write to an arbitrary out-of-repo path passes through when gate is armed."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input("/tmp/scratch/foo.py"), "session_id": "session-scope-tmp"},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_in_repo_write_still_denies(self, plan_review_repo, plan_review_home):
        """Write to a path inside the repo is still denied when gate is armed and
        no marker exists — positive control for the scope filter."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "main.py")), "session_id": "session-scope-deny"},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_sibling_repo_path_not_treated_as_in_repo(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """A path whose prefix shares the repo root name but with extra characters
        (e.g. /tmp/x/plan-repo-sibling/...) must not be mistaken for an in-repo
        path. Guards the trailing-slash prefix match."""
        sibling_path = str(tmp_path / (plan_review_repo.name + "-sibling") / "foo.py")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(sibling_path), "session_id": "session-scope-sibling"},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_outside_repo_write_allows_when_in_repo_plan_unhashable(
        self, plan_review_repo, plan_review_home
    ):
        """An out-of-repo write is decided by path shape alone, before the
        hash is computed, so it allows even when the in-repo active plan is
        unhashable. The in-repo case (test_unreadable_plan_denies_write)
        must keep denying under the same condition."""
        plan = plan_review_repo / ".claude" / "plans" / "impl-plan.md"
        plan.chmod(0o000)
        try:
            decision = run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input("/tmp/scratch/foo.py"), "session_id": "session-scope-unhashable-outside"},
                cwd=plan_review_repo,
            )
        finally:
            plan.chmod(0o644)
        assert decision == "allow"


class TestPlanFileAuthoringExemption:
    """A Write/Edit/MultiEdit whose own target is a plan file this gate
    hashes is exempt -- authoring the plan is never what trips the gate that
    exists to force that plan's review."""

    def test_write_to_own_plan_file_allows(self, plan_review_repo, plan_review_home):
        """A Write to the fixture's pre-seeded impl-plan.md, with the gate
        already armed by that file -- /plan-it Step 1 editing an
        already-drafted plan. See
        test_write_to_second_new_plan_file_allows_while_first_is_unreviewed
        for the new-path case."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / ".claude" / "plans" / "impl-plan.md")),
                    "session_id": "session-author-write",
                },
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_edit_to_own_plan_file_allows(self, plan_review_repo, plan_review_home):
        """An Edit to the same plan file a prior Write created -- the
        /plan-it Step 5 case."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **edit_input(str(plan_review_repo / ".claude" / "plans" / "impl-plan.md")),
                    "session_id": "session-author-edit",
                },
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_multiedit_to_own_plan_file_allows(self, plan_review_repo, plan_review_home):
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **multiedit_input(str(plan_review_repo / ".claude" / "plans" / "impl-plan.md")),
                    "session_id": "session-author-multiedit",
                },
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_edit_allows_under_a_different_session_from_the_one_that_created_the_file(
        self, plan_review_repo, plan_review_home
    ):
        """The restart case: a brand-new process (new session_id, no
        active-bypass marker anywhere) resuming a half-drafted plan must
        still be able to edit it. The exemption holds no per-session,
        per-process, or per-file state, so this holds by construction.
        Specification test: the exemption never reads SESSION_ID at all, so
        this is currently redundant with the simpler single-call allow tests
        above."""
        plan = plan_review_repo / ".claude" / "plans" / "impl-plan.md"
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan)), "session_id": "session-original"},
                cwd=plan_review_repo,
            )
            == "allow"
        ), "precondition: the originating session's own write must allow"
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**edit_input(str(plan)), "session_id": "session-resumed-after-restart"},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_write_to_second_new_plan_file_allows_while_first_is_unreviewed(
        self, plan_review_repo, plan_review_home
    ):
        """A brand-new plan path allows even while a different plan file
        (impl-plan.md, seeded by the fixture) is active and unreviewed."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / ".claude" / "plans" / "new-plan.md")),
                    "session_id": "session-second-plan",
                },
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_write_to_txt_plan_file_allows(self, plan_review_repo, plan_review_home):
        """The exemption's other supported suffix -- every other allow test
        in this class targets .md."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / ".claude" / "plans" / "notes.txt")),
                    "session_id": "session-txt-plan",
                },
                cwd=plan_review_repo,
            )
            == "allow"
        )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_write_to_unreadable_own_plan_file_allows(self, plan_review_repo, plan_review_home):
        """The exemption block runs before the hash computation, so a Write
        repairing the plan file itself must allow even while that same file
        is unreadable -- the self-repair path the deny message elsewhere
        points the user at."""
        plan = plan_review_repo / ".claude" / "plans" / "impl-plan.md"
        plan.chmod(0o000)
        try:
            decision = run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan)), "session_id": "session-repair-own-plan"},
                cwd=plan_review_repo,
            )
        finally:
            plan.chmod(0o644)
        assert decision == "allow"

    def test_symlinked_plan_file_denies_under_forced_realpath_fallback(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """A .claude/plans/x.md symlink whose target lies outside the repo
        must not be exempted when _lib_realpath_m falls back to its manual
        ancestor walk (neither native `realpath -m` nor `grealpath` on
        PATH) -- see TestLibRealpathM's
        test_forced_fallback_fails_closed_on_dangling_symlink in test_lib.py
        for the underlying _lib.sh-level regression test."""
        shim_dir = tmp_path / "realpath_shim"
        shim_dir.mkdir()
        (shim_dir / "realpath").write_text(_FORCED_FALLBACK_REALPATH_SHIM)
        (shim_dir / "realpath").chmod(0o755)
        (shim_dir / "grealpath").write_text(_FORCED_FALLBACK_GREALPATH_SHIM)
        (shim_dir / "grealpath").chmod(0o755)

        symlink_path = plan_review_repo / ".claude" / "plans" / "evil.md"
        symlink_path.symlink_to(tmp_path / "outside" / "target.py")

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(symlink_path)), "session_id": "session-symlink-fallback"},
                cwd=plan_review_repo,
                extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
            )
            == "deny"
        )

    def test_agent_reviews_symlink_denies_under_forced_realpath_fallback(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """The agent-reviews/ exemption shares the identical
        fallback-then-glob-compare pattern as the plan-file exemption above
        and the same dangling-symlink gap, but had no test of its own --
        mirrors test_symlinked_plan_file_denies_under_forced_realpath_fallback."""
        shim_dir = tmp_path / "realpath_shim"
        shim_dir.mkdir()
        (shim_dir / "realpath").write_text(_FORCED_FALLBACK_REALPATH_SHIM)
        (shim_dir / "realpath").chmod(0o755)
        (shim_dir / "grealpath").write_text(_FORCED_FALLBACK_GREALPATH_SHIM)
        (shim_dir / "grealpath").chmod(0o755)

        agent_reviews_dir = plan_review_repo / "agent-reviews"
        agent_reviews_dir.mkdir()
        symlink_path = agent_reviews_dir / "evil.md"
        symlink_path.symlink_to(tmp_path / "outside" / "target.md")

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(symlink_path)), "session_id": "session-agent-reviews-symlink-fallback"},
                cwd=plan_review_repo,
                extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
            )
            == "deny"
        )

    def test_non_exempt_write_denies_under_forced_realpath_fallback_with_dangling_ancestor(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """The general repo-boundary check (neither exemption) shares the
        same dangling-symlink gap when an ANCESTOR of the target, not the
        target's own leaf, is the dangling symlink -- mirrors
        test_symlinked_plan_file_denies_under_forced_realpath_fallback but
        exercises the boundary check's own branch of the gated `if`, not
        either named exemption's."""
        shim_dir = tmp_path / "realpath_shim"
        shim_dir.mkdir()
        (shim_dir / "realpath").write_text(_FORCED_FALLBACK_REALPATH_SHIM)
        (shim_dir / "realpath").chmod(0o755)
        (shim_dir / "grealpath").write_text(_FORCED_FALLBACK_GREALPATH_SHIM)
        (shim_dir / "grealpath").chmod(0o755)

        dangling_ancestor = plan_review_repo / "dangling_ancestor"
        dangling_ancestor.symlink_to(tmp_path / "outside" / "nonexistent-target")
        target_path = dangling_ancestor / "nested" / "foo.py"

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(target_path)), "session_id": "session-dangling-ancestor-fallback"},
                cwd=plan_review_repo,
                extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
            )
            == "deny"
        )

    def test_own_plan_file_allows_under_forced_realpath_fallback(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """The happy path the plan-file-authoring exemption exists to serve,
        under the same forced-fallback environment as the deny tests above.
        Only the deny/security case was covered before this test: stock
        macOS with no Homebrew coreutils lacks both native `realpath -m`
        and `grealpath`, so this fallback branch is the ordinary path there,
        not an edge case."""
        shim_dir = tmp_path / "realpath_shim"
        shim_dir.mkdir()
        (shim_dir / "realpath").write_text(_FORCED_FALLBACK_REALPATH_SHIM)
        (shim_dir / "realpath").chmod(0o755)
        (shim_dir / "grealpath").write_text(_FORCED_FALLBACK_GREALPATH_SHIM)
        (shim_dir / "grealpath").chmod(0o755)

        plan = plan_review_repo / ".claude" / "plans" / "impl-plan.md"
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan)), "session_id": "session-own-plan-fallback-allow"},
                cwd=plan_review_repo,
                extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
            )
            == "allow"
        )

    # -- Negative controls: the exemption must not overreach ----------------

    def test_notes_rst_still_denies(self, plan_review_repo, plan_review_home):
        """A .claude/plans/ file outside the .md/.txt suffix set the gate
        hashes gets the same treatment as any other unrecognized in-repo
        file."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / ".claude" / "plans" / "notes.rst")),
                    "session_id": "session-notes-rst",
                },
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_nested_plan_file_still_denies(self, plan_review_repo, plan_review_home):
        """bash's `case` glob matches `/`, unlike git's `:(glob)`, so a
        nested .claude/plans/sub/*.md path -- which _lib_active_plan_hash
        never hashes -- must not be exempted either."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / ".claude" / "plans" / "sub" / "nested.md")),
                    "session_id": "session-nested-plan",
                },
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_non_plan_target_still_denies(self, plan_review_repo, plan_review_home):
        """The carve-out must not have leaked into an ordinary in-repo write."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / "src" / "foo.py")),
                    "session_id": "session-non-plan-target",
                },
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_top_level_md_outside_plans_dir_still_denies(self, plan_review_repo, plan_review_home):
        """A .md file at repo root, outside .claude/plans/ entirely, exercises
        _lib_is_repo_plan_file's `dirname` equality check rather than its
        suffix match -- every other negative control here differs from the
        true positive by suffix or depth while staying under .claude/plans/,
        never by top-level directory alone."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {
                    **write_input(str(plan_review_repo / "README.md")),
                    "session_id": "session-top-level-md",
                },
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_exitplanmode_still_denies(self, plan_review_repo, plan_review_home):
        """ExitPlanMode carries no file_path, so it never reaches the
        target-scope block the exemption lives in -- the carve-out must not
        have leaked into plan presentation."""
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=""), "session_id": "session-epm-still-denies"},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_exitplanmode_with_file_path_naming_a_plan_file_still_denies(
        self, plan_review_repo, plan_review_home
    ):
        """Every other ExitPlanMode test in this suite goes through
        exitplanmode_input(), which never sets tool_input.file_path -- so
        none of them can detect the explicit
        `[ "$TOOL_NAME" != "ExitPlanMode" ]` guard on the target-scope block
        being removed. This hand-crafts a file_path naming a real plan file
        to pin that guard's own necessity: the harness does not populate
        file_path for ExitPlanMode today, but the guard must hold even if
        it did."""
        plan = plan_review_repo / ".claude" / "plans" / "impl-plan.md"
        payload = {
            **exitplanmode_input(plan_file_path=""),
            "session_id": "session-epm-file-path-guard",
        }
        payload["tool_input"]["file_path"] = str(plan)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                payload,
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_deny_message_names_the_plan_file_exemption(
        self, plan_review_repo, plan_review_home
    ):
        """A non-plan-target deny must not leave the agent concluding the
        plan itself is unfixable."""
        reason = run_hook_reason(
            REQUIRE_PLAN_REVIEW_HOOK,
            {
                **write_input(str(plan_review_repo / "src" / "foo.py")),
                "session_id": "session-deny-message",
            },
            cwd=plan_review_repo,
        )
        assert reason is not None
        assert "Editing the plan file itself is exempt from this gate" in reason


class TestRequirePlanReviewHonorsConfigDir:
    """CLAUDE_CONFIG_DIR relocates the plan-review marker directory the same
    way for marker.sh (write) and this hook (read) -- see marker.sh and the
    cross-account bypass this closes (ledger row 7)."""

    def test_marker_under_matching_config_dir_allows(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """CLAUDE_CONFIG_DIR-set happy path: a marker written under the
        resolved config dir satisfies the gate when the session runs under
        the same value."""
        profile = tmp_path / "profile"
        sid = "session-config-dir-match"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid, config_dir=profile)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
                extra_env={"CLAUDE_CONFIG_DIR": str(profile)},
            )
            == "allow"
        )

    def test_marker_under_different_config_dir_does_not_authorize(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """Cross-account bypass regression: a marker written under one
        CLAUDE_CONFIG_DIR value must not satisfy the gate when the session
        runs under a different one."""
        profile_a = tmp_path / "profile-a"
        profile_b = tmp_path / "profile-b"
        sid = "session-config-dir-mismatch"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid, config_dir=profile_a)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
                extra_env={"CLAUDE_CONFIG_DIR": str(profile_b)},
            )
            == "deny"
        )

    def test_unresolvable_config_dir_denies(self, plan_review_repo, plan_review_home):
        """Fail closed: a relative CLAUDE_CONFIG_DIR (unresolvable) must deny
        the gate outright, even with a valid marker at the default location."""
        sid = "session-config-dir-unresolvable"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
                extra_env={"CLAUDE_CONFIG_DIR": "relative/path"},
            )
            == "deny"
        )


class TestMarkerWriteReadAgreement:
    """The write side (marker.sh) and the read side (require-plan-review.sh)
    must agree byte-for-byte on the active-plan hash. Every other test seeds
    the marker through a Python helper, so each side is only ever checked
    against a stand-in for the other -- a divergence in how marker.sh
    resolves the repo root or writes the value would be invisible."""

    def _write_marker_via_script(self, repo, home):
        return subprocess.run(
            ["bash", str(SCRIPTS_DIR / "marker.sh"), "write", "plan-review"],
            cwd=repo,
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            text=True,
        )

    def test_real_marker_write_allows_then_denies_after_plan_edit(
        self, plan_review_repo, plan_review_home
    ):
        sid = "session-e2e-write-read"
        _seed_session(plan_review_home, sid)

        result = self._write_marker_via_script(plan_review_repo, plan_review_home)
        assert result.returncode == 0, result.stderr

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=""), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        ), "a marker written by the real marker.sh must satisfy the real hook"

        (plan_review_repo / ".claude" / "plans" / "impl-plan.md").write_text(
            "# Implementation plan\n\nStep 1...\n\nRevised after review.\n"
        )
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=""), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        ), "editing the plan after a real marker write must re-arm the gate"


class TestUnhashablePlanFailsClosed:
    """An active plan that cannot be hashed is an UNKNOWN review state, not
    an absent one. _lib_active_plan_hash returns empty for "no plan active"
    and exits non-zero for "plan active but unhashable"; collapsing those
    two onto the same empty-string signal made a transient unreadable plan
    file silently disarm the gate (fail-open)."""

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_unreadable_plan_denies_write(self, plan_review_repo, plan_review_home):
        sid = "session-unhashable-write"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        plan = plan_review_repo / ".claude" / "plans" / "impl-plan.md"
        plan.chmod(0o000)
        try:
            decision = run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
        finally:
            plan.chmod(0o644)
        assert decision == "deny"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_unreadable_plan_denies_exitplanmode(self, plan_review_repo, plan_review_home):
        """ExitPlanMode carries no file_path, so it skips the repo-scope
        filter -- the hash path is the only thing deciding allow/deny."""
        sid = "session-unhashable-epm"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        plan = plan_review_repo / ".claude" / "plans" / "impl-plan.md"
        plan.chmod(0o000)
        try:
            decision = run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=""), "session_id": sid},
                cwd=plan_review_repo,
            )
        finally:
            plan.chmod(0o644)
        assert decision == "deny"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_unreadable_plan_deny_names_file_and_repair_route(
        self, plan_review_repo, plan_review_home
    ):
        """The remedy must be actionable. Telling the user to run
        /plan-review here would be circular -- marker.sh hits the identical
        condition and aborts -- so the message names the offending file and
        points at Bash, which this hook does not gate."""
        sid = "session-unhashable-reason"
        plan = plan_review_repo / ".claude" / "plans" / "impl-plan.md"
        plan.chmod(0o000)
        try:
            reason = run_hook_reason(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=""), "session_id": sid},
                cwd=plan_review_repo,
            )
        finally:
            plan.chmod(0o644)
        assert reason is not None, "an unhashable active plan must deny, not allow"
        assert "impl-plan.md" in reason, (
            f"deny reason must name the unreadable plan file, got {reason!r}"
        )
        assert "chmod" in reason, (
            f"deny reason must name a concrete repair command, got {reason!r}"
        )


class TestContentAddressedPlanReviewMarker:
    """The completion marker stores a content hash of the active plan file
    set (GH #466), not an existence-only sentinel -- editing a reviewed plan
    must re-arm the gate on the next Write/Edit/ExitPlanMode."""

    def test_stale_hash_denies_write_after_plan_edit(self, plan_review_repo, plan_review_home):
        """Editing the plan after a clean review changes the active-plan
        hash, so the stored marker no longer matches -- the gate re-arms."""
        sid = "session-hash-stale-write"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        (plan_review_repo / ".claude" / "plans" / "impl-plan.md").write_text(
            "# Implementation plan\n\nStep 1...\n\nRevised.\n"
        )
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_stale_hash_denies_exitplanmode_after_plan_edit(self, plan_review_repo, plan_review_home):
        """Same as the Write case, but for ExitPlanMode -- the tool that has
        no file_path and so skips the scope filter entirely, making the
        marker hash comparison the only thing standing between allow/deny."""
        sid = "session-hash-stale-epm"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        (plan_review_repo / ".claude" / "plans" / "impl-plan.md").write_text(
            "# Implementation plan\n\nStep 1...\n\nRevised for ExitPlanMode.\n"
        )
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=""), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_committed_clean_plan_does_not_contribute_to_hash(self, plan_review_repo, plan_review_home):
        """One committed-clean plan + one active plan: the gate is armed;
        committing the previously-active plan (so both are now historical)
        disarms it entirely -- proving the committed-clean file contributes
        nothing to the hash. A break-on-first-find-result bug that hashed
        every enumerated file regardless of active/historical status would
        leave the gate armed forever here."""
        subprocess.run(["git", "add", ".claude/plans/impl-plan.md"], cwd=plan_review_repo, check=True)
        subprocess.run(["git", "commit", "-m", "plan"], cwd=plan_review_repo, check=True)
        active_plan = plan_review_repo / ".claude" / "plans" / "new-plan.md"
        active_plan.write_text("# New plan\n")

        sid = "session-armset-agreement"
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        ), "precondition: the untracked active plan must arm the gate"

        subprocess.run(["git", "add", ".claude/plans/new-plan.md"], cwd=plan_review_repo, check=True)
        subprocess.run(["git", "commit", "-m", "new plan"], cwd=plan_review_repo, check=True)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        ), "committing the previously-active plan should disarm the gate entirely"

    def test_multiplan_active_set_rearms_on_either_edit(self, plan_review_repo, plan_review_home):
        """Two active (untracked) plans: a marker written over both allows;
        editing EITHER one afterward denies -- guards against a ported
        break-on-first-plan bug that only hashed the first enumerated file."""
        second_plan = plan_review_repo / ".claude" / "plans" / "second-plan.md"
        second_plan.write_text("# Second plan\n")
        first_plan = plan_review_repo / ".claude" / "plans" / "impl-plan.md"

        sid = "session-multiplan"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        ), "precondition: marker over the two-plan active set must allow"

        # Editing the SECOND enumerated plan (alphabetically after the first)
        # is exactly what a break-on-first-find-result bug would miss.
        second_plan.write_text("# Second plan (edited)\n")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        ), "editing the second active plan must re-arm the gate"

        # Reset with a fresh review, then edit the FIRST plan instead.
        second_plan.write_text("# Second plan\n")
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        first_plan.write_text("# Implementation plan\n\nStep 1 (edited)...\n")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        ), "editing the first active plan must also re-arm the gate"

    def test_legacy_literal_marker_denies_cleanly(self, plan_review_repo, plan_review_home):
        """A pre-migration marker containing the literal 'reviewed' sentinel
        (written by the old existence-only code) must deny cleanly against an
        active plan under the new hash-compare logic -- fail-closed, not an
        error under pipefail."""
        sid = "session-legacy-marker"
        marker = plan_review_marker_path(plan_review_home, plan_review_repo, sid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("reviewed\n")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        )


class TestRequirePlanReviewExitPlanMode:
    """Gate behavior for the ExitPlanMode tool.

    ExitPlanMode is the harness tool that presents the completed plan to the
    user for approval. It must be gated by the same require-plan-review.sh
    hook that gates Write/Edit, but with two important differences:
      1. No file_path field — the path-scope filter is skipped (TARGET_PATH
         empty → `if [ -n "$TARGET_PATH" ]` is false → falls through to deny).
      2. Active-marker bypass does NOT apply — an active marker means
         plan-review is in progress but not yet complete; ExitPlanMode must
         remain blocked until review is finished and a completion marker exists.
    """

    def test_plan_exists_no_marker_denies_exitplanmode(self, plan_review_repo, plan_review_home):
        """Gate arms: plan exists, no marker → ExitPlanMode denied."""
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path=""), "session_id": "test-session-epm"},
            cwd=plan_review_repo,
        )
        assert result == "deny"

    def test_plan_exists_no_marker_exitplanmode_deny_reason(self, plan_review_repo, plan_review_home):
        """Deny reason contains /plan-review reference and ExitPlanMode-specific wording."""
        reason = run_hook_reason(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path=""), "session_id": "test-session-epm-reason"},
            cwd=plan_review_repo,
        )
        assert reason is not None
        assert "/plan-review" in reason
        assert "ExitPlanMode" in reason and "plan presentation" in reason.lower()

    def test_completion_marker_allows_exitplanmode(self, plan_review_repo, plan_review_home):
        """Completion marker present → ExitPlanMode allowed (happy path)."""
        sid = "test-session-epm-allow"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path=""), "session_id": sid},
            cwd=plan_review_repo,
        )
        assert result == "allow"

    def test_no_plan_file_allows_exitplanmode(self, plan_review_repo, plan_review_home):
        """No plan file in .claude/plans/ → ExitPlanMode allowed (gate inactive)."""
        plans_dir = plan_review_repo / ".claude" / "plans"
        shutil.rmtree(plans_dir, ignore_errors=True)
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path=""), "session_id": "test-session-epm-noplan"},
            cwd=plan_review_repo,
        )
        assert result == "allow"

    def test_committed_clean_plan_allows_exitplanmode(self, plan_review_repo, plan_review_home):
        """Committed, unmodified plan → ExitPlanMode allowed (historical plan)."""
        subprocess.run(["git", "add", ".claude/plans/impl-plan.md"], cwd=plan_review_repo, check=True)
        subprocess.run(["git", "commit", "-m", "plan"], cwd=plan_review_repo, check=True)
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path=""), "session_id": "test-session-epm-committed"},
            cwd=plan_review_repo,
        )
        assert result == "allow"

    def test_no_session_id_denies_exitplanmode(self, plan_review_repo, plan_review_home):
        """No session_id → deny (fail-closed; can't key per-session marker)."""
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            exitplanmode_input(plan_file_path=""),  # no session_id key
            cwd=plan_review_repo,
        )
        assert result == "deny"

    def test_no_session_id_exitplanmode_deny_reason(self, plan_review_repo, plan_review_home):
        """No session_id deny reason uses ExitPlanMode-specific wording, not a generic message."""
        reason = run_hook_reason(
            REQUIRE_PLAN_REVIEW_HOOK,
            exitplanmode_input(plan_file_path=""),  # no session_id key
            cwd=plan_review_repo,
        )
        assert reason is not None and "plan presentation" in reason.lower()

    def test_no_file_path_exitplanmode_denies_without_crash(self, plan_review_repo, plan_review_home):
        """ExitPlanMode has no file_path — must deny cleanly, no crash (set -u safe).

        TARGET_PATH resolves to empty string for ExitPlanMode payloads (no
        .tool_input.file_path field). The `if [ -n "$TARGET_PATH" ]` guard
        skips the scope filter, and the hook falls through to emit_deny.
        This test confirms the hook does not crash under set -uo pipefail.
        """
        payload = exitplanmode_input(plan_file_path="")
        # Confirm no file_path field — the helper must not include it.
        assert "file_path" not in payload.get("tool_input", {})
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**payload, "session_id": "test-session-epm-nopath"},
            cwd=plan_review_repo,
        )
        assert result == "deny"

    def test_active_marker_does_not_bypass_exitplanmode(self, plan_review_repo, plan_review_home):
        """Active marker (plan-review in progress) → ExitPlanMode still denied.

        Contrasts with Write/Edit, which are allowed through during an active
        review so the skill's own file writes don't self-deny. ExitPlanMode
        is never called by the plan-review skill itself, so an active marker
        indicates review is incomplete — ExitPlanMode must remain blocked.
        """
        sid = "test-session-epm-active"
        active_dir = plan_review_home / ".claude" / ".plan-review-active.d"
        active_dir.mkdir(parents=True, exist_ok=True)
        (active_dir / sid).write_text(str(os.getpid()))  # live PID
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path=""), "session_id": sid},
            cwd=plan_review_repo,
        )
        assert result == "deny"

    def test_other_sessions_completion_marker_authorizes_exitplanmode(
        self, plan_review_repo, plan_review_home
    ):
        """Session A's completion marker releases session B's ExitPlanMode gate.

        ExitPlanMode reads the completion marker on the same content-addressed
        terms as Write/Edit: the stored hash proves the plan set was reviewed,
        regardless of which session ran the review. Only the active-bypass
        marker is session-scoped, and ExitPlanMode is excluded from that bypass
        entirely (a review in progress is not a review complete).
        """
        other_sid = "test-session-epm-other"
        write_plan_review_marker(plan_review_home, plan_review_repo, other_sid)
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path=""), "session_id": "test-session-epm-current"},
            cwd=plan_review_repo,
        )
        assert result == "allow"

    def test_other_sessions_completion_marker_does_not_authorize_changed_plan_exitplanmode(
        self, plan_review_repo, plan_review_home
    ):
        """The negative half for ExitPlanMode: acceptance is by hash, not existence."""
        write_plan_review_marker(plan_review_home, plan_review_repo, "test-session-epm-other")
        (plan_review_repo / ".claude" / "plans" / "impl-plan.md").write_text("edited after review\n")
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path=""), "session_id": "test-session-epm-current"},
            cwd=plan_review_repo,
        )
        assert result == "deny"


def test_settings_exitplanmode_matcher_exists_and_isolated():
    """settings.json has ExitPlanMode in its own matcher block, not in Edit|Write|MultiEdit.

    ExitPlanMode must be isolated from the Edit|Write|MultiEdit block because
    that block also runs ask-review-permissions.sh and
    require-worktree-for-file-writes.sh, which must not fire on ExitPlanMode.
    """
    settings_path = CLAUDE_DIR / "settings.json"
    settings = json.loads(settings_path.read_text())
    pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])

    # ExitPlanMode must have exactly one dedicated matcher block.
    exitplanmode_blocks = [b for b in pre_tool_use if b.get("matcher") == "ExitPlanMode"]
    assert len(exitplanmode_blocks) == 1, (
        "ExitPlanMode should have exactly one matcher block in PreToolUse"
    )
    hook_commands = [h["command"] for h in exitplanmode_blocks[0].get("hooks", [])]
    assert any(cmd.endswith("require-plan-review.sh") for cmd in hook_commands), (
        "ExitPlanMode block must include require-plan-review.sh"
    )
    assert not any("ask-review-permissions" in cmd for cmd in hook_commands), (
        "ExitPlanMode block must not include ask-review-permissions.sh"
    )
    assert not any("require-worktree" in cmd for cmd in hook_commands), (
        "ExitPlanMode block must not include require-worktree-for-file-writes.sh"
    )

    # ExitPlanMode must NOT appear inside the Edit|Write|MultiEdit block.
    write_edit_blocks = [
        b for b in pre_tool_use
        if "Write" in b.get("matcher", "") and "Edit" in b.get("matcher", "")
    ]
    for block in write_edit_blocks:
        assert "ExitPlanMode" not in block.get("matcher", ""), (
            "ExitPlanMode must not be bundled into the Edit|Write|MultiEdit matcher"
        )


PLAN_TEXT = "# Implementation plan\n\nStep 1...\n"


def _init_repo_with_plan(root, plan_text: str = PLAN_TEXT):
    """A git repo with one commit and an untracked plan at a fixed relative path.

    The commit is what makes `git worktree add` possible; the plan stays
    untracked so it counts as active.
    """
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)
    plans_dir = root / ".claude" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "impl-plan.md").write_text(plan_text)
    return root


class TestRequirePlanReviewCrossWorktree:
    """The #426 repro: a plan copied into a fresh worktree.

    _lib_active_plan_hash hashes repo-RELATIVE paths plus contents, so the
    identical plan at the identical relative path hashes identically in both
    worktrees. The gate must honor a review performed in either one — but only
    across worktrees of the same repository, never across repositories.
    """

    def test_sibling_worktree_marker_authorizes(self, tmp_path, plan_review_home):
        main = _init_repo_with_plan(tmp_path / "main-repo")
        sibling = tmp_path / "sibling-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "feature", str(sibling)],
            cwd=main, check=True,
        )
        (sibling / ".claude" / "plans").mkdir(parents=True, exist_ok=True)
        (sibling / ".claude" / "plans" / "impl-plan.md").write_text(PLAN_TEXT)

        # Review recorded against the main worktree only.
        write_plan_review_marker(plan_review_home, main, "session-in-main")

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(sibling / "src" / "foo.py")), "session_id": "session-in-sibling"},
                cwd=sibling,
                home=plan_review_home,
            )
            == "allow"
        )

    def test_sibling_worktree_marker_does_not_authorize_a_diverged_plan(
        self, tmp_path, plan_review_home
    ):
        """A sibling's review covers the sibling's plan text, not any plan text."""
        main = _init_repo_with_plan(tmp_path / "main-repo")
        sibling = tmp_path / "sibling-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "feature", str(sibling)],
            cwd=main, check=True,
        )
        (sibling / ".claude" / "plans").mkdir(parents=True, exist_ok=True)
        (sibling / ".claude" / "plans" / "impl-plan.md").write_text("a different plan\n")

        write_plan_review_marker(plan_review_home, main, "session-in-main")

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(sibling / "src" / "foo.py")), "session_id": "session-in-sibling"},
                cwd=sibling,
                home=plan_review_home,
            )
            == "deny"
        )

    def test_unrelated_repo_marker_does_not_authorize(self, tmp_path, plan_review_home):
        """Identical plan text in an UNRELATED repository must not release this gate.

        This is why the read stays keyed to `git worktree list` output rather
        than scanning the marker directory repo-agnostically: the plan text
        would have been reviewed, but against a different codebase.
        """
        target = _init_repo_with_plan(tmp_path / "target-repo")
        unrelated = _init_repo_with_plan(tmp_path / "unrelated-repo")

        # Same plan text, same relative path → same active-plan hash, but the
        # marker is filed under the unrelated repo's repo-hash.
        write_plan_review_marker(plan_review_home, unrelated, "session-elsewhere")

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(target / "src" / "foo.py")), "session_id": "session-here"},
                cwd=target,
                home=plan_review_home,
            )
            == "deny"
        )

    def test_decoy_marker_under_same_prefix_does_not_false_accept(
        self, plan_review_repo, plan_review_home
    ):
        """A stale marker beside the correct one must not widen the match.

        The read scans every marker sharing this repo-hash prefix, so a stale
        entry from an earlier plan state coexists with the current one. Only an
        exact content match may release.
        """
        decoy = plan_review_marker_path(plan_review_home, plan_review_repo, "stale-session")
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text("0" * 64 + "\n")

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "session-x"},
                cwd=plan_review_repo,
                home=plan_review_home,
            )
            == "deny"
        )

        write_plan_review_marker(plan_review_home, plan_review_repo, "real-session")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": "session-x"},
                cwd=plan_review_repo,
                home=plan_review_home,
            )
            == "allow"
        )

    def test_failed_worktree_enumeration_fails_closed(self, tmp_path, plan_review_home):
        """A `git worktree list` that fails must deny, not scan fewer prefixes and allow.

        A partial or failed enumeration yields fewer worktrees, and fewer
        worktrees still "succeeds" at finding nothing — so treating enumeration
        failure as a clean miss is indistinguishable from a real miss only in
        the deny direction. This pins that it stays in that direction.
        """
        main = _init_repo_with_plan(tmp_path / "main-repo")
        sibling = tmp_path / "sibling-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "feature", str(sibling)],
            cwd=main, check=True,
        )
        (sibling / ".claude" / "plans").mkdir(parents=True, exist_ok=True)
        (sibling / ".claude" / "plans" / "impl-plan.md").write_text(PLAN_TEXT)
        write_plan_review_marker(plan_review_home, main, "session-in-main")

        # Sanity: without the stub, tier 2 finds the main worktree's marker.
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(sibling / "src" / "foo.py")), "session_id": "s"},
                cwd=sibling,
                home=plan_review_home,
            )
            == "allow"
        )

        # Stub git: passes everything through except `worktree list`, which fails.
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        real_git = shutil.which("git")
        stub = stub_dir / "git"
        stub.write_text(
            "#!/bin/bash\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "worktree" ]; then exit 1; fi\n'
            "done\n"
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(sibling / "src" / "foo.py")), "session_id": "s"},
                cwd=sibling,
                home=plan_review_home,
                extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
            )
            == "deny"
        )

    def test_failed_active_plan_files_enumeration_fails_closed(self, tmp_path, plan_review_home):
        """A `git ls-files`/`git diff` failure inside _lib_active_plan_files
        must deny end to end, not silently disarm the gate -- this function
        now backs both the fast-path guard and the hash computation, so a
        regression that flips its fail-closed polarity would open both call
        sites at once. Mirrors test_failed_worktree_enumeration_fails_closed
        above for the sibling git call this hook depends on."""
        repo = _init_repo_with_plan(tmp_path / "enum-failure-repo")

        real_git = shutil.which("git")
        assert real_git, "test host must have a real git binary on PATH"
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text(
            "#!/bin/bash\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "ls-files" ]; then exit 1; fi\n'
            "done\n"
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(repo / "src" / "foo.py")), "session_id": "s"},
                cwd=repo,
                home=plan_review_home,
                extra_env={"PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}"},
            )
            == "deny"
        )


# Marker directories grow without bound (GC is deferred) and this gate fires on
# every Write/Edit/MultiEdit/ExitPlanMode, so the read's cost must not scale
# with review history. A one-time manual `time` validates today's directory size
# once and gives no signal as it grows — hence an assertion.
#
# The assertion is BASELINE-RELATIVE, not an absolute wall-clock budget. An
# absolute ceiling loose enough to survive a throttled CI runner is far too
# loose to catch the regression that matters (a read that forks per marker
# instead of once), and one tight enough to catch it flakes on a loaded runner.
# Comparing a seeded run against an unseeded run on the SAME machine in the
# SAME test cancels machine speed out and measures the property we actually
# care about: does marker count drive cost? A single-grep read is ~flat in
# marker count; a per-file `cat`/subprocess loop is linear, so a regression
# blows past the ratio on any hardware.
#
# NOT covered here, deliberately: the ARG_MAX/E2BIG ceiling at roughly
# 13k-30k markers under one prefix. That regime fails closed (see the SCALE
# BOUNDARY note on _lib_marker_value_present in _lib.sh) and seeding 30k files
# per run costs more than the coverage is worth while marker retention remains
# unimplemented and out of scope.
SEEDED_MARKER_COUNT = 3000
SEEDED_SIBLING_WORKTREE_COUNT = 8
# A correct read spends a constant number of processes regardless of marker
# count, so seeding thousands of markers should barely move the needle. The
# allowance is generous because subprocess startup and git calls dominate the
# measured window; a per-marker fork loop still overshoots it by orders of
# magnitude.
MARKER_SCALING_RATIO = 3.0
# Absolute slack so a near-zero baseline on a fast machine can't make the
# ratio arbitrarily sensitive to scheduler noise.
MARKER_SCALING_SLACK_SECONDS = 1.0


def _seed_markers(markers_dir, prefix: str, count: int) -> None:
    markers_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (markers_dir / f"{prefix}seeded-session-{i}").write_text(f"{i:064x}\n")


def _time_hook(repo, home) -> float:
    started = time.monotonic()
    assert (
        run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**write_input(str(repo / "src" / "foo.py")), "session_id": "s"},
            cwd=repo,
            home=home,
        )
        == "allow"
    )
    return time.monotonic() - started


class TestRequirePlanReviewLatency:
    pytestmark = pytest.mark.timing

    def test_marker_count_does_not_drive_read_cost(self, tmp_path, plan_review_home):
        """Tier 1 (current repo-hash prefix) stays ~flat as markers accumulate."""
        main = _init_repo_with_plan(tmp_path / "main-repo")
        write_plan_review_marker(plan_review_home, main, "tier1-session")

        baseline_seconds = _time_hook(main, plan_review_home)

        markers_dir = plan_review_home / ".claude" / "plan-review-markers"
        main_hash = hashlib.sha256(str(main.resolve()).encode()).hexdigest()
        # Half the noise shares the scanned prefix; half sits under an unrelated
        # repo-hash, so the glob must discriminate rather than scan everything.
        _seed_markers(markers_dir, f"{main_hash}.", SEEDED_MARKER_COUNT // 2)
        _seed_markers(markers_dir, "0" * 64 + ".", SEEDED_MARKER_COUNT // 2)

        seeded_seconds = _time_hook(main, plan_review_home)

        allowed = baseline_seconds * MARKER_SCALING_RATIO + MARKER_SCALING_SLACK_SECONDS
        assert seeded_seconds < allowed, (
            f"tier-1 read took {seeded_seconds:.3f}s with {SEEDED_MARKER_COUNT} "
            f"markers vs {baseline_seconds:.3f}s with none — over the allowed "
            f"{allowed:.3f}s. The read is scaling with marker count, which means "
            f"it is forking per marker instead of running one grep."
        )

    def test_worktree_count_does_not_drive_tier_two_cost(self, tmp_path, plan_review_home):
        """Tier 2 enumerates and hashes every worktree, so its cost is checked
        against worktree count, not marker count.

        Tier 1 misses for the whole window between authoring a plan and its
        first clean review, so this path is paid per edit while drafting — the
        common case, not a rare deny path.
        """
        main = _init_repo_with_plan(tmp_path / "main-repo")
        first_sibling = tmp_path / "sibling-worktree-0"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "feature-0", str(first_sibling)],
            cwd=main, check=True,
        )
        (first_sibling / ".claude" / "plans").mkdir(parents=True, exist_ok=True)
        (first_sibling / ".claude" / "plans" / "impl-plan.md").write_text(PLAN_TEXT)
        # Review recorded against main only, so the sibling always reaches tier 2.
        write_plan_review_marker(plan_review_home, main, "tier2-session")

        baseline_seconds = _time_hook(first_sibling, plan_review_home)

        for index in range(1, SEEDED_SIBLING_WORKTREE_COUNT):
            extra = tmp_path / f"sibling-worktree-{index}"
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", f"feature-{index}", str(extra)],
                cwd=main, check=True,
            )

        seeded_seconds = _time_hook(first_sibling, plan_review_home)

        allowed = baseline_seconds * MARKER_SCALING_RATIO + MARKER_SCALING_SLACK_SECONDS
        assert seeded_seconds < allowed, (
            f"tier-2 read took {seeded_seconds:.3f}s across "
            f"{SEEDED_SIBLING_WORKTREE_COUNT} worktrees vs {baseline_seconds:.3f}s "
            f"across 1 — over the allowed {allowed:.3f}s."
        )


class TestRequirePlanReviewPlanMode:
    """ExitPlanMode's tool_input.planFilePath names the harness plan-mode file
    directly on the very call being gated, so require-plan-review.sh hashes
    it fresh and checks it against plan-review-markers/ ahead of the
    repo-relative check -- see the hook's own comment on why the ordering is
    required, not cosmetic (a stale repo-relative marker must not authorize
    unreviewed plan-mode content presented via a nested plan-mode question).

    Every other ExitPlanMode test in this file passes plan_file_path="" to
    isolate itself from this branch and exercise the repo-relative check in
    the shape it always has -- see the module-wide exitplanmode_input()
    call-site update accompanying this class.
    """

    def _write_planmode_marker(self, home, repo, session_id, plan_mode_file):
        """Seed a completion marker keyed to a plan-mode file's content hash,
        mirroring what `marker.sh write plan-review` computes when a
        `.planmode-path` sibling declares `plan_mode_file` as its target."""
        digest = hashlib.sha256(plan_mode_file.read_bytes()).hexdigest()
        marker = plan_review_marker_path(home, repo, session_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(digest + "\n")
        return marker

    def test_matching_planmode_hash_allows_exitplanmode(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        sid = "session-planmode-match"
        plan_mode_file = tmp_path / "planmode-scratch.md"
        plan_mode_file.write_text("# Plan-mode plan\n\nContent.\n")
        self._write_planmode_marker(plan_review_home, plan_review_repo, sid, plan_mode_file)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=str(plan_mode_file)), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_stale_or_no_marker_denies_with_planfilepath_set(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """Regression test for the bug this plan fixes: with no repo-relative
        plan present, the pre-fix hook's CURRENT_HASH-empty short-circuit
        let ExitPlanMode through unconditionally regardless of planFilePath.
        A plan-mode ExitPlanMode call with no covering marker must now deny."""
        sid = "session-planmode-nomark"
        shutil.rmtree(plan_review_repo / ".claude" / "plans", ignore_errors=True)
        plan_mode_file = tmp_path / "planmode-scratch.md"
        plan_mode_file.write_text("# Unreviewed plan-mode content\n")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=str(plan_mode_file)), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_nested_planmode_denies_despite_stale_repo_marker(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """Ordering regression: a valid, fresh repo-relative marker must not
        authorize a NESTED plan-mode ExitPlanMode call presenting different,
        unreviewed content. Plan-mode priority over the repo-relative check
        is what closes this -- see the companion test below, which proves
        the repo-relative marker seeded here is genuinely valid on its own
        terms, so this deny is attributable to the ordering fix."""
        sid = "session-nested-planmode"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        plan_mode_file = tmp_path / "planmode-scratch.md"
        plan_mode_file.write_text("# Different, unreviewed plan-mode content\n")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=str(plan_mode_file)), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    def test_nested_planmode_companion_repo_marker_alone_would_allow(
        self, plan_review_repo, plan_review_home
    ):
        """Companion to the case above, same session and same seeded marker:
        a Write call (which carries no planFilePath to prioritize) is
        allowed by that marker. This is what proves the prior test's deny
        comes from the ordering fix and not from an invalid or mis-seeded
        marker -- without the ordering fix, ExitPlanMode would follow this
        same repo-relative path and wrongly allow too.

        Proxy, not a mutation-tested proof: this rules out "mis-seeded
        marker" by inference from Write sharing the hook's repo-relative
        branch with post-priority-check ExitPlanMode, not by executing the
        pre-fix branch ordering with the priority block physically removed."""
        sid = "session-nested-planmode"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(plan_review_repo / "src" / "foo.py")), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )

    def test_absent_and_empty_planfilepath_fall_through_identically(
        self, plan_review_repo, plan_review_home
    ):
        """planFilePath absent (no key at all) and planFilePath="" (present,
        empty) must both fall through to the repo-relative check unchanged.
        jq's `// empty` maps a missing/null field to empty, but an empty
        STRING is already empty without that fallback ever triggering -- the
        two paths through the hook's `[ -n ... ]` guard are not guaranteed to
        agree without exercising both."""
        sid = "session-planfilepath-variants"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)

        absent_payload = {
            "tool_name": "ExitPlanMode",
            "tool_input": {"plan": "# Test plan\n"},
            "session_id": sid,
        }
        empty_payload = {**exitplanmode_input(plan_file_path=""), "session_id": sid}

        assert run_hook(REQUIRE_PLAN_REVIEW_HOOK, absent_payload, cwd=plan_review_repo) == "allow"
        assert run_hook(REQUIRE_PLAN_REVIEW_HOOK, empty_payload, cwd=plan_review_repo) == "allow"

    def test_planfilepath_naming_nonexistent_target_denies_even_with_valid_repo_marker(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        sid = "session-planmode-missing"
        write_plan_review_marker(plan_review_home, plan_review_repo, sid)
        missing_path = str(tmp_path / "does-not-exist.md")
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=missing_path), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "deny"
        )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_planfilepath_naming_unreadable_target_denies(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        sid = "session-planmode-unreadable"
        plan_mode_file = tmp_path / "unreadable-plan.md"
        plan_mode_file.write_text("# secret\n")
        plan_mode_file.chmod(0o000)
        try:
            result = run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=str(plan_mode_file)), "session_id": sid},
                cwd=plan_review_repo,
            )
        finally:
            plan_mode_file.chmod(0o644)
        assert result == "deny"

    @pytest.mark.timing
    def test_planfilepath_target_read_timeout_denies_within_budget(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """_lib_capped caps the sha256sum call at 5s; a stalled read (e.g. a
        dead network mount under the plan-mode file's path) must deny within
        that budget, not hang the ExitPlanMode call indefinitely."""
        if not shutil.which("timeout"):
            pytest.skip("timeout(1) not available — BSD/macOS without coreutils")
        real_sha256sum = shutil.which("sha256sum")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "sha256sum"
        stub.write_text(f'#!/bin/bash\nsleep 10\nexec {real_sha256sum} "$@"\n')
        stub.chmod(0o755)

        plan_mode_file = tmp_path / "slow-plan.md"
        plan_mode_file.write_text("# plan\n")

        sid = "session-planmode-timeout"
        start = time.monotonic()
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path=str(plan_mode_file)), "session_id": sid},
            cwd=plan_review_repo,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )
        elapsed = time.monotonic() - start
        assert result == "deny"
        assert elapsed < 9.5, (
            f"expected the 5s _lib_capped timeout to fire (stub sleeps 10s if it "
            f"does not), took {elapsed:.1f}s"
        )

    @pytest.mark.timing
    def test_planfilepath_hung_jq_denies_within_timeout(
        self, plan_review_repo, plan_review_home, tmp_path
    ):
        """The planFilePath extraction at line ~109 uses _lib_jq, not bare jq
        (GH-489's uncapped-secondary-jq defect class) — a hung jq binary must
        deny within the 5s _lib_jq backstop rather than blocking ExitPlanMode
        indefinitely. Deny is the correct outcome here (unlike the analogous
        require-architect-consult.sh case): an empty PLAN_MODE_FILE_PATH
        falls through to this class's baseline armed-gate logic, which
        denies with no file_path present (see this class's own docstring).

        The hook's first jq call (_lib_parse_tool_input_or_deny's six-field
        extraction) is already _lib_jq-wrapped and would itself deny within
        budget on an always-hung jq, which would pass this test even if the
        second call (planFilePath, this test's actual target) were still
        bare jq. Matching narrowly on the planFilePath filter string (rather
        than a call counter) isolates that specific call: a call-counter
        stub would also hang _lib_emit_deny's own reason-encoding jq call
        on the deny path this test exercises (a third, unrelated call),
        adding a second 5s timeout cycle and roughly doubling elapsed time —
        not a defect in the fix, just a test artifact this design avoids."""
        if not shutil.which("timeout"):
            pytest.skip("timeout(1) not available — BSD/macOS without coreutils")
        real_jq = shutil.which("jq")
        if not real_jq:
            pytest.skip("jq not found in PATH")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "jq"
        stub.write_text(
            "#!/bin/bash\n"
            'case "$*" in\n'
            '  *planFilePath*) sleep 10 ;;\n'
            f'  *) exec "{real_jq}" "$@" ;;\n'
            "esac\n"
        )
        stub.chmod(0o755)

        sid = "session-planmode-hung-jq"
        start = time.monotonic()
        result = run_hook(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**exitplanmode_input(plan_file_path="/tmp/whatever-plan.md"), "session_id": sid},
            cwd=plan_review_repo,
            extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )
        elapsed = time.monotonic() - start
        assert result == "deny"
        assert elapsed < 9.5, (
            f"expected the 5s _lib_jq timeout to fire on the second (planFilePath) "
            f"jq call (stub sleeps 10s if it does not), took {elapsed:.1f}s"
        )


class TestPlanReviewSkillPlanModeFixture:
    """Exercises the new Step 0 declare-planmode-path fixture end to end: the
    recipe lands the sibling file at the path and content marker.sh expects,
    and marker.sh write plan-review + require-plan-review.sh's ExitPlanMode
    check together honor a review declared this way."""

    def _run_activate_then_declare(self, repo, home, monkeypatch, plan_mode_file):
        run_skill_command(
            extract_skill_command(PLAN_REVIEW_SKILL, "activate-gate"),
            cwd=repo,
            isolated_home=home,
        )
        monkeypatch.setenv("PLAN_MODE_FILE_PATH", str(plan_mode_file))
        run_skill_command(
            extract_skill_command(PLAN_REVIEW_SKILL, "declare-planmode-path"),
            cwd=repo,
            isolated_home=home,
        )

    def test_declare_planmode_path_fixture_lands_sibling_file(
        self, plan_review_repo, plan_review_home, tmp_path, monkeypatch
    ):
        sid = "session-declare-planmode"
        _seed_session(plan_review_home, sid)
        plan_mode_file = tmp_path / "harness-plan.md"
        plan_mode_file.write_text("# Harness plan-mode content\n")

        self._run_activate_then_declare(plan_review_repo, plan_review_home, monkeypatch, plan_mode_file)

        sibling = plan_review_home / ".claude" / ".plan-review-active.d" / f"{sid}.planmode-path"
        assert sibling.exists(), (
            "SKILL.md declare-planmode-path recipe ran but no sibling file landed "
            "at the path marker.sh expects — skill and script disagree on layout."
        )
        assert sibling.read_text() == str(plan_mode_file), (
            "the sibling file must hold the declared plan-mode path verbatim, with "
            "no trailing newline"
        )

    def test_declared_planmode_path_chains_through_marker_write_and_gate(
        self, plan_review_repo, plan_review_home, tmp_path, monkeypatch
    ):
        """Chained integration: Step 0's recipe -> marker.sh write plan-review
        (subprocess) -> require-plan-review.sh's ExitPlanMode check
        (subprocess). Catches a cross-file data-shape mismatch (trailing
        newline, relative-path convention) that three independently-mocked
        unit tests could each pass while the composed path fails."""
        sid = "session-declare-planmode-chain"
        _seed_session(plan_review_home, sid)
        plan_mode_file = tmp_path / "harness-plan-chain.md"
        plan_mode_file.write_text("# Harness plan-mode content for the chain test\n")

        self._run_activate_then_declare(plan_review_repo, plan_review_home, monkeypatch, plan_mode_file)

        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "marker.sh"), "write", "plan-review"],
            cwd=plan_review_repo,
            env={**os.environ, "HOME": str(plan_review_home)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**exitplanmode_input(plan_file_path=str(plan_mode_file)), "session_id": sid},
                cwd=plan_review_repo,
            )
            == "allow"
        )


LIB_SH = HOOKS_DIR / "_lib.sh"


def _active_plan_files(repo, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    """Local copy of test_marker_lib.py's helper of the same name (DAMP
    test code, per the neither-test-tree-imports-the-other convention):
    shell out to the real _lib_active_plan_files against `repo`, resolving
    _lib_gate_diff_base first so a mid-merge/rebase/cherry-pick/revert
    fixture exercises the same base require-plan-review.sh/marker.sh would."""
    return subprocess.run(
        ["bash", "-c",
         f'. "{LIB_SH}"; base=$(_lib_gate_diff_base "$1"); _lib_active_plan_files "$1" "$base"',
         "_active_plan_files", str(repo)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env_overrides or {})},
    )


def _make_git_rejecting_write_tree(bin_dir):
    """Simulates git < 2.38: `merge-tree --write-tree` is rejected outright.
    Every other subcommand proxies to the real git. Local copy of
    test_lib.py's shim of the same name (DAMP test code, per CLAUDE.md's
    named exception)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    shim.write_text(
        '#!/bin/bash\n'
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "--write-tree" ]; then\n'
        '    echo "error: unknown option \x60--write-tree\x60" >&2\n'
        '    exit 129\n'
        '  fi\n'
        'done\n'
        'exec "$REAL_GIT" "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def _make_git_rejecting_merge_base_flag(bin_dir):
    """Simulates git 2.38-2.39: --write-tree is accepted but --merge-base=
    is rejected. Local copy of test_lib.py's shim of the same name."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    shim.write_text(
        '#!/bin/bash\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    --merge-base=*)\n'
        '      echo "error: unknown option \x60--merge-base\x60" >&2\n'
        '      exit 129\n'
        '      ;;\n'
        '  esac\n'
        'done\n'
        'exec "$REAL_GIT" "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def _make_blocking_merge_tree_git(bin_dir: Path) -> Path:
    """For `merge-tree` specifically, writes a partial line to stdout then
    blocks past the 5s cap; every other subcommand proxies to the real git.
    Local copy of test_require_code_review.py's shim of the same name."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    shim.write_text(
        '#!/bin/bash\n'
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "merge-tree" ]; then\n'
        '    printf "partialline"\n'
        '    sleep 20\n'
        '    exit 0\n'
        '  fi\n'
        'done\n'
        'exec "$REAL_GIT" "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def _timeout_binary_present() -> bool:
    return shutil.which("timeout") is not None or shutil.which("gtimeout") is not None


class TestActivePlanFilesMergeAwareBase:
    """_lib_active_plan_files diffs against _lib_gate_diff_base's resolved
    base instead of the literal HEAD, so a plan file a trusted merge brought
    in untouched is excluded from the active set."""

    def test_mid_merge_omits_untouched_upstream_plan_reports_edited_one(
        self, tmp_path
    ):
        bare, clone = bare_remote_with_default_branch(tmp_path)
        plans_dir = clone / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "shared-plan.md").write_text("# shared plan\n\nbase content\n")
        subprocess.run(["git", "add", ".claude/plans/shared-plan.md"], cwd=clone, check=True)
        subprocess.run(["git", "commit", "-qm", "seed shared plan"], cwd=clone, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)

        (plans_dir / "shared-plan.md").write_text("# shared plan\n\nours edit\n")
        subprocess.run(["git", "add", ".claude/plans/shared-plan.md"], cwd=clone, check=True)
        subprocess.run(["git", "commit", "-qm", "ours edits shared plan"], cwd=clone, check=True)

        push_clone = tmp_path / "push_clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(push_clone)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=push_clone, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=push_clone, check=True)
        (push_clone / ".claude" / "plans" / "shared-plan.md").write_text(
            "# shared plan\n\norigin edit\n"
        )
        (push_clone / ".claude" / "plans" / "upstream-only-plan.md").write_text(
            "# upstream-only plan\n\nnever touched by ours\n"
        )
        subprocess.run(
            ["git", "add", ".claude/plans/shared-plan.md", ".claude/plans/upstream-only-plan.md"],
            cwd=push_clone, check=True,
        )
        subprocess.run(["git", "commit", "-qm", "origin edits shared plan, adds a new one"], cwd=push_clone, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=push_clone, check=True)

        subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
        result = subprocess.run(
            ["git", "merge", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert (clone / ".git" / "MERGE_HEAD").exists()
        (plans_dir / "shared-plan.md").write_text("# shared plan\n\nresolved\n")
        subprocess.run(["git", "add", ".claude/plans/shared-plan.md"], cwd=clone, check=True)

        active = _active_plan_files(clone)
        assert active.returncode == 0, active.stderr
        active_files = active.stdout.splitlines()
        assert ".claude/plans/shared-plan.md" in active_files
        assert ".claude/plans/upstream-only-plan.md" not in active_files

    def test_fallback_write_tree_rejected_behaves_like_today(self, tmp_path):
        """`--write-tree` outright rejection (git < 2.38) must fall back to
        diffing against literal HEAD -- exactly today's plain recipe --
        rather than hanging or misreporting the active set."""
        bare, clone = bare_remote_with_default_branch(tmp_path)
        plans_dir = clone / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "shared-plan.md").write_text("# shared plan\n\nbase content\n")
        subprocess.run(["git", "add", ".claude/plans/shared-plan.md"], cwd=clone, check=True)
        subprocess.run(["git", "commit", "-qm", "seed shared plan"], cwd=clone, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)

        (plans_dir / "shared-plan.md").write_text("# shared plan\n\nours edit\n")
        subprocess.run(["git", "add", ".claude/plans/shared-plan.md"], cwd=clone, check=True)
        subprocess.run(["git", "commit", "-qm", "ours edits shared plan"], cwd=clone, check=True)

        push_clone = tmp_path / "push_clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(push_clone)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=push_clone, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=push_clone, check=True)
        (push_clone / ".claude" / "plans" / "shared-plan.md").write_text(
            "# shared plan\n\norigin edit\n"
        )
        subprocess.run(["git", "add", ".claude/plans/shared-plan.md"], cwd=push_clone, check=True)
        subprocess.run(["git", "commit", "-qm", "origin edits shared plan"], cwd=push_clone, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=push_clone, check=True)

        subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
        result = subprocess.run(
            ["git", "merge", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert (clone / ".git" / "MERGE_HEAD").exists()
        (plans_dir / "shared-plan.md").write_text("# shared plan\n\nresolved\n")
        subprocess.run(["git", "add", ".claude/plans/shared-plan.md"], cwd=clone, check=True)

        bin_dir = tmp_path / "bin-fallback-write-tree"
        _make_git_rejecting_write_tree(bin_dir)
        active = _active_plan_files(
            clone, env_overrides={"PATH": f"{bin_dir}:{os.environ['PATH']}", "REAL_GIT": shutil.which("git")}
        )
        assert active.returncode == 0, active.stderr
        assert ".claude/plans/shared-plan.md" in active.stdout.splitlines()

    def test_fallback_merge_base_flag_rejected_behaves_like_today(self, tmp_path):
        """The `--merge-base=` rejection band (git 2.38-2.39) only affects
        the three states whose merge-tree call passes that flag -- rebase,
        cherry-pick, revert, not merge -- so this needs its own fixture: a
        conflicted revert of a plan file, trusted via the HEAD anchor by
        construction, must still report the plan as active when the flag is
        rejected and _lib_gate_diff_base falls back to a plain HEAD diff."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / ".claude" / "plans").mkdir(parents=True)
        build_conflicted_revert(repo, file_name=".claude/plans/plan.md")

        bin_dir = tmp_path / "bin-fallback-merge-base"
        _make_git_rejecting_merge_base_flag(bin_dir)
        active = _active_plan_files(
            repo, env_overrides={"PATH": f"{bin_dir}:{os.environ['PATH']}", "REAL_GIT": shutil.which("git")}
        )
        assert active.returncode == 0, active.stderr
        assert ".claude/plans/plan.md" in active.stdout.splitlines()

    @pytest.mark.timing
    @pytest.mark.skipif(
        not _timeout_binary_present(), reason="no timeout/gtimeout on PATH to fire the cap"
    )
    def test_status_2_deny_message_names_undetermined_base(self, isolated_home, tmp_path):
        """Mirrors test_require_code_review.py's
        TestRequireCodeReviewMergeAwareBase::test_status_2_deny_message_names_undetermined_base
        for require-plan-review.sh: status 2 never flips the allow/deny
        decision -- it only changes what the deny message says. The
        untracked plan file here already arms the gate unconditionally (see
        _lib_active_plan_files' untracked-plans leg), so the ordinary
        status-1 case (no merge/rebase/cherry-pick/revert in progress) and
        the status-2 case (an in-progress merge whose base can't be
        computed) must both deny -- the note is the only difference."""
        no_merge_repo = tmp_path / "no-merge-repo"
        no_merge_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=no_merge_repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=no_merge_repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=no_merge_repo, check=True)
        (no_merge_repo / ".claude" / "plans").mkdir(parents=True)
        (no_merge_repo / ".claude" / "plans" / "impl-plan.md").write_text(
            "# Implementation plan\n\nStep 1...\n"
        )
        status_1_reason = run_hook_reason(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**write_input(str(no_merge_repo / "src" / "foo.py")), "session_id": "session-status-1"},
            cwd=no_merge_repo,
        )
        assert status_1_reason is not None, "untracked plan file with no marker must deny"
        assert "novel-content base could not be computed" not in status_1_reason

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
        (clone / ".claude" / "plans").mkdir(parents=True)
        (clone / ".claude" / "plans" / "impl-plan.md").write_text(
            "# Implementation plan\n\nStep 1...\n"
        )

        bin_dir = tmp_path / "bin-blocking-merge-tree"
        _make_blocking_merge_tree_git(bin_dir)
        extra_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_GIT": shutil.which("git"),
        }
        status_2_reason = run_hook_reason(
            REQUIRE_PLAN_REVIEW_HOOK,
            {**write_input(str(clone / "src" / "foo.py")), "session_id": "session-status-2"},
            cwd=clone,
            extra_env=extra_env,
        )
        assert status_2_reason is not None, "untracked plan file with no marker must deny"
        assert "novel-content base could not be computed" in status_2_reason
        assert "fell back to the full HEAD-relative plan-file diff" in status_2_reason


def _plan_review_empty_base_marker_value(base: str) -> str:
    """Independent oracle for _lib_active_plan_hash's empty-base-relative-
    active-set binding: sha256("plan-review-empty-base:<base>"), computed
    directly in Python rather than by calling the shell function under
    test."""
    return hashlib.sha256(f"plan-review-empty-base:{base}".encode()).hexdigest()


def _build_forged_anchor_invisible_plan_edit(tmp_path: Path, name: str = "repo") -> Path:
    """The same forged-anchor + clean-merge technique
    test_require_code_review.py's _build_forged_anchor_clean_merge
    demonstrates against require-code-review.sh, applied to a plan file: M's
    tree already contains the plan file's post-edit content, so after a
    clean merge the plan file's working-tree content exactly matches the
    forged base and _lib_active_plan_files' "modified vs base" leg excludes
    it -- even though the plan file's content differs from real HEAD and was
    never reviewed by anyone. `src/unrelated.py` is the Write target used to
    probe whether the gate is disarmed for a call unrelated to the plan
    file itself."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    plans_dir = repo / ".claude" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "plan.md").write_text("# plan\n\nv1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    head_oid = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (plans_dir / "plan.md").write_text("# plan\n\nforged-content-nobody-reviewed\n")
    subprocess.run(["git", "add", ".claude/plans/plan.md"], cwd=repo, check=True)
    tree_oid = subprocess.run(
        ["git", "write-tree"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    m_oid = subprocess.run(
        ["git", "commit-tree", tree_oid, "-p", head_oid, "-m", "forged"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    subprocess.run(["git", "reset", "--hard", "-q", "HEAD"], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", m_oid], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "-q", "refs/remotes/origin/main"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / ".git" / "MERGE_HEAD").exists()
    return repo


def _merge_head_tree_base(repo) -> str:
    """Independently computes the same reference tree _lib_gate_diff_base's
    merge row computes -- local copy of test_require_code_review.py's
    _merge_tree_base (DAMP test code, per the neither-test-tree-imports-
    the-other convention)."""
    merge_head_oid = (repo / ".git" / "MERGE_HEAD").read_text().strip()
    out = subprocess.run(
        ["git", "merge-tree", "--write-tree", "HEAD", merge_head_oid],
        cwd=repo, capture_output=True, text=True, check=False,
    ).stdout
    return out.strip().splitlines()[0]


class TestRequirePlanReviewForgedAnchorInvisiblePlanEdit:
    """Adversarial coverage for _lib_active_plan_files' base substitution,
    the sibling exposure ciso-reviewer flagged as likely-but-unconfirmed
    against require-code-review.sh's forged-anchor finding: a plan file
    edited to match a forged base's content disappears from the active set,
    and this must not silently disarm the gate for an unrelated Write."""

    def test_no_marker_denies_rather_than_silently_allowing(self, isolated_home, tmp_path):
        repo = _build_forged_anchor_invisible_plan_edit(tmp_path)
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(repo / "src" / "unrelated.py")), "session_id": "s1"},
                cwd=repo,
            )
            == "deny"
        )

    def test_marker_bound_to_this_forged_base_allows(self, plan_review_home, tmp_path):
        """An honest /plan-review run against this exact (forged) base still
        authorizes the write -- the fix denies by default, not
        unconditionally."""
        repo = _build_forged_anchor_invisible_plan_edit(tmp_path)
        base = _merge_head_tree_base(repo)
        marker = plan_review_marker_path(plan_review_home, repo, "s1")
        marker.write_text(_plan_review_empty_base_marker_value(base))
        assert (
            run_hook(
                REQUIRE_PLAN_REVIEW_HOOK,
                {**write_input(str(repo / "src" / "unrelated.py")), "session_id": "s1"},
                cwd=repo,
            )
            == "allow"
        )
