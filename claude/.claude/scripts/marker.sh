#!/bin/bash
# Write or remove review markers for Claude Code workflow skills.
# Called from SKILL.md HOOK_TEST_FIXTURE fenced blocks.
# Usage: marker.sh <write|activate|deactivate|clear-stale> [<skill>|--dry-run]
# _marker_lib_repo_hash is defined in the sourced library so the hash recipe
# stays in sync with the read side (require-*.sh hooks) automatically.
# shellcheck source=../hooks/_lib.sh
. "$(dirname "$0")/../hooks/_lib.sh"

set -u

# Sibling script, same directory as marker.sh itself -- used by the `status`
# arm below via _lib_cumulative_diff_hash. The `write cumulative-review` arm
# reads a recorded subject instead of calling this script directly.
PR_DIFF_SCRIPT="$(dirname "$0")/pr-diff-against-base.sh"

# The pathspecs are load-bearing: scope the hash to SKILL.md diffs (stowed,
# plugin, and plugin-root-equals-repo-root locations) plus plan-review/ROUTING.md,
# matching what require-skill-review.sh checks at commit time.
SKILL_REVIEW_PATHSPECS=('claude-skills/skills/**/SKILL.md' 'plugins/*/skills/**/SKILL.md' 'skills/**/SKILL.md' 'claude-skills/skills/plan-review/ROUTING.md')

usage() {
  cat >&2 <<'EOF'
Usage: ~/.claude/scripts/marker.sh <subcommand> [<skill>|--dry-run]

Subcommands:
  write      Write a completion marker for the given skill
  activate   Write an active-bypass marker for the given skill
  deactivate Remove the active-bypass marker for the given skill
  clear-stale [--dry-run]
             Evict active-bypass markers whose originating session is no
             longer alive, or whose mtime has aged past the 60-minute idle
             window. --dry-run reports without removing.
  resolve-session-id
             Print this session's canonically-resolved session id. Takes no
             skill argument.
  status     Report every completion marker (code-review, skill-review,
             plan-review, ready-for-review, cumulative-review) for this repo
             and every active-bypass marker (plan-review, ready-for-review,
             respond-pr, memory-skill, handoff) for this session, each as
             live, historical, or absent. Takes no skill argument. Evicts a
             stale (dead-PID) active-bypass marker for this session as a
             side effect of classifying it.
  check      Report whether a completion marker already matches the
             current state, without writing anything. Exit 0 and print
             "match" if it does, exit 1 and print "no-match" if it
             doesn't.

Valid (subcommand, skill) combinations:
  write       code-review | skill-review | plan-review | ready-for-review | cumulative-review
  activate    plan-review | ready-for-review | respond-pr | memory-skill | handoff
  deactivate  plan-review | ready-for-review | respond-pr | memory-skill | handoff
  check       code-review
EOF
}

_walk_session() {
  # Delegates to _lib.sh's _lib_resolve_claude_pid, which is this same
  # ancestor walk (moved there so hooks can call it directly without
  # sourcing this file). Kept as a thin wrapper, not inlined at call sites,
  # so this file's own error message stays specific to marker.sh's context.
  local out
  out=$(_lib_resolve_claude_pid) || {
    printf 'marker.sh: SESSION_ID empty — capture-session-id.sh SessionStart hook did not run. Abort without writing a marker.\n' >&2
    return 2
  }
  printf '%s' "$out"
}

_resolve_session_id() {
  local out sid
  out=$(_walk_session) || return 2
  sid="${out%% *}"
  # Chokepoint for every marker.sh path built from a session id (~15
  # constructions below): reject here once rather than at each call site. An
  # id containing '..' or '/' would escape the marker/active directories once
  # concatenated into a path, turning `rm -f`/`>` against it into an
  # operation against a caller-chosen path.
  if ! _lib_valid_session_id_component "$sid"; then
    printf 'marker.sh: SESSION_ID %s is not a valid path component. Abort without writing a marker.\n' "$sid" >&2
    return 2
  fi
  printf '%s' "$sid"
}

_resolve_claude_pid() {
  # The live Claude main-process PID for this session — the ancestor whose
  # session file the walk matched. Written into the active-bypass marker so
  # require-*.sh hooks can liveness-check it with kill -0. Resolving it from
  # the ancestor walk (not by content-scanning ~/.claude/sessions/) keeps it
  # immune to stale per-session files left behind after a crash.
  local out
  out=$(_walk_session) || return 2
  printf '%s' "${out##* }"
}

_refuse_main_tree_under_enforcement() {
  # A marker's path (repo hash) and its contents (staged diff, HEAD, plan set)
  # are all keyed to the tree this script resolves. When the reviewed work
  # lives in a linked worktree but this invocation resolves to the main tree,
  # the marker records a review of a tree nobody reviewed — and the reading
  # hook, resolving the same way, accepts it. There is no payload to import a
  # trusted directory from, so refuse instead of guessing.
  local root="$1" session_git_dir common_git_dir worktree
  _lib_worktree_enforcement_active "$root" || return 0
  # One rev-parse call, one line per query flag, in the order given. For the
  # main working tree both paths are identical; for a linked worktree
  # --absolute-git-dir points at <common>/worktrees/<name>.
  {
    read -r session_git_dir
    read -r common_git_dir
  } < <(_lib_capped git -C "$root" rev-parse --absolute-git-dir --path-format=absolute --git-common-dir 2>/dev/null)
  if [ -z "${session_git_dir:-}" ] || [ -z "${common_git_dir:-}" ]; then
    printf 'marker.sh: could not determine git state for %s. Refusing to write a marker under worktree enforcement.\n' "$root" >&2
    return 2
  fi
  [ "$session_git_dir" != "$common_git_dir" ] && return 0
  # Main tree, but no linked worktree exists: there is no second tree for this
  # marker to be confused with, so it correctly describes the only tree there
  # is. Refusing here would wedge a repo whose staged state was produced
  # outside Claude Code's gated tool calls — a hand-staged edit in a terminal
  # or editor, a CI checkout, or work staged before the repo opted in. The
  # worktree hooks gate tool calls, not ambient git state, so that condition is
  # reachable and legitimate.
  worktree=$(_lib_first_live_linked_worktree "$root") || return 0
  printf 'marker.sh: refusing to write a marker from the main working tree (%s) while worktree enforcement is active and a linked worktree exists (%s).\n' "$root" "$worktree" >&2
  printf 'Markers are keyed to the tree they are written from, so this would record the review against the wrong tree.\n' >&2
  printf 'Re-enter the branch worktree — EnterWorktree{path: "%s"} — and re-run the review skill there.\n' "$worktree" >&2
  return 2
}

_resolve_repo_root() {
  # _lib_repo_root is the raw resolution recipe, shared with
  # pr-diff-against-base.sh --record so both sides resolve a given tree to
  # the identical REPO_ROOT string. require-* hooks compute the hash via
  # printf '%s' "$REPO_ROOT" (no newline), so every side must agree exactly.
  local root
  root=$(_lib_repo_root) || {
    printf 'marker.sh: not inside a git repository\n' >&2
    return 2
  }
  _refuse_main_tree_under_enforcement "$root" || return 2
  printf '%s' "$root"
}

_guard_staged_vs_unstaged() {
  local repo_root="$1"; shift
  local skill="$1"; shift
  if git -C "$repo_root" diff --cached --quiet -- "$@" && ! git -C "$repo_root" diff --quiet -- "$@"; then
    # shellcheck disable=SC2016 # single-quoted for literal display text (the
    # backtick-quoted `git add` is markdown-style formatting, not command
    # substitution); %s below is the only intended expansion.
    printf 'marker.sh: staged diff is empty but unstaged tracked changes exist — run `git add` before /%s.\n' "$skill" >&2
    exit 2
  fi
}

# MARKER_TEST_FIXTURE: hash-staged-diff — start
# _HASH_STAGED_DIFF_RC_EMPTY: distinguished return code from
# _hash_staged_diff for "git succeeded and the diff is empty", kept apart
# from the bare 1 used for "git itself could not be trusted" (non-zero
# exit, or an empty hash despite a zero exit).
readonly _HASH_STAGED_DIFF_RC_EMPTY=3
# The well-known SHA-256 digest of empty input, computed once here via a
# subprocess rather than hardcoded -- a raw 64-character hex literal in the
# source trips the redaction hook's long-hex-identifier detector (see
# docs/private-project-redaction.md). Computed once at script load, not per
# call, since the digest is algorithm-fixed and _hash_staged_diff must not
# pay a second sha256sum call per invocation.
_HASH_STAGED_DIFF_EMPTY_DIGEST="$(printf '' | sha256sum | awk '{print $1}')"
readonly _HASH_STAGED_DIFF_EMPTY_DIGEST

# _hash_staged_diff CAP_MODE REPO_ROOT [PATHSPEC...]
# Hashes the staged diff (optionally scoped to PATHSPEC) via
# `git diff --cached | sha256sum`, printing the hash and returning 0 on
# real (non-empty) content. CAP_MODE is "capped" to run the git call through
# _lib_capped's 5s timeout, or "uncapped" to run it directly -- write call
# sites run uncapped since they run once per completed review, while
# status/check call sites cap it since they run on every invocation.
# pipefail is scoped to just this call: a timed-out (or otherwise failed)
# git leaves sha256sum hashing empty stdin, which succeeds and yields a real
# (non-empty) hash, so an emptiness check alone can't catch it -- git's own
# exit status has to gate the hash instead.
# Prints nothing and returns 1 on failure (non-zero git exit, or an empty
# hash despite a zero exit).
# Prints nothing and returns $_HASH_STAGED_DIFF_RC_EMPTY when git succeeded
# but the diff is empty, classified by comparing the hashed digest itself
# against $_HASH_STAGED_DIFF_EMPTY_DIGEST.
# Not a second, separately-timed `git diff --cached --quiet` probe run
# ahead of the hash -- two decoupled git calls can diverge under transient
# contention (an index lock, a background git process), and classifying
# the same digest that gets hashed avoids that TOCTOU race by construction.
_hash_staged_diff() {
  local cap_mode="$1" repo_root="$2"; shift 2
  local -a diff_args=(-C "$repo_root" diff --cached)
  # -- only when PATHSPECs are given, matching each call site's own
  # pre-refactor invocation exactly (a bare `diff --cached --` is equivalent
  # to `diff --cached` to git, but the two are different argv shapes).
  [ "$#" -gt 0 ] && diff_args+=(-- "$@")
  local -a diff_cmd
  case "$cap_mode" in
    capped) diff_cmd=(_lib_capped git "${diff_args[@]}") ;;
    uncapped) diff_cmd=(git "${diff_args[@]}") ;;
    *)
      printf '_hash_staged_diff: invalid cap_mode %s (want capped or uncapped)\n' "$cap_mode" >&2
      return 2
      ;;
  esac
  local hash git_exit
  set -o pipefail
  hash=$("${diff_cmd[@]}" | sha256sum | awk '{print $1}')
  git_exit=$?
  set +o pipefail
  [ "$git_exit" -eq 0 ] && [ -n "$hash" ] || return 1
  [ "$hash" = "$_HASH_STAGED_DIFF_EMPTY_DIGEST" ] && return "$_HASH_STAGED_DIFF_RC_EMPTY"
  printf '%s' "$hash"
}
# MARKER_TEST_FIXTURE: hash-staged-diff — end

# _status_glob_has_match DIR PREFIX
# True iff some file in DIR whose name begins with PREFIX exists, regardless
# of content -- used by `status` to tell "historical" (a marker exists for
# this repo but its hash is stale) apart from "absent" (no marker at all).
_status_glob_has_match() {
  local dir="$1" prefix="$2"
  local nullglob_was_set=0
  if shopt -q nullglob; then nullglob_was_set=1; fi
  shopt -s nullglob
  local -a matched=("$dir/$prefix"*)
  if [ "$nullglob_was_set" -eq 0 ]; then shopt -u nullglob; fi
  # nullglob leaves the array empty on no match, so the first slot is unset
  # (empty string) precisely when nothing matched.
  [ -n "${matched[0]:-}" ]
}

# _status_report_completion_marker LABEL MARKERS_DIR REPO_HASH_PREFIX CURRENT_VALUE
# Prints "  LABEL: live|historical|absent (...)" for `status`. Returns 0 when
# live so a caller needing an extra live-only check (the reconciliation flag)
# doesn't have to re-derive the state.
_status_report_completion_marker() {
  local label="$1" markers_dir="$2" prefix="$3" current_value="$4"
  if [ -n "$current_value" ] && _lib_marker_value_present "$markers_dir" "$current_value" "$prefix"; then
    printf '  %s: live (hash matches the current state)\n' "$label"
    return 0
  fi
  if _status_glob_has_match "$markers_dir" "$prefix"; then
    printf '  %s: historical (marker present, hash does not match the current state)\n' "$label"
  else
    printf '  %s: absent (no marker for this repo)\n' "$label"
  fi
  return 1
}

# _status_reconciliation_flag LABEL REPO_ROOT [PATHSPEC...]
# Prints a flag line when the working tree holds unstaged changes overlapping
# PATHSPEC (the whole repo when no pathspec is given) -- called only when the
# corresponding marker is live, since "uncommitted changes overlap a
# not-live marker" has nothing to reconcile.
_status_reconciliation_flag() {
  local label="$1" repo_root="$2"; shift 2
  local diff_status
  # Exit code checked exactly, not just nonzero: a capped call that times out
  # also exits nonzero, and must not be misread as "differences found".
  _lib_capped git -C "$repo_root" diff --quiet -- "$@"
  diff_status=$?
  if [ "$diff_status" -eq 1 ]; then
    printf '  %s reconciliation flag: uncommitted changes overlap the diff this marker covers\n' "$label"
  fi
}

# _status_report_active_bypass LABEL DIR_NAME SESSION_ID
# Prints "  LABEL: live|stale|absent (...)" for `status`. Existence is
# captured BEFORE calling _lib_active_bypass_marker_live, which evicts a
# stale marker as a side effect (see that function's own docstring for its
# two eviction triggers) -- otherwise "stale" and "absent" would be
# indistinguishable after the call evicts the file out from under us. This
# is a status-only read: it calls the unrefreshing predicate directly,
# never _lib_active_bypass_marker_live_and_touch, so enumerating status
# here can never itself extend a marker's life.
_status_report_active_bypass() {
  local label="$1" dir_name="$2" session_id="$3"
  local marker_path="$CONFIG_DIR/$dir_name/$session_id"
  local existed_before=0
  [ -f "$marker_path" ] && existed_before=1
  if _lib_active_bypass_marker_live "$dir_name" "$session_id"; then
    printf '  %s: live (bypass marker present for this session)\n' "$label"
  elif [ "$existed_before" -eq 1 ]; then
    printf '  %s: stale (marker evicted: dead PID or idle timeout)\n' "$label"
  else
    printf '  %s: absent (no bypass marker for this session)\n' "$label"
  fi
}

# _marker_mtime_epoch TARGET
# Prints TARGET's mtime as a Unix epoch, GNU stat first then BSD/macOS stat --
# same probe order as ask-new-dependency-disclosure.sh's _file_size (not
# shared via _lib.sh; that hook's comment names this as the canonical form).
# Capped at 5s via _lib_capped_for, matching _hash_staged_diff capped's own
# rationale: a check call (unlike write) reads state it doesn't control, so
# a stalled stat must not hang the gate it's backing.
_marker_mtime_epoch() {
  local target="$1"
  _lib_capped_for 5 stat -c%Y -- "$target" 2>/dev/null || _lib_capped_for 5 stat -f%m -- "$target" 2>/dev/null
}

# _resolve_code_review_check_max_age_seconds
# Sets CODE_REVIEW_CHECK_MAX_AGE_SECONDS (global). Default 86400 (24h) is a
# deliberately conservative, ungrounded choice (docs/design-decisions.md
# §62). Malformed override (empty, zero, non-digit, zero-padded, or 9+
# digits) falls back to the default -- same guard shape as
# nudge-long-turn-subagent.sh's resolve_threshold.
_resolve_code_review_check_max_age_seconds() {
  case "${CODE_REVIEW_CHECK_MAX_AGE_SECONDS:-}" in
    ''|0|*[!0-9]*|0[0-9]*|?????????*) CODE_REVIEW_CHECK_MAX_AGE_SECONDS=86400 ;;
    *) ;;
  esac
}

# _code_review_marker_fresh_age MARKERS_DIR EXPECTED_VALUE GLOB_PREFIX MAX_AGE_SECONDS
# Prints the age in seconds of the freshest file in MARKERS_DIR matching
# GLOB_PREFIX that holds EXPECTED_VALUE as a whole line and is younger than
# MAX_AGE_SECONDS, and returns 0. Prints nothing and returns 1 when no such
# file exists. Called only after _lib_marker_value_present has already
# confirmed at least one hash match. That single-grep call stays the cheap
# common-case check for "no hash match at all". This loop only runs once a
# hash match exists, and narrows that match set down to non-stale files.
# The candidates loop below is O(n) in GLOB_PREFIX's accumulated marker
# count. That's bounded today because this repo's own worktree-enforced
# hashing keys REPO_HASH to an ephemeral per-branch path rather than a
# stable long-lived one. A non-worktree-enforced repo would scale
# unboundedly here, since completion markers are never pruned.
_code_review_marker_fresh_age() {
  local markers_dir="$1" expected_value="$2" glob_prefix="$3" max_age_seconds="$4"
  local nullglob_was_set=0
  if shopt -q nullglob; then nullglob_was_set=1; fi
  shopt -s nullglob
  local -a candidates=("$markers_dir/$glob_prefix"*)
  if [ "$nullglob_was_set" -eq 0 ]; then shopt -u nullglob; fi

  local now candidate mtime age best_age=""
  now=$(date +%s)
  # A failed or non-numeric `now` must not fall through to the arithmetic
  # below. An empty $now makes `age = -mtime`, a large negative number that
  # trivially passes the `-lt max_age_seconds` check. Fail closed here the
  # same way the `check code-review` arm further below treats an
  # empty/failed staged-diff hash as no-match.
  case "$now" in ''|*[!0-9]*) return 1 ;; esac
  for candidate in "${candidates[@]}"; do
    [ -f "$candidate" ] || continue
    # Capped at 5s for the same reason _marker_mtime_epoch is: check reads a
    # marker file it doesn't control.
    _lib_capped_for 5 grep -qFx -e "$expected_value" -- "$candidate" 2>/dev/null || continue
    mtime=$(_marker_mtime_epoch "$candidate")
    [ -n "$mtime" ] || continue
    age=$(( now - mtime ))
    # Clamp a future-mtime marker (clock skew, restore tooling) to 0 rather
    # than reporting a negative age, which would otherwise pass the
    # freshness check unconditionally.
    [ "$age" -lt 0 ] && age=0
    if [ "$age" -lt "$max_age_seconds" ] && { [ -z "$best_age" ] || [ "$age" -lt "$best_age" ]; }; then
      best_age="$age"
    fi
  done
  [ -n "$best_age" ] || return 1
  printf '%s' "$best_age"
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
  usage
  exit 2
fi

# Fail closed: every marker path below is built from CONFIG_DIR, so an
# unresolvable CLAUDE_CONFIG_DIR (relative value, or empty $HOME with no
# override) must abort the write rather than fall through to a
# root-anchored path.
CONFIG_DIR=$(_lib_config_dir) || {
  # shellcheck disable=SC2016 # single-quoted for literal display text: $HOME
  # and $CLAUDE_CONFIG_DIR name the env vars in the message, not shell expansions.
  printf 'marker.sh: could not resolve the Claude Code config directory (CLAUDE_CONFIG_DIR is set to a relative path, or $HOME is unset/empty). Abort without writing a marker.\n' >&2
  exit 2
}

SUBCOMMAND="$1"
ARG2="${2:-}"

# Validate arg count per subcommand.
case "$SUBCOMMAND" in
  write|activate|deactivate|check)
    if [ -z "$ARG2" ]; then
      usage
      exit 2
    fi
    SKILL="$ARG2"
    ;;
  clear-stale)
    if [ -n "$ARG2" ] && [ "$ARG2" != "--dry-run" ]; then
      usage
      exit 2
    fi
    ;;
  resolve-session-id|status)
    if [ -n "$ARG2" ]; then
      usage
      exit 2
    fi
    ;;
  *)
    printf "marker.sh: unknown subcommand '%s'\n" "$SUBCOMMAND" >&2
    usage
    exit 2
    ;;
esac

case "$SUBCOMMAND" in
  write)
    case "$SKILL" in
      code-review)
        SESSION_ID=$(_resolve_session_id) || exit 2
        REPO_ROOT=$(_resolve_repo_root) || exit 2
        REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
        _guard_staged_vs_unstaged "$REPO_ROOT" code-review
        # Resolved once and threaded into _lib_code_review_marker_value below,
        # so the marker records the same novel-content preimage
        # require-code-review.sh reads -- a mismatch between write-side and
        # read-side base recipes would mean a marker written here can never
        # match on the read side.
        GATE_DIFF_BASE=$(_lib_gate_diff_base "$REPO_ROOT")
        # This call and require-code-review.sh's own _lib_gate_diff_base call
        # each independently hit the ~5s cap, so during an in-progress
        # merge/rebase/cherry-pick/revert a marker can fail to match on read
        # despite unchanged staged content.
        # This never causes a false accept, only a false re-review
        # requirement, so it is an availability gap, not a security one.
        # Compute before redirecting: `>` truncates the marker before the
        # pipeline runs, so a failed hash would destroy a valid marker and
        # silently force a re-review. Same shape in every arm below.
        MARKER_VALUE=$(_lib_code_review_marker_value "$REPO_ROOT" "$GATE_DIFF_BASE")
        if [ -z "$MARKER_VALUE" ]; then
          printf 'marker.sh: could not hash the staged diff. Abort without writing a marker.\n' >&2
          exit 2
        fi
        # $_HASH_STAGED_DIFF_EMPTY_DIGEST only equals sha256("") here when
        # GATE_DIFF_BASE is empty (no trusted in-progress state) and the
        # plain staged diff is itself empty -- a mid-operation degenerate-
        # empty-diff case binds to GATE_DIFF_BASE's own identity instead (see
        # _lib_code_review_marker_value), so this check can't misfire on
        # that case.
        if [ "$MARKER_VALUE" = "$_HASH_STAGED_DIFF_EMPTY_DIGEST" ]; then
          printf 'marker.sh: staged diff is empty -- nothing to review. Exiting without writing a marker.\n' >&2
          exit 0
        fi
        mkdir -p "$CONFIG_DIR/code-review-markers"
        printf '%s\n' "$MARKER_VALUE" \
          > "$CONFIG_DIR/code-review-markers/$REPO_HASH.$SESSION_ID"
        ;;
      skill-review)
        SESSION_ID=$(_resolve_session_id) || exit 2
        REPO_ROOT=$(_resolve_repo_root) || exit 2
        REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
        # The pathspecs are load-bearing: scope the hash to SKILL.md diffs (both stowed
        # and plugin locations) plus plan-review/ROUTING.md, matching what
        # require-skill-review.sh checks at commit time. SKILL_REVIEW_PATHSPECS is
        # the module-level constant defined near the top of this file.
        _guard_staged_vs_unstaged "$REPO_ROOT" skill-review "${SKILL_REVIEW_PATHSPECS[@]}"
        RC=0
        MARKER_VALUE=$(_hash_staged_diff uncapped "$REPO_ROOT" "${SKILL_REVIEW_PATHSPECS[@]}") || RC=$?
        case "$RC" in
          0) ;;
          "$_HASH_STAGED_DIFF_RC_EMPTY")
            printf 'marker.sh: staged SKILL.md diff is empty -- nothing to review. Exiting without writing a marker.\n' >&2
            exit 0
            ;;
          *)
            printf 'marker.sh: could not hash the staged SKILL.md diff. Abort without writing a marker.\n' >&2
            exit 2
            ;;
        esac
        mkdir -p "$CONFIG_DIR/skill-review-markers"
        printf '%s\n' "$MARKER_VALUE" \
          > "$CONFIG_DIR/skill-review-markers/$REPO_HASH.$SESSION_ID"
        ;;
      plan-review)
        SESSION_ID=$(_resolve_session_id) || exit 2
        REPO_ROOT=$(_resolve_repo_root) || exit 2
        REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
        # A plan-mode declaration takes priority over the repo-relative plan
        # set: the /plan-review skill's Step 0 writes the harness-designated
        # plan-mode file's path into this sibling file when the session is in
        # plan mode. Content-addressed like the repo-relative case below —
        # hash the DECLARED TARGET's current content, read fresh here, not
        # the sibling file's own bytes and not any hash computed earlier — so
        # a plan revision mid-review is still caught.
        PLANMODE_SIBLING="$CONFIG_DIR/.plan-review-active.d/$SESSION_ID.planmode-path"
        if PLANMODE_TARGET=$(_lib_capped cat "$PLANMODE_SIBLING" 2>/dev/null); then
          PLAN_HASH=$(_lib_capped sha256sum -- "$PLANMODE_TARGET" 2>/dev/null | awk '{print $1}')
          if [ -z "$PLAN_HASH" ]; then
            # Matches _lib_active_plan_hash's own abort contract below:
            # falling back to the repo-relative hash here would silently
            # write a completion marker that doesn't cover what was reviewed.
            printf 'marker.sh: cannot read plan-mode file %s — cannot compute the plan-review hash. Abort without writing a marker.\n' "$PLANMODE_TARGET" >&2
            exit 2
          fi
        else
          # Content-addressed: the marker holds a hash of the active plan
          # file set (paths + contents), so editing a reviewed plan re-arms
          # require-plan-review.sh on the next gate hit. _lib_active_plan_hash
          # is the single source of truth shared with the read side.
          #
          # Resolved here (not shared with the code-review arm above, which
          # runs in a separate `write` invocation) and threaded into
          # _lib_active_plan_hash, so the marker records the same
          # trusted-base preimage require-plan-review.sh reads.
          #
          # Capture into a variable before redirecting. Writing the
          # function's output straight into the marker path would let `>`
          # truncate an existing valid marker before the function even runs,
          # so a failed attempt would destroy a good marker as a side effect.
          PLAN_GATE_DIFF_BASE=$(_lib_gate_diff_base "$REPO_ROOT")
          # Same residual as the `write code-review` arm above: this call and
          # require-plan-review.sh's own _lib_gate_diff_base call can disagree
          # near the ~5s cap even though the staged content hasn't changed.
          # Fails toward re-review, not toward accepting an unreviewed plan.
          if ! PLAN_HASH=$(_lib_active_plan_hash "$REPO_ROOT" "$PLAN_GATE_DIFF_BASE"); then
            printf 'marker.sh: cannot read active plan file %s — cannot compute the plan-review hash. Abort without writing a marker.\n' "$PLAN_HASH" >&2
            exit 2
          fi
        fi
        mkdir -p "$CONFIG_DIR/plan-review-markers"
        printf '%s\n' "$PLAN_HASH" \
          > "$CONFIG_DIR/plan-review-markers/$REPO_HASH.$SESSION_ID"
        ;;
      ready-for-review)
        SESSION_ID=$(_resolve_session_id) || exit 2
        REPO_ROOT=$(_resolve_repo_root) || exit 2
        REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
        MARKER_VALUE=$(git -C "$REPO_ROOT" rev-parse HEAD)
        [ -n "$MARKER_VALUE" ] || { printf 'marker.sh: could not resolve HEAD. Abort without writing a marker.\n' >&2; exit 2; }
        mkdir -p "$CONFIG_DIR/ready-for-review-markers"
        printf '%s\n' "$MARKER_VALUE" \
          > "$CONFIG_DIR/ready-for-review-markers/$REPO_HASH.$SESSION_ID"
        ;;
      cumulative-review)
        SESSION_ID=$(_resolve_session_id) || exit 2
        REPO_ROOT=$(_resolve_repo_root) || exit 2
        REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
        # No _guard_staged_vs_unstaged call: this marker covers the
        # committed PR-vs-base diff, not the staged diff, so that guard's
        # staged-vs-unstaged question does not apply here.
        #
        # Reads the subject `pr-diff-against-base.sh --record` captured at
        # step 3 entry rather than recomputing (design-decisions.md §50).
        # Emptiness is checked on the canonicalized text below, not the raw
        # file's byte count, to use one definition of "recorded" throughout.
        SUBJECT_FILE="$CONFIG_DIR/cumulative-review-subject-markers/$REPO_HASH.$SESSION_ID"
        if [ ! -e "$SUBJECT_FILE" ]; then
          # shellcheck disable=SC2016 # single-quoted for literal display text (the
          # backtick-quoted command is markdown-style formatting, not command
          # substitution); %s below is the only intended expansion.
          printf 'marker.sh: no recorded cumulative-review subject for %s. Run `~/.claude/scripts/pr-diff-against-base.sh --record` (step 3 already runs this) before writing this marker. Abort without writing a marker.\n' "$REPO_ROOT" >&2
          exit 2
        fi
        # Command substitution strips trailing newlines the same way
        # _lib_cumulative_diff_hash's own diff_output capture does, so the
        # two hashing paths agree byte-for-byte on the same underlying text.
        if ! SUBJECT_TEXT=$(cat "$SUBJECT_FILE" 2>/dev/null); then
          # shellcheck disable=SC2016 # same literal-display-text reasoning as above.
          printf 'marker.sh: could not read the recorded cumulative-review subject at %s (permission denied or similar). Run `~/.claude/scripts/pr-diff-against-base.sh --record` before writing this marker. Abort without writing a marker.\n' "$SUBJECT_FILE" >&2
          exit 2
        fi
        if [ -z "$SUBJECT_TEXT" ]; then
          # shellcheck disable=SC2016 # same literal-display-text reasoning as above.
          printf 'marker.sh: the recorded cumulative-review subject for %s is empty. Run `~/.claude/scripts/pr-diff-against-base.sh --record` (step 3 already runs this) before writing this marker. Abort without writing a marker.\n' "$REPO_ROOT" >&2
          exit 2
        fi
        # Compute before redirecting -- same shape as every other write arm
        # above: `>` truncates the marker before the pipeline runs, so a
        # failed hash would destroy a valid marker and silently force a
        # re-review.
        MARKER_VALUE=$(_lib_hash_diff_text "$SUBJECT_TEXT") || {
          printf 'marker.sh: could not hash the recorded cumulative-review subject. Abort without writing a marker.\n' >&2
          exit 2
        }
        # Consumed on success: bounds a recorded-but-unreviewed subject to
        # authorizing at most one write. Chained via && rather than an `if`
        # so a failed mkdir/printf leaves the subject for a retry. The &&
        # chain also propagates that failure as the script's own exit
        # status, matching every other write arm's unguarded last command.
        mkdir -p "$CONFIG_DIR/cumulative-review-markers" \
          && printf '%s\n' "$MARKER_VALUE" \
            > "$CONFIG_DIR/cumulative-review-markers/$REPO_HASH.$SESSION_ID" \
          && rm -f "$SUBJECT_FILE"
        ;;
      *)
        printf "marker.sh: 'write %s' is not valid. 'write' supports: code-review, skill-review, plan-review, ready-for-review, cumulative-review\n" "$SKILL" >&2
        exit 2
        ;;
    esac
    ;;
  activate)
    case "$SKILL" in
      plan-review)
        SESSION_ID=$(_resolve_session_id) || exit 2
        CLAUDE_PID=$(_resolve_claude_pid) || exit 2
        mkdir -p "$CONFIG_DIR/.plan-review-active.d"
        printf '%s\n' "$CLAUDE_PID" > "$CONFIG_DIR/.plan-review-active.d/$SESSION_ID"
        # Backfill: a ROUTING.md Read landing just before this activate still
        # counts, via log-routing-read.sh's pending-read record. 5 minutes
        # covers a same-turn re-read while staying well inside
        # require-routing-read.sh's 60-minute freshness window, so a stale
        # Read from earlier in the session can't falsely backfill.
        PENDING_READ="$CONFIG_DIR/.plan-review-pending-read.d/$SESSION_ID"
        if [ -f "$PENDING_READ" ] && [ -n "$(find "$PENDING_READ" -mmin -5 2>/dev/null)" ]; then
          mkdir -p "$CONFIG_DIR/.plan-review-routing-read.d"
          touch "$CONFIG_DIR/.plan-review-routing-read.d/$SESSION_ID"
        fi
        ;;
      ready-for-review)
        SESSION_ID=$(_resolve_session_id) || exit 2
        CLAUDE_PID=$(_resolve_claude_pid) || exit 2
        mkdir -p "$CONFIG_DIR/.ready-for-review-active.d"
        printf '%s\n' "$CLAUDE_PID" > "$CONFIG_DIR/.ready-for-review-active.d/$SESSION_ID"
        ;;
      respond-pr)
        SESSION_ID=$(_resolve_session_id) || exit 2
        CLAUDE_PID=$(_resolve_claude_pid) || exit 2
        mkdir -p "$CONFIG_DIR/.respond-pr-active.d"
        printf '%s\n' "$CLAUDE_PID" > "$CONFIG_DIR/.respond-pr-active.d/$SESSION_ID"
        ;;
      memory-skill)
        SESSION_ID=$(_resolve_session_id) || exit 2
        CLAUDE_PID=$(_resolve_claude_pid) || exit 2
        mkdir -p "$CONFIG_DIR/.memory-skill-active.d"
        printf '%s\n' "$CLAUDE_PID" > "$CONFIG_DIR/.memory-skill-active.d/$SESSION_ID"
        ;;
      handoff)
        SESSION_ID=$(_resolve_session_id) || exit 2
        CLAUDE_PID=$(_resolve_claude_pid) || exit 2
        mkdir -p "$CONFIG_DIR/.handoff-active.d"
        printf '%s\n' "$CLAUDE_PID" > "$CONFIG_DIR/.handoff-active.d/$SESSION_ID"
        ;;
      *)
        printf "marker.sh: 'activate %s' is not valid. 'activate' supports: plan-review, ready-for-review, respond-pr, memory-skill, handoff\n" "$SKILL" >&2
        exit 2
        ;;
    esac
    ;;
  deactivate)
    case "$SKILL" in
      plan-review)
        SESSION_ID=$(_resolve_session_id) || exit 2
        rm -f "$CONFIG_DIR/.plan-review-active.d/$SESSION_ID"
        rm -f "$CONFIG_DIR/.plan-review-active.d/$SESSION_ID.planmode-path"
        rm -f "$CONFIG_DIR/.plan-review-routing-read.d/$SESSION_ID"
        rm -f "$CONFIG_DIR/.plan-review-pending-read.d/$SESSION_ID"
        ;;
      ready-for-review)
        SESSION_ID=$(_resolve_session_id) || exit 2
        rm -f "$CONFIG_DIR/.ready-for-review-active.d/$SESSION_ID"
        # Best-effort: bounds this session's own recorded-but-unwritten
        # cumulative-review subject, and its step-4 diff-file artifact, to
        # this gate run. Session-suffixed so this only ever removes this
        # session's own artifacts, never another session's still-pending
        # ones. Repo-root resolution is unrelated to the session-scoped
        # removal above, so a failure here must not abort it -- skip the
        # artifact cleanup instead.
        if REPO_ROOT=$(_resolve_repo_root 2>/dev/null); then
          REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
          rm -f "$CONFIG_DIR/cumulative-review-subject-markers/$REPO_HASH.$SESSION_ID"
          rm -f "$CONFIG_DIR/cumulative-review-diff-markers/$REPO_HASH.$SESSION_ID"
        else
          printf 'marker.sh: could not resolve repo root; skipping cumulative-review subject and diff-file cleanup.\n' >&2
        fi
        ;;
      respond-pr)
        SESSION_ID=$(_resolve_session_id) || exit 2
        rm -f "$CONFIG_DIR/.respond-pr-active.d/$SESSION_ID"
        ;;
      memory-skill)
        SESSION_ID=$(_resolve_session_id) || exit 2
        rm -f "$CONFIG_DIR/.memory-skill-active.d/$SESSION_ID"
        ;;
      handoff)
        SESSION_ID=$(_resolve_session_id) || exit 2
        rm -f "$CONFIG_DIR/.handoff-active.d/$SESSION_ID"
        ;;
      *)
        printf "marker.sh: 'deactivate %s' is not valid. 'deactivate' supports: plan-review, ready-for-review, respond-pr, memory-skill, handoff\n" "$SKILL" >&2
        exit 2
        ;;
    esac
    ;;
  clear-stale)
    # Sweeps only .*-active.d/ below. cumulative-review-subject-markers/ and
    # cumulative-review-diff-markers/ are unreachable here, so a session
    # killed before `deactivate ready-for-review` leaks that session's
    # artifact indefinitely. Accepted as a low-cost gap:
    #   - each artifact is bounded to one session
    #   - overwritten on the next gate pass
    #   - revisit only if unbounded accumulation shows up in practice
    DRY_RUN=0
    [ "$ARG2" = "--dry-run" ] && DRY_RUN=1
    EVICTED=0
    KEPT=0
    for active_dir in "$CONFIG_DIR"/.*-active.d; do
      [ -d "$active_dir" ] || continue
      dir_name=$(basename "$active_dir")
      for entry in "$active_dir"/*; do
        [ -f "$entry" ] || continue
        # Name-based exemption, not a PID-liveness question: this sibling
        # holds a declared plan-mode path, never a PID, so the ^[0-9]+$ test
        # below would always misread it as a dead marker and evict it.
        case "$entry" in
          *.planmode-path) continue ;;
        esac
        stored_pid=$(cat "$entry" 2>/dev/null | tr -d '[:space:]')
        entry_name=$(basename "$entry")
        # Same two-part staleness definition _lib_active_bypass_marker_live
        # uses: PID alive AND mtime within the 60-minute idle window. Kept
        # as its own loop rather than delegating to that predicate, which
        # has no dry-run mode and doesn't report which of the two triggers
        # fired.
        pid_alive=0
        [[ "$stored_pid" =~ ^[0-9]+$ ]] && kill -0 "$stored_pid" 2>/dev/null && pid_alive=1
        if [ "$pid_alive" -eq 1 ] && [ -n "$(find "$entry" -mmin -60 2>/dev/null)" ]; then
          KEPT=$((KEPT + 1))
          [ "$DRY_RUN" -eq 1 ] && printf '  keep: %s/%s (PID %s alive)\n' "$dir_name" "$entry_name" "$stored_pid"
        else
          EVICTED=$((EVICTED + 1))
          if [ "$pid_alive" -eq 1 ]; then
            reason="idle timeout, PID ${stored_pid} alive"
          else
            reason="PID ${stored_pid:-empty} dead"
          fi
          if [ "$DRY_RUN" -eq 0 ]; then
            rm -f "$entry"
            printf '  evict: %s/%s (%s)\n' "$dir_name" "$entry_name" "$reason"
          else
            printf '  evict (dry-run): %s/%s (%s)\n' "$dir_name" "$entry_name" "$reason"
          fi
        fi
      done
    done
    if [ "$DRY_RUN" -eq 1 ]; then
      printf 'clear-stale: would evict %d orphan(s), keep %d active\n' "$EVICTED" "$KEPT"
    else
      printf 'clear-stale: evicted %d orphan(s), kept %d active\n' "$EVICTED" "$KEPT"
    fi
    ;;
  resolve-session-id)
    SESSION_ID=$(_resolve_session_id) || exit 2
    printf '%s' "$SESSION_ID"
    ;;
  status)
    SESSION_ID=$(_resolve_session_id) || exit 2
    REPO_ROOT=$(_resolve_repo_root) || exit 2
    REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
    REPO_HASH_PREFIX="$REPO_HASH."

    printf 'Completion markers (this repo):\n'

    # Resolved once for this subcommand arm and reused by every value below
    # that needs it -- code-review and plan-review both thread it in
    # directly; skill-review computes its own hash back to back, since it
    # does not consume this base. A resolution per value would multiply the
    # merge-tree cost for the same result.
    GATE_DIFF_BASE=$(_lib_gate_diff_base "$REPO_ROOT")

    # code-review: hash of the whole-repo staged diff -- same recipe as the
    # `write code-review` arm above. _lib_code_review_marker_value is
    # internally capped, so a stalled git diff can't hang the whole status
    # report; a killed process, or a genuinely empty staged diff outside any
    # trusted in-progress state, both yield an empty value, which
    # _status_report_completion_marker already treats as absent/historical --
    # this read-only report has no reason to distinguish the two the way the
    # `write code-review` arm's early exit does.
    CODE_REVIEW_VALUE=$(_lib_code_review_marker_value "$REPO_ROOT" "$GATE_DIFF_BASE")
    if _status_report_completion_marker code-review "$CONFIG_DIR/code-review-markers" "$REPO_HASH_PREFIX" "$CODE_REVIEW_VALUE"; then
      _status_reconciliation_flag code-review "$REPO_ROOT"
    fi

    # skill-review: same recipe as the `write skill-review` arm above,
    # scoped to the SKILL.md/ROUTING.md pathspecs and gated the same way.
    SKILL_REVIEW_VALUE=$(_hash_staged_diff capped "$REPO_ROOT" "${SKILL_REVIEW_PATHSPECS[@]}")
    if _status_report_completion_marker skill-review "$CONFIG_DIR/skill-review-markers" "$REPO_HASH_PREFIX" "$SKILL_REVIEW_VALUE"; then
      _status_reconciliation_flag skill-review "$REPO_ROOT" "${SKILL_REVIEW_PATHSPECS[@]}"
    fi

    # plan-review: same recipe as the `write plan-review` arm above (the
    # plan-mode sibling takes priority over _lib_active_plan_hash). A hash
    # that can't be computed (unreadable plan-mode target) is treated as
    # empty here rather than aborting -- `status` is a report, not a write,
    # and the other three markers still deserve their own report.
    PLANMODE_SIBLING="$CONFIG_DIR/.plan-review-active.d/$SESSION_ID.planmode-path"
    if PLANMODE_TARGET=$(_lib_capped cat "$PLANMODE_SIBLING" 2>/dev/null); then
      PLAN_REVIEW_VALUE=$(_lib_capped sha256sum -- "$PLANMODE_TARGET" 2>/dev/null | awk '{print $1}')
    else
      PLAN_REVIEW_VALUE=$(_lib_active_plan_hash "$REPO_ROOT" "$GATE_DIFF_BASE") || PLAN_REVIEW_VALUE=""
    fi
    _status_report_completion_marker plan-review "$CONFIG_DIR/plan-review-markers" "$REPO_HASH_PREFIX" "$PLAN_REVIEW_VALUE"

    # ready-for-review: same recipe as the `write ready-for-review` arm
    # above. Unlike that arm, stderr is suppressed and an empty result is not
    # fatal -- a zero-commit repo has no HEAD to hash, which `status` must
    # report as absent rather than error on.
    READY_FOR_REVIEW_VALUE=$(_lib_capped git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)
    _status_report_completion_marker ready-for-review "$CONFIG_DIR/ready-for-review-markers" "$REPO_HASH_PREFIX" "$READY_FOR_REVIEW_VALUE"

    # cumulative-review: same recipe as the `write cumulative-review` arm
    # above, via the shared _lib_cumulative_diff_hash. Makes a network round
    # trip (gh pr view) unlike every other line in this report, which are
    # all local/offline. A hash that can't be computed (gh/network failure,
    # a legitimately empty diff -- often because the branch already has a
    # merged PR, no resolvable merge-base, or the 15s cap firing) is treated
    # as empty here rather than aborting -- `status` is a report, not a
    # write. No reconciliation flag: that check is documented
    # (_status_reconciliation_flag above) as applying only to pathspec-hash
    # markers, and the cumulative diff is not staged/unstaged tree state.
    CUMULATIVE_DIFF_HASH_FAILED=0
    CUMULATIVE_REVIEW_VALUE=$(_lib_cumulative_diff_hash "$REPO_ROOT" "$PR_DIFF_SCRIPT") || { CUMULATIVE_REVIEW_VALUE=""; CUMULATIVE_DIFF_HASH_FAILED=1; }
    _status_report_completion_marker cumulative-review "$CONFIG_DIR/cumulative-review-markers" "$REPO_HASH_PREFIX" "$CUMULATIVE_REVIEW_VALUE"
    # A failed computation with a marker on disk would otherwise print as
    # "historical (hash does not match)" -- a confirmed-mismatch claim this
    # code never actually made. Flag that distinction without changing the
    # line above, so a reader can tell "could not verify" apart from
    # "confirmed stale".
    if [ "$CUMULATIVE_DIFF_HASH_FAILED" -eq 1 ] && _status_glob_has_match "$CONFIG_DIR/cumulative-review-markers" "$REPO_HASH_PREFIX"; then
      printf '  cumulative-review: could not verify (pr-diff-against-base.sh failed, the diff is empty -- often because this branch already has a merged PR -- or resolution timed out -- the state above reflects marker presence only, not a confirmed hash comparison)\n' >&2
    fi

    printf '\nActive-bypass markers (this session):\n'
    _status_report_active_bypass plan-review ".plan-review-active.d" "$SESSION_ID"
    _status_report_active_bypass ready-for-review ".ready-for-review-active.d" "$SESSION_ID"
    _status_report_active_bypass respond-pr ".respond-pr-active.d" "$SESSION_ID"
    _status_report_active_bypass memory-skill ".memory-skill-active.d" "$SESSION_ID"
    _status_report_active_bypass handoff ".handoff-active.d" "$SESSION_ID"
    ;;
  check)
    case "$SKILL" in
      code-review)
        REPO_ROOT=$(_resolve_repo_root) || exit 2
        # Same hash recipe as the `write code-review` arm, base and all:
        # both route through _lib_code_review_marker_value, so a marker
        # written under a trusted in-progress base's own base-relative diff
        # can actually be matched here rather than compared against a plain
        # HEAD-relative diff that would never agree with it. Read-only: no
        # SESSION_ID needed since this never writes.
        # A hash that can't be computed (`_lib_code_review_marker_value`
        # returns 1) must read as no-match, not match. This is the
        # short-circuit /code-review consults before skipping its
        # specialist panel, so failing open here would silently skip a real
        # review.
        #
        # Capped unlike `write code-review`'s git calls, since `check` runs
        # on every `/code-review` invocation rather than only on a completed
        # review. A timeout here degrades to no-match, never a false match.
        GATE_DIFF_BASE=$(_lib_gate_diff_base "$REPO_ROOT")
        MARKER_VALUE=$(_lib_code_review_marker_value "$REPO_ROOT" "$GATE_DIFF_BASE") || { printf 'no-match\n'; exit 1; }
        # Checked against whatever _lib_code_review_marker_value itself
        # would produce for GATE_DIFF_BASE's own "no new content" case,
        # computed here rather than hardcoded so no single line embeds the
        # full hex digest. Not a separate `git diff --cached --quiet`
        # probe. Two decoupled git calls can diverge under transient
        # contention (index lock, background git process). A stale marker
        # can then read as a match even though this call already proved the
        # diff empty.
        #
        # This also covers `ready-for-review` step 3's invocation of
        # `/code-review` with nothing staged, where a stale empty-diff
        # marker from an unrelated earlier review could otherwise silently
        # skip that cumulative-diff review.
        if [ -n "$GATE_DIFF_BASE" ]; then
          EMPTY_DIFF_HASH=$(_lib_hash_diff_text "$(_lib_code_review_empty_base_sentinel "$GATE_DIFF_BASE")")
        else
          EMPTY_DIFF_HASH="$_HASH_STAGED_DIFF_EMPTY_DIGEST"
        fi
        if [ "$MARKER_VALUE" = "$EMPTY_DIFF_HASH" ]; then
          printf 'no-match\n'
          exit 1
        fi
        REPO_HASH=$(_marker_lib_repo_hash "$REPO_ROOT")
        # A hash match older than CODE_REVIEW_CHECK_MAX_AGE_SECONDS reads as
        # no-match too. See docs/design-decisions.md for why this age bound
        # applies only here, not to the write/commit-gate side.
        if _lib_marker_value_present "$CONFIG_DIR/code-review-markers" "$MARKER_VALUE" "$REPO_HASH."; then
          _resolve_code_review_check_max_age_seconds
          if FRESH_AGE=$(_code_review_marker_fresh_age "$CONFIG_DIR/code-review-markers" "$MARKER_VALUE" "$REPO_HASH." "$CODE_REVIEW_CHECK_MAX_AGE_SECONDS"); then
            printf 'match age_seconds=%s\n' "$FRESH_AGE"
            exit 0
          fi
        fi
        printf 'no-match\n'
        exit 1
        ;;
      *)
        printf "marker.sh: 'check %s' is not valid. 'check' supports: code-review\n" "$SKILL" >&2
        exit 2
        ;;
    esac
    ;;
esac
