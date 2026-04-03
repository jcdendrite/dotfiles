#!/bin/bash
# Gate: require /code-review before git commit
# Scoped by "if" in settings.json, but also filters internally as defense-in-depth.
INPUT=$(cat)
COMMAND=$(printf '%s\n' "$INPUT" | jq -r '.tool_input.command // empty')

# Only gate git commit commands — exit 0 (no opinion) for everything else
if ! printf '%s\n' "$COMMAND" | grep -qE '^git\s+commit'; then
  exit 0
fi

echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"Code review gate: Has /code-review been run on the changes being committed? If not, deny this commit and run /code-review first."}}'
