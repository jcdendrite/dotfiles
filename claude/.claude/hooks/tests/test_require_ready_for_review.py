"""Tests for require-ready-for-review.sh."""
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
    HOOKS_DIR,
    SKILLS_DIR,
    assert_gate_handles_traversal_session_id,
    bash_input,
    build_path_without,
    edit_input,
    extract_skill_command,
    git_toplevel,
    head_sha,
    run_hook,
    run_skill_command,
)

from .conftest import _seed_session, assert_cap_engaged

READY_FOR_REVIEW_HOOK = HOOKS_DIR / "require-ready-for-review.sh"
READY_FOR_REVIEW_SKILL = SKILLS_DIR / "ready-for-review" / "SKILL.md"


@pytest.fixture
def fake_gh_pr_exists(tmp_path, monkeypatch):
    """Inject a fake gh that reports an open PR (`pr view ...` returns 42)."""
    bin_dir = tmp_path / "fake-bin-pr"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/bash\n'
        'case "$*" in *"pr view"*) echo 42 ;; *) exit 1 ;; esac\n'
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return gh


@pytest.fixture
def fake_gh_no_pr(tmp_path, monkeypatch):
    """Inject a fake gh that reports no open PR (any subcommand exits 1)."""
    bin_dir = tmp_path / "fake-bin-nopr"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/bash\nexit 1\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return gh


@pytest.fixture
def repo_on_feature_branch(tmp_path):
    """Repo with `main` configured as default and a `feature` branch checked
    out. Fakes the origin/main ref so the hook's default-branch detection
    works without a real remote."""
    repo = tmp_path / "rfr-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f").write_text("a\n")
    subprocess.run(["git", "add", "f"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    return repo


def rfr_active_marker(home: Path, session_id: str) -> Path:
    return home / ".claude" / ".ready-for-review-active.d" / session_id


def rfr_completion_marker(
    home: Path, repo: Path, session_id: str, config_dir: Path | None = None
) -> Path:
    repo_hash = hashlib.sha256(git_toplevel(repo).encode()).hexdigest()
    config_dir = config_dir if config_dir is not None else home / ".claude"
    return config_dir / "ready-for-review-markers" / f"{repo_hash}.{session_id}"


class TestRequireReadyForReview:
    """Push gate: requires /ready-for-review to have run before pushing to a
    branch with an open PR (or marking a draft PR ready). Two-marker layout:
    active (presence-only, bypasses during skill execution) and completion
    (HEAD SHA, allows pushes against the recorded HEAD)."""

    # -- Bypass shapes (no marker required) ------------------------------

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git log --oneline",
            "git commit -m foo",
            "echo git push",
        ],
    )
    def test_non_push_commands_allowed(
        self, isolated_home, repo_on_feature_branch, command
    ):
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_non_bash_tool_allowed(self, isolated_home):
        assert (
            run_hook(READY_FOR_REVIEW_HOOK, edit_input("/tmp/foo.txt")) == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push --dry-run",
            "git push origin feature --dry-run",
            "git push origin --delete feature",
            "git push origin -d feature",
            "git push origin :feature",
        ],
    )
    def test_destructive_or_dry_push_shapes_allowed(
        self,
        isolated_home,
        repo_on_feature_branch,
        fake_gh_pr_exists,
        command,
    ):
        """Even on a PR branch, --dry-run / --delete / colon-deletion don't
        publish a reviewable artifact change. Bypass without checking marker."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_tags_only_push_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin --tags", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_tags_with_branch_still_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """`git push --tags origin feature` pushes BOTH tags AND a branch ref —
        the branch is reviewable, so gate."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push --tags origin feature", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git -C /wt push --tags origin feature",
            "git -c user.name=x push --tags origin feature",
            "git --git-dir=/wt/.git push --tags origin feature",
        ],
    )
    def test_tags_with_branch_behind_a_git_global_flag_still_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """A global flag between `git` and `push` (e.g. `-C`, `-c`) must not
        let a tag-only-looking push arm exempt a fragment that also names a
        branch ref."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git -C /wt push --tags origin",
            "git -c user.name=x push --tags origin",
            "git --git-dir=/wt/.git push --tags origin",
        ],
    )
    def test_tags_only_push_behind_a_git_global_flag_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """The don't-over-gate counterpart: once the global flag is
        consumed, a genuinely tag-only push is still recognized as such."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git -C /wt push origin :old-branch new-feature:new-feature",
            "git -c user.name=x push origin :old-branch new-feature:new-feature",
            "git --git-dir=/wt/.git push origin :old-branch new-feature:new-feature",
        ],
    )
    def test_colon_refspec_with_branch_behind_a_git_global_flag_still_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """A global flag between `git` and `push` must not let a
        delete-only-looking colon-refspec arm exempt a fragment that also
        names a real refspec — the colon-refspec arm's own version of the
        --tags guard above."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git -C /wt push origin :old-branch",
            "git -c user.name=x push origin :old-branch",
            "git --git-dir=/wt/.git push origin :old-branch",
        ],
    )
    def test_colon_refspec_only_push_behind_a_git_global_flag_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """The don't-over-gate counterpart: once the global flag is
        consumed, a genuinely delete-only colon-refspec push is still
        recognized as such."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push --tags $(echo origin feature)",
            "git push --tags `echo origin feature`",
        ],
    )
    def test_tags_only_push_with_command_substitution_still_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """Command substitution's output becomes real push arguments at
        execution time, so a branch ref hidden inside $(...) or backticks
        must not be exempted by the tag-only bypass."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_all_push_fragments_bypassable_still_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """A command whose every push fragment independently qualifies for a
        bypass (dry-run, delete) is allowed with no gated fragment remaining."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(
                    "git push --dry-run && git push origin :feature", session_id="s"
                ),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_colon_refspec_with_real_branch_ref_still_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """git's documented rename-on-remote idiom pairs a deletion refspec
        with a real one in a single fragment. The deletion refspec alone
        must not exempt the whole fragment from gating."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(
                    "git push origin :old-branch new-feature:new-feature",
                    session_id="s",
                ),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_colon_refspec_multiple_deletes_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """The don't-over-gate counterpart: two pure deletion refspecs in one
        fragment stay bypassed."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(
                    "git push origin :old-branch :another-old-branch",
                    session_id="s",
                ),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin :old-branch $(echo new-feature:new-feature)",
            "git push origin :old-branch `echo new-feature:new-feature`",
        ],
    )
    def test_colon_refspec_with_command_substitution_still_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """Command substitution's output becomes real push arguments at
        execution time, so a branch ref hidden inside $(...) or backticks
        must not be exempted by the delete-only bypass — the colon-refspec
        arm's own version of the guard the --tags arm already has."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_colon_refspec_and_tags_in_one_fragment_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """A fragment combining a deletion refspec with --tags gates, since
        neither arm's allowlist recognizes the other arm's safe token. Pins
        the current conservative behavior for this untested combination
        rather than leaving it to silently move either direction later."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin :feature --tags", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push --force origin --tags",
            "git push --force-with-lease origin --tags",
            "git push --force-with-lease=refs/heads/x:1234 origin --tags",
            "git push --force-if-includes origin --tags",
            "git push -u origin --tags",
            "git push --set-upstream origin --tags",
        ],
    )
    def test_tags_only_push_with_force_or_upstream_flag_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """Each of the --tags arm's own allowlisted flags stays tag-only
        when composed with --tags — the flags are passed through, not
        mistaken for a branch ref."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push --force origin :old-branch",
            "git push --force-with-lease origin :old-branch",
            "git push --force-with-lease=refs/heads/x:1234 origin :old-branch",
            "git push --force-if-includes origin :old-branch",
            "git push -u origin :old-branch",
            "git push --set-upstream origin :old-branch",
        ],
    )
    def test_colon_refspec_only_push_with_force_or_upstream_flag_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """Each of the colon-refspec arm's own allowlisted flags stays
        delete-only when composed with a deletion refspec — the flags are
        passed through, not mistaken for a branch ref."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push --force origin --tags feature",
            "git push -u origin :old-branch new-feature:new-feature",
        ],
    )
    def test_force_or_upstream_flag_does_not_mask_a_real_refspec(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """A real branch ref alongside one of the allowlisted flags must
        still gate — the flag's own allowlist entry doesn't widen to also
        excuse an unrelated refspec in the same fragment."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin :dummy-ref origin",
            "git push origin --tags origin",
            "git push upstream :dummy upstream",
        ],
    )
    def test_repeated_remote_name_in_refspec_position_still_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """A bare `origin`/`upstream` token is only exempt in the
        <repository> position. A second occurrence later in the fragment is
        a refspec — it pushes the identically-named local branch — and must
        not ride along on the remote-name exemption."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push upstream :dummy",
            "git push upstream --tags",
        ],
    )
    def test_upstream_only_push_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """The don't-over-gate counterpart to the repeated-remote-name tests
        above: a single, non-repeated `upstream` token in the <repository>
        position is exempt, mirroring `origin`'s existing allow coverage."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git -C /wt push origin :dummy-ref origin",
            "git -c user.name=x push origin :dummy-ref origin",
            "git --git-dir=/wt/.git push origin :dummy-ref origin",
        ],
    )
    def test_repeated_remote_name_behind_a_git_global_flag_still_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """A global flag ahead of `push` must not change the repeated-remote-
        name shape's outcome — global-flag stripping and the position-aware
        exclusion are otherwise tested independently."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git -C /wt push upstream :dummy",
            "git -c user.name=x push upstream :dummy",
            "git --git-dir=/wt/.git push upstream :dummy",
        ],
    )
    def test_upstream_only_push_behind_a_git_global_flag_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """The don't-over-gate counterpart: a global flag ahead of `push`
        must not change the single, non-repeated `upstream` shape's
        outcome either."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_default_branch_push_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Push from default branch has no PR review semantics — bypass."""
        subprocess.run(
            ["git", "checkout", "-q", "main"],
            cwd=repo_on_feature_branch,
            check=True,
        )
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_branch_with_no_pr_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        """Branch hasn't had a PR opened yet — nothing to gate."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_push_of_commit_tree_built_oid_to_default_branch_allowed_with_no_open_pr(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        """Documented-behavior assertion: this hook's own default-branch and
        no-open-PR bypasses key on the checked-out branch and its PR state,
        never on the push destination or refspec, so a commit built with
        `git commit-tree` -- which never reaches require-code-review.sh,
        since that hook matches only `git commit` -- is allowed straight
        onto the default branch's refspec. This pins the local half of the
        gap `_lib_gate_diff_base`'s origin/<default> anchor accepts (its
        soundness rests on this repo's PR-merge workflow, not on any local
        control over what reaches the remote); it does not claim a
        GitHub-side branch-protection setting is absent -- that is a
        per-repository server setting outside this suite's reach."""
        tree_oid = subprocess.run(
            ["git", "-C", str(repo_on_feature_branch), "write-tree"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        payload_oid = subprocess.run(
            [
                "git", "-C", str(repo_on_feature_branch), "commit-tree", tree_oid,
                "-m", "unreviewed payload",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(f"git push origin {payload_oid}:refs/heads/main", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_outside_git_repo_allowed(self, isolated_home, tmp_path):
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push", session_id="s"),
                cwd=non_repo,
            )
            == "allow"
        )

    # -- Active-marker bypass --------------------------------------------

    def test_fresh_active_marker_allows(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """During /ready-for-review's own iteration (step 3 fix → push →
        loop), the active marker bypasses the gate so the skill doesn't
        self-deny."""
        sid = "session-active"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / sid).write_text(str(os.getpid()))
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_active_marker_hit_advances_mtime(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """The hook is wired to the touch-refreshing wrapper, not the bare
        liveness predicate -- a live-but-idle-window-aged marker's mtime must
        advance on a gate hit, or a reverted call site would pass every
        allow/deny assertion in this file silently."""
        sid = "session-active-touch"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / sid
        marker.write_text(str(os.getpid()))
        old_time = time.time() - 300  # in-window, but old enough to detect a refresh
        os.utime(marker, (old_time, old_time))
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )
        assert marker.stat().st_mtime > old_time + 1, (
            "a gate hit against a live marker must refresh its mtime"
        )

    def test_alive_pid_active_marker_bypasses(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Active marker whose stored PID is alive bypasses the gate."""
        sid = "session-alive-pid"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / sid).write_text(str(os.getpid()))
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_dead_pid_active_marker_evicts_and_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Orphaned marker with a dead PID is evicted; gate denies."""
        sid = "session-dead-pid"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / sid
        marker.write_text("99999999")  # PID outside Linux/macOS max range → always dead
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )
        assert not marker.exists(), "hook must evict the orphan marker on dead PID"

    def test_other_sessions_active_marker_does_not_bypass(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Per-session keying: A's active marker must NOT authorize B's push."""
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / "session-A").write_text(str(os.getpid()))
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="session-B"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_active_marker_empty_content_denies_bypass(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Empty marker content (non-numeric) fails the PID regex — fails closed."""
        sid = "session-empty"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / sid).write_text("")
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_traversal_session_id_denies_and_does_not_touch_marker_dir(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """A session_id of '../canary' must not read through the traversal:
        ACTIVE_MARKER concatenates it into .ready-for-review-active.d/../canary,
        which resolves to a file one level up ($HOME/.claude/canary). The
        invalid id must skip the active-marker bypass entirely and fall
        through to the completion-marker check, which finds no match here
        and denies."""
        assert_gate_handles_traversal_session_id(
            READY_FOR_REVIEW_HOOK,
            lambda sid: bash_input("git push origin feature", session_id=sid),
            isolated_home,
            expected_decision="deny",
            cwd=repo_on_feature_branch,
        )

    def test_non_gated_command_advances_active_marker_mtime(
        self, isolated_home, repo_on_feature_branch
    ):
        """The active-marker check runs before the command-shape filter, so
        a non-gated command still refreshes a live marker's mtime."""
        sid = "session-non-gated"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / sid
        marker.write_text(str(os.getpid()))
        old_time = time.time() - 300  # in-window, but old enough to detect a refresh
        os.utime(marker, (old_time, old_time))
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("pytest -q", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )
        assert marker.stat().st_mtime > old_time + 1, (
            "a non-gated command must still refresh a live marker's mtime"
        )

    def test_live_marker_allows_despite_fragment_split_sed_failure(
        self, isolated_home, repo_on_feature_branch, tmp_path
    ):
        """GH-869: the active-marker check runs before the fragment-split
        fail-closed deny, so a session holding a live marker allows through a
        sed shim that fails only on _lib_split_fragments's invocation shape.
        Shares its PATH-shim technique with test_fragments_split_sed_failure_denied
        (GH-783); differs only in holding a live marker."""
        real_sed = shutil.which("sed")
        assert real_sed, "test host must have a real sed binary on PATH"

        shim_dir = tmp_path / "sed-fails-outside-strip-shell-quotes-shape"
        shim_dir.mkdir()
        shim_script = textwrap.dedent(f"""\
            #!/bin/bash
            if [ "$2" != "-e" ]; then
              exit 1
            fi
            exec "{real_sed}" "$@"
        """)
        (shim_dir / "sed").write_text(shim_script)
        (shim_dir / "sed").chmod(0o755)

        sid = "session-live-despite-sed-failure"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        (marker_dir / sid).write_text(str(os.getpid()))

        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
                extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
            )
            == "allow"
        )

    def test_live_marker_still_denied_by_total_sed_absence(
        self, isolated_home, tmp_path
    ):
        """GH-869: the active-marker check runs before the fragment-split
        fail-closed deny but after _lib_strip_shell_quotes's own deny, so a
        live marker does not rescue a total sed absence. That earlier deny
        still fires and leaves the marker untouched."""
        farm_dir = tmp_path / "path-without-sed"
        farm_dir.mkdir()
        restricted_path = build_path_without("sed", farm_dir)

        sid = "session-live-total-sed-absence"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / sid
        marker.write_text(str(os.getpid()))

        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                extra_env={"PATH": restricted_path},
            )
            == "deny"
        )
        assert marker.exists(), "the pre-SESSION_ID deny must not evict the marker"

    def test_dead_pid_active_marker_evicts_on_non_gated_command(
        self, isolated_home, repo_on_feature_branch
    ):
        """The active-marker check runs before the command-shape filter, so
        a non-gated command also evicts an orphaned dead-PID marker. The
        command itself is still allowed, since it never reaches a gated
        shape."""
        sid = "session-dead-pid-non-gated"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / sid
        marker.write_text("99999999")  # PID outside Linux/macOS max range → always dead
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("pytest -q", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )
        assert not marker.exists(), "hook must evict the orphan marker on dead PID"

    def test_gated_command_from_non_repo_cwd_still_refreshes_live_marker(
        self, isolated_home, tmp_path
    ):
        """GH-869: the active-marker check runs before REPO_ROOT resolution,
        so a live marker still refreshes even when the gated command's cwd
        is not a git repo (REPO_ROOT resolves empty)."""
        sid = "session-gated-non-repo-cwd"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / sid
        marker.write_text(str(os.getpid()))
        old_time = time.time() - 300
        os.utime(marker, (old_time, old_time))

        non_repo_dir = tmp_path / "not-a-repo"
        non_repo_dir.mkdir()
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=non_repo_dir,
            )
            == "allow"
        )
        assert marker.stat().st_mtime > old_time + 1, (
            "a live marker must refresh even when the gated command's cwd "
            "is not a git repo"
        )

    def test_live_marker_authorizes_push_on_a_different_branch_than_activation(
        self, isolated_home, repo_on_feature_branch
    ):
        """A live marker authorizes a push on a branch unrelated to whatever
        context it was originally activated under. The marker was never
        scoped to a specific PR/branch, before or after this diff. The
        widened refresh trigger only makes this pre-existing non-scoping
        newly reachable more often, not new in kind. See
        test_non_gated_command_advances_active_marker_mtime for the refresh
        mechanism itself, pinned separately."""
        sid = "session-cross-branch-reuse"
        marker_dir = isolated_home / ".claude" / ".ready-for-review-active.d"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / sid
        marker.write_text(str(os.getpid()))

        # A branch unrelated to whatever PR the marker was activated under --
        # the marker was never branch-scoped, before or after this diff.
        subprocess.run(
            ["git", "checkout", "-q", "-b", "unrelated-pr-branch"],
            cwd=repo_on_feature_branch,
            check=True,
        )

        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin unrelated-pr-branch", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    # -- Completion-marker check ------------------------------------------

    def test_no_marker_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_completion_marker_matching_head_allows(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        sid = "s"
        marker = rfr_completion_marker(isolated_home, repo_on_feature_branch, sid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(repo_on_feature_branch) + "\n")
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_completion_marker_stale_head_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """New commit after gate ran → HEAD moves → marker stale → deny."""
        sid = "s"
        marker = rfr_completion_marker(isolated_home, repo_on_feature_branch, sid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(repo_on_feature_branch) + "\n")
        (repo_on_feature_branch / "f").write_text("b\n")
        subprocess.run(
            ["git", "add", "f"], cwd=repo_on_feature_branch, check=True
        )
        subprocess.run(
            ["git", "commit", "-qm", "more"],
            cwd=repo_on_feature_branch,
            check=True,
        )
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.timing
    def test_current_head_git_timeout_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, git_timeout_shim
    ):
        """The CURRENT_HEAD `git rev-parse HEAD` call's _lib_capped exit
        status must fail closed on timeout. This mirrors how an unresolvable
        HEAD already denies per the completion-marker check's own
        fail-closed comment. A stalled filesystem must not hang the gate
        indefinitely. Seeds a completion marker for the branch's real HEAD,
        so an uncapped, fully-resolved CURRENT_HEAD would match it and
        allow. That seeding is what makes `decision == "deny"` actually
        discriminate a working cap (empty CURRENT_HEAD, no match) from a
        broken one, rather than passing on every path because no marker
        exists at all."""
        sid = "s"
        marker = rfr_completion_marker(isolated_home, repo_on_feature_branch, sid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(repo_on_feature_branch) + "\n")

        env = git_timeout_shim('[ "$1" = "rev-parse" ] && [ "$2" = "HEAD" ]')
        with assert_cap_engaged():
            decision = run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
                extra_env=env,
            )
        assert decision == "deny"

    @pytest.mark.timing
    def test_repo_root_git_timeout_allows(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, git_timeout_shim
    ):
        """Required regression test for a fail-open path: the header
        documents REPO_ROOT's git-timeout as the only one of this hook's
        rev-parse/symbolic-ref timeout paths that allows directly rather
        than falling through to the gate below. A timed-out `git rev-parse
        --show-toplevel` leaves REPO_ROOT empty, matching the
        `[ -z "$REPO_ROOT" ]` early exit — inverting the baseline deny this
        fixture combination otherwise produces."""
        env = git_timeout_shim('[ "$1" = "rev-parse" ] && [ "$2" = "--show-toplevel" ]')
        with assert_cap_engaged():
            decision = run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="s"),
                cwd=repo_on_feature_branch,
                extra_env=env,
            )
        assert decision == "allow"

    @pytest.mark.timing
    def test_current_branch_git_timeout_arms_the_gate(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, git_timeout_shim
    ):
        """Checked out on `main`, the repo's default branch.

        Timing out CURRENT_BRANCH's `git rev-parse --abbrev-ref HEAD` leaves
        it empty, withholding the default-branch bypass and falling through
        to the gate below.

        The match condition targets `$1 = "rev-parse"` and
        `$2 = "--abbrev-ref"` specifically so it doesn't also shadow the
        CURRENT_HEAD call's `git rev-parse HEAD`.

        With an open PR and no completion marker, the gate then denies."""
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo_on_feature_branch, check=True)
        env = git_timeout_shim('[ "$1" = "rev-parse" ] && [ "$2" = "--abbrev-ref" ]')
        with assert_cap_engaged():
            decision = run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin main", session_id="s"),
                cwd=repo_on_feature_branch,
                extra_env=env,
            )
        assert decision == "deny"

    @pytest.mark.timing
    def test_default_branch_symbolic_ref_timeout_still_allows_via_candidate_loop(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, git_timeout_shim
    ):
        """Checked out on `main`, the repo's default branch.

        Timing out _lib_default_branch_from_origin_head's `git -C <repo>
        symbolic-ref --quiet refs/remotes/origin/HEAD` lookup does not, by
        itself, withhold the default-branch bypass. The candidate-loop
        fallback's `git -C <repo> rev-parse --verify origin/main` still
        resolves quickly against the plain `refs/remotes/origin/main` ref
        repo_on_feature_branch sets up, so DEFAULT_BRANCH still gets set and
        the bypass still fires.

        `fake_output` gives a broken cap a decision-flipping outcome: naming
        a real-but-non-default ref (`origin/develop`, set up here) rather
        than a nonexistent one, since the helper's own verify step would
        otherwise reject a nonexistent ref and fall through to the same
        candidate-loop result as a working cap, losing the distinguishing
        power this test needs. If the cap fails, the full
        sleep completes and the shim emits this ref, resolving
        `DEFAULT_BRANCH="develop"`, which mismatches CURRENT_BRANCH and
        withholds the bypass instead of allowing — versus a working cap,
        where the shim is killed mid-sleep, this call stays empty, and the
        candidate loop still recovers "main"."""
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo_on_feature_branch, check=True)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/develop", "HEAD"],
            cwd=repo_on_feature_branch,
            check=True,
        )
        env = git_timeout_shim(
            '[ "$3" = "symbolic-ref" ]', fake_output="refs/remotes/origin/develop"
        )
        with assert_cap_engaged():
            decision = run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin main", session_id="s"),
                cwd=repo_on_feature_branch,
                extra_env=env,
            )
        assert decision == "allow"

    @pytest.mark.timing
    def test_candidate_loop_exhausted_arms_the_gate(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, git_timeout_shim
    ):
        """Checked out on `main`, the repo's default branch.

        DEFAULT_BRANCH's direct `symbolic-ref` lookup already fails to
        resolve on its own (repo_on_feature_branch configures no
        `refs/remotes/origin/HEAD`), so timing out the candidate loop's
        `git -C <repo> rev-parse --verify origin/main` — the only candidate
        with a ref to resolve against, since `master` and `develop` have
        none — exhausts the whole loop and leaves DEFAULT_BRANCH empty,
        withholding the default-branch bypass.

        With an open PR and no completion marker, the gate then denies."""
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo_on_feature_branch, check=True)
        env = git_timeout_shim(
            '[ "$3" = "rev-parse" ] && [ "$4" = "--verify" ] && [ "$5" = "origin/main" ]'
        )
        with assert_cap_engaged():
            decision = run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin main", session_id="s"),
                cwd=repo_on_feature_branch,
                extra_env=env,
            )
        assert decision == "deny"

    def test_dangling_origin_head_bypass_granted_on_coincidental_candidate_name_match(
        self, isolated_home, tmp_path
    ):
        """Pins the accepted-and-tracked (GH-899) widened fail-open: origin/
        HEAD's symref resolves to `refs/remotes/origin/main`, but that target
        ref was never created locally (dangling) --
        `_lib_default_branch_from_origin_head`'s verify step rejects it and
        falls through to the candidate loop. Only `refs/remotes/origin/
        develop` is live, so the loop lands on "develop".

        The branch checked out is also literally named `develop` -- purely
        by coincidence, not because it is the repo's real default (which
        origin/HEAD claims, unverifiably, to be `main`). CURRENT_BRANCH and
        the guessed DEFAULT_BRANCH match, so the gate grants the
        default-branch bypass and allows the push, with no open-PR check
        ever run. This is the exact scenario GH-899 tracks as an accepted
        risk, not a regression to fix here -- the test exists so a future
        change to this boundary is a visible, reviewed decision."""
        repo = tmp_path / "rfr-dangling"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "f").write_text("a\n")
        subprocess.run(["git", "add", "f"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
        subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/develop", "HEAD"],
            cwd=repo,
            check=True,
        )
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin develop", session_id="s"),
                cwd=repo,
            )
            == "allow"
        )

    @pytest.mark.timing
    def test_gh_pr_view_timeout_allows(
        self, isolated_home, repo_on_feature_branch, gh_timeout_shim
    ):
        """The `gh pr view` network call's own `_lib_capped` cap must actually
        engage.

        A hung `gh` leaves PR_NUMBER empty, matching the `[ -z "$PR_NUMBER" ]`
        fail-open check the same way an outright `gh` failure does (see
        test_gh_failure_fails_open).

        `fake_output` gives a broken cap a decision-flipping outcome: if the
        cap fails, the full sleep completes and the shim emits a
        plausible-but-wrong PR number instead of the real `gh` call's own
        empty result, so PR_NUMBER is non-empty and the hook proceeds to the
        completion-marker check — with no marker, that denies instead of
        allowing."""
        env = gh_timeout_shim('[ "$1" = "pr" ] && [ "$2" = "view" ]', fake_output="999")
        with assert_cap_engaged():
            decision = run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="s"),
                cwd=repo_on_feature_branch,
                extra_env=env,
            )
        assert decision == "allow"

    def test_other_sessions_completion_marker_authorizes_at_same_head(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """A's completion marker authorizes B's push at the same HEAD SHA.

        The stored SHA names the exact artifact the gate ran against, so it is
        the authorization; the filename's session suffix only keeps parallel
        sessions from overwriting each other's markers."""
        head = head_sha(repo_on_feature_branch)
        marker_a = rfr_completion_marker(isolated_home, repo_on_feature_branch, "A")
        marker_a.parent.mkdir(parents=True, exist_ok=True)
        marker_a.write_text(head + "\n")
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="B"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_other_sessions_completion_marker_does_not_authorize_moved_head(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """The negative half: acceptance is by HEAD SHA, not by marker existence.

        A new commit moves HEAD past what the gate actually reviewed, so the
        marker must stop authorizing — otherwise dropping the session key would
        degrade the gate to an existence check."""
        marker_a = rfr_completion_marker(isolated_home, repo_on_feature_branch, "A")
        marker_a.parent.mkdir(parents=True, exist_ok=True)
        marker_a.write_text(head_sha(repo_on_feature_branch) + "\n")

        (repo_on_feature_branch / "after_gate.py").write_text("print('ungated')\n")
        subprocess.run(["git", "add", "after_gate.py"], cwd=repo_on_feature_branch, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "commit after the gate ran"],
            cwd=repo_on_feature_branch, check=True,
        )

        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="B"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_marker_under_another_repo_hash_does_not_authorize(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, tmp_path
    ):
        """The repo-hash prefix stays part of the read predicate.

        Only the session suffix is globbed. All four gates share this read
        shape, so this invariant is pinned per-gate rather than once — a change
        that widens the glob for this hook alone must fail here."""
        other_repo = tmp_path / "other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=other_repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=other_repo, check=True)

        # Correct HEAD sha, filed under the other repo's hash prefix.
        decoy = rfr_completion_marker(isolated_home, other_repo, "A")
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text(head_sha(repo_on_feature_branch) + "\n")

        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="B"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_no_session_id_denies_when_pr_exists(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Fail-closed: no session_id and no gate run covering HEAD → deny.

        session_id is needed only for the active-bypass marker; the completion
        read no longer consults it. This pins that an unparseable payload still
        denies rather than becoming an allow path."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    # -- gh pr ready ------------------------------------------------------

    def test_gh_pr_ready_no_marker_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Marking a draft PR ready is a handoff signal — gate it the same
        way as push."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("gh pr ready", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_gh_pr_ready_with_completion_marker_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        sid = "s"
        marker = rfr_completion_marker(isolated_home, repo_on_feature_branch, sid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(repo_on_feature_branch) + "\n")
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("gh pr ready", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin --delete feature && gh pr ready",
            "git push origin -d feature && gh pr ready",
            "git push origin :feature && gh pr ready",
            "git push origin --tags && gh pr ready",
        ],
    )
    def test_bypassable_push_shapes_chained_before_pr_ready_deny(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """gh pr ready denies even when a bypassable push fragment is chained
        ahead of it. This test exercises the fragment-loop's continue-
        scanning behavior; push-shape classification itself is covered by
        test_bypassable_push_does_not_exempt_a_chained_gated_fragment
        (GH-773)."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    # -- gh pr create -------------------------------------------------------

    def test_gh_pr_create_no_marker_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        """Creating a PR is the publication boundary — gate it even though
        the branch has no open PR yet (fake_gh_no_pr, not fake_gh_pr_exists:
        a PR being created does not exist at gh-pr-view time, so a fixture
        that reports one existing would pass regardless of whether the
        PR-existence early-return is actually skipped for this arm)."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("gh pr create", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_gh_pr_create_with_completion_marker_allowed(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        sid = "s"
        marker = rfr_completion_marker(isolated_home, repo_on_feature_branch, sid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(repo_on_feature_branch) + "\n")
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("gh pr create", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_gh_pr_create_stale_marker_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        """A completion marker present but pointing at a stale (non-current)
        HEAD must still deny. This distinguishes 'the marker-check code path
        was reached and correctly rejected a mismatch' from
        test_gh_pr_create_no_marker_denies, which (using fake_gh_no_pr) would
        also deny via the pre-existing fail-open branch even if the
        marker-check code were unreachable for this arm — a stale marker
        only denies if that code actually runs."""
        sid = "s"
        marker = rfr_completion_marker(isolated_home, repo_on_feature_branch, sid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("0" * 40 + "\n")
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("gh pr create", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_gh_pr_create_with_flags_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(
                    'gh pr create --title "foo" --body "bar"', session_id="s"
                ),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_gh_pr_create_chained_after_push_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        """A chained `git push && gh pr create` must still gate on the
        gh pr create fragment even though the push fragment (no PR yet)
        would bypass on its own."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(
                    "git push origin feature && gh pr create", session_id="s"
                ),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_gh_pr_create_chained_after_dry_run_push_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        """A --dry-run push exempts only its own fragment, so the chained
        gh pr create still gates (GH-773)."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(
                    "git push --dry-run && gh pr create", session_id="s"
                ),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin --delete feature && gh pr create",
            "git push origin -d feature && gh pr create",
            "git push origin :feature && gh pr create",
            "git push origin --tags && gh pr create",
        ],
    )
    def test_bypassable_push_shapes_chained_before_pr_create_deny(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr, command
    ):
        """gh pr create denies even when a bypassable push fragment is
        chained ahead of it. This test exercises the fragment-loop's
        continue-scanning behavior; push-shape classification itself is
        covered by test_bypassable_push_does_not_exempt_a_chained_gated_fragment
        (GH-773)."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push --dry-run && git push origin feature",
            "git push origin feature && git push --tags origin",
            "git push origin --delete feature && git push origin feature",
            "git push origin -d feature && git push origin feature",
            "git push --dry-run && gh pr ready",
            "git push origin :feature && git push origin feature",
        ],
    )
    def test_bypassable_push_does_not_exempt_a_chained_gated_fragment(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """A bypassable push fragment exempts only itself: a second, gated
        fragment chained after it — a real push, or gh pr ready — still
        gates. Per parametrize case:

        - dry-run push chained before a real push: the real push still gates.
        - real push chained before a tag-only push: pins a fragment-scoping
          regression, since a real branch push must not be exempted just
          because a tag-only push is chained after it.
        - delete push (long form) chained before a real push: the real push
          still gates.
        - delete push (short form, -d) chained before a real push: the
          short-flag form of the delete gate is covered too.
        - dry-run push chained before gh pr ready: the gh pr ready arm
          inherits the same fix.
        - colon-refspec delete-only push chained before a real push: the
          colon-refspec arm's own version of the same regression."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "echo --dry-run && git push origin feature",
            "echo :note && git push origin feature",
        ],
    )
    def test_bypass_token_outside_the_push_fragment_does_not_exempt(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """An exemption-shaped token in a non-push fragment must not release
        the gate for a real push fragment chained after it."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "echo $(pwd) && git push --tags origin",
            "echo $(pwd) && git push origin :feature",
        ],
    )
    def test_unrelated_command_substitution_over_gates_a_safe_only_fragment(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """The command-substitution guard scans the whole command, not just
        the push fragment: a $(...) anywhere in the chain disqualifies a
        tags-only or colon-refspec-only fragment's bypass, even when the
        substitution is unrelated to the push. Makes the documented
        whole-command-scoped guard's behavior explicit rather than
        implicit."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "eval git push --dry-run && gh pr create",
            "GIT_DIR=/tmp/example.git git push --dry-run && gh pr create",
        ],
    )
    def test_wrapped_dry_run_chained_before_pr_create_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr, command
    ):
        """A wrapped --dry-run push (eval/env-prefixed) exempts only its own
        fragment; the chained gh pr create still gates, same as the unwrapped
        form. Uses a real repo with no completion marker, so the assertion
        exercises the gh-pr-create arm's marker check rather than the hook's
        not-a-git-repo fallback."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "/usr/bin/gh pr create",
            "/usr/bin/gh pr ready",
        ],
    )
    def test_full_path_gh_invocation_bypasses_detection(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """A full-path `gh` invocation is a documented gap (see hook
        header). Both arms detect via plain-text regex on the literal
        `gh pr ready`/`gh pr create` tokens. `/usr/bin/gh` doesn't match
        because `gh` there isn't preceded by whitespace or start-of-string.

        fake_gh_pr_exists (a real open PR) and no completion marker are the
        strictest available inputs: if detection ever started matching this
        shape, the same command would deny here instead of allow. Proves
        this gap is risk-neutral by test, not only by header prose.
        """
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "gh --repo o/r pr create",
            "gh --repo o/r pr ready",
        ],
    )
    def test_gh_repo_flag_before_subcommand_bypasses_detection(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """A documented gap, not yet fixed: both gh arms match `gh pr
        ready`/`gh pr create` by strict word adjacency, so a flag interposed
        before the subcommand — `--repo o/r` here — defeats detection. This
        is an inverted assertion pinning the current gap, not the fix; see
        the tracking issue linked from this hook's allowlist entry in
        test_hook_alignment.py.

        fake_gh_pr_exists (a real open PR) and no completion marker are the
        strictest available inputs: if detection ever started matching this
        shape, the same command would deny here instead of allow. Proves
        this gap is risk-neutral by test, not only by header prose.
        """
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_git_invocation_before_a_bare_ampersand_defeats_push_detection(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """A documented gap (see hook header): a bare `&` isn't a fragment
        boundary, so the git-word scan locks onto the earlier `git status`
        instead of the real `git push` that follows it.

        fake_gh_pr_exists (a real open PR) and no completion marker are the
        strictest available inputs: if detection ever started matching this
        shape, the same command would deny here instead of allow. Proves
        this gap is risk-neutral by test, not only by header prose.
        """
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(
                    "git status & git push origin feature", session_id="s"
                ),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_full_path_git_push_invocation_detected(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Unlike the gh-pr-ready/gh-pr-create arms above, the git-push arm
        detects via `_lib_fragment_invokes_git`'s token-walking tokenizer,
        not a plain-text regex on the literal `git push` tokens — so a
        full-path `/usr/bin/git push` is still caught (see hook header).
        Pins the header's documented asymmetry in both directions, not just
        the gap side test_full_path_gh_invocation_bypasses_detection above
        already covers.
        """
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("/usr/bin/git push origin feature", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_quoted_git_word_push_still_gated(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """GH-783: a quoted git word (`"git" push`) must still be detected
        as a gated push. Bash word-splitting does not remove quote
        characters, so without the hook's own COMMAND_UNQUOTED strip the
        fragment word `"git` never matches _lib_fragment_invokes_git's bare
        `git` comparison and the push sails through unreviewed."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input('"git" push origin feature', session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_dash_capital_c_space_containing_quoted_value_allow_unchanged_by_strip(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """GH-783's word-count-invariance claim, exercised rather than only
        hand-traced: a `-C` value containing a literal space is a
        pre-existing, accepted misparse (the naive whitespace word-walk
        splits inside the quotes both before and after
        COMMAND_UNQUOTED's own quote-stripping, since stripping removes
        only the quote characters, not the space) — the subcommand word
        resolves to the value's second half ("dir"), never "push", so the
        push goes undetected and the command allows. This pins that the
        fix does not change that pre-existing outcome, not that the
        outcome itself is desirable."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input('git -C "my dir" push origin feature', session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_sed_absent_from_path_denied(self, tmp_path):
        """GH-783: COMMAND_UNQUOTED is computed before any repo/gh state is
        read, so a missing sed must deny (fail-closed) rather than let
        _lib_strip_shell_quotes's failure silently collapse fragment
        detection and fall through to this gate's normal allow path."""
        farm_dir = tmp_path / "path-without-sed"
        farm_dir.mkdir()
        restricted_path = build_path_without("sed", farm_dir)
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="s"),
                extra_env={"PATH": restricted_path},
            )
            == "deny"
        )

    def test_fragments_split_sed_failure_denied(self, isolated_home, tmp_path):
        """GH-783: FRAGMENTS_SPLIT_EXIT must fail closed on its own, isolated
        from COMMAND_UNQUOTED_EXIT above -- both checks depend on the same
        sed binary, so a total sed-absent test (like the one above) can't
        tell which of the two is actually catching the failure. A sed shim
        fails on any invocation that isn't _lib_strip_shell_quotes's own
        `-e`-flagged shape, so COMMAND_UNQUOTED succeeds via the real sed
        while the later _lib_split_fragments call (a bare `sed -E
        's/.../g'`, no `-e` token) fails on its own. isolated_home: the
        active-marker check runs above this deny, so this test reaches that
        check on its way to denying -- without a sandboxed $HOME it would run
        that check against the real ambient $HOME."""
        real_sed = shutil.which("sed")
        assert real_sed, "test host must have a real sed binary on PATH"

        shim_dir = tmp_path / "sed-fails-outside-strip-shell-quotes-shape"
        shim_dir.mkdir()
        shim_script = textwrap.dedent(f"""\
            #!/bin/bash
            if [ "$2" != "-e" ]; then
              exit 1
            fi
            exec "{real_sed}" "$@"
        """)
        (shim_dir / "sed").write_text(shim_script)
        (shim_dir / "sed").chmod(0o755)

        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="s"),
                extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
            )
            == "deny"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "eval gh pr create",
            "xargs gh pr create",
            "gh pr create;",
            "(cd /wt; gh pr create)",
            "GH_TOKEN=x gh pr create",
        ],
    )
    def test_gh_pr_create_wrapper_shapes_denied(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr, command
    ):
        """Mirrors test_command_shapes_that_escaped_old_regex_are_denied's
        git-push coverage for the gh-pr-create detection regex."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_gh_pr_create_echo_false_positive_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        """Known, pre-existing limitation shared with is_gh_pr_ready: the
        detection regex scans raw fragment text rather than command
        position, so a command that merely mentions "gh pr create" (without
        invoking it) is denied too. Fail-closed, not a security gap — pins
        the current behavior so a future regex change doesn't silently flip
        it to a false negative instead."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("echo gh pr create", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_deny_message_names_pr_create(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        """The deny message must name gh pr create specifically, not fall
        into the push-worded branch (the bug this arm fixes)."""
        env = os.environ.copy()
        env["HOME"] = str(isolated_home)
        result = subprocess.run(
            [str(READY_FOR_REVIEW_HOOK)],
            input=json.dumps(bash_input("gh pr create", session_id="s")),
            capture_output=True,
            text=True,
            cwd=repo_on_feature_branch,
            env=env,
            check=False,
        )
        payload = json.loads(result.stdout)
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        assert "PR creation —" in reason
        assert "Push to a branch" not in reason

    # -- gh failure → fail-open ------------------------------------------

    def test_gh_failure_fails_open(
        self, isolated_home, repo_on_feature_branch, fake_gh_no_pr
    ):
        """If gh pr view errors (network glitch, gh not configured), the gate
        does not fire — keeps the user unblocked. The skill's prose triggers
        still cover this case."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    # -- Skill ↔ hook alignment ------------------------------------------

    def test_skill_activate_command_creates_active_marker(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """SKILL.md activate-gate fixture must produce a marker the hook
        recognizes as a fresh active-session bypass."""
        sid = "test-session-rfr-activate"
        _seed_session(isolated_home, sid)

        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        ), "precondition: gated push must deny before activation"

        cmd = extract_skill_command(READY_FOR_REVIEW_SKILL, "activate-gate")
        run_skill_command(cmd, cwd=repo_on_feature_branch, isolated_home=isolated_home)

        marker = rfr_active_marker(isolated_home, sid)
        assert marker.exists(), (
            "SKILL.md activate-gate recipe ran but no marker landed at the "
            "path the hook checks — skill and hook disagree on layout."
        )
        assert marker.read_text().strip().isdigit(), (
            "activate-gate marker must contain a unix epoch timestamp "
            f"(parseable integer), got: {marker.read_text().strip()!r}"
        )
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    def test_skill_record_completion_command_creates_marker(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """SKILL.md record-completion fixture must produce a marker keyed by
        repo-hash + session-id with HEAD SHA as content, recognized by the
        hook for HEAD-matching pushes."""
        sid = "test-session-rfr-complete"
        _seed_session(isolated_home, sid)

        cmd = extract_skill_command(READY_FOR_REVIEW_SKILL, "record-completion")
        run_skill_command(cmd, cwd=repo_on_feature_branch, isolated_home=isolated_home)

        marker = rfr_completion_marker(isolated_home, repo_on_feature_branch, sid)
        assert marker.exists(), (
            "SKILL.md record-completion recipe ran but no marker landed at "
            "the path the hook checks — skill and hook disagree on layout."
        )
        assert marker.read_text().strip() == head_sha(repo_on_feature_branch), (
            "completion marker must hold the local HEAD SHA"
        )
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
            )
            == "allow"
        )

    # -- Command-shape coverage (regex-gap fixes) -------------------------

    @pytest.mark.parametrize(
        "command",
        [
            "git -C /some/wt push origin feature",
            "git --git-dir=/some/wt/.git --work-tree=/some/wt push",
            "GIT_DIR=/some/wt/.git git push",
            "eval git push",
            "xargs git push",
            "git push;",
            "(cd /wt; git push)",
        ],
    )
    def test_command_shapes_that_escaped_old_regex_are_denied(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, command
    ):
        """Command forms that the old regex missed must now be detected and gated."""
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input(command, session_id="s"),
                cwd=repo_on_feature_branch,
            )
            == "deny"
        )

    def test_cwd_json_field_used_for_branch_resolution(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Hook must read .cwd from the JSON payload, not the inherited process CWD,
        for branch detection. When the payload's cwd points at a feature-branch
        repo but the hook process runs from an unrelated directory, the gate
        must still fire (not bypass via the default-branch check)."""
        import tempfile

        sid = "s"
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin feature"},
            "session_id": sid,
            "cwd": str(repo_on_feature_branch),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            assert (
                run_hook(
                    READY_FOR_REVIEW_HOOK,
                    payload,
                    cwd=Path(tmpdir),
                )
                == "deny"
            )

    def test_skill_deactivate_command_removes_active_marker(
        self, isolated_home, repo_on_feature_branch
    ):
        """SKILL.md deactivate-gate fixture must remove this session's active
        marker so a subsequent push (without completion marker) is re-gated."""
        sid = "test-session-rfr-deactivate"
        _seed_session(isolated_home, sid)

        activate_cmd = extract_skill_command(READY_FOR_REVIEW_SKILL, "activate-gate")
        run_skill_command(activate_cmd, cwd=repo_on_feature_branch, isolated_home=isolated_home)
        marker = rfr_active_marker(isolated_home, sid)
        assert marker.exists(), "activate-gate setup did not create the marker"

        deactivate_cmd = extract_skill_command(
            READY_FOR_REVIEW_SKILL, "deactivate-gate"
        )
        run_skill_command(deactivate_cmd, cwd=repo_on_feature_branch, isolated_home=isolated_home)
        assert not marker.exists(), (
            "SKILL.md deactivate-gate recipe ran but the marker is still "
            "present — skill and hook disagree on the marker path."
        )


class TestRequireReadyForReviewHonorsConfigDir:
    """CLAUDE_CONFIG_DIR relocates the ready-for-review marker directory the
    same way for marker.sh (write) and this hook (read) -- see marker.sh and
    the cross-account bypass this closes (ledger row 7)."""

    def test_marker_under_matching_config_dir_allows(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, tmp_path
    ):
        """CLAUDE_CONFIG_DIR-set happy path: a marker written under the
        resolved config dir satisfies the gate when the session runs under
        the same value."""
        profile = tmp_path / "profile"
        sid = "session-config-dir-match"
        marker = rfr_completion_marker(
            isolated_home, repo_on_feature_branch, sid, config_dir=profile
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(repo_on_feature_branch) + "\n")
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
                extra_env={"CLAUDE_CONFIG_DIR": str(profile)},
            )
            == "allow"
        )

    def test_marker_under_different_config_dir_does_not_authorize(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists, tmp_path
    ):
        """Cross-account bypass regression: a marker written under one
        CLAUDE_CONFIG_DIR value must not satisfy the gate when the session
        runs under a different one."""
        profile_a = tmp_path / "profile-a"
        profile_b = tmp_path / "profile-b"
        sid = "session-config-dir-mismatch"
        marker = rfr_completion_marker(
            isolated_home, repo_on_feature_branch, sid, config_dir=profile_a
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(repo_on_feature_branch) + "\n")
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
                extra_env={"CLAUDE_CONFIG_DIR": str(profile_b)},
            )
            == "deny"
        )

    def test_unresolvable_config_dir_denies(
        self, isolated_home, repo_on_feature_branch, fake_gh_pr_exists
    ):
        """Fail closed: a relative CLAUDE_CONFIG_DIR (unresolvable) must deny
        the gate outright, even with a valid marker at the default location."""
        sid = "session-config-dir-unresolvable"
        marker = rfr_completion_marker(isolated_home, repo_on_feature_branch, sid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(head_sha(repo_on_feature_branch) + "\n")
        assert (
            run_hook(
                READY_FOR_REVIEW_HOOK,
                bash_input("git push origin feature", session_id=sid),
                cwd=repo_on_feature_branch,
                extra_env={"CLAUDE_CONFIG_DIR": "relative/path"},
            )
            == "deny"
        )
