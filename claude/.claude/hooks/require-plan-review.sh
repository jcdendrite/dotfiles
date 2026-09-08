#!/bin/bash
# hook-class: gate
# PreToolUse hook: block Write/Edit/ExitPlanMode when an uncommitted or modified
# plan file exists in .claude/plans/ and no plan-review marker covering that
# exact plan state can be found. Exempt: a Write/Edit/MultiEdit whose own
# target is one of those same plan files, so authoring a plan across
# multiple calls (including a resumed session) is never blocked by the gate
# that exists to force that plan's review.
#
# Globally applied (no opt-in), consistent with require-code-review.sh,
# require-ready-for-review.sh, and require-respond-pr.sh.
# Projects without a .claude/plans/ directory, or where all plan files are
# committed and unmodified (historical), pass through silently — the
# uncommitted-plan check is the built-in filter.
#
# Two-marker pattern:
# - Active marker (~/.claude/.plan-review-active.d/<session_id>):
#   content = Claude session PID. Written by /plan-review at step 0;
#   removed at the deactivation step. Bypasses the gate so the skill's own
#   Write/Edit calls during review don't self-deny; ExitPlanMode is excluded
#   from this bypass — an active marker means review is in progress, not
#   complete, so plan presentation stays blocked. The hook checks PID
#   liveness (kill -0) on each gate hit; dead PIDs are evicted automatically,
#   which handles orphaned markers from sessions that errored before cleanup.
# - Completion marker (~/.claude/plan-review-markers/<repo-hash>.<session_id>):
#   written by /plan-review when the review is clean. Content is the
#   sha256 hash of the active plan file set (paths + contents), computed by
#   _lib_active_plan_hash in _lib.sh. Content-addressed, not
#   existence-checked: this hook recomputes the same hash at gate time and
#   allows only on an exact match, so editing a reviewed plan (including a
#   ledger row) re-arms the gate on the next Write/Edit/ExitPlanMode.
#   Mirrors require-code-review.sh's staged-diff-hash marker.
#   When an active plan cannot be hashed at all (unreadable, vanished),
#   _lib_active_plan_hash exits non-zero and this hook denies with a
#   repair-the-file message rather than allowing — an unhashable plan is
#   an unknown review state, not an absent one.
#   The <session_id> in the filename is a WRITE-side key only: it keeps
#   parallel sessions from overwriting each other's markers. The read below
#   globs across it, because the stored hash — not the filename — is what
#   proves the review covered this state. Reading the session key as an
#   authorization predicate denies a resumed session (new session_id) a review
#   it already completed.
#
# Defense-in-depth: the hook filters its own input by tool name; do not
# rely solely on the settings.json matcher condition.
#
# Exit codes:
#   0      — allow (no opinion)
#   0+JSON — deny (plan is active and no marker holds its current hash)

set -uo pipefail

DENY_GATE_LABEL="plan-review"

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

# Gate Write, Edit, MultiEdit, and ExitPlanMode tool calls.
case "$TOOL_NAME" in
  Write|Edit|MultiEdit|ExitPlanMode) ;;
  *) exit 0 ;;
esac

# Target path already extracted by the shared parse — used both for the
# repo-scope guard below and the deny gate.
TARGET_PATH="$FILE_PATH"

# Resolve the repo from the payload's cwd rather than this hook process's
# ambient cwd, so the marker is keyed to the tree the session is working in.
# Downstream hashing already threads this root (_lib_active_plan_hash), so
# root resolution is the only site that needs converting here.
[ -z "$CWD" ] && CWD="$PWD"

# Not in a git repo — can't check for plan files or key the marker.
REPO_ROOT=$(_lib_capped git -C "$CWD" rev-parse --show-toplevel 2>/dev/null)
if [ -z "$REPO_ROOT" ]; then
  exit 0
fi

# Plan-mode branch: ExitPlanMode carries tool_input.planFilePath naming the
# harness plan-mode file on the very call being gated, so it is hashed fresh
# here with no stored state and checked ahead of the repo-relative plan below.
# Priority matters: a session holding a valid repo-relative marker that opens
# a NESTED plan-mode question must not have that stale marker authorize
# unreviewed plan-mode content — so a non-empty planFilePath decides the call
# outright, on a match or a mismatch, rather than falling through. An absent
# or empty planFilePath (Write/Edit/MultiEdit, or an ExitPlanMode call outside
# plan mode) falls through to the repo-relative check unchanged.
if [ "$TOOL_NAME" = "ExitPlanMode" ]; then
  PLAN_MODE_FILE_PATH=$(printf '%s\n' "$INPUT" | _lib_jq -r '.tool_input.planFilePath // empty')
  if [ -n "$PLAN_MODE_FILE_PATH" ]; then
    # ExitPlanMode's tool description says the approval UI renders from the file named by `planFilePath`, not from `tool_input.plan` directly.
    PLAN_MODE_HASH=$(_lib_capped sha256sum -- "$PLAN_MODE_FILE_PATH" 2>/dev/null | awk '{print $1}')
    if [ -z "$PLAN_MODE_HASH" ]; then
      # Unreadable, missing, or timed out. Fail closed rather than falling
      # through to the repo-relative check -- that would silently re-permit
      # the exact silent-allow bug this branch exists to close whenever a
      # stale repo-relative marker happens to be present.
      emit_deny "cannot read the plan-mode file '$PLAN_MODE_FILE_PATH' named by ExitPlanMode, so the gate cannot tell whether it has been reviewed.

Plan presentation stays blocked until this is fixed — an unreadable plan-mode file is an unknown review state, not an absent one. Repair the file (chmod, or address whatever plan mode did to lose track of it), then retry. Running /plan-review first will fail the same way, since it hashes the same file."
      exit 0
    fi
    if ! CONFIG_DIR=$(_lib_config_dir); then
      emit_deny "could not resolve the Claude Code config directory (CLAUDE_CONFIG_DIR is set to a relative path, or \$HOME is unset/empty)."
      exit 0
    fi
    REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
    if _lib_marker_value_present "$CONFIG_DIR/plan-review-markers" "$PLAN_MODE_HASH" "$REPO_HASH."; then
      exit 0
    fi
    emit_deny "Plan presentation — this session is in harness plan mode and the plan file ExitPlanMode named has no plan-review marker covering its current content.

  Run /plan-review against the plan-mode file before calling ExitPlanMode. The skill records the review in ~/.claude/plan-review-markers/ and plan presentation will be allowed on retry."
    exit 0
  fi
fi

# Cheap, git-free short-circuit before paying for GATE_DIFF_BASE resolution
# below (a rev-parse plus, mid-merge/rebase/cherry-pick/revert, a capped
# merge-tree computation): a repo with no .claude/plans/ directory at all has
# nothing this gate could ever arm on, mirroring _lib_active_plan_files' own
# first check.
if [ ! -d "$REPO_ROOT/.claude/plans" ]; then
  exit 0
fi

# Unlike require-code-review.sh, which pays this cost once per commit, this
# hook pays it on every Write/Edit/MultiEdit/ExitPlanMode call for the whole
# span of an in-progress merge/rebase/cherry-pick/revert -- an accepted,
# materially different cost shape, not a caching bug.
#
# Resolved once for this hook invocation, after the plan-mode branch above
# (which does not consume it), and threaded through both the fast-path guard
# below and the hash computation further down -- a second resolution would
# double the merge-tree cost per gated call for the exact same result, the
# same shape require-code-review.sh and marker.sh status use.
GATE_DIFF_BASE=$(_lib_gate_diff_base "$REPO_ROOT")
GATE_DIFF_BASE_STATUS=$?

# Scope the deny to writes inside this repo. Writes targeting user-home
# directories (~/.claude/plans/), /tmp, or other repos are outside the gate's
# intent — the gate guards this repo's code, not all files on disk.
# Guarded by TOOL_NAME too, so the exclusion is structural rather than
# relying on ExitPlanMode's payload never carrying file_path.
# - Guards on _lib_active_plan_files, not directory existence, so an
#   all-committed repo also skips the realpath forks too.
# - Runs before the hash computation since path shape alone decides the
#   common case.
# - Also the gate's full disarm fast path: nothing active exits 0
#   immediately here -- but only when GATE_DIFF_BASE is also empty. With a
#   trusted base in effect, _lib_active_plan_hash's empty-active-set result
#   is no longer necessarily empty (it binds to the base's own identity
#   instead -- see that function's docstring), so this shortcut's premise
#   ("the hash computation below would independently reach this same empty
#   result") no longer holds and it must fall through instead.
# - Because this runs before the hash computation, an unhashable in-repo
#   active plan does not block an out-of-repo write.
if [ -n "$TARGET_PATH" ] && [ "$TOOL_NAME" != "ExitPlanMode" ]; then
  ACTIVE_PLAN_FILES=$(_lib_active_plan_files "$REPO_ROOT" "$GATE_DIFF_BASE")
  ACTIVE_PLAN_FILES_STATUS=$?
  if [ "$ACTIVE_PLAN_FILES_STATUS" -eq 0 ] && [ -z "$ACTIVE_PLAN_FILES" ] && [ -z "$GATE_DIFF_BASE" ]; then
    # Nothing active and no trusted base: the hash computation below would
    # independently reach this same empty result via its own call to
    # _lib_active_plan_files, so short-circuit here instead of paying for a
    # second enumeration.
    exit 0
  fi
  # - Both a failed enumeration and a non-empty active-file list fall through
  #   to here; a failed enumeration fails closed by proceeding into the checks
  #   below rather than skipping them.
  # - A failed _lib_realpath_m resolution must not feed either check below,
  #   since an empty REAL_TARGET would otherwise satisfy the negative
  #   boundary match and wrongly allow.
  # - On resolution failure the whole block is skipped, falling through to the
  #   hash computation.
  if REAL_REPO=$(_lib_realpath_m "$REPO_ROOT") && REAL_TARGET=$(_lib_realpath_m "$TARGET_PATH"); then
    # Reviewer findings writes are exempt — they land in the gitignored
    # agent-reviews/ directory and are never staged. Blocking them forces the
    # reviewer into a full-inline fallback that loses all context savings.
    # Exact prefix match only: "foo-agent-reviews/" does not satisfy this.
    # _lib_realpath_m resolves .. lexically but not symlinks, so a symlinked repo path can normalize REAL_REPO/REAL_TARGET along different chains and false-deny a legitimate write; same limitation as the repo-boundary check below.
    if [[ "$REAL_TARGET" == "$REAL_REPO"/agent-reviews/* ]]; then
      exit 0
    fi
    # A write whose own target is a plan file this gate hashes is authoring
    # the plan the gate demands a review of. (ExitPlanMode never reaches this
    # block -- see the TARGET_PATH guard above.)
    if _lib_is_repo_plan_file "$REAL_REPO" "$REAL_TARGET"; then
      exit 0
    fi
    if [[ "$REAL_TARGET" != "$REAL_REPO/"* ]]; then
      exit 0
    fi
  fi
fi

# Compute the content-addressed hash of the active plan file set (paths +
# contents; see _lib_active_plan_hash in _lib.sh for the full contract). A
# plan file that is tracked and identical to GATE_DIFF_BASE (HEAD outside any
# trusted in-progress state) is historical and does not contribute to the
# hash. Empty result means no plan is active and no trusted base was in
# effect -- gate disarmed, covering both an absent .claude/plans/ and one
# containing only historical plans. With a trusted base in effect and
# nothing active relative to it, the result is instead bound to the base's
# own identity, so the gate still requires a marker rather than disarming.
# Keep this a top-level assignment. Inside a function, `local VAR=$(...)`
# reports `local`'s exit status (always 0) and would mask the failure; a
# refactor that moves this must split the declaration from the assignment.
if ! CURRENT_HASH=$(_lib_active_plan_hash "$REPO_ROOT" "$GATE_DIFF_BASE"); then
  # A plan is active but could not be hashed; stdout carries the offending
  # path. Fail closed. This deny is deliberately worded differently from the
  # missing-marker deny below: telling the user to run /plan-review here
  # would be circular, since marker.sh hits the identical condition and
  # aborts. This hook gates only Write/Edit/MultiEdit/ExitPlanMode, so Bash
  # stays available to repair the file — point at that escape hatch.
  emit_deny "cannot read the active plan file '$CURRENT_HASH', so the gate cannot tell whether the plan has been reviewed.

This is not a missing review — running /plan-review will fail the same way, because it hashes the same file. Repair the file first, using a Bash command (this gate does not block Bash):

  - Unreadable due to permissions → chmod +r '$CURRENT_HASH'
  - A broken symlink, or a stale file you no longer need → rm '$CURRENT_HASH'
  - Belongs elsewhere → mv it out of .claude/plans/

Then retry. If the file is genuinely gone, the gate disarms on its own."
  exit 0
fi

if [ -z "$CURRENT_HASH" ]; then
  exit 0
fi

# Plans exist — look for a review covering this exact plan state.

# Active-marker bypass: the /plan-review skill is currently running.
# This marker stays strictly session-keyed, unlike the completion marker
# below: it asserts "a review is running in THIS process right now", which is
# a genuinely per-session property, and it is PID-liveness-checked.
# Skip this bypass for ExitPlanMode — the active marker means plan-review
# is in progress (not yet complete), so ExitPlanMode must still be blocked.
# An absent or path-escaping id withholds the bypass, which just means the
# completion-marker check further down decides the gate instead — never less
# safe than the bypass would have been.
if [ "$TOOL_NAME" != "ExitPlanMode" ] \
  && _lib_active_bypass_marker_live_and_touch ".plan-review-active.d" "$SESSION_ID"; then
  exit 0
fi

# Completion-marker check: allow only when some marker's stored hash equals
# the active plan set's current hash. An edit to any active plan since the
# marker was written (including a ledger row) changes CURRENT_HASH, so no
# marker matches and the gate re-arms. That content-addressing is precisely
# what makes reading across the filename's session key safe: the hash proves
# which state was reviewed, so the gate asks "has this plan state been
# reviewed?" rather than "did this session review it?".
#
# Two tiers, for latency. This hook fires on every Write/Edit/MultiEdit/
# ExitPlanMode and each repo-hash costs a sha256sum fork, so tier 1 is a
# single hash covering the common case (a resumed session reading its own
# still-valid review). Tier 2 hashes every worktree root and only runs after
# tier 1 misses — i.e. only when the gate is about to deny anyway.
# Fail closed: an unresolvable config dir must deny the gate, not silently
# skip the marker check and let the write/ExitPlanMode through.
if ! CONFIG_DIR=$(_lib_config_dir); then
  emit_deny "could not resolve the Claude Code config directory (CLAUDE_CONFIG_DIR is set to a relative path, or \$HOME is unset/empty)."
  exit 0
fi
PLAN_REVIEW_MARKERS_DIR="$CONFIG_DIR/plan-review-markers"
REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
if _lib_marker_value_present "$PLAN_REVIEW_MARKERS_DIR" "$CURRENT_HASH" "$REPO_HASH."; then
  exit 0
fi

# Tier 2 — sibling worktrees of this same repository. The plan hash covers
# repo-relative paths plus contents, so a plan copied into a fresh worktree
# hashes identically and a review performed in one worktree covers the
# identical plan text in another.
# Scoped to `git worktree list` output, not a repo-agnostic marker-directory
# read, so an unrelated repository holding a plan file at the same relative
# path and contents can't release this gate.
# A failed or timed-out enumeration falls through to the deny below — fewer
# worktrees scanned must never mean "allow", the same fail-closed discipline
# _lib_active_plan_hash applies to its own git calls.
# See docs/design-decisions/plan-review-tier-2-sibling-worktree-matching.md
# for what a tier-2 hit authorizes (content-identity, not state-identity of
# the sibling's checkout) and this tier's per-edit cost during active
# drafting.
if WORKTREE_LIST=$(_lib_capped git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null); then
  SIBLING_PREFIXES=()
  while IFS= read -r worktree_line; do
    case "$worktree_line" in
      "worktree "*) ;;
      *) continue ;;
    esac
    SIBLING_ROOT="${worktree_line#worktree }"
    [ -n "$SIBLING_ROOT" ] || continue
    # Tier 1 already scanned this repo's own prefix.
    [ "$SIBLING_ROOT" != "$REPO_ROOT" ] || continue
    SIBLING_PREFIXES+=("$(_marker_lib_repo_hash "$SIBLING_ROOT").")
  done <<< "$WORKTREE_LIST"
  # The count guard is load-bearing under `set -u`: expanding an empty array
  # is an unbound-variable error on bash before 4.4 (stock macOS ships 3.2).
  if [ "${#SIBLING_PREFIXES[@]}" -gt 0 ] \
    && _lib_marker_value_present "$PLAN_REVIEW_MARKERS_DIR" "$CURRENT_HASH" "${SIBLING_PREFIXES[@]}"; then
    exit 0
  fi
fi

# Status 2 never changes this allow/deny decision -- it only changes what
# this message says, so a timeout-driven full-diff fallback reads as
# distinguishable from an ordinary marker mismatch rather than this gate
# silently not working.
UNDETERMINED_BASE_NOTE=""
if [ "$GATE_DIFF_BASE_STATUS" -eq 2 ]; then
  UNDETERMINED_BASE_NOTE=" Note: this repo appears to be mid-merge/rebase/cherry-pick/revert, but the novel-content base could not be computed (a git call timed out, was killed, or its binary was missing), so this gate fell back to the full HEAD-relative plan-file diff instead of excluding already-reviewed upstream content."
fi

if [ "$TOOL_NAME" = "ExitPlanMode" ]; then
  emit_deny "Plan presentation — an uncommitted or modified plan file exists in .claude/plans/ but no plan-review marker covering the current plan set was found.

  Run /plan-review against the plan file before calling ExitPlanMode. The skill records the review in ~/.claude/plan-review-markers/ and plan presentation will be allowed on retry.

  If no plan covers this session yet → run /plan-it first. It authors the plan and hands off to /plan-review.${UNDETERMINED_BASE_NOTE}"
else
  emit_deny "Write/Edit — an uncommitted or modified plan file exists in .claude/plans/ but no plan-review marker covering the current plan set was found. A review from an earlier session still counts — the gate matches on the plan's content, not on which session reviewed it — so this means the plan set has changed since its last review, or has never been reviewed. Committed, unmodified plan files are treated as historical and do not arm the gate. Editing the plan file itself is exempt from this gate — this deny is for a different, non-plan target, so the plan is still editable. Next step depends on whether a plan covers this change:

  - If a plan covers this change → run /plan-review against it. The skill records the review in ~/.claude/plan-review-markers/ and this write will be allowed through on retry.

  - If no plan covers this change yet → run /plan-it first. It authors the plan and hands off to /plan-review at the end.

The model judges which case applies from conversation context. Plans live wherever you put them — typically .claude/plans/, but also /tmp/<slug>.md, handoff docs, or external design doc URLs. The hook does not try to detect plan-change correlation.${UNDETERMINED_BASE_NOTE}"
fi
