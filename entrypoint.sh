#!/bin/sh
# Self-updating entrypoint: clone (or pull) the repo, build, sync to nginx
# webroot, then loop in the background re-checking every POLL_INTERVAL seconds.
# Nginx runs in the foreground as the container's main process.
set -eu

REPO_URL="${REPO_URL:-https://github.com/CST-100/lv154.net.git}"
REPO_REF="${REPO_REF:-main}"
POLL_INTERVAL="${POLL_INTERVAL:-300}"
APP_DIR="/app"
WEB_ROOT="/usr/share/nginx/html"

git config --global --add safe.directory "$APP_DIR"

log() { echo "[entrypoint $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

fetch_repo() {
  if [ -d "$APP_DIR/.git" ]; then
    log "fetching $REPO_REF"
    git -C "$APP_DIR" fetch --depth=1 origin "$REPO_REF"
    git -C "$APP_DIR" reset --hard "origin/$REPO_REF"
  else
    log "cloning $REPO_URL ($REPO_REF) into $APP_DIR"
    rm -rf "$APP_DIR"
    git clone --depth=1 --branch "$REPO_REF" "$REPO_URL" "$APP_DIR"
  fi
}

build_and_sync() {
  log "building"
  rm -rf "$APP_DIR/dist"
  (cd "$APP_DIR" && python3 build.py)
  log "syncing to $WEB_ROOT"
  mkdir -p "$WEB_ROOT"
  # status.json is owned by status_check.py; never let rsync delete it.
  rsync -a --delete --exclude=status.json "$APP_DIR/dist/" "$WEB_ROOT/"
}

current_sha() {
  git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo "none"
}

# Initial deploy
fetch_repo
build_and_sync
log "initial deploy complete at $(current_sha)"

# Background git poll loop
(
  while :; do
    sleep "$POLL_INTERVAL"
    before=$(current_sha)
    if ! git -C "$APP_DIR" fetch --depth=1 origin "$REPO_REF" 2>&1 | sed 's/^/[fetch] /'; then
      log "fetch failed; will retry next cycle"
      continue
    fi
    after=$(git -C "$APP_DIR" rev-parse "origin/$REPO_REF")
    if [ "$before" != "$after" ]; then
      log "new commit $before -> $after; redeploying"
      git -C "$APP_DIR" reset --hard "origin/$REPO_REF"
      build_and_sync
      log "redeploy complete at $after"
    fi
  done
) &

# Status checker + heartbeat receiver (writes status.json, owns /data/heartbeats)
log "starting status_check.py"
python3 "$APP_DIR/status_check.py" &

log "starting nginx (git poll every ${POLL_INTERVAL}s, status check every ${CHECK_INTERVAL:-60}s)"
exec nginx -g 'daemon off;'
