#!/usr/bin/env python3
"""lavieohana.com static build. No deps.

Pipeline per page:
  raw text -> html.escape -> expand {trans} -> expand {g}/{d}/{hdr}
           -> expand [text](url) -> expand {systems} -> inject into template
"""
from __future__ import annotations

import html
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "src"
DIST = ROOT / "dist"

TRANS_CYCLE = ["tb", "tp", "tw", "tp", "tb"]

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
SPAN_RE = re.compile(r"\{(g|d|hdr)\}(.*?)\{/\1\}", re.DOTALL)
TRANS_RE = re.compile(r"\{trans\}(.*?)\{/trans\}", re.DOTALL)


def expand_trans(text: str) -> str:
    def sub(m: re.Match) -> str:
        word = m.group(1)
        out = []
        for i, ch in enumerate(word):
            cls = TRANS_CYCLE[i % len(TRANS_CYCLE)]
            out.append(f'<span class="{cls}">{ch}</span>')
        return "".join(out)
    return TRANS_RE.sub(sub, text)


def expand_spans(text: str) -> str:
    return SPAN_RE.sub(lambda m: f'<span class="{m.group(1)}">{m.group(2)}</span>', text)


def expand_links(text: str) -> str:
    return LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)


def render_inline(text: str) -> str:
    """Apply the inline token pipeline (no {systems}, no escape)."""
    text = expand_trans(text)
    text = expand_spans(text)
    text = expand_links(text)
    return text


def render_source(text: str) -> str:
    """Escape + apply inline tokens. For body content."""
    return render_inline(html.escape(text, quote=False))


# ---- systems block ----------------------------------------------------------

SEP = '<span class="d">' + " ".join(["-"] * 32) + "</span>"


def fmt_seen(iso: str) -> str:
    # accept "2026-05-04T14:20Z" or "...+00:00"
    s = iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return iso
    return f"{dt.month:02d}/{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}Z"


def state_badge(state: str) -> str:
    cls = {"ok": "ok", "idle": "idle"}.get(state, "err")
    return f'<span class="{cls}">■ {state.upper()}</span>'


def render_systems(status: dict) -> str:
    lines: list[str] = []
    systems = status.get("systems", [])
    for i, sys in enumerate(systems):
        if i > 0:
            lines.append("")
        name = sys["name"]
        desc = sys.get("desc", "")
        state = sys.get("state", "err")
        # line 1: indent + name + spaces + desc (name col width 22 incl. leading 2 sp)
        pre = f'  <span class="g">{name}</span>'
        pre_vis = 2 + len(name)
        pad = max(1, 22 - pre_vis)
        lines.append(pre + " " * pad + desc)
        # line 2: state badge right-padded to 64 cols total with last_seen/uptime
        badge = state_badge(state)
        badge_vis = 2 + len(state)  # "■ STATE"
        right = sys["uptime"] + " UPTIME" if sys.get("uptime") else (
            fmt_seen(sys["last_seen"]) if sys.get("last_seen") else ""
        )
        left_pad = 21
        sp = max(3, 64 - left_pad - badge_vis - len(right))
        lines.append(" " * left_pad + badge + " " * sp + right)
        # services
        services = sys.get("services") or []
        if services:
            ok_n = sum(1 for s in services if s.get("state") == "ok")
            services_label = "      services:"
            counter = f"({ok_n}/{len(services)} OK)"
            sp_hdr = max(3, 64 - len(services_label) - len(counter))
            lines.append(services_label + " " * sp_hdr + counter)
            for svc in services:
                label = "           - " + svc["name"]
                b = state_badge(svc["state"])
                bv = 2 + len(svc["state"])
                sp2 = max(1, 64 - len(label) - bv)
                lines.append(label + " " * sp2 + b)
    body = "\n".join(lines)
    return (
        f'<span id="systems-block">{SEP}\n\n'
        f'// systems                                     <span class="d">06</span>\n\n'
        f"{body}\n\n{SEP}</span>"
    )


# ---- nav -------------------------------------------------------------------

def render_nav(nav_cfg: list[dict], current_slug: str) -> str:
    parts = []
    for item in nav_cfg:
        label = item["label"]
        if item["slug"] == current_slug:
            parts.append(label)
        else:
            parts.append(f'<a href="{item["href"]}">{label}</a>')
    return "  ".join(parts)


# ---- pages ------------------------------------------------------------------

def output_path(slug: str) -> Path:
    return DIST / "index.html" if slug == "index" else DIST / slug / "index.html"


def build_page(slug: str, config: dict, template: str, systems_html: str) -> None:
    src = (SRC / "pages" / f"{slug}.txt").read_text(encoding="utf-8")
    body = render_source(src)
    body = body.replace("{systems}", systems_html)

    nav_html = render_nav(config["nav"], slug)
    socials_html = render_inline(html.escape(config.get("socials", ""), quote=False))

    page = (template
            .replace("{{title}}", html.escape(config.get("title", "")))
            .replace("{{nav}}", nav_html)
            .replace("{{body}}", body)
            .replace("{{socials}}", socials_html))

    out = output_path(slug)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"  wrote {out.relative_to(ROOT)}")


def main() -> None:
    config = json.loads((SRC / "config.json").read_text(encoding="utf-8"))
    template = (SRC / "template.html").read_text(encoding="utf-8")
    status = json.loads((SRC / "status.json").read_text(encoding="utf-8"))
    systems_html = render_systems(status)

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()

    for slug in config["pages"]:
        build_page(slug, config, template, systems_html)

    shutil.copy(SRC / "status.json", DIST / "status.json")
    print(f"  wrote dist/status.json")

    htaccess_src = ROOT / "htaccess"
    if htaccess_src.exists():
        shutil.copy(htaccess_src, DIST / ".htaccess")
        print(f"  wrote dist/.htaccess")

    print("done.")


if __name__ == "__main__":
    main()
