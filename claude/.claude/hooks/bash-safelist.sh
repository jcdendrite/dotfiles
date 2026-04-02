#!/bin/bash
# Auto-allow known safe (read-only / low-risk) bash commands.
# Commands not matched here fall through to the default "ask" prompt.

INPUT=$(cat)
COMMAND=$(printf '%s\n' "$INPUT" | jq -r '.tool_input.command // empty')

allow() {
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
}

# --- Reject compound/chained commands ---
# Commands with shell metacharacters could chain a dangerous command after
# a safe-looking prefix (e.g., "ls; rm -rf /"). Fall through to "ask".
if printf '%s\n' "$COMMAND" | grep -qE '[;|&`]|>\s*|>>\s*|<\s*|\$\('; then
  exit 0
fi

# --- Safelist: read-only and low-risk commands ---

# Extract the first token (the base command, stripping all leading KEY=VALUE assignments)
BASE_CMD=$(printf '%s\n' "$COMMAND" | sed 's/^\([A-Za-z_][A-Za-z_0-9]*=[^ ]* *\)*//' | awk '{print $1}' | sed 's|.*/||')

# Read-only git subcommands — end-anchored to prevent prefix matches on write operations
if printf '%s\n' "$COMMAND" | grep -qE '^git (status|log|diff|show|stash list|tag|describe|rev-parse|shortlog|ls-files|ls-tree)( |$)'; then
  allow
fi
# git remote — only viewing, not add/remove/rename/set-url
if printf '%s\n' "$COMMAND" | grep -qE '^git remote( -v)?$'; then
  allow
fi
# git branch — only listing operations, not -d/-D/--delete/create
if printf '%s\n' "$COMMAND" | grep -qE '^git branch( -[avr]+)*( --(list|show-current|all|remotes))?$'; then
  allow
fi

# Filesystem reads
case "$BASE_CMD" in
  ls|pwd|cat|head|tail|wc|file|which|type|stat|readlink|basename|dirname|realpath|tree)
    allow
    ;;
esac

# Process / environment inspection
# Note: env is excluded — it can execute arbitrary commands (e.g., env bash -c '...')
case "$BASE_CMD" in
  echo|printenv|whoami|hostname|date|uname|id|locale|uptime)
    allow
    ;;
esac

# Version checks (anything ending in --version or -v as sole arg)
if printf '%s\n' "$COMMAND" | grep -qE '^[a-z_-]+ (--version|-[vV])$'; then
  allow
fi

# Dev tool read-only queries
if printf '%s\n' "$COMMAND" | grep -qE '^(npm list|npm ls|npm outdated|npm view|npm info|npm config list)( |$)'; then
  allow
fi
if printf '%s\n' "$COMMAND" | grep -qE '^(pip list|pip show|pip freeze|pip check)( |$)'; then
  allow
fi
# cargo check/build and go vet/build excluded — they execute project code (build.rs, cgo)
if printf '%s\n' "$COMMAND" | grep -qE '^(cargo metadata|cargo tree)( |$)'; then
  allow
fi
if printf '%s\n' "$COMMAND" | grep -qE '^(go list|go env|go doc)( |$)'; then
  allow
fi

# Unit/component test runners — only auto-allow direct invocations of tools
# that overwhelmingly run against mocked dependencies. npm test/npm run test
# are excluded because they're indirection into arbitrary package.json scripts.
# Runners that commonly hit real dependencies (deno test, pytest, go test,
# cargo test, mvn test, gradle test) fall through to default "ask".
if printf '%s\n' "$COMMAND" | grep -qE '^(npx jest|npx vitest|npx mocha)( |$)'; then
  allow
fi

# Build commands
# make, cargo build, go build excluded — they execute arbitrary project code
if printf '%s\n' "$COMMAND" | grep -qE '^(npm run build|npm run lint|npx tsc|cargo clippy)( |$)'; then
  allow
fi

# --- Default: fall through to normal permission prompt ---
exit 0
