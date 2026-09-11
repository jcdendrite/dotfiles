# Per-state merge-tree recipe and anchor-admissibility argument for `_lib_gate_diff_base`

*2026-09-08.*

`_lib_gate_diff_base` (`claude/.claude/hooks/_lib.sh`) computes the tree-ish a commit-time gate should diff its staged content against, in place of the index's implicit `HEAD` base, so a gate hashing `git diff --cached "$(_lib_gate_diff_base "$repo")"` sees only content novel to the commit being made, even mid-merge/rebase/cherry-pick/revert.

## Per-state `merge-tree --write-tree` recipe

The exact base and tree-ish pair, matching the state-detection table in `git-state-safety/SKILL.md`:

| State | Command |
|---|---|
| rebase | `merge-tree --write-tree --merge-base=REBASE_HEAD^ HEAD REBASE_HEAD` |
| merge | `merge-tree --write-tree HEAD MERGE_HEAD` |
| cherry-pick | `merge-tree --write-tree --merge-base=CHERRY_PICK_HEAD^ HEAD CHERRY_PICK_HEAD` |
| revert | `merge-tree --write-tree --merge-base=REVERT_HEAD HEAD REVERT_HEAD^` |

`git diff --cached "$base"` against the resulting tree is then exactly what the author staged on top of it: conflict resolutions plus any hand edits, with nothing the other side of the operation brought in untouched.

## Why both trust anchors are admitted despite neither being unforgeable

Presence of `MERGE_HEAD`/`CHERRY_PICK_HEAD`/`REVERT_HEAD`/`rebase-merge` proves nothing by itself — each is a plain gitdir file an ungated `git update-ref` or `printf` can fabricate to point at arbitrary content. The state's own OID must first reach one of two anchors via `git merge-base --is-ancestor`: the resolved default remote-tracking branch (`origin/<default>`) or `HEAD`.

Neither anchor is unforgeable. Both are admitted anyway because honestly reaching either implies the content already passed review. Forging either is no cheaper than building a commit with `commit-tree` and merging it in cleanly, a route that already reaches a commit with no gate seeing it at all, forgery or not. Admitting the anchor therefore costs nothing beyond a bypass this design already accepts elsewhere: an agent with local git access can already fabricate a trusted-looking merge state without a gate seeing it. See `.claude/plans/merge-aware-review-gates.md` for the fuller residual-risk inventory.

Reaching neither anchor falls back to the empty base — over-scoping the diff rather than smuggling content past the hash — for any of:

- An unrelated cherry-pick source.
- A garbage or dangling OID.
- An octopus `MERGE_HEAD` (multi-line, never a valid single revision).
- A `--rebase-merges` replay of a merge commit, where `REBASE_HEAD^` would silently resolve to the wrong parent.

## Why `state_oid` is shape-validated before use

`state_oid` comes from a plain gitdir file (`REBASE_HEAD`, `MERGE_HEAD`, `CHERRY_PICK_HEAD`, or `REVERT_HEAD`), not from git itself. Nothing upstream guarantees its content is a real OID rather than attacker-controlled option-injection content, e.g. a leading `--upload-pack=`. `_lib_gate_diff_base` therefore requires `state_oid` to match one of git's two supported OID lengths — a bare 40-hex-char (SHA-1) or 64-hex-char (SHA-256) string — before passing it as a positional argument to any `git merge-base` or `git merge-tree` invocation. A value matching neither shape falls back to the empty base without reaching either command.

## Validating `merge-tree --write-tree` output

`merge-tree --write-tree`'s stdout is validated by taking its first line via parameter expansion (the same idiom `_lib_extract_git_subcmd` uses elsewhere in `_lib.sh`) and requiring `git rev-parse --verify --quiet "<line>^{tree}"` to succeed against it. This avoids a `head -1` subprocess. The function contains no explicit git-version check. A git binary older than 2.38 rejects `--write-tree` outright and produces no valid tree line to verify. A git binary that accepts `--write-tree` but rejects the `--merge-base=` option (used on the rebase, cherry-pick, and revert paths) fails validation the same way, one level later. Either case fails `rev-parse --verify` and falls back to the empty base rather than to a partially-computed or malformed tree.

## Why "exit 2 => empty stdout" is load-bearing

Every caller of `_lib_gate_diff_base` consumes its stdout unconditionally, regardless of its exit status. A partial or candidate OID escaping to stdout on a kill path — an in-flight capped git call that timed out, was killed, or found its binary missing — would therefore be consumed by a caller as if it were a real, validated base. That would narrow an authorization hash on nobody's authority, since the escaped value never passed the anchor-admissibility or tree-validation checks above. No caller changes its allow/deny decision on status 2 alone. Each applies the same empty-base over-gating it already applies to status 1, per its own existing fail posture. A caller may additionally name the undetermined base in its own deny message.

This distinction does not extend to the trust anchor's own check. `git merge-base --is-ancestor`'s non-zero exit — whether driven by the timeout cap or by a genuine "not an ancestor" result — is always treated as "not trusted." Collapsing those failure modes is safe because either cause produces the same conservative empty-base fallback that status 1 already produces.
