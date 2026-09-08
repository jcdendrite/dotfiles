"""Tests for _lib.sh's _lib_reviewer_round_state_key and
_lib_reviewer_round_state_value (round3-review-consult-trigger plan).
Relational assertions only -- never a golden sha256 literal -- mirroring
test_marker_lib.py's TestLibActivePlanHash precedent for
_lib_active_plan_hash: the exact digest recipe is free to evolve as long as
the read side (require-architect-consult.sh) and write side
(log-reviewer-round.sh) agree, which these tests pin by calling the
functions directly rather than through either hook.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from helpers import (
    HOOKS_DIR,
    bare_remote_with_default_branch,
    build_conflicted_rebase,
    build_conflicted_revert,
    push_conflicting_edit_to_origin,
    staged_diff_hash_at_base,
)

LIB_SH = HOOKS_DIR / "_lib.sh"


def _init_repo(repo: Path, branch: str = "main") -> None:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("first\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _state_key(repo: Path) -> subprocess.CompletedProcess:
    """Shell out to the real _lib_reviewer_round_state_key -- a fresh bash
    subprocess each call, so a "repeat calls agree" assertion exercises two
    genuinely independent invocations, not a cached result."""
    return subprocess.run(
        ["bash", "-c", f'. "{LIB_SH}"; _lib_reviewer_round_state_key "$1"', "_", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )


def _state_value(repo: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **extra_env} if extra_env else None
    return subprocess.run(
        ["bash", "-c", f'. "{LIB_SH}"; _lib_reviewer_round_state_value "$1"', "_", str(repo)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _state_cap(config_dir: str | None) -> subprocess.CompletedProcess:
    """Shell out to the real _lib_reviewer_round_state_cap with
    CLAUDE_CONFIG_DIR set to config_dir (or unset when None) -- isolates
    from any ambient CLAUDE_CONFIG_DIR the real environment carries, per
    helpers._build_subprocess_env's documented caveat."""
    env = dict(os.environ)
    if config_dir is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return subprocess.run(
        ["bash", "-c", f'. "{LIB_SH}"; _lib_reviewer_round_state_cap'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


class TestLibReviewerRoundStateKey:
    def test_deterministic_across_repeat_calls(self, tmp_path):
        repo = tmp_path / "repeat-key"
        _init_repo(repo)
        first = _state_key(repo)
        second = _state_key(repo)
        assert first.returncode == 0
        assert second.returncode == 0
        assert first.stdout == second.stdout
        assert first.stdout != ""

    def test_differs_for_different_branch(self, tmp_path):
        repo = tmp_path / "diff-branch"
        _init_repo(repo, branch="main")
        main_key = _state_key(repo)
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
        feature_key = _state_key(repo)
        assert main_key.returncode == 0
        assert feature_key.returncode == 0
        assert main_key.stdout != feature_key.stdout

    def test_differs_for_different_repo_same_branch_name(self, tmp_path):
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        _init_repo(repo_a, branch="main")
        _init_repo(repo_b, branch="main")
        key_a = _state_key(repo_a)
        key_b = _state_key(repo_b)
        assert key_a.returncode == 0
        assert key_b.returncode == 0
        assert key_a.stdout != key_b.stdout

    def test_stable_across_staged_changes(self, tmp_path):
        """The key is branch-scoped, not diff-scoped -- staging a change
        must not move it (that is _lib_reviewer_round_state_value's job)."""
        repo = tmp_path / "stable-key"
        _init_repo(repo)
        before = _state_key(repo)
        (repo / "f.txt").write_text("first\nsecond\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        after = _state_key(repo)
        assert before.returncode == 0
        assert after.returncode == 0
        assert before.stdout == after.stdout

    def test_empty_on_detached_head(self, tmp_path):
        repo = tmp_path / "detached-key"
        _init_repo(repo)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        subprocess.run(["git", "checkout", "-q", sha], cwd=repo, check=True)
        result = _state_key(repo)
        assert result.returncode != 0
        assert result.stdout == ""

    def test_empty_on_empty_repo_root_argument(self):
        result = _state_key("")
        assert result.returncode != 0
        assert result.stdout == ""


class TestLibReviewerRoundStateValue:
    def test_deterministic_across_repeat_calls(self, tmp_path):
        repo = tmp_path / "repeat-value"
        _init_repo(repo)
        first = _state_value(repo)
        second = _state_value(repo)
        assert first.returncode == 0
        assert second.returncode == 0
        assert first.stdout == second.stdout
        assert first.stdout != ""

    def test_differs_after_commit(self, tmp_path):
        """A committed change moves the head-sha half of the pair, even
        though the staged diff resets to empty."""
        repo = tmp_path / "value-after-commit"
        _init_repo(repo)
        before = _state_value(repo)
        (repo / "f.txt").write_text("first\nsecond\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "wip"], cwd=repo, check=True)
        after = _state_value(repo)
        assert before.returncode == 0
        assert after.returncode == 0
        assert before.stdout != after.stdout

    def test_differs_for_different_staged_diff_same_head(self, tmp_path):
        repo = tmp_path / "value-different-diff"
        _init_repo(repo)
        (repo / "f.txt").write_text("first\nsecond\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        value_a = _state_value(repo)
        (repo / "f.txt").write_text("first\nthird\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        value_b = _state_value(repo)
        assert value_a.returncode == 0
        assert value_b.returncode == 0
        assert value_a.stdout != value_b.stdout

    def test_empty_when_no_commits_yet(self, tmp_path):
        repo = tmp_path / "no-commits"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        result = _state_value(repo)
        assert result.returncode != 0
        assert result.stdout == ""

    def test_empty_on_empty_repo_root_argument(self):
        result = _state_value("")
        assert result.returncode != 0
        assert result.stdout == ""

    def test_unknown_diff_state_returns_1_with_no_stdout(self, tmp_path):
        """A failed or capped `git diff --cached` -- the exact call
        _lib_staged_diff_hash hashes, caught via its own `${PIPESTATUS[0]}`
        check on that call rather than a separate probe -- must not be
        stored as though it were a real round state: a later genuinely empty
        diff at the same HEAD would otherwise `grep -qFx` match a poisoned
        line. Forced with a stub `git` that fails only the hashed call's
        exact argument shape, so the real HEAD resolution and the
        no-in-progress-state gitdir check still succeed."""
        real_git = shutil.which("git")
        repo = tmp_path / "unknown-diff-state"
        _init_repo(repo)
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text(
            '#!/bin/bash\n'
            'if [ "$3" = "diff" ] && [ "$4" = "--cached" ]; then\n'
            '  exit 128\n'
            'fi\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)
        result = _state_value(repo, extra_env={"PATH": f"{stub_dir}:{os.environ['PATH']}"})
        assert result.returncode != 0
        assert result.stdout == ""


class TestLibReviewerRoundStateKeyValueIndependence:
    """Cross-agreement between the key and value recipes, independent of
    either hook: the key (branch-scoped) and value (head+diff-scoped) must
    vary on different axes, or the round-state file's whole "one line per
    reviewed state, capped at 2" design would conflate a new commit on the
    SAME branch with a genuinely different branch."""

    def test_staging_a_change_moves_value_but_not_key(self, tmp_path):
        repo = tmp_path / "independence"
        _init_repo(repo)
        key_before = _state_key(repo)
        value_before = _state_value(repo)
        (repo / "f.txt").write_text("first\nchanged\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        key_after = _state_key(repo)
        value_after = _state_value(repo)
        assert key_before.stdout == key_after.stdout
        assert value_before.stdout != value_after.stdout


class TestLibReviewerRoundStateCap:
    """Contract for _lib_reviewer_round_state_cap, consumed by both hooks
    via `$(...)` into an integer comparison. Must always print a valid
    integer to stdout and never fail silently."""

    def test_default_cap_without_pilot_sentinel(self, tmp_path):
        config_dir = tmp_path / "config-dir"
        config_dir.mkdir()
        result = _state_cap(str(config_dir))
        assert result.returncode == 0
        assert result.stdout.strip() == "2"

    def test_cap_is_one_with_pilot_sentinel_present(self, tmp_path):
        config_dir = tmp_path / "config-dir"
        config_dir.mkdir()
        (config_dir / ".round-consult-round2-pilot").touch()
        result = _state_cap(str(config_dir))
        assert result.returncode == 0
        assert result.stdout.strip() == "1"

    def test_default_cap_on_unresolvable_config_dir(self):
        """A relative CLAUDE_CONFIG_DIR fails _lib_config_dir's own
        resolution -- the cap must still print the default rather than
        leaving stdout empty."""
        result = _state_cap("relative/config/dir")
        assert result.returncode == 0
        assert result.stdout.strip() == "2"


def _make_git_rejecting_write_tree(bin_dir: Path) -> Path:
    """Simulates git < 2.38: `merge-tree --write-tree` is rejected outright.
    Every other subcommand proxies to the real git. Local copy of
    test_lib.py's shim of the same name (DAMP test code, per CLAUDE.md's
    named exception) -- this file's fallback assertion needs its own stub,
    not a shared import, per the plan's "per call site" mandate."""
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


def _merge_tree_base(repo: Path) -> str:
    """Local copy of test_require_code_review.py's helper of the same name
    (DAMP test code): independently computes the reference tree
    _lib_gate_diff_base's merge row computes, via the literal MERGE_HEAD OID
    (not the ref name -- git embeds a merge-tree argument's own textual form
    into the conflict marker label, which would compute a byte-different
    tree from production's `merge-tree --write-tree HEAD "$state_oid"`)."""
    merge_head_oid = (repo / ".git" / "MERGE_HEAD").read_text().strip()
    out = subprocess.run(
        ["git", "merge-tree", "--write-tree", "HEAD", merge_head_oid],
        cwd=repo, capture_output=True, text=True, check=False,
    ).stdout
    return out.strip().splitlines()[0]


def _build_conflicted_merge_via_origin(tmp_path: Path) -> Path:
    """Local copy of test_require_code_review.py's fixture of the same
    name: a conflicted merge whose MERGE_HEAD is trusted via the
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


def _round_state_empty_base_diff_hash(base: str) -> str:
    """Independent oracle for _lib_reviewer_round_state_value's empty-base-
    relative-diff binding: sha256("round-state-empty-base:<base>"), computed
    directly in Python rather than by calling the shell function under
    test."""
    return hashlib.sha256(f"round-state-empty-base:{base}".encode()).hexdigest()


def _build_forged_anchor_clean_merge(
    tmp_path: Path, name: str = "repo", payload_content: str = "payload\n"
) -> Path:
    """Local copy of test_require_code_review.py's fixture of the same name
    (DAMP test code): the forged-anchor + clean-merge attack -- builds M via
    `git commit-tree` (no `git commit` subprocess), forges
    refs/remotes/origin/main to point at M directly (plain plumbing -- no
    push, no fetch, no attacker infrastructure), then runs an ordinary `git
    merge --no-ff --no-commit` against that forged ref. The merge
    auto-stages M's payload file with no conflict, so the base-relative diff
    is empty even though the payload is genuinely novel content nobody
    reviewed. `payload_content` distinguishes independently-forged repos'
    trees (and therefore their resolved bases) from one another."""
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


class TestLibReviewerRoundStateValueForgedAnchorEmptyBaseDiff:
    """Adversarial coverage for _lib_reviewer_round_state_value's own
    empty-diff branch, mirroring
    TestRequireCodeReviewForgedAnchorEmptyBaseDiff in
    test_require_code_review.py -- the same sha256("") collapse surface,
    reached through the round-state gate's own call site rather than the
    code-review marker's."""

    def test_two_different_forged_bases_produce_different_diff_hashes(self, tmp_path):
        """The sha256("") reuse concern: two independently-forged bases each
        landing on an empty base-relative diff must not collapse to the same
        diff-hash half -- that collapse is exactly what would let a
        round-state entry recorded against one forged base be replayed
        against a different, independently-forged one."""
        repo1 = _build_forged_anchor_clean_merge(tmp_path, name="repo1", payload_content="payload-1\n")
        repo2 = _build_forged_anchor_clean_merge(tmp_path, name="repo2", payload_content="payload-2\n")
        base1 = _merge_tree_base(repo1)
        base2 = _merge_tree_base(repo2)
        assert base1 != base2, "fixture must produce two distinct forged bases"

        result1 = _state_value(repo1)
        result2 = _state_value(repo2)
        assert result1.returncode == 0
        assert result2.returncode == 0
        _, diff_hash1 = result1.stdout.split(" ", 1)
        _, diff_hash2 = result2.stdout.split(" ", 1)
        assert diff_hash1 != diff_hash2
        assert diff_hash1 != hashlib.sha256(b"").hexdigest()
        assert diff_hash2 != hashlib.sha256(b"").hexdigest()

    def test_value_matches_independent_oracle_for_empty_base_relative_diff(self, tmp_path):
        repo = _build_forged_anchor_clean_merge(tmp_path)
        base = _merge_tree_base(repo)
        result = _state_value(repo)
        assert result.returncode == 0
        head_sha, diff_hash = result.stdout.split(" ", 1)
        expected_head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert head_sha == expected_head_sha
        assert diff_hash == _round_state_empty_base_diff_hash(base)


class TestLibReviewerRoundStateValueMergeAwareBase:
    """_lib_reviewer_round_state_value's diff-hash half routes through
    _lib_gate_diff_base/_lib_staged_diff_hash, so a mid-merge round state
    covers the resolution only. Pinned against an independent oracle rather
    than this file's usual relational-only convention (both sides calling
    the same function), because a relational-only check cannot catch a bug
    in a shared primitive both sides would inherit identically.

    These tests exercise the shared function directly rather than through
    both call sites (log-reviewer-round.sh, require-architect-consult.sh),
    because both hook scripts invoke it identically today with no
    per-caller divergence to catch -- if either script gains caller-specific
    pre/post-processing around the call, add a direct test at that point."""

    def test_mid_merge_value_matches_independent_oracle_on_both_halves(self, tmp_path):
        repo = _build_conflicted_merge_via_origin(tmp_path)
        result = _state_value(repo)
        assert result.returncode == 0
        head_sha, diff_hash = result.stdout.split(" ", 1)

        expected_head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert head_sha == expected_head_sha

        base = _merge_tree_base(repo)
        assert diff_hash == staged_diff_hash_at_base(repo, base)

    def test_mid_merge_key_stays_armed_head_attached(self, tmp_path):
        """HEAD stays attached through a merge, so the round-3 consult gate
        stays armed -- asserted directly rather than assumed."""
        repo = _build_conflicted_merge_via_origin(tmp_path)
        result = _state_key(repo)
        assert result.returncode == 0
        assert result.stdout != ""

    def test_mid_merge_value_recorded_still_matches_while_merge_in_progress(self, tmp_path):
        """A value recorded mid-merge still matches on a later read while
        that same merge is in progress -- no intervening state change moves
        it out from under a read that follows a write in the same window."""
        repo = _build_conflicted_merge_via_origin(tmp_path)
        recorded = _state_value(repo)
        read_again = _state_value(repo)
        assert recorded.returncode == 0
        assert read_again.returncode == 0
        assert recorded.stdout == read_again.stdout

    def test_mid_rebase_key_disarmed_empty_stdout(self, tmp_path):
        """Pinned as asserted behavior: mid-rebase, detached HEAD means
        _lib_reviewer_round_state_key returns non-zero with empty stdout,
        and the base substitution in the value's diff-hash half does not
        reach this branch-half gap."""
        repo = tmp_path / "rebase-key-disarmed"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        build_conflicted_rebase(repo)
        result = _state_key(repo)
        assert result.returncode != 0
        assert result.stdout == ""

    def test_fallback_write_tree_rejected_behaves_like_today(self, tmp_path):
        """`--write-tree` outright rejection (git < 2.38) must fall back to
        exactly today's plain HEAD-relative diff-hash recipe rather than
        hanging or erroring the round-state value."""
        repo = _build_conflicted_merge_via_origin(tmp_path)
        bin_dir = tmp_path / "bin-fallback-write-tree"
        _make_git_rejecting_write_tree(bin_dir)
        extra_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_GIT": shutil.which("git"),
        }
        result = _state_value(repo, env_overrides=extra_env)
        assert result.returncode == 0, result.stderr
        head_sha, diff_hash = result.stdout.split(" ", 1)
        expected_head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert head_sha == expected_head_sha
        expected_diff = subprocess.run(
            ["git", "diff", "--cached"], cwd=repo, capture_output=True, check=True
        ).stdout
        assert diff_hash == hashlib.sha256(expected_diff).hexdigest()

    def test_fallback_merge_base_flag_rejected_behaves_like_today(self, tmp_path):
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
        bin_dir = tmp_path / "bin-fallback-merge-base"
        _make_git_rejecting_merge_base_flag(bin_dir)
        extra_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_GIT": shutil.which("git"),
        }
        result = _state_value(repo, env_overrides=extra_env)
        assert result.returncode == 0, result.stderr
        head_sha, diff_hash = result.stdout.split(" ", 1)
        expected_head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert head_sha == expected_head_sha
        expected_diff = subprocess.run(
            ["git", "diff", "--cached"], cwd=repo, capture_output=True, check=True
        ).stdout
        assert diff_hash == hashlib.sha256(expected_diff).hexdigest()
