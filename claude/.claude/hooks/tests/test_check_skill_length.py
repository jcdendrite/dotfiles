"""Tests for check-skill-length.sh."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from helpers import (
    HOOKS_DIR,
    bare_remote_with_default_branch,
    bash_input,
    build_conflicted_rebase,
    build_conflicted_revert,
    build_path_without,
    edit_input,
    push_conflicting_edit_to_origin,
    resolve_conflicted_rebase,
    run_hook,
    run_hook_reason,
)

from .conftest import assert_cap_engaged

CHECK_SKILL_LENGTH_HOOK = HOOKS_DIR / "check-skill-length.sh"
SKILL_PATH = "claude-skills/skills/my-skill/SKILL.md"


def stub_bin_without_timeout(tmp_path: Path) -> Path:
    """Stub PATH with only the binaries this hook's code path invokes
    (`cat`/`jq` via _lib.sh's JSON parsing, `dirname` to locate _lib.sh,
    `sed`/`tr` for _lib_command_invokes_git_subcmd's git-commit match
    (GH-783), `grep` for the path-filter match, `awk` for the line
    count, `git` for the _lib_capped-wrapped show and diff --cached
    --name-only calls), omitting both timeout(1) and gtimeout(1). Mirrors
    test_require_worktree_for_git_writes.py's test_python3_absent_denies
    shape; skips (does not silently under-symlink) when a needed real
    binary is itself absent from the test machine."""
    stub_bin = tmp_path / "_stub_bin"
    stub_bin.mkdir()
    for tool in ("awk", "cat", "dirname", "git", "grep", "jq", "sed", "tr"):
        real_path = shutil.which(tool)
        if not real_path:
            pytest.skip(f"{tool} not found in PATH")
        (stub_bin / tool).symlink_to(real_path)
    return stub_bin


def make_skill_content(n: int, prefix: str = "line") -> str:
    """Return content with exactly n newline-terminated lines."""
    return "\n".join(f"{prefix} {i + 1}" for i in range(n)) + "\n"


def make_repo_with_skill(tmp_path: Path, head_lines: int) -> Path:
    """Git repo with SKILL.md committed at `head_lines` lines."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    skill_dir = repo / "claude-skills" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (repo / SKILL_PATH).write_text(make_skill_content(head_lines))
    subprocess.run(["git", "add", SKILL_PATH], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def skill_repo(tmp_path):
    """Git repo with SKILL.md committed at 190 lines."""
    return make_repo_with_skill(tmp_path, 190)


@pytest.fixture
def new_skill_repo(tmp_path):
    """Git repo with no committed SKILL.md — SKILL.md will be a new staged file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / "claude-skills" / "skills" / "my-skill").mkdir(parents=True)
    return repo


class TestCheckSkillLength:
    def test_non_commit_command_allows(self, isolated_home, skill_repo):
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        assert (
            run_hook(CHECK_SKILL_LENGTH_HOOK, bash_input("git status"), cwd=skill_repo)
            == "allow"
        )

    def test_non_bash_tool_allows(self, isolated_home, skill_repo):
        assert (
            run_hook(CHECK_SKILL_LENGTH_HOOK, edit_input("/tmp/foo.txt"), cwd=skill_repo)
            == "allow"
        )

    def test_outside_git_repo_allows(self, isolated_home, tmp_path):
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=non_repo,
            )
            == "allow"
        )

    def test_no_staged_skill_files_allows(self, isolated_home, skill_repo):
        (skill_repo / "other.txt").write_text("something\n")
        subprocess.run(["git", "add", "other.txt"], cwd=skill_repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=skill_repo,
            )
            == "allow"
        )

    def test_quoted_form_reaches_same_verdict_as_bare_form(self, isolated_home, skill_repo):
        """A quote-adjacent split (`"git" commit -m x`) must reach the same
        deny verdict as the unquoted form."""
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input('"git" commit -m foo'),
                cwd=skill_repo,
            )
            == "deny"
        )

    def test_sed_absent_from_path_denies(self, isolated_home, skill_repo, tmp_path):
        """Status-2 propagation: the matcher could not determine whether
        this command invokes git commit, and this gate's own documented
        fail-closed posture means an undetermined match denies rather than
        silently falling through to allow. Asserts the distinguishing
        reason text, not just the verdict, so this test cannot be
        satisfied by an ordinary over-limit deny reaching "deny" for the
        wrong reason."""
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        farm_dir = tmp_path / "path-without-sed"
        farm_dir.mkdir()
        restricted_path = build_path_without("sed", farm_dir)
        reason = run_hook_reason(
            CHECK_SKILL_LENGTH_HOOK,
            bash_input("git commit -m foo"),
            cwd=skill_repo,
            extra_env={"PATH": restricted_path},
        )
        assert reason is not None
        assert "could not determine" in reason

    def test_new_skill_over_limit_denies(self, isolated_home, new_skill_repo):
        """New file with no HEAD version staged at 201 lines — old defaults to 0 → deny."""
        (new_skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=new_skill_repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=new_skill_repo,
            )
            == "deny"
        )

    def test_staged_deletion_of_skill_allows(self, isolated_home, skill_repo):
        """git rm-staged SKILL.md: git show ":$f" produces empty output → new=0, 0 > 200 is false → allow."""
        subprocess.run(["git", "rm", "-q", SKILL_PATH], cwd=skill_repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=skill_repo,
            )
            == "allow"
        )

    def test_deny_message_includes_filename_and_counts(self, isolated_home, skill_repo):
        """Deny reason must name the file, new line count, old line count, and limit."""
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        reason = run_hook_reason(
            CHECK_SKILL_LENGTH_HOOK,
            bash_input("git commit -m foo"),
            cwd=skill_repo,
        )
        assert reason is not None
        assert SKILL_PATH in reason
        assert "201" in reason
        assert "190" in reason
        assert "200" in reason

    def test_code_review_over_default_under_override_allows(
        self, isolated_home, tmp_path
    ):
        """code-review/SKILL.md gets a 500-line cap; 300 lines (over 200, under 500) → allow."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        cr_path = "claude-skills/skills/code-review/SKILL.md"
        (repo / "claude-skills" / "skills" / "code-review").mkdir(parents=True)
        (repo / cr_path).write_text(make_skill_content(290))
        subprocess.run(["git", "add", cr_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / cr_path).write_text(make_skill_content(300))
        subprocess.run(["git", "add", cr_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_code_review_over_override_denies(self, isolated_home, tmp_path):
        """code-review/SKILL.md over the 500-line override and growing → deny."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        cr_path = "claude-skills/skills/code-review/SKILL.md"
        (repo / "claude-skills" / "skills" / "code-review").mkdir(parents=True)
        (repo / cr_path).write_text(make_skill_content(490))
        subprocess.run(["git", "add", cr_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / cr_path).write_text(make_skill_content(501))
        subprocess.run(["git", "add", cr_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_plan_review_uses_override(self, isolated_home, tmp_path):
        """plan-review/SKILL.md also gets the 500-line cap."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        pr_path = "claude-skills/skills/plan-review/SKILL.md"
        (repo / "claude-skills" / "skills" / "plan-review").mkdir(parents=True)
        (repo / pr_path).write_text(make_skill_content(290))
        subprocess.run(["git", "add", pr_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / pr_path).write_text(make_skill_content(300))
        subprocess.run(["git", "add", pr_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_plan_review_routing_md_uses_override(self, isolated_home, tmp_path):
        """plan-review/ROUTING.md also gets the 500-line cap: at/under it, growing → allow."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        routing_path = "claude-skills/skills/plan-review/ROUTING.md"
        (repo / "claude-skills" / "skills" / "plan-review").mkdir(parents=True)
        (repo / routing_path).write_text(make_skill_content(290))
        subprocess.run(["git", "add", routing_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / routing_path).write_text(make_skill_content(300))
        subprocess.run(["git", "add", routing_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_plan_review_routing_md_over_override_denies(self, isolated_home, tmp_path):
        """plan-review/ROUTING.md over the 500-line override and growing → deny."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        routing_path = "claude-skills/skills/plan-review/ROUTING.md"
        (repo / "claude-skills" / "skills" / "plan-review").mkdir(parents=True)
        (repo / routing_path).write_text(make_skill_content(490))
        subprocess.run(["git", "add", routing_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / routing_path).write_text(make_skill_content(501))
        subprocess.run(["git", "add", routing_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_pr_description_over_default_under_override_allows(
        self, isolated_home, tmp_path
    ):
        """pr-description/SKILL.md gets a 210-line cap; 205 lines (over 200, under 210) → allow."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        pr_path = "claude-skills/skills/pr-description/SKILL.md"
        (repo / "claude-skills" / "skills" / "pr-description").mkdir(parents=True)
        (repo / pr_path).write_text(make_skill_content(195))
        subprocess.run(["git", "add", pr_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / pr_path).write_text(make_skill_content(205))
        subprocess.run(["git", "add", pr_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_pr_description_over_override_denies(self, isolated_home, tmp_path):
        """pr-description/SKILL.md over the 210-line override and growing → deny."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        pr_path = "claude-skills/skills/pr-description/SKILL.md"
        (repo / "claude-skills" / "skills" / "pr-description").mkdir(parents=True)
        (repo / pr_path).write_text(make_skill_content(205))
        subprocess.run(["git", "add", pr_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / pr_path).write_text(make_skill_content(211))
        subprocess.run(["git", "add", pr_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_byte_limit_never_fires_for_this_caller(self, isolated_home, new_skill_repo):
        """check-skill-length.sh calls _lib_staged_length_gate in its 2-arg
        form, omitting BYTE_LIMIT — a new SKILL.md well over 25,600 bytes but
        under its line limit must still be allowed, proving the opt-in byte
        check stays opt-out for this caller."""
        (new_skill_repo / SKILL_PATH).write_text("a" * 30000 + "\n")
        subprocess.run(["git", "add", SKILL_PATH], cwd=new_skill_repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=new_skill_repo,
            )
            == "allow"
        )

    def test_memory_files_skill_falls_to_default_limit(self, isolated_home, tmp_path):
        """ai-instruction-and-memory-files/SKILL.md gets no per-skill override.

        Regression test: guards against a future `limit_for()` edit re-adding
        any override above the 200-line default for this path. 195 (init)
        sits under the default; 201 (restage) is the minimal over-default,
        growing value, so it denies here. A re-added override anywhere above
        200 would put 201 back under that override's own ceiling and allow
        instead, so 195/201 pins the default with no gap between the two
        behaviors.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        memory_path = "claude-skills/skills/ai-instruction-and-memory-files/SKILL.md"
        (repo / "claude-skills" / "skills" / "ai-instruction-and-memory-files").mkdir(
            parents=True
        )
        (repo / memory_path).write_text(make_skill_content(195))
        subprocess.run(["git", "add", memory_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / memory_path).write_text(make_skill_content(201))
        subprocess.run(["git", "add", memory_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_cwd_not_repo_root_does_not_cause_false_negative(
        self, isolated_home, skill_repo
    ):
        """Hook run from a repo subdirectory must still catch over-limit SKILL.md.

        Regression test: `git diff --cached --name-only` emits repo-root-relative
        paths. An earlier version had `[ -f "$f" ] || continue` which resolved
        those paths against CWD — if CWD was a subdirectory the check failed
        and the file was silently skipped (false negative, bloated skill slips
        through). The guard was removed; `git show ":$f"` reads from the index
        directly and doesn't depend on CWD.
        """
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        subdir = skill_repo / "claude-skills"
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=subdir,
            )
            == "deny"
        )

    # --- Fail-open regression: neither timeout(1) nor gtimeout(1) present ---

    def test_growing_over_limit_denies_when_neither_timeout_nor_gtimeout_present(
        self, isolated_home, skill_repo, tmp_path
    ):
        """Fail-open regression: with neither binary present, _lib_capped
        runs the git show calls uncapped (see _lib.sh) rather than silently
        skipping — the gate must still catch a growing over-limit SKILL.md."""
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        stub_bin = stub_bin_without_timeout(tmp_path)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=skill_repo,
                extra_env={"PATH": str(stub_bin)},
            )
            == "deny"
        )

    def test_at_limit_allows_when_neither_timeout_nor_gtimeout_present(
        self, isolated_home, skill_repo, tmp_path
    ):
        """Companion allow case for the deny above: under the same PATH, a
        SKILL.md at the limit (not growing past it) must still pass —
        without this, a fallback branch that always returns nonzero would
        masquerade as a working gate."""
        (skill_repo / SKILL_PATH).write_text(make_skill_content(200))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        stub_bin = stub_bin_without_timeout(tmp_path)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=skill_repo,
                extra_env={"PATH": str(stub_bin)},
            )
            == "allow"
        )

    # --- Repo-root plugin layout (`skills/<name>/SKILL.md`) ---

    def test_repo_root_skill_growing_to_201_denies(self, isolated_home, tmp_path):
        """Repo-root layout (`skills/<name>/SKILL.md`, used when a
        marketplace declares "source": "./"): HEAD at 190, staged at 201 →
        deny. Regression test for the third staged-path alternative."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        repo_root_path = "skills/my-skill/SKILL.md"
        (repo / "skills" / "my-skill").mkdir(parents=True)
        (repo / repo_root_path).write_text(make_skill_content(190))
        subprocess.run(["git", "add", repo_root_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / repo_root_path).write_text(make_skill_content(201))
        subprocess.run(["git", "add", repo_root_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_repo_root_skill_at_exactly_200_allows(self, isolated_home, tmp_path):
        """Repo-root layout at exactly the 200-line default: allow. There is
        no per-skill override path for a repo-root skill, so it always
        resolves to the 200-line default."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        repo_root_path = "skills/my-skill/SKILL.md"
        (repo / "skills" / "my-skill").mkdir(parents=True)
        (repo / repo_root_path).write_text(make_skill_content(190))
        subprocess.run(["git", "add", repo_root_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / repo_root_path).write_text(make_skill_content(200))
        subprocess.run(["git", "add", repo_root_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_vendored_skills_dir_not_anchored_at_repo_root_allows(
        self, isolated_home, tmp_path
    ):
        """`vendor/thing/skills/x/SKILL.md` staged at 201 lines must allow:
        the new repo-root alternative (`^skills/.+/SKILL\\.md$`) is anchored
        at the start of the path, so it must not also match a `skills/`
        directory nested under an unrelated prefix. The only test in this
        set that fails if the new alternative were written unanchored —
        every other test here passes whether or not anchoring is correct."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        vendor_path = "vendor/thing/skills/x/SKILL.md"
        (repo / "vendor" / "thing" / "skills" / "x").mkdir(parents=True)
        (repo / vendor_path).write_text(make_skill_content(201))
        subprocess.run(["git", "add", vendor_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_plugin_path_skill_growing_to_201_denies(self, isolated_home, tmp_path):
        """`plugins/some-plugin/skills/x/SKILL.md`: HEAD at 190, staged at
        201 → deny. Pins the pre-existing `plugins/[^/]+/skills/`
        alternative, which had no test coverage before this change — adding
        a third `|`-joined alternative to the same combined pattern is
        exactly the edit class that can silently corrupt a sibling
        alternative (misplaced pipe, unbalanced paren, changed precedence)
        with nothing else in this suite to catch it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        plugin_path = "plugins/some-plugin/skills/x/SKILL.md"
        (repo / "plugins" / "some-plugin" / "skills" / "x").mkdir(parents=True)
        (repo / plugin_path).write_text(make_skill_content(190))
        subprocess.run(["git", "add", plugin_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / plugin_path).write_text(make_skill_content(201))
        subprocess.run(["git", "add", plugin_path], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    # --- Newly-capped `git diff --cached --name-only` and `git rev-parse
    # --is-inside-work-tree` (_lib_staged_length_gate) ---

    @pytest.mark.timing
    def test_staged_diff_git_timeout_engages_cap(
        self, isolated_home, skill_repo, git_timeout_shim
    ):
        """`git diff --cached --name-only`'s _lib_capped wrap (added to
        _lib_staged_length_gate alongside the shared driver) must actually
        engage its 5s cap rather than hang, mirroring the `git show` calls'
        pre-existing _lib_capped wrap. A capped, empty file list means no
        staged SKILL.md is scanned, so the gate degrades to allow rather
        than hanging — same degrade-not-hang shape the header comment
        documents for a machine lacking timeout(1)/gtimeout(1) entirely.
        Matches on $3, not $1: _lib_staged_length_gate now threads REPO_ROOT
        through `git -C "$repo_root" diff ...`, so $1/$2 are `-C`/the repo
        path on every call in this function."""
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        env = git_timeout_shim('[ "$3" = "diff" ]')
        with assert_cap_engaged():
            decision = run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=skill_repo,
                extra_env=env,
            )
        assert decision == "allow"

    @pytest.mark.timing
    def test_repo_detection_git_timeout_engages_cap(
        self, isolated_home, skill_repo, git_timeout_shim
    ):
        """`git rev-parse --is-inside-work-tree`'s _lib_capped wrap (added to
        _lib_staged_length_gate alongside the shared driver) must actually
        engage its 5s cap rather than hang, mirroring the `git diff` cap
        coverage immediately above it. A capped, empty result isn't the
        literal string "true", so the gate degrades to allow rather than
        hanging — same degrade-not-hang shape. One instance here suffices
        for both check-skill-length.sh and check-claude-md-length.sh: the
        capped call is caller-invariant, running identically for both hooks
        before either caller's own logic. Matches on $3, not $1: same
        -C/repo-path shift as the `diff` test above -- this call also runs
        before _lib_gate_diff_base's own rev-parse call, so matching the
        first rev-parse invocation here still hits the intended call."""
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        env = git_timeout_shim('[ "$3" = "rev-parse" ]')
        with assert_cap_engaged():
            decision = run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=skill_repo,
                extra_env=env,
            )
        assert decision == "allow"

    # --- Newly-tested `git show` calls (_lib_staged_length_gate) ---

    @pytest.mark.timing
    def test_new_content_show_git_timeout_engages_cap(
        self, isolated_home, skill_repo, git_timeout_shim
    ):
        """`git show ":$f"`'s pre-existing _lib_capped wrap (the new-revision
        read feeding the line-count check) must actually engage its 5s cap
        rather than hang -- previously an admitted gap in this function's own
        header comment. A capped, empty read means new=0, which is never
        over the limit regardless of old, so the gate degrades to allow."""
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        env = git_timeout_shim(f'[ "$1" = "show" ] && [ "$2" = ":{SKILL_PATH}" ]')
        with assert_cap_engaged():
            decision = run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=skill_repo,
                extra_env=env,
            )
        assert decision == "allow"

    @pytest.mark.timing
    def test_old_content_show_git_timeout_engages_cap(
        self, isolated_home, skill_repo, git_timeout_shim
    ):
        """`git show "HEAD:$f"`'s pre-existing _lib_capped wrap (the
        old-revision read feeding the line-count check) must actually engage
        its 5s cap rather than hang -- previously an admitted gap in this
        function's own header comment. A capped, empty read means old=0; the
        staged file (201 lines) is still over the limit and still greater
        than 0, so the gate reaches the same deny it would reach without the
        timeout -- unlike the shrinking-file scenario characterized below in
        test_head_timeout_false_denies_shrinking_file, this growing-file case
        is not an instance of the HEAD-timeout false-deny defect."""
        (skill_repo / SKILL_PATH).write_text(make_skill_content(201))
        subprocess.run(["git", "add", SKILL_PATH], cwd=skill_repo, check=True)
        env = git_timeout_shim(f'[ "$1" = "show" ] && [ "$2" = "HEAD:{SKILL_PATH}" ]')
        with assert_cap_engaged():
            decision = run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=skill_repo,
                extra_env=env,
            )
        assert decision == "deny"

    @pytest.mark.timing
    def test_head_timeout_false_denies_shrinking_file(
        self, isolated_home, tmp_path, git_timeout_shim
    ):
        """KNOWN-BUG PIN, not a spec: `git show "HEAD:$f"` timing out yields
        empty stdout, so `old` computes to 0 -- and a file that is shrinking
        but still over the limit (HEAD at 300, staged at 250, limit 200)
        flips from its correct allow to a false deny reading "was 0" instead
        of "was 300". Asserts the deny-reason text itself (that it names the
        zeroed old value), not just the verdict, so this test cannot be
        satisfied by an ordinary over-limit deny for the wrong reason --
        mirrors test_sed_absent_from_path_denies's reason-text-assertion
        shape in test_check_claude_md_length.py. The actual fix (telling
        _lib_capped's exit 124 apart from a legitimately new file with no
        HEAD ancestor, via PIPESTATUS) is out of scope here and belongs in
        its own PR; this test exists so that fix shows up as a visible,
        intentional edit to this assertion rather than a silent behavior
        flip."""
        repo = make_repo_with_skill(tmp_path, 300)
        (repo / SKILL_PATH).write_text(make_skill_content(250))
        subprocess.run(["git", "add", SKILL_PATH], cwd=repo, check=True)
        env = git_timeout_shim(f'[ "$1" = "show" ] && [ "$2" = "HEAD:{SKILL_PATH}" ]')
        with assert_cap_engaged():
            reason = run_hook_reason(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
                extra_env=env,
            )
        assert reason is not None
        assert "was 0" in reason


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


def _build_clean_merge_growing_skill(tmp_path: Path) -> Path:
    """A conflict-free merge where only upstream grows SKILL.md past the
    limit: the clone's own commit touches an unrelated file, so the merge
    cannot fast-forward and leaves MERGE_HEAD, but SKILL.md itself was never
    independently edited on the clone's side."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    skill_dir = clone / "claude-skills" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (clone / SKILL_PATH).write_text(make_skill_content(190))
    subprocess.run(["git", "add", SKILL_PATH], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "add skill at 190"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)

    (clone / "own.txt").write_text("own\n")
    subprocess.run(["git", "add", "own.txt"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "own edit"], cwd=clone, check=True)

    push_clone = tmp_path / "push_clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(push_clone)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=push_clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=push_clone, check=True)
    (push_clone / SKILL_PATH).write_text(make_skill_content(250))
    subprocess.run(["git", "add", SKILL_PATH], cwd=push_clone, check=True)
    subprocess.run(["git", "commit", "-qm", "origin grows skill"], cwd=push_clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=push_clone, check=True)

    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    result = subprocess.run(
        ["git", "merge", "--no-commit", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / ".git" / "MERGE_HEAD").exists()
    return clone


def _build_conflicted_rebase_with_growing_skill(tmp_path: Path) -> Path:
    """A conflicted rebase (on an unrelated file) with SKILL.md separately
    grown past the limit as part of the staged resolution -- REBASE_HEAD
    reaches neither anchor in this ordinary case, so the base stays empty
    and `old` falls back to mid-rebase HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    skill_dir = repo / "claude-skills" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (repo / SKILL_PATH).write_text(make_skill_content(100))
    subprocess.run(["git", "add", SKILL_PATH], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed skill at 100"], cwd=repo, check=True)

    build_conflicted_rebase(repo)
    resolve_conflicted_rebase(repo)
    (repo / SKILL_PATH).write_text(make_skill_content(250))
    subprocess.run(["git", "add", SKILL_PATH], cwd=repo, check=True)
    return repo


def _build_conflicted_revert_with_growing_skill(tmp_path: Path) -> Path:
    """A conflicted revert of an unrelated file, trusted via the HEAD anchor
    by construction (REVERT_HEAD is a real ancestor commit), with SKILL.md
    separately grown past the limit as part of the staged resolution --
    state=revert is one of the three states whose merge-tree call passes
    --merge-base=, unlike the merge-state fixture the write-tree-rejection
    fallback test above reuses."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    skill_dir = repo / "claude-skills" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (repo / SKILL_PATH).write_text(make_skill_content(100))
    subprocess.run(["git", "add", SKILL_PATH], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed skill at 100"], cwd=repo, check=True)

    build_conflicted_revert(repo)
    (repo / "f").write_text("resolved\n")
    subprocess.run(["git", "add", "f"], cwd=repo, check=True)
    (repo / SKILL_PATH).write_text(make_skill_content(250))
    subprocess.run(["git", "add", SKILL_PATH], cwd=repo, check=True)
    return repo


def _build_clean_merge_growing_skill_with_second_skill_file(tmp_path: Path) -> Path:
    """Same fixture as _build_clean_merge_growing_skill, with a second,
    independently-staged skill file added on top -- two staged paths
    matching the length gate's pattern, so its per-file loop over staged
    paths iterates twice against a single resolved base."""
    clone = _build_clean_merge_growing_skill(tmp_path)
    second_skill_dir = clone / "claude-skills" / "skills" / "my-second-skill"
    second_skill_dir.mkdir(parents=True)
    (second_skill_dir / "SKILL.md").write_text(make_skill_content(50))
    subprocess.run(
        ["git", "add", "claude-skills/skills/my-second-skill/SKILL.md"], cwd=clone, check=True
    )
    return clone


def _build_delete_modify_conflict_growing_skill(tmp_path: Path) -> Path:
    """SKILL.md-specific instance of
    test_require_code_review.py::_build_delete_modify_conflict_via_origin's
    fixture shape: the clone deletes SKILL.md, origin independently grows
    it to 250 lines (past the 200-line limit), and the merge surfaces a
    delete/modify conflict -- distinct from a two-sided content conflict,
    since one side has no blob at all to three-way-merge against. Resolved
    by staging content at 220 lines: over the limit either way `old` is
    read, but strictly between the merge-tree base's `old` (250, origin's
    surviving content) and a regressed literal-HEAD fallback's `old` (0,
    since the file doesn't exist at literal HEAD in the clone's own
    delete commit). That places the two candidate bases on opposite sides
    of _lib_staged_length_gate's `new > old` deny condition, so the
    resulting allow/deny outcome discriminates which base the gate
    actually used -- see
    test_delete_modify_conflict_growing_skill_allows's own docstring for
    the two resulting outcomes."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    skill_dir = clone / "claude-skills" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (clone / SKILL_PATH).write_text(make_skill_content(100))
    subprocess.run(["git", "add", SKILL_PATH], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "add skill at 100"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)

    subprocess.run(["git", "rm", "-q", SKILL_PATH], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "clone deletes skill"], cwd=clone, check=True)

    push_conflicting_edit_to_origin(tmp_path, bare, SKILL_PATH, make_skill_content(250))
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    result = subprocess.run(
        ["git", "merge", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (clone / ".git" / "MERGE_HEAD").exists()
    (clone / SKILL_PATH).write_text(make_skill_content(220))
    subprocess.run(["git", "add", SKILL_PATH], cwd=clone, check=True)
    return clone


def _build_clean_merge_mode_bit_only_skill(tmp_path: Path) -> Path:
    """SKILL.md-specific instance of
    test_require_code_review.py::_build_clean_merge_mode_bit_only_change's
    fixture shape: upstream's only contribution to SKILL.md is a mode-bit
    flip (chmod +x, no content change), auto-resolved with no conflict
    since the clone's own side never touched the file -- so `new` and
    `old` read back byte-identical through _lib_staged_length_gate's git
    show pair."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    skill_dir = clone / "claude-skills" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (clone / SKILL_PATH).write_text(make_skill_content(100))
    subprocess.run(["git", "add", SKILL_PATH], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "add skill at 100"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)

    (clone / "own.txt").write_text("own\n")
    subprocess.run(["git", "add", "own.txt"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "own edit"], cwd=clone, check=True)

    push_clone = tmp_path / "push_clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(push_clone)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=push_clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=push_clone, check=True)
    os.chmod(push_clone / SKILL_PATH, 0o755)
    subprocess.run(["git", "add", SKILL_PATH], cwd=push_clone, check=True)
    subprocess.run(["git", "commit", "-qm", "origin marks skill executable"], cwd=push_clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=push_clone, check=True)

    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    result = subprocess.run(
        ["git", "merge", "--no-commit", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / ".git" / "MERGE_HEAD").exists()
    return clone


class TestCheckSkillLengthMergeAwareBase:
    """_lib_staged_length_gate's `old` comparison is measured against
    _lib_gate_diff_base's resolved base, and mid-merge vs. mid-rebase differ
    in which base gets used."""

    def test_mid_merge_pure_upstream_growth_does_not_fail(self, isolated_home, tmp_path):
        """Mid-merge, a file that only upstream grew past the limit does not
        fail the gate: `old` is the resolved base, not the pre-merge feature
        tip, which never saw the growth at all."""
        repo = _build_clean_merge_growing_skill(tmp_path)
        assert (
            run_hook(CHECK_SKILL_LENGTH_HOOK, bash_input("git commit -m foo"), cwd=repo)
            == "allow"
        )

    def test_mid_rebase_growth_past_mid_rebase_head_still_denies(
        self, isolated_home, tmp_path
    ):
        """Mid-rebase, with the base empty, the gate's `new > old` comparison
        uses mid-rebase HEAD as `old`."""
        repo = _build_conflicted_rebase_with_growing_skill(tmp_path)
        assert (
            run_hook(CHECK_SKILL_LENGTH_HOOK, bash_input("git commit -m foo"), cwd=repo)
            == "deny"
        )

    def test_fallback_write_tree_rejected_behaves_like_today(self, isolated_home, tmp_path):
        """`--write-tree` outright rejection (git < 2.38) must fall back to
        exactly today's plain HEAD-relative recipe for `old` -- literal HEAD,
        not the resolved base. For this fixture (only upstream grew the
        file past the limit), literal pre-merge HEAD never saw that growth,
        so the fallback denies -- the same conservative posture this gate
        had before the base substitution existed, not the fixed behavior
        _lib_gate_diff_base's degraded (git-version-fallback) path cannot
        provide."""
        repo = _build_clean_merge_growing_skill(tmp_path)
        bin_dir = tmp_path / "bin-fallback-write-tree"
        _make_git_rejecting_write_tree(bin_dir)
        extra_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_GIT": shutil.which("git"),
        }
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
                extra_env=extra_env,
            )
            == "deny"
        )

    def test_fallback_merge_base_flag_rejected_behaves_like_today(self, isolated_home, tmp_path):
        """The `--merge-base=` rejection band (git 2.38-2.39) only affects
        the three states whose merge-tree call passes that flag -- rebase,
        cherry-pick, revert, not merge -- so this needs its own fixture,
        unlike the write-tree fallback test above which reuses the merge
        fixture because that rejection band affects every state uniformly.
        SKILL.md is untouched by the revert itself, so `old` falls back to
        mid-revert HEAD either way -- the same "no rebase-specific
        over-count" _lib_staged_length_gate's own docstring documents for
        rebase. This fixture's job is proving the rejection is absorbed
        cleanly into the fallback, not that the verdict differs from a
        working substitution."""
        repo = _build_conflicted_revert_with_growing_skill(tmp_path)
        bin_dir = tmp_path / "bin-fallback-merge-base"
        _make_git_rejecting_merge_base_flag(bin_dir)
        extra_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_GIT": shutil.which("git"),
        }
        assert (
            run_hook(
                CHECK_SKILL_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
                extra_env=extra_env,
            )
            == "deny"
        )

    def test_delete_modify_conflict_growing_skill_allows(self, isolated_home, tmp_path):
        """A delete/modify conflict on SKILL.md itself, resolved at 220
        lines -- over the 200-line limit either way `old` is read, but
        strictly between the merge-tree base's `old` (250) and a
        regressed literal-HEAD fallback's `old` (0, the file doesn't
        exist at literal HEAD in the clone's own delete commit). At the
        correct merge-tree base, 220 > 250 is false, so the gate allows;
        this discriminates the correct base from a regressed
        literal-HEAD fallback, where 220 > 0 is true and the gate would
        instead deny -- the fixture shape
        .claude/plans/merge-aware-review-gates.md's Verification section
        names as untested against this gate's own git show pair."""
        repo = _build_delete_modify_conflict_growing_skill(tmp_path)
        assert (
            run_hook(CHECK_SKILL_LENGTH_HOOK, bash_input("git commit -m foo"), cwd=repo)
            == "allow"
        )

    def test_mode_bit_only_upstream_change_to_skill_does_not_deny(self, isolated_home, tmp_path):
        """Upstream's only contribution to SKILL.md is a mode-bit flip --
        the merge auto-resolves with no conflict, and since the content is
        byte-identical, `new` never exceeds `old`."""
        repo = _build_clean_merge_mode_bit_only_skill(tmp_path)
        assert (
            run_hook(CHECK_SKILL_LENGTH_HOOK, bash_input("git commit -m foo"), cwd=repo)
            == "allow"
        )

    def test_diff_base_resolved_once_across_multiple_staged_skill_files(
        self, isolated_home, tmp_path
    ):
        """_lib_staged_length_gate resolves _lib_gate_diff_base once above
        its per-file loop, not once per staged file -- a per-file
        resolution would spawn state detection and a full merge-tree
        --write-tree once per staged SKILL.md instead of once per hook run
        (see _lib_staged_length_gate's own docstring in _lib.sh)."""
        repo = _build_clean_merge_growing_skill_with_second_skill_file(tmp_path)
        real_git = shutil.which("git")
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        invocation_log = tmp_path / "git-merge-tree-invocations"
        stub.write_text(
            '#!/bin/bash\n'
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "merge-tree" ]; then\n'
            f'    echo "$@" >> "{invocation_log}"\n'
            '  fi\n'
            'done\n'
            f'exec {real_git} "$@"\n'
        )
        stub.chmod(0o755)
        extra_env = {"PATH": f"{stub_dir}:{os.environ['PATH']}"}

        run_hook(CHECK_SKILL_LENGTH_HOOK, bash_input("git commit -m foo"), cwd=repo, extra_env=extra_env)

        invocations = invocation_log.read_text().splitlines() if invocation_log.exists() else []
        assert len(invocations) == 1, (
            f"expected exactly one merge-tree invocation across both staged "
            f"SKILL.md files, got: {invocations}"
        )
