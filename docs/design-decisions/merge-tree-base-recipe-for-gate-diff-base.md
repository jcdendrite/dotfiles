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

Neither anchor is unforgeable. Both are admitted anyway because honestly reaching either implies the content already passed review, and forging either is no cheaper than building a commit with `commit-tree` and merging it in cleanly — a route that already reaches a commit with no gate seeing it at all, forgery or not. Admitting the anchor therefore costs nothing beyond a bypass this design already accepts elsewhere (the same ungated-local-plumbing residual named in `.claude/plans/merge-aware-review-gates.md`'s G-6).

Reaching neither anchor falls back to the empty base — over-scoping the diff rather than smuggling content past the hash — for any of:

- An unrelated cherry-pick source.
- A garbage or dangling OID.
- An octopus `MERGE_HEAD` (multi-line, never a valid single revision).
- A `--rebase-merges` replay of a merge commit, where `REBASE_HEAD^` would silently resolve to the wrong parent.
