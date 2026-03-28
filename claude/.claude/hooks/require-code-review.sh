#!/bin/bash
# Gate: require /code-review before git commit
# The "if" field in settings.json ensures this only runs for git commit commands.
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"Code review gate: Has /code-review been run on the changes being committed? If not, deny this commit and run /code-review first."}}'
