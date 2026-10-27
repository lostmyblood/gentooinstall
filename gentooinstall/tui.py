"""A small curses (stdlib-only) TUI toolkit: lists, text prompts, pager, dialogs."""
from __future__ import annotations

import curses
import locale
import os
import textwrap
from typing import Callable, Optional, Sequence

KEY_ENTER = (10, 13, curses.KEY_ENTER)
KEY_BACK = (curses.KEY_BACKSPACE, 127, 8)

# Palette: pure black background, white text, muted steel-blue accents only.
C_BASE, C_TITLE, C_ERR, C_OK, C_WARN, C_SEL = 1, 2, 3, 4, 5, 6
MUTED_BLUE_256 = 67   # #5f87af



class Tui:
    def __init__(self) -> None:
        self.s = None

    def __enter__(self) -> "Tui":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- lifecycle
    def open(self) -> None:
        os.environ.setdefault("ESCDELAY", "25")
        locale.setlocale(locale.LC_ALL, "")
        self.s = curses.initscr()
        curses.noecho()
        curses.cbreak()
        self.s.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            black, white = curses.COLOR_BLACK, curses.COLOR_WHITE
            blue = MUTED_BLUE_256 if curses.COLORS >= 256 else curses.COLOR_BLUE
            curses.init_pair(C_BASE, white, black)
            curses.init_pair(C_TITLE, blue, black)
            curses.init_pair(C_ERR, white, black)    # errors: bold white + "!" marker (no red)
            curses.init_pair(C_OK, blue, black)
            curses.init_pair(C_WARN, blue, black)
            curses.init_pair(C_SEL, black, blue)     # selected row / header bar
            self.s.bkgd(" ", curses.color_pair(C_BASE))

    def close(self) -> None:
        if self.s is not None:
            try:
                self.s.keypad(False)
                curses.nocbreak()
                curses.echo()
                curses.endwin()
            except curses.error:
                pass
            self.s = None

    def suspend_run(self, argv: list[str]) -> int:
        """Leave curses, run an interactive program (cfdisk, a shell), come back."""
        import subprocess
        self.close()
        try:
            return subprocess.call(argv)
        finally:
            self.open()

    # ---------------------------------------------------------------- drawing
    @staticmethod
    def color(n: int) -> int:
        if not curses.has_colors():
            return curses.A_REVERSE if n == C_SEL else 0
        return curses.color_pair(n)

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x >= w - 1:
            return
        try:
            self.s.addnstr(y, x, text, max(0, w - x - 1), attr)
        except curses.error:
            pass

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        out: list[str] = []
        for para in text.split("\n"):
            out += textwrap.wrap(para, max(10, width)) or [""]
        return out

    def _frame(self, title: str, prompt: str = "") -> tuple[int, int, int]:
        self.s.erase()
        h, w = self.s.getmaxyx()
        self._put(0, 0, " gentooinstall".ljust(w), self.color(C_SEL))
        self._put(2, 2, title, curses.A_BOLD | self.color(C_TITLE))
        y = 4
        if prompt:
            for line in self._wrap(prompt, w - 6):
                self._put(y, 2, line)
                y += 1
            y += 1
        return h, w, y

    def _footer(self, h: int, text: str) -> None:
        self._put(h - 1, 2, text, self.color(C_TITLE))

    # ---------------------------------------------------------------- list chooser
    def choose(self, title: str, options: Sequence[tuple], *, prompt: str = "",
               current=None, start=None, multi: bool = False, selected=(),
               filterable: bool = True, help_text: str = ""):
        """Pick from [(value, label), ...]. Returns value (or list if multi); None on Esc."""
        opts = list(options)
        sel = set(selected)
        flt, filtering, cur, top = "", False, 0, 0
        for i, (v, _) in enumerate(opts):
            if v == (start if start is not None else current):
                cur = i
        while True:
            view = [o for o in opts if flt.lower() in o[1].lower()] if flt else opts
            cur = max(0, min(cur, len(view) - 1)) if view else 0
            h, w, y0 = self._frame(title, prompt)
            rows = max(1, h - y0 - 3)
            if cur < top:
                top = cur
            if cur >= top + rows:
                top = cur - rows + 1
            for i in range(rows):
                idx = top + i
                if idx >= len(view):
                    break
                val, label = view[idx]
                if multi:
                    mark = "[x] " if val in sel else "[ ] "
                else:
                    mark = "(*) " if (current is not None and val == current) else "    "
                attr = self.color(C_SEL) if idx == cur else self.color(C_BASE)
                self._put(y0 + i, 2, (mark + label).ljust(w - 5), attr)
            if len(view) > rows:
                self._put(y0 - 1, w - 14, f"{cur + 1}/{len(view)}", self.color(C_WARN))
            if flt or filtering:
                self._put(h - 2, 2, f"filter: {flt}" + ("_" if filtering else ""), self.color(C_WARN))
            self._footer(h, help_text or (
                "Up/Down move  Space toggle  Enter confirm  / filter  Esc cancel" if multi
                else "Up/Down move  Enter select  / filter  Esc back"))
            self.s.refresh()
            ch = self.s.getch()
            if filtering:
                if ch in KEY_ENTER:
                    filtering = False
                elif ch == 27:
                    flt, filtering = "", False
                elif ch in KEY_BACK:
                    flt = flt[:-1]
                elif 32 <= ch < 127:
                    flt += chr(ch)
                cur = top = 0
                continue
            if ch in (curses.KEY_UP, ord("k")):
                cur -= 1
            elif ch in (curses.KEY_DOWN, ord("j")):
                cur += 1
            elif ch == curses.KEY_PPAGE:
                cur -= rows
            elif ch == curses.KEY_NPAGE:
                cur += rows
            elif ch == curses.KEY_HOME:
                cur = 0
            elif ch == curses.KEY_END:
                cur = len(view) - 1
            elif ch == ord("/") and filterable and len(opts) > 12:
                filtering = True
            elif ch == ord(" ") and multi and view:
                v = view[cur][0]
                sel.symmetric_difference_update({v})
            elif ch in KEY_ENTER:
                if multi:
                    return [v for v, _ in opts if v in sel]
                if view:
                    return view[cur][0]
            elif ch in (27, ord("q")):
                return None

    def confirm(self, title: str, text: str = "", default: bool = False) -> bool:
        r = self.choose(title, [("y", "Yes"), ("n", "No")], prompt=text,
                        start="y" if default else "n", filterable=False,
                        help_text="Up/Down move  Enter select  Esc = No")
        return r == "y"

    # ---------------------------------------------------------------- text input
    def ask(self, title: str, prompt: str = "", default: str = "", *, secret: bool = False,
            validator: Optional[Callable[[str], str]] = None) -> Optional[str]:
        """Single-line input. validator returns an error string ('' = ok). Esc returns None."""
        buf, pos, err = list(default), len(default), ""
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                h, w, y0 = self._frame(title, prompt)
                width = max(10, w - 8)
                text = ("*" * len(buf)) if secret else "".join(buf)
                off = max(0, pos - width + 1)
                self._put(y0, 2, "> " + text[off:off + width])
                if err:
                    self._put(y0 + 2, 2, "! " + err, self.color(C_ERR) | curses.A_BOLD)
                self._footer(h, "Enter confirm  Esc cancel")
                self.s.move(min(h - 1, y0), min(w - 2, 4 + pos - off))
                self.s.refresh()
                try:
                    ch = self.s.get_wch()
                except curses.error:
                    continue
                if isinstance(ch, str):
                    if ch in ("\n", "\r"):
                        value = "".join(buf)
                        err = validator(value) if validator else ""
                        if not err:
                            return value
                    elif ch == "\x1b":
                        return None
                    elif ch in ("\x7f", "\b"):
                        if pos:
                            del buf[pos - 1]
                            pos -= 1
                    elif ch.isprintable():
                        buf.insert(pos, ch)
                        pos += 1
                else:
                    if ch in KEY_ENTER:
                        value = "".join(buf)
                        err = validator(value) if validator else ""
                        if not err:
                            return value
                    elif ch in KEY_BACK and pos:
                        del buf[pos - 1]
                        pos -= 1
                    elif ch == curses.KEY_DC and pos < len(buf):
                        del buf[pos]
                    elif ch == curses.KEY_LEFT:
                        pos = max(0, pos - 1)
                    elif ch == curses.KEY_RIGHT:
                        pos = min(len(buf), pos + 1)
                    elif ch == curses.KEY_HOME:
                        pos = 0
                    elif ch == curses.KEY_END:
                        pos = len(buf)
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def ask_password(self, title: str, prompt: str = "") -> Optional[str]:
        """Ask twice; returns None if cancelled."""
        while True:
            a = self.ask(title, prompt or "Enter password:", secret=True,
                         validator=lambda v: "" if v else "Password cannot be empty")
            if a is None:
                return None
            b = self.ask(title, "Repeat the password:", secret=True)
            if b is None:
                return None
            if a == b:
                return a
            self.message("Passwords differ", "The two entries did not match. Try again.", error=True)

    # ---------------------------------------------------------------- dialogs
    def message(self, title: str, text: str, error: bool = False) -> None:
        h, w, y = self._frame(title)
        attr = (self.color(C_ERR) | curses.A_BOLD) if error else 0
        for line in self._wrap(text, w - 6):
            self._put(y, 2, line, attr)
            y += 1
        self._footer(h, "Press any key")
        self.s.refresh()
        self.s.getch()

    def pager(self, title: str, text: str, footer: str = "Enter = continue   Esc = go back") -> bool:
        """Scrollable read-only text. Returns True on Enter, False on Esc/q."""
        top = 0
        while True:
            h, w, y0 = self._frame(title)
            lines = self._wrap(text, w - 6)
            rows = max(1, h - y0 - 2)
            top = max(0, min(top, max(0, len(lines) - rows)))
            for i, line in enumerate(lines[top:top + rows]):
                self._put(y0 + i, 2, line)
            self._footer(h, footer + ("   Up/Down scroll" if len(lines) > rows else ""))
            self.s.refresh()
            ch = self.s.getch()
            if ch in (curses.KEY_UP, ord("k")):
                top -= 1
            elif ch in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif ch == curses.KEY_PPAGE:
                top -= rows
            elif ch == curses.KEY_NPAGE:
                top += rows
            elif ch in KEY_ENTER:
                return True
            elif ch in (27, ord("q")):
                return False
