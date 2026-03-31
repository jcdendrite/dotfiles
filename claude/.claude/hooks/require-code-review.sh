#!/bin/bash
# Gate: require /code-review before git commit
INPUT=$(cat)

# Only gate git commit commands — exit 0 (allow) for everything else
if ! echo "$INPUT" | grep -q 'git commit'; then
  exit 0
fi

echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"Code review gate: Has /code-review been run on the changes being committed? If not, deny this commit and run /code-review first."}}'
