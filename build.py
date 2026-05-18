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
# SSR a placeholder; the client-side JS in template.html fetches /status.json
# (written by status_check.py) and replaces this span with real state.

SEP = '<span class="d">' + " ".join(["-"] * 32) + "</span>"

SYSTEMS_PLACEHOLDER = (
    f'<span id="systems-block">{SEP}\n\n'
    f'// systems                                     <span class="d">..</span>\n\n'
    f'  <span class="d">fetching...</span>\n\n{SEP}</span>'
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

POST_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.txt$")


def output_path(slug: str) -> Path:
    return DIST / "index.html" if slug == "index" else DIST / slug / "index.html"


def render_page(template: str, config: dict, current_slug: str, body: str) -> str:
    nav_html = render_nav(config["nav"], current_slug)
    socials_html = render_inline(html.escape(config.get("socials", ""), quote=False))
    return (template
            .replace("{{title}}", html.escape(config.get("title", "")))
            .replace("{{nav}}", nav_html)
            .replace("{{body}}", body)
            .replace("{{socials}}", socials_html))


def build_page(slug: str, config: dict, template: str) -> None:
    src = (SRC / "pages" / f"{slug}.txt").read_text(encoding="utf-8")
    body = render_source(src)
    body = body.replace("{systems}", SYSTEMS_PLACEHOLDER)

    page = render_page(template, config, slug, body)

    out = output_path(slug)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"  wrote {out.relative_to(ROOT)}")


# ---- posts ------------------------------------------------------------------

def discover_posts() -> list[dict]:
    posts_dir = SRC / "posts"
    if not posts_dir.is_dir():
        return []
    posts: list[dict] = []
    for path in posts_dir.glob("*.txt"):
        m = POST_NAME_RE.match(path.name)
        if not m:
            print(f"  skip (bad name): {path.name}")
            continue
        date, slug = m.group(1), m.group(2)
        lines = path.read_text(encoding="utf-8").splitlines()
        title = lines[0].strip() if lines else slug
        body = "\n".join(lines[2:]) if len(lines) > 2 else ""
        posts.append({"date": date, "slug": slug, "title": title, "body": body})
    posts.sort(key=lambda p: p["date"], reverse=True)
    return posts


def build_post(post: dict, config: dict, template: str) -> None:
    header = (
        f'<span class="hdr">{html.escape(post["title"])}</span>\n'
        f'<span class="d">{post["date"]}</span>\n\n'
    )
    body = header + render_source(post["body"])

    page = render_page(template, config, "posts", body)

    out = DIST / "posts" / post["slug"] / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"  wrote {out.relative_to(ROOT)}")


def render_posts_index(posts: list[dict]) -> str:
    """Render the body of the /posts/ page: title + right-aligned date per row."""
    if not posts:
        return '<span class="d">no posts yet.</span>'
    lines = [
        '// posts                                                      <span class="d">01</span>',
        '',
    ]
    for p in posts:
        title = html.escape(p["title"])
        link = f'<a href="/posts/{p["slug"]}/">{title}</a>'
        vis = len(p["title"])
        pad = max(2, 64 - vis - len(p["date"]))
        lines.append(link + " " * pad + p["date"])
    return "\n".join(lines)


def build_posts_index(posts: list[dict], config: dict, template: str) -> None:
    body = render_posts_index(posts)
    page = render_page(template, config, "posts", body)
    out = DIST / "posts" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"  wrote {out.relative_to(ROOT)}")


def main() -> None:
    config = json.loads((SRC / "config.json").read_text(encoding="utf-8"))
    template = (SRC / "template.html").read_text(encoding="utf-8")

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()

    for slug in config["pages"]:
        build_page(slug, config, template)

    posts = discover_posts()
    if posts or (SRC / "posts").is_dir():
        for post in posts:
            build_post(post, config, template)
        build_posts_index(posts, config, template)

    htaccess_src = ROOT / "htaccess"
    if htaccess_src.exists():
        shutil.copy(htaccess_src, DIST / ".htaccess")
        print(f"  wrote dist/.htaccess")

    print("done.")


if __name__ == "__main__":
    main()
