#!/bin/bash
# Gate: require /code-review before git commit, verified via marker file.
#
# WARNING: Do NOT remove the internal git commit check below.
# The "if" field in settings.json is unreliable — it has been observed
# to fire this hook on ALL Bash commands (e.g., git reset, date).
# The internal grep is the actual gate. The "if" field is a hint only.
#
# How it works:
# - The /code-review skill writes ~/.claude/review-markers/<repo-hash> with
#   the sha256 hash of `git diff --cached` when the review is clean. The
#   marker lives under $HOME (not inside the repo) so it never pollutes
#   `git status` or risks being accidentally committed.
# - This hook recomputes `git diff --cached | sha256sum` at commit time
#   and compares. Match = the staged state was reviewed, allow the commit.
#   Mismatch/missing = deny and redirect Claude to run /code-review.
# - The marker auto-invalidates as soon as the staging area changes, so
#   re-staging after review correctly forces a re-review.

INPUT=$(cat)
COMMAND=$(printf '%s\n' "$INPUT" | jq -r '.tool_input.command // empty')

# Only gate git commit commands — exit 0 (no opinion) for everything else.
# Match `git commit` at the start of the command OR after a shell separator
# (`&&`, `||`, `;`, `|`, `&`), so chained forms like `git add . && git commit`
# are also caught. The trailing `(\s|$)` ensures we don't match `git commit-tree`
# or other `git commit`-prefixed subcommands.
if ! printf '%s\n' "$COMMAND" | grep -qE '(^|&&?|;|\|\|?)\s*git\s+commit(\s|$)'; then
  exit 0
fi

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -z "$REPO_ROOT" ]; then
  # Not in a git repo — let git surface the error itself
  exit 0
fi

# Empty staged diff: amend-message-only, --allow-empty, or nothing to commit.
# No new content to review; let git decide whether the commit is valid.
if [ -z "$(git diff --cached 2>/dev/null)" ]; then
  exit 0
fi

REPO_HASH=$(printf '%s' "$REPO_ROOT" | sha256sum | awk '{print $1}')
MARKER="$HOME/.claude/review-markers/$REPO_HASH"
CURRENT_HASH=$(git diff --cached | sha256sum | awk '{print $1}')

if [ -f "$MARKER" ]; then
  MARKER_HASH=$(tr -d '[:space:]' < "$MARKER")
  if [ "$MARKER_HASH" = "$CURRENT_HASH" ]; then
    # Marker hash matches currently staged diff — review is current, allow.
    exit 0
  fi
fi

# No marker, or marker hash does not match the current staged state.
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Commit blocked by code-review gate: the currently staged changes have not been reviewed, or the staged state has changed since the last review. Run the /code-review skill now on the currently staged diff. When the review is clean (no blockers), the skill will record the review in ~/.claude/review-markers/ and this commit will be allowed through on retry. Do not ask the user for permission — run the skill, address any findings, and retry the commit."}}'
