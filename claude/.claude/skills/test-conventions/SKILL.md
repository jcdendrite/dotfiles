---
name: test-conventions
description: >
  Testing conventions for writing tests in any codebase: test pyramid layers,
  design for testability, test isolation, naming, test data, coverage judgment,
  and mock design.
  TRIGGER when: planning tests for new code, writing test infrastructure
  (mocks, helpers, fixtures), or discussing test strategy for new features.
  DO NOT TRIGGER when: a project-level test skill is already loaded, the task
  is purely mechanical (running tests, fixing a single assertion, updating a
  snapshot), or evaluating/debugging an existing test suite (use test-evaluation).
user-invocable: false
---

# Testing Conventions

## 1. Test pyramid

| Layer | Speed | External deps | What it covers | Volume |
|-------|-------|---------------|----------------|--------|
| **Unit** (stubbed deps) | Milliseconds | None | Branch logic, error paths, edge cases, data transformations | Many — every code path |
| **Integration** (real local services) | Hundreds of ms | Local DB, local services | Auth boundaries, response contracts, wiring between layers | Few — happy path + key error paths per endpoint or boundary |
| **Contract** (schema verification) | Milliseconds–seconds | None (consumer) or local (provider) | API schemas between services match consumer expectations | One per service boundary |
| **E2E** (real external APIs) | Seconds | Third-party APIs, real infra | Complete user flows work end-to-end | Few per critical flow, run pre-merge or scheduled |
| **Smoke** (post-deploy health) | Seconds | Production/staging infra | Critical paths are up after deployment | 1-2 per service, run on deploy only |

**Contract tests** verify that service-to-service API schemas stay compatible without requiring a running instance of the other service. Use them at any service boundary where teams deploy independently.

**E2E tests** verify complete user journeys across the full stack. **Smoke tests** are lightweight post-deploy checks that verify critical paths are functional — they answer "is it up?" not "does every flow work?"

## 2. Design for testability

### Dependency injection over internal construction
Functions should accept their dependencies as parameters, not create them internally. A function that constructs its own client internally cannot be tested without hitting the real service. Accepting the client as a parameter lets callers pass a test double.

### Make expected failure paths easy to assert on
In languages with explicit error types (Go, Rust, functional patterns), returning errors as values simplifies test assertions. In exception-oriented languages (Python, Java, C#, Ruby), use the framework's exception-assertion utilities (e.g., `pytest.raises`, JUnit's `assertThrows`). The key principle: expected failure paths should be as easy to test as success paths, using whatever the language's idiomatic mechanism is.

### Choose the right test double

| Double | Behavior | Use when |
|--------|----------|----------|
| **Stub** | Returns canned data, no call tracking | Testing your code's behavior given specific inputs |
| **Mock** | Records calls for assertion | Verifying your code called a dependency with the right arguments |
| **Fake** | Lightweight real implementation (in-memory DB, local HTTP server) | Testing without real infrastructure; also useful in unit tests when stubs are too complex (e.g., an in-memory repository that maintains state across calls) |
| **Spy** | Wraps the real implementation, records calls | You need real behavior but want to verify interaction |

Use the narrowest double that covers the test's intent. Prefer stubs for unit tests of pure logic; use mocks only when verifying interaction is the point of the test.

### Test double seams by dependency type
- **Database calls:** Stub/fake the client object, or use transaction rollback isolation (see section 3)
- **External HTTP APIs:** Intercept by URL pattern or use a fake HTTP server
- **Env vars / config:** Set and restore in setup/teardown blocks
- **Time:** Inject timestamps as parameters rather than relying on the system clock

## 3. Test isolation

### Avoid global state in tests when possible
Prefer designs that pass dependencies explicitly rather than relying on global state. When global state is unavoidable, follow the rules below.

### Global state must be saved and restored
When tests modify global state (env vars, global functions, singletons), always save the original and restore in a guaranteed cleanup block (teardown, `finally`, `defer`, etc.). **Never** unconditionally delete or overwrite global state — a test that removes a value without saving it first will break every subsequent test that needs it.

### Tests must be independent
- Never rely on test execution order
- Each test creates its own data and cleans up in teardown
- Use unique identifiers (timestamps, counters) to prevent cross-test collision

### Test double cleanup must be guaranteed
When replacing globals (HTTP client, fetch function, clock), always restore the original in a guaranteed cleanup block, even if the test fails or throws.

### Database test isolation
- **Transaction rollback pattern:** Wrap each test in a transaction and roll back at the end. Standard in Django, Rails, Spring, and most ORMs — more efficient than truncation.
- **Test database lifecycle:** Use a dedicated test database, run migrations before the suite, reset state between tests.
- **In-memory vs. real engine:** In-memory databases (e.g., SQLite in-memory mode) are fast but have dialect differences (JSON columns, CTEs, locking behavior). When dialect fidelity matters, use the same engine as production.

### Parallel-safe tests
Whether running locally or in CI, parallel test runners (pytest-xdist, Jest workers, Go's `t.Parallel()`, JUnit parallel mode) introduce isolation requirements:
- Never bind to hardcoded ports; use port 0 or dynamic allocation
- Use unique temp directories per test (e.g., `mkdtemp`, test-scoped `tmp_path`)
- When sharing a test database, use per-test schemas or transaction rollback isolation
- If a test mutates module-level state, it cannot safely run in parallel — document this constraint explicitly

## 4. Test naming and structure

Test names should describe the **scenario** and **expected outcome**, not just the function name:
- Good: `rejects_expired_token_with_401`, `returns_empty_list_when_no_matches`
- Bad: `test_refreshToken`, `test_search`, `it_works` (language-required prefixes like `test_` or `Test` are fine — the problem is having no scenario or expected outcome after the prefix)

A reader should understand what the test verifies without reading its body.

### Common naming structures
- `action_condition_expectedResult` — e.g., `search_withEmptyQuery_returnsEmptyList`
- `given_when_then` — e.g., `givenExpiredToken_whenRefreshCalled_thenReturns401`

Pick one convention per project and apply it consistently.

### Test body structure
Each test should follow the **Arrange / Act / Assert** (or Given / When / Then) pattern with clear visual separation between setup, action, and verification. Mixing these phases makes tests harder to diagnose when they fail.

### Regression test intent
For tests that guard against a specific past bug, include a comment or docstring referencing the issue. This prevents future developers from deleting a test that looks redundant but guards against a known failure.

## 5. Test data

- Use **factory/builder helpers** that supply sensible defaults; tests override only the fields relevant to the scenario
- **Avoid magic values** — if a test uses `status: 3`, name the constant or comment why 3 matters
- **Prefer inline construction** over shared fixtures when the data is central to the test's assertion
- **Shared fixtures** are appropriate for expensive setup (DB schemas, server instances), not for simple data objects
- For tests against shared databases, use unique prefixes/suffixes (run ID, timestamp) so parallel runs and stale data don't collide

## 6. Coverage judgment

### What each test layer should verify

**Unit tests:**
- Every `if/else` branch in the function
- Error return paths (missing input, API failure, invalid state)
- Edge cases (zero values, null, empty strings, boundary conditions)
- Side effects via mocks (did it call `.update()` with the right payload?)

**Integration tests:**
- **HTTP-level:** Auth rejection (401/403), authorized success (2xx), response shape — the actual output your endpoint produces (fields, types, status codes)
- **Service/module-level:** Wiring between layers maps results correctly, shared modules are called with expected arguments
- Not every integration test needs to go through HTTP — service-level integration tests are cheaper when you're verifying wiring, not auth or response shape
- Don't re-test every branch — that's the unit tests' job

**Contract tests:**
- Consumer expectations match the provider's actual API schema — the agreed-upon interface between services, independent of either side's implementation
- Distinct from integration response shape tests: contract tests verify the *schema agreement* between services; integration tests verify your *code's actual output*
- Run when either side changes; no need for a live instance of the other service

**E2E tests:**
- Complete user flows across the full stack
- Run pre-merge or on a schedule, not on every commit
- Use test/sandbox accounts and environments

**Smoke tests:**
- One lightweight check per critical path, post-deploy
- Never use smoke tests to verify branching logic or complete flows

### Security controls require both allow and deny paths
For any access control, auth check, or privilege boundary:
- **Deny test:** unauthorized caller is rejected (403/401)
- **Allow test:** authorized caller succeeds (200 + correct response)
- Untested security controls are indistinguishable from absent ones

### Concurrency coverage
For endpoints or functions that handle concurrent writes:
- Test with parallel requests to verify idempotency and conflict resolution
- Test for expected behavior under contention (optimistic locking failures, retry semantics, deadlock avoidance)

### Happy path alone is insufficient when:
- The function has validation logic (test invalid inputs)
- The function has auth/authorization (test unauthorized callers)
- The function handles partial failure (test one item failing in a batch)
- The function has opt-out/preference logic (test opted-out path)

## 7. Mock design principles

### Stub/mock fidelity
- Test doubles should behave like the real thing for the patterns actually used
- Document known limitations (e.g., "only supports single filter per chain")
- Unknown methods should fail loudly (throw), not silently return wrong data

### Mutation recording (mocks)
When mocking a client that performs writes, record the mutations for assertion:
- Capture table name, operation type, payload, and filter values
- Let tests assert "this function called update on table X with payload Y"

### Tautological mock test
If an assertion checks a value that was set up directly in the test double rather than derived by the code under test, you're testing the test double, not the code. The test will always pass regardless of the production code's behavior.

**Bad (tautological):** stub `getUser` to return `{name: "Alice"}`, then assert the result equals `"Alice"`. This always passes — you're testing the stub, not the code.

**Good (tests real logic):** stub `getUser` to return `{name: "Alice", role: "admin"}`, then assert the formatted display string equals `"Alice (Admin)"`. This tests the formatting/transformation logic the code actually performs.

## 8. Common authoring mistakes

Avoid these when writing new tests:

| Mistake | Fix |
|---|---|
| Assertion-free tests (code runs but nothing is asserted) | Every test must assert on a specific expected outcome |
| Sleep-based synchronization (`sleep(2)` for async work) | Use polling with timeout, await, or synchronization primitives |
| Hardcoded colliding test data (`id=1`, `email=test@example.com`) | Generate unique identifiers per test |
| Over-mocking (test encodes implementation, not behavior) | Mock only direct dependencies; let integration tests cover wiring |
| Mocking third-party internals (library's private API) | Use the library's test utilities or mock at your own abstraction boundary |
| Testing the test double (asserting stub's own return value) | Assert on values the code computed or transformed |
