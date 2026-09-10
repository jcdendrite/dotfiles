"""Tests for require-code-review.sh."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from helpers import (
    DEFAULT_TEST_SESSION_ID,
    HOOKS_DIR,
    SKILLS_DIR,
    bare_remote_with_default_branch,
    bash_input,
    build_conflicted_rebase,
    build_conflicted_revert,
    build_path_without,
    edit_input,
    extract_skill_command,
    git_toplevel,
    marker_path,
    push_conflicting_edit_to_origin,
    resolve_conflicted_rebase,
    run_hook,
    run_hook_reason,
    run_skill_command,
    staged_diff_hash,
    staged_diff_hash_at_base,
    write_marker,
)

from .conftest import _seed_session

CODE_REVIEW_HOOK = HOOKS_DIR / "require-code-review.sh"
CODE_REVIEW_SKILL = SKILLS_DIR / "code-review" / "SKILL.md"


class TestRequireCodeReview:
    # The marker layout is ~/.claude/code-review-markers/<repo-hash>.<session_id>.
    # The hook allows when any marker under this repo-hash holds the
    # staged diff's hash, across every session suffix — the stored hash
    # is the authorization, not the filename. Tests below thread
    # session_id through `bash_input` and `write_marker` because the
    # write side still keys on it. Tests that exit early (non-bash tool,
    # non-commit command, outside-repo, empty staged diff) don't need
    # session_id — the hook returns before reaching the marker logic.

    def test_no_marker_denies_commit(self, isolated_home, git_repo):
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_wrong_hash_marker_denies(self, isolated_home, git_repo):
        write_marker(isolated_home, git_repo, "0" * 64)
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_correct_hash_marker_allows(self, isolated_home, git_repo):
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "allow"
        )

    def test_chained_add_commit_allowed_when_marker_current(self, isolated_home, git_repo):
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "git add file.txt && git commit -m foo",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "allow"
        )

    def test_restaging_invalidates_marker(self, isolated_home, git_repo):
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo))
        (git_repo / "file.txt").write_text("first\nsecond\nthird\n")
        subprocess.run(["git", "add", "file.txt"], cwd=git_repo, check=True)
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_chained_add_commit_denied_when_marker_stale(self, isolated_home, git_repo):
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo))
        (git_repo / "file.txt").write_text("first\nsecond\nthird\n")
        subprocess.run(["git", "add", "file.txt"], cwd=git_repo, check=True)
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "git add file.txt && git commit -m foo",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_refreshed_marker_allows(self, isolated_home, git_repo):
        (git_repo / "file.txt").write_text("first\nsecond\nthird\n")
        subprocess.run(["git", "add", "file.txt"], cwd=git_repo, check=True)
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "allow"
        )

    def test_other_sessions_marker_authorizes_identical_staged_diff(self, isolated_home, git_repo):
        """Session A's marker authorizes session B's commit of the identical diff.

        The marker's stored hash proves a review covered exactly this staged
        state; the filename's session suffix only keeps parallel sessions from
        overwriting each other's markers. Keying the read on it denies a
        resumed session (new session_id) a review it already completed."""
        diff_hash = staged_diff_hash(git_repo)
        write_marker(isolated_home, git_repo, diff_hash, session_id="session-A")
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id="session-B"),
                cwd=git_repo,
            )
            == "allow"
        )

    def test_other_sessions_marker_does_not_authorize_a_changed_diff(self, isolated_home, git_repo):
        """The negative half: acceptance is by diff hash, not by marker existence.

        Without this, dropping the session key would degrade the gate from a
        content check to an existence check."""
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id="session-A")
        # Re-stage a different change; the reviewed hash no longer describes it.
        (git_repo / "newly_added.py").write_text("print('unreviewed')\n")
        subprocess.run(["git", "add", "newly_added.py"], cwd=git_repo, check=True)
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id="session-B"),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_no_session_id_in_input_reads_marker(self, isolated_home, git_repo):
        """A payload with no session_id still finds a marker covering this diff.

        This gate reads no session-scoped state at all, so a payload that
        cannot be session-keyed is not thereby unreviewed."""
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo))
        # bash_input() with session_id=None omits the field entirely.
        assert (
            run_hook(CODE_REVIEW_HOOK, bash_input("git commit -m foo"), cwd=git_repo)
            == "allow"
        )

    def test_no_session_id_and_no_matching_marker_denies(self, isolated_home, git_repo):
        """Fail-closed still holds: no session_id and no covering review → deny."""
        assert (
            run_hook(CODE_REVIEW_HOOK, bash_input("git commit -m foo"), cwd=git_repo)
            == "deny"
        )

    def test_marker_under_another_repo_hash_does_not_authorize(
        self, isolated_home, git_repo, tmp_path
    ):
        """The repo-hash prefix stays part of the read predicate.

        Only the session suffix is globbed. A review of an identical diff in a
        different repository reviewed different code, so its marker must not
        release this gate."""
        other_repo = tmp_path / "other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=other_repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=other_repo, check=True)

        # Same diff hash, filed under the other repo's repo-hash prefix.
        write_marker(isolated_home, other_repo, staged_diff_hash(git_repo), session_id="s")
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id="s"),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_skill_marker_write_command_matches_hook_path(self, isolated_home, git_repo):
        """Regression guard against the SKILL command and HOOK getting out
        of sync on path derivation.

        Reads the marker-write recipe directly from code-review SKILL.md
        via the HOOK_TEST_FIXTURE marker, executes it, and verifies the
        hook accepts the result. SKILL.md is the source of truth — if
        the recipe drifts from what the hook expects, this test fails.
        """
        sid = "test-session-skill-cmd"
        # Set up the session_id lookup file at the path the skill reads.
        # The skill computes its filename from $PPID inside the bash
        # subshell; subprocess.run spawns bash as a child of this pytest
        # process, so $PPID resolves to os.getpid().
        _seed_session(isolated_home, sid)

        markers_dir = isolated_home / ".claude" / "code-review-markers"
        if markers_dir.exists():
            for f in markers_dir.glob("*"):
                f.unlink()

        skill_command = extract_skill_command(CODE_REVIEW_SKILL, "marker-write")
        run_skill_command(skill_command, cwd=git_repo, isolated_home=isolated_home)
        # Sanity check: the recipe wrote a marker at the path the hook checks.
        assert marker_path(isolated_home, git_repo, session_id=sid).exists(), (
            "SKILL.md marker-write recipe ran but no marker landed at the "
            "path the hook computes — the skill and hook disagree on layout."
        )
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=sid),
                cwd=git_repo,
            )
            == "allow"
        )

    def test_empty_staged_diff_allows(self, isolated_home, git_repo):
        """Amend-message, --allow-empty, or nothing-to-commit has no new content."""
        subprocess.run(["git", "commit", "-q", "-m", "tmp"], cwd=git_repo, check=True)
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit --amend -m new-message"),
                cwd=git_repo,
            )
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git log --oneline",
            "git commit-tree abc123",
        ],
    )
    def test_non_commit_git_commands_allowed(self, isolated_home, git_repo, command):
        assert run_hook(CODE_REVIEW_HOOK, bash_input(command), cwd=git_repo) == "allow"

    def test_non_bash_tool_allowed(self, isolated_home, git_repo):
        assert run_hook(CODE_REVIEW_HOOK, edit_input("/tmp/foo.txt"), cwd=git_repo) == "allow"

    def test_outside_git_repo_allowed(self, isolated_home, tmp_path):
        """Hook should bail rather than false-deny when git can't resolve a repo."""
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        assert run_hook(CODE_REVIEW_HOOK, bash_input("git commit -m foo"), cwd=non_repo) == "allow"

    def test_chained_marker_write_then_commit_allowed_without_existing_marker(
        self, isolated_home, git_repo
    ):
        """PreToolUse fires once per Bash tool call before the chain runs, so
        an on-disk marker check finds nothing for naturally-typed forms like
        `marker.sh write code-review && git commit`. The chain itself will
        write the marker before commit, and marker.sh is the only sanctioned
        writer in either case — trust the in-chain write and allow."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "~/.claude/scripts/marker.sh write code-review && git commit -m foo",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "allow"
        )

    def test_chained_bare_marker_write_does_not_authorize(self, isolated_home, git_repo):
        """The bypass must NOT recognize a bare `marker.sh` (PATH-resolved or
        attacker-controlled path like `/home/evil/marker.sh`). Only canonical
        ~/.claude/scripts/marker.sh or absolute /.claude/scripts/marker.sh
        paths are sanctioned by permissions.allow and enforce-marker-script-shape,
        and this helper must agree to prevent a chained-form bypass via a
        non-leading bogus marker.sh path."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "marker.sh write code-review && git commit -m foo",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_chained_non_canonical_marker_path_does_not_authorize(
        self, isolated_home, git_repo
    ):
        """A bogus marker.sh path (not under /.claude/scripts/) must not
        trigger the bypass even when chained correctly. Closes the gap where
        enforce-marker-script-shape's leading-anchor check would not fire on
        a non-leading marker.sh fragment in a chain."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "git add . && /home/evil/marker.sh write code-review && git commit -m foo",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_echo_wrapping_marker_text_does_not_authorize(
        self, isolated_home, git_repo
    ):
        """`echo ~/.claude/scripts/marker.sh write code-review && git commit`
        looks like a chained marker write to a text-matcher, but `echo` does
        not actually invoke marker.sh — only prints the path. The helper must
        anchor at command start so wrapper commands (echo, printf, cat, sudo)
        cannot wedge the gate open via text appearance."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "echo ~/.claude/scripts/marker.sh write code-review && git commit -m foo",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_env_var_prefix_marker_does_not_authorize(self, isolated_home, git_repo):
        """`FOO=bar ~/.claude/scripts/marker.sh write code-review && git commit`
        is intentionally not in the sanctioned chained shape — env-var prefix
        is one of the forms enforce-marker-script-shape comments call out as
        gated by permissions.allow, not by shape regex. The helper must
        agree to prevent a bypass via prefix wrapping."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "FOO=bar ~/.claude/scripts/marker.sh write code-review && git commit -m foo",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_bash_c_wrapped_marker_does_not_authorize(self, isolated_home, git_repo):
        """`bash -c '~/.claude/scripts/marker.sh write code-review' && git commit`
        wraps marker.sh in a subshell. Whether the inner marker.sh actually
        runs depends on subshell semantics; either way the outer command does
        not match the sanctioned chained shape, so the bypass must not fire."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "bash -c '~/.claude/scripts/marker.sh write code-review' && git commit -m foo",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_heredoc_pipe_with_marker_text_does_not_authorize(
        self, isolated_home, git_repo
    ):
        """`cat <<EOF | bash\\n~/.claude/scripts/marker.sh write code-review\\nEOF`
        piped into a chain with `git commit` must not bypass the gate. The
        heredoc body text appears inside the command string but the outer
        shape (`cat | bash && ...`) is not a sanctioned chained form.
        Without anchoring at command start, the marker text in the heredoc
        body would trick a fragment walker into seeing a marker-write
        precedes the commit."""
        cmd = (
            "cat <<EOF | bash\n"
            "~/.claude/scripts/marker.sh write code-review\n"
            "EOF\n"
            "git commit -m foo"
        )
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(cmd, session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_chained_skill_review_marker_does_not_authorize_code_review(
        self, isolated_home, git_repo
    ):
        """Chaining `marker.sh write skill-review` (wrong skill) before
        `git commit` must NOT authorize a code-review-gated commit. Each
        gate's bypass is scoped to its own skill name."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "~/.claude/scripts/marker.sh write skill-review && git commit -m foo",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_marker_write_after_commit_does_not_authorize(self, isolated_home, git_repo):
        """In a hypothetical `git commit && marker.sh write code-review`, the
        marker write happens AFTER commit — too late. The bypass must only
        fire when the marker-write fragment precedes the commit fragment."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    "git commit -m foo && ~/.claude/scripts/marker.sh write code-review",
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_quoted_marker_text_in_commit_message_does_not_authorize(
        self, isolated_home, git_repo
    ):
        """A literal `marker.sh write code-review` appearing inside a quoted
        commit message must NOT bypass the gate — the marker-write text has
        to be in a fragment that precedes the commit fragment, not embedded
        in the commit's own arguments."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input(
                    'git commit -m "marker.sh write code-review"',
                    session_id=DEFAULT_TEST_SESSION_ID,
                ),
                cwd=git_repo,
            )
            == "deny"
        )

    # ------------------------------------------------------------------ #
    # Quote-split and fail-closed status-2 regression                     #
    # ------------------------------------------------------------------ #

    def test_quoted_form_reaches_same_verdict_as_bare_form(self, isolated_home, git_repo):
        """A quote-adjacent split (`"git" commit -m x`) must reach the same
        deny verdict as the unquoted form — the fragment matcher strips
        quote characters before word-walking, unlike a raw regex over
        unstripped $COMMAND."""
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input('"git" commit -m foo', session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "deny"
        )

    def test_sed_absent_from_path_denies(self, isolated_home, git_repo, tmp_path):
        """Status-2 propagation: the matcher could not determine whether
        this command invokes git commit, and this gate's own documented
        fail-closed posture means an undetermined match denies rather than
        silently falling through to allow. Asserts the distinguishing
        reason text, not just the verdict, so this test cannot be
        satisfied by an ordinary missing-review deny reaching "deny" for
        the wrong reason."""
        farm_dir = tmp_path / "path-without-sed"
        farm_dir.mkdir()
        restricted_path = build_path_without("sed", farm_dir)
        reason = run_hook_reason(
            CODE_REVIEW_HOOK,
            bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
            cwd=git_repo,
            extra_env={"PATH": restricted_path},
        )
        assert reason is not None
        assert "could not determine" in reason


class TestRequireCodeReviewHonorsConfigDir:
    """CLAUDE_CONFIG_DIR relocates the code-review marker directory the same
    way for marker.sh (write) and this hook (read) -- see marker.sh and the
    cross-account bypass this closes (ledger row 7)."""

    def test_marker_under_matching_config_dir_allows(self, isolated_home, git_repo, tmp_path):
        """CLAUDE_CONFIG_DIR-set happy path: a marker written under the
        resolved config dir satisfies the gate when the session runs under
        the same value."""
        profile = tmp_path / "profile"
        write_marker(
            isolated_home,
            git_repo,
            staged_diff_hash(git_repo),
            session_id=DEFAULT_TEST_SESSION_ID,
            config_dir=profile,
        )
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
                extra_env={"CLAUDE_CONFIG_DIR": str(profile)},
            )
            == "allow"
        )

    def test_marker_under_different_config_dir_does_not_authorize(
        self, isolated_home, git_repo, tmp_path
    ):
        """Cross-account bypass regression: a marker written under one
        CLAUDE_CONFIG_DIR value must not satisfy the gate when the session
        runs under a different one."""
        profile_a = tmp_path / "profile-a"
        profile_b = tmp_path / "profile-b"
        write_marker(
            isolated_home,
            git_repo,
            staged_diff_hash(git_repo),
            session_id=DEFAULT_TEST_SESSION_ID,
            config_dir=profile_a,
        )
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
                extra_env={"CLAUDE_CONFIG_DIR": str(profile_b)},
            )
            == "deny"
        )

    def test_unresolvable_config_dir_denies(self, isolated_home, git_repo):
        """Fail closed: a relative CLAUDE_CONFIG_DIR (unresolvable) must deny
        the gate outright, even with a valid marker at the default location."""
        write_marker(
            isolated_home,
            git_repo,
            staged_diff_hash(git_repo),
            session_id=DEFAULT_TEST_SESSION_ID,
        )
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
                extra_env={"CLAUDE_CONFIG_DIR": "relative/path"},
            )
            == "deny"
        )


class TestRequireCodeReviewComplianceLog:
    """Non-blocking `.review-ledger-compliance.log` line appended at both of
    this hook's exit paths. Never affects the gate's own decision."""

    COMPLIANCE_LOG = "review-ledger-compliance.log"

    def _log_path(self, isolated_home: Path) -> Path:
        return isolated_home / ".claude" / f".{self.COMPLIANCE_LOG}"

    def _ledger_file_path(self, isolated_home: Path, repo: Path, session_id: str) -> Path:
        repo_hash = hashlib.sha256(git_toplevel(repo).encode()).hexdigest()
        return (
            isolated_home
            / ".claude"
            / "review-narrative-ledger"
            / f"{repo_hash}.{session_id}.jsonl"
        )

    def test_log_line_appended_on_match(self, isolated_home, git_repo):
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=DEFAULT_TEST_SESSION_ID)
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "allow"
        )
        lines = self._log_path(isolated_home).read_text().splitlines()
        assert len(lines) == 1
        assert "marker=matched" in lines[0]
        assert "ledger=absent" in lines[0]

    def test_log_line_appended_on_deny(self, isolated_home, git_repo):
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "deny"
        )
        lines = self._log_path(isolated_home).read_text().splitlines()
        assert len(lines) == 1
        assert "marker=unmatched" in lines[0]
        assert "ledger=absent" in lines[0]

    def test_log_line_reports_ledger_present(self, isolated_home, git_repo):
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=DEFAULT_TEST_SESSION_ID)
        ledger = self._ledger_file_path(isolated_home, git_repo, DEFAULT_TEST_SESSION_ID)
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"finding":"f","disposition":"ADDRESS","rationale":"r","source":"n/a"}\n')

        run_hook(
            CODE_REVIEW_HOOK,
            bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
            cwd=git_repo,
        )

        lines = self._log_path(isolated_home).read_text().splitlines()
        assert "ledger=present" in lines[0]

    def test_log_line_has_iso8601_timestamp(self, isolated_home, git_repo):
        run_hook(
            CODE_REVIEW_HOOK,
            bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
            cwd=git_repo,
        )
        line = self._log_path(isolated_home).read_text().splitlines()[0]
        timestamp = line.split(" ", 1)[0]
        # Raises ValueError (failing the test) if not a well-formed
        # UTC ISO-8601 timestamp of the form the hook writes.
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_exit_code_unaffected_by_log_write_failure(self, isolated_home, git_repo):
        """An unwritable config dir (log append fails) must not change the
        gate's own allow/deny decision."""
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo), session_id=DEFAULT_TEST_SESSION_ID)
        config_dir = isolated_home / ".claude"
        config_dir.chmod(0o555)
        try:
            decision = run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
        finally:
            config_dir.chmod(0o755)
        assert decision == "allow"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission bits")
    def test_unwritable_log_dir_does_not_hang(self, isolated_home, git_repo):
        """_lib_capped's timeout wraps the compliance-log append; an
        unwritable directory must fail fast rather than hang the gate.
        subprocess.run's own timeout is the test's hang-guard: a regression
        that reintroduced a blocking write would raise TimeoutExpired here
        instead of hanging the test suite indefinitely."""
        config_dir = isolated_home / ".claude"
        config_dir.chmod(0o555)
        env = {**os.environ, "HOME": str(isolated_home)}
        try:
            result = subprocess.run(
                [str(CODE_REVIEW_HOOK)],
                input=json.dumps(bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID)),
                cwd=git_repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            config_dir.chmod(0o755)
        # _lib_emit_deny prints a JSON deny payload and returns 0 rather than
        # calling exit 2 itself (see run_hook's own docstring) — parse the
        # payload the same way run_hook does rather than asserting exit code.
        payload = json.loads(result.stdout)
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny", (
            "no marker exists, so the gate must still deny"
        )
        assert not self._log_path(isolated_home).exists()

    def test_fifo_compliance_log_does_not_hang(self, isolated_home, git_repo):
        """A compliance-log target whose `>>` open() itself blocks (no
        reader) is the genuine hang this gate must be protected against —
        distinct from test_unwritable_log_dir_does_not_hang above, which
        only proves the EACCES case fails fast and cannot tell "fails fast"
        apart from "actually timeout-protected". subprocess.run's own
        timeout is the hang-guard: a regression that reintroduced an
        unprotected `>>` redirect would raise TimeoutExpired here instead of
        hanging the test suite indefinitely."""
        write_marker(
            isolated_home, git_repo, staged_diff_hash(git_repo), session_id=DEFAULT_TEST_SESSION_ID
        )
        log_path = self._log_path(isolated_home)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(log_path)
        env = {**os.environ, "HOME": str(isolated_home)}
        try:
            result = subprocess.run(
                [str(CODE_REVIEW_HOOK)],
                input=json.dumps(bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID)),
                cwd=git_repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
        finally:
            log_path.unlink()
        # A matched marker's allow path is silent (empty stdout, exit 0) —
        # see run_hook's own docstring for this same empty-stdout mapping.
        assert result.stdout.strip() == "" and result.returncode == 0, (
            f"a matching marker exists, so the gate must still silently "
            f"allow despite the compliance-log append being unable to "
            f"complete; got returncode={result.returncode}, "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )


def _build_conflicted_merge_via_origin(tmp_path: Path) -> Path:
    """A conflicted merge whose MERGE_HEAD is trusted via the origin/<default>
    anchor -- clone edits `f`, origin independently edits `f`, merge conflicts,
    resolve and stage. Returns the clone with the conflict resolved and staged,
    ready for `git commit`."""
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


def _merge_tree_base(repo: Path) -> str:
    """Independently computes the same reference tree
    _lib_gate_diff_base's merge row computes, via the documented recipe
    directly (not by calling the shell function under test). The literal
    MERGE_HEAD *OID*, not the ref name, is passed as the argument -- git
    embeds a merge-tree argument's own textual form into the conflict
    marker label (`>>>>>>> MERGE_HEAD` vs `>>>>>>> <sha>`), so passing the
    ref name here would silently compute a byte-different tree than
    production's `merge-tree --write-tree HEAD "$state_oid"`, which reads
    the OID straight from the gitdir file. Exit status is deliberately not
    checked: `merge-tree --write-tree` exits 1 (not 0) whenever the merge it
    computes conflicts -- the expected case here -- while still writing a
    valid tree on its first stdout line."""
    merge_head_oid = (repo / ".git" / "MERGE_HEAD").read_text().strip()
    out = subprocess.run(
        ["git", "merge-tree", "--write-tree", "HEAD", merge_head_oid],
        cwd=repo, capture_output=True, text=True, check=False,
    ).stdout
    return out.strip().splitlines()[0]


def _cherry_pick_tree_base(repo: Path) -> str:
    """Independently computes the same reference tree _lib_gate_diff_base's
    cherry-pick row computes, via the documented recipe directly (not by
    calling the shell function under test): merge-tree --write-tree
    --merge-base=CHERRY_PICK_HEAD^ HEAD CHERRY_PICK_HEAD. Uses the literal
    CHERRY_PICK_HEAD OID, not the ref name, for the same reason
    _merge_tree_base documents for the merge row."""
    cherry_pick_head_oid = (repo / ".git" / "CHERRY_PICK_HEAD").read_text().strip()
    out = subprocess.run(
        ["git", "merge-tree", "--write-tree",
         f"--merge-base={cherry_pick_head_oid}^", "HEAD", cherry_pick_head_oid],
        cwd=repo, capture_output=True, text=True, check=False,
    ).stdout
    return out.strip().splitlines()[0]


def _empty_base_marker_value(base: str) -> str:
    """Independent oracle for _lib_code_review_marker_value's empty-base-
    relative-diff binding: sha256("code-review-empty-base:<base>"), computed
    directly in Python rather than by calling the shell function under
    test."""
    return hashlib.sha256(f"code-review-empty-base:{base}".encode()).hexdigest()


def _build_disjoint_files_clean_merge(tmp_path: Path) -> Path:
    """A conflict-free merge via a real origin fetch (not forged) where the
    clone and origin each add a different file, so the auto-merged result
    has an empty base-relative diff even though the plain HEAD-relative diff
    is non-empty (it includes origin's own new file). This is the
    disqualified fixture shape for M-1 (two disjoint files, not one file
    touched by both sides) -- valid only for exercising the empty-diff
    branch itself, not the merge-tree-vs-path-intersection primitive
    choice."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    (clone / "own.txt").write_text("own\n")
    subprocess.run(["git", "add", "own.txt"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "own edits"], cwd=clone, check=True)
    push_conflicting_edit_to_origin(tmp_path, bare, "other.txt", "origin-edit\n")
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    result = subprocess.run(
        ["git", "merge", "--no-commit", "-q", "origin/main"],
        cwd=clone, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / ".git" / "MERGE_HEAD").exists()
    return clone


def _build_forged_anchor_clean_merge(
    tmp_path: Path, name: str = "repo", payload_content: str = "payload\n"
) -> Path:
    """The forged-anchor + clean-merge attack: builds M via `git
    commit-tree` (no `git commit` subprocess), forges
    refs/remotes/origin/main to point at M directly (plain plumbing -- no
    push, no fetch, no attacker infrastructure), then runs an ordinary `git
    merge --no-ff --no-commit` against that forged ref. The merge
    auto-stages M's payload file with no conflict, so the base-relative diff
    is empty even though the payload is genuinely novel content nobody
    reviewed. `name` distinguishes multiple independent repos built under
    the same tmp_path; `payload_content` distinguishes their forged trees
    (and therefore their resolved bases) from one another."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    head_oid = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "payload.txt").write_text(payload_content)
    subprocess.run(["git", "add", "payload.txt"], cwd=repo, check=True)
    tree_oid = subprocess.run(
        ["git", "write-tree"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    m_oid = subprocess.run(
        ["git", "commit-tree", tree_oid, "-p", head_oid, "-m", "forged"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Return the working tree to a clean HEAD state -- the merge below must
    # introduce payload.txt itself, not find it already sitting untracked.
    subprocess.run(["git", "reset", "--hard", "-q", "HEAD"], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", m_oid], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "-q", "refs/remotes/origin/main"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / ".git" / "MERGE_HEAD").exists()
    return repo


def _make_git_rejecting_write_tree(bin_dir: Path) -> Path:
    """Simulates git < 2.38: `merge-tree --write-tree` is rejected outright.
    Every other subcommand proxies to the real git (resolved via $REAL_GIT).
    Local copy of test_lib.py's shim of the same name (DAMP test code, per
    CLAUDE.md's named exception) -- this file's fallback assertion needs its
    own stub, not a shared import, per the plan's "per call site" mandate."""
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


def _make_git_rejecting_merge_base_flag(bin_dir: Path) -> Path:
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
    Local copy of test_lib.py's shim of the same name."""
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


class TestRequireCodeReviewMergeAwareBase:
    """require-code-review.sh threads _lib_gate_diff_base's resolved base
    through both the empty-diff early exit and the marker hash, so a
    mid-merge commit is reviewed on its novel content only."""

    def test_mid_merge_marker_with_old_head_relative_preimage_denies(
        self, isolated_home, tmp_path
    ):
        """Marker invalidation, leg 1 of 3: a marker holding the plain
        HEAD-relative preimage (today's recipe) must not validate a mid-merge
        commit -- it covers upstream's whole contribution, not just the
        resolution, so treating it as authorization would be the security
        regression this base substitution exists to close."""
        repo = _build_conflicted_merge_via_origin(tmp_path)
        write_marker(isolated_home, repo, staged_diff_hash(repo))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=repo,
            )
            == "deny"
        )

    def test_mid_merge_marker_with_new_base_relative_preimage_allows(
        self, isolated_home, tmp_path
    ):
        """Marker invalidation, leg 2 of 3: a marker holding the base-relative
        preimage -- computed by the independent staged_diff_hash_at_base()
        oracle, not by seeding from the production function under test --
        must validate."""
        repo = _build_conflicted_merge_via_origin(tmp_path)
        base = _merge_tree_base(repo)
        write_marker(isolated_home, repo, staged_diff_hash_at_base(repo, base))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=repo,
            )
            == "allow"
        )

    def test_outside_merge_state_old_head_relative_preimage_still_allows(
        self, isolated_home, git_repo
    ):
        """Marker invalidation, leg 3 of 3: outside any in-progress state the
        preimage is unchanged, so an ordinary marker keeps validating -- the
        base substitution must not be a permanent break for the common case."""
        write_marker(isolated_home, git_repo, staged_diff_hash(git_repo))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
            )
            == "allow"
        )

    def test_mid_merge_empty_novel_diff_denies_without_a_marker(self, isolated_home, tmp_path):
        """A merge whose only novel content is what git's own auto-merge
        already produced (no manual edits beyond `--no-commit`) has an empty
        base-relative diff, even though the plain HEAD-relative diff is
        non-empty (it includes origin's own new file). This must not be an
        unconditional allow: with no marker present, the commit is denied,
        the same posture any other unreviewed diff gets. A silent allow here
        (sha256("") treated as authorization) is exactly the shape a forged
        origin/<default> anchor plus a clean merge could otherwise exploit --
        see TestRequireCodeReviewForgedAnchorEmptyBaseDiff below."""
        clone = _build_disjoint_files_clean_merge(tmp_path)
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=clone,
            )
            == "deny"
        )

    def test_mid_merge_empty_novel_diff_marker_bound_to_base_allows(
        self, isolated_home, tmp_path
    ):
        """The same fixture as above, with a marker holding the base-bound
        value _lib_code_review_marker_value computes for an empty
        base-relative diff -- allows. Proves an honest /code-review run
        against this exact, genuinely-empty-relative-diff state still
        authorizes the commit in one pass, so the fix does not make the
        ordinary, non-adversarial case harder to pass."""
        clone = _build_disjoint_files_clean_merge(tmp_path)
        base = _merge_tree_base(clone)
        write_marker(isolated_home, clone, _empty_base_marker_value(base))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=clone,
            )
            == "allow"
        )

    def test_single_file_non_overlapping_hunks_auto_merge_contributes_nothing(
        self, isolated_home, tmp_path
    ):
        """The primitive-choice case (M-1): one file touched by both sides in
        non-overlapping hunks, auto-merging cleanly with no conflict. The
        fixture must be a single shared file -- two files each touched by one
        side (the case above) would pass under a path-intersection heuristic
        too and would prove nothing about the merge-tree-vs-path-intersection
        choice. A marker holding the base-bound empty-diff value allows;
        without it, the commit denies -- either way, the base-relative diff
        (not the whole file) is what the gate reasons about."""
        bare, clone = bare_remote_with_default_branch(
            tmp_path, file_name="shared.txt", file_content="line1\nline2\nline3\n"
        )
        (clone / "shared.txt").write_text("line1\nline2\nline3\nours-addition\n")
        subprocess.run(["git", "add", "shared.txt"], cwd=clone, check=True)
        subprocess.run(["git", "commit", "-qm", "ours edits tail"], cwd=clone, check=True)
        push_conflicting_edit_to_origin(
            tmp_path, bare, "shared.txt", "origin-addition\nline1\nline2\nline3\n"
        )
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
        result = subprocess.run(
            ["git", "merge", "--no-commit", "-q", "origin/main"],
            cwd=clone, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (clone / ".git" / "MERGE_HEAD").exists()
        base = _merge_tree_base(clone)
        assert staged_diff_hash_at_base(clone, base) == hashlib.sha256(b"").hexdigest(), (
            "fixture must auto-merge to exactly the base-relative empty-diff shape"
        )
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=clone,
            )
            == "deny"
        )
        write_marker(isolated_home, clone, _empty_base_marker_value(base))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=clone,
            )
            == "allow"
        )

    def test_conflicted_cherry_pick_new_base_relative_preimage_allows(
        self, isolated_home, tmp_path
    ):
        """Cherry-pick coverage: CHERRY_PICK_HEAD's merge-tree call uses
        `--merge-base=CHERRY_PICK_HEAD^` (the parent, not the ref itself) --
        a wiring bug specific to this call site's consumption of that arm
        would ship undetected without a fixture reaching it, since the
        primitive-level tests for _lib_gate_diff_base exercise it directly
        rather than through this hook. The cherry-picked commit must be
        trusted via the origin/<default> anchor: an ordinary
        build_conflicted_cherry_pick() fixture (two sibling commits, neither
        an ancestor of the other) reaches neither anchor, which would
        exercise the empty-base fallback instead of this arm."""
        bare, clone = bare_remote_with_default_branch(tmp_path)
        (clone / "f").write_text("ours-edit\n")
        subprocess.run(["git", "add", "f"], cwd=clone, check=True)
        subprocess.run(["git", "commit", "-qm", "ours edits f"], cwd=clone, check=True)
        push_conflicting_edit_to_origin(tmp_path, bare, "f", "origin-edit\n")
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
        result = subprocess.run(
            ["git", "cherry-pick", "origin/main"], cwd=clone, capture_output=True, text=True
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert (clone / ".git" / "CHERRY_PICK_HEAD").exists()
        (clone / "f").write_text("resolved\n")
        subprocess.run(["git", "add", "f"], cwd=clone, check=True)
        base = _cherry_pick_tree_base(clone)
        write_marker(isolated_home, clone, staged_diff_hash_at_base(clone, base))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=clone,
            )
            == "allow"
        )

    @pytest.mark.timing
    @pytest.mark.skipif(
        not _timeout_binary_present(), reason="no timeout/gtimeout on PATH to fire the cap"
    )
    def test_status_2_deny_message_names_undetermined_base(self, isolated_home, tmp_path):
        """Status 2 never flips the allow/deny decision -- it only changes
        what the deny message says, so a timeout-driven full-diff fallback
        is distinguishable from an ordinary marker mismatch."""
        repo = _build_conflicted_merge_via_origin(tmp_path)
        bin_dir = tmp_path / "bin-blocking-merge-tree"
        _make_blocking_merge_tree_git(bin_dir)
        extra_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_GIT": shutil.which("git"),
        }
        reason = run_hook_reason(
            CODE_REVIEW_HOOK,
            bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
            cwd=repo,
            extra_env=extra_env,
        )
        assert reason is not None, "expected a deny with no matching marker"
        assert "novel-content base could not be computed" in reason
        assert "fell back to the full HEAD-relative diff" in reason

    def test_fallback_write_tree_rejected_behaves_like_today(self, isolated_home, tmp_path):
        """`--write-tree` outright rejection (git < 2.38) affects every state
        uniformly, so the merge fixture already used above exercises it: a
        git that can't compute the novel-content base must fall back to
        exactly today's plain HEAD-relative recipe, so a marker written
        under the old recipe still validates."""
        repo = _build_conflicted_merge_via_origin(tmp_path)
        write_marker(isolated_home, repo, staged_diff_hash(repo))
        bin_dir = tmp_path / "bin-fallback-write-tree"
        _make_git_rejecting_write_tree(bin_dir)
        extra_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_GIT": shutil.which("git"),
        }
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=repo,
                extra_env=extra_env,
            )
            == "allow"
        )

    def test_fallback_merge_base_flag_rejected_behaves_like_today(self, isolated_home, tmp_path):
        """The `--merge-base=` rejection band (git 2.38-2.39) only affects
        the three states whose merge-tree call passes that flag -- rebase,
        cherry-pick, revert, not merge -- so this needs its own fixture: a
        conflicted revert, trusted via the HEAD anchor by construction."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        build_conflicted_revert(repo)
        write_marker(isolated_home, repo, staged_diff_hash(repo))
        bin_dir = tmp_path / "bin-fallback-merge-base"
        _make_git_rejecting_merge_base_flag(bin_dir)
        extra_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_GIT": shutil.which("git"),
        }
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=repo,
                extra_env=extra_env,
            )
            == "allow"
        )

    def test_mid_rebase_ordinary_case_hash_matches_plain_head_relative_recipe(
        self, isolated_home, tmp_path
    ):
        """Pinned through require-code-review.sh's own wiring, not only
        through _lib_gate_diff_base: on an ordinary (non-anchor-reaching)
        mid-rebase fixture, a bare `git commit` produces a hash matching
        staged_diff_hash() -- the plain HEAD-relative recipe, since
        REBASE_HEAD reaches neither anchor in the ordinary case -- not
        staged_diff_hash_at_base() with any non-empty base."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        build_conflicted_rebase(repo)
        resolve_conflicted_rebase(repo)
        write_marker(isolated_home, repo, staged_diff_hash(repo))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=repo,
            )
            == "allow"
        )


def _make_blocking_diff_git(bin_dir: Path) -> Path:
    """For `diff` specifically, writes a partial line to stdout then blocks
    past the 5s cap; every other subcommand proxies to the real git. Local
    copy of test_lib.py's shim of the same name."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    shim.write_text(
        '#!/bin/bash\n'
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "diff" ]; then\n'
        '    printf "partialline"\n'
        '    sleep 20\n'
        '    exit 0\n'
        '  fi\n'
        'done\n'
        'exec "$REAL_GIT" "$@"\n'
    )
    shim.chmod(0o755)
    return shim


class TestRequireCodeReviewEmptyDiffCheckCapFaultInjection:
    """require-code-review.sh's own EMPTY_DIFF_CHECK line (reached when
    GATE_DIFF_BASE is empty, i.e. no merge/rebase/cherry-pick/revert is in
    progress) wraps `git diff --cached` in _lib_capped, the third of three
    capped-git-diff call sites guarding against the same hang risk. The
    other two -- _lib_gate_diff_base's merge-tree call and
    _lib_staged_diff_hash's diff call -- are covered at the function level
    in test_lib.py; this class covers EMPTY_DIFF_CHECK at the full-hook
    level."""

    @pytest.mark.timing
    @pytest.mark.skipif(
        not _timeout_binary_present(), reason="no timeout/gtimeout on PATH to fire the cap"
    )
    def test_blocked_diff_denies_rather_than_allows_as_nothing_staged(
        self, isolated_home, git_repo, tmp_path
    ):
        """A `git diff --cached` killed past the cap must not fall through
        EMPTY_DIFF_CHECK's early exit as though nothing were staged --
        git_repo has a real staged change, so allowing here would let an
        unreviewed commit through."""
        bin_dir = tmp_path / "bin-blocking-diff"
        _make_blocking_diff_git(bin_dir)
        extra_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_GIT": shutil.which("git"),
        }
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=git_repo,
                extra_env=extra_env,
            )
            == "deny"
        )


class TestRequireCodeReviewForgedAnchorEmptyBaseDiff:
    """Adversarial coverage for the empty-diff branch when GATE_DIFF_BASE is
    non-empty: a fabricated `commit-tree` commit and a hand-forged
    refs/remotes/origin/main ref (no real push, no real fetch), merged
    cleanly. Empirically confirmed to silently allow with no marker, no
    deny, and no compliance-log line before this fix -- see
    _build_forged_anchor_clean_merge's docstring for the exact attack
    shape."""

    def test_no_marker_denies_rather_than_silently_allowing(self, isolated_home, tmp_path):
        repo = _build_forged_anchor_clean_merge(tmp_path)
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m done", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=repo,
            )
            == "deny"
        )

    def test_marker_for_a_different_forged_base_does_not_authorize_this_one(
        self, isolated_home, tmp_path
    ):
        """The sha256("") reuse concern: a marker obtained for one forged
        base's degenerate empty-diff case must not validate a different,
        independently-forged base landing on the same empty result. Writes
        the base-bound marker value for a SECOND, differently-forged repo
        and confirms it does not authorize the first repo's commit."""
        repo = _build_forged_anchor_clean_merge(tmp_path, name="repo1", payload_content="payload-1\n")
        other_repo = _build_forged_anchor_clean_merge(
            tmp_path, name="repo2", payload_content="payload-2\n"
        )
        base = _merge_tree_base(repo)
        other_base = _merge_tree_base(other_repo)
        assert base != other_base, "fixture must produce two distinct forged bases"
        write_marker(isolated_home, repo, _empty_base_marker_value(other_base))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m done", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=repo,
            )
            == "deny"
        )

    def test_marker_bound_to_this_forged_base_allows(self, isolated_home, tmp_path):
        """An honest /code-review run against this exact (forged) base still
        authorizes the commit -- the fix denies by default, not
        unconditionally; a marker matching the actual resolved base still
        validates, matching the ordinary marker-comparison contract."""
        repo = _build_forged_anchor_clean_merge(tmp_path)
        base = _merge_tree_base(repo)
        write_marker(isolated_home, repo, _empty_base_marker_value(base))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m done", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=repo,
            )
            == "allow"
        )

    def test_compliance_log_names_the_empty_base_relative_diff_branch(
        self, isolated_home, tmp_path
    ):
        """The compliance-log backstop must record that an empty
        relative-to-base diff occurred during a detected in-progress state,
        so a human auditor can distinguish this branch from an ordinary
        large-diff mismatch."""
        repo = _build_forged_anchor_clean_merge(tmp_path)
        run_hook(
            CODE_REVIEW_HOOK,
            bash_input("git commit -m done", session_id=DEFAULT_TEST_SESSION_ID),
            cwd=repo,
        )
        compliance_log = isolated_home / ".claude" / ".review-ledger-compliance.log"
        assert compliance_log.exists(), "expected a compliance-log line to be appended"
        lines = compliance_log.read_text().splitlines()
        assert lines, "expected at least one compliance-log line"
        assert "marker=unmatched-empty-base-relative-diff" in lines[-1]


def _build_delete_modify_conflict_via_origin(tmp_path: Path) -> Path:
    """A delete/modify conflict: the clone deletes `f`, origin independently
    edits it, and merging surfaces the delete/modify conflict shape --
    distinct from a two-sided content conflict, since one side has no blob
    at all to three-way-merge against."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    subprocess.run(["git", "rm", "-q", "f"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "clone deletes f"], cwd=clone, check=True)
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


def _build_clean_merge_mode_bit_only_change(tmp_path: Path) -> Path:
    """A conflict-free merge where upstream's only contribution to a shared
    file is a mode-bit flip (chmod +x, no content change): the clone's own
    commit touches an unrelated file so the merge cannot fast-forward and
    leaves MERGE_HEAD, but `f` itself was never independently edited on the
    clone's side."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    (clone / "own.txt").write_text("own\n")
    subprocess.run(["git", "add", "own.txt"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "own edit"], cwd=clone, check=True)

    push_clone = tmp_path / "push_clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(push_clone)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=push_clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=push_clone, check=True)
    os.chmod(push_clone / "f", 0o755)
    subprocess.run(["git", "add", "f"], cwd=push_clone, check=True)
    subprocess.run(["git", "commit", "-qm", "origin marks f executable"], cwd=push_clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=push_clone, check=True)

    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    result = subprocess.run(
        ["git", "merge", "--no-commit", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / ".git" / "MERGE_HEAD").exists()
    return clone


def _resolve_gitdir(repo: Path) -> Path:
    """Resolve `repo`'s actual gitdir via `rev-parse --absolute-git-dir` --
    for a linked worktree this is `<main-repo>/.git/worktrees/<name>`, not a
    `.git` directory under `repo` itself, since a linked worktree's `.git`
    is a gitdir-pointer file."""
    return Path(
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )


def _merge_tree_base_for_gitdir(repo: Path, gitdir: Path) -> str:
    """Like _merge_tree_base above, but reads MERGE_HEAD from an explicit
    gitdir rather than assuming `repo/.git` is a real directory -- needed
    for a linked worktree."""
    merge_head_oid = (gitdir / "MERGE_HEAD").read_text().strip()
    out = subprocess.run(
        ["git", "merge-tree", "--write-tree", "HEAD", merge_head_oid],
        cwd=repo, capture_output=True, text=True, check=False,
    ).stdout
    return out.strip().splitlines()[0]


class TestRequireCodeReviewDiffBaseFixtureShapes:
    """Coverage for _lib_gate_diff_base's base-relative diff computation
    across conflict/state shapes not otherwise exercised in this file:
    delete/modify conflicts, mode-bit-only changes, and a linked worktree as
    the repo root the gate operates on."""

    def test_delete_modify_conflict_new_base_relative_preimage_allows(
        self, isolated_home, tmp_path
    ):
        """A delete/modify conflict (one side removes `f`, the other edits
        it) still produces a valid merge-tree base -- the marker computed
        against that base validates the resolved commit."""
        repo = _build_delete_modify_conflict_via_origin(tmp_path)
        base = _merge_tree_base(repo)
        write_marker(isolated_home, repo, staged_diff_hash_at_base(repo, base))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=repo,
            )
            == "allow"
        )

    def test_mode_bit_only_upstream_change_contributes_nothing_to_base_relative_diff(
        self, isolated_home, tmp_path
    ):
        """Upstream's only contribution to a shared file is a mode-bit flip
        (chmod +x, no content change) -- the merge auto-resolves it with no
        conflict, and since the clone's own side never touched `f`, the
        base-relative diff for it is empty: the trusted mode change is
        already reflected in the resolved base, not novel content to
        review."""
        clone = _build_clean_merge_mode_bit_only_change(tmp_path)
        base = _merge_tree_base(clone)
        assert staged_diff_hash_at_base(clone, base) == hashlib.sha256(b"").hexdigest(), (
            "fixture must auto-merge to exactly the base-relative empty-diff shape"
        )
        write_marker(isolated_home, clone, _empty_base_marker_value(base))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=clone,
            )
            == "allow"
        )

    def test_linked_worktree_repo_root_computes_base_relative_diff(
        self, isolated_home, tmp_path
    ):
        """A linked git worktree (not the main working tree) as the repo
        root the gate operates on: _lib_gate_diff_base's
        `rev-parse --absolute-git-dir` call resolves the worktree-specific
        gitdir correctly, and the base-relative marker still validates a
        mid-merge commit performed there."""
        bare, clone = bare_remote_with_default_branch(tmp_path)
        subprocess.run(["git", "branch", "-q", "wt-branch"], cwd=clone, check=True)
        worktree = tmp_path / "linked-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-q", str(worktree), "wt-branch"], cwd=clone, check=True
        )
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=worktree, check=True)
        (worktree / "f").write_text("ours-edit\n")
        subprocess.run(["git", "add", "f"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "ours edits f"], cwd=worktree, check=True)
        push_conflicting_edit_to_origin(tmp_path, bare, "f", "origin-edit\n")
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=worktree, check=True)
        result = subprocess.run(
            ["git", "merge", "-q", "origin/main"], cwd=worktree, capture_output=True, text=True
        )
        assert result.returncode != 0, result.stdout + result.stderr
        gitdir = _resolve_gitdir(worktree)
        assert (gitdir / "MERGE_HEAD").exists()
        (worktree / "f").write_text("resolved\n")
        subprocess.run(["git", "add", "f"], cwd=worktree, check=True)

        base = _merge_tree_base_for_gitdir(worktree, gitdir)
        write_marker(isolated_home, worktree, staged_diff_hash_at_base(worktree, base))
        assert (
            run_hook(
                CODE_REVIEW_HOOK,
                bash_input("git commit -m foo", session_id=DEFAULT_TEST_SESSION_ID),
                cwd=worktree,
            )
            == "allow"
        )

