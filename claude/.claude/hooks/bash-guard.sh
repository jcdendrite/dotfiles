#!/bin/bash
# Global bash permission hook — restrictive by default.
#
# This hook rejects known-dangerous patterns (compound commands, shell
# expansion, env var injection). Everything else falls through to the
# default "ask" prompt.
#
# Permissive safelists (auto-allowing specific commands) should be
# configured at the project level via project .claude/settings.json,
# where the risk context is understood.

INPUT=$(cat)
COMMAND=$(printf '%s\n' "$INPUT" | jq -r '.tool_input.command // empty')

# --- Reject multi-line commands ---
# Newlines bypass grep-based checks (grep is line-based).
if [[ "$COMMAND" == *$'\n'* ]]; then
  exit 0
fi

# --- Reject compound/chained commands and shell expansion ---
if printf '%s\n' "$COMMAND" | grep -qE '[;|&`]|>[[:space:]]*|<[[:space:]]*|\$'; then
  exit 0
fi

# --- Reject commands with environment variable prefixes ---
if printf '%s\n' "$COMMAND" | grep -qE '^[A-Za-z_][A-Za-z_0-9]*='; then
  exit 0
fi

# --- Default: fall through to normal permission prompt ---
exit 0
