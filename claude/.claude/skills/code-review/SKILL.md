---
name: code-review
description: Principal engineer code review of changed/new code before presenting to user
allowed-tools: Read, Grep, Glob
---

Review the code that was just written or modified. Act as a principal engineer reviewing a junior engineer's work. Be thorough but not pedantic.

**Core principle: review the ripple effects, not just the change.** The
checklist below catches issues within the change. The ripple effect triage
step (at the end) catches cross-boundary impacts — a migration breaking
frontend workflows, an API shape change breaking consumers, a rename
breaking callers in another domain.

## Step 0 — Detect changed domains

Before reviewing, determine which files were changed (from context, git diff, or the conversation). Classify each changed file into one or more domains:

- **Infrastructure**: `.github/`, `*.tf`, `Dockerfile`, `docker-compose*`, CI/CD configs
- **Data**: `**/migrations/**`, `*.sql`, schema definitions
- **Frontend**: `*.tsx`, `*.jsx`, `*.css`, `src/components/**`, `src/pages/**`
- **Backend**: edge functions, API routes, server-side utilities, `*.go`, `*.py`
- **Claude Code config**: `.claude/**`
- **Lovable config**: `.lovable/**`

Apply the **Base checklist** always. Apply each **Domain checklist** only when at least one changed file matches that domain.

## Base checklist

Evaluate the code against each item. Only flag items where there is a concrete issue — do not flag items just to show you checked them.

### Correctness

1. **API misuse** — Are libraries, frameworks, and language APIs used as designed? Flag any reliance on accidental or undocumented behavior (e.g., passing invalid arguments that happen to work, using internal methods, relying on side effects of unrelated calls).

2. **Silent error swallowing** — Are there catch blocks, fallback defaults, or error handlers that hide failures the caller would want to know about? Empty catch blocks, catch-and-return-null, and catch-and-log-only are all suspects.

3. **Race conditions** — Is shared mutable state accessed concurrently without synchronization? Check module-level variables, singletons, caches, and lazy-init patterns.

4. **Silent defaults for unexpected values** — Does the code silently substitute a default when it encounters an unexpected value (e.g., unknown enum variant, unrecognized config key)? In infrastructure and test code, prefer throwing over guessing.

### Hygiene

5. **Dead exports** — Are there exported types, functions, or constants that are not imported by any other file? Check with grep before flagging.

6. **Unnecessary wrappers** — Are there functions that simply delegate to another function without adding any logic, type narrowing, or meaningful naming? These add indirection without value.

7. **Inline business logic where a library method exists** — Is there hand-rolled logic (regex parsing, string manipulation, date math, data structure operations) where the project's existing dependencies already provide a tested, maintained function for the same thing?

### Clarity

8. **Undocumented limitations** — Does the code make assumptions or have known constraints that aren't visible to future readers? Examples: only handling the first element of a list, assuming single-tenant usage, ignoring edge cases by design.

9. **Misleading names** — Do function or variable names promise more or less than they deliver? A function called `validateUser` that only checks one field, or a variable called `allItems` that contains a filtered subset.

### Security

10. **Test adequacy for security controls** — For code that enforces security invariants (access control, input validation, privilege boundaries), are there tests that verify both the allow and deny paths? This overrides the general "add tests" exclusion — untested security controls are indistinguishable from absent ones. Check: for each security boundary, is there a test that an unauthorized caller is rejected AND an authorized caller succeeds?

### Scope discipline

11. **Pre-existing issues in unchanged code** — If you notice issues in code that was NOT written or modified in this change, flag them in a separate "Pre-existing issues" section. Do NOT fix them — they are informational only and out of scope.

## Domain: Infrastructure

12. **Concurrency and parallelism scoping** — Do concurrency groups, mutex locks, or job dependencies match their intended scope? A workflow-level concurrency group affects all jobs, including no-op or unrelated ones. Check that cancel-in-progress won't kill an important job due to an unrelated trigger.

13. **Secret exposure** — Are secrets used in contexts that could log them? Check for secrets in `run:` commands that echo or pipe output, in `env:` blocks visible to steps that don't need them, and in artifact uploads. Ensure secrets are not passed as command-line arguments (visible in process lists).

14. **Permissions least privilege** — Are workflow permissions, IAM roles, or service accounts scoped to what's actually needed? Flag `contents: write` when only `read` is required, `admin` when `write` suffices, or wildcard permissions.

15. **Idempotency** — Is the workflow/script safe to re-run? Check for unconditional creates (without "if not exists"), non-atomic operations that leave partial state on failure, and missing cleanup on retry.

16. **Trigger-condition alignment** — Do trigger filters (branch, path, actor, event type) match the job's purpose? A job intended only for bot commits but triggered on all pushes is a mismatch even if individual steps have `if` guards.

## Domain: Data

17. **Migration reversibility** — Can this migration be rolled back without data loss? Flag destructive operations (`DROP COLUMN`, `DROP TABLE`, type narrowing) that have no corresponding backup or reversal strategy.

18. **Index coverage** — Do new queries or new foreign keys have supporting indexes? Flag new columns used in WHERE, JOIN, or ORDER BY clauses that lack indexes, especially on tables expected to grow.

19. **Lock safety** — Could this migration take a long-running lock on a large table? `ALTER TABLE` with defaults, `CREATE INDEX` without `CONCURRENTLY`, and backfills inside the migration are suspects.

20. **RLS and access control on new tables** — Do new tables have RLS enabled and appropriate policies? A new table with no RLS is accessible to any authenticated user via the PostgREST API.

## Domain: Frontend

21. **Accessibility** — Do interactive elements have accessible names (aria-label, visible label, alt text)? Are click handlers on non-button elements keyboard-accessible? Check for missing focus management in modals and drawers.

22. **Render performance** — Are there new inline object/array/function literals in JSX props that would cause child re-renders on every parent render? Check for missing `key` props on list items and expensive computations not wrapped in `useMemo`/`useCallback` where the component re-renders frequently.

23. **Bundle impact** — Does the change add a large new dependency where a smaller alternative or existing utility exists? Flag full-library imports (e.g., all of lodash) when only one function is used.

24. **State-dependent rendering coverage** — Does the change modify which UI state a component enters (conditional branches, state machines, context-driven rendering)? If so, check whether component tests exist that verify the affected states render correctly. For each new or changed condition, is there a test that the component renders the expected output for each branch?

## Domain: Backend

25. **Auth boundary coverage** — Does every new endpoint or RPC have both authentication (who is the caller?) and authorization (can they do this?)? Check that auth checks are not bypassable by hitting the endpoint directly rather than through the expected UI flow.

26. **Input validation at system boundaries** — Is user input validated/sanitized before use in SQL queries, shell commands, file paths, or external API calls? Framework-provided parameterization counts; string concatenation does not.

27. **Error response leakage** — Do error responses expose internal details (stack traces, internal IDs, database error messages, file paths) to the caller? Internal errors should be logged server-side and return a generic message to the client.

## Domain: Claude Code config

28. **Skill trigger accuracy** — Do TRIGGER and DO NOT TRIGGER conditions
    match the skill's actual purpose? A skill that triggers too broadly wastes
    context; one that triggers too narrowly gets skipped when needed.

29. **Context budget** — Are skill files, plan files, and settings concise enough
    to fit within the AI's working context without displacing active task
    instructions? Long files dilute attention on the actual task. Flag files that
    could be shortened without losing actionable information.

30. **Permission scope** — Do `permissions.allow` rules in settings.json follow
    least-privilege? Flag blanket allows (`"Bash"`) where scoped allows
    (`"Bash(git:*)"`) would suffice.

31. **Hook correctness** — Do PreToolUse/PostToolUse hooks block the right
    operations without false positives? A hook that blocks legitimate work
    is worse than no hook — it trains users to bypass the system.

## Domain: Lovable config

Apply when changed files match `.lovable/**`.

32. **Perspective** — Are instructions written from Lovable's perspective
    (second person, addressed to Lovable)? Knowledge files that read as
    internal engineering notes will confuse Lovable.

33. **Specificity** — Are instructions specific enough to prevent unintended
    behavior? Lovable follows instructions literally and may over-apply vague
    guidance (e.g., "be careful with auth" → Lovable adds auth checks to
    public endpoints).

34. **Context budget** — Are knowledge files concise enough to fit within
    Lovable's working context without displacing active task instructions?
    Same principle as Claude Code skills — long knowledge files dilute
    attention.

35. **Sync status** — If project-knowledge.md or workspace-knowledge.md
    changed, does the PR description mention syncing to the Lovable UI?
    The file is the source of truth, but Lovable reads from the UI field.

## Exclusions — do NOT flag these

- Issues that a linter, typechecker, or compiler would catch (imports, type errors, formatting)
- Stylistic nitpicks in unchanged code (naming conventions, whitespace, comment style)
- Generic improvement suggestions ("add tests," "add docs," "improve error messages") not tied to a specific finding from the checklist above, **except** for security controls (see item 10)
- Domain checklist items for domains where no files were changed

## Output format

Start with a one-line summary of which domains were detected (e.g., "Domains: Infrastructure, Backend").

For each finding, state:

1. **Which checklist item** (by number and name)
2. **File and line**
3. **What the issue is** (one sentence)
4. **Why it matters** (one sentence)
5. **Suggested fix** (concrete, not "consider improving")

If no issues are found, say: "No issues found" — do not pad with praise or generic observations.

## Ripple effect triage

After the checklist review, identify whether the change crosses system
boundaries and recommend specialist follow-up reviews. This step is
**always required** — even if the checklist found no issues, ripple
effects may exist that only a domain specialist would catch.

Evaluate the change against these cross-boundary patterns. These are
review *perspectives*, not org chart roles — one person may wear multiple
hats. Update this table as new patterns emerge.

| Change type | Follow-up |
|-------------|-----------|
| Restricts DB access (RLS, GRANT, triggers) | **Product Engineer** — trace restrictions against caller code |
| Changes API response shape | **Product Engineer** — verify all consumers handle new shape |
| Adds/modifies security controls | **Senior SDET** — verify test pyramid and coverage |
| Changes auth model (JWT, roles, permissions) | **Security Engineer** — trace all auth paths |
| Modifies shared utilities (helpers, hooks, contexts) | **Backend/Frontend Engineer** — verify all call sites |
| Changes data model (columns, types, defaults) | **Product + Backend Engineer** — check queries, types, UI |
| Modifies infrastructure (CI, deploy, config) | **DevOps/Infra Engineer** — verify pipelines |

**Output:** If no impacts, state which boundaries you checked and why none
are affected. If impacts exist, add a **"Recommended follow-up reviews"**
section with entries like:
- **[Reviewer]:** This change [does what] — verify [specific workflows]
  by tracing [specific code paths].

Be specific about WHAT to check, not just WHO. "PE review recommended" is
useless; "PE should verify the checkout flow in CheckoutPage.tsx still works
after the new validation constraint" is actionable.
