---
name: plan-review
description: >
  Review implementation plans before presenting to the user. Evaluates against
  domain-specific checklists (backend, frontend, security, infrastructure, data)
  based on which domains the plan touches.
  TRIGGER when: an implementation plan has been written or updated in .claude/plans/
  or is about to be presented to the user for review.
  DO NOT TRIGGER when: the plan is a trivial one-liner (single migration, config
  change), or the user has explicitly said to skip review.
user-invocable: true
---

Review an implementation plan. Act as a review board evaluating a proposal before
engineering effort is committed. Be thorough but practical — flag real risks, not
hypothetical ones.

## Step 0 — Identify the plan

Find the plan to review. Check, in order:
1. If a plan file path was provided as an argument, read it
2. If a plan was just written in `.claude/plans/`, read the most recent one
3. If a plan exists in the current conversation context, use that

## Step 1 — Detect domains

Read the plan and classify which domains it touches:

- **Infrastructure**: CI/CD, workflows, deployment, hosting, config files
- **Data**: Database migrations, schema changes, indexes, RLS policies
- **Frontend**: React components, hooks, client-side state, UI behavior
- **Backend**: Edge functions, API routes, server-side logic, shared modules
- **Security**: Auth, authorization, token handling, secret management, data exposure

## Step 2 — Evaluate

Evaluate the plan against the **Base checklist** first, then each detected
**Domain checklist**. For multi-phase plans, evaluate each phase against the
relevant checklists. Reference the specific phase/section when reporting findings.

If this project also has a project-level plan-review skill, both skills will
trigger independently. This skill covers generic plan quality; the project
skill covers project-specific concerns.

## Base checklist

Evaluate the plan against each item. Only flag items where there is a concrete
issue — do not flag items just to show you checked them.

### Feasibility

B1. **Unstated assumptions** — Does the plan assume behavior of a library, framework,
   or SDK without verifying it? Check for claims about how APIs, clients, or protocols
   work. The most dangerous plans are the ones that sound correct but rely on behavior
   the author hasn't tested.

B2. **Missing consumer analysis** — Does the plan account for all callers, importers,
   or consumers of the code being changed? A plan that changes a response format
   without enumerating who reads that response will break things.

B3. **Breaking intermediate states** — During phased migrations, is there a window
   where some components use the old format and others use the new? Is that window
   safe, or will it cause runtime failures?

B4. **Unresolved external dependencies** — Does the plan depend on external services,
   APIs, or third-party tools whose availability, rate limits, or behavior the author
   hasn't verified? A plan that assumes an API endpoint exists or a service has a
   specific capability without checking is fragile.

### Scope

B5. **Proportionality** — Is the solution proportional to the problem? Flag both
   over-engineering (abstractions for one-time operations, premature extensibility)
   and under-engineering (band-aids that will need immediate follow-up).

B6. **Scope creep** — Does the plan include work that isn't required to solve the
   stated problem? Improvements to adjacent code, premature optimizations, or
   "while we're here" refactors should be captured in the **Out of Scope** section
   of the review output — don't lose the observation, but don't plan for it either.

B7. **Missing scope** — Does the plan omit work that IS required? Common gaps:
   test updates for breaking changes, documentation updates, migration rollback
   strategy, frontend changes for backend format changes.

### Risk

B8. **Phase independence** — For multi-phase plans, can each phase be merged and
   deployed independently without breaking the system? Can any phase be reverted
   without reverting all subsequent phases? Are there cross-phase dependencies
   that would leave the system in a broken state if a later phase is delayed?

B9. **Test realism** — Are the planned test assertions realistic given the changes?
   Will existing tests actually break as claimed? Are new test scenarios sufficient
   to catch regressions?

B10. **Rollback strategy** — For destructive or hard-to-reverse changes (data
   migrations, API format changes, dependency removals), is there a rollback plan?
   Or is the change structured to be safely reversible by default?

B11. **Dependency risk** — Does the plan add, upgrade, or remove dependencies? If so,
   does it account for transitive dependency conflicts, license implications, and
   the maintenance health of new dependencies?

### Clarity

B12. **Ambiguous instructions** — Could an implementer misinterpret the plan and
    produce the wrong result? Look for instructions that describe the wrong file,
    wrong pattern, or make claims about code structure that don't match reality.

B13. **Missing decision rationale** — Are design choices explained? A plan that says
    "use approach X" without explaining why X was chosen over Y leaves the implementer
    unable to make judgment calls when they encounter edge cases.

## Domain: Infrastructure

Apply when the plan touches CI/CD, workflows, deployment, or config.

I1. **Environment parity** — Will the change work in all environments (local dev,
    CI, staging, production)? Plans that work locally but fail in CI (different OS,
    missing tools, permission differences) are common.

I2. **Idempotency** — Can the infrastructure change be applied multiple times safely?
    Migrations, deployments, and config changes should not fail on re-run.

I3. **Deployment ordering** — Does the plan require infrastructure changes to be
    deployed in a specific order relative to application changes? A backend that
    expects a new env var before it's provisioned, or a migration that must run
    before new code reads the column, are ordering dependencies that the plan
    should make explicit.

I4. **Secret and config provisioning** — Does the plan introduce new secrets,
    environment variables, or config values? If so, does it specify where and how
    they are provisioned in each environment? Missing a secret in production while
    it works locally is a common deployment failure.

## Domain: Data

Apply when the plan touches database schema, migrations, or data.

D1. **Migration safety** — Can the migration run on a live database without downtime?
    Flag schema changes that take locks on large tables, backfills that run inside
    transactions, or destructive operations without backup strategy. Also flag
    `NOT NULL` additions without defaults, column type changes that require rewrites,
    and `CREATE INDEX` without `CONCURRENTLY` on large tables.

D2. **Migration reversibility** — Can this migration be rolled back without data loss?
    Flag destructive operations (`DROP COLUMN`, `DROP TABLE`, type narrowing) that
    have no corresponding backup or reversal strategy.

D3. **Deploy-time compatibility** — During deployment, old code may run against the
    new schema (or vice versa). Does the plan account for this? Renaming a column
    that old code still references, or adding a `NOT NULL` constraint before new code
    populates the column, will cause failures during the deploy window.

D4. **Access control on new objects** — Do new tables, views, or functions have
    appropriate access control? A new table without RLS is accessible to any
    authenticated user.

D5. **Index coverage** — Do new queries, new foreign keys, or new filter patterns
    have supporting indexes? Flag new columns used in `WHERE`, `JOIN`, or
    `ORDER BY` clauses that lack indexes, especially on tables expected to grow.

## Domain: Frontend

Apply when the plan touches React components, hooks, or client-side code.

F1. **User-facing impact** — Does the plan account for how changes affect the user
    experience? Error message changes, loading state changes, and behavioral changes
    should be called out explicitly.

F2. **State management** — Does the plan account for client-side state that depends
    on the changed backend behavior? Cached data, optimistic updates, and polling
    intervals may need updating.

F3. **Query contract mapping** — If the plan changes a backend response format, does
    the frontend consume the new shape correctly? Check that React Query keys,
    selector functions, and type definitions are updated to match the new contract.

F4. **Loading, error, and empty states** — Does the plan cover all three states for
    new or changed data-fetching paths? Plans that describe only the happy path leave
    the implementer to improvise error and empty states, which often results in
    missing or inconsistent UX.

F5. **Auth state transitions** — If the plan touches authentication or session
    handling, does it account for auth state transitions (logged-in to logged-out,
    token refresh, session expiry) and how they affect the UI? Stale auth state is
    a common source of broken UX.

## Domain: Backend

Apply when the plan touches edge functions, API routes, or server-side code.

K1. **Contract compatibility** — Does the plan maintain backward compatibility with
    existing callers during the transition? If not, is the breaking change coordinated
    with frontend/consumer updates?

K2. **Error handling completeness** — Does the plan cover both success and error paths
    for new or changed endpoints? Plans that only describe the happy path miss half
    the implementation.

## Domain: Security

Apply when the plan touches auth, authorization, secrets, tokens, or data exposure.

S1. **Threat model** — Does the plan identify what an attacker could do if the
    implementation has a bug? Plans that add auth or access control should enumerate
    bypass vectors.

S2. **Defense in depth** — Does the plan rely on a single control, or are there
    layered defenses? A plan that says "RLS will handle it" without in-code checks
    is single-layer.

S3. **Auth boundary coverage** — Does every new endpoint, RPC, or data path have
    both authentication (who is the caller?) and authorization (can they do this
    specific action on this specific resource)? Plans that mention "add auth" without
    specifying both layers leave gaps.

S4. **Privilege escalation paths** — Could a user exploit the planned changes to
    gain access to another user's data or perform actions beyond their role? Check
    for IDOR vectors, role-check gaps, and cases where user-supplied IDs are trusted
    without ownership verification.

S5. **Data minimization** — Does the plan expose more data than necessary in API
    responses, logs, or error messages? Check for full object returns where only
    specific fields are needed, and for internal details (stack traces, query text,
    internal IDs) leaking to callers.

S6. **Secret lifecycle** — If the plan introduces, rotates, or references secrets
    (API keys, tokens, credentials), does it account for how they are provisioned,
    where they are stored, and what happens when they expire or are compromised?

## Exclusions — do NOT flag these

- Style preferences (naming, formatting, file organization) unless they cause ambiguity
- "Consider adding" suggestions not tied to a specific checklist finding
- Theoretical risks with no concrete attack vector or failure scenario
- Domain checklist items for domains the plan doesn't touch
- Generic "add more tests" suggestions, **except** for security controls where
  untested invariants are indistinguishable from absent ones (see S1)

## Reviewer roles

When spawning reviewer agents, adopt the persona that matches each detected
domain. Different personas catch different things — a security reviewer thinks
about attack vectors while a backend reviewer thinks about API contracts.

| Domain | Reviewer role | Focus |
|--------|--------------|-------|
| Backend | Staff backend engineer | API contracts, error handling, idempotency, retry semantics, service boundaries, SDK behavior |
| Frontend | Staff frontend engineer | Component patterns, state management, data fetching and cache consistency, accessibility, UX impact |
| Security | CISO | Threat modeling, auth boundaries, privilege escalation, data exposure, defense in depth |
| Data | Staff data engineer | Migration safety, schema design, reversibility, deploy-time compatibility, index coverage, access control on new objects |
| Infrastructure | Staff DevOps engineer | CI/CD pipelines, IaC, deployment ordering, environment parity, secret provisioning |

For multi-domain plans, evaluate from each relevant persona. Always include the
CISO persona when the plan touches auth, authorization, secrets, tokens, data
exposure, logging of sensitive data, third-party data sharing, or infrastructure
permissions.

Project-level plan-review skills may extend this table with project-specific
reviewer roles and focus areas, but must not remove or narrow the CISO trigger
conditions.

## Output format

Start with which domains were detected and which plan sections/phases were reviewed.

For each finding, state:
1. **Which checklist item** (ID and name, e.g., "B3 — Breaking intermediate states")
2. **Which plan section or phase** the finding applies to
3. **What the issue is** (one sentence)
4. **Why it matters** (one sentence)
5. **Suggested resolution** (concrete, not "consider improving")

If any items were flagged by B6 (scope creep), include an **Out of Scope** section
listing them. These are observations worth preserving — the reviewer can decide
whether to bring them into scope or create follow-up tickets.

End with a verdict: **Approve**, **Approve with changes** (list what), or
**Request changes** (list blockers).
