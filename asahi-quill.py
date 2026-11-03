#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asahi-quill.py -- Asahi-Quill, a mouse-first terminal editor

* click to place the caret, drag to select, double-click to pick a word, wheel to scroll
* menu bar, toolbar and status line are all drawn by hand (half blocks + 24-bit colour)
* CJK aware: wide characters take two cells; caret and selection use display width
* Ctrl-S save, Ctrl-O open, Ctrl-Z undo, Ctrl-F find; clipboard via pbcopy/pbpaste

Usage:
    python3 asahi-quill.py                # empty document
    python3 asahi-quill.py notes.txt      # open a file
    python3 asahi-quill.py --png a.png [--demo]
"""

import os
import sys
import time
import unicodedata
import select
import signal
import shutil
import subprocess
import termios
import tty

import termui as T
import themes

MENUS = [
    ("File", [
        ("New", "new"), ("Open...", "open"), ("Save", "save"),
        ("Save As...", "saveas"), None, ("Exit", "exit")]),
    ("Edit", [
        ("Undo", "undo"), None, ("Cut", "cut"), ("Copy", "copy"),
        ("Paste", "paste"), ("Select All", "all"), None, ("Find...", "find")]),
    ("Format", [("Word Wrap  on/off", "wrap")]),
    ("Help", [("About", "about"), ("Keys", "keys")]),
]

TOOLBAR = [("New", "new"), ("Open", "open"), ("Save", "save"),
           ("Undo", "undo"), ("Find", "find"), ("Wrap", "wrap")]

TOOL_TIPS = {
    "new": "New document",
    "open": "Open file · Ctrl-O",
    "save": "Save · Ctrl-S",
    "undo": "Undo · Ctrl-Z",
    "find": "Find · Ctrl-F",
    "wrap": "Word wrap on/off",
}
MENU_TIPS = {
    "File": "File menu: new / open / save",
    "Edit": "Edit menu: undo, clipboard, find",
    "Format": "Format menu: word wrap",
    "Help": "Help: about and keys",
}

DEMO_TEXT = """Asahi-Quill
============

A tiny word processor that lives in your terminal.

- click anywhere to place the caret
- drag to select, double-click to select a word
- the wheel scrolls the page
- menus and toolbar buttons are clickable

Save with Ctrl+S, open with Ctrl+O, find with Ctrl+F.
Undo is Ctrl+Z; copy/paste use the system clipboard.

F2 changes the look. That's it - no modes, no chords.
"""


def char_w(ch):
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def str_w(s):
    return sum(char_w(c) for c in s)


class Doc:
    def __init__(self, path=None):
        self.lines = [""]
        self.path = path
        self.modified = False
        self.undo = []
        if path:
            self.load(path)

    # ---------- file ----------
    def load(self, path):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        self.lines = data.split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        if not self.lines:
            self.lines = [""]
        self.path = path
        self.modified = False

    def save(self, path=None):
        if path:
            self.path = path
        if not self.path:
            return False
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.lines) + "\n")
        self.modified = False
        return True

    def text(self):
        return "\n".join(self.lines)

    # ---------- undo ----------
    def push_undo(self):
        self.undo.append((list(self.lines),))
        if len(self.undo) > 200:
            self.undo.pop(0)

    def pop_undo(self):
        if not self.undo:
            return False
        self.lines = list(self.undo.pop()[0])
        self.modified = True
        return True


class App:
    def __init__(self, cols, rows, path=None, demo=False, theme=None):
        self.cols, self.rows = cols, rows
        if isinstance(theme, str):
            self.th = themes.get(theme)
        else:
            self.th = theme or themes.load_last() or themes.VIVID
        self.doc = Doc(path)
        if demo:
            self.doc.lines = DEMO_TEXT.rstrip("\n").split("\n")
            self.doc.path = None
            self.doc.modified = False
        self.cur = [0, 0]           # [line, character index]
        self.anchor = None          # selection anchor
        self.scroll = 0             # first visible row (counted in wrapped rows)
        self.wrap = True
        self.cwd = os.path.expanduser("~/Documents")
        if not os.path.isdir(self.cwd):
            self.cwd = os.path.expanduser("~")
        self.menu_open = None
        self.menu_sel = 0
        self.dialog = None
        self.status = ""
        self.status_until = 0.0
        self.quit = False
        self.hits = []
        self.caret_cell = None
        self.caret_on = True
        self.dragging = False
        self.last_click = (0.0, 0, 0)
        self.recount()
        self.quit_armed = False
        self.hover = None
        self.menu_pos = []
        self.pressed = None
        self.pressed_until = 0.0

    # ---------------------------------------------------------------- geometry
    def text_width(self):
        return max(20, self.cols - 6)

    def head_rows(self):
        """Rows taken by title bar / menu / toolbar / ruler."""
        return {"bevel": 7, "flat": 4, "paper": 1, "studio": 4}.get(self.th.kind, 7)

    def bar_rows(self):
        return 2 if self.th.kind == "bevel" else 1

    def rail_width(self):
        if self.cols >= 110:
            return 20
        if self.cols >= 92:
            return 18
        if self.cols >= 78:
            return 15
        return 11

    def paper_rect(self):
        if self.th.kind == "studio":
            rw = self.rail_width()
            return rw + 3, 4, self.cols - rw - 4, self.rows - 6
        y = self.head_rows()
        return 2, y, self.cols - 4, self.rows - y - self.bar_rows()

    def text_origin(self):
        if self.th.kind == "studio":
            return self.rail_width() + 5, 6
        y = self.head_rows()
        return 3, y + 1

    def text_rows(self):
        if self.th.kind == "studio":
            return max(3, self.paper_rect()[3] - 4)
        return max(3, self.rows - self.head_rows() - self.bar_rows() - 2)

    # ---------------------------------------------------------------- wrapping
    def segments(self):
        """Split logical lines into visual rows: [(line, start, end), ...]."""
        out = []
        width = self.text_width()
        for idx, line in enumerate(self.doc.lines):
            if not self.wrap:
                out.append((idx, 0, len(line)))
                continue
            if not line:
                out.append((idx, 0, 0))
                continue
            start, used = 0, 0
            for i, ch in enumerate(line):
                w = char_w(ch)
                if used + w > width:
                    out.append((idx, start, i))
                    start, used = i, 0
                used += w
            out.append((idx, start, len(line)))
        return out

    def seg_index_of(self, row, col):
        segs = self.segments()
        best = 0
        for i, (r, s, e) in enumerate(segs):
            if r == row and s <= col <= e:
                best = i
                if col < e:
                    break
        return best

    def ensure_visible(self):
        i = self.seg_index_of(*self.cur)
        rows = self.text_rows()
        if i < self.scroll:
            self.scroll = i
        elif i >= self.scroll + rows:
            self.scroll = i - rows + 1

    # ---------------------------------------------------------------- selection
    def sel(self):
        if self.anchor is None:
            return None
        a, b = tuple(self.anchor), tuple(self.cur)
        return (a, b) if a <= b else (b, a)

    def selection_text(self):
        sel = self.sel()
        if not sel:
            return ""
        (r1, c1), (r2, c2) = sel
        if r1 == r2:
            return self.doc.lines[r1][c1:c2]
        out = [self.doc.lines[r1][c1:]]
        out.extend(self.doc.lines[r1 + 1:r2])
        out.append(self.doc.lines[r2][:c2])
        return "\n".join(out)

    def delete_selection(self):
        sel = self.sel()
        if not sel:
            return False
        (r1, c1), (r2, c2) = sel
        if r1 == r2:
            self.doc.lines[r1] = self.doc.lines[r1][:c1] + self.doc.lines[r1][c2:]
        else:
            self.doc.lines[r1] = self.doc.lines[r1][:c1] + self.doc.lines[r2][c2:]
            del self.doc.lines[r1 + 1:r2 + 1]
        self.cur = [r1, c1]
        self.anchor = None
        self.doc.modified = True
        return True

    # ---------------------------------------------------------------- editing
    def insert(self, s):
        self.doc.push_undo()
        self.delete_selection()
        row, col = self.cur
        line = self.doc.lines[row]
        self.doc.lines[row] = line[:col] + s + line[col:]
        self.cur = [row, col + len(s)]
        self.doc.modified = True
        self.after_edit()

    def insert_text(self, s):
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in s:
            self.insert(s)
            return
        self.doc.push_undo()
        self.delete_selection()
        row, col = self.cur
        line = self.doc.lines[row]
        head, tail = line[:col], line[col:]
        parts = s.split("\n")
        self.doc.lines[row] = head + parts[0]
        for k, part in enumerate(parts[1:], start=1):
            self.doc.lines.insert(row + k, part)
        last = row + len(parts) - 1
        self.doc.lines[last] = self.doc.lines[last] + tail
        self.cur = [last, len(parts[-1])]
        self.doc.modified = True
        self.after_edit()

    def newline(self):
        self.doc.push_undo()
        self.delete_selection()
        row, col = self.cur
        line = self.doc.lines[row]
        self.doc.lines[row] = line[:col]
        self.doc.lines.insert(row + 1, line[col:])
        self.cur = [row + 1, 0]
        self.doc.modified = True
        self.after_edit()

    def backspace(self):
        if self.sel():
            self.doc.push_undo()
            self.delete_selection()
            self.after_edit()
            return
        row, col = self.cur
        if col > 0:
            self.doc.push_undo()
            line = self.doc.lines[row]
            self.doc.lines[row] = line[:col - 1] + line[col:]
            self.cur = [row, col - 1]
        elif row > 0:
            self.doc.push_undo()
            prev = self.doc.lines[row - 1]
            self.doc.lines[row - 1] = prev + self.doc.lines[row]
            del self.doc.lines[row]
            self.cur = [row - 1, len(prev)]
        else:
            return
        self.doc.modified = True
        self.after_edit()

    def delete(self):
        if self.sel():
            self.doc.push_undo()
            self.delete_selection()
            self.after_edit()
            return
        row, col = self.cur
        line = self.doc.lines[row]
        if col < len(line):
            self.doc.push_undo()
            self.doc.lines[row] = line[:col] + line[col + 1:]
        elif row + 1 < len(self.doc.lines):
            self.doc.push_undo()
            self.doc.lines[row] = line + self.doc.lines[row + 1]
            del self.doc.lines[row + 1]
        else:
            return
        self.doc.modified = True
        self.after_edit()

    def recount(self):
        chars = sum(len(line) for line in self.doc.lines) + max(0, len(self.doc.lines) - 1)
        words = sum(len(line.split()) for line in self.doc.lines)
        self.counts = (chars, words)

    def after_edit(self):
        self.recount()
        self.ensure_visible()

    def move(self, dr, dc, select=False):
        if select and self.anchor is None:
            self.anchor = list(self.cur)
        elif not select:
            self.anchor = None
        row = max(0, min(len(self.doc.lines) - 1, self.cur[0] + dr))
        col = self.cur[1]
        if dr:
            col = min(col, len(self.doc.lines[row]))
        col = max(0, min(len(self.doc.lines[row]), col + dc))
        self.cur = [row, col]
        self.ensure_visible()

    def home(self, select=False):
        if select and self.anchor is None:
            self.anchor = list(self.cur)
        elif not select:
            self.anchor = None
        self.cur = [self.cur[0], 0]
        self.ensure_visible()

    def end(self, select=False):
        if select and self.anchor is None:
            self.anchor = list(self.cur)
        elif not select:
            self.anchor = None
        self.cur = [self.cur[0], len(self.doc.lines[self.cur[0]])]
        self.ensure_visible()

    def set_caret(self, row, col, select=False):
        if select and self.anchor is None:
            self.anchor = list(self.cur)
        elif not select:
            self.anchor = None
        self.cur = [row, col]
        self.ensure_visible()

    # ---------------------------------------------------------------- clipboard
    def copy(self):
        text = self.selection_text()
        if not text:
            row, col = self.cur
            text = self.doc.lines[row]
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
        self.toast("copied %d chars" % len(text))

    def cut(self):
        text = self.selection_text()
        if not text:
            return
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
        self.doc.push_undo()
        self.delete_selection()
        self.after_edit()
        self.toast("cut %d chars" % len(text))

    def paste(self):
        try:
            out = subprocess.run(["pbpaste"], capture_output=True, timeout=2).stdout
            text = out.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            text = ""
        if not text:
            self.toast("clipboard is empty")
            return
        self.insert_text(text)
        self.toast("pasted")

    # ---------------------------------------------------------------- file actions
    def save(self, path=None):
        try:
            self.doc.save(path)
        except OSError as exc:
            self.toast("save failed: %s" % exc)
            return False
        self.quit_armed = False
        self.toast("saved %s" % (self.doc.path or "?"))
        return True

    def open_path(self, path):
        try:
            self.doc.load(path)
        except OSError as exc:
            self.toast("open failed: %s" % exc)
            return
        self.cur = [0, 0]
        self.anchor = None
        self.scroll = 0
        self.recount()
        self.toast("opened %s" % os.path.basename(path))

    def new_doc(self):
        self.doc = Doc()
        self.cur = [0, 0]
        self.anchor = None
        self.scroll = 0
        self.recount()
        self.toast("new document")

    # ---------------------------------------------------------------- dispatch
    def do(self, action):
        self.menu_open = None
        if action == "new":
            self.new_doc()
        elif action == "open":
            self.open_dialog()
        elif action == "save":
            if self.doc.path:
                self.save()
            else:
                self.save_as_dialog()
        elif action == "saveas":
            self.save_as_dialog()
        elif action == "exit":
            self.try_quit()
        elif action == "undo":
            if self.doc.pop_undo():
                self.cur = [min(self.cur[0], len(self.doc.lines) - 1), self.cur[1]]
                self.cur[1] = min(self.cur[1], len(self.doc.lines[self.cur[0]]))
                self.after_edit()
                self.toast("undo")
            else:
                self.toast("nothing to undo")
        elif action in ("cut", "copy", "paste"):
            getattr(self, action)()
        elif action == "all":
            self.anchor = [0, 0]
            self.cur = [len(self.doc.lines) - 1, len(self.doc.lines[-1])]
            self.ensure_visible()
        elif action == "find":
            self.open_dialog(kind="find")
        elif action == "wrap":
            self.wrap = not self.wrap
            self.toast("word wrap %s" % ("on" if self.wrap else "off"))
            self.ensure_visible()
        elif action == "about":
            self.open_dialog(kind="about")
        elif action == "keys":
            self.open_dialog(kind="keys")

    # ---------------------------------------------------------------- dialogs
    def open_dialog(self, kind="open", **kw):
        if kind == "open":
            entries = []
            try:
                for name in sorted(os.listdir(self.cwd)):
                    if name.startswith("."):
                        continue
                    full = os.path.join(self.cwd, name)
                    entries.append((name, os.path.isdir(full)))
            except OSError:
                entries = []
            entries.insert(0, ("..", True))
            self.dialog = {"kind": "open", "entries": entries, "sel": 0,
                           "scroll": 0, "dir": self.cwd}
        elif kind == "saveas":
            default = os.path.basename(self.doc.path) if self.doc.path else "untitled.txt"
            self.dialog = {"kind": "saveas", "text": default, "dir": self.cwd}
        elif kind == "find":
            self.dialog = {"kind": "find", "text": ""}
        else:
            self.dialog = {"kind": kind}

    def close_dialog(self):
        self.dialog = None

    def find_next(self, needle):
        if not needle:
            return False
        row, col = self.cur
        lines = self.doc.lines
        # search forward from the caret, wrapping at the end
        for i in range(len(lines)):
            r = (row + i) % len(lines)
            start = col if i == 0 else 0
            pos = lines[r].find(needle, start)
            if pos < 0 and i > 0:
                pos = lines[r].find(needle)
            if pos >= 0:
                self.anchor = [r, pos]
                self.cur = [r, pos + len(needle)]
                self.ensure_visible()
                self.toast("found in line %d" % (r + 1))
                return True
        self.toast("not found: %s" % needle)
        return False

    # ---------------------------------------------------------------- keyboard
    def key(self, k):
        if self.dialog:
            return self.key_dialog(k)
        if self.menu_open is not None:
            return self.key_menu(k)

        if k in ("\x11",):                      # Ctrl-Q
            self.try_quit()
            return False
        if k == "\x13":                         # Ctrl-S
            self.do("save")
            return True
        if k == "\x0f":                         # Ctrl-O
            self.do("open")
            return True
        if k == "\x06":                         # Ctrl-F
            self.do("find")
            return True
        if k == "\x1a":                         # Ctrl-Z
            self.do("undo")
            return True
        if k == "\x01":                         # Ctrl-A
            self.do("all")
            return True
        if k == "\x1bOQ" or k == "\x1b[12~":    # F2 cycles themes
            self.th = themes.cycle(self.th)
            themes.save_last(self.th)
            self.ensure_visible()
            self.toast("theme: %s" % self.th.name)
            return True
        if k == "\x03":                         # Ctrl-C
            self.do("copy")
            return True
        if k == "\x18":                         # Ctrl-X
            self.do("cut")
            return True
        if k == "\x16":                         # Ctrl-V
            self.do("paste")
            return True

        if k in ("\r", "\n"):
            self.newline()
            return True
        if k in ("\x7f", "\x08"):
            self.backspace()
            return True
        if k == "\x1b[3~":
            self.delete()
            return True
        if k == "\t":
            self.insert("    ")
            return True
        if k == "\x1b":
            self.anchor = None
            return True
        if k == "\x1b[A":
            self.move(-1, 0, select=False)
            return True
        if k == "\x1b[B":
            self.move(1, 0)
            return True
        if k == "\x1b[C":
            self.move(0, 1)
            return True
        if k == "\x1b[D":
            self.move(0, -1)
            return True
        if k in ("\x1b[1;2A",):
            self.move(-1, 0, select=True)
            return True
        if k in ("\x1b[1;2B",):
            self.move(1, 0, select=True)
            return True
        if k in ("\x1b[1;2C",):
            self.move(0, 1, select=True)
            return True
        if k in ("\x1b[1;2D",):
            self.move(0, -1, select=True)
            return True
        if k == "\x1b[H":
            self.home()
            return True
        if k == "\x1b[F":
            self.end()
            return True
        if k in ("\x1b[5~",):
            for _ in range(self.text_rows() - 1):
                self.move(-1, 0)
            return True
        if k in ("\x1b[6~",):
            for _ in range(self.text_rows() - 1):
                self.move(1, 0)
            return True
        if len(k) == 1 and k.isprintable():
            self.insert(k)
            return True
        return False

    def key_menu(self, k):
        items = [it for it in MENUS[self.menu_open][1] if it]
        idx = self.menu_sel
        if k in ("\x1b[A", "k"):
            self.menu_sel = (idx - 1) % len(items)
            return True
        if k in ("\x1b[B", "j"):
            self.menu_sel = (idx + 1) % len(items)
            return True
        if k in ("\r", "\n", " "):
            self.do(items[idx][1])
            return True
        if k in ("\x1b", "\x1b[D", "\x1b[C"):
            self.menu_open = None
            return True
        return False

    def key_dialog(self, k):
        d = self.dialog
        kind = d["kind"]
        if kind == "open":
            if k in ("\x1b[A", "k"):
                d["sel"] = max(0, d["sel"] - 1)
                self.clamp_dialog_scroll()
                return True
            if k in ("\x1b[B", "j"):
                d["sel"] = min(len(d["entries"]) - 1, d["sel"] + 1)
                self.clamp_dialog_scroll()
                return True
            if k in ("\r", "\n"):
                self.open_selected()
                return True
            if k == "\x1b":
                self.close_dialog()
                return True
            return False
        if kind == "find":
            if k in ("\r", "\n"):
                needle = d["text"]
                self.close_dialog()
                self.find_next(needle)
                return True
            if k == "\x1b":
                self.close_dialog()
                return True
            if k in ("\x7f", "\x08"):
                d["text"] = d["text"][:-1]
                return True
            if len(k) == 1 and k.isprintable():
                d["text"] += k
                return True
            return False
        if kind == "saveas":
            if k in ("\r", "\n"):
                name = d["text"].strip()
                if name:
                    path = name if os.path.isabs(name) else os.path.join(d["dir"], name)
                    self.close_dialog()
                    self.save(path)
                return True
            if k == "\x1b":
                self.close_dialog()
                return True
            if k in ("\x7f", "\x08"):
                d["text"] = d["text"][:-1]
                return True
            if len(k) == 1 and k.isprintable():
                d["text"] += k
                return True
            return False
        if k in ("\r", "\n", "\x1b", " "):
            self.close_dialog()
            return True
        return False

    def clamp_dialog_scroll(self):
        d = self.dialog
        rows = self.dialog_list_rows()
        if d["sel"] < d["scroll"]:
            d["scroll"] = d["sel"]
        elif d["sel"] >= d["scroll"] + rows:
            d["scroll"] = d["sel"] - rows + 1

    def dialog_list_rows(self):
        return max(3, min(14, self.rows - 14))

    def open_selected(self):
        d = self.dialog
        name, is_dir = d["entries"][d["sel"]]
        full = os.path.normpath(os.path.join(d["dir"], name))
        if is_dir:
            self.cwd = full
            self.dialog = None
            self.open_dialog("open")
            return
        self.close_dialog()
        self.open_path(full)

    # ---------------------------------------------------------------- mouse
    def click(self, x, y):
        self.dragging = False
        if self.dialog:
            return self.click_dialog(x, y)
        if self.menu_open is not None:
            for kind, payload, x0, y0, x1, y1, *_tip in reversed(self.hits):
                if kind == "menu" and x0 <= x <= x1 and y0 <= y <= y1:
                    self.do(payload)
                    return True
            self.menu_open = None
            return True
        for kind, payload, x0, y0, x1, y1, *_tip in reversed(self.hits):
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.pressed = (x0, y0, x1, y1)
                self.pressed_until = time.time() + 0.14
                if kind == "action":
                    self.do(payload)
                elif kind == "menubar":
                    self.menu_open = payload
                    self.menu_sel = 0
                elif kind == "text":
                    self.click_text(x, y)
                return True
        return False

    def click_text(self, x, y):
        tx, ty = self.text_origin()
        segs = self.segments()
        idx = self.scroll + (y - ty)
        if idx < 0 or idx >= len(segs):
            return
        row, s, e = segs[idx]
        col = s
        used = 0
        target = x - tx
        for i in range(s, e):
            w = char_w(self.doc.lines[row][i])
            if used + w > target:
                break
            used += w
            col = i + 1
        # double click selects a word
        now = time.time()
        last_t, lx, ly = self.last_click
        if now - last_t < 0.45 and abs(lx - x) <= 1 and abs(ly - y) <= 1:
            self.select_word(row, col)
            self.last_click = (0.0, 0, 0)
            return
        self.last_click = (now, x, y)
        self.dragging = True
        self.anchor = None
        self.set_caret(row, col)
        self.anchor = [row, col]

    def select_word(self, row, col):
        line = self.doc.lines[row]
        if not line:
            return
        def issym(c):
            return not (c.isalnum() or c in "_")
        a = min(col, len(line) - 1)
        if issym(line[a]):
            return
        s = a
        while s > 0 and not issym(line[s - 1]):
            s -= 1
        e = a
        while e < len(line) and not issym(line[e]):
            e += 1
        self.anchor = [row, s]
        self.cur = [row, e]
        self.ensure_visible()

    def drag_to(self, x, y):
        if not self.dragging:
            return False
        tx, ty = self.text_origin()
        segs = self.segments()
        idx = max(0, min(len(segs) - 1, self.scroll + (y - ty)))
        row, s, e = segs[idx]
        col = s
        used = 0
        target = x - tx
        for i in range(s, e):
            w = char_w(self.doc.lines[row][i])
            if used + w > target:
                break
            used += w
            col = i + 1
        if self.anchor is None:
            self.anchor = list(self.cur)
        self.cur = [row, col]
        self.ensure_visible()
        return True

    def click_dialog(self, x, y):
        d = self.dialog
        for kind, payload, x0, y0, x1, y1, *_tip in reversed(self.hits):
            if kind == "dialog_item" and x0 <= x <= x1 and y0 <= y <= y1:
                d["sel"] = payload
                return True
            if kind == "dialog_open" and x0 <= x <= x1 and y0 <= y <= y1:
                self.open_selected()
                return True
            if kind == "dialog_ok" and x0 <= x <= x1 and y0 <= y <= y1:
                if d["kind"] == "open":
                    self.open_selected()
                elif d["kind"] == "saveas":
                    name = d["text"].strip()
                    if name:
                        path = name if os.path.isabs(name) else os.path.join(d["dir"], name)
                        self.close_dialog()
                        self.save(path)
                elif d["kind"] == "find":
                    needle = d["text"]
                    self.close_dialog()
                    self.find_next(needle)
                else:
                    self.close_dialog()
                return True
            if kind == "dialog_cancel" and x0 <= x <= x1 and y0 <= y <= y1:
                self.close_dialog()
                return True
        return False

    def wheel(self, up):
        step = -3 if up else 3
        self.scroll = max(0, min(self.scroll + step,
                                  max(0, len(self.segments()) - 1)))
        return True

    def try_quit(self):
        if self.doc.modified and not self.quit_armed:
            self.quit_armed = True
            self.toast("unsaved changes - Ctrl-Q again to discard", 4)
            return
        self.quit = True

    def hovering(self, x0, y0, x1, y1):
        h = self.hover
        return h is not None and x0 <= h[0] <= x1 and y0 <= h[1] <= y1

    def toast(self, msg, secs=2.5):
        self.status = msg
        self.status_until = time.time() + secs


# ---------------------------------------------------------------- rendering
def render(app):
    W, H = app.cols, app.rows
    th = app.th
    cv = T.Canvas(W, H)
    app.hits = []

    if th.kind == "bevel":
        chrome_bevel(app, cv)
    elif th.kind == "flat":
        chrome_flat(app, cv)
    elif th.kind == "studio":
        chrome_studio(app, cv)
    else:
        chrome_paper(app, cv)

    draw_paper(app, cv)

    if app.menu_open is not None:
        draw_menu(app, cv)
    if app.dialog:
        draw_dialog(app, cv)
    if th.kind == "studio":
        chips(app, cv)
    return cv


def doc_title(app):
    name = os.path.basename(app.doc.path) if app.doc.path else "Untitled"
    return name + (" *" if app.doc.modified else "")


# ---------------------------------------------------------------- page
def draw_paper(app, cv):
    th = app.th
    px, py, pw, ph = app.paper_rect()
    if th.kind == "studio":
        card(cv, px, py, pw, ph, th.paper, th.edge)
    else:
        cv.cfill(px, py, pw, ph, th.paper)
        cv.hline(px * 2, py * 2, pw * 2, th.edge)
        cv.hline(px * 2, (py + ph) * 2 - 1, pw * 2, th.edge)
    if th.kind != "paper":
        for yy in range(py, py + ph):
            cv.text(px, yy, '│', th.edge, th.paper)
            cv.text(px + pw - 1, yy, '│', th.edge, th.paper)

    tx, ty = app.text_origin()
    app.hits.append(("text", None, tx, ty, tx + app.text_width() - 1, ty + app.text_rows() - 1))
    if th.kind in ("bevel", "flat"):
        draw_ruler(app, cv, tx, py - 1)
    draw_text(app, cv, tx, ty)


def draw_ruler(app, cv, tx, y):
    th = app.th
    w = app.text_width()
    bg = th.chrome if th.kind == "bevel" else th.paper
    cv.cfill(0, y, app.cols, 1, bg)
    cv.hline(0, (y + 1) * 2 - 1, app.cols * 2, th.edge)
    if th.kind == "bevel":
        for i in range(0, w + 1, 5):
            cv.fill((tx + i) * 2, (y + 1) * 2 - 1, 1, 1,
                    (0x8A, 0x8A, 0x8A) if i % 10 == 0 else (0x9E, 0x9E, 0x9E))
    else:
        # flat themes: a faint tick every 10 cells instead of a solid band
        for i in range(0, w + 1, 10):
            cv.fill((tx + i) * 2, (y + 1) * 2 - 1, 1, 1, (0xBE, 0xBE, 0xBE))


def in_sel(row, col, sel):
    if not sel:
        return False
    (r1, c1), (r2, c2) = sel
    if r1 == r2:
        return row == r1 and c1 <= col < c2
    if row == r1:
        return col >= c1
    if row == r2:
        return col < c2
    return r1 < row < r2


def draw_text(app, cv, tx, ty):
    th = app.th
    segs = app.segments()
    rows = app.text_rows()
    sel = app.sel()
    cur = tuple(app.cur)
    app.caret_cell = None

    for i in range(rows):
        idx = app.scroll + i
        if idx >= len(segs):
            break
        row, s, e = segs[idx]
        line = app.doc.lines[row]
        y = ty + i
        x = tx
        line_fg = th.ink
        if th.kind == "studio":
            head = line.lstrip()
            if head.startswith("#"):
                line_fg = th.pink
            elif head.startswith(">"):
                line_fg = th.green
        for j in range(s, e):
            ch = line[j]
            fg, bg = line_fg, th.paper
            if in_sel(row, j, sel):
                fg, bg = th.accent_text, th.accent
            if (row, j) == cur:
                fg, bg = th.paper, th.ink
                app.caret_cell = (x, y, ch)
            cv.text(x, y, ch, fg, bg)
            x += char_w(ch)
        if cur == (row, e) and e == len(line):
            app.caret_cell = (x, y, " ")
            cv.text(x, y, " ", th.paper, th.ink)


# ---------------------------------------------------------------- chrome variants
APP_NAME = "Asahi-Quill"

LOGO_BLUE = (0x2E, 0x86, 0xFF)
LOGO_ORANGE = (0xFF, 0x8A, 0x3D)
LOGO_GREEN = (0x2F, 0xB5, 0x6A)
LOGO_YELLOW = (0xFF, 0xC8, 0x3D)
LOGO_INK = (0x12, 0x20, 0x33)


def draw_logo(cv, x, y):
    """The 8x4 mark: orange spine, white page, pink and green lines."""
    cv.fill(x + 1, y, 1, 4, (0xFF, 0x8A, 0x3D))     # spine
    cv.fill(x + 2, y, 5, 4, (0xFF, 0xFF, 0xFF))     # page
    cv.fill(x + 3, y + 1, 3, 1, (0xFF, 0x5D, 0x8F))  # pink line
    cv.fill(x + 3, y + 2, 3, 1, (0x2F, 0xB5, 0x6A))  # green line
    cv.fill(x + 3, y + 3, 2, 1, (0x12, 0x20, 0x33))  # ink line


def chrome_bevel(app, cv):
    th = app.th
    W, H = app.cols, app.rows

    cv.vgrad(0, 0, W * 2, 4, th.title)
    cv.hline(0, 3, W * 2, getattr(th, "title_rule", (0x0B, 0x33, 0x63)))
    draw_logo(cv, 1, 0)
    cv.text(6, 0, "Menu", th.title_text)
    app.hits.append(("menubar", 0, 0, 0, 11, 1))
    name = doc_title(app)
    cv.text((W - len(name)) // 2, 0, name, th.title_text)
    cv.text(W - 14, 0, time.strftime("%H:%M"), th.title_text)
    cv.text(W - 4, 0, "✕", (0xFF, 0xE0, 0xE0))
    app.hits.append(("close", None, W - 5, 0, W - 2, 0))

    # second row: the info line
    sub = getattr(th, "title_sub", (0xD8, 0xE6, 0xF8))
    path = app.doc.path or "new document"
    stats = "Ln %d, Col %d   %d words   %d chars   %s" % (
        app.cur[0] + 1, app.cur[1] + 1, app.counts[1], app.counts[0],
        "wrap" if app.wrap else "nowrap")
    start_x = 12
    room = W - len(stats) - 3 - start_x - 2
    if room > 8 and len(path) > room:
        path = "…" + path[-(room - 1):]
    if room > 0:
        cv.text(start_x, 1, path[:max(0, room)], sub)
    cv.text(W - len(stats) - 2, 1, stats, th.title_text)
    cv.hline(0, 4, W * 2, th.edge)

    cv.cfill(0, 2, W, 1, th.chrome)
    app.menu_pos = []
    x = 1
    for i, (label, _items) in enumerate(MENUS):
        w = len(label) + 2
        active = (app.menu_open == i)
        hover = app.hovering(x - 1, 2, x + w - 1, 2)
        cv.text(x, 2, label, th.accent_text if active else getattr(th, "menu_fg", th.ink),
                th.accent if active else (th.hi if hover else th.chrome))
        app.menu_pos.append((x, 3))
        app.hits.append(("menubar", i, x - 1, 2, x + w - 1, 2, MENU_TIPS.get(label, "")))
        x += w + 1
    cv.hline(0, 2 * 2 + 1, W * 2, th.lo)

    cv.cfill(0, 3, W, 3, th.chrome)
    bx2 = 1
    marks = getattr(th, "tools", None)
    for i, (label, action) in enumerate(TOOLBAR):
        w = len(label) + (5 if marks else 3)
        bevel_button(app, cv, bx2, 3, w, 3, label,
                     hit=("action", action), tip=TOOL_TIPS.get(action, ""),
                     mark=(marks[i] if marks else None),
                     base=(themes.tint(marks[i], 0.30) if marks else None),
                     label_color=(themes.shade(marks[i], 0.45) if marks else None))
        bx2 += w + 1
    st = "wrap" if app.wrap else "nowrap"
    cv.text(W - len(st) - 2, 4, st, th.muted, th.chrome)

    bar_bevel(app, cv)


def chrome_flat(app, cv):
    """NeXT / Plan 9: solid title bar, 1px rule, no gradients or shadows."""
    th = app.th
    W, H = app.cols, app.rows
    cv.cfill(0, 0, W, H, th.chrome)

    cv.cfill(0, 0, W, 1, th.chrome)
    cv.text(2, 0, "Start", th.ink, th.chrome)
    app.hits.append(("menubar", 0, 0, 0, 8, 0))
    name = doc_title(app)
    cv.text((W - len(name)) // 2, 0, name, th.title_text, th.chrome)
    cv.text(W - 7, 0, time.strftime("%H:%M"), th.ink, th.chrome)
    cv.hline(0, 1, W * 2, th.edge)          # 1px rule under the title bar

    cv.cfill(0, 1, W, 1, th.chrome)
    app.menu_pos = []
    x = 1
    for i, (label, _items) in enumerate(MENUS):
        w = len(label) + 1
        active = (app.menu_open == i)
        hover = app.hovering(x - 1, 1, x + w - 1, 1)
        cv.text(x, 1, label, th.accent_text if active else th.ink,
                th.accent if active else (th.hi if hover else th.chrome))
        app.menu_pos.append((x, 2))
        app.hits.append(("menubar", i, x - 1, 1, x + w - 1, 1, MENU_TIPS.get(label, "")))
        x += w + 2
    st = "wrap on" if app.wrap else "wrap off"
    cv.text(W - len(st) - 2, 1, st, th.muted, th.chrome)

    # toolbar: one row of text buttons; state shown by colour
    cv.cfill(0, 2, W, 1, th.chrome)
    x = 2
    for label, action in TOOLBAR:
        cv.text(x, 2, label, th.ink, th.chrome)
        app.hits.append(("action", action, x - 1, 2, x + len(label), 2))
        x += len(label) + 3

    bar_flat(app, cv)


def chrome_paper(app, cv):
    """WordPerfect 5.1: no title bar, one menu row and a bottom status line."""
    th = app.th
    W, H = app.cols, app.rows
    cv.cfill(0, 0, W, H, th.paper)

    cv.cfill(0, 0, W, 1, th.paper)
    app.menu_pos = []
    x = 1
    for i, (label, _items) in enumerate(MENUS):
        w = len(label) + 1
        active = (app.menu_open == i)
        hover = app.hovering(x - 1, 0, x + w - 1, 0)
        cv.text(x, 0, label, th.accent_text if active else th.accent,
                th.accent if active else (0xEC, 0xEC, 0xF8) if hover else th.paper)
        app.menu_pos.append((x, 1))
        app.hits.append(("menubar", i, x - 1, 0, x + w - 1, 0, MENU_TIPS.get(label, "")))
        x += w + 2
    name = doc_title(app)
    cv.text(W - len(name) - 2, 0, name, th.muted, th.paper)

    bar_flat(app, cv)


# ---------------------------------------------------------------- status bar
def bar_bevel(app, cv):
    th = app.th
    W, H = app.cols, app.rows
    by = H - 2
    cv.cfill(0, by, W, 2, th.chrome)
    cv.hline(0, by * 2, W * 2, th.hi)
    tools = getattr(th, "tools", None)
    newface = themes.tint(tools[0], 0.30) if tools else th.chrome
    cv.cfill(1, by, 10, 2, newface)
    cv.box(1, by, 10, 2, newface, raised=True)
    cv.text(3, by, "New", th.ink, newface)
    app.hits.append(("action", "new", 1, by, 10, by + 1))
    tip = hover_tip(app)
    if tip:
        cv.text(13, by, tip[:W - 30], th.tip_fg, th.tip_bg)
        cv.cfill(12, by, 1, 2, th.chrome)
    else:
        cv.text(13, by, status_text(app), th.bar_text, th.chrome)
    menuface = themes.tint(tools[1], 0.30) if tools and len(tools) > 1 else th.chrome
    cv.cfill(W - 11, by, 10, 2, menuface)
    cv.box(W - 11, by, 10, 2, menuface, raised=True)
    cv.text(W - 9, by, "Menu", th.ink, menuface)
    app.hits.append(("menubar", 0, W - 11, by, W - 2, by + 1))


def bar_flat(app, cv):
    th = app.th
    W, H = app.cols, app.rows
    by = H - 1
    cv.cfill(0, by, W, 1, th.bar)
    cv.text(1, by, status_text(app), th.bar_text, th.bar)
    keys = "F2 theme   Ctrl-S save   Ctrl-Q quit"
    cv.text(W - len(keys) - 1, by, keys, th.bar_text, th.bar)


def status_text(app):
    if app.status and time.time() < app.status_until:
        return app.status
    row, col = app.cur
    return "Ln %d  Col %d    %d chars" % (row + 1, col + 1, len(app.doc.text()))


def draw_menu(app, cv):
    th = app.th
    items = MENUS[app.menu_open][1]
    pos = app.menu_pos[app.menu_open] if app.menu_open < len(app.menu_pos) else (1, 3)
    x, y = pos
    width = max(len(it[0]) for it in items if it) + 4
    cv.cfill(x + 1, y + 1, width, len(items) + 2, (0x30, 0x30, 0x30))
    if th.kind == "bevel":
        cv.box(x, y, width, len(items) + 2, th.chrome, raised=True)
    else:
        cv.cfill(x, y, width, len(items) + 2, th.chrome)
        cv.hline(x * 2, y * 2, width * 2, th.edge)
        cv.hline(x * 2, (y + len(items) + 2) * 2 - 1, width * 2, th.edge)
        for yy in range(y, y + len(items) + 2):
            cv.text(x, yy, '│', th.edge, th.chrome)
            cv.text(x + width - 1, yy, '│', th.edge, th.chrome)
    sel = 0
    for k, it in enumerate(items):
        yy = y + 1 + k
        if it is None:
            cv.hline((x + 1) * 2, yy * 2 + 1, (width - 2) * 2, th.lo)
            continue
        active = (sel == app.menu_sel)
        bg = th.accent if active else th.chrome
        fg = th.accent_text if active else th.ink
        cv.cfill(x + 1, yy, width - 2, 1, bg)
        cv.text(x + 2, yy, it[0], fg, bg)
        app.hits.append(("menu", it[1], x, yy, x + width - 1, yy,
                         "%s ▸ %s" % (MENUS[app.menu_open][0], it[0])))
        sel += 1


def dialog_frame(app, cv, x, y, w, h, title):
    th = app.th
    if th.kind == "bevel":
        cv.cfill(x + 1, y + 1, w, h, (0x30, 0x30, 0x30))
        cv.box(x, y, w, h, th.chrome, raised=True)
        cv.vgrad(x * 2 + 1, y * 2, (w - 2) * 2, 4, th.title)
        cv.text(x + 2, y, " " + title, th.title_text)
        return y + 1
    cv.cfill(x + 1, y + 1, w, h, (0x30, 0x30, 0x30))
    cv.cfill(x, y, w, h, th.chrome)
    cv.hline(x * 2, y * 2, w * 2, th.edge)
    cv.hline(x * 2, (y + h) * 2 - 1, w * 2, th.edge)
    for yy in range(y, y + h):
        cv.text(x, yy, '│', th.edge, th.chrome)
        cv.text(x + w - 1, yy, '│', th.edge, th.chrome)
    cv.cfill(x + 1, y + 1, w - 2, 1, th.accent)
    cv.text(x + 2, y + 1, " " + title, th.accent_text, th.accent)
    return y + 2


def draw_dialog(app, cv):
    th = app.th
    d = app.dialog
    kind = d["kind"]
    W, H = app.cols, app.rows

    if kind == "open":
        w = min(64, W - 10)
        h = min(app.rows - 8, 18)
        x, y = (W - w) // 2, (H - h) // 2
        top = dialog_frame(app, cv, x, y, w, h, "Open")
        cv.text(x + 3, top, d["dir"][:w - 6], th.muted, th.chrome)
        list_y = top + 1
        rows = min(h - 6, app.dialog_list_rows())
        cv.sunken(x + 2, list_y, w - 4, rows, th.paper, thin=(th.kind != "bevel"))
        n = len(d["entries"])
        if d["sel"] < d["scroll"]:
            d["scroll"] = d["sel"]
        if d["sel"] >= d["scroll"] + rows:
            d["scroll"] = d["sel"] - rows + 1
        for i in range(rows):
            idx = d["scroll"] + i
            if idx >= n:
                break
            name, is_dir = d["entries"][idx]
            yy = list_y + i
            active = (idx == d["sel"])
            bg = th.accent if active else th.paper
            fg = th.accent_text if active else th.ink
            cv.cfill(x + 3, yy, w - 6, 1, bg)
            cv.text(x + 3, yy, ("[%s]" % name if is_dir else " " + name)[:w - 6], fg, bg)
            app.hits.append(("dialog_item", idx, x + 2, yy, x + w - 3, yy))
        buttons(app, cv, x, y, w, h, [("Open", "dialog_ok", None),
                                      ("Cancel", "dialog_cancel", None)])
        return

    if kind in ("saveas", "find"):
        w, h = min(56, W - 12), 8
        x, y = (W - w) // 2, (H - h) // 2
        top = dialog_frame(app, cv, x, y, w, h, "Save As" if kind == "saveas" else "Find")
        label = "File name:" if kind == "saveas" else "Find what:"
        cv.text(x + 3, top, label, th.ink, th.chrome)
        cv.sunken(x + 3, top + 1, w - 6, 2, th.paper, thin=(th.kind != "bevel"))
        cv.text(x + 4, top + 1, d["text"][:w - 8], th.ink, th.paper)
        buttons(app, cv, x, y, w, h, [("OK", "dialog_ok", None),
                                      ("Cancel", "dialog_cancel", None)])
        return

    w, h = min(58, W - 12), 12
    x, y = (W - w) // 2, (H - h) // 2
    top = dialog_frame(app, cv, x, y, w, h, "About" if kind == "about" else "Keys")
    if kind == "about":
        lines = ["%s 1.0" % APP_NAME, "a mouse-first terminal editor",
                 "", "click to place the caret, drag to select",
                 "Ctrl-S save   Ctrl-O open   Ctrl-F find",
                 "F2 cycles the theme"]
    else:
        lines = ["click / drag    caret and selection",
                 "wheel           scroll",
                 "Ctrl-C / V / X  copy, paste, cut",
                 "Ctrl-Z          undo",
                 "Ctrl-S / O      save, open",
                 "F2              theme"]
    for i, line in enumerate(lines):
        if top + i < y + h - 3:
            cv.text(x + 3, top + i, line[:w - 6], th.ink, th.chrome)
    buttons(app, cv, x, y, w, h, [("OK", "dialog_ok", None)])


def buttons(app, cv, x, y, w, h, items):
    th = app.th
    bx = x + w - 10
    for label, kind, payload in reversed(items):
        cv.box(bx, y + h - 3, 8, 2, th.chrome, raised=True)
        cv.text(bx + 1, y + h - 3, label, th.ink, th.chrome)
        app.hits.append((kind, payload, bx, y + h - 3, bx + 7, y + h - 2))
        bx -= 9



# ---------------------------------------------------------------- studio: card layout
def card(cv, x, y, w, h, bg, edge):
    """Rounded card: box-drawing characters, so the borders never break."""
    cv.cfill(x, y, w, h, bg)
    cv.text(x, y, '╭', edge, bg)
    cv.text(x + w - 1, y, '╮', edge, bg)
    cv.text(x, y + h - 1, '╰', edge, bg)
    cv.text(x + w - 1, y + h - 1, '╯', edge, bg)
    for i in range(1, w - 1):
        cv.text(x + i, y, '─', edge, bg)
        cv.text(x + i, y + h - 1, '─', edge, bg)
    for j in range(1, h - 1):
        cv.text(x, y + j, '│', edge, bg)
        cv.text(x + w - 1, y + j, '│', edge, bg)


def bevel_button(app, cv, x, y, w, h, label, hit=None, tip="", mark=None, base=None,
                 label_color=None):
    """Bevel button: light top-left, dark bottom-right, blue outline on hover, sunken when pressed."""
    th = app.th
    box = (x, y, x + w - 1, y + h - 1)
    hover = app.hovering(*box)
    pressed = (app.pressed == box)
    base = base or th.card_hi
    if pressed:
        face = themes.darken(base, 24)
        hi, lo = themes.darken(face, 12), themes.lighten(face, 18)
        fg = th.ink
    elif hover:
        face = themes.lighten(base, 22)
        hi = lo = th.accent
        fg = th.ink
    else:
        face = base
        hi, lo = themes.lighten(face, 22), themes.darken(face, 42)
        fg = label_color or th.ink
    cv.cfill(x, y, w, h, face)
    cv.hline(x * 2, y * 2, w * 2, hi)
    cv.hline(x * 2, (y + h) * 2 - 1, w * 2, lo)
    for yy in range(y, y + h):
        cv.text(x, yy, '▌', hi, face)
        cv.text(x + w - 1, yy, '▐', lo, face)
    if mark:
        cv.text(x + 2, y + h // 2, "▪", mark, face)
        cv.text(x + 4 + max(0, (w - 5 - len(label)) // 2), y + h // 2, label, fg, face)
    else:
        cv.text(x + max(1, (w - len(label)) // 2), y + h // 2, label, fg, face)
    if hit:
        app.hits.append(hit + box + (tip,))


def toolbar_studio(app, cv):
    th = app.th
    rw = app.rail_width()
    x = rw + 3
    y = 1
    for tool_name, action in TOOLBAR:
        w = len(tool_name) + 4
        if x + w > app.cols - 2:
            break
        bevel_button(app, cv, x, y, w, 3, tool_name,
                     hit=("action", action), tip=TOOL_TIPS.get(action, ""))
        x += w + 1


def headings(app):
    out = []
    for i, line in enumerate(app.doc.lines):
        text = line.strip()
        if text.startswith("#"):
            level = len(text) - len(text.lstrip("#"))
            out.append((i, level, text))
    return out


def chrome_studio(app, cv):
    th = app.th
    W, H = app.cols, app.rows
    cv.cfill(0, 0, W, H, th.bg)

    # breadcrumb
    cv.text(2, 0, APP_NAME, th.accent, th.bg)
    cv.text(14, 0, "›", th.dim, th.bg)
    cv.text(16, 0, doc_title(app), th.fg, th.bg)
    right = "Ctrl-S save    F2 theme"
    cv.text(max(20, W - len(right) - 2), 0, right, th.dim, th.bg)

    toolbar_studio(app, cv)

    rx, ry, rw = 1, 1, app.rail_width()
    rh = H - 2
    card(cv, rx, ry, rw, rh, th.card, th.edge)
    app.menu_pos = []
    bottom = ry + rh - 1
    y = ry + 1

    def section(title, gap=True, color=None):
        """Section label: small and tinted, clearly not a button."""
        nonlocal y
        if gap:
            y += 1
        if y >= bottom:
            return False
        cv.text(rx + 2, y, title, color or th.dim, th.card)
        y += 1
        return True

    def item(text, color=None, indent=0, hit=None, bullet="·", tip=""):
        """Clickable row: pale fill, blue on hover, solid when pressed."""
        nonlocal y
        if y >= bottom:
            return
        box = (rx + 1, y, rx + rw - 2, y)
        hover = app.hovering(*box)
        pressed = (app.pressed == box)
        if pressed:
            bg, fg, mark = th.accent, th.accent_text, "▸"
        elif hover:
            bg, fg, mark = th.accent_soft, th.ink, "▸"
        else:
            bg, fg, mark = th.card, (color or th.fg), bullet
        cv.cfill(rx + 1, y, rw - 2, 1, bg)
        cv.text(rx + 2 + indent, y, mark, th.cyan if (hover or pressed) else th.dim, bg)
        cv.text(rx + 4 + indent, y, text[:rw - 7 - indent], fg, bg)
        if hit:
            app.hits.append(hit + box + (tip,))
        y += 1

    section("FILE", gap=False, color=th.accent)
    name = os.path.basename(app.doc.path) if app.doc.path else "Untitled"
    cv.text(rx + 2, y, name[:rw - 4], th.amber if app.doc.modified else th.fg, th.card)
    y += 2


    y += 1
    if section("MENU", color=th.pink):
        for i, (label, _items) in enumerate(MENUS):
            item(label, hit=("menubar", i), tip=MENU_TIPS.get(label, ""))
            app.menu_pos.append((rx + rw + 1, y - 1))

    y += 1
    if section("STATS", color=th.green):
        row, col = app.cur
        cv.text(rx + 2, y, "Ln %d Col %d" % (row + 1, col + 1), th.fg, th.card)
        y += 1
        cv.text(rx + 2, y, "%d chars" % len(app.doc.text()), th.dim, th.card)
        y += 1

    if section("OUTLINE", color=th.amber):
        lines = headings(app)
        if not lines:
            cv.text(rx + 2, y, "(no headings)", th.dim, th.card)
        for lineno, level, text in lines:
            if y >= bottom:
                break
            color = (th.pink, th.cyan, th.dim)[min(level, 3) - 1]
            label_txt = text.lstrip("#").strip() or text
            width = rw - 7 - (level - 1) * 2
            if len(label_txt) > width:
                label_txt = label_txt[:width - 1] + "…"
            item(label_txt, color=color, indent=(level - 1) * 2,
                 hit=("outline", lineno), bullet="›",
                 tip="Go to line %d" % (lineno + 1))


def chips(app, cv):
    th = app.th
    y = app.rows - 1
    x = 0

    def pill(text, bg, fg):
        nonlocal x
        w = str_w(text) + 2
        if x + w > app.cols:
            return
        cv.cfill(x, y, w, 1, bg)
        cv.wtext(x + 1, y, text, fg, bg)
        x += w + 1

    row, col = app.cur
    pill("Ln %d  Col %d" % (row + 1, col + 1), (0xEF, 0xEA, 0xE1), th.fg)
    pill("%d chars" % len(app.doc.text()), th.accent, th.accent_text)
    pill("wrap on" if app.wrap else "wrap off",
         (0xE3, 0xF1, 0xE6) if app.wrap else (0xF6, 0xE4, 0xE2),
         th.green if app.wrap else th.red)

    tip = hover_tip(app)
    if tip:
        w = str_w(tip) + 2
        if w <= app.cols - x - 1:
            cv.cfill(app.cols - w, y, w, 1, getattr(th, "tip_bg", th.accent_soft))
            cv.wtext(app.cols - w + 1, y, tip, getattr(th, "tip_fg", th.ink),
                     getattr(th, "tip_bg", th.accent_soft))
    elif app.status and time.time() < app.status_until:
        pill(app.status, th.card_hi, th.amber)


def hover_tip(app):
    h = app.hover
    if not h:
        return ""
    for hit in reversed(app.hits):
        kind, payload, x0, y0, x1, y1 = hit[:6]
        if x0 <= h[0] <= x1 and y0 <= h[1] <= y1:
            return hit[6] if len(hit) > 6 else ""
    return ""


# ---------------------------------------------------------------- main loop
def run(app):
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    out = sys.stdout

    def cleanup():
        out.write("\x1b[?1000l\x1b[?1006l\x1b[?25h\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def bye(*_a):
        raise SystemExit(0)

    signal.signal(signal.SIGWINCH, lambda *a: None)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, bye)
        except (ValueError, OSError):
            pass

    try:
        tty.setraw(fd)
        out.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h")
        out.flush()
        dirty = True
        last_blink = time.time()
        while not app.quit:
            if dirty:
                size = shutil.get_terminal_size()
                app.cols, app.rows = size.columns, size.lines
                app.ensure_visible()
                out.write("\x1b[H" + render(app).to_ansi() + "\x1b[J")
                out.flush()
                dirty = False
                last_blink = time.time()

            r, _, _ = select.select([fd], [], [], 0.15)
            if r:
                data = os.read(fd, 4096)
                if not data:
                    break
                events, _ = T.parse_events(data)
                for ev in events:
                    if ev[0] == "key":
                        dirty |= app.key(ev[1])
                    else:
                        _, btn, x, y = ev
                        if btn & 32:                       # motion / drag
                            if (x, y) != app.hover:
                                app.hover = (x, y)
                                dirty = True
                            dirty |= app.drag_to(x, y)
                        elif btn == 64:
                            dirty |= app.wheel(True)
                        elif btn == 65:
                            dirty |= app.wheel(False)
                        elif (btn & 3) == 0:
                            dirty |= app.click(x, y)
            else:
                if app.status and time.time() > app.status_until:
                    app.status = ""
                    dirty = True
                if app.pressed and time.time() > app.pressed_until:
                    app.pressed = None
                    dirty = True
                elif time.time() - last_blink > 0.55 and app.caret_cell:
                    cx, cy, ch = app.caret_cell
                    if 0 <= cx < app.cols and 0 <= cy < app.rows and char_w(ch) == 1:
                        fg, bg = (T.WHITE, T.INK) if app.caret_on else (T.INK, T.WHITE)
                        out.write("\x1b[%d;%dH\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm%s\x1b[0m"
                                  % ((cy + 1, cx + 1) + tuple(fg) + tuple(bg) + (ch,)))
                        out.flush()
                        app.caret_on = not app.caret_on
                    last_blink = time.time()
    finally:
        cleanup()


def main():
    args = sys.argv[1:]
    opts, positional = {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--png", "--cols", "--rows", "--theme") and i + 1 < len(args):
            opts[a] = args[i + 1]
            i += 2
            continue
        if a.startswith("--"):
            opts[a] = True
            i += 1
            continue
        positional.append(a)
        i += 1

    path = positional[0] if positional else None

    if "--png" in opts:
        cols = int(opts.get("--cols", 100))
        rows = int(opts.get("--rows", 32))
        app = App(cols, rows, path, demo=(path is None), theme=opts.get("--theme"))
        if "--dialog" in opts:
            app.open_dialog("open")
        if "--select" in opts:
            app.anchor = [2, 0]
            app.cur = [4, 12]
        T.to_png(render(app), opts["--png"])
        print(opts["--png"])
        return

    size = shutil.get_terminal_size()
    demo = (path is None) and ("--empty" not in opts)
    app = App(size.columns, size.lines, path, demo=demo,
              theme=opts.get("--theme"))
    run(app)


if __name__ == "__main__":
    main()
