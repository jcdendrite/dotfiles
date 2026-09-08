"""Tests for check-claude-md-length.sh."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from helpers import (
    HOOKS_DIR,
    bare_remote_with_default_branch,
    bash_input,
    build_conflicted_rebase,
    build_path_without,
    edit_input,
    resolve_conflicted_rebase,
    run_hook,
    run_hook_reason,
)

from .conftest import assert_cap_engaged

CHECK_CLAUDE_MD_LENGTH_HOOK = HOOKS_DIR / "check-claude-md-length.sh"
CLAUDE_MD_PATH = "claude/.claude/CLAUDE.md"

# Mirrors GLOBAL_CLAUDE_MD_BYTE_LIMIT in check-claude-md-length.sh.
# test_byte_limit_constant_matches_hook_source below cross-checks the two
# stay in sync.
BYTE_LIMIT = 25600

_GLOBAL_CLAUDE_MD_BYTE_LIMIT_RE = re.compile(r"^GLOBAL_CLAUDE_MD_BYTE_LIMIT=(\d+)", re.MULTILINE)

SETTINGS_PATH = Path(__file__).resolve().parents[4] / "claude/.claude/settings.json"


def make_lines(n: int, prefix: str = "line") -> str:
    """Return content with exactly n newline-terminated lines."""
    return "\n".join(f"{prefix} {i + 1}" for i in range(n)) + "\n"


def make_bytes(n: int, filler: str = "a") -> str:
    """Return content that is exactly n bytes: one line of (n - 1) filler
    characters plus a trailing newline. A single line keeps the line-count
    dimension out of play so byte-cap tests isolate the byte dimension."""
    return filler * (n - 1) + "\n"


def make_multibyte_bytes(n: int, filler: str = "é") -> str:
    """Return content that is exactly n bytes (UTF-8 encoded), padded with a
    multi-byte filler character plus a trailing newline. Isolates byte count
    from character/codepoint count: `filler` must encode to more than one
    byte, so a test built on this can distinguish `wc -c` semantics from a
    codepoint count, which every `make_bytes` (single-byte ASCII filler)
    test cannot. `n - 1` must be evenly divisible by the filler's UTF-8
    byte length so the padding lands on an exact character boundary."""
    filler_byte_length = len(filler.encode("utf-8"))
    if filler_byte_length < 2:
        raise ValueError(f"filler {filler!r} must be multi-byte in UTF-8")
    if (n - 1) % filler_byte_length != 0:
        raise ValueError(
            f"n - 1 ({n - 1}) must be divisible by filler byte length {filler_byte_length}"
        )
    return filler * ((n - 1) // filler_byte_length) + "\n"


def make_lines_over_byte_limit(n: int, min_bytes: int, filler: str = "a") -> str:
    """Return content with exactly n lines whose total byte count is at
    least min_bytes, padding the last line with filler characters. Lets a
    test grow both the line-count and byte-count dimensions at once from a
    single piece of content."""
    lines = [f"line {i + 1}" for i in range(n)]
    content = "\n".join(lines) + "\n"
    deficit = min_bytes - len(content.encode("utf-8"))
    if deficit > 0:
        lines[-1] += filler * deficit
        content = "\n".join(lines) + "\n"
    return content


def make_repo_with_byte_file(tmp_path: Path, target_path: str, head_bytes: int) -> Path:
    """Git repo with `target_path` committed at exactly `head_bytes` bytes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    target = repo / target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(make_bytes(head_bytes))
    subprocess.run(["git", "add", target_path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def stub_bin_without_timeout(tmp_path: Path) -> Path:
    """Stub PATH with only the binaries this hook's code path invokes
    (`cat`/`jq` via _lib.sh's JSON parsing, `dirname` to locate _lib.sh,
    `sed`/`tr` for _lib_command_invokes_git_subcmd's git-commit match
    (GH-783), `grep` for the path-filter match, `awk` for the line
    count, `git` for the _lib_capped-wrapped show and cat-file -s
    calls), omitting both timeout(1) and gtimeout(1). Mirrors
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


def make_repo_with_file(tmp_path: Path, target_path: str, head_lines: int) -> Path:
    """Git repo with `target_path` committed at `head_lines` lines."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    target = repo / target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(make_lines(head_lines))
    subprocess.run(["git", "add", target_path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


class TestCheckClaudeMdLength:
    # --- Logic matrix (CLAUDE_MD_PATH fixture) ---

    def test_non_commit_command_allows(self, isolated_home, tmp_path):
        """Non-git-commit Bash command is allowed regardless of staged content."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(CHECK_CLAUDE_MD_LENGTH_HOOK, bash_input("git status"), cwd=repo)
            == "allow"
        )

    def test_non_bash_tool_allows(self, isolated_home, tmp_path):
        """Non-Bash tool inputs are passed through unconditionally."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        assert (
            run_hook(CHECK_CLAUDE_MD_LENGTH_HOOK, edit_input("/tmp/foo.txt"), cwd=repo)
            == "allow"
        )

    def test_outside_git_repo_allows(self, isolated_home, tmp_path):
        """Hook exits 0 silently when CWD is not inside a git repo."""
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=non_repo,
            )
            == "allow"
        )

    def test_no_staged_matching_file_allows(self, isolated_home, tmp_path):
        """A staged non-CLAUDE.md/AGENTS.md file does not trigger the gate."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / "other.txt").write_text("something\n")
        subprocess.run(["git", "add", "other.txt"], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_quoted_form_reaches_same_verdict_as_bare_form(self, isolated_home, tmp_path):
        """A quote-adjacent split (`"git" commit -m x`) must reach the same
        deny verdict as the unquoted form."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input('"git" commit -m foo'),
                cwd=repo,
            )
            == "deny"
        )

    def test_sed_absent_from_path_denies(self, isolated_home, tmp_path):
        """Status-2 propagation: the matcher could not determine whether
        this command invokes git commit, and this gate's own documented
        fail-closed posture means an undetermined match denies rather than
        silently falling through to allow. Asserts the distinguishing
        reason text, not just the verdict, so this test cannot be
        satisfied by an ordinary over-limit deny reaching "deny" for the
        wrong reason."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        farm_dir = tmp_path / "path-without-sed"
        farm_dir.mkdir()
        restricted_path = build_path_without("sed", farm_dir)
        reason = run_hook_reason(
            CHECK_CLAUDE_MD_LENGTH_HOOK,
            bash_input("git commit -m foo"),
            cwd=repo,
            extra_env={"PATH": restricted_path},
        )
        assert reason is not None
        assert "could not determine" in reason

    def test_new_claude_md_over_limit_denies(self, isolated_home, tmp_path):
        """New file with no HEAD version staged at 201 lines — old defaults to 0 → deny."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        (repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        target = repo / CLAUDE_MD_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_staged_deletion_of_claude_md_allows(self, isolated_home, tmp_path):
        """git rm-staged CLAUDE.md: git show ":$f" produces empty output → new=0, 0 > 200 is false → allow."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        subprocess.run(["git", "rm", "-q", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_deny_message_includes_filename_and_counts(self, isolated_home, tmp_path):
        """Deny reason must name the file, new line count, old line count, and
        limit — and must NOT carry the byte-violation fragment, since this
        commit only crosses the line limit."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        reason = run_hook_reason(
            CHECK_CLAUDE_MD_LENGTH_HOOK,
            bash_input("git commit -m foo"),
            cwd=repo,
        )
        assert reason is not None
        assert CLAUDE_MD_PATH in reason
        assert "201" in reason
        assert "190" in reason
        assert "200" in reason
        assert "bytes (was" not in reason

    # --- Byte-cap logic matrix (mirrors the line-cap matrix above) ---

    def test_new_claude_md_over_byte_limit_denies(self, isolated_home, tmp_path):
        """New file with no HEAD version staged over BYTE_LIMIT — there is no
        `HEAD:$f` for `git cat-file -s` to read, so the file is new to this
        commit and old_bytes is 0 → deny. Mirrors
        test_new_claude_md_over_limit_denies for the byte dimension."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        (repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        target = repo / CLAUDE_MD_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(make_bytes(BYTE_LIMIT + 1))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_byte_cap_deny_message_includes_both_byte_counts(
        self, isolated_home, tmp_path
    ):
        """Deny reason must name both the new and old byte counts — and must
        NOT carry the line-violation fragment, since this commit only
        crosses the byte limit."""
        repo = make_repo_with_byte_file(tmp_path, CLAUDE_MD_PATH, BYTE_LIMIT + 1)
        new_bytes = BYTE_LIMIT + 10
        (repo / CLAUDE_MD_PATH).write_text(make_bytes(new_bytes))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        reason = run_hook_reason(
            CHECK_CLAUDE_MD_LENGTH_HOOK,
            bash_input("git commit -m foo"),
            cwd=repo,
        )
        assert reason is not None
        assert str(new_bytes) in reason
        assert str(BYTE_LIMIT + 1) in reason
        assert str(BYTE_LIMIT) in reason
        assert "lines (was" not in reason

    def test_byte_cap_multibyte_utf8_content_denies_at_byte_threshold(
        self, isolated_home, tmp_path
    ):
        """Staged content's UTF-8 byte count crosses BYTE_LIMIT while its
        character/codepoint count stays well under it — isolates `wc -c`
        byte semantics from a codepoint count, which no `make_bytes`
        (single-byte ASCII filler) test can distinguish."""
        repo = make_repo_with_byte_file(tmp_path, CLAUDE_MD_PATH, BYTE_LIMIT - 100)
        multibyte_content = make_multibyte_bytes(BYTE_LIMIT + 1, filler="é")
        assert len(multibyte_content.encode("utf-8")) == BYTE_LIMIT + 1
        assert len(multibyte_content) < BYTE_LIMIT
        (repo / CLAUDE_MD_PATH).write_text(multibyte_content, encoding="utf-8")
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_combined_line_and_byte_violation_denies_with_both_reasons(
        self, isolated_home, tmp_path
    ):
        """HEAD under both limits, staged crosses both simultaneously: the
        deny reason must include both the line-violation and byte-violation
        message fragments, not just one (mutation regression: overwriting
        instead of appending the byte-violation message would silently drop
        the line-violation message)."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        content = make_lines_over_byte_limit(250, BYTE_LIMIT + 100)
        (repo / CLAUDE_MD_PATH).write_text(content)
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        reason = run_hook_reason(
            CHECK_CLAUDE_MD_LENGTH_HOOK,
            bash_input("git commit -m foo"),
            cwd=repo,
        )
        assert reason is not None
        assert "lines (was" in reason
        assert "limit 200)" in reason
        assert "bytes (was" in reason
        assert f"limit {BYTE_LIMIT})" in reason

    def test_cwd_not_repo_root_does_not_cause_false_negative(
        self, isolated_home, tmp_path
    ):
        """Hook run from a repo subdirectory must still catch over-limit CLAUDE.md.

        Regression test: `git diff --cached --name-only` emits repo-root-relative
        paths. An earlier version had `[ -f "$f" ] || continue` which resolved
        those paths against CWD — if CWD was a subdirectory the check failed
        and the file was silently skipped (false negative, bloated file slips
        through). The guard was removed; `git show ":$f"` reads from the index
        directly and doesn't depend on CWD.
        """
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        subdir = repo / "claude"
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=subdir,
            )
            == "deny"
        )

    # --- Path-shape positive cases (must → deny) ---

    def test_root_claude_md_denies(self, isolated_home, tmp_path):
        """CLAUDE.md at the repo root matches the filter → deny."""
        repo = make_repo_with_file(tmp_path, "CLAUDE.md", 190)
        (repo / "CLAUDE.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "CLAUDE.md"], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_root_agents_md_denies(self, isolated_home, tmp_path):
        """AGENTS.md at the repo root matches the filter → deny."""
        repo = make_repo_with_file(tmp_path, "AGENTS.md", 190)
        (repo / "AGENTS.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_dot_claude_claude_md_denies(self, isolated_home, tmp_path):
        """.claude/CLAUDE.md matches the filter → deny."""
        repo = make_repo_with_file(tmp_path, ".claude/CLAUDE.md", 190)
        (repo / ".claude" / "CLAUDE.md").write_text(make_lines(201))
        subprocess.run(["git", "add", ".claude/CLAUDE.md"], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_stowed_source_path_denies(self, isolated_home, tmp_path):
        """claude/.claude/CLAUDE.md (stowed-source path) matches the filter → deny."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_stowed_source_agents_md_denies(self, isolated_home, tmp_path):
        """claude/.claude/AGENTS.md (stowed-source path) matches the filter → deny."""
        repo = make_repo_with_file(tmp_path, "claude/.claude/AGENTS.md", 190)
        (repo / "claude" / ".claude" / "AGENTS.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "claude/.claude/AGENTS.md"], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_dot_claude_agents_md_denies(self, isolated_home, tmp_path):
        """.claude/AGENTS.md matches the filter → deny."""
        repo = make_repo_with_file(tmp_path, ".claude/AGENTS.md", 190)
        (repo / ".claude" / "AGENTS.md").write_text(make_lines(201))
        subprocess.run(["git", "add", ".claude/AGENTS.md"], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    # --- AGENTS.md transition triad ---

    def test_agents_md_at_exactly_200_allows(self, isolated_home, tmp_path):
        """AGENTS.md at 200 lines is at the limit — the gate is `> 200`, so 200 passes."""
        repo = make_repo_with_file(tmp_path, "AGENTS.md", 190)
        (repo / "AGENTS.md").write_text(make_lines(200))
        subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_agents_md_growing_to_201_denies(self, isolated_home, tmp_path):
        """AGENTS.md HEAD at 190, staged at 201: new > 200 and new > old → deny."""
        repo = make_repo_with_file(tmp_path, "AGENTS.md", 190)
        (repo / "AGENTS.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_new_agents_md_over_limit_denies(self, isolated_home, tmp_path):
        """New AGENTS.md staged at 201 lines — old defaults to 0 → deny."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        (repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / "AGENTS.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    def test_first_commit_claude_md_over_limit_denies(self, isolated_home, tmp_path):
        """First-ever commit to a repo (no HEAD): CLAUDE.md staged at 201 lines → deny.

        Regression test: git show "HEAD:$f" fails when no commits exist; awk
        'END{print NR}' on empty output returns 0 for old, so deny fires correctly
        when new > 200 regardless of HEAD state.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        target = repo / CLAUDE_MD_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m init"),
                cwd=repo,
            )
            == "deny"
        )

    # --- Negative-path cases (must → allow because regex does not match) ---

    def test_claude_md_bak_allows(self, isolated_home, tmp_path):
        """CLAUDE.md.bak does not match the filter → allow."""
        repo = make_repo_with_file(tmp_path, "CLAUDE.md.bak", 190)
        (repo / "CLAUDE.md.bak").write_text(make_lines(201))
        subprocess.run(["git", "add", "CLAUDE.md.bak"], cwd=repo, check=True)
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=repo).decode()
        assert "CLAUDE.md.bak" in staged, "file was not actually staged"
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_not_claude_md_allows(self, isolated_home, tmp_path):
        """not-CLAUDE.md does not match the filter → allow."""
        repo = make_repo_with_file(tmp_path, "not-CLAUDE.md", 190)
        (repo / "not-CLAUDE.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "not-CLAUDE.md"], cwd=repo, check=True)
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=repo).decode()
        assert "not-CLAUDE.md" in staged, "file was not actually staged"
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_foo_slash_claude_md_allows(self, isolated_home, tmp_path):
        """foo/CLAUDE.md (CLAUDE.md outside root and outside .claude/) does not match → allow."""
        repo = make_repo_with_file(tmp_path, "foo/CLAUDE.md", 190)
        (repo / "foo" / "CLAUDE.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "foo/CLAUDE.md"], cwd=repo, check=True)
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=repo).decode()
        assert "foo/CLAUDE.md" in staged, "file was not actually staged"
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_docs_agents_claude_md_allows(self, isolated_home, tmp_path):
        """docs/agents/CLAUDE.md does not match the filter → allow."""
        repo = make_repo_with_file(tmp_path, "docs/agents/CLAUDE.md", 190)
        (repo / "docs" / "agents" / "CLAUDE.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "docs/agents/CLAUDE.md"], cwd=repo, check=True)
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=repo).decode()
        assert "docs/agents/CLAUDE.md" in staged, "file was not actually staged"
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_subfolder_agents_md_allows(self, isolated_home, tmp_path):
        """subfolder/AGENTS.md does not match the filter → allow."""
        repo = make_repo_with_file(tmp_path, "subfolder/AGENTS.md", 190)
        (repo / "subfolder" / "AGENTS.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "subfolder/AGENTS.md"], cwd=repo, check=True)
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=repo).decode()
        assert "subfolder/AGENTS.md" in staged, "file was not actually staged"
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    def test_lowercase_claude_md_allows(self, isolated_home, tmp_path):
        """claude.md (lowercase) does not match the case-sensitive filter → allow."""
        repo = make_repo_with_file(tmp_path, "claude.md", 190)
        (repo / "claude.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "claude.md"], cwd=repo, check=True)
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=repo).decode()
        assert "claude.md" in staged, "file was not actually staged"
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
            )
            == "allow"
        )

    # --- Multi-file staged commit ---

    def test_multi_file_both_over_limit_denies_and_names_both(
        self, isolated_home, tmp_path
    ):
        """Both CLAUDE.md and AGENTS.md staged over limit → deny; both filenames in message."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        (repo / "CLAUDE.md").write_text(make_lines(190))
        (repo / "AGENTS.md").write_text(make_lines(190))
        subprocess.run(["git", "add", "CLAUDE.md", "AGENTS.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / "CLAUDE.md").write_text(make_lines(201))
        (repo / "AGENTS.md").write_text(make_lines(201))
        subprocess.run(["git", "add", "CLAUDE.md", "AGENTS.md"], cwd=repo, check=True)
        result = subprocess.run(
            [str(CHECK_CLAUDE_MD_LENGTH_HOOK)],
            input=json.dumps(bash_input("git commit -m foo")),
            capture_output=True,
            text=True,
            cwd=repo,
            check=False,
        )
        payload = json.loads(result.stdout)
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = payload["hookSpecificOutput"].get("permissionDecisionReason", "")
        assert "CLAUDE.md" in reason
        assert "AGENTS.md" in reason
        assert "201" in reason

    def test_commit_amend_denies(self, isolated_home, tmp_path):
        """git commit --amend is still a commit command and must be caught."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit --amend --no-edit"),
                cwd=repo,
            )
            == "deny"
        )

    def test_chained_git_add_commit_denies(self, isolated_home, tmp_path):
        """Chained `git add ... && git commit` is caught by the internal
        _lib_command_invokes_git_subcmd check.

        The `if: "Bash(git commit *)"` predicate in settings.json matches
        chained and prefixed commands (a `true && git commit ...` with a
        real unreviewed staged diff got a genuine deny from
        require-code-review.sh). This test invokes the hook binary directly
        regardless, since the internal check is the authoritative gate
        either way — consistent with the hook header's note that the `if`
        field is a hint only.
        """
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git add . && git commit -m foo"),
                cwd=repo,
            )
            == "deny"
        )

    # --- Fail-open regression: neither timeout(1) nor gtimeout(1) present ---

    def test_growing_over_limit_denies_when_neither_timeout_nor_gtimeout_present(
        self, isolated_home, tmp_path
    ):
        """Fail-open regression: with neither binary present, _lib_capped
        runs the git show calls uncapped (see _lib.sh) rather than silently
        skipping — the gate must still catch a growing over-limit file."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(201))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        stub_bin = stub_bin_without_timeout(tmp_path)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
                extra_env={"PATH": str(stub_bin)},
            )
            == "deny"
        )

    def test_at_limit_allows_when_neither_timeout_nor_gtimeout_present(
        self, isolated_home, tmp_path
    ):
        """Companion allow case for the deny above: under the same PATH, a
        file at the limit (not growing past it) must still pass — without
        this, a fallback branch that always returns nonzero would
        masquerade as a working gate."""
        repo = make_repo_with_file(tmp_path, CLAUDE_MD_PATH, 190)
        (repo / CLAUDE_MD_PATH).write_text(make_lines(200))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        stub_bin = stub_bin_without_timeout(tmp_path)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
                extra_env={"PATH": str(stub_bin)},
            )
            == "allow"
        )

    def test_byte_cap_growing_over_limit_denies_when_neither_timeout_nor_gtimeout_present(
        self, isolated_home, tmp_path
    ):
        """Byte-dimension analog of
        test_growing_over_limit_denies_when_neither_timeout_nor_gtimeout_present:
        stub_bin_without_timeout's PATH has no wc, exercising the byte
        dimension's git-cat-file-s derivation without wc on PATH. File
        stays a single line (well under the 200-line limit) so only the
        byte dimension is in play."""
        repo = make_repo_with_byte_file(tmp_path, CLAUDE_MD_PATH, BYTE_LIMIT - 100)
        (repo / CLAUDE_MD_PATH).write_text(make_bytes(BYTE_LIMIT + 1))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        stub_bin = stub_bin_without_timeout(tmp_path)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
                extra_env={"PATH": str(stub_bin)},
            )
            == "deny"
        )

    def test_byte_cap_at_limit_allows_when_neither_timeout_nor_gtimeout_present(
        self, isolated_home, tmp_path
    ):
        """Companion allow case for the deny above: under the same PATH, a
        file at the byte limit (not growing past it) must still pass."""
        repo = make_repo_with_byte_file(tmp_path, CLAUDE_MD_PATH, BYTE_LIMIT - 100)
        (repo / CLAUDE_MD_PATH).write_text(make_bytes(BYTE_LIMIT))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        stub_bin = stub_bin_without_timeout(tmp_path)
        assert (
            run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
                extra_env={"PATH": str(stub_bin)},
            )
            == "allow"
        )

    # --- Newly-capped `git cat-file -s` calls (byte dimension of
    # _lib_staged_length_gate) ---

    @pytest.mark.timing
    def test_byte_cap_cat_file_git_timeout_engages_cap(
        self, isolated_home, tmp_path, git_timeout_shim
    ):
        """Both `git cat-file -s ":$f"` and `git cat-file -s "HEAD:$f"` (the
        byte-count dimension's _lib_capped wrap) must actually engage their
        5s cap rather than hang, mirroring check-skill-length.py's coverage
        of the shared git show/diff/rev-parse call sites. One shim predicate
        (matching on the `cat-file` subcommand) covers both the new- and
        old-revision calls, since they share it. A capped, empty byte count
        defaults new_bytes/old_bytes to 0 (see _lib_staged_length_gate), so
        the gate degrades to allow rather than hanging."""
        repo = make_repo_with_byte_file(tmp_path, CLAUDE_MD_PATH, BYTE_LIMIT - 100)
        (repo / CLAUDE_MD_PATH).write_text(make_bytes(BYTE_LIMIT + 1))
        subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
        env = git_timeout_shim('[ "$1" = "cat-file" ]')
        with assert_cap_engaged():
            decision = run_hook(
                CHECK_CLAUDE_MD_LENGTH_HOOK,
                bash_input("git commit -m foo"),
                cwd=repo,
                extra_env=env,
            )
        assert decision == "allow"

    # --- Byte-limit constant cross-check ---

    def test_byte_limit_constant_matches_hook_source(self):
        """This file's BYTE_LIMIT must track GLOBAL_CLAUDE_MD_BYTE_LIMIT in
        check-claude-md-length.sh — that constant's own header comment
        documents a dated log of prior values appended on every future
        change, so drift between the two is an anticipated future event.
        The boundary-value tests above already fail on any such drift
        indirectly, via a deny-vs-allow mismatch a maintainer has to trace
        back to the constant. This test adds a direct, single-assertion
        diagnostic for the same drift instead. Mirrors
        test_transcript_analysis.py's
        test_bootstrap_fallback_hooks_matches_every_hook_declaring_deny_gate_label,
        which extracts a bash constant out of hook source the same way."""
        match = _GLOBAL_CLAUDE_MD_BYTE_LIMIT_RE.search(CHECK_CLAUDE_MD_LENGTH_HOOK.read_text())
        assert match is not None, (
            "GLOBAL_CLAUDE_MD_BYTE_LIMIT not found in check-claude-md-length.sh"
        )
        assert int(match.group(1)) == BYTE_LIMIT

    # --- Settings.json wiring ---

    def test_settings_json_contains_hook_entry(self):
        """settings.json must wire check-claude-md-length.sh to a Bash PreToolUse group.

        PreToolUse is a list of {matcher, hooks} objects. The hook must be in a
        group whose matcher includes "Bash" — a PostToolUse entry or a non-Bash
        matcher would register the command string but disable the gate.
        """
        settings = json.loads(SETTINGS_PATH.read_text())
        matcher_groups = settings.get("hooks", {}).get("PreToolUse", [])
        matches = [
            (group.get("matcher", ""), entry)
            for group in matcher_groups
            if isinstance(group, dict)
            for entry in group.get("hooks", [])
            if isinstance(entry, dict)
            and entry.get("command") == "~/.claude/hooks/check-claude-md-length.sh"
        ]
        assert matches, "No hook entry found for check-claude-md-length.sh in PreToolUse"
        matcher, _ = matches[0]
        assert "Bash" in matcher, (
            f"check-claude-md-length.sh must be in a Bash matcher group; found: {matcher!r}"
        )


def _build_clean_merge_growing_claude_md(tmp_path: Path) -> Path:
    """Local copy of test_check_skill_length.py's fixture of the same shape
    (DAMP test code): a conflict-free merge where only upstream grows
    CLAUDE.md past the limit, the clone's own commit touching an unrelated
    file so the merge cannot fast-forward and leaves MERGE_HEAD."""
    bare, clone = bare_remote_with_default_branch(tmp_path)
    (clone / CLAUDE_MD_PATH).parent.mkdir(parents=True, exist_ok=True)
    (clone / CLAUDE_MD_PATH).write_text(make_lines(190))
    subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "add claude.md at 190"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)

    (clone / "own.txt").write_text("own\n")
    subprocess.run(["git", "add", "own.txt"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "own edit"], cwd=clone, check=True)

    push_clone = tmp_path / "push_clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(push_clone)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=push_clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=push_clone, check=True)
    (push_clone / CLAUDE_MD_PATH).parent.mkdir(parents=True, exist_ok=True)
    (push_clone / CLAUDE_MD_PATH).write_text(make_lines(250))
    subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=push_clone, check=True)
    subprocess.run(["git", "commit", "-qm", "origin grows claude.md"], cwd=push_clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=push_clone, check=True)

    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    result = subprocess.run(
        ["git", "merge", "--no-commit", "-q", "origin/main"], cwd=clone, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / ".git" / "MERGE_HEAD").exists()
    return clone


def _build_conflicted_rebase_with_growing_claude_md(tmp_path: Path) -> Path:
    """Local copy of test_check_skill_length.py's fixture of the same shape:
    a conflicted rebase on an unrelated file, with CLAUDE.md separately
    grown past the limit as part of the staged resolution."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / CLAUDE_MD_PATH).parent.mkdir(parents=True, exist_ok=True)
    (repo / CLAUDE_MD_PATH).write_text(make_lines(100))
    subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed claude.md at 100"], cwd=repo, check=True)

    build_conflicted_rebase(repo)
    resolve_conflicted_rebase(repo)
    (repo / CLAUDE_MD_PATH).write_text(make_lines(250))
    subprocess.run(["git", "add", CLAUDE_MD_PATH], cwd=repo, check=True)
    return repo


class TestCheckClaudeMdLengthMergeAwareBase:
    """_lib_staged_length_gate's `old` comparison is measured against
    _lib_gate_diff_base's resolved base, and mid-merge vs. mid-rebase differ
    in which base gets used. Local mirror of
    TestCheckSkillLengthMergeAwareBase -- the shared driver in _lib.sh means
    both callers must show the same fixed defect and the same fix."""

    def test_mid_merge_pure_upstream_growth_does_not_fail(self, isolated_home, tmp_path):
        repo = _build_clean_merge_growing_claude_md(tmp_path)
        assert (
            run_hook(CHECK_CLAUDE_MD_LENGTH_HOOK, bash_input("git commit -m foo"), cwd=repo)
            == "allow"
        )

    def test_mid_rebase_growth_past_mid_rebase_head_still_denies(
        self, isolated_home, tmp_path
    ):
        repo = _build_conflicted_rebase_with_growing_claude_md(tmp_path)
        assert (
            run_hook(CHECK_CLAUDE_MD_LENGTH_HOOK, bash_input("git commit -m foo"), cwd=repo)
            == "deny"
        )
