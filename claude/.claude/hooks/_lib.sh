#!/bin/bash
# Shared helper library sourced by require-*.sh hooks and scripts/marker.sh.
# Keep this file the single source of truth for any recipe that must produce
# byte-identical output on both the read side (hooks) and the write side
# (marker.sh). Source it; do not invoke it directly.

# Backstop against a hung jq (~5s, not a per-fire latency budget).
# Cites guard-settings-session-keys.sh's _lib_capped 5s precedent.
# Probes timeout(1) then gtimeout(1) (Homebrew coreutils' g-prefixed name),
# falling back to bare jq only when neither is on PATH (stock macOS with no
# coreutils installed). install.sh warns about missing timeout at onboarding
# time.
# Security implication: on a machine with neither binary, a stalled or
# replaced jq binary can hold a gate hook open indefinitely. The harness's
# own hook timeout (if any) then governs — not this wrapper.
_lib_jq() {
  _lib_capped_for 5 jq "$@"
}

# Same backstop and same probe-then-fallback as _lib_jq, for any other
# command that reads the filesystem and can stall on it (git against a
# locked .git/index, sha256sum against a dead NFS mount). Callers MUST check
# the exit status: a bare `timeout 5 git ...` is not just uncapped when
# timeout(1) is missing, it is "command not found" (127), which silently
# yields empty output on stock macOS.
# Usage: out=$(_lib_capped git -C "$root" ls-files ...) || <fail closed>
_lib_capped() {
  _lib_capped_for 5 "$@"
}

# _lib_capped_for SECONDS CMD [ARGS...]
# Shared probe-then-run logic behind _lib_capped and _lib_jq: probes
# timeout(1), then gtimeout(1) (Homebrew coreutils' g-prefixed name), and
# runs CMD uncapped only when neither is on PATH. Callers MUST check the
# exit status — see _lib_capped's usage note above, which applies here too.
# SECONDS must be a literal or a value guaranteed non-empty -- an empty or
# unset value hard-aborts the sourcing script instead of failing this call.
_lib_capped_for() {
  local seconds="${1:?_lib_capped_for requires a seconds argument}"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$seconds" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$seconds" "$@"
  else
    "$@"
  fi
}

# Portable `realpath -m TARGET`: normalizes a path without requiring TARGET (a Write's not-yet-existing destination) or any ancestor to exist. BSD/macOS realpath has no -m; falls back to grealpath, then to resolving the nearest existing ancestor and reattaching the unresolved suffix.
# Each external realpath/grealpath call below is wrapped individually in _lib_capped -- `timeout` can't wrap a shell function directly.
_lib_realpath_m() {
  local target="$1"
  local resolved
  if resolved=$(_lib_capped realpath -m -- "$target" 2>/dev/null) && [ -n "$resolved" ]; then
    printf '%s\n' "$resolved"
    return 0
  fi
  if command -v grealpath >/dev/null 2>&1 \
    && resolved=$(_lib_capped grealpath -m -- "$target" 2>/dev/null) && [ -n "$resolved" ]; then
    printf '%s\n' "$resolved"
    return 0
  fi
  local suffix="" current="$target" suffix_component
  while true; do
    if [ -e "$current" ]; then
      resolved=$(_lib_capped realpath -- "$current" 2>/dev/null) || return 1
      [ -n "$resolved" ] || return 1
      if [ -z "$suffix" ]; then
        printf '%s\n' "$resolved"
      elif [ "$resolved" = "/" ]; then
        printf '/%s\n' "$suffix"
      else
        printf '%s/%s\n' "$resolved" "$suffix"
      fi
      return 0
    fi
    if [ -L "$current" ]; then
      return 1  # dangling symlink: [ -e ] reports false for it, so without this check its own name would be reattached literally as an unresolved suffix component instead of failing closed.
    fi
    if [ "$current" = "/" ] || [ "$current" = "." ]; then
      return 1
    fi
    suffix_component=$(basename -- "$current")
    case "$suffix_component" in
      ..)
        return 1  # a `..` here could defeat a caller's same-prefix boundary check, so fail closed instead of normalizing it.
        ;;
    esac
    if [ -z "$suffix" ]; then
      suffix="$suffix_component"
    else
      suffix="$suffix_component/$suffix"
    fi
    current=$(dirname -- "$current")
  done
}

# _lib_advance_offset_past_complete_lines TRANSCRIPT OFFSET CURRENT_SIZE
# Prints the resume-from byte offset, stopping before any trailing
# partially-written line — shared mid-write-safety helper for hooks that scan
# a transcript incrementally from a stored byte offset instead of rescanning
# it whole on every fire. Used by nudge-handoff-near-context-cap.sh (see
# docs/handoff-nudge.md "What the hook does") and nudge-long-turn-subagent.sh.
# Known limitation: on a scan timeout in the slow path below, this returns
# OFFSET unchanged rather than partial progress. Total absence of `tail`
# from PATH makes the fast path above silently and permanently freeze the
# offset at CURRENT_SIZE, indistinguishable from the file genuinely ending
# in a newline.
_lib_advance_offset_past_complete_lines() {
  local transcript_path="$1" offset="$2" current_size="$3"
  if [ "$current_size" -le "$offset" ] 2>/dev/null; then
    printf '%s' "$offset"
    return
  fi
  # Fast path: the file's current last byte is a newline, so everything up
  # to current_size is complete lines — one 1-byte read covers the common
  # case (Claude Code writes each transcript record as a complete line).
  local last_byte
  last_byte=$(_lib_capped_for 2 tail -c 1 "$transcript_path" 2>/dev/null)
  if [ -z "$last_byte" ]; then
    printf '%s' "$current_size"
    return
  fi
  # Slow path: the file currently ends mid-line (caught mid-write). Count
  # complete lines in the unread slice, then measure exactly that many bytes
  # with `head`/`wc -c` — avoids locale-sensitive string-length arithmetic on
  # a captured shell variable.
  local newline_count complete_bytes
  newline_count=$(_lib_capped_for 2 tail -c +$((offset + 1)) "$transcript_path" 2>/dev/null \
    | tr -cd '\n' | wc -c | tr -d '[:space:]')
  case "$newline_count" in ''|*[!0-9]*|0) printf '%s' "$offset"; return ;; esac
  complete_bytes=$(_lib_capped_for 2 tail -c +$((offset + 1)) "$transcript_path" 2>/dev/null \
    | head -n "$newline_count" | wc -c | tr -d '[:space:]')
  case "$complete_bytes" in ''|*[!0-9]*) printf '%s' "$offset"; return ;; esac
  printf '%s' "$(( offset + complete_bytes ))"
}

# Prints the active Claude Code config directory: $CLAUDE_CONFIG_DIR if set
# (must be absolute — a relative value resolves differently per invocation
# cwd, the same path-mismatch bug this function exists to fix), else
# $HOME/.claude. Returns 1 with no stdout when CLAUDE_CONFIG_DIR is relative,
# or when CLAUDE_CONFIG_DIR is unset/empty and $HOME is also unset/empty.
# Call-site contract (load-bearing): bare interpolation,
# "$(_lib_config_dir)/whatever", is unsafe — under `set -e`, a failing
# *nested* command substitution does not abort the script, so a resolver
# failure silently collapses to "/whatever" (root-anchored) instead of being
# caught. Every call site must capture and check the exit status first:
#   config_dir=$(_lib_config_dir) || { <fail-open-or-deny per this caller>; }
_lib_config_dir() {
  if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
    case "$CLAUDE_CONFIG_DIR" in
      /*) ;;
      *) return 1 ;;  # relative values resolve differently per invocation
                      # cwd — the exact read/write path-mismatch bug this
                      # function fixes, just triggered a different way.
    esac
    printf '%s\n' "${CLAUDE_CONFIG_DIR%/}"
    return 0
  fi
  local home_norm="${HOME%/}"
  [ -n "$home_norm" ] || return 1
  printf '%s\n' "$home_norm/.claude"
}

# Canonical jq-encode-or-hard-block body for a gate hook's deny path.
# Deliberately NOT named `emit_deny`: sourcing this file must not silently
# satisfy the "CALLER MUST define emit_deny" contract below on its own, or a
# hook that forgets its own bootstrap `emit_deny` would inherit this one
# silently instead of bash's loud "command not found" —
# test_missing_emit_deny_loud_fail in test_lib.py pins that. Each gate hook
# defines a minimal bootstrap `emit_deny` before sourcing this file (so a
# failed `source` can still deny), then re-points `emit_deny` at this
# function immediately after a successful source:
#   emit_deny() { printf '%s\n' "$1" >&2; exit 2; }
#   if ! . ".../_lib.sh" 2>/dev/null; then emit_deny "..."; fi
#   emit_deny() { _lib_emit_deny "$1"; }
# An accidentally-dropped re-point line degrades UX only (every deny in that
# hook falls back to the bootstrap stub's plain exit-2 stderr instead of this
# function's JSON envelope) — it does not weaken the fail-closed guarantee,
# since the bootstrap stub still blocks. _lib_jq's timeout backstop is safe
# to rely on unconditionally here (unlike in the bootstrap stub) because this
# function only ever runs after _lib.sh
# has fully sourced.
#
# DENY_GATE_LABEL is declared by each gate hook before its bootstrap
# emit_deny stub above, so the pre-source and post-source deny paths emit
# the same "Blocked by <label> gate: " identity. A hook that forgets the
# declaration falls back to ${0##*/} with a trailing .sh stripped, rather
# than an unattributed bare body.
_lib_emit_deny() {
  local reason="$1"
  local label="${DENY_GATE_LABEL:-}"
  if [ -z "$label" ]; then
    label="${0##*/}"
    label="${label%.sh}"
  fi
  local prefixed_reason="Blocked by ${label} gate: ${reason}"
  local reason_json
  reason_json=$(printf '%s' "$prefixed_reason" | _lib_jq -Rs . 2>/dev/null)
  if [ -z "$reason_json" ]; then
    # jq is absent, failed, or was killed by the timeout backstop. Exit 2 is
    # the harness's blocking path for PreToolUse and carries the reason on
    # stderr, so it needs no JSON encoding. Emitting a half-built payload on
    # exit 0 instead would parse as no-decision and let the tool run.
    #
    # The fixed prefix is load-bearing: every gate parses its input with jq
    # before any command filtering, so a missing jq denies every tool call
    # with the parse-failure reason below — which names the wrong cause.
    # Without this line the session has no in-agent route to a fix.
    printf 'Hook gate could not encode its deny reason: jq is missing from PATH, failed, or timed out. Every gate hook blocks until this is fixed — this is deliberate, not a bug. In an interactive session, install jq (and GNU coreutils timeout) using the ! shell escape, which runs outside the tool-call path these hooks gate; in a headless or non-interactive run, ensure jq is installed in the execution environment beforehand. Underlying gate reason follows.\n%s\n' \
      "$prefixed_reason" >&2
    exit 2
  fi
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}\n' \
    "$reason_json"
}

# Emits a PreToolUse allow decision carrying an informational
# additionalContext note, for a caller that wants to explain a side effect
# of its own allow rather than silently permitting it. Mirrors
# _lib_emit_deny's jq-encode-or-degrade shape above, but degrades to a
# silent allow — no stdout — rather than _lib_emit_deny's hard block.
# A parse failure here resolves to no decision on stdout, which the
# harness already reads as the allow this caller wants, so losing the
# note is not the fail-closed case _lib_emit_deny protects against.
# permissionDecision is the exact lowercase literal "allow" -- the harness
# is case-sensitive here the same way it is for _lib_emit_deny's "deny".
# Caller contract matches _lib_emit_deny's. This function prints the JSON
# envelope (or nothing, on the degrade path) and returns. The caller still
# issues its own `exit 0` afterward, exactly as every _lib_emit_deny call
# site already does for the deny path.
_lib_emit_allow_with_context() {
  local context="$1"
  local context_json
  context_json=$(printf '%s' "$context" | _lib_jq -Rs . 2>/dev/null)
  [ -z "$context_json" ] && return 0
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","additionalContext":%s}}\n' \
    "$context_json"
}

# Reads stdin into INPUT (global), extracts TOOL_NAME, COMMAND, CWD,
# SESSION_ID, FILE_PATH, and AGENT_TYPE (globals) via a single _lib_jq call
# using ASCII Unit Separator (0x1f) as delimiter. The single call surfaces a
# structural-type error when .tool_input is non-object (jq non-zero exit).
#
# Four deny paths protect against silent-allow:
#   (a) jq non-zero exit (parse failure, timeout exit=124, missing jq binary)
#   (b) empty INPUT (stdin EOF, closed pipe, harness misbehavior)
#   (c) empty TOOL_NAME (valid JSON but PreToolUse contract not honored, e.g. "{}")
#   (d) a 0x1f byte inside any extracted value, which would otherwise shift
#       every field after it into the wrong global
# Per Anthropic PreToolUse contract, every legitimate event has a non-empty
# .tool_name; absence indicates the call did not originate from a real tool
# invocation. Without (b)/(c), downstream gates that early-exit on
# `[ "$TOOL_NAME" = "Bash" ]` silently allow on zero-byte or "{}" inputs.
#
# Contract: if this function returns, parse succeeded AND TOOL_NAME is set.
# On failure, calls emit_deny (caller-defined) with the supplied message and
# exits 0 — caller never checks $?. Never call this with `|| true` or in
# a pipeline. CALLER MUST define emit_deny before sourcing _lib.sh so this
# helper can resolve it. test_hook_alignment.py enforces the
# define-emit_deny-then-source pattern. See _lib_emit_deny above for the
# post-source re-pointing pattern hooks use to pick up the shared body.
_lib_parse_tool_input_or_deny() {
  local deny_msg="${1:-Blocked: could not parse tool-input JSON.}"
  INPUT=$(cat)  # command substitution strips trailing newlines — safe for JSON payloads
  if [ -z "$INPUT" ]; then
    emit_deny "$deny_msg"
    exit 0
  fi
  # Single jq call extracts all six fields delimited by ASCII Unit Separator
  # (0x1f) rather than newlines, preventing a value containing an embedded
  # newline from corrupting a later field via line splitting. Unit Separator
  # cannot appear in a valid Claude Code tool name, shell command, cwd,
  # session id, path, or agent type.
  # The .tool_input.command extraction additionally surfaces a structural-type
  # error when .tool_input is non-object (e.g. "Cannot index string with string
  # 'command'"), returning non-zero.
  # .cwd, .session_id, and .agent_type silently stringify via jq's \(...)
  # interpolation rather than erroring when the field holds a non-string
  # JSON value (a number, object, or array).
  # For AGENT_TYPE this is safe because both of its consumers
  # (_lib_is_review_only_agent, _lib_is_no_gate_release_agent) are
  # exact-match denylists, so a garbled value just fails to match and
  # falls through to the existing safe default.
  local jq_out
  # WARNING: the format string below contains five literal 0x1f (ASCII Unit
  # Separator) bytes, one between each of the six interpolated fields. They
  # are invisible in editors and diff views — do not remove them. test_lib.py's
  # six-field characterization test will fail immediately if a delimiter is
  # missing, catching accidental deletion.
  jq_out=$(printf '%s\n' "$INPUT" | _lib_jq -r '"\(.tool_name // "")\(.tool_input.command // "")\(.cwd // "")\(.session_id // "")\(.tool_input.file_path // "")\(.agent_type // "")"' 2>/dev/null)
  local jq_exit=$?
  if [ "$jq_exit" -ne 0 ]; then
    emit_deny "$deny_msg"
    exit 0
  fi
  # -d '' (NUL delimiter, absent from the input) is required rather than the
  # default newline delimiter, because COMMAND may legitimately contain
  # embedded newlines. mapfile/readarray, the bash-4 alternative, is
  # forbidden in this repo (test_no_bash4_constructs.py) because the target
  # includes bash 3.2. `read -r -d ''` returns non-zero at EOF without ever
  # finding that delimiter, on every invocation including this one's own
  # success path, hence the trailing `|| true`.
  # The appended sixth 0x1f plus the trailing _lib_parse_overflow variable is
  # a field-shift detector, not a per-field guard. A 0x1f byte inside any one
  # of the six values raises the split's field count above six regardless of
  # which field carries it. A well-formed payload therefore leaves
  # _lib_parse_overflow holding exactly the herestring's own trailing
  # newline and nothing else.
  # shellcheck disable=SC2034 # set for hook scripts that source this file and reference $COMMAND/$CWD/$SESSION_ID/$FILE_PATH/$AGENT_TYPE
  IFS=$'\x1f' read -r -d '' TOOL_NAME COMMAND CWD SESSION_ID FILE_PATH AGENT_TYPE _lib_parse_overflow <<< "$jq_out"$'\x1f' || true
  # This deny carries its own message rather than $deny_msg, so a deliberate
  # field-shift attempt classifies as a behavioral denial instead of being
  # filed under the shared parse-failure (infra) reason.
  if [ "$_lib_parse_overflow" != $'\n' ]; then
    emit_deny "a tool-input field contained a Unit Separator (U+001F) byte, which would shift extracted-field boundaries — refusing rather than acting on values that may not be the ones the harness sent."
    exit 0
  fi
  # Embedded newline in TOOL_NAME means the payload violated the PreToolUse
  # contract; deny rather than allow with a corrupted TOOL_NAME value.
  if [ -z "$TOOL_NAME" ]; then
    emit_deny "$deny_msg"
    exit 0
  fi
  case "$TOOL_NAME" in
    *$'\n'*) emit_deny "$deny_msg"; exit 0 ;;
  esac
}

# Raw repo-root resolution, shared so every caller resolving a given tree
# lands on the identical REPO_ROOT string. marker.sh's _resolve_repo_root
# layers _refuse_main_tree_under_enforcement on top of this; that check is
# write-specific and stays in marker.sh. Callers needing the same string on a
# read-only path (e.g. pr-diff-against-base.sh --record) call this directly.
# tr -d '\n' is load-bearing: git rev-parse appends a trailing newline that
# both sides must strip identically to agree on REPO_HASH. _lib_capped bounds
# a locked .git/index or a stale NFS mount, the same hazard
# _lib_cumulative_diff_hash's own git-dependent call guards against below.
# Exit 1, empty stdout: not inside a git repository, git is absent, or the
# call timed out.
_lib_repo_root() {
  local root
  root=$(_lib_capped git rev-parse --show-toplevel 2>/dev/null | tr -d '\n')
  [ -n "$root" ] || return 1
  printf '%s' "$root"
}

# Compute the marker repo-hash for an absolute repo-toplevel path.
# Input must have no trailing newline -- printf '%s' omits one, so the SHA
# covers exactly the bytes of $1.
# This binds PATH identity, not REPOSITORY identity: it digests the toplevel
# path string, not the initial commit, remote URL, or .git inode. A worktree
# removed and later replaced by an unrelated repository at the same absolute
# path inherits the old path's markers. Harmless today (a stale marker still
# has to match the content hash to authorize anything), but do not read this
# hash as proof that two markers came from the same repository.
# Separate known limitation, not computed by this function: a code-review
# marker's content hash (`git diff --cached`, computed in marker.sh) covers
# only the touched-file diff, not repo state outside those files.
# Usage: hash=$(_marker_lib_repo_hash "$REPO_ROOT")
_marker_lib_repo_hash() {
  local digest
  digest=$(_lib_hash_diff_text "$1") || return 1
  printf '%s\n' "$digest"
}

# _lib_marker_value_present MARKERS_DIR EXPECTED_VALUE GLOB_PREFIX...
# Returns 0 (true) iff some file in MARKERS_DIR whose name begins with one of
# the supplied prefixes holds EXPECTED_VALUE as a whole line.
#
# Completion markers are content-addressed: the stored value is a hash of
# exactly the state that was reviewed. That content is the authorization, so
# the read asks "has this state been reviewed?" rather than "did this filename
# review it?" — the filename keys only namespace concurrent writers apart.
#
# One `grep` process regardless of marker count. Marker directories are
# unbounded and already hold thousands of entries, so a per-file `cat` loop
# would put a fork count proportional to review history on a hook that fires
# on every Write/Edit.
#
# `-x` (whole-line) is load-bearing, not stylistic: with a bare `-F` a stored
# value that merely CONTAINS the expected hash — a truncated write, or a
# longer digest sharing this one as a prefix — would falsely release the gate.
#
# `nullglob` is equally load-bearing. Bash's default expands a zero-match
# pattern to the literal, unexpanded pattern string, which grep then tries to
# open as a real path and fails on (exit 2) — and "no marker exists yet for
# this prefix" is the single most common call, so the default behavior would
# misreport the common case as an error rather than as a clean not-found.
# The shopt state is saved and restored so callers that rely on the default
# glob behavior elsewhere are unaffected.
_lib_marker_value_present() {
  local markers_dir="$1" expected_value="$2"
  shift 2
  [ -n "$markers_dir" ] || return 1
  [ -n "$expected_value" ] || return 1
  [ -d "$markers_dir" ] || return 1
  [ "$#" -gt 0 ] || return 1

  local nullglob_was_set=0
  if shopt -q nullglob; then nullglob_was_set=1; fi
  shopt -s nullglob
  local -a marker_files=()
  local prefix
  for prefix in "$@"; do
    [ -n "$prefix" ] || continue
    marker_files+=("$markers_dir/$prefix"*)
  done
  if [ "$nullglob_was_set" -eq 0 ]; then shopt -u nullglob; fi

  [ "${#marker_files[@]}" -gt 0 ] || return 1
  # -e pins EXPECTED_VALUE as the pattern even if it begins with a dash; the
  # stderr redirect swallows the "Is a directory" noise a stray subdirectory
  # under MARKERS_DIR would otherwise produce.
  #
  # SCALE BOUNDARY: the matched files become one argv, so a single prefix
  # holding roughly 13k-30k markers (host-dependent; ARG_MAX bounded by the
  # stack rlimit) makes grep fail to exec with E2BIG. That is a nonzero exit,
  # which every call site reads as "no matching marker" — so it fails CLOSED,
  # denying rather than releasing. Completion markers are never pruned today
  # (marker.sh clear-stale only evicts active-bypass markers), so the ceiling
  # is reachable by unattended growth. Whoever adds marker retention should
  # remove this note; until then a wedged gate at that scale is a deny with no
  # diagnostic, and manual pruning of ~/.claude/*-markers/ is the workaround.
  # A flat glob (not `grep -r`) is deliberate: recursion would let a file
  # nested in a stray subdirectory authorize a gate.
  #
  # Callers must test truthiness, never `[ $? -eq 1 ]`: grep reports a plain
  # no-match as 1 but returns 2 when any argv entry errors — which a stray
  # subdirectory under MARKERS_DIR ("Is a directory") triggers even under -q.
  # Both are correctly false here; an exact-code check would not be.
  grep -qFx -e "$expected_value" -- "${marker_files[@]}" 2>/dev/null
}

# Enumerate the "active" plan file set in a repo's .claude/plans/ directory:
# untracked, or tracked-and-modified-vs-HEAD. A plan that is tracked and
# byte-identical to HEAD is historical (its PR shipped) and is excluded.
# Prints one repo-relative active-plan path per line. Shared by
# _lib_active_plan_hash below (which hashes the printed set) and by
# require-plan-review.sh's own fast-path guard, which needs to know whether
# anything is active before paying for a hash.
#
# Two-outcome contract -- exit status disambiguates stdout, because "nothing
# active" and "could not enumerate" must never collapse onto the same
# caller-visible signal:
#   - exit 0, stdout: zero or more repo-relative active-plan paths, one per
#     line. Empty stdout means no plan is active.
#   - exit 1, stdout = the path of .claude/plans/ itself: a git enumeration
#     call failed or timed out. Callers MUST fail closed -- a partial
#     enumeration must never be read as "nothing is active".
#
# Determinism contract (write side [marker.sh] and read side
# [require-plan-review.sh] must agree byte-for-byte, or the gate wedges):
#   - `LC_ALL=C sort` is required because a bare `sort` honors
#     `$LC_COLLATE`, which can differ between the write-side (Bash-tool
#     locale) and read-side (harness hook environment) callers, flipping
#     list order on >=2 active plans and producing a false-deny.
#   - `-u` collapses the overlap between the two unioned (not disjoint) git
#     queries.
#
# "Active" is exactly `untracked` UNION `tracked and modified vs HEAD`, so
# ask git for those two sets directly rather than listing the directory and
# probing each file's status. Enumerate-then-probe costs two git spawns per
# plan file (~1.2s on a 61-plan directory) and this runs on every
# Write/Edit/MultiEdit/ExitPlanMode.
#
# A failed or timed-out enumeration yields fewer files, which still looks
# like a clean result to both the write side and the read side -- so an
# unchecked call lets the gate open on a set neither side actually saw, with
# nothing logged. Every git call here is therefore capped and status-checked,
# so a partial enumeration fails closed instead.
# The "modified vs HEAD" leg diffs against BASE (already resolved by the
# caller, typically via _lib_gate_diff_base -- see that function's own
# docstring) instead of the literal HEAD, so a plan file a trusted
# in-progress state brought in untouched is excluded from the active set;
# an empty BASE means no override, diffing against literal HEAD exactly as
# before this substitution existed.
# :(glob) confines the `*` to one path segment, preserving the maxdepth-1
# scope.
# --others without --exclude-standard keeps gitignored plans in the set,
# since an ignored plan is still an unreviewed plan.
# --diff-filter=d drops deletions: a tracked plan deleted from the worktree
# is reported as modified but has no bytes left to hash, and treating it as
# active would fail the hash and deny forever instead of disarming.
# core.quotePath=false keeps non-ASCII filenames raw rather than C-escaped.
# Newline-delimited (not -z): a plan filename containing a newline is
# already unsupported, and -z would force the output through a command
# substitution, which strips NUL bytes.
# Usage: files=$(_lib_active_plan_files "$REPO_ROOT" "$BASE") || <fail closed>
_lib_active_plan_files() {
  local repo_root="$1" base="$2"
  local plans_dir="$repo_root/.claude/plans"
  [ -d "$plans_dir" ] || return 0

  local -a plan_pathspecs=(":(glob).claude/plans/*.md" ":(glob).claude/plans/*.txt")
  local untracked_plans modified_plans
  untracked_plans=$(_lib_capped git -C "$repo_root" -c core.quotePath=false \
    ls-files --others -- "${plan_pathspecs[@]}" 2>/dev/null) || {
    printf '%s' "$plans_dir"
    return 1
  }
  if _lib_capped git -C "$repo_root" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    modified_plans=$(_lib_capped git -C "$repo_root" -c core.quotePath=false \
      diff --name-only --diff-filter=d "${base:-HEAD}" -- "${plan_pathspecs[@]}" 2>/dev/null) || {
      printf '%s' "$plans_dir"
      return 1
    }
  else
    # No HEAD to diff against means nothing has shipped yet, so every
    # tracked plan still counts as active.
    modified_plans=$(_lib_capped git -C "$repo_root" -c core.quotePath=false \
      ls-files -- "${plan_pathspecs[@]}" 2>/dev/null) || {
      printf '%s' "$plans_dir"
      return 1
    }
  fi

  local plan_file
  while IFS= read -r plan_file; do
    [ -n "$plan_file" ] || continue
    printf '%s\n' "$plan_file"
  done < <(printf '%s\n%s\n' "$untracked_plans" "$modified_plans" | LC_ALL=C sort -u)
  return 0
}

# Compute a content-addressed hash of _lib_active_plan_files' output, for the
# plan-review completion marker. Hashes repo-relative paths AND contents, so
# editing an active plan (including a ledger row) changes the hash and
# re-arms the gate. Paths are hashed repo-relative rather than absolute
# because the write side and read side resolve the repo root independently;
# an absolute path would fold any difference between those two resolutions
# into the digest.
#
# BASE is already resolved by the caller and threaded straight into
# _lib_active_plan_files -- see that function's own docstring for why this
# is a required parameter rather than a self-contained resolution.
#
# Three-outcome contract -- exit status disambiguates stdout, because
# "nothing to gate" and "could not compute" must never collapse onto the
# same caller-visible signal:
#   - exit 0, non-empty stdout: the active plan set's hash -- the ordinary
#     content hash, or (when BASE is non-empty and no plan file differs from
#     it) a value bound to BASE's own identity instead of collapsing to
#     empty stdout, closing the same forged-base risk documented at
#     _lib_gate_diff_base's docstring and _lib_code_review_marker_value above.
#   - exit 0, empty stdout: no plan is active AND no trusted in-progress
#     state was detected (BASE empty) -- the gate is disarmed.
#   - exit 1, stdout = the path of the plan file that could not be hashed
#     (unreadable, vanished mid-enumeration, sha256sum failed), or of
#     .claude/plans/ itself when _lib_active_plan_files' own enumeration
#     failed. Callers MUST fail closed.
#
# Treating the exit-1 case as the disarmed case is fail-*open*: it lets an
# unreviewed plan edit through on a transient disk or permission blip,
# silently and with nothing logged. Reusing stdout for the offending path
# keeps this to one call site with no subshell visibility problem -- the
# exit status already says which meaning applies.
#
# Determinism contract (write side [marker.sh] and read side
# [require-plan-review.sh] must agree byte-for-byte, or the gate wedges) --
# see _lib_active_plan_files above for the file-list-ordering half of this
# contract:
#   - Path and per-file content-hash are newline-delimited per entry. This is
#     defensive, not load-bearing, given today's fixed-width 64-hex digests.
#     It would matter only if the digest becomes variable-width (keeps the
#     serialization injective) or when debugging a mismatch (keeps the hashed
#     input readable) -- do not cite it as a live collision defense.
#   - Every digest is captured into a variable and tested for emptiness
#     rather than trusted as a pipeline's exit status, which keeps the
#     contract independent of the caller's shell options. This is
#     load-bearing for marker.sh, which sources this file under `set -u`
#     with no `pipefail`: a failed `sha256sum` there still leaves `awk`
#     exiting 0 with empty output, which the emptiness check -- not
#     `pipefail` -- is what catches.
# Runs via require-plan-review.sh on every Edit/Write/MultiEdit/ExitPlanMode,
# same trigger as _lib_active_plan_files above.
# The per-file sha256sum loop below is bounded by the *active* plan count
# (0-1 in normal use, per that function's untracked-or-modified filter), not
# .claude/plans/'s total size, and returns early with zero forks when
# nothing is active.
# Usage: hash=$(_lib_active_plan_hash "$REPO_ROOT" "$BASE")
_lib_active_plan_hash() {
  local repo_root="$1" base="$2"
  local plans_dir="$repo_root/.claude/plans"

  local active_files
  if ! active_files=$(_lib_active_plan_files "$repo_root" "$base"); then
    printf '%s' "$active_files"
    return 1
  fi
  if [ -z "$active_files" ]; then
    if [ -n "$base" ]; then
      # A trusted in-progress state was detected but no plan file differs
      # from BASE. Binding to BASE's own identity rather than returning
      # empty stdout keeps a forged base's degenerate empty-active-set
      # result from silently disarming the gate the way an honestly-empty
      # result (no trusted state at all) safely does.
      _lib_hash_diff_text "plan-review-empty-base:$base"
      return $?
    fi
    return 0
  fi

  local file file_hash combined=""
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    file_hash=$(_lib_capped sha256sum -- "$repo_root/$file" 2>/dev/null | awk '{print $1}')
    if [ -z "$file_hash" ]; then
      # Unreadable or vanished mid-enumeration. Name the offending file on
      # stdout so the caller's deny message can point the user at it.
      printf '%s' "$repo_root/$file"
      return 1
    fi
    combined+="$file"$'\n'"$file_hash"$'\n'
  done <<< "$active_files"

  local digest
  if ! digest=$(_lib_hash_diff_text "$combined"); then
    printf '%s' "$plans_dir"
    return 1
  fi
  printf '%s' "$digest"
}

# _lib_hash_diff_text TEXT
# Hashes TEXT via the shared sha256 recipe every cumulative-review value must
# use: _lib_cumulative_diff_hash's own post-hash step below, and marker.sh's
# `write cumulative-review` arm, which hashes a recorded subject through this
# same function rather than a second, possibly-drifting copy of the recipe.
# TEXT may be empty -- sha256 of an empty string is still a valid digest, so
# this function doesn't treat empty input as failure. Refusing an empty
# subject is marker.sh's precondition, not this helper's.
# Exit 0, non-empty stdout: the sha256 hex digest of TEXT.
# Exit 1, empty stdout: sha256sum/awk produced no output (tool misbehavior).
_lib_hash_diff_text() {
  local text="$1"
  local digest
  digest=$(printf '%s' "$text" | sha256sum | awk '{print $1}')
  [ -n "$digest" ] || return 1
  printf '%s' "$digest"
}

# _lib_cumulative_diff_hash REPO_ROOT PR_DIFF_SCRIPT
# Hashes PR_DIFF_SCRIPT's (pr-diff-against-base.sh's) stdout for REPO_ROOT --
# the completion-marker value for the `cumulative-review` kind. Only `status`
# calls this helper; `write cumulative-review` hashes a recorded subject via
# _lib_hash_diff_text instead (design-decisions.md §50).
#
# Two-outcome contract, matching _lib_active_plan_hash:
#   - exit 0, non-empty stdout: the sha256 hex digest of the diff.
#   - exit 1, empty stdout: PR_DIFF_SCRIPT failed, produced no output, or was
#     killed by the cap. Callers MUST fail closed -- status degrades to
#     reporting the marker absent rather than erroring the whole report.
_lib_cumulative_diff_hash() {
  local repo_root="$1" pr_diff_script="$2"
  local diff_output
  # 15s, not the shared 5s _lib_capped default: this is the only _lib_capped
  # call site that's network-bound, since PR_DIFF_SCRIPT shells out to
  # `gh pr view`.
  diff_output=$(cd "$repo_root" && _lib_capped_for 15 "$pr_diff_script" 2>/dev/null) || return 1
  [ -n "$diff_output" ] || return 1
  _lib_hash_diff_text "$diff_output"
}

# _lib_default_branch_from_origin_head REPO_ROOT
# Resolve REPO_ROOT's default branch from the local symbolic ref
# refs/remotes/origin/HEAD alone, verifying the target actually resolves to
# a commit before returning it.
# Local refs only, never the network -- callers use the result as a local ref immediately.
# `--quiet symbolic-ref`, not `rev-parse --abbrev-ref origin/HEAD`: the
# latter never returns empty, which would mask an unset origin/HEAD.
# Two-outcome contract (same failure shape as _lib_repo_root):
#   - exit 0, non-empty stdout: the resolved default branch name.
#   - exit 1, empty stdout: origin/HEAD is unset, dangling, or the call
#     timed out. Callers decide their own fallback.
_lib_default_branch_from_origin_head() {
  local repo_root="$1"
  local ref default_branch
  ref=$(_lib_capped git -C "$repo_root" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null)
  [ -n "$ref" ] || return 1
  _lib_capped git -C "$repo_root" rev-parse --verify --quiet "$ref" >/dev/null 2>&1 || return 1
  default_branch="${ref#refs/remotes/origin/}"
  [ -n "$default_branch" ] || return 1
  printf '%s' "$default_branch"
}

# _lib_default_branch_or_guess REPO_ROOT
# Resolve REPO_ROOT's default branch: first via
# _lib_default_branch_from_origin_head, then, on its failure, by falling
# through to probing conventional candidate names (main, master, develop)
# against existing origin/<candidate> refs. Designed to be shared by every
# caller whose wrong-answer consequence is a diff, a message, or a withheld
# gate rather than the base it fetches, rebases onto, or deletes against
# (docs/design-decisions.md §54).
# Local refs only, never the network -- callers use the result as a local ref immediately.
# Candidate order is a prior, not a guarantee: with origin/HEAD unset and
# several candidate refs present, the first match wins even when it is stale.
# Two-outcome contract (same failure shape as _lib_repo_root):
#   - exit 0, non-empty stdout: the resolved default branch name.
#   - exit 1, empty stdout: neither the symbolic ref nor any candidate
#     resolved. Callers decide their own fallback -- this helper does not
#     pick a fail posture (same call-site contract as
#     _lib_command_invokes_git_subcmd).
_lib_default_branch_or_guess() {
  local repo_root="$1"
  local default_branch candidate
  if default_branch=$(_lib_default_branch_from_origin_head "$repo_root"); then
    printf '%s' "$default_branch"
    return 0
  fi
  for candidate in main master develop; do
    if _lib_capped git -C "$repo_root" rev-parse --verify "origin/$candidate" >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

# _lib_staged_diff_state REPO_ROOT [PATHSPEC...]
# Classifies the staged diff for REPO_ROOT (optionally scoped to PATHSPEC) as
# "content", "empty", or "unknown" on stdout, always exiting 0.
#
# Probes before hashing, unlike marker.sh's _hash_staged_diff -- needed only
# when a caller must hash an empty diff through as a legitimate recorded
# value rather than treat it as nothing-to-do. Its one remaining caller,
# _lib_reviewer_round_state_value below, is exactly that case: an empty
# staged diff is itself a valid round state to record.
#
# Callers branch on the printed word rather than a bare 0/1 exit status, so
# "unknown" can never be silently read as "no changes".
_lib_staged_diff_state() {
  local repo_root="$1"; shift
  # Fail closed on an empty REPO_ROOT rather than letting `git -C ""` resolve
  # relative to the caller's cwd -- every current call site already validates
  # REPO_ROOT, but this is a shared primitive future callers may not.
  if [ -z "$repo_root" ]; then
    printf 'unknown'
    return 0
  fi
  local probe_status=0
  # Capped via the shared 5s _lib_capped -- a local git operation, not the
  # 15s network-bound exception _lib_cumulative_diff_hash documents for
  # itself above.
  # Captured as `probe_status=0; ... || probe_status=$?`, never a bare `$?`.
  # marker.sh sources this file under `set -u` only. Other callers source it
  # under `set -euo pipefail`.
  # Under the latter, a bare `probe_status=$?` after a nonzero-returning
  # statement would trip errexit before the assignment ever ran.
  _lib_capped git -C "$repo_root" diff --cached --quiet -- "$@" || probe_status=$?
  # Exit 0 (no differences) -> "empty"; exit 1 (differences) -> "content";
  # anything else, including a _lib_capped kill, -> "unknown".
  # A no-op GIT_EXTERNAL_DIFF/diff.external driver makes `--quiet` report
  # "content" for a hashed diff that is actually zero bytes -- pre-existing
  # on every `git diff --cached | sha256sum` site, not introduced here.
  case "$probe_status" in
    0) printf 'empty' ;;
    1) printf 'content' ;;
    *) printf 'unknown' ;;
  esac
}

# _lib_git_inprogress_state REPO_ROOT [GITDIR]
# Detects which git operation, if any, is paused mid-way in REPO_ROOT:
# rebase, merge, cherry-pick, or revert. Precedence is checked in that
# order, matching git-state-safety/SKILL.md's own rule-of-thumb ordering.
# GITDIR is optional: when the caller has already resolved
# `--absolute-git-dir` for its own purposes (_lib_gate_diff_base does), pass
# it here to skip this function's own resolution and avoid spawning git
# twice for the same answer. Omit it to have this function resolve gitdir
# itself.
# Detection recipe per state (`git rev-parse --absolute-git-dir` resolved
# once, then plain file tests against it -- not `git rev-parse --git-path`
# per state, which would spawn git four times for the same answer):
#   rebase       rebase-merge/ or rebase-apply/ dir exists
#   merge        MERGE_HEAD exists
#   cherry-pick  CHERRY_PICK_HEAD exists
#   revert       REVERT_HEAD exists
# --absolute-git-dir (not --git-dir) resolves a linked worktree's own
# per-worktree gitdir rather than the shared main one, which is where these
# five markers actually live.
#
# Tri-state via exit status, the same 0/1/2 contract
# _lib_command_invokes_git_subcmd already establishes in this file:
#   - exit 0, stdout = one of rebase/merge/cherry-pick/revert: that state is
#     in progress.
#   - exit 1, stdout empty: no in-progress state.
#   - exit 2, stdout empty: the gitdir could not be resolved (or, when
#     GITDIR was passed in, it was empty) -- the underlying _lib_capped call
#     timed out, was killed, or git itself was missing. Callers MUST NOT
#     treat this as "no state" -- see _lib_gate_diff_base below, whose
#     safety property depends on this status never being read as a green
#     light.
_lib_git_inprogress_state() {
  [ "$#" -eq 1 ] || [ "$#" -eq 2 ] || return 2
  local repo_root="$1"
  local gitdir="${2:-}"
  if [ -z "$gitdir" ]; then
    if ! gitdir=$(_lib_capped git -C "$repo_root" rev-parse --absolute-git-dir 2>/dev/null); then
      return 2
    fi
  fi
  [ -n "$gitdir" ] || return 2
  if [ -d "$gitdir/rebase-merge" ] || [ -d "$gitdir/rebase-apply" ]; then
    printf '%s' rebase
    return 0
  fi
  if [ -f "$gitdir/MERGE_HEAD" ]; then
    printf '%s' merge
    return 0
  fi
  if [ -f "$gitdir/CHERRY_PICK_HEAD" ]; then
    printf '%s' cherry-pick
    return 0
  fi
  if [ -f "$gitdir/REVERT_HEAD" ]; then
    printf '%s' revert
    return 0
  fi
  return 1
}

# _lib_gate_diff_base REPO_ROOT
# Prints the tree-ish a commit-time gate should diff its staged content
# against, in place of the index's implicit HEAD base -- so a gate that
# hashes or scans `git diff --cached "$(_lib_gate_diff_base "$repo")"` sees
# only content novel to the commit being made, even mid-merge.
#
# Outside an in-progress state (the overwhelming common case) this prints
# nothing, and the call site issues plain `git diff --cached` with no base
# argument. `git diff --cached HEAD` is deliberately not used as that "no
# override" value: it fails on an unborn branch (no commits yet), where the
# bare form succeeds.
#
# During a trusted in-progress state (see the anchor check below), this
# computes the tree git's own automatic merge/rebase/cherry-pick/revert
# machinery would have produced, via `git merge-tree --write-tree`. See
# docs/design-decisions/merge-tree-base-recipe-for-gate-diff-base.md for the
# per-state command table and the anchor-admissibility argument this
# function implements.
#
# Trust check, required before any of the above runs. Presence of
# MERGE_HEAD/CHERRY_PICK_HEAD/REVERT_HEAD/rebase-merge proves nothing by
# itself: each is a plain gitdir file an ungated `git update-ref` or
# `printf` can fabricate to point at arbitrary content. The state's own OID
# (REBASE_HEAD/MERGE_HEAD/CHERRY_PICK_HEAD/REVERT_HEAD's content,
# unmodified) must therefore first reach one of two anchors via `git
# merge-base --is-ancestor`:
#   - the resolved default remote-tracking branch (origin/<default>, via
#     _lib_default_branch_or_guess) -- content already reviewed upstream.
#   - HEAD -- content already committed in this repo, which required
#     passing this same gate at its own commit time.
# Reaching neither anchor falls back to the empty base, over-scoping rather
# than smuggling content past the hash, for any of:
#   - an unrelated cherry-pick source.
#   - a garbage or dangling OID.
#   - an octopus MERGE_HEAD (multi-line, never a valid single revision).
#   - a `--rebase-merges` replay of a merge commit, where REBASE_HEAD^ would
#     silently resolve to the wrong parent.
#
# The trust-anchor check above is also the reason state_oid must be
# shape-validated (a bare 40- or 64-hex-char string, git's two supported OID
# lengths) before this function passes it as a positional argument to any
# `git merge-base`/`merge-tree` invocation: state_oid comes from a plain
# gitdir file, not from git itself, so nothing upstream of this function
# guarantees it isn't attacker-controlled option-injection content (e.g. a
# leading `--upload-pack=`) rather than a real OID.
#
# `merge-tree --write-tree`'s stdout is validated the same way regardless
# of cause: take its first line (parameter expansion, matching
# _lib_extract_git_subcmd's idiom, not a `head -1` fork) and require `git
# rev-parse --verify --quiet "<line>^{tree}"` to succeed. A git older than
# 2.38 rejecting `--write-tree` outright, or one accepting `--write-tree`
# but rejecting `--merge-base=`, both fail this validation and fall back to
# the empty base -- no version literal appears anywhere in this function.
#
# Tri-state via exit status, same contract as _lib_git_inprogress_state:
#   - exit 0, stdout = a tree OID: an in-progress state was detected, its
#     OID reached a trusted anchor, and the reference tree was computed.
#   - exit 1, stdout empty: no in-progress state, or one whose OID reached
#     no anchor, or a topology/git-version this design computes no base
#     for. This is the correct answer, not a degraded one.
#   - exit 2, stdout empty: undetermined -- a capped git call inside
#     detection or tree computation timed out, was killed, or its binary
#     was missing (distinguished from an ordinary git failure by exit codes
#     124/125/126/127/137, `timeout(1)`'s own documented set for "the
#     wrapped command did not run to completion normally", as opposed to
#     git's own exit codes for an ordinary negative answer).
# "exit 2 => empty stdout" is load-bearing: every caller consumes stdout
# unconditionally regardless of exit status, so a partial or candidate OID
# escaping on a kill path would otherwise be consumed as a real base and
# would narrow an authorization hash on nobody's authority. No caller
# changes its allow/deny decision on status 2 alone -- it denies (or
# allows, per its own existing fail posture) on the same empty-base
# over-gating it already applies to status 1, and may additionally name the
# undetermined base in its own deny message.
#
# The trust-anchor check (`git merge-base --is-ancestor`) is exempt from
# the above status-2 distinction: any non-zero exit from it -- including
# one driven by the same cap -- is treated identically as "not trusted",
# per `git merge-base --is-ancestor`'s own documented contract (0 ancestor,
# non-zero otherwise, including an unresolvable OID). Collapsing that
# call's failure modes is safe because its only effect either way is the
# conservative empty-base fallback that status 1 already produces.
_lib_gate_diff_base() {
  [ "$#" -eq 1 ] || return 2
  local repo_root="$1"

  local gitdir
  gitdir=$(_lib_capped git -C "$repo_root" rev-parse --absolute-git-dir 2>/dev/null) || return 2
  [ -n "$gitdir" ] || return 2

  local state state_status
  state=$(_lib_git_inprogress_state "$repo_root" "$gitdir")
  state_status=$?
  [ "$state_status" -eq 2 ] && return 2
  [ "$state_status" -eq 1 ] && return 1

  local ref_file
  case "$state" in
    rebase) ref_file="REBASE_HEAD" ;;
    merge) ref_file="MERGE_HEAD" ;;
    cherry-pick) ref_file="CHERRY_PICK_HEAD" ;;
    revert) ref_file="REVERT_HEAD" ;;
    *) return 2 ;;
  esac
  [ -f "$gitdir/$ref_file" ] || return 1
  local state_oid
  state_oid=$(_lib_capped cat "$gitdir/$ref_file" 2>/dev/null)
  [ -n "$state_oid" ] || return 1
  [[ "$state_oid" =~ ^[0-9a-f]{40}$ ]] || [[ "$state_oid" =~ ^[0-9a-f]{64}$ ]] || return 1

  local default_branch anchor_reached=1
  default_branch=$(_lib_default_branch_or_guess "$repo_root")
  if [ -n "$default_branch" ] \
    && _lib_capped git -C "$repo_root" merge-base --is-ancestor "$state_oid" "origin/$default_branch" >/dev/null 2>&1
  then
    anchor_reached=0
  elif _lib_capped git -C "$repo_root" merge-base --is-ancestor "$state_oid" HEAD >/dev/null 2>&1; then
    anchor_reached=0
  fi
  [ "$anchor_reached" -eq 0 ] || return 1

  local tree_out tree_status
  case "$state" in
    merge)
      tree_out=$(_lib_capped git -C "$repo_root" merge-tree --write-tree HEAD "$state_oid" 2>/dev/null)
      tree_status=$?
      ;;
    rebase | cherry-pick)
      tree_out=$(_lib_capped git -C "$repo_root" merge-tree --write-tree "--merge-base=${state_oid}^" HEAD "$state_oid" 2>/dev/null)
      tree_status=$?
      ;;
    revert)
      tree_out=$(_lib_capped git -C "$repo_root" merge-tree --write-tree "--merge-base=${state_oid}" HEAD "${state_oid}^" 2>/dev/null)
      tree_status=$?
      ;;
  esac
  case "$tree_status" in
    124 | 125 | 126 | 127 | 137) return 2 ;;
  esac
  # tree_status itself is not the validation signal: `merge-tree
  # --write-tree` exits 1 (not 0) whenever the merge it computed conflicts
  # -- the expected, common case here, since resolving that conflict is the
  # whole reason this function exists -- while still writing a valid tree
  # (with embedded conflict markers) on its first stdout line. Whether that
  # first line resolves to a real tree is the actual check, below.
  local tree_oid="${tree_out%%$'\n'*}"
  [ -n "$tree_oid" ] || return 1

  _lib_capped git -C "$repo_root" rev-parse --verify --quiet "${tree_oid}^{tree}" >/dev/null 2>&1
  local verify_status=$?
  case "$verify_status" in
    124 | 125 | 126 | 127 | 137) return 2 ;;
  esac
  [ "$verify_status" -eq 0 ] || return 1

  printf '%s' "$tree_oid"
}

# _lib_staged_diff_hash REPO_ROOT BASE [PATHSPEC...]
# Shared body behind every content-addressed marker preimage and round-state
# value in this file and in scripts/marker.sh: sha256 of `git diff --cached`,
# restricted to PATHSPEC when given. BASE is already resolved by the caller
# (typically via _lib_gate_diff_base) -- this function has no way to
# recompute one, which is deliberate: the write side (marker.sh) and every
# read side must hash byte-identical input, and threading an
# already-resolved value through the signature makes that structural rather
# than a discipline each of the seven call sites has to remember.
# BASE empty means no override: this runs plain `git diff --cached
# [-- PATHSPEC...]`. BASE non-empty means `git diff --cached "$BASE"
# [-- PATHSPEC...]`.
# Two-outcome contract: exit 0 with the hex digest on stdout, or exit 1 with
# empty stdout when git or sha256sum failed or was killed. sha256 of even an
# empty diff is a non-empty 64-hex digest, so stdout alone can't distinguish
# "nothing staged" from a failed pipeline -- what actually distinguishes them
# is the capped `git diff` call's own exit status, captured via
# `${PIPESTATUS[0]}` in the same command substitution immediately after the
# pipeline runs (before any later command in that subshell can overwrite
# it): any nonzero status there -- an ordinary git error or a cap kill
# (124/125/126/127/137) alike -- fails this closed regardless of whether
# sha256sum/awk still produced output downstream. Callers must fail closed
# on exit 1, the same posture _lib_active_plan_hash and
# _lib_reviewer_round_state_value already document for this class of
# failure.
# Fails closed on an empty REPO_ROOT rather than letting `git -C ""` resolve
# relative to the caller's cwd -- every current call site already validates
# REPO_ROOT, but this is a shared primitive future callers may not.
_lib_staged_diff_hash() {
  [ "$#" -ge 2 ] || return 1
  local repo_root="$1" base="$2"
  [ -n "$repo_root" ] || return 1
  shift 2
  local -a diff_args=(-C "$repo_root" diff --cached)
  [ -n "$base" ] && diff_args+=("$base")
  [ "$#" -gt 0 ] && diff_args+=(-- "$@")
  local out git_status digest
  out=$(
    _lib_capped git "${diff_args[@]}" 2>/dev/null | sha256sum | awk '{print $1}'
    printf '\n%s' "${PIPESTATUS[0]}"
  )
  git_status="${out##*$'\n'}"
  digest="${out%$'\n'*}"
  digest="${digest%$'\n'}"
  [ "$git_status" -eq 0 ] || return 1
  [ -n "$digest" ] || return 1
  printf '%s' "$digest"
}

# _lib_code_review_empty_base_sentinel BASE
# The literal string _lib_code_review_marker_value hashes when BASE is
# non-empty and the base-relative staged diff is itself empty (see that
# function's own docstring below). require-code-review.sh's and marker.sh's
# read sites reconstruct the same value, so all three call this rather than
# inlining the literal.
_lib_code_review_empty_base_sentinel() {
  printf 'code-review-empty-base:%s' "$1"
}

# _lib_code_review_marker_value REPO_ROOT BASE
# The code-review completion-marker preimage. Ordinarily identical to
# _lib_staged_diff_hash REPO_ROOT BASE's own output. When BASE is non-empty
# (a trusted in-progress merge/rebase/cherry-pick/revert) and the
# base-relative staged diff is itself empty, the value binds to BASE's own
# identity instead of falling through to sha256(""). sha256("") is a fixed
# value independent of which base produced it, so a marker obtained during
# one trusted operation's degenerate empty-diff case would otherwise
# validate a different, forged base landing on the same empty result.
# Shared by marker.sh's `write code-review`/`status` arms and
# require-code-review.sh's read side so a value computed separately at each
# call site cannot drift out of agreement -- the same discipline
# _lib_staged_diff_hash's own docstring describes for BASE threading
# generally.
# Two-outcome contract, matching _lib_staged_diff_hash: exit 0 with the hex
# digest on stdout, exit 1 with empty stdout when the base-relative diff
# itself could not be computed (git failure or cap kill).
_lib_code_review_marker_value() {
  [ "$#" -eq 2 ] || return 1
  local repo_root="$1" base="$2"
  if [ -n "$base" ]; then
    local relative_diff diff_status
    relative_diff=$(_lib_capped git -C "$repo_root" diff --cached "$base" 2>/dev/null)
    diff_status=$?
    [ "$diff_status" -eq 0 ] || return 1
    if [ -z "$relative_diff" ]; then
      _lib_hash_diff_text "$(_lib_code_review_empty_base_sentinel "$base")"
      return $?
    fi
  fi
  _lib_staged_diff_hash "$repo_root" "$base"
}

# _lib_is_repo_plan_file REPO_ROOT ABS_PATH
# Both arguments must already be _lib_realpath_m-normalized by the caller --
# the same precondition the agent-reviews/ check in require-plan-review.sh
# relies on. Returns 0 iff ABS_PATH is a direct child of
# REPO_ROOT/.claude/plans whose name ends in .md or .txt.
# The suffix set must stay identical to plan_pathspecs above, or this
# exemption and the hash it exempts would describe two different file sets.
# The depth check is a separate `dirname` comparison rather than a combined
# glob because bash's `case` glob matches `/` where git's `:(glob)` does not
# -- `case ".claude/plans/sub/x.md" in .claude/plans/*.md) ...` matches in
# bash, which would wrongly exempt a nested path the hash never covers.
# Deliberately fork-free and git-free, unlike _lib_active_plan_hash, because
# this runs on every gated tool call.
_lib_is_repo_plan_file() {
  # Arity guard, matching _lib_active_bypass_marker_live's shape: `[ ]`
  # rather than `(( ))`, for the same set -e-safety reason documented at
  # that function's arity guard.
  [ "$#" -eq 2 ] || return 1
  local repo_root="$1" abs_path="$2"
  [ "$(dirname -- "$abs_path")" = "$repo_root/.claude/plans" ] || return 1
  case "$abs_path" in
    *.md|*.txt) return 0 ;;
    *) return 1 ;;
  esac
}

# Decide whether a shell fragment actually invokes `git`, not just mentions it
# as a substring of a path or URL. Walks whitespace-separated words; returns
# success iff any word equals `git` or ends in `/git`. Env-var prefixes
# (GIT_DIR=... git ...), wrapper commands (eval, sudo, xargs), and `git` as a
# non-first word are all handled by scanning every word, not just the first.
#
# Rejects: `ls .github/`, `cat .gitignore`, `grep github.com`, `./git-foo`.
# Accepts: `git log`, `sudo git commit`, `GIT_DIR=x git push`, `/usr/bin/git status`.
#
# Caller contract: word comparisons here are quote-blind by design (bash
# word-splitting does not remove quote characters, so `"git" log` fails
# every comparison below). The caller is responsible for passing a fragment
# already quote-stripped via _lib_strip_shell_quotes if it needs to match a
# quoted invocation.
_lib_fragment_invokes_git() {
  local fragment="$1"
  local saved_opts=$-
  set -f
  local found=false word
  for word in $fragment; do
    if [[ "$word" == "git" || "$word" == */git ]]; then
      found=true
      break
    fi
  done
  if [[ "$saved_opts" != *f* ]]; then set +f; fi
  $found
}

# Print a git fragment's argv from the subcommand onward, one word per line.
# Walks words to the `git` command word (same scan as _lib_fragment_invokes_git).
# Then consumes git's global flags and the values the value-taking ones absorb.
# The first line is the subcommand; each later line is one of that
# subcommand's own arguments.
# Prints nothing when no subcommand word follows the flags (`git --version`).
# The `--git-dir=<path>` form carries its value in the same word, so the
# catch-all `-*` arm skips it rather than the value-consuming arm.
# Globbing is disabled so a wildcard in the command text is not expanded.
#
# Caller contract: word comparisons here are quote-blind by design, same as
# _lib_fragment_invokes_git — the caller is responsible for passing a
# fragment already quote-stripped via _lib_strip_shell_quotes.
_lib_git_argv_from_subcmd() {
  local fragment="$1"
  local saved_opts=$-
  set -f
  local past_git=false skip_next=false past_subcmd=false word
  for word in $fragment; do
    if $past_subcmd; then
      printf '%s\n' "$word"
      continue
    fi
    if ! $past_git; then
      if [[ "$word" == "git" || "$word" == */git ]]; then
        past_git=true
      fi
      continue
    fi
    if $skip_next; then skip_next=false; continue; fi
    case "$word" in
      # Every git 2.43 global flag taking a separate-word value, per `git
      # help --all`'s global-options list.
      # A future git version adding another one needs this list updated by
      # hand.
      -C|-c|--git-dir|--work-tree|--namespace|--super-prefix|--config-env)
        skip_next=true ;;
      -*) ;;
      *) printf '%s\n' "$word"; past_subcmd=true ;;
    esac
  done
  if [[ "$saved_opts" != *f* ]]; then set +f; fi
}

# Extract the git subcommand from a fragment like "git -C path push -u origin"
# or "GIT_DIR=x git push". Strips trailing non-alnum characters so that `push)`
# from paren-group splitting yields `push`.
_lib_extract_git_subcmd() {
  local subcmd
  subcmd=$(_lib_git_argv_from_subcmd "$1")
  subcmd="${subcmd%%$'\n'*}"
  printf '%s' "${subcmd%%[^a-zA-Z0-9_-]*}"
}

# Print the arguments a git fragment passes to its subcommand, one per line, so
# `git -C /wt push --tags origin` yields `--tags` and `origin`.
# Words are printed verbatim, so an unstripped trailing character (`feature)`)
# fails a caller's exact-match allowlist rather than passing it.
_lib_extract_git_subcmd_args() {
  local argv
  argv=$(_lib_git_argv_from_subcmd "$1")
  [[ "$argv" == *$'\n'* ]] || return 0
  printf '%s\n' "${argv#*$'\n'}"
}

# Decide whether a `git commit` fragment carries `-a`/`--all`, a `--`
# pathspec separator, or a bare pathspec argument — any of which commits
# working-tree content that is not in the index when a PreToolUse hook's
# `git diff --cached` snapshot runs. Shared by deny-pii-in-commits.sh
# (decides whether it also needs to scan `git diff HEAD`) and
# deny-invisible-commit-content.sh (denies the commit outright).
# Usage: _lib_commit_fragment_has_worktree_target "git commit -am wip"
_lib_commit_fragment_has_worktree_target() {
  # Each stage is captured into a variable rather than run as one pipeline.
  # The awk exits as soon as it finds a worktree-target token; in a single
  # pipeline that closes the pipe on a still-running `xargs -n1`, and under
  # `set -o pipefail` the resulting SIGPIPE surfaces as a non-zero pipeline
  # status — spuriously reporting "no target". Testing the captured awk
  # output isolates the verdict from that SIGPIPE false failure, but
  # xargs's and awk's own exit codes are still checked below so a genuine
  # tool failure fails closed rather than reading as "no target".
  local fragment_tokens verdict xargs_exit awk_exit
  fragment_tokens=$(printf '%s\n' "$1" | xargs -n1 2>/dev/null)
  xargs_exit=$?
  verdict=$(awk '
    BEGIN { past = 0; skip = 0 }
    {
      if (!past) { if ($0 == "commit") past = 1; next }
      if (skip) { skip = 0; next }
      if ($0 == "--") { print "Y"; exit }
      if ($0 ~ /^--/) {
        if ($0 == "--all") { print "Y"; exit }
        # Long options that consume a separate-token value.
        if ($0 ~ /^(--message|--file|--reuse-message|--reedit-message|--template|--author|--date|--cleanup|--fixup|--squash|--trailer|--pathspec-from-file)$/) skip = 1
        next
      }
      if ($0 ~ /^-./) {
        # Short-flag bundle. `a` anywhere means --all; a bundle ending in a
        # value-taking letter consumes the next token.
        if ($0 ~ /a/) { print "Y"; exit }
        if ($0 ~ /[mFCct]$/) skip = 1
        next
      }
      # A bare, non-consumed token after `commit` is a pathspec.
      print "Y"; exit
    }
  ' <<< "$fragment_tokens")
  awk_exit=$?
  # A missing, killed, or otherwise-failing xargs/awk must not read as "no
  # worktree target" -- treat it the same as a target found, the safe
  # direction for both callers (deny outright in
  # deny-invisible-commit-content.sh; also scan `git diff HEAD` in
  # deny-pii-in-commits.sh).
  if [ "$xargs_exit" -ne 0 ] || [ "$awk_exit" -ne 0 ]; then
    return 0
  fi
  [ "$verdict" = "Y" ]
}

# Split a shell command string into fragments on shell operators (;, &&, ||, |,
# $(...), backticks). Each fragment may invoke a distinct command. Leading/
# trailing parentheses are stripped from each fragment so that `(cd /x; git push)`
# yields `git push` as a clean fragment rather than `git push)`.
# Call-site contract (load-bearing): the underlying sed pipeline can fail
# (missing, killed, or erroring), and no caller runs under `set -e`, so
# every call site must capture and check the exit status immediately and
# fail closed on non-zero, rather than proceeding with a silently empty
# fragment list — see deny-invisible-commit-content.sh's SPLIT_EXIT
# computation for the pattern.
_lib_split_fragments() {
  printf '%s' "$1" \
    | sed -E 's/;/\n/g; s/&&/\n/g; s/\|\|/\n/g; s/\|/\n/g; s/\$\(/\n/g; s/`/\n/g' \
    | sed -E 's/^[[:space:]]*\(//; s/\)[[:space:]]*$//'
}

# Resolve a fragment's effective command word: skip leading env-var
# assignments and a closed set of runner/wrapper words (plus each runner's
# connector sub-token), then return the first remaining word. `npx prettier`,
# `python -m black`, `sudo env X=1 isort` resolve to prettier/black/isort.
#
# WHY command-word, not any-word (unlike the shared _lib_fragment_invokes_git
# used for git): a tool name like black/sed/mv is a common word that can
# legitimately appear as an argument in a read-only command (`grep black
# file`), so an any-word scan would false-deny it; "git" doesn't have that
# problem.
#
# Shared by deny-reviewer-tree-mutation.sh and deny-repo-relocation.sh.
#
# Caller contract: word comparisons here are quote-blind by design, same as
# _lib_fragment_invokes_git — the caller is responsible for passing a
# fragment already quote-stripped via _lib_strip_shell_quotes.
_lib_fragment_command_word() {
  local fragment="$1"
  local saved_opts=$-
  set -f
  local word cmd="" expect_after_runner=false
  for word in $fragment; do
    # Leading env-var assignment (VAR=val); precedes the command. A flag like
    # --write=false starts with '-' and does not match, so it is never eaten.
    if [[ "$word" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      continue
    fi
    if $expect_after_runner; then
      # A runner's own connector sub-token or flag (e.g. `poetry run`,
      # `python -m`, `npx --yes`) — skip it; the command is still ahead.
      case "$word" in
        run|exec|dlx|tool|-*) continue ;;
      esac
      expect_after_runner=false
    fi
    # Matched against the basename, not the full path, so an absolute or
    # relative runner path (e.g. /usr/local/bin/pnpm) still resolves like
    # the bare name.
    case "${word##*/}" in
      sudo|doas|env|command|time|nice|xargs|npx|pnpm|yarn|bunx|bun|pipx|uvx|uv|poetry|pipenv|rye|hatch|pdm|python|python2|python3|node|deno)
        expect_after_runner=true
        continue ;;
    esac
    cmd="$word"
    break
  done
  if [[ "$saved_opts" != *f* ]]; then set +f; fi
  printf '%s' "$cmd"
}

# True iff the fragment's command word equals $2, or ends in "/$2" (an
# absolute/relative path invocation, e.g. /usr/bin/terraform).
#
# Caller contract: inherits _lib_fragment_command_word's quote-blindness —
# the caller is responsible for passing a fragment already quote-stripped
# via _lib_strip_shell_quotes.
_lib_fragment_invokes_tool() {
  local fragment="$1" tool="$2"
  local cmd
  cmd=$(_lib_fragment_command_word "$fragment")
  [[ -n "$cmd" && ( "$cmd" == "$tool" || "$cmd" == */"$tool" ) ]]
}

# True iff $2 appears in $1 as a standalone whitespace-delimited token — for
# exact-flag checks (e.g. --fix, --remove-source-files) where a real value
# never appends more non-space characters.
#
# Caller contract: this is a regex boundary match against $1 verbatim, no
# word-walk — but it is still quote-blind (a quoted `"--fix"` never matches
# the bare $2). The caller is responsible for passing a fragment already
# quote-stripped via _lib_strip_shell_quotes.
_lib_fragment_has_token() {
  local fragment="$1" token="$2"
  [[ "$fragment" =~ (^|[[:space:]])${token}([[:space:]]|$) ]]
}

# _lib_command_invokes_git_subcmd COMMAND SUBCMD
# Tri-state via exit status: 0 if any fragment of COMMAND invokes `git
# SUBCMD`, 1 if no fragment does, 2 if a fork this needed (the quote-strip
# or the fragment-split) failed and the answer could not be determined.
# Composes _lib_strip_shell_quotes, _lib_split_fragments,
# _lib_fragment_invokes_git, and _lib_extract_git_subcmd, so the eight gate
# hooks share one fragment-aware matcher instead of each hand-copying a raw
# regex over unstripped $COMMAND (which a quote-split defeats, e.g. `"git"
# commit`).
#
# Call-site contract (load-bearing): never picks a fail posture itself —
# every caller must check for status 2 and decide allow-or-deny for its own
# gate, the same discipline _lib_split_fragments's own call-site contract
# already requires. Six checked-fail-closed hooks deny on status 2; the two
# correctly-fail-open hooks (guard-settings-session-keys.sh,
# require-stow-reminder.sh) treat anything other than 0 as "no match" and
# stay silent about the distinction, matching their own documented posture.
_lib_command_invokes_git_subcmd() {
  [ "$#" -eq 2 ] || return 2
  local command="$1" subcmd="$2"
  local command_unquoted fragments fragment
  command_unquoted=$(_lib_strip_shell_quotes "$command") || return 2
  fragments=$(_lib_split_fragments "$command_unquoted") || return 2
  while IFS= read -r fragment; do
    [ -z "$fragment" ] && continue
    _lib_fragment_invokes_git "$fragment" || continue
    if [ "$(_lib_extract_git_subcmd "$fragment")" = "$subcmd" ]; then
      return 0
    fi
  done <<< "$fragments"
  return 1
}

# Shape sets shared between _lib_command_concludes_commit and
# _lib_command_concludes_marker_gated_commit below, and between the two
# public wrappers, via their one shared private matcher -- so the decision
# of which verbs' `--continue` form concludes a commit is written once, not
# duplicated across two independently-maintained regexes.
_LIB_CONTINUE_VERBS_ALL="merge rebase cherry-pick revert"
_LIB_CONTINUE_VERBS_MARKER_GATED="merge cherry-pick revert"

# _lib_command_concludes_commit_shape COMMAND VERBS
# Private. True iff any fragment of COMMAND is `git commit` (in any form
# _lib_command_invokes_git_subcmd already recognizes), or `git <verb>
# --continue` for a verb in the whitespace-separated VERBS list. A clean
# merge, rebase, or cherry-pick creates its commit inside the initiating
# command with no separate `git commit` call, so this is the only PreToolUse
# shape a conflict-resolution commit takes.
# `--continue` must be one of the matched verb's own arguments, not merely
# present somewhere else in COMMAND -- `_lib_extract_git_subcmd_args` is
# checked per matching fragment rather than grepping the whole command
# string, so `git rebase origin/main && echo --continue` does not match.
# Tri-state via exit status, same 0/1/2 contract as
# _lib_command_invokes_git_subcmd, which this delegates the `commit` check
# to directly.
_lib_command_concludes_commit_shape() {
  [ "$#" -eq 2 ] || return 2
  local command="$1" verbs="$2"
  local command_unquoted fragments fragment
  command_unquoted=$(_lib_strip_shell_quotes "$command") || return 2
  fragments=$(_lib_split_fragments "$command_unquoted") || return 2
  local subcmd verb is_continue_verb arg
  while IFS= read -r fragment; do
    [ -z "$fragment" ] && continue
    _lib_fragment_invokes_git "$fragment" || continue
    subcmd=$(_lib_extract_git_subcmd "$fragment")
    if [ "$subcmd" = commit ]; then
      return 0
    fi
    is_continue_verb=false
    for verb in $verbs; do
      if [ "$subcmd" = "$verb" ]; then
        is_continue_verb=true
        break
      fi
    done
    $is_continue_verb || continue
    while IFS= read -r arg; do
      if [ "$arg" = --continue ]; then
        return 0
      fi
    done < <(_lib_extract_git_subcmd_args "$fragment")
  done <<< "$fragments"
  return 1
}

# _lib_command_concludes_commit COMMAND
# Tri-state, true for `git commit` and for `git <merge|rebase|cherry-pick|
# revert> --continue` -- the full set of PreToolUse-visible shapes that
# conclude one of those four operations with new content. Shared by every
# gate whose recourse on a bad commit is mechanical (unstage a value,
# shorten a file, remove a session key) rather than a review, so narrowing
# this predicate to skip a verb would silently disarm those gates for that
# verb's `--continue` form. See _lib_command_concludes_marker_gated_commit
# below for the narrower sibling used by the two gates whose recourse is a
# review.
_lib_command_concludes_commit() {
  [ "$#" -eq 1 ] || return 2
  _lib_command_concludes_commit_shape "$1" "$_LIB_CONTINUE_VERBS_ALL"
}

# _lib_command_concludes_marker_gated_commit COMMAND
# Tri-state, identical to _lib_command_concludes_commit above except that
# `git rebase --continue` does not match. REBASE_HEAD cannot reach a trusted
# anchor in the ordinary case (see _lib_gate_diff_base), so gating a review
# marker on it would mean demanding a full review at every conflicted step
# of a rebase against content that, for the most part, already passed
# review at its own original commit time. The two gates that consume this
# narrower predicate still deny an ordinary `git commit` made mid-rebase
# without `--continue`, and the five gates that consume the broad predicate
# above stay armed on `git rebase --continue` -- this predicate narrows
# review-marker enforcement specifically, not rebase's overall gate
# coverage.
_lib_command_concludes_marker_gated_commit() {
  [ "$#" -eq 1 ] || return 2
  _lib_command_concludes_commit_shape "$1" "$_LIB_CONTINUE_VERBS_MARKER_GATED"
}

# Print a tool fragment's subcommand-word sequence, one word per line, after
# walking past the tool's own command word (matched the same way
# _lib_fragment_invokes_tool does: exact, or a path ending in "/$tool") and
# any flags interposed before the subcommand. Mirrors _lib_git_argv_from_subcmd's
# state machine, but the flag-consumption rule is a per-TOOL grammar rather
# than a hardcoded flag list, since only gh's cobra-based resolution is
# modeled below.
#
# Call-site contract: caller passes an already quote-stripped fragment, the
# same discipline as _lib_fragment_command_word and its siblings. The
# function forks nothing, so there is no exit status to check — a caller
# fails closed only on the forks it depends on elsewhere (the quote-strip
# and fragment-split upstream of this call). Two direct consumers today:
# _lib_command_invokes_tool_subcmd below, and deny-private-project-refs.sh's
# fragment_gh_gated_surface (its gh pr / gh issue redaction-gate keying).
#
# gh is cobra-based, and while resolving a subcommand cobra treats any flag
# not registered on the command being traversed as taking the next word as
# its value, so a leaf flag written between a surface word and its
# subcommand is consumed by gh along with its value. Three shapes do not
# consume the next word:
#   - a flag containing "="
#   - a short flag longer than two characters
#   - a bare "--" (which also ends the emitted stream, not just itself)
# Every other TOOL keeps the never-consume default, which can miss a real
# subcommand match but cannot over-consume a positional word, so it cannot
# produce a false match. The one residual is a future gh flag with no value
# placeholder at a gated scope (root/`pr`/`issue`). Present-day instances --
# `-h`/`--help` (registered broadly) and `--version` (root only) -- are
# harmless since they exit before any network call; test_lib.py's
# traversal-scope guard fails if a non-terminating one is ever added.
_lib_tool_argv_from_subcmd() {
  local fragment="$1" tool="$2"
  local saved_opts=$-
  set -f
  local past_tool=false skip_next=false word
  for word in $fragment; do
    if ! $past_tool; then
      if [[ "$word" == "$tool" || "$word" == */"$tool" ]]; then
        past_tool=true
      fi
      continue
    fi
    if $skip_next; then
      skip_next=false
      continue
    fi
    case "$word" in
      -*)
        # gh-only cobra grammar: a flag not registered on the command
        # being traversed consumes the next token as its value, except the
        # three shapes below. Every other TOOL falls straight through to
        # the unconditional "continue" and never sets skip_next.
        if [ "$tool" = gh ]; then
          case "$word" in
            *=*) ;;
            --) break ;;
            --?*) skip_next=true ;;
            -?) skip_next=true ;;
            *) ;;
          esac
        fi
        continue
        ;;
    esac
    printf '%s\n' "$word"
  done
  if [[ "$saved_opts" != *f* ]]; then set +f; fi
}

# _lib_words_start_with WORD... -- PREFIX...
# Boolean exit status: 0 if WORD's first ${#PREFIX[@]} words equal PREFIX
# word-for-word, 1 on the first mismatch or if WORD has fewer words than
# PREFIX. Bounds-checks WORD against PREFIX's length itself, so it is safe to
# call under set -u regardless of what the caller has already checked.
# Internal to _lib_command_invokes_tool_subcmd below, the sole caller. That
# caller flattens both arrays across the call boundary via "$@" plus a "--"
# sentinel. This is the same by-value idiom want_subcmd uses at its own call
# boundary a few lines down.
# Invariant this sentinel depends on: neither WORDS nor PREFIX may contain
# the literal string "--". WORDS-side: _lib_tool_argv_from_subcmd strips
# every "-*"-shaped word, including a bare "--", before it reaches
# got_subcmd. PREFIX-side: want_subcmd is always a hardcoded literal.
# No local -n/declare -n: this repo targets macOS system bash 3.2.
_lib_words_start_with() {
  local -a words=()
  while [ "$#" -gt 0 ] && [ "$1" != -- ]; do
    words+=("$1")
    shift
  done
  shift
  local -a prefix_words=("$@")
  [ "${#words[@]}" -lt "${#prefix_words[@]}" ] && return 1
  local i=0
  while [ "$i" -lt "${#prefix_words[@]}" ]; do
    [ "${words[$i]}" = "${prefix_words[$i]}" ] || return 1
    i=$((i + 1))
  done
  return 0
}

# _lib_command_invokes_tool_subcmd COMMAND TOOL SUBCMD...
# Tri-state via exit status, same 0/1/2 contract as
# _lib_command_invokes_git_subcmd above: 0 if any fragment of COMMAND
# invokes TOOL followed by exactly the given SUBCMD word sequence (e.g. `gh`
# `pr` `merge`), 1 if no fragment does, 2 if a needed fork failed.
#
# WHY command-word, not any-word (unlike the git helper above): a gh-family
# caller (block-gh-pr-merge.sh) must allow `echo "gh pr merge"` through,
# which any-word matching would falsely block. Resolving the fragment's
# command word (via _lib_fragment_invokes_tool, quote-blind by contract)
# delivers that: on the quote-stripped fragment `echo gh pr merge`, the
# command word resolves to `echo`, not `gh`.
# The same resolution also matches a quote-split subcommand word, an
# independent capability: `gh pr "merge"` quote-strips to a bare `merge`
# token, which this word-sequence match catches — see block-gh-pr-merge.sh
# for the regression test.
#
# Call-site contract (load-bearing): never picks a fail posture itself,
# same discipline as _lib_command_invokes_git_subcmd above — every caller
# must check for status 2 and decide allow-or-deny for its own gate.
#
# Accepted cost, not cached: deny-escaped-backticks-in-pr-body.sh and
# require-stow-reminder.sh each call this 2-3 times per hook invocation on
# the same $COMMAND, and every call re-pays the full quote-strip-plus-
# fragment-split baseline from scratch.
_lib_command_invokes_tool_subcmd() {
  [ "$#" -ge 3 ] || return 2
  local command="$1" tool="$2"
  shift 2
  local -a want_subcmd=("$@")
  local command_unquoted fragments fragment
  command_unquoted=$(_lib_strip_shell_quotes "$command") || return 2
  fragments=$(_lib_split_fragments "$command_unquoted") || return 2
  while IFS= read -r fragment; do
    [ -z "$fragment" ] && continue
    _lib_fragment_invokes_tool "$fragment" "$tool" || continue
    local -a got_subcmd=()
    while IFS= read -r word; do
      got_subcmd+=("$word")
    done < <(_lib_tool_argv_from_subcmd "$fragment" "$tool")
    [ "${#got_subcmd[@]}" -lt "${#want_subcmd[@]}" ] && continue
    _lib_words_start_with "${got_subcmd[@]}" -- "${want_subcmd[@]}" && return 0
  done <<< "$fragments"
  return 1
}

# _lib_length_ratchet_exceeded NEW OLD LIMIT
# Pure predicate: exit 0 (true) iff NEW is over LIMIT AND NEW is longer than
# OLD. Reducing an already-over-limit file commit by commit is allowed; new
# bloat is not. Extracted out of _lib_staged_length_gate's loop body so this
# boundary is reachable without a real git repo. NEW, OLD, and LIMIT are
# plain integers regardless of whether the caller derived them from a line
# count or a byte count — both dimensions in _lib_staged_length_gate below
# call this same predicate.
# Current behavior on a non-integer or empty LIMIT: `[ "$new" -gt "$limit" ]`
# exits 2 with "integer expression expected" stderr noise, which every
# caller's `if _lib_length_ratchet_exceeded ...; then deny; fi` treats the
# same as "not exceeded" -- i.e. this fails open. Pre-existing property
# inherited from the inline code before extraction, not a new defect.
_lib_length_ratchet_exceeded() {
  local new="$1" old="$2" limit="$3"
  [ "$new" -gt "$limit" ] && [ "$new" -gt "$old" ]
}

# _lib_staged_length_gate REPO_ROOT PATTERN OVER_LIMIT_MESSAGE [BYTE_LIMIT]
# Shared body behind check-skill-length.sh and check-claude-md-length.sh:
# deny a git commit when a staged file matching PATTERN (a grep -E pattern
# over `git diff --cached --name-only` output) is over its per-file limit
# AND longer than the previously committed version — reducing an
# already-over-limit file commit by commit is allowed; new bloat is not.
#
# Runs only when a Bash command invokes `git commit`, gated behind both
# callers' `if: git commit *` matcher in settings.json — commit-time-cold,
# not per-tool-call like most _lib.sh helpers.
# REPO_ROOT is resolved by the caller from the payload's cwd, the same shape
# require-code-review.sh uses, and threaded through every git call below --
# an ambient-cwd call here would let a session whose shell drifted to a
# different working tree of the same repo compare against the wrong tree.
#
# BYTE_LIMIT is optional and opt-in. When set, the byte check derives
# counts via `git cat-file -s` rather than reusing the `git show` reads
# captured for the line-count check — bash command substitution silently
# drops embedded NUL bytes, which would undercount `wc -c` over that
# captured content. check-skill-length.sh's call site omits it (3-arg
# form, unchanged behavior). check-claude-md-length.sh passes it. Both
# dimensions accumulate into the same $messages/$fail pair below,
# producing one combined emit_deny call at the bottom rather than two.
#
# Callback-by-convention, the same shape _lib_parse_tool_input_or_deny
# already establishes: CALLER MUST define `emit_deny` (as every gate hook
# does, per that function's own contract comment) and `limit_for` (a
# function mapping a repo-root-relative staged path to its line-count
# limit) before calling this. Also relies on the caller having already
# populated $COMMAND and $TOOL_NAME via _lib_parse_tool_input_or_deny, on
# the caller having already exited for a non-Bash TOOL_NAME, and on the
# caller having already confirmed this is a git-commit-shaped command
# (_lib_command_invokes_git_subcmd "$COMMAND" commit, failing closed on an
# undetermined match) before ever resolving REPO_ROOT or calling this --
# the commit-shape check must run before any git subprocess spawns, so it
# lives in the caller, ahead of REPO_ROOT resolution, not in here.
#
# OVER_LIMIT_MESSAGE now carries only the caller's own over-limit sentence:
# the gate-identity prefix comes from _lib_emit_deny's DENY_GATE_LABEL, not
# from this parameter, so the two fail-closed/internal-error denies below
# emit their own body text without re-deriving a prefix from it.
#
# Checked fail-closed on the commit-match check, matching both callers'
# documented fail-closed posture: an undetermined match (sed/tr missing,
# killed, or erroring inside _lib_command_invokes_git_subcmd) denies rather
# than silently skipping the length check.
#
# The git calls below are capped via _lib_capped. rev-parse, diff --cached,
# and the ":$f" show call all degrade to allow (not just to not-hanging) on
# timeout: a locked index or network mount silently skips the length check
# rather than blocking the commit. That is a deliberate choice for a
# style/lint gate, not a security-relevant scanner — contrast
# deny-pii-in-commits.sh, which fails closed on the same class of timeout
# because an unscanned commit there is an unscanned leak vector.
#
# The "HEAD:$f" show call is the one exception: its timeout yields old=0,
# which can flip a shrinking-but-still-over-limit file to a false deny (see
# test_check_skill_length.py's HEAD-timeout characterization test).
#
# The rev-parse, diff, and both show calls' cap-engagement characterization
# tests live in test_check_skill_length.py, valid for both callers because
# these capped calls are caller-invariant. The two cat-file -s calls (one
# per revision, feeding the byte-count check) are covered by
# test_byte_cap_cat_file_git_timeout_engages_cap in
# test_check_claude_md_length.py.
#
# `old` (the previously committed version, for both the line-count and
# byte-count checks) is measured against _lib_gate_diff_base's resolved
# base, not the literal HEAD -- resolved once above the loop below, since a
# per-file resolution inside it would spawn state detection and a full
# merge-tree --write-tree once per staged file for a value identical on
# every iteration. This is a delta comparison (new > limit && new > old),
# not an absolute diff: mid-merge, the base substitution stops upstream's
# own growth of a file from being charged to the merger, since `old` would
# otherwise be the pre-merge feature tip. Mid-rebase, where the base stays
# empty because REBASE_HEAD reaches neither anchor in the ordinary case,
# `old` falls back to mid-rebase HEAD -- the new base plus already-replayed
# commits -- which is already the correct pre-commit baseline for the
# commit being replayed, so this consumer has no rebase-specific over-count
# either way.
_lib_staged_length_gate() {
  local repo_root="$1" pattern="$2" over_limit_message="$3" byte_limit="${4:-}"

  # Fail closed if a caller forgot to define limit_for, rather than letting
  # `limit=$(limit_for "$f")` silently yield empty and skip the length check.
  if ! declare -f limit_for >/dev/null 2>&1; then
    emit_deny "internal error — limit_for is not defined. This is a caller-contract violation, not a policy violation; report it."
    return 0
  fi

  if [ "$(_lib_capped git -C "$repo_root" rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]; then
    return 0
  fi

  # base_status is intentionally not checked -- ${base:-HEAD} below already
  # falls back to plain HEAD on status 1 or 2 alike, an over-strict rather
  # than permissive failure direction. Captured for parity with this
  # function's other _lib_gate_diff_base callers and for a future log line.
  local base base_status
  base=$(_lib_gate_diff_base "$repo_root")
  # shellcheck disable=SC2034 # base_status is captured for a future log line, not consumed today -- see the comment above.
  base_status=$?

  local fail=0 messages="" f new old limit new_content old_content
  while IFS= read -r f; do
    # The trailing 'x' sentinel, stripped back off via "${var%x}", preserves
    # trailing blank lines that command substitution would otherwise strip,
    # so the line count below doesn't undercount a file ending in blank
    # lines. Byte counts do NOT go through this captured content — they use
    # the separate `git cat-file -s` calls below, precisely to avoid this
    # same command substitution's NUL-byte-dropping behavior (see the
    # BYTE_LIMIT comment on _lib_staged_length_gate's header above).
    new_content=$(_lib_capped git -C "$repo_root" show ":$f" 2>/dev/null; printf x)
    new_content="${new_content%x}"
    old_content=$(_lib_capped git -C "$repo_root" show "${base:-HEAD}:$f" 2>/dev/null; printf x)
    old_content="${old_content%x}"
    new=$(printf '%s' "$new_content" | awk 'END{print NR}')
    old=$(printf '%s' "$old_content" | awk 'END{print NR}')
    limit=$(limit_for "$f")
    if _lib_length_ratchet_exceeded "$new" "$old" "$limit"; then
      messages="${messages}  $f: $new lines (was $old, limit $limit)\n"
      fail=1
    fi
    if [ -n "$byte_limit" ]; then
      local new_bytes old_bytes
      new_bytes=$(_lib_capped git -C "$repo_root" cat-file -s ":$f" 2>/dev/null)
      old_bytes=$(_lib_capped git -C "$repo_root" cat-file -s "${base:-HEAD}:$f" 2>/dev/null)
      [ -n "$new_bytes" ] || new_bytes=0
      [ -n "$old_bytes" ] || old_bytes=0
      if _lib_length_ratchet_exceeded "$new_bytes" "$old_bytes" "$byte_limit"; then
        messages="${messages}  $f: $new_bytes bytes (was $old_bytes, limit $byte_limit)\n"
        fail=1
      fi
    fi
  done < <(_lib_capped git -C "$repo_root" diff --cached --name-only 2>/dev/null | grep -E "$pattern")

  if [ "$fail" -eq 1 ]; then
    local reason
    reason=$(printf '%s Reduce to the limit before committing:\n%b' "$over_limit_message" "$messages")
    emit_deny "$reason"
  fi
  return 0
}

# Decide whether a command chains `marker.sh write <skill>` before its first
# `git commit`. PreToolUse hooks fire once per Bash tool call before the chain
# runs, so an on-disk marker check denies naturally-typed forms like
# `marker.sh write code-review && git commit`. When the same Bash call will
# write the marker before invoking commit, the in-chain marker.sh invocation
# is the same evidence the on-disk marker would later provide — marker.sh is
# the only sanctioned writer in either case.
#
# **Anchored at command start, not fragment start.** A fragment-walking
# approach (split on `&&` / `;` / `|`, then scan each fragment) would treat
# heredoc-body lines and wrapper-command arguments as "fragments" — so
# `echo ~/.claude/scripts/marker.sh write code-review && git commit` or
# `cat <<EOF | bash\n...marker.sh write code-review\nEOF && git commit` would
# wedge the gate open without ever actually invoking marker.sh. The strict
# command-start anchor here mirrors enforce-marker-script-shape.sh's
# VALID_CHAINED_COMMIT_PATTERN: only the literal shape
# `marker.sh write <skill>{,&& marker.sh write <skill2>...} && git commit`
# is honored. Wrapper commands, env-var prefixes, `bash -c`, heredoc bodies,
# and pipes all fail the anchor.
#
# Usage: _lib_chains_marker_write_before_commit "$COMMAND" code-review
# Returns 0 (true) if the command matches the sanctioned chained shape AND
# the target skill appears among the chained writes.
_lib_chains_marker_write_before_commit() {
  local command="$1" skill="$2"
  # Step 1: command matches the sanctioned chained shape (mirrors
  # enforce-marker-script-shape.sh's VALID_CHAINED_COMMIT_PATTERN). One or
  # more marker.sh write fragments joined by `&&`, then git commit. Anchored
  # so wrapper commands cannot trick the gate.
  if ! printf '%s' "$command" | grep -qE \
    "^[[:space:]]*((~|/[A-Za-z0-9_./-]+)/\.claude/scripts/marker\.sh[[:space:]]+write[[:space:]]+(code-review|skill-review|plan-review|ready-for-review)[[:space:]]*&&[[:space:]]*)+git[[:space:]]+commit([[:space:]].*)?$"; then
    return 1
  fi
  # Step 2: target skill is among the chained writes. A chain like
  # `marker.sh write skill-review && git commit` must not authorize a
  # code-review-gated commit.
  printf '%s' "$command" | grep -qE \
    "(~|/[A-Za-z0-9_./-]+)/\.claude/scripts/marker\.sh[[:space:]]+write[[:space:]]+${skill}([[:space:]]|$)"
}

# _lib_worktree_enforcement_active REPO_ROOT
# Returns 0 (true) when worktree discipline is active for the given repo root.
# Three-marker logic:
#   1. Committed repo sentinel (.claude/worktree-required at repo root): hard requirement,
#      travels via git pull. Opt-out cannot defeat it.
#   2. Machine sentinel (~/.claude/worktree-required): personal default-on for all repos
#      on this machine. Defeated by the per-repo opt-out.
#   3. Per-repo opt-out (.claude/worktree-optout at repo root): exempts this repo from
#      the machine sentinel only — has no effect when the repo sentinel is present.
# On filesystem error (EACCES, ESTALE, NFS timeout), [ -f ] returns false — the machine
# sentinel silently deactivates for that invocation, the same outcome as an absent sentinel.
# Empty REPO_ROOT returns 1 (no-enforce): the machine sentinel must never fire when
# the session is outside a git repo.
_lib_worktree_enforcement_active() {
  local repo_root="$1"
  [ -n "$repo_root" ] || return 1                                     # degenerate: no repo, never enforce
  [ -f "$repo_root/.claude/worktree-required" ] && return 0           # committed requirement (opt-out has no effect)
  # Machine default, minus per-repo opt-out.
  # An unresolvable config dir (empty/unset $HOME, no CLAUDE_CONFIG_DIR) skips
  # the resolved-config-dir arm rather than probing a root-anchored path —
  # same load-bearing guard as before, now expressed via _lib_config_dir.
  # Union, not swap: checks the resolved config dir first, then falls back
  # to the literal $HOME/.claude sentinel — a machine-wide `worktree-required`
  # armed before CLAUDE_CONFIG_DIR adoption must not silently go dark under a
  # differentiated profile, the same enforcement-invariant-regression shape
  # the guard-config hooks (deny-credential-file-reads.sh et al.) fix for
  # their own opt-in configs. Mirrors require-worktree-for-file-writes.sh's
  # exemption union.
  local config_dir
  if config_dir=$(_lib_config_dir) && [ -f "$config_dir/worktree-required" ]; then
    [ ! -f "$repo_root/.claude/worktree-optout" ] && return 0
    return 1
  fi
  [ -f "$HOME/.claude/worktree-required" ] \
    && [ ! -f "$repo_root/.claude/worktree-optout" ] \
    && return 0
  return 1
}

# _lib_autonomous_shipping_sentinel_present CONFIG_DIR
# Returns 0 (true) iff the autonomous-shipping-required sentinel exists at
# either CONFIG_DIR or the literal ~/.claude/autonomous-shipping-required —
# a union, not a swap, so a sentinel armed before CLAUDE_CONFIG_DIR adoption
# still activates.
# Sentinel presence only: this is NOT the full autonomous-shipping-active
# verdict, which also requires the per-repo .claude/autonomous-shipping-optout
# check — see _lib_autonomous_shipping_active below.
# CONFIG_DIR is a required argument rather than resolved internally via
# _lib_config_dir so that a caller which already has it resolved (e.g. the
# fast path, once per Stop event) skips a redundant _lib_config_dir call.
# Inherits, rather than introduces, the case where CONFIG_DIR is set but
# $HOME is empty or unset: the legacy-location check then evaluates against
# a root-anchored path. This is unguarded by design.
_lib_autonomous_shipping_sentinel_present() {
  [ "$#" -eq 1 ] || return 1
  local config_dir="$1"
  [ -n "$config_dir" ] || return 1
  [ -f "$config_dir/autonomous-shipping-required" ] || [ -f "$HOME/.claude/autonomous-shipping-required" ]
}

# _lib_autonomous_shipping_active REPO_ROOT
# Returns 0 (true) when this machine has opted into autonomous shipping
# (commit/push/PR without asking) for the given repo.
#
# This function is not a generalization of _lib_worktree_enforcement_active
# above: it has no committed-sentinel arm. That function's committed-sentinel
# arm is safe because worktree enforcement only restricts a hostile repo.
# Autonomous shipping removes a human checkpoint instead, so a repo's own
# committed content must never grant it. There is no repo-level "required"
# file in this code path; committing one has no effect. Two tiers only: (1)
# machine sentinel, required — _lib_autonomous_shipping_sentinel_present's
# union of the resolved config dir and the legacy ~/.claude location; (2)
# per-repo opt-out (.claude/autonomous-shipping-optout), narrows the machine
# default off for this repo only. Every error path fails toward NOT shipping
# — the safe direction for a granting mechanism:
#   - filesystem error
#   - empty $HOME
#   - empty REPO_ROOT
#   - wrong argument count
_lib_autonomous_shipping_active() {
  [ "$#" -eq 1 ] || return 1
  local repo_root="$1"
  [ -n "$repo_root" ] || return 1
  # Load-bearing, same reasoning as _lib_worktree_enforcement_active above:
  # an unresolvable config dir would otherwise probe a root-anchored path,
  # and a stray root-owned file there would force-activate autonomous
  # shipping — a materially worse consequence here than for worktree
  # enforcement, since this mechanism removes a human checkpoint rather
  # than adding one.
  local config_dir
  config_dir=$(_lib_config_dir) || return 1
  _lib_autonomous_shipping_sentinel_present "$config_dir" || return 1
  [ -f "$repo_root/.claude/autonomous-shipping-optout" ] && return 1
  return 0
}

# _lib_permission_prompt_tracking_active
# Returns 0 (true) when this machine has opted into permission-prompt
# tracking (~/.claude/track-permission-prompts exists) — gates
# track-permission-prompts.sh. Same sentinel-file shape as
# _lib_autonomous_shipping_active above, minus the per-repo optout: this
# mechanism only appends to a local log and changes no git/PR/tool
# behavior, so there is no per-repo axis to narrow (see
# docs/permission-prompt-tracking.md). Zero-arity by design — unlike
# _lib_autonomous_shipping_active, which takes a repo root to check a
# per-repo optout against, this sentinel is machine-global with nothing
# repo-scoped to look up. Fails toward NOT tracking on an unresolvable
# config dir, the same error direction every other opt-in gate in this
# file takes.
_lib_permission_prompt_tracking_active() {
  local config_dir
  config_dir=$(_lib_config_dir) || return 1
  [ -f "$config_dir/track-permission-prompts" ] || return 1
  return 0
}

# _lib_valid_session_id_component SESSION_ID
# Returns 0 (true) iff SESSION_ID is safe to use as a single filesystem path
# component (e.g. "$STATE_DIR/$SESSION_ID"). Every call site that builds such
# a path takes SESSION_ID straight from the hook payload's `.session_id` with
# no further sanitization, so a value containing ".." or "/" escapes the
# intended directory once concatenated in — turning an `rm -f`, `>`, or
# `touch` against that path into an operation against a caller-chosen path
# instead. Harness session ids are UUIDs, so this conservative allow-list
# (letters, digits, underscore, hyphen) has ample room without ever needing
# '.' or '/'. Empty input is rejected — callers must not fall through to an
# unvalidated empty SESSION_ID.
_lib_valid_session_id_component() {
  local session_id="$1"
  [[ "$session_id" =~ ^[A-Za-z0-9_-]+$ ]]
}

# _lib_active_bypass_marker_live MARKER_DIR_NAME SESSION_ID
# - Returns 0 iff $HOME/.claude/MARKER_DIR_NAME/SESSION_ID holds a live PID
#   within the 60-minute idle window; evicts the marker (dead/unreadable
#   PID, or aged-out mtime) and returns 1 otherwise, so a session that
#   never cleaned up can't wedge a gate open indefinitely.
# - A capped-out cat/find read (past _lib_capped's 5s timeout, e.g. a
#   stalled NFS mount) evicts the marker the same as a dead PID or an
#   aged-out mtime, rather than hanging the caller.
# - Session-id validation lives here so callers can't forget it. An empty
#   or path-escaping id returns 1 having touched the filesystem not at all.
# - Reports only liveness, not a verdict on the tool call: a 1 withholds a
#   standing exception where the marker is the sole gate, or lets further
#   checks below decide where the gate has more of them.
# - Marker path has no repo hash, so a live marker releases its gate for
#   every tree the session touches while the skill runs, unlike hash-bound
#   completion markers.
# - Side-effect-free on mtime regardless of outcome. The refresh that
#   slides the idle window forward lives in
#   _lib_active_bypass_marker_live_and_touch below, so a status-only read
#   can never keep a marker artificially fresh.
# See docs/hooks.md's "Gate deadlock recovery" section for the tree-switching
# gap this leaves open and why 60 minutes matches
# session-marker-dashboard.sh's own staleness threshold.
#
# Usage: if _lib_active_bypass_marker_live ".respond-pr-active.d" "$SESSION_ID"; then exit 0; fi
_lib_active_bypass_marker_live() {
  # Arity guard. Under `set -u` a call that omits an argument aborts the whole
  # hook process at expansion time rather than returning here, and a gate hook
  # that dies before emitting a decision has no defined disposition. Degrade a
  # malformed call to the same withheld-bypass outcome as every other rejection
  # path instead. `[ ]` rather than `(( ))`: a standalone arithmetic command
  # evaluating to zero is itself a non-zero exit status, which `set -e` callers
  # would abort on.
  [ "$#" -eq 2 ] || return 1
  local marker_dir_name="$1" session_id="$2"
  _lib_valid_session_id_component "$session_id" || return 1
  # No empty-$HOME guard here, unlike _lib_worktree_enforcement_active above.
  # That one probes a path whose mere presence force-enables enforcement for
  # every repo on the machine, so an empty $HOME there fails toward more
  # enforcement from a root-writable path. This function's sinks fail the other
  # way: an unresolvable config dir just makes the marker read miss and the
  # bypass is withheld, leaving the gate enforcing.
  local config_dir
  config_dir=$(_lib_config_dir) || return 1
  local marker="$config_dir/$marker_dir_name/$session_id"
  [ -f "$marker" ] || return 1
  local stored_pid
  # `|| true` keeps this line's status irrelevant to a caller's shell options.
  # The pipeline reports failure under `pipefail` when `cat` loses a race with
  # the marker's removal even though `tr` succeeds on empty stdin; a `set -e`
  # caller would then abort mid-gate-check rather than fall through to the
  # eviction below. No caller sets `-e` today — this keeps that from mattering.
  stored_pid=$(_lib_capped cat "$marker" 2>/dev/null | _lib_capped tr -d '[:space:]') || true
  # `find -mmin -60` mirrors require-routing-read.sh's own freshness idiom.
  # This is not a hard age cap: the touch-on-use wrapper below slides this
  # same 60-minute window forward on every gating call that finds the marker
  # live.
  if [[ "$stored_pid" =~ ^[0-9]+$ ]] && kill -0 "$stored_pid" 2>/dev/null \
    && [ -n "$(_lib_capped find "$marker" -mmin -60 2>/dev/null)" ]; then
    return 0
  fi
  rm -f "$marker" 2>/dev/null
  return 1
}

# _lib_active_bypass_marker_live_and_touch MARKER_DIR_NAME SESSION_ID
# - Same liveness verdict as _lib_active_bypass_marker_live, but on a live
#   marker also refreshes its mtime, sliding the 60-minute idle window
#   forward instead of letting it expire mid-run.
# - Callers, each refreshing on its own gate-check cadence rather than
#   narrowly on the owning skill's own activity — see docs/hooks.md's "Gate
#   deadlock recovery" section for what triggers each hook's check:
#   - require-plan-review.sh, require-ready-for-review.sh,
#     require-respond-pr.sh, require-memory-skill.sh, on their own gate check.
#   - nudge-handoff-near-context-cap.sh, for its .handoff-active.d label
#     only, inside its otherwise status-only marker-family enumeration.
# - Every other status-only reader (marker.sh status's
#   _status_report_active_bypass; nudge-handoff-near-context-cap.sh's other
#   four enumerated labels) must keep calling the unrefreshing predicate
#   directly — see its own docstring for why the refresh can't live there.
#
# Usage: if _lib_active_bypass_marker_live_and_touch ".respond-pr-active.d" "$SESSION_ID"; then exit 0; fi
_lib_active_bypass_marker_live_and_touch() {
  [ "$#" -eq 2 ] || return 1
  local marker_dir_name="$1" session_id="$2"
  _lib_active_bypass_marker_live "$marker_dir_name" "$session_id" || return 1
  local config_dir
  config_dir=$(_lib_config_dir) || return 0
  # `-c` (no-create): a concurrent eviction (clear-stale, deactivate, another
  # gate hit) can remove the marker between the liveness check above and this
  # touch. Without -c, a bare `touch` would recreate it as an empty file with
  # no PID -- resurrecting a marker this call didn't itself find live. Fail
  # silent, matching the eviction idiom in the predicate above: a touch
  # failure must not turn a verdict this call already committed to into a
  # hook-process abort.
  _lib_capped touch -c "$config_dir/$marker_dir_name/$session_id" 2>/dev/null || true
  return 0
}

# _lib_first_live_linked_worktree REPO_ROOT
# Prints the path of the first linked worktree of REPO_ROOT whose directory is
# present on disk and returns 0; prints nothing and returns 1 when there is
# none. `git worktree list` still reports entries whose directory was deleted
# but not pruned, so each candidate is tested rather than trusting the entry
# count. Callers use this to distinguish "session is in the main tree while the
# work belongs in a worktree" from "this repo has no worktree at all" — the
# latter is the ordinary state just before `git worktree add`, and carries no
# wrong-tree risk because there is no second tree to confuse the first with.
# Parses newline-delimited output, so a worktree path containing a literal
# newline (pathological, and unhandled here) would be split across two lines
# and matched incorrectly; git's own docs recommend `-z` for that case.
_lib_first_live_linked_worktree() {
  local repo_root="$1" worktree_path
  [ -n "$repo_root" ] || return 1
  while IFS= read -r worktree_path; do
    [ -n "$worktree_path" ] || continue
    [ "$worktree_path" = "$repo_root" ] && continue
    if [ -d "$worktree_path" ]; then
      printf '%s' "$worktree_path"
      return 0
    fi
  done < <(_lib_capped git -C "$repo_root" worktree list --porcelain 2>/dev/null \
    | awk '/^worktree /{print substr($0, 10)}')
  return 1
}

# _lib_stray_marker_hint REPO_ROOT
# Returns a hint string when the repo-root sentinel exists in the working
# tree but is not tracked in the git index — a stray copy still enforces
# per _lib_worktree_enforcement_active's `[ -f ]` check, and this surfaces
# why: the deny messages that consume this hint already state the
# tracked/committed case, so an untracked marker producing the same deny
# reads as unexplained. Returns empty when the marker is absent, or present
# and tracked (the normal opted-in case) — no noise on the common path.
# Deny-path only: callers interpolate this into an already-decided deny
# message, so the `git ls-files` call here never runs on the allow path.
# _lib_capped's 5s timeout backstop: a stalled `git ls-files` (e.g.
# NFS-mounted repo root) would otherwise hold the deny message open
# indefinitely.
_lib_stray_marker_hint() {
  local repo_root="$1"
  [ -f "$repo_root/.claude/worktree-required" ] || return 0
  _lib_capped git -C "$repo_root" ls-files --error-unmatch .claude/worktree-required \
    >/dev/null 2>&1 && return 0
  printf '%s' " Note: .claude/worktree-required is present but untracked — an accidental stray copy activates enforcement exactly like a committed one. Commit it if intentional, or remove it if it was created by accident."
}

# _lib_hook_claude_pid
# Resolves this hook's own Claude Code main-process PID. Hooks are direct
# children of `claude`, so $PPID is already that process, unlike
# _lib_resolve_claude_pid's ancestor walk below (for callers, such as a Bash
# tool script, that sit one or more hops further away).
#
# Validate-then-select: an unusable $CLAUDE_PID must fall back to $PPID, not
# abort the caller, so substitution can't happen before the checks below run.
# Accepted only within one hop of $PPID (itself, or its immediate parent --
# the shim case) so an unrelated live process can't be named. Shared by
# capture-session-id.sh (SessionStart, SubagentStart) and
# record-session-end.sh (SessionEnd), both of which need this identical
# resolution.
#
# Always prints something (falls back to $PPID) and returns 0; the printed
# value can still be empty if $PPID itself is empty, so callers must check
# for that themselves and decide their own failure message, mirroring
# capture-session-id.sh's own post-call check.
# Usage: CLAUDE_PID=$(_lib_hook_claude_pid)
_lib_hook_claude_pid() {
  local resolved_claude_pid=$PPID
  if [ -n "${CLAUDE_PID:-}" ] && [[ $CLAUDE_PID =~ ^[0-9]+$ ]]; then
    local ppid_parent
    ppid_parent=$(_lib_capped ps -o ppid= -p "$PPID" 2>/dev/null | tr -d ' ')
    if [ "$CLAUDE_PID" = "$PPID" ] || { [ -n "$ppid_parent" ] && [ "$CLAUDE_PID" = "$ppid_parent" ]; }; then
      resolved_claude_pid=$CLAUDE_PID
    fi
  fi
  printf '%s\n' "$resolved_claude_pid"
}

# _lib_resolve_claude_pid
# Prints "<session_id> <pid>" for the live Claude Code main-process PID that
# is an ancestor of the calling process, and returns 0. Returns 2 with no
# stdout when no ancestor carries a live, start-time-validated session file.
# Moved here from marker.sh's private _walk_session (which now delegates to
# this) so a hook — which cannot safely source marker.sh, since it dispatches
# on $1 at file scope — can resolve its own session's live PID directly, with
# no subprocess spawn.
#
# Walk up the process ancestor chain looking for a session file written by
# capture-session-id.sh at $CONFIG_DIR/sessions/<pid>. Direct hook invocation
# resolves in one step ($PPID = Claude Code PID); a Bash-tool script
# invocation resolves in two steps ($PPID = Bash tool shell, grandparent =
# Claude Code PID). The loop handles any depth. A recorded start time that
# doesn't match the live process's current start time — including an entry
# with no recorded start time at all — means the PID was reused since the
# entry was written, so it is untrusted and treated as if the file were
# absent. `lstart`'s one-second resolution leaves a residual false-positive
# window: a reused PID whose new process starts within the same wall-clock
# second as the stale entry's process still compares equal.
_lib_resolve_claude_pid() {
  local config_dir pid
  config_dir=$(_lib_config_dir) || return 2
  pid=$PPID
  while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ]; do
    local sid recorded_start current_start
    sid=""
    if [ -r "$config_dir/sessions/$pid" ]; then
      {
        IFS= read -r sid
        IFS= read -r recorded_start
      } < "$config_dir/sessions/$pid" 2>/dev/null
      if [ -n "$sid" ] && [ -n "$recorded_start" ]; then
        current_start=$(TZ=UTC LC_ALL=C _lib_capped ps -o lstart= -p "$pid" 2>/dev/null)
        [ "$current_start" = "$recorded_start" ] || sid=""
      else
        sid=""
      fi
    fi
    if [ -n "$sid" ]; then
      printf '%s %s' "$sid" "$pid"
      return 0
    fi
    pid=$(_lib_capped ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' \t')
  done
  return 2
}

# _lib_resolve_session_id
# Prints this session's live, path-safe session id and returns 0. Wraps
# _lib_resolve_claude_pid's ancestor walk with
# _lib_valid_session_id_component's validation. pr-diff-against-base.sh
# --record needs this resolve-then-validate pair to key its own
# subject-marker file the way marker.sh already keys every completion marker.
# Exit 1, empty stdout: no live ancestor session found, or the resolved id
# fails path-safety validation. Silent on failure -- callers print their own
# context-specific error message.
_lib_resolve_session_id() {
  local out sid
  out=$(_lib_resolve_claude_pid) || return 1
  sid="${out%% *}"
  _lib_valid_session_id_component "$sid" || return 1
  printf '%s' "$sid"
}

# _lib_worktree_lock_pid WORKTREE_ROOT PORCELAIN_TEXT
# Parses a captured `git worktree list --porcelain` text for WORKTREE_ROOT's
# lock state. Prints "<pid> <session_id>" from a `locked claude-code pid <N>`
# or `locked claude-code pid <N> session <ID>` reason (the exact string
# _lib_worktree_collision_guard writes) and returns 0 — session_id is empty
# for an old-format, pre-session-id lock, but the separating space is always
# printed, so a caller splits with `read -r pid session_id <<< "$output"`
# with no dependency on a trailing token actually being present. Prints
# nothing and returns 1 when WORKTREE_ROOT has no `locked` line in this
# capture (unlocked, or the capture is stale and no longer lists it at all);
# prints nothing and returns 2 when locked but the reason isn't that exact
# shape (e.g. a human-authored reason for an unrelated purpose, or a session
# field truncated to just the `session` keyword) — matched on the full
# reason, not a loose `pid <N>` substring search, so an unrelated reason that
# happens to mention "pid" can't be misread as a Claude-session lock. The
# `worktree <path>` line is always the first line of each porcelain record,
# so unlike a branch-name match (which can appear after several other
# lines), the target record is known immediately with no deferred-commit
# buffering.
_lib_worktree_lock_pid() {
  local worktree_root="$1" porcelain="$2"
  local line path in_target=0 locked=0 pid="" session_id=""
  while IFS= read -r line; do
    case "$line" in
      "worktree "*)
        path="${line#"worktree "}"
        if [ "$path" = "$worktree_root" ]; then
          in_target=1
        else
          in_target=0
        fi
        ;;
      "locked"*)
        if [ "$in_target" -eq 1 ]; then
          locked=1
          if [[ "$line" =~ ^locked\ claude-code\ pid\ ([0-9]+)(\ session\ ([A-Za-z0-9_-]+))?$ ]]; then
            pid="${BASH_REMATCH[1]}"
            session_id="${BASH_REMATCH[3]}"
          fi
        fi
        ;;
    esac
  done <<< "$porcelain"
  if [ "$locked" -eq 0 ]; then
    return 1
  fi
  if [ -z "$pid" ]; then
    return 2
  fi
  printf '%s %s' "$pid" "$session_id"
  return 0
}

# _lib_worktree_lock_absent WORKTREE_GIT_DIR
# Returns 0 (true) iff WORKTREE_GIT_DIR/locked does not exist yet.
# Every _lib_worktree_collision_guard call site runs this immediately
# before calling the guard.
# A pre-existing foreign *live* lock always denies, never allows.
# A pre-existing foreign *dead* lock may now be reclaimed and allowed via
# the guard's own eviction path (_lib_worktree_reclaim_dead_lock), which
# this pre-check cannot see, since the lock file itself was never absent.
# If the guard then allows, its own O_EXCL write inside that call is the
# only way the combination "was absent, now allows" happens for a foreign
# session. A same-session parallel call can also produce that combination,
# via the guard's self-lock-recognition fast path. See the hook callers'
# known-gaps notes on the same-session double-message race.
# So for the foreign-session case, the caller then knows this call is the
# reason the worktree is now locked.
_lib_worktree_lock_absent() {
  local worktree_git_dir="$1"
  [ ! -e "$worktree_git_dir/locked" ]
}

# _lib_worktree_acquire_lock WORKTREE_ROOT WT_GIT_DIR MY_PID MY_SESSION_ID
# Attempts the O_EXCL lock-file create in WT_GIT_DIR and, on success,
# re-reads porcelain to confirm the write landed as MY_PID/MY_SESSION_ID.
# Prints nothing. Returns 0 when acquired and verified, 1 when the write
# itself failed (contended -- caller diagnoses the current holder), 2 when
# the write succeeded but the post-write reread could not confirm it
# (caller emits its own "could not be confirmed after acquiring it"
# message -- message ownership stays with the caller, not this function).
_lib_worktree_acquire_lock() {
  local worktree_root="$1" wt_git_dir="$2" my_pid="$3" my_session_id="$4"
  local porcelain lock_output locked_pid locked_session_id state
  # shellcheck disable=SC2016 # single-quoted on purpose: $1/$2/$3 must resolve as the inner bash's own positional parameters, not expand in this shell before exec.
  # Double-quoting would open a shell-injection surface via $wt_git_dir.
  _lib_capped bash -c 'set -o noclobber; printf "claude-code pid %s session %s\n" "$1" "$2" > "$3/locked"' _ "$my_pid" "$my_session_id" "$wt_git_dir" 2>/dev/null || return 1

  porcelain=$(_lib_capped git -C "$worktree_root" worktree list --porcelain 2>/dev/null) || return 2
  lock_output=$(_lib_worktree_lock_pid "$worktree_root" "$porcelain") && state=0 || state=$?
  read -r locked_pid locked_session_id <<< "$lock_output"
  if [ "$state" -eq 0 ]; then
    if [ -n "$locked_session_id" ]; then
      [ "$locked_session_id" = "$my_session_id" ] && return 0
    elif [ "$locked_pid" = "$my_pid" ]; then
      return 0
    fi
  fi
  return 2
}

# _lib_worktree_reclaim_dead_lock WORKTREE_ROOT WT_GIT_DIR DEAD_PID DEAD_SESSION_ID MY_PID MY_SESSION_ID
# Claims a once-only, per-lock-identity right to evict a worktree lock the
# caller has already proven dead via `kill -0`, then evicts it and
# re-acquires it for MY_PID/MY_SESSION_ID. The claim is an O_EXCL create of
# `WT_GIT_DIR/claude-evicted-lock-<DEAD_PID>-<DEAD_SESSION_ID or nosession>`
# and is never removed -- see docs/design-decisions.md §36 for why a
# release-free claim is the race-safe primitive here. Winning the claim
# reads the raw lock file and unlinks it in the same subprocess only if its
# content still matches the diagnosed-dead identity, closing the window a
# separate reread-then-delete call pair would leave open between confirming
# the lock's content and removing it.
# Prints nothing. Returns 0 only on a verified reclaim; every failure path
# returns 1 with the claim file left in place.
_lib_worktree_reclaim_dead_lock() {
  local worktree_root="$1" wt_git_dir="$2" dead_pid="$3" dead_session_id="$4" my_pid="$5" my_session_id="$6"
  local claim_path="$wt_git_dir/claude-evicted-lock-${dead_pid}-${dead_session_id:-nosession}"

  # shellcheck disable=SC2016 # single-quoted on purpose: $1/$2 must resolve as the inner bash's own positional parameters, not expand in this shell before exec.
  # Double-quoting would open a shell-injection surface via $claim_path.
  _lib_capped bash -c 'set -o noclobber; printf "claimed by claude-code pid %s\n" "$1" > "$2"' _ "$my_pid" "$claim_path" 2>/dev/null || return 1

  local expected_reason="claude-code pid ${dead_pid}"
  [ -n "$dead_session_id" ] && expected_reason="${expected_reason} session ${dead_session_id}"
  # shellcheck disable=SC2016 # single-quoted on purpose: $1/$2 must resolve as the inner bash's own positional parameters, not expand in this shell before exec.
  # Double-quoting would open a shell-injection surface via $expected_reason.
  _lib_capped bash -c 'content=$(cat "$1" 2>/dev/null); [ "$content" = "$2" ] && rm -f "$1"' _ "$wt_git_dir/locked" "$expected_reason" 2>/dev/null || return 1

  _lib_worktree_acquire_lock "$worktree_root" "$wt_git_dir" "$my_pid" "$my_session_id" && return 0
  return 1
}

# _lib_worktree_collision_guard TARGET_PATH REPO_GIT_COMMON_DIR
# Enforces that at most one live session holds write access to a given
# linked worktree at a time. TARGET_PATH is any path inside the worktree to
# protect; REPO_GIT_COMMON_DIR is the caller's already-verified common-dir
# for the enforced repo, cross-checked here against TARGET_PATH's own
# resolution as a defense against the target having changed underneath the
# caller between its own check and this call. On success, prints nothing and
# returns 0. On failure, prints a human-readable reason to stdout (for the
# caller to fold into its own deny message) and returns 1. The lock reason
# string's exact shape is documented at _lib_worktree_lock_pid's header, the
# format's source of truth — not restated here.
#
# Self-recognition ("is this my own lock?") compares session_id, which
# `claude --continue`/`--resume` keeps stable across the CLI process's PID
# change, falling back to PID comparison only for an old-format lock that
# carries no session_id (predates this fix, or was truncated mid-write).
#
# Read-only fast path: an already-self-held lock (from an earlier write this
# session) returns 0 with no write attempt.
#
# Otherwise acquires via an O_EXCL create (bash `noclobber`) against the
# worktree's own `<git-dir>/locked` file, not `git worktree lock` -- git's
# own lock write is not atomic (source citation and the CI race this fixes:
# .claude/plans/atomic-worktree-lock-acquisition.md). The write targets
# git's own lock-file path and format, so `git worktree list --porcelain`/
# `unlock`/`remove` all still read and act on it correctly. A successful
# write is re-read via porcelain to confirm our own pid before returning 0,
# fail-closed on mismatch. A failed write (contended) re-reads porcelain to
# diagnose the holder: a live holder denies; a dead holder gets one
# claim-gated reclaim attempt (_lib_worktree_reclaim_dead_lock) before
# falling back to today's manual-unlock deny. This function still never
# calls `git worktree unlock` itself (verified empirically to have no
# ownership check) -- eviction removes the raw lock file directly, guarded
# by an exclusive-create-only claim that is never released, which is what
# makes it race-safe where a bare evict-then-relock sequence is not (see
# docs/design-decisions.md §36).
#
# Known gaps (single-developer-machine threat model, not an adversarial
# boundary):
# - `kill -0` can't distinguish a dead PID from one owned by another user.
#   The same root cause also covers PID reuse that already happened before
#   or at the instant of that single call -- an unavoidable ambiguity, not
#   a check-then-recheck window, since nothing downstream re-derives
#   liveness.
# - A non-contention write failure (a permission error, or `bash` missing
#   from PATH) is misdiagnosed as a transient race and told to "retry" --
#   permanently wrong advice in that case.
# - `_lib_worktree_reclaim_dead_lock`'s re-acquisition call collapses
#   `_lib_worktree_acquire_lock`'s exit codes 1 (write failed) and 2 (wrote
#   but couldn't confirm) into a single failure -- a code-2 outcome (the
#   lock was actually just removed and rewritten as this caller's own,
#   unconfirmed lock) gets the same "no longer running, and could not be
#   cleared automatically" deny message as a genuine failure, which could
#   mislead a user into running `git worktree unlock` and stripping their
#   own freshly-written lock.
# - A write killed mid-write (the 5s `_lib_capped` timeout) can leave a
#   truncated `locked` file, misdiagnosed as a foreign manual lock instead
#   of our own timed-out write.
# - O_EXCL/`noclobber` exclusivity is not guaranteed atomic on older
#   NFS-mounted git-dirs, which would silently defeat this function's core
#   guarantee -- out of scope for this threat model.
# - A claim burnt by an interrupted eviction permanently disables
#   auto-eviction for that one lock identity in that one worktree.
# - Claim files are not garbage-collected.
# - If the harness kills the hook, or `_lib_capped`'s own 5s timeout fires,
#   after the lock removal succeeds but before reacquisition is confirmed,
#   the worktree is left fully unlocked with an orphaned, permanently-burnt
#   claim file. This is the one path that skips the deny-plus-manual-unlock
#   fallback every other failure mode in this list otherwise guarantees.
# - The reclaim path's aggregate `_lib_capped` call count is not a fixed
#   constant across successful reclaims: it varies with
#   `_lib_resolve_claude_pid`'s depth-dependent ancestor-PID-walk.
# - Its fixed component is 6 ordinary-path calls (3 rev-parse + self-check
#   porcelain + write + confirm-porcelain) plus a reclaim delta of exactly
#   +4 -- about 1.67x, not triple.
# - On a machine with neither `timeout` nor `gtimeout` on PATH,
#   `_lib_capped_for`'s fallback runs every call uncapped (pre-existing
#   gap).
# - Narrows but does not eliminate the residual race described in
#   docs/design-decisions.md §36 (a manual unlock racing this
#   subprocess's own read-then-unlink).
_lib_worktree_collision_guard() {
  local target_path="$1" repo_git_common_dir="$2"
  local worktree_root
  worktree_root=$(_lib_capped git -C "$target_path" rev-parse --show-toplevel 2>/dev/null) || {
    printf 'could not resolve the worktree root for %s' "$target_path"
    return 1
  }

  local wt_common_dir
  wt_common_dir=$(_lib_capped git -C "$worktree_root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
    printf 'could not confirm %s belongs to this repository' "$worktree_root"
    return 1
  }
  if [ "$wt_common_dir" != "$repo_git_common_dir" ]; then
    printf '%s does not belong to this repository — refusing to evaluate its lock state' "$worktree_root"
    return 1
  fi

  local my_pid_pair my_pid my_session_id
  my_pid_pair=$(_lib_resolve_claude_pid) || {
    printf "could not resolve this session's own process identity to check the worktree lock"
    return 1
  }
  my_pid="${my_pid_pair##* }"
  my_session_id="${my_pid_pair%% *}"

  local porcelain lock_output locked_pid locked_session_id state
  porcelain=$(_lib_capped git -C "$worktree_root" worktree list --porcelain 2>/dev/null) || {
    printf 'could not read worktree lock state for %s' "$worktree_root"
    return 1
  }
  # `lock_output=$(fn) || state=$?` (not a bare `lock_output=$(fn); state=$?`):
  # _lib_worktree_lock_pid's ordinary "unlocked" outcome is exit 1, and under
  # a caller's `set -e` a plain assignment aborts the script at that line
  # before `$?` is ever read — this file's own _lib_config_dir header
  # documents the same hazard. The `||` puts the assignment inside a
  # compound list, which `-e` does not abort on.
  lock_output=$(_lib_worktree_lock_pid "$worktree_root" "$porcelain") && state=0 || state=$?
  read -r locked_pid locked_session_id <<< "$lock_output"
  if [ "$state" -eq 0 ]; then
    if [ -n "$locked_session_id" ]; then
      [ "$locked_session_id" = "$my_session_id" ] && return 0
    elif [ "$locked_pid" = "$my_pid" ]; then
      return 0
    fi
  fi

  local wt_git_dir
  wt_git_dir=$(_lib_capped git -C "$worktree_root" rev-parse --path-format=absolute --git-dir 2>/dev/null) || {
    printf 'could not resolve the worktree-specific git-dir for %s' "$worktree_root"
    return 1
  }
  if [ "$wt_git_dir" = "$wt_common_dir" ]; then
    printf '%s is the main working tree, not a linked worktree — refusing to evaluate its lock state' "$worktree_root"
    return 1
  fi

  local acquire_status
  _lib_worktree_acquire_lock "$worktree_root" "$wt_git_dir" "$my_pid" "$my_session_id" && acquire_status=0 || acquire_status=$?
  if [ "$acquire_status" -eq 0 ]; then
    return 0
  elif [ "$acquire_status" -eq 2 ]; then
    printf 'the worktree lock for %s could not be confirmed after acquiring it — treating as unresolved' "$worktree_root"
    return 1
  fi

  porcelain=$(_lib_capped git -C "$worktree_root" worktree list --porcelain 2>/dev/null) || {
    printf 'could not confirm the worktree lock holder for %s' "$worktree_root"
    return 1
  }
  lock_output=$(_lib_worktree_lock_pid "$worktree_root" "$porcelain") && state=0 || state=$?
  read -r locked_pid locked_session_id <<< "$lock_output"
  # A concurrent self-race (e.g. two parallel subagents both writing into
  # this worktree with no `isolation: worktree` between them, per this
  # repo's own Agent Briefing) can land here after losing the lock attempt
  # above to an earlier call from this SAME live pid — not a different
  # session. Treat that identically to the first read's self-lock check
  # rather than misreporting the caller's own pid as a foreign collision.
  if [ "$state" -eq 0 ]; then
    if [ -n "$locked_session_id" ]; then
      [ "$locked_session_id" = "$my_session_id" ] && return 0
    elif [ "$locked_pid" = "$my_pid" ]; then
      return 0
    fi
  fi
  case "$state" in
    1)
      printf 'this worktree was locked at the moment of the write attempt but is already clear again — retry'
      ;;
    2)
      # shellcheck disable=SC2016 # single-quoted on purpose: the backticks are literal markdown-style code formatting in the deny message, not command substitution.
      printf 'this worktree is locked, but the lock reason does not name a process — if this was set deliberately for another purpose, resolve manually; otherwise run `git worktree unlock %s`' "$worktree_root"
      ;;
    0)
      if kill -0 "$locked_pid" 2>/dev/null; then
        printf 'this worktree is already in use by a live Claude Code session (pid %s) — wait for it to finish, or work in a different worktree' "$locked_pid"
      elif _lib_worktree_reclaim_dead_lock "$worktree_root" "$wt_git_dir" "$locked_pid" "$locked_session_id" "$my_pid" "$my_session_id"; then
        return 0
      else
        # shellcheck disable=SC2016 # single-quoted on purpose: the backticks are literal markdown-style code formatting in the deny message, not command substitution.
        printf 'this worktree is locked by pid %s, which is no longer running, and could not be cleared automatically — clear it with `git worktree unlock %s` and retry' "$locked_pid" "$worktree_root"
      fi
      ;;
  esac
  return 1
}

# Byte-size threshold above which content is too large to scan cheaply. 5 MB, shared between deny-data-file-reads.sh's Read-target cap and redact-credential-values.sh's tool_response cap.
_LIB_SIZE_THRESHOLD_BYTES=5242880

# _lib_config_lines FILE
# Prints each non-blank, non-comment line of a per-user config file
# (credential-file-guard.md, data-file-read-guard.md, pii-patterns.md,
# credential-value-patterns.md) as "<1-based raw line number>\t<CR-stripped,
# trimmed line>". The line number counts every raw line, including ones this
# function skips, so a caller reporting a parse error can point the user at
# the actual line in their file. Prints nothing (returns 0) when FILE is
# absent or unreadable. Callers apply their own per-line grammar and match
# semantics (substring glob, exact glob, or "<label>: <regex>") to the line
# field -- those differ by design and are not this function's concern.
_lib_config_lines() {
  local file="$1"
  [ -f "$file" ] && [ -r "$file" ] || return 0
  local raw_line line lineno=0
  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    lineno=$((lineno + 1))
    line=${raw_line%$'\r'}
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -z "$line" ] && continue
    case "$line" in '#'*) continue ;; esac
    printf '%s\t%s\n' "$lineno" "$line"
  done < "$file"
}

# Collapses bash's character-removal-based literal-reassembly mechanisms for
# credential/PII pattern matching against raw Bash command text: bash
# executes `cat ~/.ssh/config"_backup"`, `cat ~/id_r\sa`, and
# `cat ~/id_r$''sa` all identically to their unquoted/unescaped form (an
# adjacent quote split, a backslash-escaped character, and an ANSI-C/
# locale-translated quoted segment respectively all have the delimiting
# characters removed, then the remaining literals are joined into one
# word), but a `grep -E` scan of the unexpanded command sees each
# delimiter as a hard break and can miss a credential-path or
# credential-value pattern that only completes once they're removed.
# Deny-credential-bash-reads.sh and deny-pii-in-commits.sh's
# credential-value sub-check must run this over $COMMAND before matching
# against _LIB_CREDENTIAL_PATH_REGEX or _LIB_CREDENTIAL_VALUE_REGEX. Order:
# first drop the leading `$` of a $'...'/$"..." opener so its content
# reassembles the same way a plain "..."/'...' segment does, then remove
# backslash-escapes (bash drops an unquoted backslash and treats the
# following character literally, everywhere -- including inside a
# single-quoted region, where bash itself would NOT remove it; this
# over-strips relative to bash's real single-quote semantics, but only in
# the safe over-matching direction for a deny gate), then remove the
# remaining bare quote characters. Each step only ever joins
# previously-separated literal characters -- never breaks an existing
# contiguous match -- so the whole pipeline stays a safe superset
# transformation for these substring regexes. Does not attempt full
# shell-word tokenization: variable expansion and command substitution
# remain an accepted residual, same as the documented indirection gap (see
# docs/security-hardening.md).
# Call-site contract (load-bearing): the underlying sed/tr pipeline can fail
# (missing, killed, or erroring), and no caller runs under `set -e`, so
# every call site must capture and check the exit status immediately and
# fail closed on non-zero, rather than proceeding with a silently empty or
# partial result — see deny-invisible-commit-content.sh's COMMAND_UNQUOTED
# computation for the pattern.
_lib_strip_shell_quotes() {
  local stripped stripped_exit unquoted unquoted_exit
  stripped=$(printf '%s' "$1" | sed -E -e "s/\\\$'/'/g" -e 's/\$"/"/g' -e 's/\\(.)/\1/g')
  stripped_exit=$?
  [ "$stripped_exit" -ne 0 ] && return 1
  unquoted=$(printf '%s' "$stripped" | tr -d "\"'")
  unquoted_exit=$?
  [ "$unquoted_exit" -ne 0 ] && return 1
  printf '%s' "$unquoted"
  return 0
}

# Credential-shaped PATH tokens, sourced by deny-credential-bash-reads.sh and deny-credential-file-reads.sh. POSIX ERE, basename-token match (not path-qualified): matches a bare filename wherever it appears, closing a `cd ~/.ssh && cat id_rsa` bypass.
# Three alternations with different trailing boundaries. Group 1 excludes a following `.` so `id_rsa` doesn't match inside the safe-to-read `id_rsa.pub`, and `.env` doesn't match inside `.env.foo`/`package.env`; `.env`'s own dotted variants beyond the ones enumerated here are deliberately left to deny-env-reads.sh's broader `.env.*` gate. Group 2 (`.netrc`, `.git-credentials`, `credentials.json`, and the three directory-qualified stores) has no known safe dotted-suffix variant, so it allows a following `.` too — closing a `credentials.json.bak`/`.netrc.bak`-style backup-copy bypass group 1's exclusion would otherwise leave open. Group 3 matches `.ssh` (optionally backup/rename-suffixed, e.g. `.ssh.bak`, `.ssh_backup`, `.ssh.old` — the same `.bak`-style continuation group 2 allows) only as a directory/glob reference (`~/.ssh`, `~/.ssh/`, `~/.ssh//`, `~/.ssh/*`, `~/.ssh/.*`), not `.ssh/<filename>`; a named-file reference under `.ssh` (or its backup-suffixed siblings) is instead deny-by-default via `_lib_has_unsafe_ssh_dir_reference` below, since enumerating every unsafe key basename doesn't scale the way enumerating the few safe ones does.
_LIB_CREDENTIAL_PATH_REGEX='(^|[^A-Za-z0-9_.])(id_rsa|id_dsa|id_ecdsa|id_ed25519|\.env|\.env\.local|\.env\.production|\.env\.development|\.env\.staging|\.env\.test)([^A-Za-z0-9_.]|$)|(^|[^A-Za-z0-9_.])(\.netrc|_netrc|\.git-credentials|credentials\.json|\.credentials\.json|\.aws/credentials|\.docker/config\.json|\.kube/config|\.config/gh/hosts\.yml)([^A-Za-z0-9_]|$)|(^|[^A-Za-z0-9_.])\.ssh([._-][A-Za-z0-9_.-]*)?(/+(\*|\.|[^A-Za-z0-9_./]|$)|[^A-Za-z0-9_./]|$)'

# Env-file loader flags whose argument is loaded into a subprocess
# environment rather than printed: Deno/Docker/podman/docker compose
# `--env-file`, Node's `--env-file-if-exists`, and pytest-dotenv's
# `--envfile`. Consumed only by _lib_strip_env_file_flag_args below.
_LIB_ENV_FILE_FLAG_REGEX='--env-file-if-exists|--env-file|--envfile'

# Strips a `.env`-shaped argument to one of the flags above from $1, so
# deny-credential-bash-reads.sh can re-scan the result and downgrade a match
# caused only by that flag argument to an allow. Only ever called on text
# that already matched _LIB_CREDENTIAL_PATH_REGEX -- the caller's
# scan-then-strip-then-re-scan ordering is what makes this fail-closed, not
# anything in this function. Precondition: expects shell-quote-stripped input
# (via _lib_strip_shell_quotes first, as the sole caller does) -- a quote
# character adjacent to the argument breaks the terminator match, so calling
# this directly on raw quoted text (e.g. `--env-file="t/.env"`) silently no-ops
# the strip (fails closed, not a bypass, but surprising to a future caller).
# Two substitutions (the `=` and space argument forms), each: left-anchored
# to start-of-string or whitespace, re-emitted via \1 so a strip can't join
# two previously-separated tokens; requires the argument's basename to be
# `.env`-shaped, optionally with one dotted suffix (`.env.production`, ...) --
# `--env-file ~/.netrc`/`~/.aws/credentials`/etc. all still deny, since none
# of those arguments is `.env`-shaped; and requires the argument run to
# terminate at whitespace or a shell metacharacter (`; & | < > ( ) $` or a
# backtick), not whitespace alone, so a following credential token (as in
# `--env-file=t/.env;cat </foo/.netrc`) survives into the re-scan.
# Case-sensitive (no `-i`): errs toward leaving `--ENV-FILE=...` denied
# rather than risking a silent case-insensitive-filesystem bypass.
# Looped to a fixed point: a single pass consumes a stripped flag's own
# whitespace terminator, which is the same character the next flag's left
# anchor needs, so back-to-back `--env-file=... --env-file=...` occurrences
# would otherwise leave every occurrence after the first unstripped. Each
# pass that changes the string strictly shortens it, so this terminates.
# On any sed failure (a non-zero exit -- e.g. BSD sed on an invalid UTF-8
# byte under a UTF-8 locale, which exits 1 emitting nothing), returns $1
# unchanged rather than a partial/empty result, so the caller's re-scan
# still sees the original credential-shaped text and denies.
_lib_strip_env_file_flag_args() {
  local text prev stripped
  text="$1"
  while :; do
    prev="$text"
    if ! stripped=$(printf '%s' "$text" | sed -E \
      -e "s/(^|[[:space:]])($_LIB_ENV_FILE_FLAG_REGEX)=[^[:space:];&|<>()\$\`]*\.env(\.[A-Za-z0-9_-]+)?([[:space:];&|<>()\$\`]|\$)/\1 /g" \
      -e "s/(^|[[:space:]])($_LIB_ENV_FILE_FLAG_REGEX)[[:space:]]+[^[:space:];&|<>()\$\`]*\.env(\.[A-Za-z0-9_-]+)?([[:space:];&|<>()\$\`]|\$)/\1 /g"); then
      printf '%s' "$1"
      return 0
    fi
    text="$stripped"
    [ "$text" = "$prev" ] && break
  done
  printf '%s' "$text"
}

# Basenames under a `.ssh`-shaped directory that are safe to read (never
# private-key material): the three conventional non-secret files, and
# anything ending `.pub` (a public key). Consumed only by
# _lib_has_unsafe_ssh_dir_reference below.
_LIB_SSH_SAFE_BASENAME_REGEX='^(authorized_keys|known_hosts|known_hosts\.old|config)$|\.pub$'

# Deny-by-default counterpart to _LIB_CREDENTIAL_PATH_REGEX's .ssh group: extracts every apparent named-file reference under a `.ssh` or `.ssh`-backup-suffixed directory (`~/.ssh/deploy_key`, `~/.ssh.bak/id_rsa`, `~/.ssh/subdir/deploy_key`, ...) from $1, and returns success (0) if ANY extracted leaf basename is not on the safe allowlist above. Mirrors deny-env-reads.sh's allowlist design (deny by default under the directory, allow only documented-safe names) rather than enumerating every unsafe key basename, which doesn't scale — a custom-named key (`deploy_key`, `github_actions_key`) has no fixed shape to enumerate.
# Each candidate is the whole shell-word remainder after `.ssh/` (may itself
# contain further `/`-nesting). A trailing `/` is NOT treated as proof this
# is a directory reference rather than a named file: `tar czf x
# ~/.ssh/deploy_key/` (BSD tar) still archives the file's full content
# despite the trailing slash, so special-casing it as always-safe would
# reopen the exact bypass this function exists to close. The basename is
# checked the same way with or without a trailing slash -- a legitimate
# directory reference (`~/.ssh/sockets/`, a ControlMaster socket dir) is an
# accepted false positive here, same as this hook family's other documented
# over-denial residuals (e.g. the `grep id_rsa` search-pattern residual).
_lib_has_unsafe_ssh_dir_reference() {
  local text="$1" candidate base
  while IFS= read -r candidate; do
    [ -z "$candidate" ] && continue
    # A `..` path segment anywhere in the candidate is unsafe outright,
    # without attempting to resolve it -- this function only ever inspects
    # the trailing string segment as a basename, so `.ssh/deploy_key/../
    # deploy_key.pub` would otherwise read as the safe basename
    # `deploy_key.pub` while the string still names `deploy_key`. Mirrors
    # _lib_realpath_m's own `..`-rejection precedent elsewhere in this file.
    case "/${candidate}/" in
      */../*) return 0 ;;
    esac
    base="${candidate%/}"
    base="${base##*/}"
    if ! printf '%s' "$base" | grep -qEi "$_LIB_SSH_SAFE_BASENAME_REGEX"; then
      return 0
    fi
  done < <(printf '%s' "$text" | grep -oEi '\.ssh([._-][A-Za-z0-9_.-]*)?/[^[:space:]"'"'"']+')
  return 1
}

# Credential-shaped VALUE patterns, sourced by redact-credential-values.sh (jq gsub) and deny-pii-in-commits.sh's credential-value sub-check (grep -E). Must compile under both POSIX ERE and jq's Oniguruma engine, so only dialect-neutral syntax is used.
# Token prefixes (ghp_/gho_/ghu_/ghs_/ghr_ classic and github_pat_ fine-grained) per GitHub's "About authentication to GitHub" docs. The {20,} length floor is NOT vendor-grounded — chosen low enough that a genuine token is never missed, not a verified minimum.
# AKIA (long-term access key) and ASIA (temporary/STS access key) prefixes per AWS's "IAM identifiers" doc (Understanding unique ID prefixes table). The 16-character suffix length is the widely-observed convention for these IDs, not independently vendor-confirmed for this exact length — same non-verified-minimum caveat as the GitHub {20,} floor above.
# The PEM alternative matches only the BEGIN header line: grep -E is line-oriented and can't match across a newline, so a header-only form is what lets deny-pii-in-commits.sh detect a PEM key at commit time at all. See _LIB_PEM_PRIVATE_KEY_BLOCK_REGEX below for the full-block counterpart.
_LIB_CREDENTIAL_VALUE_REGEX='(gh[opsur]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|(AKIA|ASIA)[A-Z0-9]{16}|-----BEGIN[A-Z ]*PRIVATE KEY-----)'

# Redaction-only counterpart to the PEM alternative above: matches the full PEM block (BEGIN through END), not just the header line, since redact-credential-values.sh must strip the actual base64 key body via jq's whole-string gsub, which (unlike grep -E) can match across embedded newlines.
# Body class excludes `-` so a greedy match stops at the first END footer rather than consuming past it; [:space:] (not `.`) lets the match span embedded newlines under Oniguruma without a dot-matches-newline flag.
_LIB_PEM_PRIVATE_KEY_BLOCK_REGEX='-----BEGIN[A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=[:space:]]*-----END[A-Z ]*PRIVATE KEY-----'

# _lib_warn_skipped_credential_pattern FILE LINENO
# One source of truth for the diagnostic _lib_redact_credential_shaped_strings
# emits on both its skip paths (batch-success and per-pattern fallback) below
# -- both branches print the same 4-line message, so a change to its wording
# only needs to happen once.
_lib_warn_skipped_credential_pattern() {
  local file="$1" lineno="$2"
  printf '_lib_redact_credential_shaped_strings: skipping unparseable pattern at %s line %d (jq could not compile it as a regex) — built-in credential redaction is unaffected, but this addition is not being applied.\n' \
    "$file" "$lineno" >&2
}

# _lib_redact_credential_shaped_strings JSON
# Replaces every credential-shaped string anywhere in JSON's value tree
# (the PEM block/header, GitHub token prefixes, an AWS access key ID, plus
# any optional additions from credential-value-patterns.md) with
# [REDACTED-CREDENTIAL], regardless of field name -- extracted from
# redact-credential-values.sh's original inline pattern-assembly-and-walk
# so a second caller doesn't duplicate this security-sensitive logic.
# Echoes redacted JSON on success; on any jq resolution/parse failure emits
# nothing and returns non-zero, so a caller's `[ -n "$result" ]` guard
# treats "redaction failed" the same as "nothing to act on" rather than
# silently passing the unredacted input through.
# Invoked by redact-credential-values.sh, which runs on every
# Bash|Read|WebFetch|Grep|Task PostToolUse -- cost scales with how many
# custom patterns a user has added to credential-value-patterns.md.
_lib_redact_credential_shaped_strings() {
  local json="$1"

  # Full PEM block first: Oniguruma takes the first alternative that matches at each position, not the longest, so ordering it before the header-only PEM alternative is what makes gsub prefer redacting the whole key body when a complete block is present.
  local credential_value_pattern="${_LIB_PEM_PRIVATE_KEY_BLOCK_REGEX}|${_LIB_CREDENTIAL_VALUE_REGEX}"
  # Optional user additions: ~/.claude/credential-value-patterns.md, one `<label>: <regex>` line per pattern (same grammar as deny-pii-in-commits.sh's pii-patterns.md, minus `exclude:`).
  # Union, not swap: $(_lib_config_dir)'s copy wins if present, else the legacy $HOME/.claude location -- keeps an already-armed CLAUDE_CONFIG_DIR user's guard live.
  # An unresolvable config dir leaves this at the legacy path; this is an opt-in guard, not a gate, so resolver failure must not disable it.
  local credential_value_patterns_file="${HOME}/.claude/credential-value-patterns.md"
  local config_dir
  if config_dir=$(_lib_config_dir) && [ -f "$config_dir/credential-value-patterns.md" ]; then
    credential_value_patterns_file="$config_dir/credential-value-patterns.md"
  fi
  if [ -f "$credential_value_patterns_file" ] && [ -r "$credential_value_patterns_file" ]; then
    local addition_lineno line addition_value
    local -a addition_linenos=() addition_values=()
    while IFS=$'\t' read -r addition_lineno line; do
      case "$line" in
        *:*) ;;
        *) continue ;;
      esac

      addition_value="${line#*:}"
      addition_value="${addition_value#"${addition_value%%[![:space:]]*}"}"
      [ -n "$addition_value" ] || continue

      addition_linenos+=("$addition_lineno")
      addition_values+=("$addition_value")
    done < <(_lib_config_lines "$credential_value_patterns_file")

    if [ "${#addition_values[@]}" -gt 0 ]; then
      local batch_i batch_rows batch_status batch_output
      batch_rows=""
      batch_i=0
      while [ "$batch_i" -lt "${#addition_values[@]}" ]; do
        batch_rows="${batch_rows}${addition_linenos[$batch_i]}"$'\t'"${addition_values[$batch_i]}"$'\n'
        batch_i=$((batch_i + 1))
      done

      # Validates every addition in one jq call instead of one fork per
      # pattern. An unparseable pattern makes jq's test() throw; per-row
      # try/catch isolates that failure to the offending line. Contrast the
      # combined gsub call below, which fails wholesale on the same error.
      # Reads "<lineno>\t<pattern>" rows from stdin (the same tab-delimited
      # shape _lib_config_lines produces) and emits "1\t<pattern>" for a
      # pattern that compiles or "0\t<lineno>" for one that doesn't.
      # shellcheck disable=SC2016 # single-quoted on purpose: $p and $l are jq variable bindings, not shell variables; double-quoting would expand them in the shell before jq sees them.
      batch_output=$(printf '%s' "$batch_rows" | _lib_jq -R -n -r '
        [inputs | select(length > 0) | {lineno: .[0:(index("\t"))], pattern: .[(index("\t")+1):]}]
        | .[]
        | .pattern as $p
        | .lineno as $l
        | if (try (("" | test($p)) | true) catch false)
          then "1\t\($p)"
          else "0\t\($l)"
          end
      ' 2>/dev/null)
      batch_status=$?

      if [ "$batch_status" -eq 0 ]; then
        local valid_flag payload batch_parsed_rows=0
        local credential_value_pattern_before_batch="$credential_value_pattern"
        # Safe to re-parse on a bare tab because _lib_config_lines trims
        # every line first, so no addition_value can carry a leading tab.
        while IFS=$'\t' read -r valid_flag payload; do
          [ -z "$valid_flag" ] && continue
          batch_parsed_rows=$((batch_parsed_rows + 1))
          if [ "$valid_flag" = "1" ]; then
            credential_value_pattern="${credential_value_pattern}|${payload}"
          else
            _lib_warn_skipped_credential_pattern "$credential_value_patterns_file" "$payload"
          fi
        done <<< "$batch_output"
        # Row-count parity: a batch call that exits 0 but returns fewer rows
        # than it was given (truncated output) must not be trusted -- undo
        # its partial additions and fall back to per-pattern validation
        # instead of silently under-applying the rest.
        if [ "$batch_parsed_rows" -ne "${#addition_values[@]}" ]; then
          credential_value_pattern="$credential_value_pattern_before_batch"
          batch_status=1
        fi
      fi

      if [ "$batch_status" -ne 0 ]; then
        # The batched call itself failed (jq crashed, timed out, or errored
        # outright) or returned a row count that doesn't match what it was
        # given, not just one pattern failing to compile within it -- fall
        # back to validating each addition individually so only the
        # malformed pattern(s) are skipped, not every custom addition.
        batch_i=0
        while [ "$batch_i" -lt "${#addition_values[@]}" ]; do
          addition_value="${addition_values[$batch_i]}"
          addition_lineno="${addition_linenos[$batch_i]}"
          # shellcheck disable=SC2016 # single-quoted on purpose: $pattern is a jq --arg binding, not a shell variable; double-quoting would expand it in the shell before jq sees it.
          if ! _lib_jq -n --arg pattern "$addition_value" '"" | test($pattern)' >/dev/null 2>&1; then
            _lib_warn_skipped_credential_pattern "$credential_value_patterns_file" "$addition_lineno"
          else
            credential_value_pattern="${credential_value_pattern}|${addition_value}"
          fi
          batch_i=$((batch_i + 1))
        done
      fi
    fi
  fi

  local redacted
  # shellcheck disable=SC2016 # single-quoted on purpose: $pattern is a jq --arg binding, not a shell variable; double-quoting would expand it in the shell before jq sees it.
  redacted=$(printf '%s' "$json" | _lib_jq -c --arg pattern "$credential_value_pattern" \
    'walk(if type == "string" then gsub($pattern; "[REDACTED-CREDENTIAL]") else . end)' 2>/dev/null)
  [ -n "$redacted" ] || return 1
  printf '%s' "$redacted"
}

# Six structural-shape detectors for content that can identify a specific
# machine, person, or private project without naming it directly. Sourced by
# deny-private-project-refs.sh's always-on structural scan. POSIX ERE, one
# constant per detector so a match can be reported by label rather than
# collapsed into one alternation.

# An RFC 1918 private-range (10/8, 172.16/12, 192.168/16) or RFC 1122
# §3.2.1.3 loopback (127/8) IPv4 literal; a public IPv4 no longer matches.
# Every octet position is prefixed `0*` to also match zero-padded forms
# (e.g. `010.000.000.001`).
_LIB_IPV4_LITERAL_REGEX='(0*10\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|0*172\.0*(1[6-9]|2[0-9]|3[01])\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|0*192\.0*168\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|0*127\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.0*(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))'

# A path segment naming the SSH config directory, or a bare/boundary-
# delimited `id_<algorithm>` SSH key filename (the four OpenSSH default key
# algorithms). The config-directory alternative is written as a bracket
# expression (`[.]`), not `\.`, purely to keep this constant's own source
# line from containing the literal substring this same detector matches —
# `\.` and `[.]` are equivalent POSIX ERE for a literal dot, so this changes
# no matching behavior; without it, committing this line would trip the
# detector it defines.
_LIB_SSH_KEY_PATH_REFERENCE_REGEX='([.]ssh/|(^|[^A-Za-z0-9_])id_(rsa|dsa|ecdsa|ed25519)([^A-Za-z0-9_]|$))'

# A /Users/<name> or /home/<name> home-rooted filesystem path.
_LIB_HOME_ROOTED_PATH_REGEX='(/Users/[A-Za-z0-9_.-]+|/home/[A-Za-z0-9_.-]+)'

# A 32+ contiguous hex-char run, or a UUID-shaped (8-4-4-4-12) hex sequence.
_LIB_LONG_HEX_IDENTIFIER_REGEX='([0-9a-fA-F]{32,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'

# A hostname ending in a non-public-suffix TLD. Two alternatives, not one,
# because "local" alone needs a tighter trailing boundary than the other
# five words: "local" is both a legitimate zeroconf/mDNS TLD (printer[.]local)
# and a ubiquitous per-machine-override filename convention (.env[.]local,
# settings.local.json, docker-compose.local.yml), so its boundary excludes a
# "." *only when that dot is itself followed by another identifier
# character* -- without that qualifier, "settings.local.json" (this repo's
# own documented convention) false-positives, since "local" followed by
# another dot-segment ("json") reads as a boundary under a looser class even
# though "local" isn't ending anything there. Qualifying the exclusion this
# way (rather than excluding every trailing dot outright) keeps a sentence-
# final period after a real "*.local" hostname ("Deployed to host[.]local.")
# matching: that trailing dot isn't followed by another identifier character,
# so it still counts as a boundary. The other five words (internal, corp,
# lan, intranet, private) keep the original, looser boundary: real
# corp-internal hostnames commonly take the FQDN shape
# `host[.]corp[.]<company>[.]com`, where the TLD-like word is a subdomain
# label followed by more dot-segments, not the literal string end --
# narrowing their boundary the same way "local" needed would stop catching
# exactly that shape. Bracket-expression dots (`[.]`), not literal ones, in
# this comment's illustrative genuinely-matching examples above, the same
# trick the SSH-key-path detector's own comment above uses -- otherwise this
# comment would trip the very detector it documents.
_LIB_INTERNAL_HOSTNAME_REGEX='[A-Za-z0-9.-]+\.(internal|corp|lan|intranet|private)([^A-Za-z0-9_-]|$)|[A-Za-z0-9.-]+\.local([^A-Za-z0-9_.-]|\.([^A-Za-z0-9]|$)|$)'

# A `#`-prefixed lowercase-hyphenated Slack-channel shape.
# - Excludes all-digit runs so a plain GitHub issue reference (e.g. issue
#   #421) doesn't false-positive.
# - The `#` must be reachable from a valid start position, across a run
#   that excludes parens, braces, and whitespace.
#   - Valid start positions: line start, whitespace, a close-paren, a
#     close-brace, or an open-paren not immediately preceded by `]`.
#   - The run may still contain another `#`.
# - The single rule underlying every case above: no `{` may appear
#   between the start position and the channel `#`. This is what excludes
#   bash parameter-expansion syntax (`${var#<pattern>}`,
#   `${var##<pattern>}`) and array-length syntax (`${#array[@]}`), since
#   in both a `{` sits between the run's start and the `#`.
# - `}` is a valid start position: without it, a real mention immediately
#   after a closed brace (e.g. `${x}#<slug>`, or a `}#<slug>`-shaped CSS
#   selector) would be missed, unlike `${var#<pattern>}` where `}` comes
#   after the `#`.
# - A bare `#` is not a valid start position: it can't be distinguished
#   from the `#` inside `${var#<pattern>}`. Allowing `#` inside the run
#   (above) still catches a "second #<slug> elsewhere in the same match"
#   without needing a bare `#` as its own start point.
# - That reachability predicate is what excludes the `#` inside a markdown
#   inline link's destination (`[text](other-file.md#<anchor-name>)`): the
#   link's own `(` is immediately preceded by `]`, which disqualifies it as
#   a valid start for the destination run.
# - A second `#<slug>` inside the same link destination is still caught
#   by a dedicated alternative: a `]` immediately followed by `(`, then a
#   run to a `#`, is itself a valid start for a later `#<slug>` match.
# - Residual gap: the exemption doesn't require a well-formed `[text]`
#   before the `]`, and it doesn't restrict what the destination run
#   contains. A splice like `](x#<slug>)` right after a bare `]` therefore
#   evades this detector too.
# - Residual gap: a channel reference wrapped as `{#<slug>}` (e.g. a
#   kramdown/Jekyll header-ID anchor, or a deliberate dodge of this gate)
#   evades this detector, because its `{` sits directly before the `#`
#   with nothing rescuing it.
# - Residual gap: a `#<slug>` inside a compact JSON object with no space
#   after the colon (e.g. `{"channel":"#<slug>"}`) evades this detector
#   for the same reason. The spaced form (`{"channel": "#<slug>"}`) still
#   denies, because the space after the colon is its own valid start
#   position that reaches the `#` without crossing the `{`.
# - Residual gap: the `{`-before-`#` exemption is content-blind. It
#   exempts any `${...}`-shaped span between a start position and the
#   channel `#` regardless of what occupies the pattern position, so a
#   real Slack-channel-shaped slug placed there (e.g. `${var#<slug>}`,
#   `${var##<slug>}`) evades this detector too.
# - A CommonMark angle-bracket link destination (`[t](<a b.md#<slug>>)`)
#   stays denied. The space that form permits breaks the destination run,
#   so the scan reaches the `#` past that break as an unparenthesized
#   mention.
# - The link exemption is purely syntactic: it exempts the `#` inside any
#   well-formed `[text](destination#<slug>)` without verifying that
#   `destination` resolves to a real file. This is a separate gap from the
#   smuggled-second-slug residual above, which concerns a second `#`
#   token rather than the first token's unresolved destination.
# - Residual gap: a `{` anywhere in the link destination before the first
#   anchor `#`, with no rescuing whitespace/`)`/`}` before a real second
#   mention, evades the "second slug is caught" guarantee too. The
#   second-slug alternative's own internal run excludes `{` the same way
#   the outer run does, so a `{` there blocks reachability just like it
#   does in the outer run. Same content-blindness root cause as the
#   sibling gaps above.
_LIB_SLACK_CHANNEL_SHAPE_REGEX='((^|[)}[:space:]]|(^|[^]])\()|[]]\([^(){[:space:]]*#)[^(){[:space:]]*#[a-z0-9_-]*[a-z_-][a-z0-9_-]*'

# Single source of truth for read-only git subcommands. Sourced by
# require-worktree-for-git-writes.sh. Closed enumeration — this is a
# security surface, so new entries are added deliberately (a subcommand
# proven read-only against git's own docs), not accreted via "etc./like".
_LIB_READONLY_GIT_SUBCMDS=(
  blame
  branch           # "git branch" lists; creating/deleting takes flags
  cat-file
  check-attr       # read-only attribute lookup
  check-ignore     # read-only gitignore query
  check-mailmap    # read-only mailmap lookup
  check-ref-format # read-only ref name validation
  cherry           # comparison of commits; distinct from the still-denied cherry-pick
  count-objects
  describe
  diff
  diff-files       # read-only plumbing diff
  diff-index       # read-only plumbing diff
  diff-tree        # read-only plumbing diff
  fetch            # updates remote-tracking refs only, not working tree
  for-each-ref
  fsck
  grep             # read-only content search
  help
  log
  ls-files
  ls-remote
  ls-tree
  merge-base
  name-rev
  range-diff
  reflog
  remote
  rev-list
  rev-parse
  shortlog
  show
  show-branch
  show-ref
  status
  symbolic-ref     # "symbolic-ref HEAD <ref>" repoints HEAD but touches neither the
                   # working tree nor the index — acceptable risk under this hook's
                   # working-tree-race threat model, same tier as branch/tag below
  tag              # "git tag" lists; creating takes flags — acceptable risk
  var              # read-only git variable lookup
  verify-commit
  verify-tag
  version
  whatchanged
  worktree         # bootstrap for the whole mechanism — don't block it
)
_lib_readonly_git_subcmds() {
  printf '%s\n' "${_LIB_READONLY_GIT_SUBCMDS[@]}"
}

# Single source of truth for review-only agent identities: the eight
# staff-*/ciso-reviewer personas dispatched by /plan-review and /code-review,
# skill-fidelity-reviewer (dispatched by /ready-for-review) and
# comment-discipline-reviewer (dispatched by /code-review's Change-type
# table) as non-specialist reviewers outside that roster, plus the harness
# built-ins Explore and Plan. Sourced by deny-reviewer-tree-mutation.sh. Closed
# enumeration, same discipline as _LIB_READONLY_GIT_SUBCMDS above — new entries
# are added deliberately (a persona proven review-only, never dispatched to
# write project files), not accreted via "etc./like".
_LIB_REVIEW_ONLY_AGENTS=(
  ciso-reviewer
  comment-discipline-reviewer
  skill-fidelity-reviewer
  staff-analytics-engineer
  staff-backend-engineer
  staff-data-engineer
  staff-frontend-engineer
  staff-platform-engineer
  staff-product-engineer
  staff-sdet
  Explore
  Plan
)
_lib_review_only_agents() {
  printf '%s\n' "${_LIB_REVIEW_ONLY_AGENTS[@]}"
}

# _lib_list_contains VALUE ITEM...
# Boolean exit status: 0 iff VALUE string-equals one of ITEM....
# Matches via `[ "$a" = "$b" ]`, never a `case` glob -- an ITEM containing a
# glob metacharacter (e.g. "code*") matches only that literal string.
# Zero ITEMs (a flattened empty array) is a clean no-match, not a `set -u`
# crash.
# That guarantee covers only this helper's own zero-ITEMs case: an unset
# array expanded via "${arr[@]}" under `set -u` would abort at the call
# site, before _lib_list_contains is even entered, and this helper cannot
# protect against that. Currently latent, not live -- all three call sites'
# arrays below are module-level constants defined by construction, never a
# conditionally-set variable, even though their gate-script callers do run
# under `set -u`.
# Shared by the three membership scans below, each over its own derived
# array.
_lib_list_contains() {
  local value="$1"
  shift
  local item
  for item in "$@"; do
    [ "$value" = "$item" ] && return 0
  done
  return 1
}

# _lib_is_review_only_agent AGENT_TYPE
# Returns 0 (true) iff AGENT_TYPE exactly matches an entry in
# _LIB_REVIEW_ONLY_AGENTS. Empty input (agent_type absent from the
# PreToolUse payload, e.g. the main session) never matches.
_lib_is_review_only_agent() {
  local agent_type="$1"
  [ -n "$agent_type" ] || return 1
  _lib_list_contains "$agent_type" "${_LIB_REVIEW_ONLY_AGENTS[@]}"
}

# Agent identities that may never release a review gate — every review-only
# persona above, plus the code-writer implementer. Sourced by
# enforce-marker-script-shape.sh to deny `marker.sh write` and
# `marker.sh activate` from these callers.
#
# The boundary rests on two different grounds, and conflating them misleads
# whoever adds the next entry:
#   - Every file-backed identity here (code-writer, the staff-* reviewers,
#     ciso-reviewer, skill-fidelity-reviewer, and — as of the Explore.md
#     same-named override — Explore) declares no `Skill` tool in its
#     agents/*.md frontmatter, so it cannot invoke a review skill at all.
#     test_agent_roster.py asserts that mechanically, so granting `Skill` to
#     one of them fails a test rather than silently widening what a subagent
#     can release.
#   - `Plan` is the one harness built-in with no agents/*.md file in this
#     repo, and is understood to carry `Skill` — it ships with the harness, so
#     this repo holds no frontmatter and no registry that could confirm or
#     falsify it. It is listed on mandate (dispatched read-only), which holds
#     either way; do not rewrite this as a tool-absence claim on the strength
#     of the harness behaving one way today.
# Either way a marker write from one of these identities is unearned rather
# than merely unusual, which is what the deny turns on.
#
# Derived from _LIB_REVIEW_ONLY_AGENTS rather than re-enumerated, so a persona
# added there is covered here automatically. `general-purpose` and `claude`
# carry the full tool set and can genuinely run a review skill, so they are
# deliberately absent — that is the documented delegation escape hatch.
_LIB_NO_GATE_RELEASE_AGENTS=(
  "${_LIB_REVIEW_ONLY_AGENTS[@]}"
  code-writer
)
_lib_no_gate_release_agents() {
  printf '%s\n' "${_LIB_NO_GATE_RELEASE_AGENTS[@]}"
}

# _lib_is_no_gate_release_agent AGENT_TYPE
# Returns 0 (true) iff AGENT_TYPE exactly matches an entry in
# _LIB_NO_GATE_RELEASE_AGENTS. Empty input (agent_type absent from the
# PreToolUse payload, e.g. the main session) never matches.
_lib_is_no_gate_release_agent() {
  local agent_type="$1"
  [ -n "$agent_type" ] || return 1
  _lib_list_contains "$agent_type" "${_LIB_NO_GATE_RELEASE_AGENTS[@]}"
}

# Reviewer-persona agents dispatched by /code-review's fan-out, for
# require-architect-consult.sh and log-reviewer-round.sh: every entry in
# _LIB_REVIEW_ONLY_AGENTS except the two harness built-ins Explore and Plan,
# which that array's own header names as such. Derived rather than
# re-enumerated, so a persona added to _LIB_REVIEW_ONLY_AGENTS is covered
# here automatically.
_LIB_REVIEWER_PERSONA_AGENTS=()
for _lib_reviewer_persona_candidate in "${_LIB_REVIEW_ONLY_AGENTS[@]}"; do
  case "$_lib_reviewer_persona_candidate" in
    Explore | Plan) continue ;;
  esac
  _LIB_REVIEWER_PERSONA_AGENTS+=("$_lib_reviewer_persona_candidate")
done
unset _lib_reviewer_persona_candidate
_lib_reviewer_persona_agents() {
  printf '%s\n' "${_LIB_REVIEWER_PERSONA_AGENTS[@]}"
}

# _lib_is_reviewer_persona AGENT_TYPE
# Returns 0 (true) iff AGENT_TYPE exactly matches an entry in
# _LIB_REVIEWER_PERSONA_AGENTS. Empty input (subagent_type absent from the
# PreToolUse/PostToolUse payload) never matches.
_lib_is_reviewer_persona() {
  local agent_type="$1"
  [ -n "$agent_type" ] || return 1
  _lib_list_contains "$agent_type" "${_LIB_REVIEWER_PERSONA_AGENTS[@]}"
}

# Round-state cap shared by require-architect-consult.sh (the read side,
# which denies once a genuinely new state arrives at the cap) and
# log-reviewer-round.sh (the write side, which never appends past it) --
# one constant rather than two literal "2"s that would have to be kept in
# sync by hand. The case study's own recommendation is to fire at *entry to
# round 3*, not round 1 -- "firing after round 1 would catch roughly half
# of all PRs to address a 14% tail"
# (docs/case-studies/opus-frontload-review-rounds.md:260-263) -- so the cap
# is 2 recorded rounds, with the 3rd distinct state tripping the gate.
_LIB_REVIEWER_ROUND_STATE_CAP=2
# Pilot override: see _lib_reviewer_round_state_cap below and
# docs/design-decisions/round2-consult-trigger-pilot.md.

# _lib_reviewer_round_state_cap
# Prints the round-state cap shared by require-architect-consult.sh
# (read side) and log-reviewer-round.sh (write side).
# Returns 1 if <config-dir>/.round-consult-round2-pilot exists, else
# $_LIB_REVIEWER_ROUND_STATE_CAP.
# Contract: always echoes a valid integer to stdout, including on an
# unresolvable config dir -- both call sites consume this via `$(...)` into
# an integer comparison that would error ("integer expression expected") on
# empty stdout. Unlike _lib_round_consult_gate_disabled's boolean-exit-code
# shape below. Zero-arity, same machine-global scope as the kill switch
# below.
_lib_reviewer_round_state_cap() {
  local config_dir
  if config_dir=$(_lib_config_dir) \
    && [ -n "$(_lib_capped find "$config_dir/.round-consult-round2-pilot" -maxdepth 0 2>/dev/null)" ]; then
    printf '1\n'
  else
    printf '%s\n' "$_LIB_REVIEWER_ROUND_STATE_CAP"
  fi
}

# _lib_reviewer_round_state_key REPO_ROOT
# Prints "<repo-hash>.<branch-hash>" for require-architect-consult.sh's and
# log-reviewer-round.sh's shared per-branch state and latch paths. Returns 1
# with no stdout when REPO_ROOT is empty or HEAD is detached (no branch name
# to key on) -- callers must fail open on either, per this gate's
# allow-on-state-failure posture (see require-architect-consult.sh's header).
#
# Determinism contract (read side [require-architect-consult.sh] and write
# side [log-reviewer-round.sh] must agree byte-for-byte, or the gate wedges):
# Branch name comes from `git symbolic-ref -q --short HEAD`, hashed via
# _lib_hash_diff_text. _marker_lib_repo_hash hashes the repo half via the
# same function, so both halves are produced identically regardless of
# caller.
_lib_reviewer_round_state_key() {
  local repo_root="$1"
  [ -n "$repo_root" ] || return 1
  local branch
  branch=$(_lib_capped git -C "$repo_root" symbolic-ref -q --short HEAD 2>/dev/null)
  [ -n "$branch" ] || return 1
  local repo_hash branch_hash
  repo_hash=$(_marker_lib_repo_hash "$repo_root")
  branch_hash=$(_lib_hash_diff_text "$branch")
  [ -n "$repo_hash" ] && [ -n "$branch_hash" ] || return 1
  printf '%s.%s' "$repo_hash" "$branch_hash"
}

# _lib_reviewer_round_state_value REPO_ROOT
# Prints "<head-sha> <staged-diff-sha256>" -- the one-line-per-round-state
# unit each entry in <config-dir>/.reviewer-round-state.d/<key> holds. Returns
# 1 with no stdout when REPO_ROOT is empty, HEAD is unresolvable (no commits
# yet), or the staged-diff git call itself failed or was capped-killed --
# caught by _lib_staged_diff_hash's own PIPESTATUS check on that exact call,
# not a separate probe -- callers must fail open, same posture as
# _lib_reviewer_round_state_key above.
# An empty staged diff is a legitimate round state here (unlike marker.sh's
# write-arm callers), so it hashes through like content -- only a failed or
# killed git call is rejected.
#
# The diff-hash half routes through _lib_gate_diff_base/_lib_staged_diff_hash,
# so a mid-merge round-state value covers the same novel content the other
# marker preimages do rather than the whole pre-merge diff. When BASE is
# non-empty and the base-relative staged diff is itself empty, the diff-hash
# half binds to BASE's own identity ("round-state-empty-base:$base") instead
# of falling through to sha256(""), matching _lib_code_review_marker_value's
# own reasoning: sha256("") is a fixed value independent of which base
# produced it, so a round-state entry recorded during one trusted
# operation's degenerate empty-diff case would otherwise validate a
# different, forged base landing on the same empty result. This is the
# diff-hash half only -- it does not reach _lib_reviewer_round_state_key's
# separate branch-half gap, where a detached HEAD mid-rebase disarms the
# round-3 consult gate for the operation's duration regardless of this value.
#
# Capped-git-call count varies by branch: 3 in the common case outside any
# in-progress merge/rebase/cherry-pick/revert (HEAD rev-parse, the gitdir
# rev-parse inside _lib_gate_diff_base, the diff itself), up to 8
# mid-operation (adds the ref-file cat, up to two merge-base anchor checks,
# the merge-tree computation, and the tree verification, all inside
# _lib_gate_diff_base) -- a future added capped call here pushes the
# mid-operation ceiling higher still.
#
# Determinism contract (read side and write side must agree byte-for-byte):
# both halves are captured into variables and tested for emptiness rather
# than trusted as a pipeline's exit status, matching _lib_active_plan_hash's
# own documented reason -- this keeps the contract independent of the
# caller's shell options (a caller sourcing this under `set -u` with no
# `pipefail` would otherwise see a failed `git diff` silently yield an
# empty-but-"successful" sha256sum of nothing).
_lib_reviewer_round_state_value() {
  local repo_root="$1"
  [ -n "$repo_root" ] || return 1
  local head_sha diff_hash base
  # `--verify` is load-bearing, not stylistic: bare `rev-parse HEAD` on an
  # unborn branch (no commits yet) echoes the literal string "HEAD" back to
  # STDOUT while exiting non-zero, so a caller checking only for a non-empty
  # captured value -- as this function otherwise would -- reads that as a
  # genuine (bogus) sha instead of the "no HEAD yet" failure it actually is.
  # `--verify` suppresses that echo-back-on-failure behavior, printing
  # nothing on failure (_lib_active_plan_files uses the identical flag pair
  # for the same reason, `git rev-parse --verify -q HEAD`).
  head_sha=$(_lib_capped git -C "$repo_root" rev-parse --verify -q HEAD 2>/dev/null)
  [ -n "$head_sha" ] || return 1
  base=$(_lib_gate_diff_base "$repo_root")
  if [ -n "$base" ]; then
    local relative_diff diff_status
    relative_diff=$(_lib_capped git -C "$repo_root" diff --cached "$base" 2>/dev/null)
    diff_status=$?
    [ "$diff_status" -eq 0 ] || return 1
    if [ -z "$relative_diff" ]; then
      diff_hash=$(_lib_hash_diff_text "round-state-empty-base:$base") || return 1
      printf '%s %s' "$head_sha" "$diff_hash"
      return 0
    fi
  fi
  diff_hash=$(_lib_staged_diff_hash "$repo_root" "$base") || return 1
  printf '%s %s' "$head_sha" "$diff_hash"
}

# _lib_round_consult_gate_disabled
# Returns 0 (true) iff <config-dir>/.round-consult-gate-disabled is present
# -- the presence-only kill switch for require-architect-consult.sh, same
# shape as _lib_permission_prompt_tracking_active above. Zero-arity: this
# sentinel is machine-global with nothing repo- or session-scoped to look
# up. Fails toward NOT disabled (i.e. the gate stays armed) on an
# unresolvable config dir, matching every other opt-in-sentinel check in
# this file's fail direction.
_lib_round_consult_gate_disabled() {
  local config_dir
  config_dir=$(_lib_config_dir) || return 1
  [ -f "$config_dir/.round-consult-gate-disabled" ] || return 1
  return 0
}

# Shared bounded-retry count for _lib_append_line_locked below, used by both
# review-ledger.sh and log-reviewer-round.sh. Small and fixed: this runs
# synchronously inside a hook or CLI script, so the worst-case added latency
# is bounded retries * the sleep below.
_LIB_APPEND_LOCK_RETRIES=5

# _lib_append_line_locked FILE LOCK_FILE LINE
# Sets a bare `trap ... EXIT` to release its lock, which per this repo's
# shell-script-conventions rule silently clobbers any other EXIT trap
# already registered in the calling process -- a future caller sharing this
# primitive must ensure no other EXIT trap is active in the same process.
# Shared by review-ledger.sh and log-reviewer-round.sh, which each need the
# identical check-then-append critical section against a different state
# file. Acquires a same-directory noclobber lock
# (bash `set -o noclobber`, the idiom _lib_worktree_collision_guard already
# establishes in this repo) around the check-then-append: no-ops if LINE
# already exists verbatim in FILE, else appends it. The lock file's content
# is the holder's PID. A lock whose PID is dead is evicted and retried
# immediately, rather than waiting out every retry against a crashed
# holder. This is the same PID-liveness eviction _lib_active_bypass_marker_live
# uses for its own markers. It matters more here than at review-ledger.sh's
# own call site, since a PostToolUse hook is more exposed to being killed
# mid-lock by the harness's own hook timeout than a skill-invoked CLI
# script. Falls through to an unlocked append after
# _LIB_APPEND_LOCK_RETRIES failed acquisitions rather than blocking -- a
# duplicate line from a lost race is a low-consequence outcome (an inflated
# round count), not data loss. The lock is released via the EXIT trap noted
# above, so it clears whether the append succeeds or fails.
_lib_append_line_locked() {
  local file="$1" line="$3"
  # Deliberately not `local`: the EXIT trap below evaluates this lazily at
  # script-exit time, after this function has already returned, and any
  # `local` binding of the same name would be out of scope by then.
  _LIB_APPEND_LOCK_PATH="$2"
  local attempt=0 stored_pid
  while [ "$attempt" -lt "$_LIB_APPEND_LOCK_RETRIES" ]; do
    if (set -o noclobber; printf '%s\n' "$$" > "$_LIB_APPEND_LOCK_PATH") 2>/dev/null; then
      trap 'rm -f "$_LIB_APPEND_LOCK_PATH"' EXIT
      break
    fi
    stored_pid=$(cat "$_LIB_APPEND_LOCK_PATH" 2>/dev/null | tr -d '[:space:]')
    if [[ "$stored_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$stored_pid" 2>/dev/null; then
      # Dead holder: evict now and retry acquisition on the very next
      # iteration, with no sleep -- this is what makes eviction prompt
      # rather than waiting out the remaining retries.
      rm -f "$_LIB_APPEND_LOCK_PATH" 2>/dev/null
      attempt=$((attempt + 1))
      continue
    fi
    attempt=$((attempt + 1))
    sleep 0.05
  done
  if [ -f "$file" ] && grep -qFx -e "$line" -- "$file" 2>/dev/null; then
    # A dedup no-op must still count as activity on this file's own mtime,
    # or a long-running branch's later no-op append leaves a stale mtime
    # for a directory-wide 30-day sweep to delete out from under it.
    touch -- "$file" 2>/dev/null
    return 0
  fi
  printf '%s\n' "$line" >> "$file"
}

# _lib_resume_context_tmpdir_root
# The one home for resume-context.sh's temp-dir-root formula
# (${RESUME_CONTEXT_TMPDIR:-${TMPDIR:-/tmp}}), shared by the move itself and
# by _lib_resume_context_index_dir below -- a second independent copy of
# this formula would silently split the index from the destinations it
# indexes on any future rename.
_lib_resume_context_tmpdir_root() {
  printf '%s\n' "${RESUME_CONTEXT_TMPDIR:-${TMPDIR:-/tmp}}"
}

# _lib_resume_context_index_dir
# Prints the path to the per-uid consumed-continuity-file index directory
# (<tmpdir-root>/resume-context-index-$EUID), creating and
# permission-guarding it first. Rows live in day-files named
# consumed.<UTC YYYY-MM-DD>.tsv inside it:
#   - the writer composes today's name
#   - the reader globs consumed.*.tsv in name order (chronological, since
#     the names are fixed-width ASCII digits)
#   - retention deletes whole files by mtime
# That naming contract is stated once, here, and enforced across the
# writer/reader boundary by their shared end-to-end test rather than by a
# third helper.
# Returns 1, printing nothing, if the directory is a symlink or not owned
# by $EUID. /tmp's predictable per-uid path makes it plantable by another
# local user, so ownership -- not the chmod below -- is the actual control.
# mkdir(2)'s atomicity plus the -O check closes the race. A successful call
# always yields a directory owned by the calling EUID. A failed call is
# caught by `[ -O ]` evaluating false, since forging EUID ownership needs
# privilege an attacker doesn't have. The different-owner pre-creation case
# this closes can't be reproduced in single-uid CI -- see
# TestLibResumeContextIndexDir's docstring in test_lib.py for the full
# POSIX argument, recorded there rather than as an executable test.
# Also returns 1 if the tmpdir root is other- or group-writable without the
# sticky bit -- see the guard below for why.
_lib_resume_context_index_dir() {
  local dir tmpdir_root
  tmpdir_root="$(_lib_resume_context_tmpdir_root)"
  dir="$tmpdir_root/resume-context-index-$EUID"
  # A world- or group-writable tmpdir root needs the sticky bit to stop
  # another local user from unlinking/renaming our directory entry in the
  # mkdir-to-chmod window below.
  # This is verified here rather than assumed, since
  # RESUME_CONTEXT_TMPDIR/TMPDIR can point anywhere, not only at a sticky
  # production /tmp.
  # A root writable by neither the "other" nor the "group" permission class
  # has no other uid able to race this entry in the first place, sticky bit
  # or not.
  # That is also what keeps this guard from rejecting pytest's private
  # tmp_path fixture, which is writable by neither class.
  if [ -n "$(find "$tmpdir_root" -maxdepth 0 \( -perm -0002 -o -perm -0020 \) 2>/dev/null)" ] && [ ! -k "$tmpdir_root" ]; then
    return 1
  fi
  # EEXIST here is the expected steady state from the second call onward;
  # left unguarded by design — safe under `set -e` only because this
  # function always runs inside a command substitution (`$(...)`), never
  # called directly. A future direct call would need its own `|| true`.
  ( umask 077; mkdir -- "$dir" ) 2>/dev/null
  [ ! -L "$dir" ] && [ -d "$dir" ] && [ -O "$dir" ] || return 1
  # The repair chmod below closes a check-then-act window against another
  # local user racing our directory entry, now that the guard above has
  # confirmed $tmpdir_root is not other- or group-writable without the
  # sticky bit.
  chmod 700 -- "$dir" 2>/dev/null
  printf '%s\n' "$dir"
}

# _lib_print_recovery_hint DEST
# Prints, to stderr, the reload command a human would run to load a moved
# continuity file back into a session's system prompt. Shared by
# resume-context.sh (after a launch-mode move) and
# find-consumed-continuity-file.sh (after resolving a slug to a still-live
# destination), so the reload string can't drift between the two callers.
# Uses the literal `claude`, not $RESUME_CONTEXT_LAUNCHER: this is a hint
# for a human to run manually, potentially in a later shell/session where
# that override won't be set.
_lib_print_recovery_hint() {
  local dest="$1"
  printf 'reload with: claude --append-system-prompt-file %s\n' "$dest" >&2
}

# _lib_sanitize_for_terminal VALUE
# Prints VALUE with raw control bytes (0x01-0x08, 0x0a-0x1f, 0x7f) stripped.
# Tab (0x09) is preserved: it is a legitimate field separator in at least
# one caller's format, not a byte that caller needs guarding against. See
# docs/scripts.md's find-consumed-continuity-file.sh entry for the shared
# callers, threat model, and the C1/bidi/zero-width residual-scope
# rationale.
# Pure-bash loop, not `tr -d`, so a PATH missing `tr` can't abort this
# always-on-success-path sanitizer.
# `local LC_ALL=C` forces byte-wise indexing (`${value:i:1}`, `${#value}`)
# over VALUE, which need not be valid UTF-8: every UTF-8 continuation and
# lead byte is >=0x80, so this byte-wise indexing never splits a multi-byte
# sequence at a strip point.
_lib_sanitize_for_terminal() {
  local value="$1" out="" i len char
  local LC_ALL=C
  len=${#value}
  for (( i = 0; i < len; i++ )); do
    char="${value:i:1}"
    case "$char" in
      [$'\x01'-$'\x08']|[$'\x0a'-$'\x1f']|$'\x7f') continue ;;
    esac
    out+="$char"
  done
  printf '%s' "$out"
}
