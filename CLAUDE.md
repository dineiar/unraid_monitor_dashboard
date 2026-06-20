# CLAUDE.md

Instructions for AI agents working in this repository.

## Project
A decoupled, two-tier home-server dashboard for Unraid:
- **Collector (Tier 1, `collector.py`)** — Python 3 script run on the Unraid host
  (User Scripts, cron). Gathers system/array/network/Docker/routine metrics and
  writes `metrics.json`.
- **Dashboard (Tier 2, `index.html`)** — vanilla single-file web app served by a
  hardened, unprivileged nginx container that reads `metrics.json` read-only.

## Layout
- `collector.py` — collector; edit `CONFIG`, `APPS`, `HEALTHCHECKS` at the top.
- `index.html` — dashboard; polls `data/metrics.json`, falls back to the
  `EMBEDDED` snapshot when it can't be fetched.
- `Dockerfile`, `nginx/default.conf`, `docker-compose.yml` — the container.
- `README.md` — setup and usage.

## Critical invariant
`collector.py` must emit the EXACT JSON schema `index.html` consumes (documented
in the `EMBEDDED` object and its comment). Change both together.

## Do not break
- **Vanilla frontend**: one self-contained `index.html`, no frameworks, no build step.
- **Hardened container**: unprivileged (uid 101), `cap_drop: ALL`, `read_only`,
  no `docker.sock`, no host networking; only the metrics dir is mounted read-only.
- **No secrets in the repo**: `HEALTHCHECKS_API_KEY`, `HEALTHCHECKS_UUID_*`, and
  `GITHUB_TOKEN` come from the environment (host `collector.env`, gitignored).
  Never commit keys, UUIDs, or personal IPs/hostnames.

## Verifying (no Unraid host required)
- `python3 -m py_compile collector.py`.
- The collector degrades gracefully off-Unraid; import it and unit-test functions directly.
- Frontend: eval `index.html`'s `<script>` in a minimal Node DOM stub and assert
  on the rendered HTML.

## Build & deploy
- `index.html` is baked into the image → frontend changes need an image rebuild +
  push; `collector.py` is host-side → no rebuild.
- The image is `linux/amd64` (Unraid is x86-64):
  ```bash
  docker buildx build --platform linux/amd64 \
    -t ghcr.io/dineiar/unraid_monitor_dashboard:latest --load .
  ```

## Commits
- Use **Conventional Commits** (https://www.conventionalcommits.org/): `feat`,
  `fix`, `chore`, `docs`, `build`, etc., with optional scopes
  (e.g. `chore(compose): ...`).
- Default branch: `main`.

## Release workflow
**Never run this unless the user explicitly asks for a release, and always
confirm the exact version number with the user before proceeding.** Then, in order:
1. Build the image tagged with the `v`-prefixed semantic version (e.g. `vX.Y.Z`).
2. Tag that same image `latest`.
3. Push both tags to GHCR (`ghcr.io/dineiar/unraid_monitor_dashboard`).
4. Tag the commit with the same `vX.Y.Z` and push the tag.
5. Create a GitHub release pointing at that tag (`gh release create`).
