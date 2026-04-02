#!/bin/bash
# Auto-allow known safe (read-only / low-risk) bash commands.
# Commands not matched here fall through to the default "ask" prompt.

INPUT=$(cat)
COMMAND=$(printf '%s\n' "$INPUT" | jq -r '.tool_input.command // empty')

allow() {
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
}

# --- Reject multi-line commands ---
# Newlines in the command string bypass grep-based checks (grep is line-based).
# A command like "git status\nrm -rf /" would match the git safelist on line 1.
if [[ "$COMMAND" == *$'\n'* ]]; then
  exit 0
fi

# --- Reject compound/chained commands ---
# Commands with shell metacharacters could chain a dangerous command after
# a safe-looking prefix (e.g., "ls; rm -rf /"). Fall through to "ask".
if printf '%s\n' "$COMMAND" | grep -qE '[;|&`]|>[[:space:]]*|<[[:space:]]*|\$[({A-Za-z_]'; then
  exit 0
fi

# --- Reject commands with environment variable prefixes ---
# KEY=VALUE prefixes can override LD_PRELOAD, PATH, etc. to hijack safe commands.
if printf '%s\n' "$COMMAND" | grep -qE '^[A-Za-z_][A-Za-z_0-9]*='; then
  exit 0
fi

# --- Safelist: read-only and low-risk commands ---

# Extract the first token as the base command
BASE_CMD=$(printf '%s\n' "$COMMAND" | awk '{print $1}' | sed 's|.*/||')

# Read-only git subcommands — reject dangerous flags (--output, --ext-diff, --textconv)
if printf '%s\n' "$COMMAND" | grep -qE '^git (status|log|diff|show|stash list|tag|describe|rev-parse|shortlog|ls-files|ls-tree)( |$)'; then
  if printf '%s\n' "$COMMAND" | grep -qiE '\-\-(output|ext-diff|textconv|exec|upload-pack)'; then
    exit 0
  fi
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

# Process / system inspection
# Excluded: env, printenv (secrets), whoami, hostname, id (PII),
# uname (machine fingerprinting), locale (geographic/language info)
case "$BASE_CMD" in
  echo|date|uptime)
    allow
    ;;
esac

# Version checks (anything ending in --version or -v as sole arg)
if printf '%s\n' "$COMMAND" | grep -qE '^[a-z0-9._-]+ (--version|-[vV])$'; then
  allow
fi

# Dev tool read-only queries
# npm config list excluded — leaks _authToken from .npmrc
if printf '%s\n' "$COMMAND" | grep -qE '^(npm list|npm ls|npm outdated|npm view|npm info)( |$)'; then
  allow
fi
if printf '%s\n' "$COMMAND" | grep -qE '^(pip list|pip show|pip freeze|pip check)( |$)'; then
  allow
fi
if printf '%s\n' "$COMMAND" | grep -qE '^(cargo metadata|cargo tree)( |$)'; then
  allow
fi
if printf '%s\n' "$COMMAND" | grep -qE '^(go list|go env|go doc)( |$)'; then
  allow
fi

# Test runners — all excluded. They execute project code (config files, test
# files, setup scripts) and may hit real dependencies.

# Build commands — all excluded. npm run build/lint execute arbitrary
# package.json scripts, cargo clippy runs build.rs/proc macros. Same
# blast radius as test runners.

# --- Default: fall through to normal permission prompt ---
exit 0
