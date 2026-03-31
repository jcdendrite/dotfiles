---
name: test-evaluation
description: >
  Evaluate and debug existing test suites: diagnose inverted pyramids, identify
  wrong-layered tests, root-cause flaky tests, and spot common anti-patterns.
  TRIGGER when: reviewing or evaluating an existing test suite, debugging slow
  or flaky tests, assessing test coverage quality, or reviewing test code.
  DO NOT TRIGGER when: writing new tests (use test-conventions), or the task is
  purely mechanical (running tests, fixing a single assertion, updating a snapshot).
user-invocable: false
---

# Test Evaluation

## 1. Signs of an inverted pyramid

- Most tests call real services (DB, APIs) to test internal branching logic
- Tests are slow (>1s each) because of network round-trips
- Individual unit tests taking >50-100ms, indicating a hidden real dependency or overly complex setup
- External API rate limits constrain how often you can run the test suite
- Tests are flaky because of network timeouts or service availability
- You can't run the test suite offline or in CI without credentials
- Adding a new code branch requires setting up complex test state in a real database

## 2. When to consider moving a test down the pyramid

These are heuristics, not rules — evaluate each case against the specific codebase's constraints.

**Integration → unit when:**
- The test sets up complex DB state to exercise one `if` branch
- The test hits an external API just to test error handling
- The test is slow (>500ms) and tests pure logic, not wiring
- You're hitting rate limits on external APIs

**E2E → integration when:**
- The test calls a real third-party API to verify your code handles errors
- The test is flaky because of network conditions
- The test creates real resources (emails, contacts) as a side effect

Note: smoke tests serve a fundamentally different purpose (verifying deployment health, not behavior). They don't move down the pyramid — if a smoke test is failing, the issue is the deployment or infrastructure, not the test layer.

## 3. Flaky test diagnosis

When a test is intermittently failing:

1. **Root-cause first** — diagnose the specific cause:
   - Test isolation failure (shared state, execution order dependency)
   - External service timing (network, container startup, API availability)
   - Time zone sensitivity (test passes in one TZ, fails in another — common with date boundary logic)
   - Locale-dependent formatting (number/date formats vary by system locale)
   - Floating point comparison (use approximate equality, not exact)
   - Async timing (asserting on async state without proper synchronization)
   - Non-deterministic iteration order (hash maps/sets with no guaranteed order)
   - Port or resource conflicts in parallel execution
2. **Fix the layer** — if the test is flaky because it hits a real service to test logic, push it down the pyramid (see section 2)
3. **Quarantine in-test retries** — retry loops inside test code mask the underlying issue. If a fix isn't immediate, quarantine the test (skip with a tracking issue) rather than letting it erode trust in the suite. CI-level retry policies (rerun failed tests once before failing the build) are a separate, reasonable practice for transient infrastructure issues — but track retry rates and investigate if a test needs retries frequently
4. **Never delete without replacement** — a flaky integration test that covers an auth boundary still represents needed coverage. Replace it with a reliable test at the right layer before removing it

## 4. Anti-patterns

| Anti-pattern | Why it's wrong | Fix |
|---|---|---|
| All tests are integration tests | Slow, flaky, can't test every branch | Extract logic, add unit tests with stubs |
| Testing the test double | Assertions check values set up in the stub, not derived by code under test | Assert on values the code computed or transformed |
| No integration tests at all | Auth bugs, response shape regressions | Keep a few focused integration tests per endpoint |
| Smoke tests on every commit | Slow CI, rate limit exhaustion, flaky | Run smoke tests on deploy only |
| Testing implementation details | Tests break on refactor with no behavior change | Test inputs and outputs, not internal mechanics |
| Tautological assertions | `assert("error" in body or "data" in body)` passes on any response | Assert specific values |
| Duplicating production code in tests | Test passes with stale copy, drift | Import or call via integration |
| Reading source files to test behavior | Tests source text, not runtime behavior | Call function, assert output |
| Unconditional global state deletion | Breaks subsequent tests that need the value | Save and restore in guaranteed cleanup |
| In-test retry loops for flaky tests | Masks root cause, inflates suite duration | Root-cause and fix or quarantine |
| Test interdependence | Test B depends on state from Test A; reorder breaks both | Each test sets up and tears down its own state |
| Assertion-free tests | Test runs code but never asserts; passes as long as nothing throws | Every test must assert on a specific expected outcome |
| Sleep-based synchronization | `sleep(2)` to wait for async work; slow and still flaky | Use polling with timeout, await, or synchronization primitives |
| Hardcoded colliding test data | Every test uses `id=1` or `email=test@example.com`; parallel runs collide | Generate unique identifiers per test |
| Over-mocking | So many mocks that the test encodes the implementation, not behavior; brittle to refactors | Mock only direct dependencies; let integration tests cover wiring |
| Mocking third-party internals | Mocking a library's internal API rather than its public interface | Use the library's test utilities or mock at your own abstraction boundary |
