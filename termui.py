#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
termui.py -- the shared terminal canvas

One cell equals two sub-pixels: U+2580 paints the top half with the foreground
colour and the bottom half with the background, which gives us 1px bevels,

gradient title bars and sunken fields. Output uses absolute cursor addressing
(CSI r;1H) instead of newlines: in raw mode \n does not return the carriage.
"""

import re
import sys
import unicodedata
import time

# ---------------------------------------------------------------- palette
FACE      = (0xC3, 0xC3, 0xC3)
FACE_HI   = (0xF5, 0xF5, 0xF5)
FACE_LO   = (0x7A, 0x7A, 0x7A)
WHITE     = (0xFF, 0xFF, 0xFF)
INK       = (0x10, 0x10, 0x10)
MUTED     = (0x5A, 0x5A, 0x5A)
NAVY      = (0x0A, 0x24, 0x6A)
SEL_BG    = (0x1C, 0x4E, 0x96)
DESK      = (0x4A, 0x52, 0x60)
DESK_DOT  = (0x55, 0x5E, 0x6D)
PAPER     = (0xFF, 0xFF, 0xFF)
LINE      = (0xB4, 0xB4, 0xB4)
RULE      = (0xC8, 0xC8, 0xC8)

TITLE_TOP = (0x6E, 0xA8, 0xDC)
TITLE_MID = (0x2F, 0x77, 0xBE)
TITLE_BOT = (0x14, 0x4A, 0x8C)


class Canvas:
    """A canvas of character cells, each split into two sub-pixels."""

    def __init__(self, cols, rows, bg=DESK):
        self.cols, self.rows = cols, rows
        self.px = [[bg] * (cols * 2) for _ in range(rows * 2)]
        self.ch = [[None] * cols for _ in range(rows)]

    # ---------- sub-pixels ----------
    def fill(self, x, y, w, h, c):
        for yy in range(max(0, y), min(self.rows * 2, y + h)):
            row = self.px[yy]
            for xx in range(max(0, x), min(self.cols * 2, x + w)):
                row[xx] = c

    def hline(self, x, y, w, c):
        self.fill(x, y, w, 1, c)

    def vline(self, x, y, h, c):
        self.fill(x, y, 1, h, c)

    # ---------- cells ----------
    def cfill(self, x, y, w, h, c):
        self.fill(x * 2, y * 2, w * 2, h * 2, c)
        for yy in range(max(0, y), min(self.rows, y + h)):
            row = self.ch[yy]
            for xx in range(max(0, x), min(self.cols, x + w)):
                row[xx] = None

    def text(self, cx, cy, s, fg=INK, bg=None):
        for i, ch in enumerate(s):
            x = cx + i
            if 0 <= x < self.cols and 0 <= cy < self.rows:
                self.ch[cy][x] = (ch, fg, bg if bg is not None else self.px[cy * 2 + 1][x * 2])

    def wtext(self, cx, cy, s, fg=INK, bg=None):
        """Write a string by display width: wide characters take two cells."""
        x = cx
        for ch in s:
            if 0 <= x < self.cols and 0 <= cy < self.rows:
                self.ch[cy][x] = (ch, fg, bg if bg is not None else self.px[cy * 2 + 1][x * 2])
            x += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

    def box(self, x, y, w, h, face=FACE, raised=True, fill=True, thin=False):
        if fill:
            self.cfill(x, y, w, h, face)
        hi, lo = (FACE_HI, FACE_LO) if raised else (FACE_LO, FACE_HI)
        self.hline(x * 2, y * 2, w * 2, hi)
        self.hline(x * 2, (y + h) * 2 - 1, w * 2, lo)
        lch, rch = ('│', '│') if thin else ('▌', '▐')
        for yy in range(y, y + h):
            self.text(x, yy, lch, hi, face)
            self.text(x + w - 1, yy, rch, lo, face)

    def sunken(self, x, y, w, h, face=WHITE, thin=False):
        self.cfill(x, y, w, h, face)
        self.hline(x * 2, y * 2, w * 2, FACE_LO)
        self.hline(x * 2, (y + h) * 2 - 1, w * 2, FACE_HI)
        lch, rch = ('│', '│') if thin else ('▌', '▐')
        for yy in range(y, y + h):
            self.text(x, yy, lch, FACE_LO, face)
            self.text(x + w - 1, yy, rch, FACE_HI, face)

    def vgrad(self, x, y, w, h, stops):
        n = len(stops) - 1
        for i in range(h):
            t = i / max(1, h - 1) * n
            k = min(int(t), n - 1)
            f = t - k
            a, b = stops[k], stops[k + 1]
            self.hline(x, y + i, w, tuple(int(a[j] + (b[j] - a[j]) * f) for j in range(3)))

    # ---------- output ----------
    def cells(self):
        for cy in range(self.rows):
            for cx in range(self.cols):
                item = self.ch[cy][cx]
                top = self.px[cy * 2][cx * 2]
                bot = self.px[cy * 2 + 1][cx * 2]
                if item:
                    yield item
                elif top == bot:
                    yield ' ', top, bot
                else:
                    yield '▀', top, bot

    def to_ansi(self):
        """Row-by-row absolute addressing; the cell after a wide character is skipped."""
        cells = list(self.cells())
        out = []
        for row in range(self.rows):
            out.append('\x1b[%d;1H' % (row + 1))
            last = None
            i = 0
            while i < self.cols:
                ch, fg, bg = cells[row * self.cols + i]
                if last != (fg, bg):
                    out.append('\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm' % (fg + bg))
                    last = (fg, bg)
                out.append(ch)
                i += 2 if (ch and unicodedata.east_asian_width(ch) in ("W", "F")) else 1
            out.append('\x1b[0m')
        return ''.join(out)

    def cell_ansi(self, x, y):
        """Emit a single cell (used by the caret blink to avoid full repaints)."""
        ch, fg, bg = list(self.cells())[y * self.cols + x]
        return '\x1b[%d;%dH\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm%s\x1b[0m' % (
            (y + 1, x + 1) + tuple(fg) + tuple(bg) + (ch,))


# ---------------------------------------------------------------- input
MOUSE_RE = re.compile(rb'\x1b\[<(\d+);(\d+);(\d+)([Mm])')
CSI_RE = re.compile(rb'\x1b\[[0-9;]*[A-Za-z~]|\x1bO[A-Za-z]')


def parse_events(buf):
    """Split a byte stream into ("key", s) / ("mouse", btn, x, y) events."""
    events = []
    i = 0
    while i < len(buf):
        b = buf[i:i + 1]
        if b == b'\x1b':
            m = MOUSE_RE.match(buf, i)
            if m:
                btn, x, y, kind = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
                if kind == b'M':
                    events.append(("mouse", btn, x - 1, y - 1))
                i = m.end()
                continue
            m = CSI_RE.match(buf, i)
            if m:
                events.append(("key", m.group(0).decode("latin-1")))
                i = m.end()
                continue
            if len(buf) - i == 1:
                events.append(("key", "\x1b"))
                i += 1
                continue
            i += 1
            continue
        if b in (b'\r', b'\n'):
            events.append(("key", "\r"))
            i += 1
            continue
        try:
            ch = buf[i:i + 4].decode("utf-8")
            events.append(("key", ch))
            i += len(ch.encode("utf-8"))
        except UnicodeDecodeError:
            i += 1
    return events, b""


# ---------------------------------------------------------------- PNG preview
def to_png(cv, path, cw=9, ch=18):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise SystemExit("--png needs Pillow (pip install pillow); "
                         "normal runs do not")
    img = Image.new("RGB", (cv.cols * cw, cv.rows * ch), DESK)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", int(ch * 0.80), index=0)
    cells = list(cv.cells())
    for cy in range(cv.rows):
        for cx in range(cv.cols):
            chx, fg, bg = cells[cy * cv.cols + cx]
            px, py = cx * cw, cy * ch
            if chx == ' ':
                d.rectangle([px, py, px + cw - 1, py + ch - 1], fill=bg)
            elif chx == '▀':
                d.rectangle([px, py, px + cw - 1, py + ch // 2 - 1], fill=fg)
                d.rectangle([px, py + ch // 2, px + cw - 1, py + ch - 1], fill=bg)
            else:
                d.rectangle([px, py, px + cw - 1, py + ch - 1], fill=bg)
                d.text((px + cw / 2, py + ch / 2 - 1), chx, font=font, fill=fg, anchor="mm")
    img.save(path)
    return path
