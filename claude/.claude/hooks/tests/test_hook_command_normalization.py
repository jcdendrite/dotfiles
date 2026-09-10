"""Structural regression guard for GH-783's recurrence path.

Every gate hook that word-walks a Bash command fragment or feeds command
text into a content scan must do so over quote-stripped text — otherwise a
gated word written in quotes (`gh "pr" "create"`, `git "commit"`) walks
past the gate undetected, the exact bypass GH-783 fixed. Two structural
scans, mirroring test_agent_roster.py's EXPECTED_EFFORT map-based
precedent (a module-level map, iterated by a positive-path test and
proven load-bearing by a negative-path test). Both extract the actual
variable token at each call/append site rather than checking file
membership alone, so a hook that still splits from raw $COMMAND cannot
pass by merely being absent from the exception map.

Scan 1 checks that every `_lib_split_fragments` call site reads a
`*_UNQUOTED` variable, with named exceptions in `RAW_SPLIT_BY_DESIGN`
(each entry's own reason string explains why that one site stays raw).

Scan 2 checks that `deny-private-project-refs.sh`'s four content-scan sink
sites all read `SCAN_TARGET_BOTH`, the raw+stripped union buffer, not raw
`$SCAN_TARGET` alone:
- The tracker-ID `HITS` pipeline.
- The structural fast-path pre-check.
- The structural per-detector loop.
- The blocklist's `matched_lines` grep.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from helpers import HOOKS_DIR

DENY_PRIVATE_PROJECT_REFS = HOOKS_DIR / "deny-private-project-refs.sh"

# Matches the variable token _lib_split_fragments is called with:
# "$VAR" or "${VAR}", double-quoted per this codebase's own convention
# (shell-script-conventions.md, "Quote every expansion").
_SPLIT_FRAGMENTS_CALL_RE = re.compile(r'_lib_split_fragments\s+"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"')

# Case-insensitive: enforce-marker-script-shape.sh's local helper parameter
# is lowercase (command_unquoted) while every top-level hook variable is
# UPPER_SNAKE (COMMAND_UNQUOTED) — both spellings satisfy the same
# caller-strips-first contract this scan enforces.
_UNQUOTED_SUFFIX_RE = re.compile(r"_unquoted$", re.IGNORECASE)

# filename -> (reason, exact variable name the exception covers). Exactly
# two entries. A call site whose file is here but whose variable does NOT
# match the recorded name is still a violation — the exception is scoped
# to one specific, already-audited call site per file, not a blanket pass
# for every _lib_split_fragments call the file happens to contain.
RAW_SPLIT_BY_DESIGN = {
    "deny-pii-in-commits.sh": (
        "the raw fragment must reach "
        "_lib_commit_fragment_has_worktree_target's own xargs -n1 tokenizer — "
        "pre-stripping splits a quoted -m message into words the pathspec "
        "check misreads as a worktree target.",
        "COMMAND",
    ),
    "deny-invisible-commit-content.sh": (
        "arm 2's split is masked, not stripped, by design: an operator "
        "inside a quoted argument must not synthesize a fake commit "
        "fragment.",
        "MASKED_COMMAND",
    ),
}


def _hook_scripts() -> list[Path]:
    return sorted(HOOKS_DIR.glob("*.sh"))


def _split_fragments_call_sites() -> list[tuple[Path, str]]:
    """(hook_path, variable_token) for every _lib_split_fragments call site
    across every hook script."""
    sites = []
    for path in _hook_scripts():
        text = path.read_text()
        for match in _SPLIT_FRAGMENTS_CALL_RE.finditer(text):
            sites.append((path, match.group(1)))
    return sites


def _is_compliant_split(variable: str, filename: str, exception_map: dict) -> bool:
    if _UNQUOTED_SUFFIX_RE.search(variable):
        return True
    exception = exception_map.get(filename)
    return exception is not None and exception[1] == variable


def _split_fragments_violations(exception_map: dict) -> list[str]:
    return [
        f"{path.name}: _lib_split_fragments call reads ${{{variable}}}, not a "
        "*_UNQUOTED variable and not a recorded RAW_SPLIT_BY_DESIGN exception"
        for path, variable in _split_fragments_call_sites()
        if not _is_compliant_split(variable, path.name, exception_map)
    ]


class TestSplitFragmentsCallSitesQuoteStripped:
    """Scan 1: every _lib_split_fragments call site splits from a
    *_UNQUOTED variable, with exactly two named exceptions.

    Checks the call-site variable's name only, not that it was actually
    assigned via _lib_strip_shell_quotes — unlike Scan 2 below, which does
    check the SCAN_TARGET_UNQUOTED assignment itself."""

    def test_scan_finds_call_sites(self):
        """Sanity check the regex itself still matches real call sites, at
        the exact count (12) rather than a slack floor — a future call site
        regressing to an entirely-unquoted-variable form (invisible to
        _SPLIT_FRAGMENTS_CALL_RE, which only recognizes the
        "$VAR"/"${VAR}" double-quoted-expansion call shape) must drop this
        count and fail loudly rather than pass silently under a >= floor.
        The count is 12: _lib_command_invokes_git_subcmd,
        _lib_command_invokes_tool_subcmd, and
        _lib_command_concludes_commit_shape each add one compliant call site
        inside _lib.sh itself."""
        assert len(_split_fragments_call_sites()) == 12

    def test_every_call_site_reads_an_unquoted_variable_or_named_exception(self):
        violations = _split_fragments_violations(RAW_SPLIT_BY_DESIGN)
        assert not violations, "\n".join(violations)

    @pytest.mark.parametrize("filename", sorted(RAW_SPLIT_BY_DESIGN))
    def test_removing_exception_entry_fails_the_scan(self, filename):
        """Negative-path test: proves the exception map is load-bearing,
        not vacuous. Removing one file's entry must make the scan fail —
        and the reported violation must name that specific file, proving
        the scanner attributes a real future regression to the right file
        rather than reporting a generic "some site is non-compliant"."""
        narrowed_map = {k: v for k, v in RAW_SPLIT_BY_DESIGN.items() if k != filename}
        violations = _split_fragments_violations(narrowed_map)
        assert violations, f"expected a violation once {filename}'s exception entry is removed"
        assert any(filename in v for v in violations), (
            f"the reported violation(s) must name {filename} specifically: {violations}"
        )


# Scan 2: the SCAN_TARGET append sites in deny-private-project-refs.sh
# deliberately stay raw, since _lib_strip_shell_quotes deletes a literal
# apostrophe and the blocklist tier's whole-word match needs that
# punctuation to survive in at least one scanned copy (AcmeCorp's must
# still match AcmeCorp). The actual quote-split fix is the
# SCAN_TARGET_UNQUOTED/SCAN_TARGET_BOTH union buffer this scan checks at
# each sink site listed above.

_SCAN_TARGET_UNQUOTED_DEFINITION_RE = re.compile(r'SCAN_TARGET_UNQUOTED=\$\(_lib_strip_shell_quotes "\$SCAN_TARGET"\)')
_SCAN_TARGET_BOTH_DEFINITION_RE = re.compile(
    r"SCAN_TARGET_BOTH=\$\(printf '%s\\n%s' \"\$SCAN_TARGET\" \"\$SCAN_TARGET_UNQUOTED\"\)"
)

# label -> regex whose sole capture group is the variable token the sink
# actually reads. All four must resolve to SCAN_TARGET_BOTH.
_TIER_SINK_PATTERNS = {
    "tracker-ID HITS pipeline": re.compile(r"HITS=\$\(printf '%s' \"\$([A-Za-z_][A-Za-z0-9_]*)\""),
    "structural fast-path pre-check": re.compile(
        r'grep -Eq -- "\$structural_combined_pattern" <<< "\$([A-Za-z_][A-Za-z0-9_]*)"'
    ),
    "structural per-detector loop": re.compile(r'grep -Eq -- "\$detector_pattern" <<< "\$([A-Za-z_][A-Za-z0-9_]*)"'),
    "blocklist matched_lines grep": re.compile(
        r"matched_lines=\$\(printf '%s' \"\$([A-Za-z_][A-Za-z0-9_]*)\" \| grep -iw -F"
    ),
}


def _union_scan_violations(text: str) -> list[str]:
    violations = []
    if not _SCAN_TARGET_UNQUOTED_DEFINITION_RE.search(text):
        violations.append(
            "deny-private-project-refs.sh: no SCAN_TARGET_UNQUOTED=$(_lib_strip_shell_quotes "
            '"$SCAN_TARGET") definition found — the quote-split union-scan fix is missing'
        )
    if not _SCAN_TARGET_BOTH_DEFINITION_RE.search(text):
        violations.append(
            "deny-private-project-refs.sh: no SCAN_TARGET_BOTH=$(printf '%s\\n%s' \"$SCAN_TARGET\" "
            '"$SCAN_TARGET_UNQUOTED") definition found — the raw+stripped union buffer is missing'
        )
    if violations:
        return violations
    for tier_label, pattern in _TIER_SINK_PATTERNS.items():
        match = pattern.search(text)
        if match is None:
            violations.append(f"deny-private-project-refs.sh: could not find the {tier_label}")
            continue
        if match.group(1) != "SCAN_TARGET_BOTH":
            violations.append(
                f"deny-private-project-refs.sh: the {tier_label} reads ${{{match.group(1)}}}, not "
                "the raw+stripped union SCAN_TARGET_BOTH — a quote-split token would evade "
                "detection in this tier"
            )
    return violations


class TestUnionScanCoversAllThreeTiers:
    """Scan 2: every one of the three scan tiers (tracker-ID, structural,
    blocklist) reads the raw+stripped union buffer, not raw $SCAN_TARGET
    alone."""

    def test_all_tier_sinks_read_the_union_buffer(self):
        violations = _union_scan_violations(DENY_PRIVATE_PROJECT_REFS.read_text())
        assert not violations, "\n".join(violations)

    @pytest.mark.parametrize("tier_label", sorted(_TIER_SINK_PATTERNS))
    def test_regressing_a_tier_sink_to_raw_scan_target_fails_the_scan(self, tier_label):
        """Negative-path test: regresses one tier's sink back to reading
        raw $SCAN_TARGET (the exact quote-split-bypass shape this scan
        guards against) and confirms the reported violation names
        deny-private-project-refs.sh and the regressed tier specifically."""
        text = DENY_PRIVATE_PROJECT_REFS.read_text()
        pattern = _TIER_SINK_PATTERNS[tier_label]
        match = pattern.search(text)
        assert match is not None, f"fixture setup: could not find the {tier_label} in the source"
        start, end = match.span(1)
        regressed_text = text[:start] + "SCAN_TARGET" + text[end:]
        assert regressed_text != text, "fixture did not actually change the source text"
        violations = _union_scan_violations(regressed_text)
        assert violations, f"expected a violation once the {tier_label} is regressed to raw $SCAN_TARGET"
        assert any("deny-private-project-refs.sh" in v and tier_label in v for v in violations)
