#!/bin/bash
# hook-class: gate
# Gate: block git commit when a staged CLAUDE.md or AGENTS.md grows past its limit.
#
# Policy: deny when the staged file is over its limit AND longer than the
# previously committed version. This allows reducing an already-over-limit
# file commit by commit without blocking the work, while still catching new
# bloat.
#
# Fail posture: fail-closed — parse errors deny the commit. This gate
# enforces a style rule, not a security boundary, but consistent fail-closed
# posture across all gate hooks prevents a whole class of silent-allow
# regressions.
#
# Default limit is 200 lines, matching the Anthropic-documented threshold for
# CLAUDE.md/AGENTS.md files (Claude Code — memory: "Longer files consume more
# context and reduce adherence"). No per-file overrides exist today; the case
# structure is kept so future exceptions can slot in without touching the
# surrounding logic.
#
# The "if" field in settings.json is unreliable — the internal
# _lib_command_invokes_git_subcmd check is the actual gate. See
# require-code-review.sh for the same pattern and rationale.
#
# On a machine lacking both timeout(1) and gtimeout(1), _lib_capped runs the
# git calls below uncapped, so a stalled git (locked index, network mount)
# hangs this gate rather than degrading gracefully.
#
# The growth-comparison and deny-message logic is shared with
# check-skill-length.sh via _lib_staged_length_gate in _lib.sh — this file
# supplies the staged-path pattern, limit_for, and (unlike
# check-skill-length.sh) the byte-limit constant below. The commit-shape
# check and REPO_ROOT resolution above are duplicated per file (not inside
# _lib_staged_length_gate), matching require-code-review.sh's own ordering:
# the commit-shape check must run before any git subprocess spawns, and
# _lib_staged_length_gate needs REPO_ROOT already resolved as its own first
# argument.

set -uo pipefail

DENY_GATE_LABEL="CLAUDE.md length"

# Byte limit: 25,600 bytes = 25 KiB (binary reading), the nearest in-family
# precedent — MEMORY.md's documented "200 lines or 25KB" threshold
# (claude-skills/skills/ai-instruction-and-memory-files/REFERENCES.md's
# "Cross-vendor size table" section). Applies to every stow consumer's
# CLAUDE.md/AGENTS.md, the same scope the 200-line check above already has.
#
# Dated log of prior values (one line per raise: date, old value, new value,
# one-line reason) — empty today; append here on every future change.
GLOBAL_CLAUDE_MD_BYTE_LIMIT=25600

# Minimal bootstrap so a failed `source` of _lib.sh below can still deny.
# Re-pointed at _lib.sh's _lib_emit_deny immediately after a successful
# source — see _lib_parse_tool_input_or_deny's contract comment in _lib.sh
# for why the full jq-encode-or-hard-block body lives there, not here.
emit_deny() {
  printf 'Blocked by %s gate: %s\n' "$DENY_GATE_LABEL" "$1" >&2
  exit 2
}

if ! . "$(dirname "$0")/_lib.sh" 2>/dev/null; then
  # False positive: shellcheck's static pass doesn't model this stub-then-
  # override redefinition, which resolves correctly at call time (see
  # _lib.sh's _lib_emit_deny comment). Considered moving the definition
  # after the call instead, but that defeats the bootstrap's job of
  # covering the case where sourcing _lib.sh itself fails.
  # shellcheck disable=SC2218
  emit_deny "could not source _lib.sh."
fi
emit_deny() { _lib_emit_deny "$1"; }

_lib_parse_tool_input_or_deny "could not parse tool-input JSON."

# Only gate Bash tool calls.
if [ "$TOOL_NAME" != "Bash" ]; then
  exit 0
fi

# Only gate git commit commands -- checked here, before REPO_ROOT resolution
# below, so the overwhelming majority of Bash calls this hook is dispatched
# for (per the "if" field's documented unreliability above) never spawn a
# git subprocess at all. Matches require-code-review.sh's actual ordering,
# not just its REPO_ROOT-resolution shape. Checked and fail-closed: an
# undetermined match (sed/tr missing, killed, or erroring inside the helper)
# must not silently let an unscanned commit bypass the length check.
_lib_command_invokes_git_subcmd "$COMMAND" commit
GIT_COMMIT_MATCH_STATUS=$?
if [ "$GIT_COMMIT_MATCH_STATUS" -eq 1 ]; then
  exit 0
fi
if [ "$GIT_COMMIT_MATCH_STATUS" -ne 0 ]; then
  emit_deny "could not determine whether this command invokes git commit (status ${GIT_COMMIT_MATCH_STATUS}) — sed/tr may be missing, killed, or errored. Failing closed rather than letting an unscanned git commit bypass the length check."
  exit 0
fi

# Resolve the repo from the payload's cwd rather than this hook process's
# ambient cwd, matching require-code-review.sh's shape -- an ambient-cwd
# resolution would let a session whose shell drifted to a different working
# tree of the same repo compare against the wrong tree.
[ -z "$CWD" ] && CWD="$PWD"

REPO_ROOT=$(_lib_capped git -C "$CWD" rev-parse --show-toplevel 2>/dev/null)
if [ -z "$REPO_ROOT" ]; then
  # Not in a git repo — let git surface the error itself
  exit 0
fi

# Per-file limit override. Listed paths are repo-root-relative.
limit_for() {
  case "$1" in
    *)
      echo 200 ;;
  esac
}

# Matches CLAUDE.md and AGENTS.md at the repo root, inside any .claude/
# directory, or at any depth inside a .claude/ directory. Does NOT match
# files in arbitrary subdirectories (e.g. foo/CLAUDE.md) — only root-level
# and .claude/-scoped files.
_lib_staged_length_gate "$REPO_ROOT" '^(CLAUDE\.md|AGENTS\.md|(.*/)?\.claude/(CLAUDE|AGENTS)\.md)$' "one or more files grew past the 200-line or ${GLOBAL_CLAUDE_MD_BYTE_LIMIT}-byte limit." "$GLOBAL_CLAUDE_MD_BYTE_LIMIT"
