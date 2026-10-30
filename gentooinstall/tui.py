"""Curses (stdlib-only) TUI that reproduces archinstall's look.

Colours and layout are taken from archinstall's Textual CSS (archinstall/tui/components.py):

* background transparent (terminal default), text white
* app header: white bold on blue, full width, centred
* highlighted row: white bold on blue
* Input: gray (dim) border, blue border while focused
* messages: red = error, yellow = warning, green = info
* footer: key chips white on black
* list on the left, vertical rule, preview pane on the right
Only the 8 basic ANSI colours are used, so the terminal's own palette applies (like Textual's ansi_color mode).
"""
from __future__ import annotations

import curses
import locale
import os
import textwrap
from typing import Callable, Optional, Sequence

KEY_ENTER = (10, 13, curses.KEY_ENTER)
KEY_BACK = (curses.KEY_BACKSPACE, 127, 8)

(C_BASE, C_HEADER, C_SEL, C_ERR, C_WARN, C_OK, C_FOOT, C_BLUE) = range(1, 9)

APP_TITLE = "Gentoo Installer"


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
            try:
                curses.use_default_colors()       # transparent background, like ansi_color=True
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            W, B, K = curses.COLOR_WHITE, curses.COLOR_BLUE, curses.COLOR_BLACK
            curses.init_pair(C_BASE, W, bg)
            curses.init_pair(C_HEADER, W, B)      # .app-header
            curses.init_pair(C_SEL, W, B)         # option-list--option-highlighted
            curses.init_pair(C_ERR, curses.COLOR_RED, bg)
            curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
            curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
            curses.init_pair(C_FOOT, W, K)        # .footer-key--key / --description
            curses.init_pair(C_BLUE, B, bg)       # focused border

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

    # ---------------------------------------------------------------- drawing primitives
    @staticmethod
    def color(n: int) -> int:
        if not curses.has_colors():
            return curses.A_REVERSE if n in (C_SEL, C_HEADER) else 0
        return curses.color_pair(n)

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x >= w - 1 or x < 0:
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

    def _header(self, title: str, prompt: str = "") -> int:
        """App header bar + centred header text. Returns the next free row."""
        self.s.erase()
        h, w = self.s.getmaxyx()
        bar = self.color(C_HEADER) | curses.A_BOLD
        self._put(0, 0, " " * (w - 1), bar)
        self._put(0, max(0, (w - len(APP_TITLE)) // 2), APP_TITLE, bar)
        pad = 1 if h < 34 else 2                       # .header-text padding
        y = 1 + pad
        text = title if not prompt else f"{title}\n\n{prompt}"
        for line in self._wrap(text, min(w - 8, 100)):
            self._put(y, max(0, (w - len(line)) // 2), line)
            y += 1
        return y + (1 if pad == 1 else pad)

    def _footer(self, items: Sequence[tuple[str, str]]) -> None:
        h, w = self.s.getmaxyx()
        x = 1
        chip = self.color(C_FOOT)
        for key, desc in items:
            self._put(h - 1, x, f" {key} ", chip | curses.A_BOLD)
            x += len(key) + 2
            self._put(h - 1, x, f"{desc}  ", chip)
            x += len(desc) + 2 + 1

    def _box(self, y: int, x: int, bh: int, bw: int, attr: int) -> None:
        self._put(y, x, "┌" + "─" * (bw - 2) + "┐", attr)
        for i in range(1, bh - 1):
            self._put(y + i, x, "│", attr)
            self._put(y + i, x + bw - 1, "│", attr)
        self._put(y + bh - 1, x, "└" + "─" * (bw - 2) + "┘", attr)

    # ---------------------------------------------------------------- list chooser
    def choose(self, title: str, options: Sequence[tuple], *, prompt: str = "",
               current=None, start=None, multi: bool = False, selected=(),
               filterable: bool = True, help_text: str = "",
               preview: Optional[Callable[[object], Optional[str]]] = None,
               preview_header: str = "", col_header: str = ""):
        """Pick from [(value, label), ...]. Returns value (list if multi); None on Esc.

        `preview(value)` -> text shown in a right-hand pane (archinstall's preview_location='right').
        `col_header` is a table header line shown above the rows (used for partition tables).
        """
        opts = list(options)
        sel = set(selected)
        flt, filtering, cur, top = "", False, 0, 0
        for i, (v, _) in enumerate(opts):
            if v == (start if start is not None else current):
                cur = i
        while True:
            view = [o for o in opts if flt.lower() in o[1].lower()] if flt else opts
            cur = max(0, min(cur, len(view) - 1)) if view else 0
            h, w = self.s.getmaxyx()
            y0 = self._header(title, prompt)
            if col_header:
                y0 += 0
            rows = max(1, h - y0 - 3 - (2 if col_header else 0))
            marker = 4 if multi else 0
            lw = max([len(lab) for _, lab in view] + [len(col_header), 12]) + marker + 2
            if preview:
                lw = min(lw, max(24, w // 2 - 4))
                lx = 2
            else:
                lw = min(lw, w - 4)
                lx = max(1, (w - lw) // 2)
            ry = y0
            if col_header:
                self._put(ry, lx + 1 + marker, col_header[: lw - marker - 1], curses.A_BOLD)
                self._put(ry + 1, lx, "─" * lw)
                ry += 2
            if cur < top:
                top = cur
            if cur >= top + rows:
                top = cur - rows + 1
            for i in range(rows):
                idx = top + i
                if idx >= len(view):
                    break
                val, label = view[idx]
                mark = ("[x] " if val in sel else "[ ] ") if multi else ""
                if not multi and current is not None and val == current and not preview:
                    label = label  # current value is simply where the cursor starts
                text = (" " + mark + label)[: lw - 1].ljust(lw)
                attr = (self.color(C_SEL) | curses.A_BOLD) if idx == cur else self.color(C_BASE)
                self._put(ry + i, lx, text, attr)
            if len(view) > rows:                      # 1-col scrollbar, white on black
                for i in range(rows):
                    thumb = top * rows // len(view) <= i <= (top + rows) * rows // len(view)
                    self._put(ry + i, lx + lw, "█" if thumb else " ", self.color(C_FOOT))
            if preview:
                rx = lx + lw + 2
                for i in range(ry - (2 if col_header else 0), h - 3):
                    self._put(i, rx - 1, "│", self.color(C_BASE) | curses.A_DIM)
                py = y0
                if preview_header:
                    self._put(py, rx + 1, preview_header, curses.A_BOLD)
                    py += 2
                text = preview(view[cur][0]) if view else None
                for line in (text or "").split("\n"):
                    if py >= h - 2:
                        break
                    self._put(py, rx + 1, line[: max(0, w - rx - 3)])
                    py += 1
            if flt or filtering:
                self._put(h - 2, 1, "/" + flt + ("▏" if filtering else ""), self.color(C_BLUE) | curses.A_BOLD)
            foot = [("esc", "Cancel"), ("↓", "Down"), ("↑", "Up")]
            if filterable and len(opts) > 12:
                foot.append(("/", "Search"))
            foot.append(("space", "Toggle") if multi else ("enter", "Select"))
            if multi:
                foot.append(("enter", "Confirm"))
            if help_text:
                foot = [("", help_text)]
            self._footer(foot)
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
                sel.symmetric_difference_update({view[cur][0]})
            elif ch in KEY_ENTER:
                if multi:
                    return [v for v, _ in opts if v in sel]
                if view:
                    return view[cur][0]
            elif ch in (27, ord("q")):
                return None

    def confirm(self, title: str, text: str = "", default: bool = False) -> bool:
        r = self.choose(title, [("y", "Yes"), ("n", "No")], prompt=text,
                        start="y" if default else "n", filterable=False)
        return r == "y"

    # ---------------------------------------------------------------- text input
    def ask(self, title: str, prompt: str = "", default: str = "", *, secret: bool = False,
            validator: Optional[Callable[[str], str]] = None) -> Optional[str]:
        """Bordered single-line input (border turns blue while focused). Esc returns None."""
        buf, pos, err = list(default), len(default), ""
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                h, w = self.s.getmaxyx()
                y0 = self._header(title, prompt)
                bw = min(60, w - 4)
                bx = (w - bw) // 2
                by = max(y0 + 1, min(h - 6, h // 2 - 1))
                self._box(by, bx, 3, bw, self.color(C_BLUE))
                width = bw - 4
                text = ("*" * len(buf)) if secret else "".join(buf)
                off = max(0, pos - width + 1)
                self._put(by + 1, bx + 2, text[off:off + width])
                if err:
                    self._put(by + 3, max(0, (w - len(err)) // 2), err, self.color(C_ERR))
                self._footer([("esc", "Cancel"), ("enter", "Confirm")])
                self.s.move(min(h - 1, by + 1), min(w - 2, bx + 2 + pos - off))
                self.s.refresh()
                try:
                    ch = self.s.get_wch()
                except curses.error:
                    continue
                submit = False
                if isinstance(ch, str):
                    if ch in ("\n", "\r"):
                        submit = True
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
                        submit = True
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
                if submit:
                    value = "".join(buf)
                    err = validator(value) if validator else ""
                    if not err:
                        return value
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def ask_password(self, title: str, prompt: str = "") -> Optional[str]:
        """Ask twice; returns None if cancelled."""
        while True:
            a = self.ask(title, prompt or "Enter password", secret=True,
                         validator=lambda v: "" if v else "Password cannot be empty")
            if a is None:
                return None
            b = self.ask(title, "Repeat the password", secret=True)
            if b is None:
                return None
            if a == b:
                return a
            self.message("Passwords differ", "The two entries did not match. Try again.", level="error")

    # ---------------------------------------------------------------- dialogs
    def message(self, title: str, text: str, error: bool = False, level: str = "") -> None:
        """level: '', 'info' (green), 'warning' (yellow), 'error' (red)."""
        level = "error" if error and not level else level
        attr = {"error": self.color(C_ERR), "warning": self.color(C_WARN),
                "info": self.color(C_OK)}.get(level, self.color(C_BASE))
        h, w = self.s.getmaxyx()
        y = self._header(title)
        for line in self._wrap(text, min(w - 8, 100)):
            self._put(y, max(0, (w - len(line)) // 2), line, attr)
            y += 1
        self._footer([("any key", "Continue")])
        self.s.refresh()
        self.s.getch()

    def pager(self, title: str, text: str, footer: str = "Install") -> bool:
        """Scrollable read-only text. Returns True on Enter, False on Esc/q."""
        top = 0
        while True:
            h, w = self.s.getmaxyx()
            y0 = self._header(title)
            lines = text.split("\n")
            rows = max(1, h - y0 - 2)
            top = max(0, min(top, max(0, len(lines) - rows)))
            for i, line in enumerate(lines[top:top + rows]):
                self._put(y0 + i, 3, line)
            self._footer([("esc", "Back"), ("↓", "Down"), ("↑", "Up"), ("enter", footer)])
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
