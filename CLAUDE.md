# lavieohana.com / lv154.net — v5

Custom Python static site generator. No deps. Outputs `dist/` from `src/`.

## Build

```
make build         # python3 build.py → dist/
make serve         # build + serve dist/ on :8000
make docker-up     # build image + run via docker-compose
make deploy        # build + rsync dist/ to Dreamhost (needs DH_USER/DH_HOST/DH_PATH env)
```

## Layout

- `src/config.json` — site title, nav (slug/label/href), `pages` list, socials line.
- `src/template.html` — outer chrome (header, nav, body slot, socials, systems-block JS).
- `src/pages/<slug>.txt` — page bodies. Only slugs listed in `config.pages` are built.
- `src/posts/<YYYY-MM-DD-slug>.txt` — blog posts. Filename encodes date+slug. First line = title, line 2 blank, rest = body. Auto-discovered; not in `config.pages`.
- `src/status.json` — live systems block data. Bind-mounted into the container so host-side cron can update it without rebuilding.

Output:
- `dist/index.html` for `index` slug.
- `dist/<slug>/index.html` for every other page/post.
- `dist/posts/index.html` auto-generated index of posts (newest first).
- `dist/status.json` copied through for the client-side fetch in the systems block.

## Token DSL (works in pages and posts)

The body text is HTML-escaped, then these tokens expand:

| Token | Becomes |
| --- | --- |
| `{g}…{/g}` | `<span class="g">…</span>` — green |
| `{d}…{/d}` | dim grey (used for section numbers, separators, `<EOM>`) |
| `{hdr}…{/hdr}` | header green (header lines) |
| `{trans}word{/trans}` | per-character trans-flag color cycle (tb/tp/tw/tp/tb) |
| `[text](url)` | anchor tag |
| `{systems}` | full live-systems block (rendered server-side from `src/status.json`, re-fetched client-side) |

Pipeline lives in `build.py` (`render_source` → `render_inline`). `{systems}` is substituted after inline rendering and only on pages that contain it.

## Editing

Two local editors, both showing per-line *visible* width (markup stripped) against the 64-col limit and a rendered preview:

- `make tui` — curses TUI (`tools/tui.py`, stdlib only). Source pane with highlighted markup + gutter widths on the left, rendered preview on the right. `make tui FILE=posts/2026-05-17-hello.txt` opens a file directly; no arg opens the file picker. `make tui ARGS="--keys helix"` passes flags. F1 (or `:help [about|markup|ctrl|helix]`) opens a scrollable cheat sheet; `python3 tools/tui.py --man` prints it.
  - Two key styles: `ctrl` (default: `^S` save, `^O` files, `^N` new post, `^F` find, `^P` preview, `^Z`/`^R` undo/redo, `^Q` quit) and `helix` (modal: `h j k l w b e x d c y p u U`, `i a o`, `/`, `:w :q :e :new :page :keys`, `space-f` files). `^S ^O ^P ^Q F1` work in both.
  - Theme: preview pane paints the site's fg/bg by default; editor pane uses the terminal's colours. Config at `~/.config/lv154/tui.json` (or `$LV154_TUI_CONFIG`; `--init-config` writes a starter): `keys`, `theme.preview`/`theme.editor` (`site`|`terminal`), `palette` (`#rrggbb` overrides). Flags `--keys --preview --editor --config` override the file.
  - Publish from inside: `F2` / `:publish [message]` saves, commits **only** `src/pages` + `src/posts`, pushes `main` (refuses on other branches). `F3` / `:status` shows pending content changes. Unrelated changes elsewhere in the tree are never swept in.
  - One command from anywhere: `python3 tools/tui.py --install` symlinks `~/.local/bin/lv` to the script (re-run after moving the repo). Then `lv`, `lv posts/<file>.txt`, `lv new <slug>` (today's post, created on first save), `lv page <slug>`.
- `make edit` — browser version (`tools/edit.py` + `tools/edit.html`) on `127.0.0.1:8001`.

Width logic (`visible_text`, `line_widths`, `overruns`) lives in `build.py`; the build prints a `warn:` line for any source line wider than 64 visible columns but does not fail.

## Adding things

**New static page** (e.g. resume):
1. Write `src/pages/resume.txt`.
2. Append `"resume"` to `config.pages`.
3. Set the matching nav entry's `href` to `/resume/`.
4. `make build`.

**New post:**
1. Drop `src/posts/2026-05-17-some-slug.txt` (first line = title, blank line, body).
2. `make build`. It shows up at `/posts/some-slug/` and on `/posts/`.

## Deploy

- **Primary (unraid)**: self-updating container. The image bundles nginx + git + python3 + `entrypoint.sh`. On startup the container clones this repo, runs `build.py`, syncs `dist/` to the nginx webroot, then loops in the background pulling every `POLL_INTERVAL` seconds (default 300). Traefik routes `lv154.net`/`www.lv154.net` to it as before.
- **Secondary (Dreamhost)**: `deploy.sh` rsyncs a locally-built `dist/` to the configured Dreamhost path. Source env from a gitignored `.env` before running.

### How auto-deploy works

- `git push` to `main` on GitHub → the unraid container picks up the new commit on its next poll (≤5 min) and rebuilds in place.
- `docker restart lv154-site` forces an immediate pull + rebuild — useful if you want the update *now*.
- Tail deploy events: `docker logs -f lv154-site`. Each redeploy logs the before/after SHA.
- Tunable via env on the compose service: `REPO_URL`, `REPO_REF` (default `main`), `POLL_INTERVAL` (seconds).

### What does *not* auto-update

Only the *site source* is pulled at runtime. These are baked into the image and need a manual rebuild (`docker compose up -d --build` on the unraid host):

- `Dockerfile`
- `entrypoint.sh`
- `nginx.conf`
- `docker-compose.yml` (it's not even in the image — it lives on the host)

When you change any of those, copy the new versions to `/mnt/user/appdata/lv154dotnet/` on unraid and run `docker compose up -d --build`.

## Systems block (live status)

The status block on the home page is driven by `status_check.py`, which runs as a background process inside the container. It does two things:

1. **Probes/checks each system** every `CHECK_INTERVAL` seconds (default 60) per `src/systems_config.json`, then writes `/usr/share/nginx/html/status.json`. The client-side JS in `template.html` fetches that file and renders the block.
2. **Receives heartbeats** on a private port. nginx proxies `GET /_hb/<secret>/<name>` → `127.0.0.1:8765`; if `<secret>` matches `HEARTBEAT_SECRET`, the receiver writes `/data/heartbeats/<name>.epoch` with the current unix time.

Heartbeat data lives in the `lv154-heartbeats` docker volume so it survives container restarts.

### Check types

In `src/systems_config.json` each system has a `check` block:

- `{"type": "self"}` — always `ok` (used for cygnus, which *is* the host running the container).
- `{"type": "tcp", "host": "...", "port": N, "timeout": 3}` — TCP connect probe. `ok` if it connects, `down` otherwise.
- `{"type": "heartbeat"}` — derive state from the last heartbeat timestamp:
  - `<= thresholds.ok_seconds` → `ok`
  - `<= thresholds.idle_seconds` → `idle`
  - older / never seen → `down`

### Setting up heartbeats on a laptop

Pick a name matching `systems_config.json` (e.g. `delta-IV`), then drop this on the laptop (linux/macos) and run every couple of minutes via cron or launchd:

```bash
#!/bin/sh
# /usr/local/bin/lv154-heartbeat.sh
SECRET="<paste HEARTBEAT_SECRET>"
NAME="delta-IV"
curl -sf --max-time 5 "https://lv154.net/_hb/${SECRET}/${NAME}" >/dev/null || true
```

Cron line:
```
*/2 * * * * /usr/local/bin/lv154-heartbeat.sh
```

This works from anywhere the laptop has internet (home, coffee shop, hotel, VPN — all fine). The laptop only goes `idle`/`down` when it has no internet at all.

### Setting up the secret on unraid

The secret lives in `/mnt/user/appdata/lv154dotnet/.env` (gitignored). Generate once:

```bash
echo "HEARTBEAT_SECRET=$(openssl rand -hex 24)" >> /mnt/user/appdata/lv154dotnet/.env
docker compose up -d
```

Rotation: change the value in `.env`, `docker compose up -d`, update each laptop's script.

## Notes / Gotchas

- There's a live copy on the unraid box at `root@192.168.1.150:/mnt/user/appdata/lv154dotnet`. It's now a real git clone — `git pull` on the host picks up Dockerfile/entrypoint/compose changes. Site source updates flow through the container automatically.
- `dist/status.json` is NOT generated by `build.py` anymore; it's written exclusively by `status_check.py`. The entrypoint's rsync excludes `status.json` so build/redeploy never clobbers the live state.
- Column widths in the systems block and posts index are hand-tuned for 64 cols. Keep titles short or adjust the padding math if you go wider.
