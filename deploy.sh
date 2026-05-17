#!/usr/bin/env bash
# rsync the built site to Dreamhost.
#
# Configure via environment (e.g. in a gitignored .env you source manually):
#   DH_USER  — Dreamhost SSH username
#   DH_HOST  — Dreamhost SSH host (e.g. iad1-shared-foo.dreamhost.com)
#   DH_PATH  — webroot path on the server (e.g. /home/<user>/lavieohana.com)
#
# Usage:
#   source .env && ./deploy.sh
set -euo pipefail

: "${DH_USER:?set DH_USER}"
: "${DH_HOST:?set DH_HOST}"
: "${DH_PATH:?set DH_PATH}"

if [ ! -d dist ]; then
  echo "dist/ not found — run 'make build' first" >&2
  exit 1
fi

rsync -avz --delete dist/ "${DH_USER}@${DH_HOST}:${DH_PATH}/"
