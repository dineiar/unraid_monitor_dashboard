# Unraid Monitor Dashboard

A **decoupled, two-tier** home-server dashboard for Unraid.

```
┌─ Collector ── Unraid HOST (User Scripts, cron */5) ─────────────┐
│  collector.py  →  gathers system/array/network/docker/routine   │
│                   metrics  →  writes  metrics.json (atomic)     │
└──────────────────────────────┬──────────────────────────────────┘
                               │  (read-only bind mount)
┌──────────────────────────────▼──────────────────────────────────┐
│  Dashboard ── nginx-unprivileged CONTAINER (sandboxed)          │
│  serves index.html  +  reads data/metrics.json  →  browser      │
│  NO docker.sock · NO host net · NO privileges · read-only FS    │
└─────────────────────────────────────────────────────────────────┘
```

The container is a **dumb static file server**. All discovery/collection happens
on the host in `collector.py`. The container never gets `docker.sock`, host
networking, capabilities, or a writable filesystem — its only host touch-point
is a single **read-only** directory containing `metrics.json`.

## Files

| File | Purpose |
|------|---------|
| `index.html` | Vanilla HTML/CSS/JS dashboard. Polls `data/metrics.json`, falls back to an embedded snapshot. |
| `nginx/default.conf` | Hardened vhost: listens on 8080, never caches `metrics.json`. |
| `Dockerfile` | Builds on `nginxinc/nginx-unprivileged:alpine` (uid 101). |
| `docker-compose.yml` | `cap_drop: ALL`, `no-new-privileges`, `read_only`, tmpfs scratch. |
| `collector.py` | Host metrics collector. Emits the exact schema the frontend expects. |

## Setup

### Host Collector

1. Copy `collector.py` to `/mnt/user/appdata/dashboard/collector.py`.
2. Install optional deps (recommended via venv — see the header comment in
   `collector.py` for Dev Pack / virtualenv instructions):
   ```bash
   python3 -m venv /mnt/user/appdata/dashboard/venv
   /mnt/user/appdata/dashboard/venv/bin/pip install psutil speedtest-cli
   ```
3. **Edit the `CONFIG`, `APPS`, and `HEALTHCHECKS` blocks** at the top of
   `collector.py` (paths, network interface, your Healthchecks.io check slugs +
   routine metadata). Container-name regexes in `APPS` match the examples in the
   prompt — tweak them to your actual container names. **Do not put your
   Healthchecks API key here** — it is read from `$HEALTHCHECKS_API_KEY` (see
   *Loading secrets from an env file* below).
4. In the **User Scripts** plugin, add a script with a custom schedule of
   `*/5 * * * *` that runs the wrapper from the next section (it sources the env
   file, then execs the collector). The speed test self-throttles to every 4 h
   regardless. Run it once manually first so `metrics.json` exists before the
   container starts.

#### Loading secrets from an env file

`collector.py` reads the Healthchecks API key, each routine's check UUID, and
the optional `GITHUB_TOKEN` from the environment, so secrets stay in a separate
file instead of in the script. The User Script sources that file into memory,
then launches the collector.

The per-routine UUID env var names are referenced directly in the
`HEALTHCHECKS["routines"]` config (e.g. `HEALTHCHECKS_UUID_RPI4B_RSYNC`); add one
line per routine.

1. Create `collector.env` in the same appdata folder as your collector (match
   your `CONFIG` paths) and lock down its permissions so only root can read it:
   ```bash
   cat > /mnt/user/appdata/dashboard/collector.env <<'EOF'
   HEALTHCHECKS_API_KEY=hcr_xxxxxxxxxxxxxxxxxxxxxxxx
   HEALTHCHECKS_UUID_RPI4B_RSYNC=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   HEALTHCHECKS_UUID_MEALIE_BACKUP=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   HEALTHCHECKS_UUID_CALIBRE_BACKUP=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   # GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx   # optional, raises GitHub API rate limit
   EOF
   chmod 600 /mnt/user/appdata/dashboard/collector.env
   ```
2. Paste this as the **User Script** body (this wrapper — *not* the collector
   itself — is what the cron schedule runs):
   ```bash
   #!/bin/bash
   APP=/mnt/user/appdata/dashboard

   # Load secrets into memory without printing them. `set -a` auto-exports every
   # variable in the sourced file, so the Python process inherits it.
   set -a
   [ -f "$APP/collector.env" ] && . "$APP/collector.env"
   set +a

   exec "$APP/venv/bin/python" "$APP/collector.py" >> "$APP/collector.log" 2>&1
   ```
   The key exists only in the User Script process's memory for the duration of
   the run — it is never written into `metrics.json` or the log. If
   `HEALTHCHECKS_API_KEY` is unset, the collector simply skips the routines
   section and logs a warning rather than failing.

> Keep `collector.env` out of version control — `*.env` is already in
> `.gitignore`.

### Dashboard Container

```bash
# from this directory on the Unraid host (Docker Compose Manager plugin)
docker compose up -d --build
```

Open `http://<server>:8088/`. The "synced" pill turns green once it reads live
data; it shows an amber *static snapshot* badge while only the embedded fallback
is available.

> **Why a directory mount, not a file mount?** Compose mounts
> `…/dashboard/data → /usr/share/nginx/html/data:ro`. Bind-mounting a single
> file that doesn't exist yet makes Docker silently create a *directory* in its
> place. Mounting the parent dir avoids that, so the stack survives a first boot
> before the collector has run.

## Security posture (Dashboard)

- `user: 101:101` — never root
- `cap_drop: [ALL]` — binds 8080, so `NET_BIND_SERVICE` isn't needed
- `security_opt: [no-new-privileges:true]`
- `read_only: true` root filesystem; only tmpfs (`/tmp`, `/var/cache/nginx`,
  `/var/run`) is writable
- no `docker.sock`, no `network_mode: host`, no privileged flags
- the metrics volume is mounted **`:ro`** — the container cannot write to the host
