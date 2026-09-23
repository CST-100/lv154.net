#!/usr/bin/env python3
"""lv154 TUI editor: source on the left, rendered preview on the right.

Usage:
  python3 tools/tui.py [options] [FILE | new SLUG | page SLUG]
  make tui [FILE=posts/2026-05-17-hello.txt]
  lv [FILE | new SLUG | page SLUG]      after --install (symlinks ~/.local/bin/lv)

Options:
  --keys ctrl|helix         key style (config default, else ctrl)
  --preview site|terminal   preview pane colours (config default, else site)
  --editor terminal|site    editor pane colours (config default, else terminal)
  --config PATH             config file (default $LV154_TUI_CONFIG or
                            ~/.config/lv154/tui.json)
  --init-config             write a starter config file and exit
  --man                     print the cheat sheet (usage, markup, keys) and exit
  --install                 symlink this script as ~/.local/bin/lv and exit

Publishing: F2 (or :publish [message]) saves, commits ONLY src/pages and
src/posts, and pushes main; the server pulls main every few minutes. It
fetches first and refuses when origin/main has commits this clone lacks;
a failed push leaves the commit local and :publish retries it.
F3 (or :status) shows pending content changes. Unrelated changes elsewhere
in the repo are never swept into a publish commit.

Drafts: `new SLUG` (^N, :new) starts a post as src/drafts/<slug>.txt, which
is never built, published or committed (src/drafts is gitignored). :post
[YYYY-MM-DD] moves it into src/posts dated today, ready to :publish; :unpost
moves a post back. The picker's p key does the same for the highlighted file.

Long lines: anything wider than 64 visible columns soft-wraps in both panes
at word boundaries, exactly where the build wraps it for the site; the
continuation lines start at column 0. Lines that fit are never touched.
:post and :fmt [all] write the wrap into the file.

Clipboard: space-y (helix), ^Y (ctrl) or :copy push the selection (ctrl keys:
the current line) to the system clipboard through whichever of clip.exe,
pbcopy, wl-copy or xclip is on PATH, and yank it too.

Config is JSON:
  {
    "keys": "helix",
    "theme": {"preview": "site", "editor": "terminal"},
    "palette": {"fg": "#e0e0d8", "bg": "#070707", "green": "#40e080"}
  }
Palette names: fg bg dim green hdr tb tp tw link warn err overbg. "site"
theming paints a pane with the site's fg/bg; "terminal" leaves your
terminal's defaults (so the editor can be your usual orange-on-whatever
while the preview stays faithful to the site).

Stdlib only (curses). Mirrors tools/edit.html: left pane is the source with
DSL markup highlighted in place and a gutter of visible widths (yellow at
60, red past 64); right pane is the rendered preview at 64 columns with a
ruler; footer has cursor position, the current line's visible width and an
overrun summary. Width logic is imported from build.py so the editor and the
build agree on what "64 columns" means.

Press F1 (or :help [about|markup|ctrl|helix] in helix mode) for the cheat
sheet; `--man` prints the same text.
"""
from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import build  # noqa: E402
from edit import PAGE_NAME_RE, POST_NAME_RE  # noqa: E402

SRC = ROOT / "src"
PAGES = SRC / "pages"
POSTS = SRC / "posts"
DRAFTS = SRC / "drafts"     # posts in progress: not built, not published, gitignored

MAX_COLS = build.MAX_COLS
NEAR_COLS = MAX_COLS - 4
GUTTER_W = 8            # "NNN WWW "
PREVIEW_W = 70          # 64 cols + ruler + a few cells of overflow
MIN_EDIT_W = 48         # narrower than this and we stop splitting
UNDO_DEPTH = 200
GROUP_SECS = 1.0        # ctrl mode: keystrokes closer than this share one undo step

POST_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PAGE_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
# a draft is <slug>.txt; a dated name (a post moved in by hand) is fine too
DRAFT_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-)?[a-z0-9][a-z0-9_-]*\.txt$")

POST_SEED = "Untitled post\n\n"


def page_seed(slug: str) -> str:
    return (f"// {slug}\n\n"
            f'{{d}}add "{slug}" to config.json pages + nav, then delete this.{{/d}}\n')


# ---- config -----------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "keys": "ctrl",
    "theme": {"preview": "site", "editor": "terminal"},
    "palette": {},
}
STARTER_CONFIG = {
    "_help": "keys: ctrl|helix. theme.*: site|terminal. palette: #rrggbb overrides "
             "for fg bg dim green hdr tb tp tw link warn err overbg.",
    "keys": "ctrl",
    "theme": {"preview": "site", "editor": "terminal"},
    "palette": {},
}


def default_config_path() -> Path:
    env = os.environ.get("LV154_TUI_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "lv154" / "tui.json"


def load_config(path: Path | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = path or default_config_path()
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            sys.exit(f"error: bad config {p}: {e}")
        if not isinstance(user, dict):
            sys.exit(f"error: bad config {p}: expected an object")
        if isinstance(user.get("keys"), str):
            cfg["keys"] = user["keys"]
        if isinstance(user.get("theme"), dict):
            cfg["theme"].update({k: v for k, v in user["theme"].items() if isinstance(v, str)})
        if isinstance(user.get("palette"), dict):
            cfg["palette"].update({k: v for k, v in user["palette"].items() if isinstance(v, str)})
    return cfg


def validate_config(cfg: dict) -> None:
    if cfg["keys"] not in ("ctrl", "helix"):
        sys.exit(f"error: keys must be ctrl or helix, not {cfg['keys']!r}")
    for pane in ("preview", "editor"):
        if cfg["theme"].get(pane) not in ("site", "terminal"):
            sys.exit(f"error: theme.{pane} must be site or terminal")
    for name, val in cfg["palette"].items():
        if name not in PALETTE:
            sys.exit(f"error: unknown palette name {name!r} (want one of {' '.join(PALETTE)})")
        if parse_hex(val) is None:
            sys.exit(f"error: palette.{name} must be #rrggbb, not {val!r}")


def parse_hex(s: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", s.strip())
    if not m:
        return None
    v = int(m.group(1), 16)
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


# ---- styles -----------------------------------------------------------------

NORM, TAG, G, D, HDR, TB, TP, TW, LINK = range(9)
SPAN_STYLE = {"g": G, "d": D, "hdr": HDR}
TRANS_STYLE = {"tb": TB, "tp": TP, "tw": TW}
STYLE_COLOR: dict[int, str | None] = {
    NORM: None, TAG: "dim", G: "green", D: "dim", HDR: "hdr",
    TB: "tb", TP: "tp", TW: "tw", LINK: "link",
}

# (r, g, b) from template.html / edit.html. Order matters: it fixes which
# palette slot each colour lands in when we redefine slots CUSTOM_BASE.. .
PALETTE: dict[str, tuple[int, int, int]] = {
    "fg":     (0xe0, 0xe0, 0xd8),
    "bg":     (0x07, 0x07, 0x07),
    "dim":    (0x50, 0x58, 0x48),
    "green":  (0x40, 0xe0, 0x80),
    "hdr":    (0x58, 0xd8, 0x58),
    "tb":     (0x55, 0xcd, 0xfc),
    "tp":     (0xf7, 0xa8, 0xb8),
    "tw":     (0xff, 0xff, 0xff),
    "link":   (0x58, 0xb8, 0xd8),
    "warn":   (0xc8, 0xa8, 0x48),
    "err":    (0xe0, 0x50, 0x50),
    "overbg": (0x48, 0x18, 0x18),
}
FALLBACK8 = {
    "fg": -1, "bg": -1, "dim": -1, "green": 2, "hdr": 2, "tb": 6, "tp": 5,
    "tw": 7, "link": 6, "warn": 3, "err": 1, "overbg": 1,
}
CUSTOM_BASE = 16        # palette slots we redefine when the terminal allows


def nearest256(rgb: tuple[int, int, int]) -> int:
    """Closest xterm-256 index to an RGB triple (cube or grey ramp)."""
    steps = [0, 95, 135, 175, 215, 255]

    def q(v: int) -> int:
        return min(range(6), key=lambda i: abs(steps[i] - v))

    qr, qg, qb = (q(v) for v in rgb)
    cube_idx = 16 + 36 * qr + 6 * qg + qb
    cube = (steps[qr], steps[qg], steps[qb])
    gi = max(0, min(23, round((sum(rgb) / 3 - 8) / 10)))
    grey = 8 + gi * 10
    grey_idx = 232 + gi

    def dist(c: tuple[int, int, int]) -> int:
        return sum((a - b) ** 2 for a, b in zip(c, rgb))

    return cube_idx if dist(cube) <= dist((grey, grey, grey)) else grey_idx


class Colors:
    """Lazily allocated (fg name, bg name) -> curses attr."""

    def __init__(self, overrides: dict[str, str]) -> None:
        self.ok = curses.has_colors()
        self.custom = False
        self.idx: dict[str, int] = {}
        self.extra: dict[str, int] = {}
        self._pairs: dict[tuple[str | None, str | None], int] = {}
        self._next = 1
        if not self.ok:
            return
        curses.start_color()
        curses.use_default_colors()
        many = curses.COLORS >= 256
        self.custom = many and curses.can_change_color()
        palette = dict(PALETTE)
        for name, hexval in overrides.items():
            rgb = parse_hex(hexval)
            if rgb:
                palette[name] = rgb
        for n, (name, rgb) in enumerate(palette.items()):
            if self.custom:
                c = CUSTOM_BASE + n
                try:
                    curses.init_color(c, *(v * 1000 // 255 for v in rgb))
                except curses.error:
                    self.custom = False
                    c = nearest256(rgb)
            elif many:
                c = nearest256(rgb)
            else:
                c = FALLBACK8[name]
            self.idx[name] = c
        if not many:
            self.extra["dim"] = curses.A_DIM

    def pair(self, fg: str | None, bg: str | None) -> int:
        if not self.ok:
            return 0
        key = (fg, bg)
        if key not in self._pairs:
            if self._next >= curses.COLOR_PAIRS:
                return 0
            curses.init_pair(self._next, self.idx.get(fg, -1), self.idx.get(bg, -1))
            self._pairs[key] = curses.color_pair(self._next) | self.extra.get(fg, 0)
            self._next += 1
        return self._pairs[key]

    def c(self, name: str | None) -> int:
        return self.pair(name, None)

    def attr(self, style: int, site: bool, over: bool = False) -> int:
        """Attr for a DSL style inside a pane that is site- or terminal-themed."""
        if not self.ok:
            return curses.A_REVERSE if over else 0
        fg = STYLE_COLOR[style] or ("fg" if site else None)
        bg = "overbg" if over else ("bg" if site else None)
        return self.pair(fg, bg)

    def fill(self, site: bool) -> int:
        return self.pair("fg", "bg") if (site and self.ok) else 0


# ---- source scanning --------------------------------------------------------

def scan(text: str) -> tuple[list[int], list[bool]]:
    """Per-character style id, and whether the character is markup (a tag)."""
    style = [NORM] * len(text)
    tag = [False] * len(text)

    def mark_tags(a: int, b: int) -> None:
        for i in range(a, b):
            tag[i] = True

    for m in build.SPAN_RE.finditer(text):
        a, b = m.span()
        ia, ib = m.span(2)
        mark_tags(a, ia)
        mark_tags(ib, b)
        st = SPAN_STYLE[m.group(1)]
        for i in range(ia, ib):
            style[i] = st
    for m in build.TRANS_RE.finditer(text):
        a, b = m.span()
        ia, ib = m.span(1)
        mark_tags(a, ia)
        mark_tags(ib, b)
        for k, i in enumerate(range(ia, ib)):
            style[i] = TRANS_STYLE[build.TRANS_CYCLE[k % len(build.TRANS_CYCLE)]]
    for m in build.LINK_RE.finditer(text):
        a, b = m.span()
        ia, ib = m.span(1)
        mark_tags(a, ia)
        mark_tags(ib, b)
        for i in range(ia, ib):
            style[i] = LINK
    return style, tag


def display_char(ch: str) -> str:
    if ch == "\t":
        return "→"
    if ch < " " or ch == "\x7f":
        return "·"
    return ch


def scan_lines(text: str) -> list[list[tuple[str, int, bool, int]]]:
    """Split `text` into lines of (display char, style, is_tag, cell width)."""
    style, tag = scan(text)
    lines: list[list[tuple[str, int, bool, int]]] = []
    cur: list[tuple[str, int, bool, int]] = []
    for i, ch in enumerate(text):
        if ch == "\n":
            lines.append(cur)
            cur = []
            continue
        dch = display_char(ch)
        cur.append((dch, style[i], tag[i], build.char_width(dch)))
    lines.append(cur)
    return lines


# ---- word motions (on the flat text; newline counts as whitespace) ----------

def cls(ch: str) -> int:
    if ch.isspace():
        return 0
    return 1 if (ch.isalnum() or ch == "_") else 2


def _advance(text: str, i: int) -> int:
    """Index of the next word start at or after `i`."""
    n = len(text)
    j = i
    if j < n and not text[j].isspace():
        c = cls(text[j])
        while j < n and cls(text[j]) == c:
            j += 1
    while j < n and text[j].isspace():
        j += 1
    return j


def w_motion(text: str, i: int) -> int:
    """helix `w`: head lands just before the next word start."""
    n = len(text)
    if n == 0:
        return 0
    j = _advance(text, i)
    if j - 1 <= i and j < n:
        j = _advance(text, i + 1)
    return max(i, min(j - 1, n - 1))


def e_motion(text: str, i: int) -> int:
    """helix `e`: head lands on the last char of the current/next word."""
    n = len(text)
    if n == 0:
        return 0
    j = i + 1
    while j < n and text[j].isspace():
        j += 1
    if j >= n:
        return n - 1
    c = cls(text[j])
    while j + 1 < n and cls(text[j + 1]) == c:
        j += 1
    return j


def b_motion(text: str, i: int) -> int:
    """helix `b`: head lands on the start of the current/previous word."""
    if i <= 0:
        return 0
    j = i - 1
    while j > 0 and text[j].isspace():
        j -= 1
    c = cls(text[j])
    while j > 0 and cls(text[j - 1]) == c:
        j -= 1
    return j


# ---- keys -------------------------------------------------------------------

SPECIAL = {
    curses.KEY_UP: "up", curses.KEY_DOWN: "down",
    curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
    curses.KEY_HOME: "home", curses.KEY_END: "end",
    curses.KEY_PPAGE: "pgup", curses.KEY_NPAGE: "pgdn",
    curses.KEY_BACKSPACE: "backspace", curses.KEY_DC: "delete",
    curses.KEY_ENTER: "enter", curses.KEY_RESIZE: "resize",
    curses.KEY_F1: "f1", curses.KEY_F2: "f2", curses.KEY_F3: "f3",
    curses.KEY_IC: "insert",
}

# Sequences ncurses hands back raw (ESC + these) because they're not in the
# terminfo entry for the current TERM. Terminals disagree on Home/End/PgUp
# encodings, so we map the common ones ourselves.
ESC_SEQ = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left",
    "OA": "up", "OB": "down", "OC": "right", "OD": "left",
    "[H": "home", "[F": "end", "OH": "home", "OF": "end",
    "[1~": "home", "[4~": "end", "[7~": "home", "[8~": "end",
    "[5~": "pgup", "[6~": "pgdn", "[3~": "delete", "[2~": "insert",
    "[1;5C": "ctrl-right", "[1;5D": "ctrl-left",
    "[1;3C": "ctrl-right", "[1;3D": "ctrl-left",
    "[1;5H": "home", "[1;5F": "end",
    "OP": "f1", "[11~": "f1", "OQ": "f2", "[12~": "f2", "OR": "f3", "[13~": "f3",
}
NAMED = {
    "kLFT5": "ctrl-left", "kRIT5": "ctrl-right",
    "kLFT3": "ctrl-left", "kRIT3": "ctrl-right",
    "kHOM5": "home", "kEND5": "end",
}


def keyname(k) -> str | None:
    if k is None:
        return None
    if isinstance(k, int):
        if k in SPECIAL:
            return SPECIAL[k]
        try:
            return NAMED.get(curses.keyname(k).decode(), f"key{k}")
        except (ValueError, UnicodeDecodeError):
            return f"key{k}"
    if k in ("\n", "\r"):
        return "enter"
    if k in ("\x7f", "\x08"):
        return "backspace"
    if k == "\x1b":
        return "esc"
    if k == "\t":
        return "tab"
    if k == " ":
        return "space"
    if len(k) == 1 and ord(k) < 32:
        return "^" + chr(ord(k) + 64)
    return k


CLIPBOARD_TOOLS = (
    ["clip.exe"],                          # WSL -> the Windows clipboard
    ["pbcopy"],                            # macOS
    ["wl-copy"],                           # Wayland
    ["xclip", "-selection", "clipboard"],  # X11
)


def clipboard_copy(text: str) -> str | None:
    """Push `text` to the system clipboard; returns the tool used, or None.

    Tries each CLIPBOARD_TOOLS entry that is on PATH and moves on when one
    fails (xclip without a DISPLAY, say). Output goes to /dev/null rather
    than a pipe so tools that fork and linger to serve the selection can't
    hold us up.
    """
    for cmd in CLIPBOARD_TOOLS:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5)
            return cmd[0]
        except (OSError, subprocess.SubprocessError):
            continue
    return None


# Cheat sheet. Shown by F1 / :help [topic] inside the editor, printed by --man.
# Keep lines <= 78 columns so the overlay fits.
HELP: list[tuple[str, str, list[str]]] = [
    ("about", "using the editor", [
        "lv154 tui edits src/pages, src/drafts and src/posts (*.txt) for the site.",
        "",
        "left pane   the source. markup is highlighted in place. the gutter shows",
        "            the line number and the line's VISIBLE width once markup is",
        "            stripped: yellow at 60+, red past 64.",
        "right pane  the rendered preview at 64 columns. ruler at column 65;",
        "            anything past it gets a red background.",
        "footer      cursor line/col, current line's visible (and source) width,",
        "            overrun summary, and status messages.",
        "",
        "files       pages/<slug>.txt  drafts/<slug>.txt  posts/<YYYY-MM-DD>-<slug>.txt",
        "            posts: line 1 = title, line 2 blank, then the body.",
        "            new files get a seed; a page also needs a config.json entry.",
        "drafts      ^N (or :new SLUG, lv new SLUG) starts a post in src/drafts/.",
        "            drafts are saved locally only: never built, published or",
        "            committed (the dir is gitignored). :post [YYYY-MM-DD] moves",
        "            one into src/posts dated today; then publish. :unpost moves",
        "            a post back. in the picker, p does either for the highlight.",
        "long lines  a line wider than 64 wraps at word boundaries in the preview,",
        "            on the site (the build wraps it) and into the file with :post",
        "            or :fmt; continuation lines start at column 0. lines that fit",
        "            are never touched, so a paragraph can stay one long line while",
        "            you draft. the editor soft-wraps at its own width; j/k move by",
        "            screen row.",
        "publish     F2 (or :publish [message]) saves, commits ONLY src/pages and",
        "            src/posts, then pushes main. the server pulls main every few",
        "            minutes; nothing else to do. F3 (or :status) shows what's",
        "            pending. other changes in the repo are never swept in.",
        "            it fetches first and refuses if origin has moved (pull in a",
        "            shell, then retry). if a push fails the commit stays local;",
        "            :publish again retries the push.",
        "picker      ^O (or space-f in helix mode): enter open, n new draft,",
        "            N new page, p post / unpost, d delete (asks first), esc close.",
        "",
        "always      ^S save   ^O files   ^N new draft   ^P toggle preview",
        "            ^Q quit   F1 help   F2 publish   F3 git status",
        "",
        "anywhere    python3 tools/tui.py --install  symlinks ~/.local/bin/lv, then:",
        "              lv                 picker        lv posts/<file>.txt   open",
        "              lv new SLUG        new draft     lv page SLUG          page",
        "",
        "config      ~/.config/lv154/tui.json  (or $LV154_TUI_CONFIG)",
        "              keys: ctrl | helix",
        "              theme.preview / theme.editor: site | terminal",
        "              palette: {\"green\": \"#40e080\", ...}",
        "            --init-config writes a starter. flags override the file:",
        "            --keys --preview --editor --config.  --man prints this sheet.",
    ]),
    ("markup", "markup tokens", [
        "{g}text{/g}          green          {d}text{/d}          dim grey",
        "{hdr}text{/hdr}      header green   {trans}word{/trans}  trans-flag cycle",
        "[text](url)          link           {systems}            live systems block",
        "// heading  ...  {d}01{/d}          section heading + number (home page)",
        "",
        "markup costs zero visible columns; trust the gutter, not the raw length.",
        "link text may wrap across lines; keep the (url) on the last line of it.",
        "text is HTML-escaped, so < > & are safe to type as-is.",
    ]),
    ("ctrl", "ctrl keys", [
        "^S save        ^O files        ^N new draft    ^P toggle preview",
        "^F find        ^G find next    ^Z undo         ^R redo         ^Q quit",
        "F1 help        F2 publish      F3 git status   ^Y copy line to clipboard",
        "",
        "arrows / home / end / pgup / pgdn move.  ^A ^E = line start / end.",
        "ctrl-left / ctrl-right jump by word.  tab inserts two spaces.",
        "undo groups quick consecutive typing; a paste is one undo step.",
        "long lines soft-wrap in both panes; :post (picker p) hard-wraps them at 64.",
        "to use helix keys instead: --keys helix, or \"keys\": \"helix\" in the config.",
    ]),
    ("helix", "helix keys", [
        "MODES    NOR normal (keys are commands)   INS insert (keys type)",
        "         SEL select (motions extend the selection instead of moving it)",
        "         esc  -> normal, collapses the selection, clears a pending key",
        "",
        "MOVE     h j k l / arrows   one char or line (no wrap at line ends)",
        "         w    select from cursor to just before the NEXT word start",
        "         e    select from cursor to the END of this / the next word",
        "         b    select from cursor BACK to this / the previous word start",
        "         f<c> t<c>   select to / to just before <c> on this line",
        "         F<c> T<c>   same, backwards",
        "         gg   file start (3gg = line 3)      ge   file end",
        "         gh   line start    gl  line end     gs   first non-blank",
        "         ^u ^d   half page up / down         ^b ^f  page up / down",
        "         / <regex>   search (selects the match)   n N  next / previous",
        "",
        "SELECT   x    extend to the whole line; again = one more line (3x = three)",
        "         %    select everything          ;    collapse to the cursor",
        "         v    toggle select mode",
        "         a lone cursor IS a one-char selection: d y r ~ act on that char",
        "",
        "INSERT   i    insert before the selection    a    append after it",
        "         I    insert at first non-blank      A    append at line end",
        "         o O  open a line below / above (keeps indent) and insert",
        "         in INS: ^w delete word back   ^u delete to line start",
        "",
        "EDIT     d    delete the selection (it goes to the yank register)",
        "         c    change: delete, then insert",
        "         y    yank      p P   paste after / before the selection",
        "              (a yanked whole line pastes as its own line)",
        "         space y   yank AND copy to the system clipboard",
        "         R    replace the selection with the yanked text",
        "         r<c> replace every selected char with <c>",
        "         u U  undo / redo: one step per command or per insert session",
        "         J    join with the next line(s)     > <  indent / dedent 2",
        "         ~    swap case",
        "",
        "OTHER    space f   file picker      :   command line",
        "         a count prefixes most keys: 5j  2w  3x  4u",
        "",
        "COMMANDS :w  :q  :q!  :wq        :e FILE   :o (picker)",
        "         :new SLUG (a draft)   :page SLUG   :NUMBER goto line   :p preview",
        "         :post [YYYY-MM-DD]  draft -> src/posts     :unpost  post -> draft",
        "         :fmt [all]  hard-wrap the selected lines (all: whole file) at 64",
        "         :publish [message]       :status (git, content only)",
        "         :keys ctrl|helix         :help [about|markup|ctrl|helix]",
        "         :copy  selection -> system clipboard (same as space y)",
    ]),
]
HELP_TOPICS = {sid: i for i, (sid, _t, _b) in enumerate(HELP)}
HELP_TOPICS.update({"dsl": HELP_TOPICS["markup"], "keys": HELP_TOPICS["helix"],
                    "usage": HELP_TOPICS["about"]})


def man_text() -> str:
    out = []
    for _sid, title, body in HELP:
        out.append(title.upper())
        out.append("-" * len(title))
        out.extend(body)
        out.append("")
    return "\n".join(out)


# ---- files ------------------------------------------------------------------

def validate_path(p: Path) -> Path:
    """Resolve `p` and require it to be src/{pages,drafts}/<slug>.txt or src/posts/<date>-<slug>.txt."""
    p = p.resolve()
    try:
        rel = p.relative_to(SRC.resolve())
    except ValueError:
        raise ValueError(f"{p} is not under src/")
    if len(rel.parts) != 2:
        raise ValueError("expected src/pages/, src/drafts/ or src/posts/<file>.txt")
    bucket, name = rel.parts
    if bucket == "pages":
        if not PAGE_NAME_RE.match(name):
            raise ValueError("page name must be lowercase slug + .txt")
    elif bucket == "posts":
        if not POST_NAME_RE.match(name):
            raise ValueError("post name must be YYYY-MM-DD-slug.txt")
    elif bucket == "drafts":
        if not DRAFT_NAME_RE.match(name):
            raise ValueError("draft name must be slug.txt")
    else:
        raise ValueError("only src/pages, src/drafts and src/posts are editable")
    return p


def resolve_arg(arg: str) -> Path:
    p = Path(arg)
    if not p.is_absolute():
        for cand in (Path.cwd() / p, ROOT / p, SRC / p):
            if cand.exists():
                p = cand
                break
        else:
            p = SRC / p if len(p.parts) == 2 else ROOT / p
    return validate_path(p)


def relpath(p: Path | None) -> str:
    if p is None:
        return "no file"
    try:
        return str(p.resolve().relative_to(SRC.resolve()))
    except ValueError:
        return str(p)


def list_entries() -> list[Path]:
    PAGES.mkdir(parents=True, exist_ok=True)
    POSTS.mkdir(parents=True, exist_ok=True)
    DRAFTS.mkdir(parents=True, exist_ok=True)
    return (sorted(PAGES.glob("*.txt")) + sorted(DRAFTS.glob("*.txt"))
            + sorted(POSTS.glob("*.txt"), reverse=True))


# ---- git --------------------------------------------------------------------

CONTENT_PATHS = ["src/pages", "src/posts"]
DEPLOY_BRANCH = "main"


def git(*args: str, timeout: int = 60) -> tuple[int, str]:
    """Run git in the repo. Never prompts: a push needing a passphrase fails fast."""
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                           text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "git not found"
    except subprocess.TimeoutExpired:
        return 124, f"git {' '.join(args)} timed out after {timeout}s"
    return r.returncode, (r.stdout + r.stderr).strip()


def install_symlink(name: str = "lv") -> None:
    target = Path(__file__).resolve()
    bindir = Path.home() / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    link = bindir / name
    if link.is_symlink():
        if link.resolve() == target:
            print(f"{link} already points at {target}")
        else:
            link.unlink()
            link.symlink_to(target)
            print(f"re-pointed {link} -> {target}")
    elif link.exists():
        sys.exit(f"{link} exists and is not a symlink; remove it first")
    else:
        link.symlink_to(target)
        print(f"linked {link} -> {target}")
    try:
        os.chmod(target, os.stat(target).st_mode | 0o111)
    except OSError:
        pass
    if str(bindir) not in os.environ.get("PATH", "").split(":"):
        print(f"note: {bindir} is not on PATH in this shell. log out and back in "
              f"(ubuntu's .profile adds it), or: export PATH=\"{bindir}:$PATH\"")


# ---- editor -----------------------------------------------------------------

Pos = tuple


class Editor:
    def __init__(self, scr, cfg: dict) -> None:
        self.scr = scr
        self.cfg = cfg
        self.keys = cfg["keys"]
        self.site_editor = cfg["theme"]["editor"] == "site"
        self.site_preview = cfg["theme"]["preview"] == "site"
        self.colors = Colors(cfg["palette"])
        self.lines: list[str] = [""]
        self.path: Path | None = None
        self.saved_text: str | None = ""
        self.cy = self.cx = 0
        self.want_x: int | None = None
        self.scroll_y = self.scroll_row = 0   # top line, and which of its rows is on top
        self.body_h = 1
        self.wrap_w = 10 ** 9       # editor wrap width; draw() sets it from the layout
        self.wrap_mode = "edit"     # split | edit | preview, also from draw()
        self._lc_key: tuple | None = None
        self._lc: tuple = ([], [], {})
        self.undo_stack: list[tuple[list[str], int, int]] = []
        self.redo_stack: list[tuple[list[str], int, int]] = []
        self.last_edit: tuple[str | None, float] = (None, 0.0)
        self.in_burst = False
        self.burst_snapped = False
        self.last_raw = None
        self.show_preview = True    # split when the terminal is wide enough
        self.preview_solo = False   # narrow terminals: preview replaces editor
        self.msg = ""
        self.msg_until = 0.0
        self.running = True
        # helix-mode state
        self.mode = "normal"        # normal | insert | select
        self.anchor = (0, 0)        # selection anchor; head is (cy, cx)
        self.pending = ""           # prefix key waiting for its argument
        self.count = ""
        self.register = ""
        self.in_insert = False
        self.insert_snapped = False
        self.search_pat = ""

    # -- buffer ---------------------------------------------------------------

    def text(self) -> str:
        return "\n".join(self.lines)

    def set_text(self, t: str) -> None:
        self.lines = t.split("\n")

    @property
    def dirty(self) -> bool:
        return self.saved_text is None or self.text() != self.saved_text

    def open_path(self, path: Path, seed: str | None = None) -> None:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            self.saved_text = text
        else:
            if seed is None:
                seed = POST_SEED if path.parent in (POSTS, DRAFTS) else page_seed(path.stem)
            text = seed
            self.saved_text = None
        self.set_text(text)
        self.path = path
        self.reset_view()

    def close_file(self) -> None:
        self.lines = [""]
        self.path = None
        self.saved_text = ""
        self.reset_view()

    def reset_view(self) -> None:
        self.cy = self.cx = 0
        self.want_x = None
        self.scroll_y = self.scroll_row = 0
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.last_edit = (None, 0.0)
        self.mode = "normal"
        self.in_insert = False
        self.pending = self.count = ""
        self.collapse()

    def save(self) -> bool:
        if self.path is None:
            self.flash("no file open  (^N new draft, ^O files)")
            return False
        data = self.text()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
        self.saved_text = data
        self.flash(f"saved {len(data.encode('utf-8'))} bytes")
        return True

    def flash(self, msg: str, secs: float = 3.0) -> None:
        self.msg = msg
        self.msg_until = time.time() + secs

    # -- undo -----------------------------------------------------------------

    def snapshot(self, kind: str, group: bool = True) -> None:
        """Push an undo checkpoint before an edit.

        ctrl mode groups quick consecutive edits of the same kind; a paste
        burst is one step. helix mode makes one step per command and one per
        insert-mode session.
        """
        if self.in_insert:
            if self.insert_snapped:
                return
            self.insert_snapped = True
            group = False
        elif self.in_burst:
            if self.burst_snapped:
                return
            self.burst_snapped = True
        now = time.time()
        last_kind, last_t = self.last_edit
        self.last_edit = (kind, now)
        if group and kind == last_kind and now - last_t < GROUP_SECS and self.undo_stack:
            return
        self.undo_stack.append((self.lines[:], self.cy, self.cx))
        del self.undo_stack[:-UNDO_DEPTH]
        self.redo_stack.clear()

    def reset_group(self) -> None:
        self.last_edit = (None, 0.0)

    def undo(self) -> None:
        if not self.undo_stack:
            self.flash("nothing to undo")
            return
        self.redo_stack.append((self.lines[:], self.cy, self.cx))
        self.lines, self.cy, self.cx = self.undo_stack.pop()
        self.lines = self.lines[:]
        self.clamp_cursor()
        self.collapse()
        self.reset_group()

    def redo(self) -> None:
        if not self.redo_stack:
            self.flash("nothing to redo")
            return
        self.undo_stack.append((self.lines[:], self.cy, self.cx))
        self.lines, self.cy, self.cx = self.redo_stack.pop()
        self.lines = self.lines[:]
        self.clamp_cursor()
        self.collapse()
        self.reset_group()

    # -- positions / selection ------------------------------------------------

    def clamp_cursor(self) -> None:
        self.cy = max(0, min(self.cy, len(self.lines) - 1))
        self.cx = max(0, min(self.cx, len(self.lines[self.cy])))
        self.want_x = None

    def off(self, y: int, x: int) -> int:
        return sum(len(l) + 1 for l in self.lines[:y]) + x

    def pos(self, off: int) -> tuple[int, int]:
        for y, l in enumerate(self.lines):
            if off <= len(l):
                return (y, off)
            off -= len(l) + 1
        return (len(self.lines) - 1, len(self.lines[-1]))

    def head(self) -> tuple[int, int]:
        return (self.cy, self.cx)

    def collapse(self) -> None:
        self.anchor = self.head()

    def has_selection(self) -> bool:
        return self.anchor != self.head()

    def sel_range(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Inclusive (lo, hi); a collapsed selection is the char under the cursor."""
        a, h = self.anchor, self.head()
        ay = max(0, min(a[0], len(self.lines) - 1))
        a = (ay, max(0, min(a[1], len(self.lines[ay]))))
        return (a, h) if a <= h else (h, a)

    def sel_text(self) -> str:
        lo, hi = self.sel_range()
        text = self.text()
        a = self.off(*lo)
        b = min(len(text), self.off(*hi) + 1)
        return text[a:b]

    def delete_sel(self) -> str:
        """Delete the selection (min. one char) and return the deleted text."""
        lo, hi = self.sel_range()
        text = self.text()
        a = self.off(*lo)
        b = self.off(*hi) + 1
        if b > len(text):
            # the selection ran through the virtual newline at EOF; if it was
            # a whole line, take the newline before it so the line vanishes
            b = len(text)
            if lo[1] == 0 and a > 0 and text[a - 1] == "\n":
                a -= 1
        deleted = text[a:b]
        self.set_text(text[:a] + text[b:])
        self.cy, self.cx = self.pos(a)
        self.want_x = None
        self.collapse()
        return deleted

    def insert_text(self, off: int, s: str, select: bool = False) -> None:
        text = self.text()
        off = max(0, min(off, len(text)))
        self.set_text(text[:off] + s + text[off:])
        if select and s:
            self.anchor = self.pos(off)
            self.cy, self.cx = self.pos(off + len(s) - 1)
        else:
            self.cy, self.cx = self.pos(off + len(s))
            self.collapse()
        self.want_x = None

    # -- cursor motion --------------------------------------------------------

    def col_of(self, y: int, cx: int) -> int:
        return sum(build.char_width(display_char(c)) for c in self.lines[y][:cx])

    def idx_for_col(self, y: int, col: int) -> int:
        acc = 0
        for i, ch in enumerate(self.lines[y]):
            w = build.char_width(display_char(ch))
            if acc + w > col:
                return i
            acc += w
        return len(self.lines[y])

    def move_v(self, dy: int) -> None:
        """Move by screen rows (a wrapped line has several). Past the first or
        last row of the buffer the cursor goes to that line's start / end."""
        if self.want_x is None:
            self.want_x = self.cursor_seg()[1]
        step = 1 if dy > 0 else -1
        for _ in range(abs(dy)):
            if not self.step_row(step):
                self.cx = 0 if dy < 0 else len(self.lines[self.cy])
                self.want_x = None
                break
        self.reset_group()

    def step_row(self, step: int) -> bool:
        """One screen row up (-1) or down (+1), keeping want_x. False at the ends."""
        s, _x = self.cursor_seg()
        er, _pr = self.rows_of(self.cy)
        ln = self.cy
        if 0 <= s + step < len(er):
            s += step
        elif 0 <= ln + step < len(self.lines):
            ln += step
            er, _pr = self.rows_of(ln)
            s = 0 if step > 0 else len(er) - 1
        else:
            return False
        cells = self.layout_cache()[0][ln]
        want = self.want_x or 0
        # the row covers [start, next start); the last row also takes cx == len
        start = er[s][0]
        stop = er[s + 1][0] if s + 1 < len(er) else len(cells) + 1
        x, i = 0, start
        while i < min(stop, len(cells)) and x + cells[i][3] <= want:
            x += cells[i][3]
            i += 1
        self.cy, self.cx = ln, min(i, stop - 1)
        return True

    def move_h(self, dx: int, wrap: bool = True) -> None:
        if dx < 0:
            if self.cx > 0:
                self.cx -= 1
            elif wrap and self.cy > 0:
                self.cy -= 1
                self.cx = len(self.lines[self.cy])
        else:
            if self.cx < len(self.lines[self.cy]):
                self.cx += 1
            elif wrap and self.cy < len(self.lines) - 1:
                self.cy += 1
                self.cx = 0
        self.want_x = None
        self.reset_group()

    def word_left(self) -> None:
        if self.cx == 0:
            self.move_h(-1)
            return
        line = self.lines[self.cy]
        i = self.cx
        while i > 0 and line[i - 1].isspace():
            i -= 1
        while i > 0 and not line[i - 1].isspace():
            i -= 1
        self.cx = i
        self.want_x = None
        self.reset_group()

    def word_right(self) -> None:
        line = self.lines[self.cy]
        if self.cx >= len(line):
            self.move_h(1)
            return
        i = self.cx
        while i < len(line) and not line[i].isspace():
            i += 1
        while i < len(line) and line[i].isspace():
            i += 1
        self.cx = i
        self.want_x = None
        self.reset_group()

    def home(self) -> None:
        self.cx = 0
        self.want_x = None
        self.reset_group()

    def end(self) -> None:
        self.cx = len(self.lines[self.cy])
        self.want_x = None
        self.reset_group()

    def first_nonblank(self) -> None:
        line = self.lines[self.cy]
        self.cx = len(line) - len(line.lstrip())
        self.want_x = None

    def goto_line(self, n: int) -> None:
        self.cy = max(0, min(n - 1, len(self.lines) - 1))
        self.cx = 0
        self.want_x = None

    def simple_move(self, k: str, wrap: bool = True) -> bool:
        """Shared cursor keys. Returns True if `k` was a movement key."""
        if k in ("up", "k"):
            self.move_v(-1)
        elif k in ("down", "j"):
            self.move_v(1)
        elif k in ("left", "h"):
            self.move_h(-1, wrap)
        elif k in ("right", "l"):
            self.move_h(1, wrap)
        elif k in ("home", "^A"):
            self.home()
        elif k in ("end", "^E"):
            self.end()
        elif k == "pgup":
            self.move_v(-(self.body_h - 1))
        elif k == "pgdn":
            self.move_v(self.body_h - 1)
        elif k == "ctrl-left":
            self.word_left()
        elif k == "ctrl-right":
            self.word_right()
        else:
            return False
        return True

    # -- editing primitives ---------------------------------------------------

    def insert(self, s: str) -> None:
        self.snapshot("type")
        line = self.lines[self.cy]
        self.lines[self.cy] = line[:self.cx] + s + line[self.cx:]
        self.cx += len(s)
        self.want_x = None

    def newline(self) -> None:
        self.snapshot("newline")
        line = self.lines[self.cy]
        self.lines[self.cy:self.cy + 1] = [line[:self.cx], line[self.cx:]]
        self.cy += 1
        self.cx = 0
        self.want_x = None

    def backspace(self) -> None:
        if self.cx > 0:
            self.snapshot("backspace")
            line = self.lines[self.cy]
            self.lines[self.cy] = line[:self.cx - 1] + line[self.cx:]
            self.cx -= 1
        elif self.cy > 0:
            self.snapshot("backspace")
            prev = self.lines[self.cy - 1]
            self.lines[self.cy - 1:self.cy + 1] = [prev + self.lines[self.cy]]
            self.cy -= 1
            self.cx = len(prev)
        self.want_x = None

    def delete(self) -> None:
        line = self.lines[self.cy]
        if self.cx < len(line):
            self.snapshot("delete")
            self.lines[self.cy] = line[:self.cx] + line[self.cx + 1:]
        elif self.cy < len(self.lines) - 1:
            self.snapshot("delete")
            self.lines[self.cy:self.cy + 2] = [line + self.lines[self.cy + 1]]
        self.want_x = None

    def delete_word_back(self) -> None:
        if self.cx == 0:
            self.backspace()
            return
        self.snapshot("backspace")
        line = self.lines[self.cy]
        i = self.cx
        while i > 0 and line[i - 1].isspace():
            i -= 1
        while i > 0 and not line[i - 1].isspace():
            i -= 1
        self.lines[self.cy] = line[:i] + line[self.cx:]
        self.cx = i
        self.want_x = None

    def kill_to_line_start(self) -> None:
        if self.cx == 0:
            return
        self.snapshot("backspace")
        line = self.lines[self.cy]
        self.lines[self.cy] = line[self.cx:]
        self.cx = 0
        self.want_x = None

    # -- search ---------------------------------------------------------------

    def search(self, pattern: str, backward: bool = False) -> None:
        if not pattern:
            return
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        text = self.text()
        here = self.off(self.cy, self.cx)
        matches = [m for m in rx.finditer(text) if m.end() > m.start()]
        if not matches:
            self.flash(f"no match: {pattern}")
            return
        if backward:
            before = [m for m in matches if m.start() < here]
            m = before[-1] if before else matches[-1]
        else:
            after = [m for m in matches if m.start() > here]
            m = after[0] if after else matches[0]
        if self.keys == "helix":
            self.anchor = self.pos(m.start())
            self.cy, self.cx = self.pos(m.end() - 1)
        else:
            self.cy, self.cx = self.pos(m.start())
            self.collapse()
        self.want_x = None

    # -- layout / drawing -----------------------------------------------------

    def layout(self, W: int) -> tuple[str, int, int]:
        """-> (mode, editor width, preview x). mode: split | edit | preview."""
        can_split = W >= GUTTER_W + MIN_EDIT_W + 1 + PREVIEW_W
        if can_split:
            if self.show_preview:
                edit_w = W - GUTTER_W - 1 - PREVIEW_W
                return "split", edit_w, GUTTER_W + edit_w + 1
            return "edit", W - GUTTER_W, 0
        if self.preview_solo:
            return "preview", W - GUTTER_W, 0
        return "edit", W - GUTTER_W, 0

    # -- soft wrap ------------------------------------------------------------
    # Both panes wrap lines that don't fit: the preview at 64 visible columns,
    # exactly where the build (and :post / :fmt) hard-wraps them; the editor at
    # its own width. A line takes max(editor rows, preview rows) screen rows so
    # the two panes stay line-aligned. Lines that fit are drawn as before.

    def layout_cache(self) -> tuple[list, list[int], dict]:
        """(scan_lines cells, visible widths, per-line row cache) for the buffer."""
        key = (self.lines, self.wrap_w, self.wrap_mode)
        if self._lc_key is None or self._lc_key != key:
            self._lc_key = (list(self.lines), self.wrap_w, self.wrap_mode)
            text = self.text()
            self._lc = (scan_lines(text), build.line_widths(text), {})
        return self._lc

    def rows_of(self, ln: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """(editor rows, preview rows) of line `ln` as (start, end) cell ranges,
        see build.wrap_rows."""
        slines, _w, cache = self.layout_cache()
        if ln not in cache:
            cells = slines[ln]
            spaces = [ch == " " and not tag for ch, _s, tag, _w in cells]
            one = [(0, len(cells))]
            er = pr = one
            if self.wrap_mode != "preview":
                er = build.wrap_rows([w for *_r, w in cells], spaces, self.wrap_w)
            if self.wrap_mode != "edit":
                pr = build.wrap_rows([0 if tag else w for _c, _s, tag, w in cells],
                                     spaces, MAX_COLS)
            cache[ln] = (er, pr)
        return cache[ln]

    def nrows(self, ln: int) -> int:
        er, pr = self.rows_of(ln)
        return max(len(er), len(pr))

    def cursor_seg(self) -> tuple[int, int]:
        """(editor row within line cy, x cell offset in that row) of the cursor."""
        er, _pr = self.rows_of(self.cy)
        cells = self.layout_cache()[0][self.cy]
        s = 0
        for i, (a, _b) in enumerate(er):
            if a <= self.cx:
                s = i
        return s, sum(w for *_r, w in cells[er[s][0]:self.cx])

    def visible_rows(self, body_h: int) -> list[tuple[int, int]]:
        """(line, row within the line) for each screen row from the scroll position."""
        out: list[tuple[int, int]] = []
        ln, r = self.scroll_y, self.scroll_row
        while len(out) < body_h and ln < len(self.lines):
            n = self.nrows(ln)
            while r < n and len(out) < body_h:
                out.append((ln, r))
                r += 1
            ln, r = ln + 1, 0
        return out

    def clamp_scroll(self, body_h: int) -> None:
        self.scroll_y = max(0, min(self.scroll_y, len(self.lines) - 1))
        if self.scroll_row >= self.nrows(self.scroll_y):
            self.scroll_row = 0
        if self.wrap_mode == "preview":
            return
        cs, _x = self.cursor_seg()
        if (self.cy, cs) < (self.scroll_y, self.scroll_row):
            self.scroll_y, self.scroll_row = self.cy, cs
            return
        row = cs - self.scroll_row
        for ln in range(self.scroll_y, self.cy):
            row += self.nrows(ln)
        while row >= body_h:          # pull the top down one row at a time
            if self.scroll_row + 1 < self.nrows(self.scroll_y):
                self.scroll_row += 1
            else:
                self.scroll_y, self.scroll_row = self.scroll_y + 1, 0
            row -= 1

    def put(self, y: int, x: int, s: str, attr: int = 0, maxx: int | None = None) -> None:
        H, W = self.scr.getmaxyx()
        limit = W if maxx is None else min(W, maxx)
        if y < 0 or y >= H or x >= limit:
            return
        if x < 0:
            s = s[-x:]
            x = 0
        out, w = [], 0
        for ch in s:
            cw = build.char_width(ch)
            if x + w + cw > limit:
                break
            out.append(ch)
            w += cw
        if not out:
            return
        try:
            self.scr.addstr(y, x, "".join(out), attr)
        except curses.error:
            pass  # writing the bottom-right cell always raises; harmless

    def draw_cells(self, y: int, x0: int, cells: list[tuple[str, int, int]],
                   width: int, scroll: int) -> None:
        """Draw (char, attr, cellwidth) cells at column x0, clipped to `width`."""
        x = -scroll
        run: list[str] = []
        run_attr = 0
        run_x = 0

        def flush() -> None:
            if run:
                self.put(y, x0 + run_x, "".join(run), run_attr, maxx=x0 + width)
                run.clear()

        for ch, attr, w in cells:
            if x + w > width:
                flush()
                self.put(y, x0 + width - 1, "»", self.colors.c("dim"))
                return
            if x >= 0:
                if attr != run_attr or not run:
                    flush()
                    run_attr = attr
                    run_x = x
                run.append(ch)
            x += w
        flush()

    def mode_badge(self) -> tuple[str, int]:
        c = self.colors
        if self.keys != "helix":
            return "", 0
        if self.mode == "insert":
            return "INS", c.c("warn") | curses.A_BOLD
        if self.mode == "select":
            return "SEL", c.c("tb") | curses.A_BOLD
        return "NOR", c.c("hdr") | curses.A_BOLD

    def draw_header(self, W: int) -> None:
        c = self.colors
        self.put(0, 0, " lv154 ✎ ", c.c("hdr") | curses.A_BOLD)
        x = 10
        badge, battr = self.mode_badge()
        if badge:
            self.put(0, x, badge, battr)
            x += len(badge) + 2
        name = relpath(self.path)
        self.put(0, x, name)
        x += build.display_width(name)
        if self.path is not None:
            if self.dirty:
                self.put(0, x, " ●", c.c("warn"))
            else:
                self.put(0, x, " ✓", c.c("green"))
            x += 2
        pend = self.count + self.pending
        if pend:
            self.put(0, x + 1, pend, c.c("warn"))
            x += len(pend) + 1
        if self.keys == "helix":
            if self.mode == "insert":
                hint = "esc normal   ^W del word   ^U del to start   F1 help"
            else:
                hint = "i insert  x line  d y p  u U  w b e  / find  :w :q :publish  spc-f files  F1 help"
        else:
            hint = "^S save  ^O files  ^N new  ^F find  ^P preview  ^Z/^R undo  F2 publish  F1 help  ^Q quit"
        if W - x - 2 < len(hint):
            hint = "F1 help"
        if W - x - 2 >= len(hint):
            self.put(0, W - len(hint) - 1, hint, c.c("dim"))

    def selection_cols(self, ln: int) -> tuple[int, int] | None:
        """Inclusive selected column range on line `ln`, or None."""
        if self.keys != "helix" or self.mode == "insert":
            return None
        if not (self.has_selection() or self.mode == "select"):
            return None
        lo, hi = self.sel_range()
        if ln < lo[0] or ln > hi[0]:
            return None
        start = lo[1] if ln == lo[0] else 0
        end = hi[1] if ln == hi[0] else len(self.lines[ln])
        return start, end

    def draw_editor(self, top: int, edit_w: int, slines: list, widths: list[int],
                    rows: list[tuple[int, int]]) -> None:
        c = self.colors
        site = self.site_editor
        fill = c.fill(site)
        for y, (ln, r) in enumerate(rows, start=top):
            if fill:
                self.put(y, GUTTER_W, " " * edit_w, fill, maxx=GUTTER_W + edit_w)
            er, _pr = self.rows_of(ln)
            if r == 0:
                vw = widths[ln]
                numattr = 0 if ln == self.cy else c.c("dim")
                if vw > MAX_COLS:
                    wattr = c.c("err")
                elif vw >= NEAR_COLS:
                    wattr = c.c("warn")
                else:
                    wattr = c.c("dim")
                self.put(y, 0, f"{ln + 1:>3}", numattr)
                self.put(y, 4, f"{vw:>3}", wattr)
            if r >= len(er):
                continue
            a, b = er[r]
            sel = self.selection_cols(ln)
            line_cells = slines[ln]
            cells = []
            for i in range(a, b):
                ch, st, _tag, w = line_cells[i]
                attr = c.attr(st, site)
                if sel and sel[0] <= i <= sel[1]:
                    attr |= curses.A_REVERSE
                cells.append((ch, attr, w))
            if (r == len(er) - 1 and sel and sel[1] >= len(self.lines[ln])
                    and ln < len(self.lines) - 1):
                cells.append((" ", c.attr(NORM, site) | curses.A_REVERSE, 1))
            self.draw_cells(y, GUTTER_W, cells, edit_w, 0)

    def draw_preview(self, top: int, x0: int, width: int, slines: list,
                     rows: list[tuple[int, int]]) -> None:
        c = self.colors
        site = self.site_preview
        fill = c.fill(site)
        dim = c.pair("dim", "bg" if site else None)
        for y, (ln, r) in enumerate(rows, start=top):
            if fill:
                self.put(y, x0, " " * width, fill, maxx=x0 + width)
            if MAX_COLS < width:
                self.put(y, x0 + MAX_COLS, "│", dim)
            _er, pr = self.rows_of(ln)
            if r >= len(pr):
                continue
            a, b = pr[r]
            cells = []
            x = 0
            for ch, st, tag, w in slines[ln][a:b]:
                if tag:
                    continue
                cells.append((ch, c.attr(st, site, over=(x + w > MAX_COLS)), w))
                x += w
            self.draw_cells(y, x0, cells, width, 0)

    def draw_footer(self, y: int, W: int, widths: list[int]) -> None:
        c = self.colors
        dim = c.c("dim")
        vw = widths[self.cy]
        raw = len(self.lines[self.cy])
        x = 1
        s = f"ln {self.cy + 1}/{len(self.lines)}  col {self.cx + 1}  vis "
        self.put(y, x, s, dim)
        x += len(s)
        wattr = c.c("err") if vw > MAX_COLS else c.c("warn") if vw >= NEAR_COLS else 0
        s = f"{vw}/{MAX_COLS}"
        self.put(y, x, s, wattr)
        x += len(s)
        if raw != vw:
            s = f"  (src {raw})"
            self.put(y, x, s, dim)
            x += len(s)
        self.put(y, x, "  │  ", dim)
        x += 5
        over = [(i + 1, w) for i, w in enumerate(widths) if w > MAX_COLS]
        if not over:
            self.put(y, x, f"all lines fit {MAX_COLS}", c.c("green"))
        else:
            head = ", ".join(f"L{ln}={w}" for ln, w in over[:5])
            more = "…" if len(over) > 5 else ""
            self.put(y, x, f"{len(over)} line(s) wrap at {MAX_COLS}: {head}{more}", c.c("warn"))
        if self.msg and time.time() < self.msg_until:
            self.put(y, max(x + 2, W - build.display_width(self.msg) - 1), self.msg, c.c("warn"))

    def draw(self) -> None:
        scr = self.scr
        H, W = scr.getmaxyx()
        scr.erase()
        mode, edit_w, prev_x = self.layout(W)
        body_h = max(1, H - 2)
        self.body_h = body_h
        self.wrap_w, self.wrap_mode = edit_w, mode
        slines, widths, _cache = self.layout_cache()
        self.clamp_scroll(body_h)
        rows = self.visible_rows(body_h)
        self.draw_header(W)
        if mode in ("split", "edit"):
            self.draw_editor(1, edit_w, slines, widths, rows)
        if mode == "split":
            sep = self.colors.c("dim")
            for row in range(body_h):
                self.put(1 + row, GUTTER_W + edit_w, "│", sep)
            self.draw_preview(1, prev_x, PREVIEW_W, slines, rows)
        elif mode == "preview":
            self.draw_preview(1, 0, W, slines, rows)
        self.draw_footer(H - 1, W, widths)
        if mode in ("split", "edit"):
            cs, x = self.cursor_seg()
            try:
                scr.move(1 + rows.index((self.cy, cs)), GUTTER_W + min(x, edit_w - 1))
                curses.curs_set(1)
            except (ValueError, curses.error):
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        scr.noutrefresh()

    def overlay(self, title: str, rows: list, hint: str,
                sel: int | None = None) -> None:
        """Draw a centred box over the current screen (caller calls doupdate)."""
        H, W = self.scr.getmaxyx()
        h = min(len(rows) + 4, max(6, H - 2))
        longest = max((len(r[0] if isinstance(r, tuple) else r) for r in rows), default=0)
        w = min(max(len(hint) + 4, longest + 6, 44), max(20, W - 4))
        y0, x0 = max(0, (H - h) // 2), max(0, (W - w) // 2)
        win = curses.newwin(h, w, y0, x0)
        win.erase()
        dim = self.colors.c("dim")
        try:  # the bottom-right cell always raises; draw it first
            win.addstr(h - 1, 0, "└" + "─" * (w - 2) + "┘", dim)
        except curses.error:
            pass
        try:
            win.addstr(0, 0, "┌" + "─" * (w - 2) + "┐", dim)
            for r in range(1, h - 1):
                win.addstr(r, 0, "│", dim)
                win.addstr(r, w - 1, "│", dim)
            win.addstr(0, 2, f" {title} ", self.colors.c("hdr") | curses.A_BOLD)
            for i, row in enumerate(rows[:h - 4]):
                attr = curses.A_REVERSE if i == sel else 0
                if isinstance(row, tuple):
                    row, attr = row[0], row[1] | attr
                win.addstr(1 + i, 2, row[:w - 4].ljust(w - 4), attr)
            win.addstr(h - 2, 2, hint[:w - 4], dim)
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        win.noutrefresh()

    # -- input ----------------------------------------------------------------

    def getkey(self):
        try:
            return self.scr.get_wch()
        except curses.error:
            return None

    def read_key(self, block: bool = False) -> str | None:
        """Next key as a name. Assembles ESC sequences ncurses didn't recognise.

        `block` forces a blocking read even inside the paste-drain loop, which
        is what prompts and overlays need (otherwise they'd see "no key" and
        cancel themselves).
        """
        if block:
            self.scr.timeout(-1)
        try:
            return self._read_key()
        finally:
            self.scr.timeout(0 if self.in_burst else -1)

    def _read_key(self) -> str | None:
        k = self.getkey()
        self.last_raw = k
        if k != "\x1b":
            return keyname(k)
        seq = ""
        self.scr.timeout(30)
        try:
            while len(seq) < 16:
                c = self.getkey()
                if c is None:
                    break
                if isinstance(c, int) or c == "\x1b":
                    # a whole key of its own (F2, arrows, another ESC): hand it
                    # back so the next read_key sees it instead of losing it
                    curses.ungetch(c) if isinstance(c, int) else curses.unget_wch(c)
                    break
                seq += c
                if len(seq) == 1 and c not in "[O":
                    curses.unget_wch(c)         # a real key right after a bare ESC
                    seq = ""
                    break
                if seq[0] == "O" and len(seq) == 2:
                    break
                if seq[0] == "[" and len(seq) >= 2 and "@" <= c <= "~":
                    break
        finally:
            self.scr.timeout(0 if self.in_burst else -1)
        return ESC_SEQ.get(seq, "esc")

    def ask(self, msg: str) -> str | None:
        """Show `msg` in the footer and return the next key name."""
        self.draw()
        H, W = self.scr.getmaxyx()
        self.put(H - 1, 0, " " * (W - 1))
        self.put(H - 1, 1, msg, self.colors.c("warn"))
        self.scr.noutrefresh()
        curses.doupdate()
        return self.read_key(block=True)

    def confirm(self, msg: str) -> bool:
        return self.ask(msg + "  [y/N]") == "y"

    def prompt(self, label: str) -> str | None:
        """Single-line input in the footer. None if cancelled."""
        buf = ""
        while True:
            self.draw()
            H, W = self.scr.getmaxyx()
            self.put(H - 1, 0, " " * (W - 1))
            self.put(H - 1, 1, label, self.colors.c("warn"))
            x = 1 + build.display_width(label)
            self.put(H - 1, x, buf)
            try:
                self.scr.move(H - 1, min(W - 1, x + build.display_width(buf)))
                curses.curs_set(1)
            except curses.error:
                pass
            self.scr.noutrefresh()
            curses.doupdate()
            k = self.read_key(block=True)
            if k in ("esc", "^C", "^Q"):
                return None
            if k == "enter":
                return buf.strip()
            if k == "backspace":
                if not buf:
                    return None
                buf = buf[:-1]
            elif k == "^U":
                buf = ""
            elif k == "space":
                buf += " "
            elif k is not None and len(k) == 1 and k >= " ":
                buf += k

    def show_text(self, title: str, rows: list, hint: str = "j/k scroll   q close",
                  jumps: list[int] | None = None, top: int = 0,
                  enter_closes: bool = True) -> None:
        """Scrollable read-only overlay.

        Typeahead is dropped first: a key pressed while git was running must
        not land here and close the result before it has been read.
        `enter_closes=False` keeps a stray Enter from closing it either.
        """
        curses.flushinp()
        closers = {"q", "esc", "f1", "^Q", "^C"} | ({"enter"} if enter_closes else set())
        while True:
            H, _ = self.scr.getmaxyx()
            page = max(1, H - 6)
            top = max(0, min(top, max(0, len(rows) - page)))
            self.draw()
            self.overlay(title, rows[top:top + page], hint)
            curses.doupdate()
            k = self.read_key(block=True)
            if k in closers:
                return
            if k in ("j", "down"):
                top += 1
            elif k in ("k", "up"):
                top -= 1
            elif k in ("pgdn", "space", "^D", "^F"):
                top += page - 1
            elif k in ("pgup", "b", "^U", "^B"):
                top -= page - 1
            elif k in ("g", "home"):
                top = 0
            elif k in ("G", "end"):
                top = len(rows)
            elif jumps and len(k) == 1 and k.isdigit() and 0 < int(k) <= len(jumps):
                top = jumps[int(k) - 1]

    def help(self, topic: str | None = None) -> None:
        """Scrollable cheat sheet. F1 / :help [topic]."""
        hdr = self.colors.c("hdr") | curses.A_BOLD
        rows: list = []
        starts: list[int] = []
        for _sid, title, body in HELP:
            starts.append(len(rows))
            rows.append((f"# {title}", hdr))
            rows.extend(body)
            rows.append("")
        if topic is None:
            topic = self.keys
        self.show_text("help", rows,
                       "j/k scroll  pgdn/pgup  1 about 2 markup 3 ctrl 4 helix  q close",
                       jumps=starts, top=starts[HELP_TOPICS.get(topic, 0)])

    # -- git ------------------------------------------------------------------

    def git_status_rows(self) -> list[str]:
        code, branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            return [f"git error: {branch}"]
        note = "" if branch == DEPLOY_BRANCH else f"   (the site deploys from {DEPLOY_BRANCH})"
        rows = [f"branch: {branch}{note}", ""]
        code, out = git("status", "--porcelain", "--", *CONTENT_PATHS)
        if code != 0:
            return rows + [out]
        rows.append("content changes:" if out else "content is clean: nothing to publish")
        rows += ["  " + l for l in out.splitlines()]
        drafts = sorted(DRAFTS.glob("*.txt")) if DRAFTS.is_dir() else []
        if drafts:
            rows += ["", "drafts (local only, never published; :post moves one to src/posts):"]
            rows += ["  " + relpath(d) for d in drafts]
        code, ahead = git("rev-list", "--count", "@{u}..HEAD")
        if code == 0 and ahead.strip() not in ("", "0"):
            rows += ["", f"{ahead.strip()} local commit(s) not pushed yet (:publish retries the push)"]
        code, behind = git("rev-list", "--count", "HEAD..@{u}")
        if code == 0 and behind.strip() not in ("", "0"):
            rows += ["", f"{behind.strip()} commit(s) on origin not pulled yet (as of the last fetch);",
                     "  run `git pull --rebase --autostash` in a shell before publishing"]
        code, log = git("log", "--oneline", "-5")
        if code == 0 and log:
            rows += ["", "recent commits:"] + ["  " + l for l in log.splitlines()]
        return rows

    def status(self) -> None:
        self.show_text("git status", self.git_status_rows())

    def default_message(self) -> str:
        if self.path is not None and self.path.parent == POSTS:
            return f"post: {self.lines[0].strip() or self.path.stem}"
        if self.path is not None and self.path.parent == PAGES:
            return f"page: {self.path.stem}"
        return "content update"

    def publish(self, message: str | None = None) -> None:
        """Save, then commit src/pages + src/posts only, then push the deploy branch."""
        log: list[str] = []
        if self.path is not None and self.dirty:
            if not self.save():
                return
            log.append(f"saved {relpath(self.path)}")
        code, branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            self.show_text("publish", [f"git error: {branch}"])
            return
        if branch != DEPLOY_BRANCH:
            self.show_text("publish", [
                f"on branch '{branch}', but the site deploys from '{DEPLOY_BRANCH}'.",
                "switch branches in a shell, then publish again."])
            return
        code, changes = git("status", "--porcelain", "--", *CONTENT_PATHS)
        if code != 0:
            self.show_text("publish", [f"git error: {changes}"])
            return
        draft_open = self.path is not None and self.path.parent == DRAFTS
        if not changes and draft_open:
            self.flash("draft saved, not published. :post moves it into src/posts first")
            return
        # know where origin is before committing anything on top of a stale main
        self.flash("checking origin...", 30)
        self.draw()
        curses.doupdate()
        code, out = git("fetch", "origin", DEPLOY_BRANCH, timeout=30)
        self.msg_until = 0.0
        if code != 0:
            self.show_text("publish", log + ["can't reach origin; nothing committed or pushed.", ""]
                           + ["  " + l for l in out.splitlines()], enter_closes=False)
            return
        code, behind = git("rev-list", "--count", f"HEAD..origin/{DEPLOY_BRANCH}")
        behind = behind.strip() if code == 0 else "?"
        if behind not in ("0", ""):
            self.show_text("publish", log + [
                f"origin/{DEPLOY_BRANCH} has {behind} commit(s) this clone doesn't, so a push",
                "would be rejected. nothing committed or pushed.", "",
                "in a shell:  git pull --rebase --autostash",
                "then publish again."], enter_closes=False)
            return
        code, ahead = git("rev-list", "--count", f"origin/{DEPLOY_BRANCH}..HEAD")
        ahead = ahead.strip() if code == 0 else "0"
        if not changes:
            # nothing new, but an earlier publish may have committed and failed to push
            if ahead in ("", "0"):
                self.flash("nothing to publish: the site matches the last commit")
                return
            if not self.confirm(f"content is clean, but {ahead} local commit(s) never made it to "
                                f"{DEPLOY_BRANCH}. push now?"):
                return
            self.push_and_report(log)
            return
        msg = message or self.default_message()
        n = len(changes.splitlines())
        note = "  (the open draft stays out)" if draft_open else ""
        if not self.confirm(f'commit "{msg}" ({n} file(s)) and push {DEPLOY_BRANCH}?{note}'):
            return
        log += ["changes:"] + ["  " + l for l in changes.splitlines()] + [""]
        code, out = git("add", "-A", "--", *CONTENT_PATHS)
        if code != 0:
            self.show_text("publish", log + ["git add failed:", out])
            return
        code, out = git("commit", "-m", msg, "--", *CONTENT_PATHS)
        log += [f"commit: {msg}"] + ["  " + l for l in out.splitlines()]
        if code != 0:
            self.show_text("publish", log + ["", "commit failed; nothing pushed."])
            return
        log.append("")
        self.push_and_report(log)

    def push_and_report(self, log: list[str]) -> None:
        """Push the deploy branch and show the outcome; a failure also lingers in the footer."""
        code, out = git("push", "origin", DEPLOY_BRANCH, timeout=90)
        log += ["push:"] + ["  " + l for l in (out.splitlines() or ["ok"])]
        if code != 0:
            log += ["", "push failed; the commit is safe locally and nothing is on the site yet.",
                    "fix the cause, then :publish again to retry the push (F3 shows what's pending).",
                    "(a passphrase or login prompt can't happen inside the tui, so it fails fast.)"]
            self.show_text("publish", log, enter_closes=False)
            self.flash(f"push to {DEPLOY_BRANCH} FAILED: commit is local only. :publish retries", 8)
        else:
            log += ["", f"done. the server pulls {DEPLOY_BRANCH} within ~5 min."]
            self.show_text("publish", log)

    # -- files ----------------------------------------------------------------

    def new_draft(self, slug: str | None = None) -> None:
        """Start a post as src/drafts/<slug>.txt; :post moves it into src/posts."""
        if slug is None:
            slug = self.prompt("new draft slug (lowercase-with-dashes): ")
        if not slug:
            return
        if not POST_SLUG_RE.match(slug):
            self.flash("bad slug: lowercase letters, digits, - and _ only")
            return
        self.create_and_open(DRAFTS / f"{slug}.txt", POST_SEED)

    def relocate(self, src: Path, dst: Path) -> bool:
        """Move a content file (draft <-> post), saving it first if it's open."""
        if dst.exists():
            self.flash(f"already exists: {relpath(dst)}")
            return False
        if src == self.path and self.dirty and not self.save():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        if src == self.path:
            self.path = dst
        return True

    def post_draft(self, target: Path | None = None, when: str | None = None) -> None:
        """Move a draft into src/posts as <date>-<slug>.txt (date defaults to today)."""
        src = target or self.path
        if src is None or src.parent != DRAFTS:
            self.flash("not a draft (drafts live in src/drafts; ^N / :new starts one)")
            return
        when = when or date.today().isoformat()
        try:
            date.fromisoformat(when)
        except ValueError:
            self.flash("usage: post [YYYY-MM-DD]")
            return
        dst = POSTS / f"{when}-{DATE_PREFIX_RE.sub('', src.stem)}.txt"
        if dst.exists():
            self.flash(f"already exists: {relpath(dst)}")
            return
        n = self.hard_wrap_file(src)
        if self.relocate(src, dst):
            wrapped = f", {n} long line(s) wrapped at {MAX_COLS}" if n else ""
            self.flash(f"now a post: {relpath(dst)}{wrapped}   (:publish pushes it live)")

    def unpost(self, target: Path | None = None) -> None:
        """Move a post back into src/drafts (the next publish drops it from the site)."""
        src = target or self.path
        if src is None or src.parent != POSTS:
            self.flash("not a post")
            return
        dst = DRAFTS / f"{DATE_PREFIX_RE.sub('', src.stem)}.txt"
        tracked = git("ls-files", "--error-unmatch", "--", str(src))[0] == 0
        if tracked and not self.confirm(
                f"unpost {src.name}? it is live; the next publish removes it from the site"):
            return
        if self.relocate(src, dst):
            self.flash(f"now a draft: {relpath(dst)}   (not built, not published)")

    def new_page(self, slug: str | None = None) -> None:
        if slug is None:
            slug = self.prompt("new page slug (becomes /<slug>/): ")
        if not slug:
            return
        if not PAGE_SLUG_RE.match(slug):
            self.flash("bad slug: must start with a letter; lowercase, digits, - and _")
            return
        self.create_and_open(PAGES / f"{slug}.txt", page_seed(slug))

    def create_and_open(self, path: Path, seed: str) -> None:
        if path.exists():
            self.flash(f"already exists: {relpath(path)}")
            return
        if self.dirty and not self.confirm("discard unsaved changes?"):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(seed)
        self.open_path(path)
        self.flash(f"created {relpath(path)}")

    def open_arg(self, arg: str) -> None:
        try:
            path = resolve_arg(arg)
        except ValueError as e:
            self.flash(f"can't open: {e}")
            return
        if path == self.path:
            return
        if self.dirty and not self.confirm("discard unsaved changes?"):
            return
        self.open_path(path)

    def picker(self) -> None:
        sel = 0
        entries = list_entries()
        if self.path in entries:
            sel = entries.index(self.path)
        hint = "enter open  n new draft  N new page  p post/unpost  d delete  esc"
        while True:
            entries = list_entries()
            sel = max(0, min(sel, len(entries) - 1)) if entries else 0
            labels = [relpath(p) for p in entries] or ["(no files yet)"]
            H, _ = self.scr.getmaxyx()
            rows = max(1, min(len(labels), H - 6))
            top = max(0, min(sel - rows + 1, sel))
            self.draw()
            self.overlay("files", labels[top:top + rows], hint,
                         sel - top if entries else None)
            curses.doupdate()
            k = self.read_key(block=True)
            if k in ("esc", "q", "^O", "^Q", "^C"):
                return
            if k in ("up", "k"):
                sel -= 1
            elif k in ("down", "j"):
                sel += 1
            elif k == "pgup":
                sel -= rows
            elif k == "pgdn":
                sel += rows
            elif k == "home":
                sel = 0
            elif k == "end":
                sel = len(entries) - 1
            elif k == "enter" and entries:
                target = entries[sel]
                if target == self.path:
                    return
                if self.dirty and not self.confirm("discard unsaved changes?"):
                    continue
                self.open_path(target)
                return
            elif k == "n":
                before = self.path
                self.new_draft()
                if self.path is not None and self.path != before:
                    return
            elif k == "p" and entries:
                target = entries[sel]
                if target.parent == DRAFTS:
                    self.post_draft(target)
                elif target.parent == POSTS:
                    self.unpost(target)
                else:
                    self.flash("pages don't have drafts")
            elif k == "N":
                before = self.path
                self.new_page()
                if self.path is not None and self.path != before:
                    return
            elif k == "d" and entries:
                target = entries[sel]
                if self.confirm(f"delete {relpath(target)}?"):
                    target.unlink(missing_ok=True)
                    if target == self.path:
                        self.close_file()
                    self.flash(f"deleted {relpath(target)}")

    def quit(self, force: bool = False) -> None:
        if force or not self.dirty:
            self.running = False
            return
        k = self.ask("unsaved changes:  [s]ave & quit   [d]iscard   [esc] cancel")
        if k == "s":
            if self.save():
                self.running = False
        elif k == "d":
            self.running = False

    def toggle_preview(self) -> None:
        H, W = self.scr.getmaxyx()
        if W >= GUTTER_W + MIN_EDIT_W + 1 + PREVIEW_W:
            self.show_preview = not self.show_preview
        else:
            self.preview_solo = not self.preview_solo

    # -- key handling ---------------------------------------------------------

    def handle(self, k: str | None) -> None:
        if k is None or k == "resize":
            return
        mode = self.layout(self.scr.getmaxyx()[1])[0]
        if mode == "preview":
            self.handle_preview_solo(k)
            return
        if k in ("^Q", "^C"):
            self.quit()
        elif k == "^S":
            self.save()
        elif k == "^O":
            self.picker()
        elif k == "^N":
            self.new_draft()
        elif k == "^P":
            self.toggle_preview()
        elif k == "f1":
            self.help()
        elif k == "f2":
            self.publish()
        elif k == "f3":
            self.status()
        elif self.keys == "helix":
            self.handle_helix(k)
        else:
            self.handle_ctrl(k)

    def handle_preview_solo(self, k: str) -> None:
        """Narrow-terminal preview: scroll only."""
        if k in ("^P", "esc"):
            self.preview_solo = False
        elif k in ("up", "k"):
            self.scroll_y = max(0, self.scroll_y - 1)
        elif k in ("down", "j"):
            self.scroll_y = min(len(self.lines) - 1, self.scroll_y + 1)
        elif k == "pgup":
            self.scroll_y = max(0, self.scroll_y - self.body_h)
        elif k == "pgdn":
            self.scroll_y = min(len(self.lines) - 1, self.scroll_y + self.body_h)
        elif k in ("^Q", "^C"):
            self.quit()
        elif k == "^S":
            self.save()
        elif k == "^O":
            self.picker()
        else:
            self.flash("preview: ^P back to editing")
        self.scroll_row = 0
        self.cy = max(self.scroll_y, min(self.cy, self.scroll_y + self.body_h - 1))
        self.clamp_cursor()
        self.collapse()

    # .. ctrl style ..........................................................

    def handle_ctrl(self, k: str) -> None:
        if k not in ("h", "j", "k", "l") and self.simple_move(k, wrap=True):
            return
        if k == "^Z":
            self.undo()
        elif k == "^R":
            self.redo()
        elif k == "^F":
            pat = self.prompt("find: ")
            if pat:
                self.search_pat = pat
                self.search(pat)
        elif k == "^G":
            if self.search_pat:
                self.search(self.search_pat)
            else:
                self.flash("no search yet (^F)")
        elif k == "^Y":
            self.copy_clipboard()
        elif k == "enter":
            self.newline()
        elif k == "backspace":
            self.backspace()
        elif k == "delete":
            self.delete()
        elif k == "tab":
            self.insert("  ")
        elif k == "space":
            self.insert(" ")
        elif k in ("esc", "insert"):
            pass
        elif len(k) == 1 and k >= " ":
            self.insert(k)

    # .. helix style .........................................................

    def take_count(self) -> int | None:
        n = int(self.count) if self.count else None
        self.count = ""
        return n

    def after_move(self, ext: bool) -> None:
        if not ext:
            self.collapse()

    def enter_insert(self, snap: bool = True) -> None:
        if snap:
            self.snapshot("insert", group=False)
        self.in_insert = True
        self.insert_snapped = True
        self.mode = "insert"
        self.collapse()

    def leave_insert(self) -> None:
        self.mode = "normal"
        self.in_insert = False
        self.insert_snapped = False
        if self.cx > 0:
            self.cx -= 1
        self.want_x = None
        self.collapse()
        self.reset_group()

    def handle_helix(self, k: str) -> None:
        if self.mode == "insert":
            self.handle_insert(k)
            return
        if k == "esc":
            self.pending = self.count = ""
            self.mode = "normal"
            self.collapse()
            return
        if self.pending:
            p, self.pending = self.pending, ""
            self.handle_pending(p, k)
            return
        if len(k) == 1 and k.isdigit() and (self.count or k != "0"):
            self.count += k
            return
        if k in ("g", "space", "f", "t", "F", "T", "r"):
            self.pending = k
            return
        n = self.take_count() or 1
        ext = self.mode == "select"

        # motions
        if k in ("h", "j", "k", "l", "up", "down", "left", "right",
                 "home", "end", "pgup", "pgdn", "ctrl-left", "ctrl-right"):
            for _ in range(n):
                self.simple_move(k, wrap=False)
            self.after_move(ext)
        elif k == "^U":
            self.move_v(-max(1, self.body_h // 2))
            self.after_move(ext)
        elif k == "^D":
            self.move_v(max(1, self.body_h // 2))
            self.after_move(ext)
        elif k == "^B":
            self.move_v(-(self.body_h - 1))
            self.after_move(ext)
        elif k == "^F":
            self.move_v(self.body_h - 1)
            self.after_move(ext)
        elif k in ("w", "b", "e"):
            for _ in range(n):
                self.word_motion(k, ext)
        elif k == "x":
            for _ in range(n):
                self.extend_line()
        elif k == "%":
            self.anchor = (0, 0)
            self.cy, self.cx = len(self.lines) - 1, len(self.lines[-1])
        elif k == "v":
            self.mode = "normal" if self.mode == "select" else "select"
        elif k == ";":
            self.collapse()
            self.mode = "normal"
        elif k == "n":
            if self.search_pat:
                self.search(self.search_pat)
            else:
                self.flash("no search yet (/)")
        elif k == "N":
            if self.search_pat:
                self.search(self.search_pat, backward=True)
            else:
                self.flash("no search yet (/)")
        elif k == "/":
            pat = self.prompt("/")
            if pat:
                self.search_pat = pat
                self.search(pat)

        # insert entry
        elif k == "i":
            lo, _ = self.sel_range()
            self.cy, self.cx = lo
            self.enter_insert()
        elif k == "a":
            _, hi = self.sel_range()
            self.cy, self.cx = hi[0], min(hi[1] + 1, len(self.lines[hi[0]]))
            self.enter_insert()
        elif k == "I":
            self.first_nonblank()
            self.enter_insert()
        elif k == "A":
            self.cx = len(self.lines[self.cy])
            self.enter_insert()
        elif k == "o":
            self.open_line(below=True)
        elif k == "O":
            self.open_line(below=False)

        # edits
        elif k == "d":
            self.snapshot("delete", group=False)
            self.register = self.delete_sel()
            self.mode = "normal"
        elif k == "c":
            self.snapshot("change", group=False)
            self.register = self.delete_sel()
            self.enter_insert(snap=False)
        elif k == "y":
            self.register = self.sel_text()
            self.flash(f"yanked {len(self.register)} chars")
            self.mode = "normal"
        elif k == "p":
            self.paste(after=True)
        elif k == "P":
            self.paste(after=False)
        elif k == "R":
            if not self.register:
                self.flash("nothing yanked")
            else:
                self.snapshot("replace", group=False)
                lo, _ = self.sel_range()
                self.delete_sel()
                self.insert_text(self.off(*lo), self.register, select=True)
            self.mode = "normal"
        elif k == "u":
            for _ in range(n):
                self.undo()
        elif k == "U":
            for _ in range(n):
                self.redo()
        elif k == "J":
            self.join_lines(n)
        elif k in (">", "<"):
            self.indent(k == ">")
        elif k == "~":
            self.swap_case()
        elif k == ":":
            cmd = self.prompt(":")
            if cmd:
                self.run_command(cmd)
        # anything else in normal mode is ignored

    def handle_pending(self, p: str, k: str) -> None:
        count = self.take_count()
        ext = self.mode == "select"
        if p == "g":
            if k == "g":
                self.goto_line(count or 1)
            elif k == "e":
                self.goto_line(len(self.lines))
                self.cx = len(self.lines[self.cy])
            elif k == "h":
                self.home()
            elif k == "l":
                self.end()
            elif k == "s":
                self.first_nonblank()
            else:
                return
            self.after_move(ext)
        elif p == "space":
            if k == "f":
                self.picker()
            elif k == "y":
                self.copy_clipboard()
        elif p in ("f", "t", "F", "T"):
            if k == "space" or (len(k) == 1 and k >= " "):
                self.find_char(" " if k == "space" else k, p, count or 1, ext)
        elif p == "r":
            if k == "space" or (len(k) == 1 and k >= " "):
                self.replace_chars(" " if k == "space" else k)

    def handle_insert(self, k: str) -> None:
        if k == "esc":
            self.leave_insert()
        elif k not in ("h", "j", "k", "l") and self.simple_move(k, wrap=True):
            self.collapse()
        elif k == "enter":
            self.newline()
        elif k == "backspace":
            self.backspace()
        elif k == "delete":
            self.delete()
        elif k == "tab":
            self.insert("  ")
        elif k == "space":
            self.insert(" ")
        elif k == "^W":
            self.delete_word_back()
        elif k == "^U":
            self.kill_to_line_start()
        elif len(k) == 1 and k >= " ":
            self.insert(k)
        self.collapse()   # typing never grows a selection

    def word_motion(self, k: str, ext: bool) -> None:
        text = self.text()
        i = self.off(self.cy, self.cx)
        if k == "w":
            head = w_motion(text, i)
        elif k == "e":
            head = e_motion(text, i)
        else:
            head = b_motion(text, i)
        if not ext:
            self.anchor = self.pos(i)
        self.cy, self.cx = self.pos(head)
        self.want_x = None

    def extend_line(self) -> None:
        lo, hi = self.sel_range()
        full = (self.has_selection() and lo[1] == 0
                and hi[1] == len(self.lines[hi[0]]))
        if full and hi[0] + 1 < len(self.lines):
            hi = (hi[0] + 1, len(self.lines[hi[0] + 1]))
        else:
            lo = (lo[0], 0)
            hi = (hi[0], len(self.lines[hi[0]]))
        self.anchor = lo
        self.cy, self.cx = hi
        self.want_x = None

    def find_char(self, ch: str, kind: str, n: int, ext: bool) -> None:
        line = self.lines[self.cy]
        i = self.cx
        for _ in range(n):
            if kind in ("f", "t"):
                j = line.find(ch, i + 1)
            else:
                j = line.rfind(ch, 0, max(0, i))
            if j < 0:
                self.flash(f"no '{ch}' on this line")
                return
            i = j
        if kind == "t":
            i -= 1
        elif kind == "T":
            i += 1
        if not ext:
            self.anchor = self.head()
        self.cx = max(0, min(i, len(line)))
        self.want_x = None

    def replace_chars(self, ch: str) -> None:
        self.snapshot("replace-char", group=False)
        lo, hi = self.sel_range()
        for y in range(lo[0], hi[0] + 1):
            line = self.lines[y]
            a = lo[1] if y == lo[0] else 0
            b = hi[1] if y == hi[0] else len(line) - 1
            b = min(b, len(line) - 1)
            if a <= b:
                self.lines[y] = line[:a] + ch * (b - a + 1) + line[b + 1:]

    def open_line(self, below: bool) -> None:
        self.snapshot("insert", group=False)
        y = self.cy
        indent = re.match(r"[ \t]*", self.lines[y]).group(0)
        if below:
            self.lines.insert(y + 1, indent)
            self.cy = y + 1
        else:
            self.lines.insert(y, indent)
        self.cx = len(indent)
        self.enter_insert(snap=False)

    def paste(self, after: bool) -> None:
        reg = self.register
        if not reg:
            self.flash("nothing yanked")
            return
        self.snapshot("paste", group=False)
        lo, hi = self.sel_range()
        if reg.endswith("\n"):                      # linewise
            if after:
                y = hi[0]
                if y + 1 < len(self.lines):
                    off = self.off(y + 1, 0)
                else:
                    off = len(self.text())
                    reg = "\n" + reg.rstrip("\n")
            else:
                off = self.off(lo[0], 0)
        else:
            off = self.off(*hi) + 1 if after else self.off(*lo)
        self.insert_text(off, reg, select=True)
        self.mode = "normal"

    def copy_clipboard(self) -> None:
        """Send the selection (ctrl keys: the current line) to the system
        clipboard, and yank it so p pastes the same text."""
        if self.keys == "helix":
            text = self.sel_text()
            what = f"{len(text)} chars"
        else:
            text = self.lines[self.cy] + "\n"
            what = f"line {self.cy + 1}"
        self.register = text
        tool = clipboard_copy(text)
        if tool:
            self.flash(f"copied {what} to the clipboard via {tool}")
        else:
            self.flash("no clipboard tool on PATH (clip.exe / pbcopy / wl-copy / xclip)")
        self.mode = "normal"

    def join_lines(self, n: int) -> None:
        lo, hi = self.sel_range()
        y0, y1 = lo[0], hi[0]
        if y0 == y1:
            y1 = min(y0 + n, len(self.lines) - 1)
        if y1 == y0:
            return
        self.snapshot("join", group=False)
        result = self.lines[y0]
        join_at = len(result)
        for l in self.lines[y0 + 1:y1 + 1]:
            l = l.lstrip()
            result = (result + " " + l) if (result and l) else result + l
        self.lines[y0:y1 + 1] = [result]
        self.cy, self.cx = y0, min(join_at, len(result))
        self.want_x = None
        self.collapse()

    def indent(self, more: bool) -> None:
        self.snapshot("indent", group=False)
        lo, hi = self.sel_range()
        ay, ax = self.anchor
        for y in range(lo[0], hi[0] + 1):
            line = self.lines[y]
            if more:
                delta = 2
                self.lines[y] = "  " + line
            else:
                delta = -min(2, len(line) - len(line.lstrip(" ")))
                self.lines[y] = line[-delta:] if delta else line
            # keep the selection over the same text (helix shifts it too)
            if y == self.cy and self.cx > 0:
                self.cx = max(0, self.cx + delta)
            if y == ay and ax > 0:
                ax = max(0, ax + delta)
        self.clamp_cursor()
        self.anchor = (ay, max(0, min(ax, len(self.lines[ay]))))

    def swap_case(self) -> None:
        self.snapshot("case", group=False)
        lo, hi = self.sel_range()
        text = self.text()
        a = self.off(*lo)
        b = min(len(text), self.off(*hi) + 1)
        self.set_text(text[:a] + text[a:b].swapcase() + text[b:])

    def hard_wrap(self, y0: int, y1: int) -> int:
        """Hard-wrap the lines y0..y1 (inclusive) that are wider than 64, in
        the buffer, exactly as the preview shows them. Returns how many."""
        y0, y1 = max(0, y0), min(y1, len(self.lines) - 1)
        text = self.text()
        mask = build.tag_mask(text)
        out: list[str] = []
        pos, n, new_cy = 0, 0, self.cy
        for i, line in enumerate(self.lines):
            m = mask[pos:pos + len(line)]
            pos += len(line) + 1
            rows = build.wrap_line(line, m) if y0 <= i <= y1 else [line]
            if len(rows) > 1:
                n += 1
                if i < self.cy:
                    new_cy += len(rows) - 1     # the cursor's line moved down
                elif i == self.cy:
                    self.cx = 0                 # its own line got split: start of it
            out.extend(rows)
        if not n:
            return 0
        self.snapshot("wrap", group=False)
        self.lines = out
        self.cy = new_cy
        self.clamp_cursor()
        self.collapse()
        self.mode = "normal"
        return n

    def hard_wrap_file(self, path: Path) -> int:
        """Hard-wrap a post's long body lines: in the buffer if it's open, else on disk."""
        if path == self.path:
            return self.hard_wrap(2, len(self.lines) - 1)
        text = path.read_text(encoding="utf-8")
        n = sum(1 for ln, _w in build.overruns(text) if ln > 2)
        if n:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(build.wrap_text(text, first=2))
        return n

    def fmt(self, arg: str | None = None) -> None:
        """:fmt hard-wraps the selected lines (a lone cursor: this line); :fmt all, the file."""
        if arg == "all":
            is_post = self.path is not None and self.path.parent in (POSTS, DRAFTS)
            y0, y1 = (2 if is_post else 0), len(self.lines) - 1
        elif arg:
            self.flash("usage: fmt [all]")
            return
        else:
            lo, hi = self.sel_range()
            y0, y1 = lo[0], hi[0]
        n = self.hard_wrap(y0, y1)
        self.flash(f"wrapped {n} long line(s) at {MAX_COLS}" if n
                   else f"nothing to wrap: those lines fit {MAX_COLS}")

    def run_command(self, cmd: str) -> None:
        parts = cmd.split()
        name, args = parts[0], parts[1:]
        if name.isdigit():
            self.goto_line(int(name))
            self.collapse()
        elif name in ("w", "write"):
            self.save()
        elif name in ("q", "quit"):
            self.quit()
        elif name in ("q!", "quit!"):
            self.quit(force=True)
        elif name in ("wq", "x", "wq!"):
            if self.save():
                self.quit(force=True)
        elif name in ("o", "open", "files"):
            self.picker()
        elif name in ("e", "edit"):
            if args:
                self.open_arg(args[0])
            else:
                self.picker()
        elif name in ("new", "draft"):
            self.new_draft(args[0] if args else None)
        elif name == "post":
            self.post_draft(when=args[0] if args else None)
        elif name == "unpost":
            self.unpost()
        elif name in ("fmt", "wrap"):
            self.fmt(args[0] if args else None)
        elif name in ("page", "new-page"):
            self.new_page(args[0] if args else None)
        elif name in ("p", "preview"):
            self.toggle_preview()
        elif name == "keys":
            if args and args[0] in ("ctrl", "helix"):
                self.keys = args[0]
                self.mode = "normal"
                self.in_insert = False
                self.collapse()
                self.flash(f"keys: {self.keys}")
            else:
                self.flash("usage: keys ctrl|helix")
        elif name in ("publish", "pub"):
            self.publish(" ".join(args) if args else None)
        elif name in ("status", "st"):
            self.status()
        elif name in ("copy", "clip"):
            self.copy_clipboard()
        elif name in ("h", "help"):
            self.help(args[0] if args else None)
        else:
            self.flash(f"unknown command: {name}")

    # -- main loop ------------------------------------------------------------

    def run(self) -> None:
        while self.running:
            self.draw()
            curses.doupdate()
            self.handle(self.read_key())
            # Drain whatever else is already buffered (a paste) before the
            # next redraw, and keep the whole burst as one undo step.
            self.scr.timeout(0)
            self.in_burst = True
            self.burst_snapped = False
            try:
                while self.running:
                    prev = self.last_raw
                    k = self.read_key()
                    if k is None:
                        break
                    if self.last_raw == "\n" and prev == "\r":
                        continue  # CRLF paste: one newline, not two
                    self.handle(k)
            finally:
                self.in_burst = False
                self.scr.timeout(-1)


# ---- main -------------------------------------------------------------------

def main(scr, path: Path | None, cfg: dict) -> bool:
    curses.raw()          # pass ^S/^Q/^Z/^C through instead of the tty eating them
    scr.keypad(True)
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(25)
    ed = Editor(scr, cfg)
    if path is not None:
        ed.open_path(path)
    else:
        ed.picker()
        if ed.path is None:
            return ed.colors.custom
    ed.run()
    return ed.colors.custom


def cli() -> None:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("args", nargs="*", metavar="FILE | new SLUG | page SLUG",
                    help="a file under src/pages, src/drafts or src/posts; "
                         "new SLUG starts a draft, page SLUG a page")
    ap.add_argument("--keys", choices=("ctrl", "helix"))
    ap.add_argument("--preview", choices=("site", "terminal"))
    ap.add_argument("--editor", choices=("site", "terminal"))
    ap.add_argument("--config", type=Path)
    ap.add_argument("--init-config", action="store_true")
    ap.add_argument("--man", action="store_true", help="print the cheat sheet and exit")
    ap.add_argument("--install", action="store_true", help="symlink this script as ~/.local/bin/lv")
    a = ap.parse_args()

    if a.man:
        print(man_text())
        return
    if a.install:
        install_symlink()
        return

    if a.init_config:
        p = a.config or default_config_path()
        if p.exists():
            sys.exit(f"{p} already exists; edit it directly")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(STARTER_CONFIG, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {p}")
        return

    cfg = load_config(a.config)
    if a.keys:
        cfg["keys"] = a.keys
    if a.preview:
        cfg["theme"]["preview"] = a.preview
    if a.editor:
        cfg["theme"]["editor"] = a.editor
    validate_config(cfg)

    path: Path | None = None
    try:
        if a.args and a.args[0] in ("new", "draft", "page"):
            if len(a.args) != 2:
                sys.exit(f"usage: {ap.prog} {a.args[0]} SLUG")
            slug = a.args[1]
            if a.args[0] in ("new", "draft"):
                if not POST_SLUG_RE.match(slug):
                    sys.exit("error: slug must be lowercase letters, digits, - or _")
                path = DRAFTS / f"{slug}.txt"
            else:
                if not PAGE_SLUG_RE.match(slug):
                    sys.exit("error: page slug must start with a letter; lowercase, digits, - or _")
                path = PAGES / f"{slug}.txt"
        elif len(a.args) == 1:
            path = resolve_arg(a.args[0])
        elif a.args:
            sys.exit(f"usage: {ap.prog} [FILE | new SLUG | page SLUG]")
    except ValueError as e:
        sys.exit(f"error: {e}")
    custom = curses.wrapper(main, path, cfg)
    if custom:
        # we redefined palette slots 16..27; hand the terminal its defaults back
        sys.stdout.write("\x1b]104\x1b\\")
        sys.stdout.flush()


if __name__ == "__main__":
    cli()
