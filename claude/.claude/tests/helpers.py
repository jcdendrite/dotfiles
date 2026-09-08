"""Pure helpers and path constants shared across hook, skill, and script test files.

No pytest decorators here — this is a plain Python module. Import
explicitly from each test file that needs these symbols.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml

CLAUDE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CLAUDE_DIR.parent.parent

HOOKS_DIR = CLAUDE_DIR / "hooks"
SKILLS_DIR = REPO_ROOT / "claude-skills" / "skills"
SCRIPTS_DIR = CLAUDE_DIR / "scripts"

_CI_DETECT_STEP_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# SKILL.md fences may be indented when the fixture sits inside a
# numbered list (e.g. respond-pr's "0. **Enable hook bypass.**"). The
# closing-fence match has to tolerate the same leading whitespace as
# the opening, otherwise the non-greedy body capture runs past every
# indented fence until it finds an unindented one elsewhere in the file.
_SKILL_FIXTURE_RE = re.compile(
    r"<!--\s*HOOK_TEST_FIXTURE:\s*(?P<id>[A-Za-z0-9_-]+)\b[^>]*-->\s*"
    r"```[a-z]*\n(?P<body>.*?)\n[ \t]*```",
    re.DOTALL,
)


def extract_skill_command(skill_path: Path, fixture_id: str) -> str:
    """Return the body of the fenced code block tagged with `fixture_id`.

    SKILL.md files mark hook-alignment fixtures with
    `<!-- HOOK_TEST_FIXTURE: <id> -->` immediately followed by a fenced
    code block. Reading the recipe from SKILL.md at test time (rather
    than embedding a hardcoded copy in the test source) makes SKILL.md
    the single source of truth — drift between the documented recipe
    and what the test executes can't happen silently.
    """
    text = skill_path.read_text()
    matches = [m for m in _SKILL_FIXTURE_RE.finditer(text) if m.group("id") == fixture_id]
    if not matches:
        raise AssertionError(
            f"HOOK_TEST_FIXTURE '{fixture_id}' not found in {skill_path} — "
            "either the marker was removed or the immediately-following "
            "fenced block is missing."
        )
    if len(matches) > 1:
        raise AssertionError(
            f"HOOK_TEST_FIXTURE '{fixture_id}' appears {len(matches)} times in "
            f"{skill_path} — fixture ids must be unique so the test runs the "
            "intended block."
        )
    return matches[0].group("body").strip()


def _build_subprocess_env(
    home: Path | None,
    extra_env: dict | None,
) -> dict | None:
    """Build a subprocess env with optional HOME override and extra variables.

    Returns None when neither argument is provided, so subprocess.run inherits
    the parent environment as-is — preserving PATH for hook tool lookups
    (jq, grep, git, etc.).

    Full parent env — including any ambient CLAUDE_CONFIG_DIR — is always
    inherited; this function can't distinguish an ambient leak from a test's
    deliberate `monkeypatch.setenv`, so a caller needing it cleared must
    `monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)` before calling.
    """
    if home is None and extra_env is None:
        return None
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    if extra_env is not None:
        env.update(extra_env)
    return env


def run_hook(
    hook: Path,
    tool_input: dict,
    cwd: Path | None = None,
    home: Path | None = None,
    extra_env: dict | None = None,
) -> str:
    """Invoke `hook` with `tool_input` as JSON stdin. Return the decision.

    Silent exit 0 (empty stdout) maps to "allow" to match the hook protocol,
    where absence of output means "no opinion". Exit 2 (empty stdout) maps to
    "deny": per the harness's PreToolUse contract, exit 2 is itself the
    blocking signal, delivered via stderr rather than a JSON payload — a gate
    hook's jq-absent fallback takes exactly this path. Without this mapping,
    an exit-2 block with empty stdout would be misread as "allow". A
    non-empty stdout payload is expected to carry
    `hookSpecificOutput.permissionDecision` — missing it raises `KeyError`,
    since for every hook that always emits `hookSpecificOutput` that shape
    break is itself a regression worth a hard test failure. Hooks that
    legitimately emit a decision-less advisory payload (e.g. a PostToolUse
    `systemMessage`, or a `hookSpecificOutput.additionalContext` with no
    `permissionDecision` key at all) should use `run_hook_advisory` instead,
    which treats that absence as "no opinion" rather than a broken payload.

    home: when set, overrides $HOME in the subprocess environment so the
    hook writes into an isolated temp directory rather than real ~/.claude.
    extra_env: additional environment variables merged on top of the base env
    (applied after home override, so extra_env can also override HOME).
    """
    env = _build_subprocess_env(home, extra_env)
    result = subprocess.run(
        [str(hook)],
        input=json.dumps(tool_input),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if not result.stdout.strip():
        return "deny" if result.returncode == 2 else "allow"
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"]["permissionDecision"]


def run_hook_advisory(
    hook: Path,
    tool_input: dict,
    cwd: Path | None = None,
    home: Path | None = None,
    extra_env: dict | None = None,
) -> str:
    """Like `run_hook`, but for hooks documented as PostToolUse/informational,
    where a non-empty stdout payload may carry only an advisory field (e.g.
    `systemMessage`) with no `hookSpecificOutput.permissionDecision` at all —
    that absence means "no opinion" per the hook protocol, not a broken
    payload shape. Use `run_hook` instead for hooks that always emit
    `hookSpecificOutput`, so a shape regression there still surfaces as a
    hard failure rather than silently defaulting to "allow".

    home: when set, overrides $HOME in the subprocess environment so the
    hook writes into an isolated temp directory rather than real ~/.claude.
    extra_env: additional environment variables merged on top of the base env
    (applied after home override, so extra_env can also override HOME).
    """
    env = _build_subprocess_env(home, extra_env)
    result = subprocess.run(
        [str(hook)],
        input=json.dumps(tool_input),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if not result.stdout.strip():
        return "allow"
    payload = json.loads(result.stdout)
    return payload.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


def run_hook_reason(
    hook: Path,
    tool_input: dict,
    cwd: Path | None = None,
    home: Path | None = None,
    extra_env: dict | None = None,
) -> str | None:
    """Like `run_hook` but returns the deny `permissionDecisionReason` string
    (or `None` if the hook allowed silently). Used by tests that need to
    assert on the contents of the deny message, not just the decision.

    home: when set, overrides $HOME in the subprocess environment so the
    hook writes into an isolated temp directory rather than real ~/.claude.
    extra_env: additional environment variables merged on top of the base env
    (applied after home override, so extra_env can also override HOME).
    """
    env = _build_subprocess_env(home, extra_env)
    result = subprocess.run(
        [str(hook)],
        input=json.dumps(tool_input),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if not result.stdout.strip():
        return None
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"].get("permissionDecisionReason")


def run_hook_context(
    hook: Path,
    tool_input: dict,
    cwd: Path | None = None,
    home: Path | None = None,
    extra_env: dict | None = None,
) -> str | None:
    """Like `run_hook_reason` but returns the allow
    `hookSpecificOutput.additionalContext` string (or `None` if the hook
    allowed silently, with no additionalContext at all). Used by tests that
    need to assert on the contents of an informational allow-path note.

    home: when set, overrides $HOME in the subprocess environment so the
    hook writes into an isolated temp directory rather than real ~/.claude.
    extra_env: additional environment variables merged on top of the base env
    (applied after home override, so extra_env can also override HOME).
    """
    env = _build_subprocess_env(home, extra_env)
    result = subprocess.run(
        [str(hook)],
        input=json.dumps(tool_input),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if not result.stdout.strip():
        return None
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"].get("additionalContext")


def run_hook_payload(
    hook: Path,
    tool_input: dict,
    cwd: Path | None = None,
    home: Path | None = None,
    extra_env: dict | None = None,
) -> dict | None:
    """Like `run_hook` but returns the full `hookSpecificOutput` dict. Used
    by tests that need to assert on more than one field from a single
    invocation -- e.g. both `permissionDecision` and `additionalContext` --
    without a second, branch-diverging call.

    Empty stdout is ambiguous, so the return value branches on the exit code:

    - Exit 0: returns `None`. There is no `hookSpecificOutput` to return.
    - Exit 2: returns `{"permissionDecision": "deny"}`, a minimal
      stand-in for the PreToolUse block signal (e.g. a gate hook's
      jq-absent fallback) that omits the `hookEventName`/
      `permissionDecisionReason` fields a real `_lib_emit_deny` payload
      carries.

    home: when set, overrides $HOME in the subprocess environment so the
    hook writes into an isolated temp directory rather than real ~/.claude.
    extra_env: additional environment variables merged on top of the base env
    (applied after home override, so extra_env can also override HOME).
    """
    env = _build_subprocess_env(home, extra_env)
    result = subprocess.run(
        [str(hook)],
        input=json.dumps(tool_input),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if not result.stdout.strip():
        return {"permissionDecision": "deny"} if result.returncode == 2 else None
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"]


def run_hook_stop(
    hook: Path,
    tool_input: dict,
    cwd: Path | None = None,
    home: Path | None = None,
    extra_env: dict | None = None,
) -> dict | None:
    """Stop-specific runner. A Stop hook's block payload is a top-level
    {"decision": "block", "reason": ...} pair — distinct from run_hook's
    PreToolUse hookSpecificOutput.permissionDecision (KeyError on this
    shape) and run_hook_advisory's "no opinion" default (a Stop hook has no
    allow/deny axis at all, only "block this turn from ending" or silence,
    so mapping silence to "allow" would misrepresent what the hook did).

    Returns the parsed payload dict when the hook blocks, or None when it
    stays silent (empty stdout). Asserts the exact {"decision", "reason"}
    key pair on any non-empty payload — the harness routes on those literal
    keys, so a typo in the emitting hook must fail loudly here rather than
    silently no-op in production.

    home: when set, overrides $HOME in the subprocess environment so the
    hook writes into an isolated temp directory rather than real ~/.claude.
    extra_env: additional environment variables merged on top of the base env
    (applied after home override, so extra_env can also override HOME).
    """
    env = _build_subprocess_env(home, extra_env)
    result = subprocess.run(
        [str(hook)],
        input=json.dumps(tool_input),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if not result.stdout.strip():
        return None
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"decision", "reason"}, (
        f"Stop hook emitted unexpected keys {sorted(payload.keys())} — "
        'the harness routes on the exact {"decision", "reason"} pair'
    )
    assert payload["decision"] == "block", (
        f'Stop hook emitted decision={payload["decision"]!r}, expected "block" '
        "— the harness's Stop contract only recognizes that value"
    )
    return payload


class _NoUpdatedOutputSentinel:
    """Distinct from Python `None`: `run_hook_updated_output` returns this
    when the hook produced no stdout at all (its fail-open passthrough
    path), never for a hook that explicitly emits
    `updatedToolOutput: null`. `json.loads` collapses "no output" and "JSON
    null" to the same Python `None` if the helper returns `None` for both,
    which would let a hook that switches from silent passthrough to an
    explicit-null emission — turning "leave content untouched" into
    "replace the tool result with null", a materially different and more
    dangerous outcome depending on harness semantics — pass any test
    written against `is None`."""

    def __repr__(self) -> str:
        return "NO_UPDATED_OUTPUT"


NO_UPDATED_OUTPUT = _NoUpdatedOutputSentinel()


def run_hook_updated_output(
    hook: Path,
    tool_input: dict,
    cwd: Path | None = None,
    home: Path | None = None,
    extra_env: dict | None = None,
):
    """Like `run_hook`, but for a PostToolUse hook whose only success-path
    field is `hookSpecificOutput.updatedToolOutput` — no `permissionDecision`
    is ever emitted for this shape, so `run_hook`/`run_hook_advisory` (which
    key on `permissionDecision`) can't express "did it redact, and to what".

    Returns the parsed `updatedToolOutput` value (any JSON type — an object,
    a bare string, a number, `null` as Python `None`) on a non-empty stdout
    payload, or the `NO_UPDATED_OUTPUT` sentinel when the hook produced no
    output at all (its fail-open path: the harness reads silence as "no
    change", i.e. the original tool_response passed through untouched).
    Callers asserting fail-open passthrough must check `is NO_UPDATED_OUTPUT`,
    not `is None` — the latter also matches a hook that explicitly emitted
    `updatedToolOutput: null`, a different and non-equivalent outcome.

    home: when set, overrides $HOME in the subprocess environment so the
    hook reads its optional additions file from an isolated temp directory
    rather than real ~/.claude.
    extra_env: additional environment variables merged on top of the base env
    (applied after home override, so extra_env can also override HOME).
    """
    env = _build_subprocess_env(home, extra_env)
    result = subprocess.run(
        [str(hook)],
        input=json.dumps(tool_input),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if not result.stdout.strip():
        return NO_UPDATED_OUTPUT
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"]["updatedToolOutput"]


def run_hook_session_start(
    hook: Path,
    tool_input: dict,
    cwd: Path | None = None,
    home: Path | None = None,
    extra_env: dict | None = None,
) -> str | None:
    """SessionStart-specific runner. A title-setting SessionStart hook's
    payload is `{"hookSpecificOutput": {"hookEventName": "SessionStart",
    "sessionTitle": ...}}` — distinct from run_hook's PreToolUse
    permissionDecision shape and run_hook_stop's {"decision", "reason"} pair.

    Returns the emitted title string, or None when the hook stays silent
    (empty stdout). Asserts the exact {"hookEventName", "sessionTitle"} key
    set on any non-empty payload's hookSpecificOutput and that hookEventName
    equals "SessionStart" — a typo in the emitting hook's key name (e.g.
    "sessionTittle") must fail loudly here rather than silently no-op in
    production.

    home: when set, overrides $HOME in the subprocess environment so the
    hook writes into an isolated temp directory rather than real ~/.claude —
    required for kill-switch test cases, since without it a machine with the
    real sentinel present turns the whole test file vacuously green.
    extra_env: additional environment variables merged on top of the base env
    (applied after home override, so extra_env can also override HOME).
    """
    env = _build_subprocess_env(home, extra_env)
    result = subprocess.run(
        [str(hook)],
        input=json.dumps(tool_input),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if not result.stdout.strip():
        return None
    payload = json.loads(result.stdout)
    hook_specific_output = payload["hookSpecificOutput"]
    assert set(hook_specific_output.keys()) == {"hookEventName", "sessionTitle"}, (
        f"SessionStart hook emitted unexpected keys {sorted(hook_specific_output.keys())} "
        '— the harness routes on the exact {"hookEventName", "sessionTitle"} pair'
    )
    assert hook_specific_output["hookEventName"] == "SessionStart", (
        f'SessionStart hook emitted hookEventName={hook_specific_output["hookEventName"]!r}, '
        'expected "SessionStart"'
    )
    return hook_specific_output["sessionTitle"]


def posttooluse_input(file_path: str) -> dict:
    """Build a PostToolUse Write event payload for consume-migration-token tests.

    Covers the payload shape only — env setup is the caller's responsibility.
    Tests routing through run_hook / run_hook_reason must also pass
    extra_env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)}; without it the hook
    exits 0 via fail-open before touching any token, making token-state
    assertions vacuously true. The consume test suite uses its own _run_consume
    runner to enforce this contract explicitly.
    """
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "x"},
    }


def bash_input(
    command: str,
    session_id: str | None = None,
    agent_type: str | None = None,
    cwd: str | None = None,
) -> dict:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": command}}
    if session_id is not None:
        payload["session_id"] = session_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def edit_input(
    file_path: str,
    agent_type: str | None = None,
    cwd: str | None = None,
    old_string: str = "a",
    new_string: str = "b",
    replace_all: bool | None = None,
) -> dict:
    """`old_string`/`new_string`/`replace_all` default to the prior
    hardcoded placeholders ("a" -> "b", no replace_all field) — existing
    call sites that don't care about content keep the same payload. Pass
    them explicitly for a content-dependent test (e.g. a manifest-diffing
    hook)."""
    tool_input: dict = {"file_path": file_path, "old_string": old_string, "new_string": new_string}
    if replace_all is not None:
        tool_input["replace_all"] = replace_all
    payload: dict = {"tool_name": "Edit", "tool_input": tool_input}
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def write_input(
    file_path: str,
    agent_type: str | None = None,
    cwd: str | None = None,
    content: str = "x",
) -> dict:
    """`content` defaults to the prior hardcoded placeholder — existing call
    sites that don't care about content keep the same payload."""
    payload: dict = {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def multiedit_input(
    file_path: str, agent_type: str | None = None, cwd: str | None = None, edits: list | None = None
) -> dict:
    """`edits` defaults to the prior hardcoded empty list — existing call
    sites that don't care about content keep the same payload. Each item is
    a dict with `old_string`/`new_string` and an optional `replace_all`,
    matching the real MultiEdit tool_input shape."""
    payload: dict = {
        "tool_name": "MultiEdit",
        "tool_input": {"file_path": file_path, "edits": edits if edits is not None else []},
    }
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def stop_input(
    last_assistant_message: str,
    session_id: str | None = None,
    prompt_id: str | None = None,
    agent_type: str | None = None,
    permission_mode: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Build a Stop event payload matching the real harness shape for
    advance-past-commit-stall.sh's tests."""
    payload: dict = {
        "hook_event_name": "Stop",
        "last_assistant_message": last_assistant_message,
    }
    if session_id is not None:
        payload["session_id"] = session_id
    if prompt_id is not None:
        payload["prompt_id"] = prompt_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if permission_mode is not None:
        payload["permission_mode"] = permission_mode
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def exitplanmode_input(plan_file_path: str = "/home/user/.claude/plans/test-plan.md") -> dict:
    """Build an ExitPlanMode event payload matching the real harness shape.

    The ExitPlanMode tool_input has `plan` and `planFilePath` fields — no
    `file_path` field. The hook extracts `.tool_input.file_path // empty`,
    which yields an empty string for this payload, so the path-scope filter
    is skipped and the gate applies unconditionally.

    Field names (`plan`, `planFilePath` camelCase) verified empirically via
    live plan-mode session observation (spike run, prior session).
    """
    return {
        "tool_name": "ExitPlanMode",
        "tool_input": {
            "plan": "# Test plan\n\nTest plan content for spike/unit tests.",
            "planFilePath": plan_file_path,
        },
    }


def read_input(file_path: str, session_id: str | None = None) -> dict:
    payload: dict = {"tool_name": "Read", "tool_input": {"file_path": file_path}}
    if session_id is not None:
        payload["session_id"] = session_id
    return payload


def agent_input(
    session_id: str | None = None,
    subagent_type: str | None = None,
    prompt: str | None = None,
    tool_name: str = "Agent",
    cwd: str | None = None,
    description: str = "test",
) -> dict:
    """Build an Agent (or Task) dispatch payload.

    `tool_name` defaults to "Agent" (the harness's confirmed subagent-dispatch
    tool name), overridable to "Task" for hooks registered on the Agent|Task
    matcher union (require-architect-consult.sh, log-reviewer-round.sh).
    `subagent_type` is omitted from tool_input when None, matching a
    dispatch with no reviewer-persona target. `prompt` defaults to the
    literal string "test" when None, preserving every pre-existing caller's
    payload shape. `description` defaults to `"test"` when not provided,
    preserving existing callers' payload shape. It is threaded through as a
    parameter so a `deny-no-op-dispatch.sh` fixture can construct a payload
    with a distinct description without a separate builder.
    """
    tool_input: dict = {"description": description, "prompt": prompt if prompt is not None else "test"}
    if subagent_type is not None:
        tool_input["subagent_type"] = subagent_type
    payload: dict = {"tool_name": tool_name, "tool_input": tool_input}
    if session_id is not None:
        payload["session_id"] = session_id
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def skill_input(
    skill_name: str, session_id: str | None = None, agent_type: str | None = None
) -> dict:
    """Build a Skill tool_use payload. `skill_name` lands under
    `tool_input.skill` -- the field name pinned by capturing a real `Skill`
    tool_use record from a local transcript (not documented in the harness's
    hooks/tools-reference pages)."""
    payload: dict = {"tool_name": "Skill", "tool_input": {"skill": skill_name}}
    if session_id is not None:
        payload["session_id"] = session_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return payload


# activate-handoff-bypass.sh wraps its marker.sh call in a 2s `_lib_capped_for`
# cap that can be exceeded under parallel-test-worker contention -- see that
# hook's own header comment. Retrying is safe because `marker.sh activate` is
# idempotent (marker.sh:387-392 -- it just overwrites the same PID file).
ACTIVATE_MARKER_RETRY_ATTEMPTS = 10


def run_hook_until_marker_exists(
    hook: Path,
    tool_input: dict,
    marker: Path,
    attempts: int = ACTIVATE_MARKER_RETRY_ATTEMPTS,
    home: Path | None = None,
    extra_env: dict | None = None,
) -> None:
    """Retry `hook` against `tool_input` until `marker` exists, or fail."""
    for _ in range(attempts):
        run_hook(hook, tool_input, home=home, extra_env=extra_env)
        if marker.exists():
            return
        time.sleep(0.5)
    assert marker.exists(), (
        f"marker never landed at {marker} after {attempts} attempts -- "
        "not just a single cap-timeout miss"
    )


# -- Hostile session_id ------------------------------------------------------
#
# Every hook that builds a filesystem path from the payload's `.session_id`
# guards it with `_lib_valid_session_id_component` (_lib.sh). The tests that
# pin that guard all share one setup: put a file where a traversing id would
# resolve, run the hook with that id, and check the file survived. The three
# names below are that shared setup; the per-hook sink assertions are not
# shared, because each hook reaches the traversed path by a different sink
# (`rm -f`, `touch`, a truncating write, a `--checkpoint` argument) and the
# assertion that discriminates guard-present from guard-absent differs with it.

TRAVERSAL_SESSION_ID = "../canary"
"""A session_id that escapes one directory level when concatenated into
`$HOME/.claude/<marker-dir>/$SESSION_ID`, resolving to `$HOME/.claude/canary`.
Single level is deliberate: every marker directory this suite exercises sits
directly under `~/.claude`, so one `..` lands in a directory that exists and
the traversal is live rather than inert on a missing path component."""

CANARY_CONTENT = "untouched\n"


def plant_traversal_canary(home: Path, name: str = "canary") -> Path:
    """Create the file that `TRAVERSAL_SESSION_ID` resolves to, and return it.

    `name` covers hooks that build more than one path from the session id
    (e.g. a marker plus a `-drift` sidecar), each needing its own canary.
    """
    canary = home / ".claude" / name
    canary.write_text(CANARY_CONTENT)
    return canary


def assert_gate_handles_traversal_session_id(
    hook: Path,
    make_input,
    home: Path,
    expected_decision: str,
    cwd: Path | None = None,
) -> None:
    """Assert a PreToolUse gate's decision for a traversing session_id, and
    that it touched nothing outside its marker directory.

    `make_input` is a callable taking a session id and returning the hook
    payload, rather than a prebuilt payload: the helper supplies
    `TRAVERSAL_SESSION_ID` itself, so a caller cannot pass a payload carrying
    some other id and leave the test asserting nothing.

    `expected_decision` is per-hook and not a constant. Bypass-shaped gates
    (the marker grants an exception to a standing deny) must withhold the
    exception and deny; activation-shaped gates (the marker turns enforcement
    on) must leave it off and allow. Callers state which they are.
    """
    payload = make_input(TRAVERSAL_SESSION_ID)
    # A make_input that drops the id it was handed would leave this test
    # asserting a hook's disposition for an ordinary payload — passing, and
    # pinning nothing about traversal. Checking the built payload closes that,
    # since supplying the id is the only reason the callable form exists.
    assert TRAVERSAL_SESSION_ID in json.dumps(payload), (
        f"make_input did not thread the session id into the payload: {payload!r}"
    )
    canary = plant_traversal_canary(home)
    assert run_hook(hook, payload, cwd=cwd) == expected_decision
    assert canary.read_text() == CANARY_CONTENT, (
        "a traversal session_id must not touch a file outside the marker dir"
    )


def git_toplevel(repo: Path) -> str:
    """Return what `git rev-parse --show-toplevel` sees — this is what the
    hook hashes, and it may differ from `str(repo)` when /tmp is a symlink."""
    return subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


DEFAULT_TEST_SESSION_ID = "test-session-default"


def marker_path(
    home: Path,
    repo: Path,
    session_id: str = DEFAULT_TEST_SESSION_ID,
    config_dir: Path | None = None,
) -> Path:
    repo_hash = hashlib.sha256(git_toplevel(repo).encode()).hexdigest()
    config_dir = config_dir if config_dir is not None else home / ".claude"
    return config_dir / "code-review-markers" / f"{repo_hash}.{session_id}"


def staged_diff_hash(repo: Path) -> str:
    """The plain HEAD-relative preimage: `git diff --cached` with no base
    override. This is today's production recipe, and stays exactly this
    outside any in-progress git state (merge/rebase/cherry-pick/revert),
    where _lib_gate_diff_base resolves no base and the real hooks issue
    this identical command. See staged_diff_hash_at_base() below for the
    base-relative oracle used inside a trusted in-progress state -- this
    function is deliberately not extended to take a base, since the
    marker-invalidation tests need exactly this old recipe to build an
    old-preimage marker."""
    diff = subprocess.run(
        ["git", "diff", "--cached"], cwd=repo, capture_output=True, check=True
    ).stdout
    return hashlib.sha256(diff).hexdigest()


def staged_diff_hash_at_base(repo: Path, base: str) -> str:
    """Independent oracle for a base-relative marker preimage: computes
    `git diff --cached <base>` directly in Python rather than by calling
    the production `_lib_staged_diff_hash`/`_lib_gate_diff_base` shell
    functions under test -- a test seeding a marker via the function it is
    testing would only prove the function agrees with itself, not that its
    output is correct (see write_plan_review_marker's docstring below for
    the same caution applied to a different marker kind)."""
    diff = subprocess.run(
        ["git", "diff", "--cached", base], cwd=repo, capture_output=True, check=True
    ).stdout
    return hashlib.sha256(diff).hexdigest()


def _run_git(repo: Path, *args: str) -> str:
    """Run a git subcommand in `repo`, returning stdout. Raises on failure --
    for the failure-is-expected calls in the in-progress-state builders
    below (a merge/rebase/cherry-pick/revert whose whole point is to
    conflict), use subprocess.run directly and assert on the returncode."""
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _current_branch(repo: Path) -> str:
    return _run_git(repo, "symbolic-ref", "--short", "HEAD").strip()


def _seed_tracked_file(repo: Path, file_name: str, content: str = "base\n") -> Path:
    """Ensure `file_name` exists, tracked, and committed in `repo`, so a
    conflict-building fixture has a common baseline both diverging sides
    can edit differently -- an add/add conflict (both sides create the file
    independently) exercises different git machinery than the
    content-conflict shape these fixtures need. Idempotent: does nothing if
    the file is already tracked."""
    target = repo / file_name
    if not target.exists():
        target.write_text(content)
        _run_git(repo, "add", file_name)
        _run_git(repo, "commit", "-qm", f"seed {file_name}")
    return target


def bare_remote_with_default_branch(
    tmp_path: Path,
    branch: str = "main",
    file_name: str = "f",
    file_content: str = "a\n",
) -> tuple[Path, Path]:
    """Build a bare 'origin' repo and a clone checked out on `branch`, with
    origin/HEAD set and one shared commit -- the bare-remote-plus-clone
    shape test_check_branch_divergence.py's bare_remote/feature_clone
    pytest fixtures already establish, generalized to a plain function so
    every test file needing a pushable remote (the trusted/untrusted-anchor
    tests here, and require-ready-for-review.sh's push-anchor test) can
    call it directly rather than duplicating the construction. Returns
    (bare_remote, clone)."""
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", branch, str(bare)], check=True, capture_output=True
    )
    seed = tmp_path / "_bare_remote_seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=seed, check=True)
    (seed / file_name).write_text(file_content)
    subprocess.run(["git", "add", file_name], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=seed, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", branch], cwd=seed, check=True)

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(clone)], check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=clone, check=True)
    subprocess.run(["git", "remote", "set-head", "origin", branch], cwd=clone, check=True)
    return bare, clone


def push_conflicting_edit_to_origin(
    tmp_path: Path, bare: Path, file_name: str, content: str, branch: str = "main"
) -> None:
    """Push a new commit editing `file_name` to `bare`'s default branch from
    a throwaway clone, independent of any other clone's own worktree -- the
    "someone else pushed while I was working" shape a real sync merge
    needs, and the shape test_check_branch_divergence.py's
    repo_behind_conflict fixture already establishes. Does not touch any
    other clone; callers run `git fetch origin` there afterward to see the
    new origin/<branch> tip."""
    push_clone = tmp_path / f"_push_{branch}_{abs(hash(content))}"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(push_clone)], check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=push_clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=push_clone, check=True)
    (push_clone / file_name).write_text(content)
    subprocess.run(["git", "add", file_name], cwd=push_clone, check=True)
    subprocess.run(
        ["git", "commit", "-qm", f"origin edits {file_name}"], cwd=push_clone, check=True
    )
    subprocess.run(["git", "push", "-q", "origin", branch], cwd=push_clone, check=True)


def build_conflicted_merge(repo: Path, *, file_name: str = "f") -> str:
    """Build a real conflicted two-way merge inside `repo`: branch "theirs"
    off the checked-out branch, edit `file_name` differently on each side,
    then `git merge theirs` on the original branch. Leaves MERGE_HEAD and
    unresolved conflict markers staged. `repo` must already have a
    configured user.email/user.name. Returns the merged-in branch's tip
    oid -- MERGE_HEAD's expected content."""
    base_branch = _current_branch(repo)
    target = _seed_tracked_file(repo, file_name)
    _run_git(repo, "checkout", "-qb", "theirs")
    target.write_text("theirs-edit\n")
    _run_git(repo, "add", file_name)
    _run_git(repo, "commit", "-qm", f"theirs edits {file_name}")
    theirs_oid = _run_git(repo, "rev-parse", "HEAD").strip()
    _run_git(repo, "checkout", "-q", base_branch)
    target.write_text("ours-edit\n")
    _run_git(repo, "add", file_name)
    _run_git(repo, "commit", "-qm", f"ours edits {file_name}")
    result = subprocess.run(
        ["git", "merge", "-q", "theirs"], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0, (
        f"expected merge conflict, got: {result.stdout}{result.stderr}"
    )
    assert (repo / ".git" / "MERGE_HEAD").exists(), "merge did not leave MERGE_HEAD"
    return theirs_oid


def build_conflicted_cherry_pick(repo: Path, *, file_name: str = "f") -> str:
    """Build a real conflicted cherry-pick inside `repo`: branch "source"
    off the checked-out branch, commit a conflicting edit to `file_name` on
    each side, then `git cherry-pick` the source commit back onto the
    original branch. Leaves CHERRY_PICK_HEAD and unresolved conflict
    markers staged. Returns the cherry-picked commit's oid -- CHERRY_PICK_HEAD's
    expected content."""
    base_branch = _current_branch(repo)
    target = _seed_tracked_file(repo, file_name)
    _run_git(repo, "checkout", "-qb", "source")
    target.write_text("source-edit\n")
    _run_git(repo, "add", file_name)
    _run_git(repo, "commit", "-qm", f"source edits {file_name}")
    source_oid = _run_git(repo, "rev-parse", "HEAD").strip()
    _run_git(repo, "checkout", "-q", base_branch)
    target.write_text("base-edit\n")
    _run_git(repo, "add", file_name)
    _run_git(repo, "commit", "-qm", f"base edits {file_name}")
    result = subprocess.run(
        ["git", "cherry-pick", source_oid], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0, (
        f"expected cherry-pick conflict, got: {result.stdout}{result.stderr}"
    )
    assert (repo / ".git" / "CHERRY_PICK_HEAD").exists(), "cherry-pick did not leave CHERRY_PICK_HEAD"
    return source_oid


def build_conflicted_revert(repo: Path, *, file_name: str = "f") -> str:
    """Build a real conflicted revert inside `repo`: commit A introduces an
    edit to `file_name`, commit B further edits the same region, then
    `git revert A`. Reverting the tip essentially never conflicts, so this
    three-commit shape is required to exercise the conflicting case. Leaves
    REVERT_HEAD and unresolved conflict markers staged. Returns commit A's
    oid -- REVERT_HEAD's expected content."""
    target = _seed_tracked_file(repo, file_name)
    target.write_text("A-edit\n")
    _run_git(repo, "add", file_name)
    _run_git(repo, "commit", "-qm", "commit A")
    commit_a = _run_git(repo, "rev-parse", "HEAD").strip()
    target.write_text("B-edit\n")
    _run_git(repo, "add", file_name)
    _run_git(repo, "commit", "-qm", "commit B")
    result = subprocess.run(
        ["git", "revert", "--no-edit", commit_a], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0, (
        f"expected revert conflict, got: {result.stdout}{result.stderr}"
    )
    assert (repo / ".git" / "REVERT_HEAD").exists(), "revert did not leave REVERT_HEAD"
    return commit_a


def build_conflicted_rebase(
    repo: Path, *, file_name: str = "f", interactive: bool = False
) -> str:
    """Build a real conflicted rebase inside `repo`, non-interactive by
    default: branch "upstream" off the checked-out branch, edit
    `file_name` differently on each side, then rebase the original branch
    onto "upstream". Returns control at the pre-resolution checkpoint --
    the index carries genuine stage 1/2/3 entries for `file_name` and the
    conflict is not yet resolved -- with the replayed commit's oid
    (REBASE_HEAD's expected content). Call resolve_conflicted_rebase() to
    advance to the post-resolution, staged checkpoint.

    `interactive=True` runs `git -c sequence.editor=true rebase -i
    upstream` instead: an interactive rebase whose todo list is accepted
    unmodified. Unlike the plain form, git implements each interactive
    "pick" step via the same code path as cherry-pick, so this leaves a
    CHERRY_PICK_HEAD alongside rebase-merge/ while paused -- the fixture
    the state-detection precedence order (rebase before cherry-pick) is
    verified against."""
    base_branch = _current_branch(repo)
    target = _seed_tracked_file(repo, file_name)
    _run_git(repo, "checkout", "-qb", "upstream")
    target.write_text("upstream-edit\n")
    _run_git(repo, "add", file_name)
    _run_git(repo, "commit", "-qm", f"upstream edits {file_name}")
    _run_git(repo, "checkout", "-q", base_branch)
    target.write_text("feature-edit\n")
    _run_git(repo, "add", file_name)
    _run_git(repo, "commit", "-qm", f"feature edits {file_name}")
    feature_tip = _run_git(repo, "rev-parse", "HEAD").strip()

    cmd = (
        ["git", "-c", "sequence.editor=true", "rebase", "-i", "upstream"]
        if interactive
        else ["git", "rebase", "upstream"]
    )
    result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    assert result.returncode != 0, (
        f"expected rebase conflict, got: {result.stdout}{result.stderr}"
    )
    gitdir = repo / ".git"
    assert (gitdir / "rebase-merge").is_dir() or (gitdir / "rebase-apply").is_dir(), (
        "rebase did not leave rebase-merge/ or rebase-apply/"
    )
    # Whether the installed git writes REBASE_HEAD is asserted, not assumed
    # -- git's older apply-based backend (rebase-apply/) predates
    # REBASE_HEAD, so this is only guaranteed on the merge-based backend
    # (the default since git 2.26) that both the plain and interactive
    # forms exercised here use.
    assert (gitdir / "REBASE_HEAD").exists(), "rebase did not leave REBASE_HEAD"
    return feature_tip


def resolve_conflicted_rebase(
    repo: Path, *, file_name: str = "f", resolution: str = "resolved\n"
) -> None:
    """Advance a build_conflicted_rebase() fixture to the post-resolution,
    staged checkpoint: write `resolution` to `file_name` and `git add` it,
    leaving the rebase paused with a clean, staged resolution ready for
    `git rebase --continue`."""
    (repo / file_name).write_text(resolution)
    _run_git(repo, "add", file_name)


def build_octopus_merge_conflict(repo: Path, *, file_name: str = "f") -> None:
    """Forge a genuine multi-line MERGE_HEAD naming two real, divergent
    commits. git's own octopus merge strategy aborts outright on any
    conflicting step rather than leaving a resolvable state to fix by
    hand -- confirmed empirically: `git merge b1 b2` against branches
    diverged from a shared base and conflicting on the same file exits
    nonzero with no MERGE_HEAD left at all, unlike the two-line MERGE_HEAD
    plus ordinary conflict markers a two-parent merge leaves. Producing
    this on-disk shape -- which the OID-validation code must still handle
    defensively -- means writing MERGE_HEAD directly, naming two real
    commits so the ancestry check this fixture exercises runs against
    genuine objects rather than fabricated ones."""
    base_branch = _current_branch(repo)
    target = _seed_tracked_file(repo, file_name)
    base_sha = _run_git(repo, "rev-parse", "HEAD").strip()
    oids = []
    for branch, content in (("b1", "b1-edit\n"), ("b2", "b2-edit\n")):
        _run_git(repo, "checkout", "-qb", branch, base_sha)
        target.write_text(content)
        _run_git(repo, "add", file_name)
        _run_git(repo, "commit", "-qm", f"{branch} edits {file_name}")
        oids.append(_run_git(repo, "rev-parse", "HEAD").strip())
    _run_git(repo, "checkout", "-q", base_branch)
    (repo / ".git" / "MERGE_HEAD").write_text("\n".join(oids) + "\n")


def build_rebase_merges_replay_conflict(repo: Path, *, file_name: str = "f") -> None:
    """Build a `git rebase --rebase-merges upstream` replay that conflicts
    while reconstructing a merge commit, so REBASE_HEAD names that merge
    commit. This is the topology where REBASE_HEAD^ would silently resolve
    to parent 1 rather than erroring.

    `upstream` edits `file_name`. `side` and `feature` both edit a second
    file, `second_file_name`, differently, so each replays cleanly onto
    `upstream` individually -- `upstream` never touches that file. The
    conflict surfaces only when --rebase-merges reconstructs the merge step
    combining `side` and `feature`: this fixture replays, by hand, the same
    conflict `feature`'s own merge of `side` hit originally. A conflict
    placed directly in `file_name` instead would surface during an earlier
    individual pick step and never reach the merge reconstruction at all --
    confirmed empirically."""
    second_file_name = f"{file_name}2"
    target = _seed_tracked_file(repo, file_name)
    second_target = repo / second_file_name
    second_target.write_text("base\n")
    _run_git(repo, "add", second_file_name)
    _run_git(repo, "commit", "-qm", f"seed {second_file_name}")
    base_sha = _run_git(repo, "rev-parse", "HEAD").strip()

    _run_git(repo, "checkout", "-qb", "upstream", base_sha)
    target.write_text("upstream-edit\n")
    _run_git(repo, "add", file_name)
    _run_git(repo, "commit", "-qm", f"upstream edits {file_name}")

    _run_git(repo, "checkout", "-qb", "side", base_sha)
    second_target.write_text("side-edit\n")
    _run_git(repo, "add", second_file_name)
    _run_git(repo, "commit", "-qm", f"side edits {second_file_name}")

    _run_git(repo, "checkout", "-qb", "feature", base_sha)
    second_target.write_text("feature-edit\n")
    _run_git(repo, "add", second_file_name)
    _run_git(repo, "commit", "-qm", f"feature edits {second_file_name}")
    merge_result = subprocess.run(
        ["git", "merge", "--no-ff", "-q", "side"], cwd=repo, capture_output=True, text=True
    )
    assert merge_result.returncode != 0, (
        f"expected feature's own merge of side to conflict, got: "
        f"{merge_result.stdout}{merge_result.stderr}"
    )
    second_target.write_text("resolved\n")
    _run_git(repo, "add", second_file_name)
    subprocess.run(
        ["git", "commit", "--no-edit", "-q"],
        cwd=repo, check=True, capture_output=True,
        env={**os.environ, "GIT_EDITOR": "true"},
    )
    merge_oid = _run_git(repo, "rev-parse", "HEAD").strip()

    result = subprocess.run(
        ["git", "rebase", "--rebase-merges", "upstream"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0, (
        f"expected rebase --rebase-merges conflict, got: {result.stdout}{result.stderr}"
    )
    assert (repo / ".git" / "rebase-merge").is_dir(), "rebase did not leave rebase-merge/"
    rebase_head = _run_git(repo, "rev-parse", "REBASE_HEAD").strip()
    assert rebase_head == merge_oid, (
        "expected REBASE_HEAD to name the original merge commit, not an "
        "individually-replayed side commit"
    )
    parents = _run_git(repo, "rev-list", "--parents", "-n", "1", rebase_head).split()
    assert len(parents) >= 3, "expected REBASE_HEAD to be a merge commit with 2+ parents"


def write_marker(
    home: Path,
    repo: Path,
    diff_hash: str,
    session_id: str = DEFAULT_TEST_SESSION_ID,
    config_dir: Path | None = None,
) -> Path:
    marker = marker_path(home, repo, session_id, config_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(diff_hash + "\n")
    return marker


def head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def stage_settings(repo: Path, settings_file: Path, content: str) -> None:
    """Write `content` to `settings_file` and stage it."""
    settings_file.write_text(content)
    subprocess.run(
        ["git", "add", "claude/.claude/settings.json"],
        cwd=repo, check=True,
    )


def plan_review_marker_path(
    home: Path, repo: Path, session_id: str, config_dir: Path | None = None
) -> Path:
    repo_hash = hashlib.sha256(git_toplevel(repo).encode()).hexdigest()
    config_dir = config_dir if config_dir is not None else home / ".claude"
    return config_dir / "plan-review-markers" / f"{repo_hash}.{session_id}"


def write_plan_review_marker(
    home: Path, repo: Path, session_id: str, config_dir: Path | None = None, base: str = ""
) -> Path:
    """Write a plan-review completion marker whose content is the real
    active-plan hash for `repo`, computed by shelling out to the production
    `_lib_active_plan_hash` (_lib.sh) rather than reimplementing the recipe
    in Python, which would diverge silently on any
    newline/delimiter/normalization detail. `base` defaults to "" (no
    trusted in-progress state) -- pass the merge-tree base explicitly for a
    marker meant to validate mid-merge/rebase/cherry-pick/revert.

    Note the tradeoff, and do not mistake this for the technique
    `write_marker`/`write_skill_review_marker` use: those recompute the hash
    independently in Python from a real `git diff`, so they can catch drift
    in the shell-side recipe. This one calls the very function under test, so
    a test that seeds a marker here and asserts the hook allows is checking
    that the function agrees with itself across two invocations -- not that
    its output is correct. Independent correctness is covered by the
    relational unit tests in `hooks/tests/test_marker_lib.py`, which do not
    route through this helper."""
    marker = plan_review_marker_path(home, repo, session_id, config_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    lib_sh = HOOKS_DIR / "_lib.sh"
    active_plan_hash = subprocess.run(
        ["bash", "-c", f'. "{lib_sh}"; _lib_active_plan_hash "$1" "$2"',
         "write_plan_review_marker", str(repo), base],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    marker.write_text(active_plan_hash)
    return marker


def skill_review_marker_path(
    home: Path,
    repo: Path,
    session_id: str = DEFAULT_TEST_SESSION_ID,
    config_dir: Path | None = None,
) -> Path:
    repo_hash = subprocess.run(
        ["sha256sum"],
        input=git_toplevel(repo).encode(),
        capture_output=True,
    ).stdout.decode().split()[0]
    config_dir = config_dir if config_dir is not None else home / ".claude"
    return config_dir / "skill-review-markers" / f"{repo_hash}.{session_id}"


def write_skill_review_marker(
    home: Path,
    repo: Path,
    session_id: str = DEFAULT_TEST_SESSION_ID,
    config_dir: Path | None = None,
) -> None:
    diff = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--",
            "claude-skills/skills/**/SKILL.md",
            "plugins/*/skills/**/SKILL.md",
            "skills/**/SKILL.md",
            "claude-skills/skills/plan-review/ROUTING.md",
        ],
        capture_output=True,
        check=True,
        cwd=repo,
    ).stdout
    diff_hash = hashlib.sha256(diff).hexdigest()
    marker = skill_review_marker_path(home, repo, session_id, config_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(diff_hash + "\n")


def plan_review_active_marker_path(home: Path, session_id: str) -> Path:
    return home / ".claude" / ".plan-review-active.d" / session_id


def write_plan_review_active_marker(home: Path, session_id: str) -> Path:
    """Create a plan-review active marker with empty content.

    This produces a dead-PID marker intentionally for hooks that check marker
    existence only (e.g., require-routing-read.sh, log-routing-read.sh).
    require-plan-review.sh reads the file and validates the PID with kill -0,
    so an empty-content marker is immediately evicted by that hook. For tests
    that need a live-marker bypass in require-plan-review.sh, write the PID
    directly: `(marker_dir / sid).write_text(str(os.getpid()))`.
    """
    marker = plan_review_active_marker_path(home, session_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return marker


def plan_review_routing_read_marker_path(home: Path, session_id: str) -> Path:
    return home / ".claude" / ".plan-review-routing-read.d" / session_id


def write_plan_review_routing_read_marker(home: Path, session_id: str) -> Path:
    marker = plan_review_routing_read_marker_path(home, session_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return marker


def plan_review_pending_read_marker_path(home: Path, session_id: str) -> Path:
    return home / ".claude" / ".plan-review-pending-read.d" / session_id


def reviewer_round_state_key(repo: Path) -> str:
    """Shell out to the real _lib_reviewer_round_state_key against `repo`,
    so a test's seeded state file lands at the exact path
    require-architect-consult.sh/log-reviewer-round.sh will look under.

    Uses git_toplevel(repo), not str(repo): the hooks resolve REPO_ROOT via
    `git -C "$CWD" rev-parse --show-toplevel` before hashing it, which
    normalizes a symlinked tmp prefix (e.g. macOS /tmp -> /private/tmp) that
    the raw tmp_path string would not — passing the unnormalized path here
    would key a test's seeded file under a different repo-hash than the
    hook computes at runtime.

    Returns "" (not raising) when the repo has no branch to key on (e.g.
    detached HEAD), mirroring the function's own fail-open contract.
    """
    result = subprocess.run(
        ["bash", "-c", f'. "{HOOKS_DIR}/_lib.sh"; _lib_reviewer_round_state_key "$1"',
         "_", git_toplevel(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def reviewer_round_state_value(repo: Path) -> str:
    """Shell out to the real _lib_reviewer_round_state_value against `repo`
    — see reviewer_round_state_key's docstring for the git_toplevel
    normalization rationale, which applies identically here. Returns ""
    (not raising) when HEAD is unresolvable (no commits yet), or when the
    staged diff is unanswerable (_lib_staged_diff_state reports "unknown")."""
    result = subprocess.run(
        ["bash", "-c", f'. "{HOOKS_DIR}/_lib.sh"; _lib_reviewer_round_state_value "$1"',
         "_", git_toplevel(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def reviewer_round_state_path(config_dir: Path, repo: Path) -> Path:
    return config_dir / ".reviewer-round-state.d" / reviewer_round_state_key(repo)


def architect_consult_latch_path(config_dir: Path, repo: Path) -> Path:
    return config_dir / ".architect-consult-latch.d" / reviewer_round_state_key(repo)


def write_reviewer_round_state(config_dir: Path, repo: Path, values: list[str]) -> Path:
    """Seed the round-state file directly with `values` (each already a
    "<head-sha> <staged-diff-sha256>" line), bypassing log-reviewer-round.sh
    entirely — for tests that need a precondition (e.g. "at cap") set up
    without exercising the recorder itself."""
    state_file = reviewer_round_state_path(config_dir, repo)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("".join(f"{v}\n" for v in values))
    return state_file


def _symlink_if_absent(link: Path, target: Path) -> Path:
    """Create link -> target if link doesn't already exist. Idempotent."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        link.symlink_to(target)
    return link


def install_resume_context_script(isolated_home: Path) -> Path:
    """Symlink the real resume-context.sh into an isolated $HOME/.claude/scripts/.

    So hook/script tests that shell out to resume-context.sh (directly, or via
    consume-durable-continuity-file-on-read.sh) exercise the real script
    rather than a copy that can drift from it.
    """
    return _symlink_if_absent(
        isolated_home / ".claude" / "scripts" / "resume-context.sh",
        SCRIPTS_DIR / "resume-context.sh",
    )


def install_marker_script(isolated_home: Path) -> Path:
    """Symlink the real marker.sh, and the _lib.sh it sources, into an
    isolated $HOME/.claude/ -- so a hook or skill recipe invoking marker.sh
    via `$CONFIG_DIR/scripts/marker.sh` resolves the real script rather than
    a missing one. Idempotent, so a caller under the `isolated_home` fixture
    (which already symlinks hooks/_lib.sh itself) can call this unconditionally.
    """
    _symlink_if_absent(isolated_home / ".claude" / "hooks" / "_lib.sh", HOOKS_DIR / "_lib.sh")
    return _symlink_if_absent(
        isolated_home / ".claude" / "scripts" / "marker.sh", SCRIPTS_DIR / "marker.sh"
    )


def run_skill_command(command: str, cwd: Path, isolated_home: Path) -> None:
    """Run a SKILL.md-extracted bash command in a sandboxed $HOME."""
    install_marker_script(isolated_home)
    _symlink_if_absent(
        isolated_home / ".claude" / "scripts" / "ensure-account-dir.sh",
        SCRIPTS_DIR / "ensure-account-dir.sh",
    )
    subprocess.run(
        ["bash", "-c", command],
        cwd=cwd,
        env=_build_subprocess_env(isolated_home, None),
        check=True,
    )


def extract_ci_detect_step_run_block() -> str:
    """Pull the `run:` script for tests.yml's step with `id: detect` via
    pyyaml, locating the step by id rather than regexing the YAML."""
    workflow = yaml.safe_load(_CI_DETECT_STEP_WORKFLOW.read_text())
    steps = workflow["jobs"]["tests"]["steps"]
    detect_steps = [step for step in steps if step.get("id") == "detect"]
    assert len(detect_steps) == 1, (
        "Expected exactly one step with id: detect in tests.yml — did "
        "the step move or get renamed?"
    )
    return detect_steps[0]["run"]


def substitute_ci_detect_step_expressions(script: str, base_sha: str, head_sha: str) -> str:
    """Replace GitHub Actions `${{ }}` expressions with literal values.

    This textual substitution is the one unavoidable fidelity gap in this
    test: the real Actions runner evaluates these expressions with its own
    expression engine before handing bash the resulting script, and that
    evaluator isn't available here, so plain string substitution stands in
    for it. Forcing github.event_name to a non-"pull_request" value routes
    through the push-event branch, which is the one that reads
    github.event.before / github.sha into BASE/HEAD.
    """
    substitutions = {
        "${{ github.event_name }}": "push",
        "${{ github.event.pull_request.base.sha }}": "unused-in-push-branch",
        "${{ github.event.pull_request.head.sha }}": "unused-in-push-branch",
        "${{ github.event.before }}": base_sha,
        "${{ github.sha }}": head_sha,
    }
    for placeholder, value in substitutions.items():
        script = script.replace(placeholder, value)
    return script


def init_ci_detect_step_test_repo(
    tmp_path: Path, second_commit_files: dict[str, str]
) -> tuple[Path, str, str]:
    """Build a throwaway two-commit git repo; return (repo, base_sha, head_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    for rel_path, content in second_commit_files.items():
        path = repo / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=repo, check=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    return repo, base_sha, head_sha


def run_ci_detect_step(repo: Path, base_sha: str, head_sha: str) -> dict[str, str]:
    """Run the substituted detect script under bash; parse GITHUB_OUTPUT."""
    script = substitute_ci_detect_step_expressions(
        extract_ci_detect_step_run_block(), base_sha, head_sha
    )
    github_output = repo / "github_output.txt"
    github_output.write_text("")
    env = {**os.environ, "GITHUB_OUTPUT": str(github_output)}
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"detect step exited nonzero: {result.stdout}\n{result.stderr}"
    )
    outputs: dict[str, str] = {}
    for line in github_output.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return outputs


def build_path_without(binary: str, farm_dir: Path) -> str:
    """Build a PATH string mirroring the real PATH via a symlink farm, with
    `binary` omitted, inside the caller-supplied (already-created) `farm_dir`.

    A full mirror (not a hand-picked minimal tool subset) is deliberate:
    under-symlinking is a silent false pass here — a hook denying because
    some OTHER required tool is missing looks identical to the hook denying
    correctly for the binary this test actually targets. First real PATH
    directory wins on a duplicate basename, mirroring normal PATH shadowing
    order; unreadable directories are skipped rather than raising.

    Callers own `farm_dir`'s lifetime and any caching strategy (e.g.
    session-scoped memoization) — this function only builds the farm once
    per call.
    """
    seen: set[str] = set()
    for real_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not real_dir:
            continue
        try:
            entries = os.listdir(real_dir)
        except OSError:
            continue
        for name in entries:
            if name == binary or name in seen:
                continue
            src = Path(real_dir) / name
            try:
                if not os.access(src, os.X_OK):
                    continue
            except OSError:
                continue
            seen.add(name)
            try:
                (farm_dir / name).symlink_to(src)
            except OSError:
                continue
    path_str = str(farm_dir)
    assert shutil.which(binary, path=path_str) is None, (
        f"{binary}: still resolvable on the built PATH {path_str!r} — farm construction bug"
    )
    return path_str


# (first_line, expect_consult, id) rows behind plan-architect consult
# classification, reused by test_log_reviewer_round.py's bash-latch test and
# test_transcript_analysis.py's Python-classifier test. Proves the two
# runtimes agree on classification behaviorally, not that their source text
# matches byte-for-byte -- a byte-equality assertion across the literal sites
# would still pass with the Python `!=` comparison inverted to `==`. No
# pytest import here (see module docstring), so each test file wraps these
# rows in pytest.param(...) at its own @pytest.mark.parametrize call site.
CONSULT_CLASSIFICATION_TABLE: list[tuple[str, bool, str]] = [
    ("MODE=consult", True, "mode_consult"),
    ("MODE=plan-sections", False, "mode_plan_sections"),
    ("", True, "empty_first_line"),
    ("MODE=plna-sections", True, "typo_mode_value"),
    ("Just look at the plan and tell me if it's sound.", True, "no_mode_line"),
    ("MODE=plan-sections ", True, "mode_plan_sections_trailing_space"),
    ("Some preamble.\nMODE=plan-sections", True, "mode_plan_sections_not_first_line"),
    ("MODE=plan-sections\r\n## Section A", True, "mode_plan_sections_crlf"),
]
