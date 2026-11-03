#!/usr/bin/env python3
"""Replay Asahi-Quill's ANSI output through a small terminal emulator.

The editor draws its frame with absolute cursor addressing and never relies on
newlines (it runs in raw mode, where "\\n" does not return the carriage).  This
script re-implements the relevant parts of a terminal -- CUP, SGR, erase, the
pending-wrap state after the last column, and wide characters -- and then
compares the reconstructed grid with the canvas cell by cell.

    python3 tests/verify_ansi.py
"""

import os
import sys
import unicodedata
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "asahi-quill.py")
sys.path.insert(0, os.path.dirname(HERE))

import re

RE_CUP = re.compile(r'\x1b\[(\d+);(\d+)H')
RE_SGR = re.compile(r'\x1b\[([0-9;]*)m')
RE_J = re.compile(r'\x1b\[0J')


def emulate(stream, cols, rows):
    """Replay the stream into a grid; returns (grid, scroll_count)."""
    grid = [[(' ', (0, 0, 0), (0, 0, 0))] * cols for _ in range(rows)]
    fg = bg = (0, 0, 0)
    r = c = 0
    scrolls = 0
    pending = False          # DECAWM: waiting to wrap after the last column
    i = 0
    n = len(stream)
    while i < n:
        ch = stream[i]
        if ch == '\x1b':
            m = RE_CUP.match(stream, i)
            if m:
                r, c = int(m.group(1)) - 1, int(m.group(2)) - 1
                pending = False
                i = m.end()
                continue
            m = RE_SGR.match(stream, i)
            if m:
                parts = [int(x) for x in m.group(1).split(';') if x != '']
                k = 0
                while k < len(parts):
                    if parts[k] == 38 and k + 4 < len(parts):
                        fg = tuple(parts[k + 2:k + 5])
                        k += 5
                    elif parts[k] == 48 and k + 4 < len(parts):
                        bg = tuple(parts[k + 2:k + 5])
                        k += 5
                    elif parts[k] == 0:
                        fg = bg = (0, 0, 0)
                        k += 1
                    else:
                        k += 1
                i = m.end()
                continue
            if RE_J.match(stream, i):
                for rr in range(r, rows):
                    start = c if rr == r else 0
                    for cc in range(start, cols):
                        grid[rr][cc] = (' ', fg, bg)
                i += 4
                continue
            i += 1
            continue
        if ch == '\r':
            c = 0
            pending = False
            i += 1
            continue
        if ch == '\n':
            r += 1              # raw mode: no carriage return
            pending = False
            i += 1
        else:
            if pending:
                r += 1
                c = 0
                pending = False
            w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if 0 <= r < rows and 0 <= c < cols:
                grid[r][c] = (ch, fg, bg)
            c += w
            i += 1
            if c >= cols:
                c = cols - 1
                pending = True
        if r >= rows:
            grid.pop(0)
            grid.append([(' ', fg, bg)] * cols)
            r = rows - 1
            scrolls += 1
    return grid, scrolls


def diff_count(want, grid, cols, rows):
    """Compare canvas and replay; skip the cell after a wide character."""
    skip = set()
    for rr in range(rows):
        for cc in range(cols - 1):
            ch = want[rr * cols + cc][0]
            if ch and unicodedata.east_asian_width(ch) in ("W", "F"):
                skip.add(rr * cols + cc + 1)
    bad = 0
    for idx, (w, g) in enumerate(zip(want, [c for row in grid for c in row])):
        if idx in skip:
            continue
        if w != g:
            bad += 1
            if bad <= 5:
                print("  diff @%d:%d want=%r got=%r" % (idx // cols, idx % cols, w, g))
    return bad


def check(cols, rows, theme=None, dialog=False, wide=False):
    spec = importlib.util.spec_from_file_location("aq", APP)
    aq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aq)

    app = aq.App(cols, rows, theme=theme, demo=not wide)
    if wide:
        app.doc.lines = ["ＡＢＣＤ：wide-character test for Asahi-Quill ＡＢＣＤＥＦＧ",
                         "ＳＥＣＯＮＤ line: mixed ＡＢＣ and ascii, check alignment.",
                         "", "  - indented ＸＹＺ", "。" * 0 + "Ａ" * 40]
        app.cur = [0, 0]
        app.anchor = [0, 2]
        app.recount()
    if dialog:
        app.open_dialog("open")
    cv = aq.render(app)
    grid, scrolls = emulate(cv.to_ansi(), cols, rows)
    bad = diff_count(list(cv.cells()), grid, cols, rows)
    ok = bad == 0 and scrolls == 0
    print("%-16s %3dx%-3d cells=%d scroll=%d mismatch=%d %s"
          % (theme or "default", cols, rows, cols * rows, scrolls, bad,
             "OK" if ok else "FAIL"))
    return ok


def main():
    cases = [
        (100, 32, None, False, False),
        (82, 34, None, False, False),
        (60, 20, None, False, False),
        (100, 32, "pocket", False, False),
        (100, 32, "studio", False, False),
        (100, 32, "next", False, False),
        (100, 32, "paper", False, False),
        (100, 32, None, True, False),
        (82, 34, None, False, True),
    ]
    results = [check(*c) for c in cases]
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
