#!/bin/bash
# hook-class: gate
# Gate: require /code-review before git commit, verified via marker file.
#
# WARNING: Do NOT remove the internal git commit check below.
# The "if" field in settings.json is unreliable — it has been observed
# to fire this hook on ALL Bash commands (e.g., git reset, date).
# The internal _lib_command_invokes_git_subcmd check is the actual gate.
# The "if" field is a hint only.
#
# How it works:
# - The /code-review skill writes
#   ~/.claude/code-review-markers/<repo-hash>.<session_id> with the sha256 hash of
#   `git diff --cached` when the review is clean. The marker lives under
#   $HOME (not inside the repo) so it never pollutes `git status` or risks
#   being accidentally committed.
# - This hook recomputes `git diff --cached | sha256sum` at commit time and
#   looks for any marker under this repo-hash holding that value. Match =
#   the staged state was reviewed, allow the commit. Mismatch/missing = deny
#   and redirect Claude to run /code-review.
# - The <session_id> in the filename is a WRITE-side key only: it prevents
#   two parallel sessions in the same worktree from overwriting each other's
#   markers when they stage different diffs. The read globs across it,
#   because the stored hash — not the filename — is what proves the review
#   covered this diff. Reading the session key as an authorization predicate
#   would deny a resumed session (new session_id) a review it already
#   completed against the identical staged state.
# - The marker auto-invalidates as soon as the staging area changes, so
#   re-staging after review correctly forces a re-review.

set -uo pipefail

DENY_GATE_LABEL="code-review"

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

# Only gate Bash tool calls — exit 0 (no opinion) for everything else.
if [ "$TOOL_NAME" != "Bash" ]; then
  exit 0
fi

# Only gate git commit commands — exit 0 (no opinion) for everything else.
# Checked and fail-closed: an undetermined match (sed/tr missing, killed, or
# erroring inside the helper) must not silently let an unscanned commit
# through the review gate.
_lib_command_invokes_git_subcmd "$COMMAND" commit
GIT_COMMIT_MATCH_STATUS=$?
if [ "$GIT_COMMIT_MATCH_STATUS" -eq 1 ]; then
  exit 0
fi
if [ "$GIT_COMMIT_MATCH_STATUS" -ne 0 ]; then
  emit_deny "could not determine whether this command invokes git commit (status ${GIT_COMMIT_MATCH_STATUS}) — sed/tr may be missing, killed, or errored. Failing closed rather than letting an unscanned git commit bypass the review gate."
  exit 0
fi

# Resolve the repo from the payload's cwd rather than this hook process's
# ambient cwd, and thread that one root through every git call below. The
# marker's path (repo hash) and its value (staged-diff hash) must describe the
# same tree: resolving the root one way and hashing the diff another lets a
# session whose shell drifted to a different working tree of the same repo
# satisfy the gate with a review of a tree nobody reviewed.
[ -z "$CWD" ] && CWD="$PWD"

REPO_ROOT=$(_lib_capped git -C "$CWD" rev-parse --show-toplevel 2>/dev/null)
if [ -z "$REPO_ROOT" ]; then
  # Not in a git repo — let git surface the error itself
  exit 0
fi

# Resolved once for this hook invocation and threaded through both the
# empty-diff check below and the marker hash further down -- a second
# resolution here would double the merge-tree cost per commit for the exact
# same result. Empty BASE means no in-progress state was trusted (status 1)
# or the base could not be computed (status 2); either way the empty-diff
# check below issues plain `git diff --cached`, with no base argument.
GATE_DIFF_BASE=$(_lib_gate_diff_base "$REPO_ROOT")
GATE_DIFF_BASE_STATUS=$?

# Empty literal `git diff --cached`, with no trusted in-progress state to
# substitute a base for: this commit authors nothing at all --
# amend-message-only, --allow-empty, or nothing staged. Unaffected by the
# base substitution below -- there is no trusted-but-forgeable anchor in
# play here, so this stays a plain, unconditional early exit with no base
# argument.
# deny-invisible-commit-content.sh is what makes an empty diff here mean this
# commit authors an empty commit, not that no commit will happen — do not
# remove either half independently.
#
# A non-empty GATE_DIFF_BASE never takes this early exit, even on an empty
# base-relative diff, since that emptiness could be a forged anchor rather
# than an honest resolution. The marker comparison below still catches a
# forged base: its value binds to GATE_DIFF_BASE's identity instead of
# collapsing to sha256(""), so a marker from one base's empty-diff case
# can't validate a different base.
if [ -z "$GATE_DIFF_BASE" ]; then
  EMPTY_DIFF_CHECK=$(_lib_capped git -C "$REPO_ROOT" diff --cached 2>/dev/null)
  if [ -z "$EMPTY_DIFF_CHECK" ]; then
    exit 0
  fi
fi

# Honor in-chain marker writes. When the same Bash call chains
# `marker.sh write code-review` before `git commit`, the on-disk marker
# does not exist yet at PreToolUse time (the chain has not run), so the
# usual marker check below would deny. The in-chain marker.sh invocation
# is the same evidence the on-disk marker would later provide -- marker.sh
# is the only sanctioned writer in either case.
if _lib_chains_marker_write_before_commit "$COMMAND" code-review; then
  exit 0
fi

REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
CURRENT_HASH=$(_lib_code_review_marker_value "$REPO_ROOT" "$GATE_DIFF_BASE")

# Recomputed here, not threaded from `_lib_code_review_marker_value`, so the
# compliance log can flag this security-relevant empty-base-relative-diff
# branch separately from an ordinary mismatch.
EMPTY_BASE_RELATIVE_DIFF_SUFFIX=""
if [ -n "$GATE_DIFF_BASE" ] \
  && [ "$CURRENT_HASH" = "$(_lib_hash_diff_text "$(_lib_code_review_empty_base_sentinel "$GATE_DIFF_BASE")")" ]; then
  EMPTY_BASE_RELATIVE_DIFF_SUFFIX="-empty-base-relative-diff"
fi

# Fail closed: an unresolvable config dir must deny the gate, not silently
# skip the marker check and let the commit through.
if ! CONFIG_DIR=$(_lib_config_dir); then
  emit_deny "could not resolve the Claude Code config directory (CLAUDE_CONFIG_DIR is set to a relative path, or \$HOME is unset/empty)."
  exit 0
fi

# Compliance backstop: non-blocking log line recording ledger presence +
# marker outcome at both exit paths; never affects this gate's decision. See
# docs/hooks.md's require-code-review.sh entry for the accepted-risk rationale.
LEDGER_SESSION_ID="$SESSION_ID"
LEDGER_STATE="absent"
if [ -n "$LEDGER_SESSION_ID" ] && _lib_valid_session_id_component "$LEDGER_SESSION_ID" \
  && [ -f "$CONFIG_DIR/review-narrative-ledger/$REPO_HASH.$LEDGER_SESSION_ID.jsonl" ]; then
  LEDGER_STATE="present"
fi
COMPLIANCE_LOG="$CONFIG_DIR/.review-ledger-compliance.log"

# Appends one compliance line, end to end timeout-bounded. A bare `>>` on
# this script's own shell opens the target before _lib_capped's internal
# timeout ever starts (bash sets up a function call's redirect before
# entering the function body) — forking the redirect into its own `bash -c`
# under _lib_capped moves that open() into the timeout-supervised subprocess.
_log_compliance_line() {
  local outcome="$1"
  local line
  line=$(_lib_capped printf '%s marker=%s ledger=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$outcome" "$LEDGER_STATE")
  # shellcheck disable=SC2016 # single-quoted on purpose: $1/$2 are the
  # nested bash -c script's own positional params, meant to expand there, not
  # in this outer shell.
  _lib_capped bash -c 'printf "%s\n" "$1" >> "$2"' _ "$line" "$COMPLIANCE_LOG" 2>/dev/null || true
}

# Allow when any marker under this repo-hash holds the currently staged
# diff's hash. The stored hash is the authorization — it proves a review
# covered exactly this diff — so the question is "has this diff been
# reviewed?", not "did this session review it?". An empty CURRENT_HASH
# (sha256sum unavailable) never matches, so a hashing failure denies.
if _lib_marker_value_present "$CONFIG_DIR/code-review-markers" "$CURRENT_HASH" "$REPO_HASH."; then
  _log_compliance_line "matched${EMPTY_BASE_RELATIVE_DIFF_SUFFIX}"
  exit 0
fi

_log_compliance_line "unmatched${EMPTY_BASE_RELATIVE_DIFF_SUFFIX}"

# No marker, or marker hash does not match the current staged state.
# Build the reason as a bash variable so the conditional undetermined-base
# note can be interpolated; jq -Rs handles JSON-encoding safely regardless
# of what characters appear in the appended note.
DENY_REASON="Commit — the currently staged changes have not been reviewed, or the staged state has changed since the last review. Run the /code-review skill now on the currently staged diff. When the review is clean (no blockers), the skill will record the review in ~/.claude/code-review-markers/ and this commit will be allowed through on retry. Do not ask the user for permission — run the skill, address any findings, and retry the commit."
# Status 2 never changes this allow/deny decision -- it only changes what
# this message says, so a timeout-driven full-diff fallback reads as
# distinguishable from an ordinary marker mismatch rather than this gate
# silently not working.
if [ "$GATE_DIFF_BASE_STATUS" -eq 2 ]; then
  DENY_REASON="${DENY_REASON} Note: this repo appears to be mid-merge/rebase/cherry-pick/revert, but the novel-content base could not be computed (a git call timed out, was killed, or its binary was missing), so this gate fell back to the full HEAD-relative diff instead of excluding already-reviewed upstream content."
fi
emit_deny "$DENY_REASON"
