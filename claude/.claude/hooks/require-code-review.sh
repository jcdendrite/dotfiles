#!/bin/bash
# PreToolUse hook for Bash commands:
# 1. Gates git commit behind /code-review
# 2. Auto-allows known safe (read-only / low-risk) commands
# 3. Falls through to default "ask" for everything else

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

allow() {
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
}

ask_with_reason() {
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"ask\",\"permissionDecisionReason\":\"$1\"}}"
  exit 0
}

# --- Gate: require /code-review before git commit ---
if echo "$COMMAND" | grep -qE '^git\s+commit'; then
  ask_with_reason "Code review gate: Has /code-review been run on the changes being committed? If not, deny this commit and run /code-review first."
fi

# --- Block: integration / e2e test runners hit real dependencies ---
# Only flag when a known test runner is the command AND integration/e2e keywords are present
if echo "$COMMAND" | grep -qiE '(^|\s)(npm|npx|jest|vitest|mocha|pytest|python|go|cargo|make)\s' \
   && echo "$COMMAND" | grep -qiE '(integrat|e2e|end.to.end|test:int|test:e2e)'; then
  ask_with_reason "This command may run integration/e2e tests against shared resources (DB, APIs). Multiple sessions could conflict — confirm the target environment is safe to use."
fi

# --- Safelist: read-only and low-risk commands ---

# Extract the first token (the base command, ignoring leading env vars / cd chains)
BASE_CMD=$(echo "$COMMAND" | sed 's/^[A-Z_]*=[^ ]* *//' | awk '{print $1}' | sed 's|.*/||')

# Read-only git subcommands
if echo "$COMMAND" | grep -qE '^git (status|log|diff|branch|show|remote|stash list|tag|describe|rev-parse|shortlog|ls-files|ls-tree)'; then
  allow
fi

# Filesystem reads
case "$BASE_CMD" in
  ls|pwd|cat|head|tail|wc|file|which|type|stat|readlink|basename|dirname|realpath|tree)
    allow
    ;;
esac

# Process / environment inspection
case "$BASE_CMD" in
  echo|env|printenv|whoami|hostname|date|uname|id|locale|uptime)
    allow
    ;;
esac

# Version checks (anything ending in --version or -v as sole arg)
if echo "$COMMAND" | grep -qE '^[a-z_-]+ (--version|-[vV])$'; then
  allow
fi

# Dev tool read-only queries
if echo "$COMMAND" | grep -qE '^(npm list|npm ls|npm outdated|npm view|npm info|npm config list)'; then
  allow
fi
if echo "$COMMAND" | grep -qE '^(pip list|pip show|pip freeze|pip check)'; then
  allow
fi
if echo "$COMMAND" | grep -qE '^(cargo metadata|cargo tree|cargo check)'; then
  allow
fi
if echo "$COMMAND" | grep -qE '^(go list|go env|go doc|go vet)'; then
  allow
fi

# Unit/component test runners — only auto-allow direct invocations of tools
# that overwhelmingly run against mocked dependencies. npm test/npm run test
# are excluded because they're indirection into arbitrary package.json scripts.
# Runners that commonly hit real dependencies (deno test, pytest, go test,
# cargo test, mvn test, gradle test) fall through to default "ask".
if echo "$COMMAND" | grep -qE '^(npx jest|npx vitest|npx mocha)'; then
  allow
fi

# Build commands
if echo "$COMMAND" | grep -qE '^(npm run build|npm run lint|npx tsc|make |make$|cargo build|cargo clippy|go build)'; then
  allow
fi

# --- Default: fall through to normal permission prompt ---
exit 0
