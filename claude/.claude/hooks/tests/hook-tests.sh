#!/bin/bash
# End-to-end tests for the three Claude Code PreToolUse hooks.
#
# Feeds each hook realistic tool-input JSON on stdin and checks the
# permissionDecision on stdout. Uses a temp git repo for the code-review
# hook so `git diff --cached` is real.
#
# Run: bash hook-tests.sh
# Exit: 0 on all-pass, 1 on any failure.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS="$(cd "$SCRIPT_DIR/.." && pwd)"
CODE_REVIEW_HOOK="$HOOKS/require-code-review.sh"
RESPOND_PR_HOOK="$HOOKS/require-respond-pr.sh"
REVIEW_PERMS_HOOK="$HOOKS/ask-review-permissions.sh"

for hook in "$CODE_REVIEW_HOOK" "$RESPOND_PR_HOOK" "$REVIEW_PERMS_HOOK"; do
  if [ ! -x "$hook" ]; then
    echo "ERROR: hook not executable: $hook" >&2
    exit 2
  fi
done

PASS=0
FAIL=0

# Run a hook with given stdin. Echo "allow" if the hook exits silently,
# otherwise echo the permissionDecision field from its JSON output.
run_hook() {
  local hook="$1"
  local input="$2"
  local out
  out=$(printf '%s' "$input" | "$hook" 2>/dev/null)
  if [ -z "$out" ]; then
    echo allow
    return
  fi
  echo "$out" | jq -r '.hookSpecificOutput.permissionDecision // "unknown"'
}

check() {
  local label="$1"
  local expected="$2"
  local actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS+1))
    printf '  PASS  %s\n' "$label"
  else
    FAIL=$((FAIL+1))
    printf '  FAIL  %s (expected=%s actual=%s)\n' "$label" "$expected" "$actual"
  fi
}

bash_input() {
  jq -nc --arg c "$1" '{tool_name:"Bash",tool_input:{command:$c}}'
}

edit_input() {
  jq -nc --arg p "$1" '{tool_name:"Edit",tool_input:{file_path:$p,old_string:"a",new_string:"b"}}'
}

write_input() {
  jq -nc --arg p "$1" '{tool_name:"Write",tool_input:{file_path:$p,content:"x"}}'
}

# ----- setup: temp git repo -----
TMPREPO=$(mktemp -d)
REPO_HASH=$(printf '%s' "$TMPREPO" | sha256sum | awk '{print $1}')
MARKER="$HOME/.claude/review-markers/$REPO_HASH"
mkdir -p "$HOME/.claude/review-markers"

# Preserve any existing respond-pr marker so the test doesn't clobber live state
RESPOND_PR_BACKUP=""
if [ -f "$HOME/.claude/.respond-pr-active" ]; then
  RESPOND_PR_BACKUP=$(mktemp)
  cp "$HOME/.claude/.respond-pr-active" "$RESPOND_PR_BACKUP"
fi

cleanup() {
  rm -rf "$TMPREPO"
  rm -f "$MARKER"
  rm -f "$HOME/.claude/.respond-pr-active"
  if [ -n "$RESPOND_PR_BACKUP" ] && [ -f "$RESPOND_PR_BACKUP" ]; then
    mv "$RESPOND_PR_BACKUP" "$HOME/.claude/.respond-pr-active"
  fi
}
trap cleanup EXIT

cd "$TMPREPO"
git init -q
git config user.email test@test.com
git config user.name test
echo first > file.txt
git add file.txt
git commit -q -m init
echo second >> file.txt
git add file.txt

# ================================================================
echo
echo "=== require-code-review.sh ==="
# ================================================================

# No marker yet → deny on commit
rm -f "$MARKER"
check "no marker → deny" deny \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git commit -m foo')")"

# Wrong hash marker → deny
echo "0000000000000000000000000000000000000000000000000000000000000000" > "$MARKER"
check "wrong-hash marker → deny" deny \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git commit -m foo')")"

# Correct hash marker → allow
git diff --cached | sha256sum | awk '{print $1}' > "$MARKER"
check "correct-hash marker → allow" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git commit -m foo')")"

# Chained form: git add ... && git commit → still gated, marker current
check "chained 'git add && git commit' → allow (marker current)" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git add file.txt && git commit -m foo')")"

# Stage more changes → marker is now stale → deny
echo third >> file.txt
git add file.txt
check "re-staged → stale marker → deny" deny \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git commit -m foo')")"

# Chained form also denied when marker is stale
check "chained 'git add && git commit' → deny (marker stale)" deny \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git add file.txt && git commit -m foo')")"

# Refresh marker → allow
git diff --cached | sha256sum | awk '{print $1}' > "$MARKER"
check "refreshed marker → allow" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git commit -m foo')")"

# End-to-end: run the EXACT shell command from code-review SKILL.md and verify
# the hook finds the marker it writes. Guards against trailing-newline drift
# between `printf '%s' "$REPO_ROOT"` (hook) and `git rev-parse | sha256sum`
# (skill) producing different repo-hash paths.
rm -f "$HOME/.claude/review-markers/"*
mkdir -p "$HOME/.claude/review-markers" && git diff --cached | sha256sum | awk '{print $1}' > "$HOME/.claude/review-markers/$(git rev-parse --show-toplevel | tr -d '\n' | sha256sum | awk '{print $1}')"
check "skill's marker-write command → hook allows" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git commit -m foo')")"
# Restore the canonical marker path for subsequent tests
rm -f "$HOME/.claude/review-markers/"*
git diff --cached | sha256sum | awk '{print $1}' > "$MARKER"

# Empty staged diff → allow (amend-message, --allow-empty, nothing to commit)
git commit -q -m tmp
check "empty staged diff → allow" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git commit --amend -m new-message')")"

# Non-commit git commands → allow (hook has no opinion)
check "git status → allow" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git status')")"
check "git log → allow" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git log --oneline')")"
check "git commit-tree → allow (prefix collision)" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git commit-tree abc123')")"

# Non-Bash tool → allow (hook only gates Bash)
check "Edit tool → allow" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(edit_input '/tmp/foo.txt')")"

# Outside a git repo → allow (hook bails rather than false-denying)
cd /tmp
check "outside git repo → allow" allow \
  "$(run_hook "$CODE_REVIEW_HOOK" "$(bash_input 'git commit -m foo')")"
cd "$TMPREPO"

# ================================================================
echo
echo "=== require-respond-pr.sh ==="
# ================================================================

rm -f "$HOME/.claude/.respond-pr-active"

# Matching patterns → deny
check "gh api pulls/N/comments → deny" deny \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh api repos/foo/bar/pulls/5/comments')")"
check "gh api pulls/N/reviews → deny" deny \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh api repos/foo/bar/pulls/5/reviews')")"
check "gh api issues/N/comments → deny" deny \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh api repos/foo/bar/issues/5/comments')")"
check "gh pr comment → deny" deny \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh pr comment 5 --body test')")"
check "gh pr review → deny" deny \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh pr review 5 --approve')")"
check "posting via gh api with -F body → deny" deny \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh api repos/foo/bar/pulls/5/comments -F body=hi')")"

# Non-matching gh commands → allow
check "gh pr view → allow" allow \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh pr view 5')")"
check "gh pr list → allow" allow \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh pr list')")"
check "gh api user → allow" allow \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh api user')")"
check "gh pr checkout → allow" allow \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh pr checkout 5')")"

# Unrelated commands → allow
check "echo foo → allow" allow \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'echo foo')")"
check "git status → allow" allow \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'git status')")"

# Bypass marker (fresh) → allow matching patterns
touch "$HOME/.claude/.respond-pr-active"
check "bypass marker fresh → allow gh api pulls comments" allow \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh api repos/foo/bar/pulls/5/comments')")"
check "bypass marker fresh → allow gh pr comment" allow \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh pr comment 5 --body test')")"

# Bypass marker (stale, >60 min) → deny again
touch -d '90 minutes ago' "$HOME/.claude/.respond-pr-active"
check "bypass marker stale → deny" deny \
  "$(run_hook "$RESPOND_PR_HOOK" "$(bash_input 'gh api repos/foo/bar/pulls/5/comments')")"
rm -f "$HOME/.claude/.respond-pr-active"

# Non-Bash tool → allow
check "Edit tool → allow" allow \
  "$(run_hook "$RESPOND_PR_HOOK" "$(edit_input '/tmp/foo.txt')")"

# ================================================================
echo
echo "=== ask-review-permissions.sh ==="
# ================================================================

# Matching paths → ask
check "Edit .claude/settings.json → ask" ask \
  "$(run_hook "$REVIEW_PERMS_HOOK" "$(edit_input '/home/jared/foo/.claude/settings.json')")"
check "Edit .claude/settings.local.json → ask" ask \
  "$(run_hook "$REVIEW_PERMS_HOOK" "$(edit_input '/home/jared/foo/.claude/settings.local.json')")"
check "Write .claude/settings.json → ask" ask \
  "$(run_hook "$REVIEW_PERMS_HOOK" "$(write_input '/home/jared/foo/.claude/settings.json')")"

# Non-matching paths → allow
check "Edit project/package.json → allow" allow \
  "$(run_hook "$REVIEW_PERMS_HOOK" "$(edit_input '/home/jared/foo/package.json')")"
check "Edit .claude/CLAUDE.md → allow" allow \
  "$(run_hook "$REVIEW_PERMS_HOOK" "$(edit_input '/home/jared/foo/.claude/CLAUDE.md')")"
check "Edit .claude/skills/foo.md → allow" allow \
  "$(run_hook "$REVIEW_PERMS_HOOK" "$(edit_input '/home/jared/foo/.claude/skills/foo.md')")"

# Non-Edit/Write tool → allow
check "Bash tool → allow" allow \
  "$(run_hook "$REVIEW_PERMS_HOOK" "$(bash_input 'cat /home/jared/.claude/settings.json')")"

echo
echo "================================================================"
echo "Total: $((PASS+FAIL))  Pass: $PASS  Fail: $FAIL"
[ "$FAIL" -eq 0 ]
