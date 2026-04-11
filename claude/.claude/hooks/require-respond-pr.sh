#!/bin/bash
# Gate: require /respond-pr when fetching or posting PR comments.
#
# Why: Claude habitually fetches only inline file comments
# (gh api .../pulls/N/comments) and misses top-level reviews and issue-level
# comments, which is the common PR response failure mode. The /respond-pr
# skill fetches all three AND enforces the [Claude Code] attribution prefix
# on replies.
#
# Bypass: the /respond-pr skill touches ~/.claude/.respond-pr-active at its
# start and removes it at the end. While the marker exists AND is fresh
# (<60 min old), this hook lets gh commands through so the skill itself
# doesn't recurse into its own gate. The 60-minute staleness cutoff
# prevents an orphaned marker from permanently disabling the gate if the
# skill errored out mid-execution.

INPUT=$(cat)
TOOL=$(printf '%s\n' "$INPUT" | jq -r '.tool_name // empty')

# Only gate Bash tool use
if [ "$TOOL" != "Bash" ]; then
  exit 0
fi

COMMAND=$(printf '%s\n' "$INPUT" | jq -r '.tool_input.command // empty')

# Bypass: fresh marker from the /respond-pr skill means we're already inside
# the skill and should let its own gh commands through.
MARKER="$HOME/.claude/.respond-pr-active"
if [ -f "$MARKER" ] && [ -n "$(find "$MARKER" -mmin -60 2>/dev/null)" ]; then
  exit 0
fi

# Match PR comment read/write patterns. Three forms:
#   gh api .../pulls/N/comments       (inline review comments)
#   gh api .../pulls/N/reviews        (top-level review bodies)
#   gh api .../issues/N/comments      (issue-level, which GH uses for PR top-level threads)
#   gh pr comment ...                 (post a top-level comment)
#   gh pr review ...                  (post a review)
if printf '%s\n' "$COMMAND" | grep -qE 'gh\s+api\s+[^|&;]*(pulls|issues)/[0-9]+/(comments|reviews)'; then
  :
elif printf '%s\n' "$COMMAND" | grep -qE 'gh\s+pr\s+(comment|review)(\s|$)'; then
  :
else
  exit 0
fi

echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"PR comment access blocked by respond-pr gate. Run the /respond-pr skill instead — it fetches inline file comments, top-level review bodies, AND issue-level comments (Claude habitually fetches only the first and misses real feedback), and it enforces the [Claude Code] attribution prefix on replies so comments posted through the GitHub token are clearly labeled as AI-generated. Do not ask the user for permission — run /respond-pr and let it handle this operation."}}'
