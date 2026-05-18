#!/usr/bin/env python3
"""Local-only editor server for src/pages/*.txt and src/posts/*.txt.

Run with `make edit` (from v5/). Binds 127.0.0.1:8001 by default; never
exposed externally. The HTML UI lives in tools/edit.html and talks to a
small REST surface here:

  GET  /                          -> tools/edit.html
  GET  /api/files                 -> {pages: [...], posts: [...]}
  GET  /api/file?path=...         -> raw file content (text/plain)
  PUT  /api/file?path=...         -> write file content (request body)
  DELETE /api/file?path=...       -> delete file

All paths must resolve under src/pages/ or src/posts/. Filenames must
match the existing schemas (slug.txt for pages, YYYY-MM-DD-slug.txt for
posts). No git ops — commit and push by hand once you're happy.
"""
from __future__ import annotations

import json
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PAGES = SRC / "pages"
POSTS = SRC / "posts"
UI_PATH = ROOT / "tools" / "edit.html"

HOST = "127.0.0.1"
PORT = 8001

PAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*\.txt$")
POST_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9_-]*\.txt$")


def resolve_path(rel: str) -> Path:
    """Resolve `rel` against SRC, rejecting traversal and bad names."""
    if not rel or ".." in rel.split("/") or rel.startswith("/"):
        raise ValueError("invalid path")
    target = (SRC / rel).resolve()
    try:
        target.relative_to(SRC.resolve())
    except ValueError:
        raise ValueError("path escapes src/")
    parts = target.relative_to(SRC.resolve()).parts
    if len(parts) != 2:
        raise ValueError("expected src/pages/<file>.txt or src/posts/<file>.txt")
    bucket, name = parts
    if bucket == "pages":
        if not PAGE_NAME_RE.match(name):
            raise ValueError("page name must be lowercase slug + .txt")
    elif bucket == "posts":
        if not POST_NAME_RE.match(name):
            raise ValueError("post name must be YYYY-MM-DD-slug.txt")
    else:
        raise ValueError("only src/pages or src/posts are editable")
    return target


def list_files() -> dict:
    PAGES.mkdir(parents=True, exist_ok=True)
    POSTS.mkdir(parents=True, exist_ok=True)
    return {
        "pages": sorted(p.name for p in PAGES.glob("*.txt")),
        "posts": sorted((p.name for p in POSTS.glob("*.txt")), reverse=True),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _path_param(self) -> Path:
        q = parse_qs(urlparse(self.path).query)
        if "path" not in q:
            raise ValueError("missing ?path=")
        return resolve_path(q["path"][0])

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        sys.stderr.write(f"[edit] {self.address_string()} - {fmt % args}\n")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            try:
                self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, b"edit.html missing")
            return
        if url.path == "/api/files":
            self._send_json(200, list_files())
            return
        if url.path == "/api/file":
            try:
                target = self._path_param()
            except ValueError as e:
                return self._send(400, str(e).encode())
            if not target.exists():
                return self._send(404, b"not found")
            return self._send(200, target.read_bytes())
        self._send(404, b"not found")

    def do_PUT(self) -> None:
        if urlparse(self.path).path != "/api/file":
            return self._send(404, b"not found")
        try:
            target = self._path_param()
        except ValueError as e:
            return self._send(400, str(e).encode())
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        self._send_json(200, {"ok": True, "bytes": len(body)})

    def do_DELETE(self) -> None:
        if urlparse(self.path).path != "/api/file":
            return self._send(404, b"not found")
        try:
            target = self._path_param()
        except ValueError as e:
            return self._send(400, str(e).encode())
        if target.exists():
            target.unlink()
        self._send_json(200, {"ok": True})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"editor: {url}  (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")


if __name__ == "__main__":
    main()
