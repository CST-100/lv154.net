#!/usr/bin/env python3
"""Status checker + heartbeat receiver for the lv154 systems block.

Runs two things in one process:
  - HTTP receiver on 127.0.0.1:HB_PORT. nginx proxies /_hb/<secret>/<name>
    to /<secret>/<name> here. On match, writes /data/heartbeats/<name>.epoch
    with the current unix timestamp.
  - Background loop every CHECK_INTERVAL seconds: probes each system per
    systems_config.json, writes /usr/share/nginx/html/status.json.

State derivation per system:
  self      → ok, last_seen=now (we're alive iff this process is)
  tcp       → ok if TCP connect succeeds, else down (no last_seen update)
  heartbeat → ok if last heartbeat <= ok_seconds ago
              idle if <= idle_seconds ago
              down otherwise (or if never seen)
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("SYSTEMS_CONFIG", "/app/src/systems_config.json"))
STATUS_OUT = Path(os.environ.get("STATUS_OUT", "/usr/share/nginx/html/status.json"))
HEARTBEAT_DIR = Path(os.environ.get("HEARTBEAT_DIR", "/data/heartbeats"))
SECRET = os.environ.get("HEARTBEAT_SECRET", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
HB_HOST = "127.0.0.1"
HB_PORT = int(os.environ.get("HB_PORT", "8765"))


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[status_check {ts}] {msg}", flush=True)


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


# --- heartbeat receiver -----------------------------------------------------

class HBHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # path: /<secret>/<name>
        if not SECRET:
            return self._reply(503)
        parts = self.path.strip("/").split("/")
        if len(parts) != 2:
            return self._reply(404)
        secret, name = parts
        if secret != SECRET:
            return self._reply(401)
        if not name or any(c in name for c in "/.\\"):
            return self._reply(400)
        HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
        (HEARTBEAT_DIR / f"{name}.epoch").write_text(str(int(time.time())))
        return self._reply(204)

    def log_message(self, fmt: str, *args) -> None:
        log(f"hb {self.client_address[0]} {fmt % args}")

    def _reply(self, code: int) -> None:
        self.send_response(code)
        self.end_headers()


def serve_heartbeats() -> None:
    server = ThreadingHTTPServer((HB_HOST, HB_PORT), HBHandler)
    log(f"heartbeat receiver listening on {HB_HOST}:{HB_PORT}")
    server.serve_forever()


# --- probes -----------------------------------------------------------------

def tcp_check(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_heartbeat(name: str) -> int | None:
    p = HEARTBEAT_DIR / f"{name}.epoch"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def state_for_age(age_s: int, ok_s: int, idle_s: int) -> str:
    if age_s <= ok_s:
        return "ok"
    if age_s <= idle_s:
        return "idle"
    return "down"


def resolve_system(system: dict, now: int, thresholds: dict) -> dict:
    name = system["name"]
    check = system.get("check", {"type": "self"})
    typ = check.get("type", "self")
    out = {"name": name, "desc": system.get("desc", ""), "state": "down"}
    if typ == "self":
        out["state"] = "ok"
        out["last_seen"] = iso(now)
    elif typ == "tcp":
        ok = tcp_check(
            check["host"],
            int(check["port"]),
            float(check.get("timeout", 3)),
        )
        if ok:
            out["state"] = "ok"
            out["last_seen"] = iso(now)
    elif typ == "heartbeat":
        last = read_heartbeat(name)
        if last is not None:
            age = now - last
            out["state"] = state_for_age(age, thresholds["ok_seconds"], thresholds["idle_seconds"])
            out["last_seen"] = iso(last)
    else:
        log(f"unknown check type for {name!r}: {typ}")
    return out


def write_status() -> None:
    cfg = json.loads(CONFIG_PATH.read_text())
    thresholds = cfg.get("thresholds", {"ok_seconds": 300, "idle_seconds": 3600})
    now = int(time.time())
    systems = [resolve_system(s, now, thresholds) for s in cfg["systems"]]
    payload = {"updated": iso(now), "systems": systems}
    STATUS_OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATUS_OUT)


def check_loop() -> None:
    while True:
        try:
            write_status()
        except Exception as exc:
            log(f"check error: {exc}")
        time.sleep(CHECK_INTERVAL)


# --- entry ------------------------------------------------------------------

def main() -> None:
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"config={CONFIG_PATH} out={STATUS_OUT} hb_dir={HEARTBEAT_DIR} interval={CHECK_INTERVAL}s")
    if not SECRET:
        log("WARNING: HEARTBEAT_SECRET not set; receiver will return 503 for all requests")
    try:
        write_status()
        log("initial status written")
    except Exception as exc:
        log(f"initial status error: {exc}")
    threading.Thread(target=check_loop, daemon=True).start()
    serve_heartbeats()


if __name__ == "__main__":
    main()
