# What `require-plan-review.sh`'s tier-2 sibling-worktree matching authorizes, and its cost shape

*2026-09-08.*

`require-plan-review.sh`'s completion-marker check runs in two tiers. Tier 1 hashes the active plan set against this repo's own marker directory prefix. Tier 2, which only runs after tier 1 misses, additionally hashes every sibling worktree's own repo-hash prefix, so a review recorded in one worktree of a repository also releases the gate in another worktree of the same repository holding byte-identical plan text.

## What a tier-2 hit authorizes

Content-identity of the plan text, not state-identity of the sibling's checkout. Two worktrees on divergent branches that hold byte-identical plan files cross-validate even though neither review assessed the other's `HEAD`. This is bounded to one repository's own worktrees (via `git worktree list`), so it is not an external surface, but it is a broader acceptance than the copied-plan-into-the-same-worktree case alone.

## Cost shape

Tier 1 misses for the whole window between authoring a plan and its first clean `/plan-review` — the normal state of a session actively drafting. So tier 2's `git worktree list` fork plus one `sha256sum` per worktree is a per-edit steady-state cost during drafting, not an occasional deny-path cost. Worktree count and marker count both grow unboundedly and independently, so that cost compounds over a repository's life. This is an accepted cost, not a caching bug — see `require-plan-review.sh`'s own comment above its `GATE_DIFF_BASE` resolution for the parallel, per-edit merge-tree cost this hook already pays on every gated call.
