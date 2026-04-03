"""Smart copy kitten — joins wrapped prose lines, preserves code and structure."""

import subprocess
import re

from kittens.tui.handler import result_handler


def main(args: list[str]) -> None:
    raise SystemExit('This kitten must be run with no_ui=True')


@result_handler(no_ui=True)
def handle_result(
    args: list[str], answer: str, target_window_id: int, boss: 'Boss'
) -> None:
    window = boss.window_id_map.get(target_window_id)
    if window is None:
        return

    selection = window.text_for_selection()
    if not selection:
        return

    columns = window.screen.columns if window.screen else 80

    try:
        processed = smart_unwrap(selection, columns)
    except Exception:
        processed = selection

    encoded = processed.encode('utf-8')
    result = subprocess.run(
        ['kitten', 'clipboard'],
        input=encoded,
        check=False,
    )
    if result.returncode != 0:
        subprocess.run(
            ['xclip', '-selection', 'clipboard'],
            input=encoded,
            check=False,
        )


def smart_unwrap(text: str, columns: int = 80) -> str:
    """Join lines that look like wrapped prose. Preserve code and structure."""
    threshold = max(int(columns * 0.85), 40)
    lines = text.split('\n')
    result: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if not line.strip():
            result.append(line)
            i += 1
            continue

        joined = line
        current_indent = _indent_level(line)
        last_segment_len = len(line.rstrip())

        while i + 1 < len(lines):
            next_line = lines[i + 1]
            next_indent = _indent_level(next_line)

            if not next_line.strip():
                break
            if _is_structural(next_line):
                break
            if _is_tabular(next_line) or _is_tabular(line):
                break
            if joined.rstrip().endswith(':'):
                break
            if _is_url_line(joined.rstrip()):
                break

            # Different indent levels suggest code, not wrapped prose
            if next_indent != current_indent:
                break

            if last_segment_len < threshold:
                break

            joined = joined.rstrip() + ' ' + next_line.lstrip()
            last_segment_len = len(next_line.rstrip())
            i += 1

        result.append(joined)
        i += 1

    return '\n'.join(result)


def _indent_level(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_structural(line: str) -> bool:
    """Lines that indicate intentional structure (lists, headers, quotes, tables, diffs, logs)."""
    stripped = line.lstrip()
    # Bullets, headers, blockquotes
    if stripped.startswith(('- ', '* ', '> ', '# ')):
        return True
    # Numbered lists: 1. or 1)
    if re.match(r'^\d+[\.\)]\s', stripped):
        return True
    # Pipe-delimited tables
    if stripped.startswith('|'):
        return True
    # Diff hunks: +line, -line, @@ range @@
    if stripped.startswith(('+ ', '- ', '+\t', '-\t', '@@')):
        return True
    # Backtick code fences
    if stripped.startswith('```'):
        return True
    # ISO timestamp-prefixed log lines (2026-04-03T... or 2026-04-03 10:...)
    if re.match(r'^\d{4}-\d{2}-\d{2}[T ]', stripped):
        return True
    return False


def _is_tabular(line: str) -> bool:
    """Lines with column-aligned whitespace (tables, --help, aligned output)."""
    stripped = line.strip()
    # 3+ consecutive spaces in the middle of content indicates column alignment
    return bool(re.search(r'\S {3,}\S', stripped))


def _is_url_line(line: str) -> bool:
    return bool(re.search(r'https?://\S{10,}$', line))
