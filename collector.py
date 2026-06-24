#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================================
 collector.py  —  host-side metrics collector for the Unraid dashboard
============================================================================

WHAT THIS IS
------------
A self-contained Python 3 script that runs *natively on the Unraid host*
(via the User Scripts plugin) on a cron schedule. It gathers system, array,
network, Docker, and routine metrics and writes a single `metrics.json`
matching the EXACT schema embedded in the dashboard frontend.

The Docker container that serves the dashboard is intentionally dumb: it has
no docker.sock, no host network, no privileges. It just serves index.html and
reads the metrics.json that THIS script drops into a read-only volume.

    Collector (this script, on the host)  ->  writes  ->  metrics.json
    Dashboard (nginx container, sandboxed) -> reads (ro) -> serves to browser

----------------------------------------------------------------------------
 DEPENDENCIES & INSTALLATION ON UNRAID
----------------------------------------------------------------------------
The script leans on the standard library wherever possible. Two optional
third-party packages improve accuracy:

  * psutil       — CPU%, CPU temperature, RAM. (Falls back to /proc parsing.)
  * speedtest-cli — Internet speed test.        (Falls back to the Ookla CLI.)

Everything else (docker, tailscale) is shelled out to binaries already
present on Unraid. Pick ONE of the install strategies below.

  Option A — Python virtualenv (RECOMMENDED, survives Unraid upgrades)
  --------------------------------------------------------------------
  Unraid wipes /usr on reboot, so install into persistent appdata and point
  cron at the venv's interpreter:

      mkdir -p /mnt/user/appdata/dashboard
      python3 -m venv /mnt/user/appdata/dashboard/venv
      /mnt/user/appdata/dashboard/venv/bin/pip install --upgrade pip
      /mnt/user/appdata/dashboard/venv/bin/pip install psutil speedtest-cli

      # then run with:
      /mnt/user/appdata/dashboard/venv/bin/python /mnt/user/appdata/dashboard/collector.py

  Option B — Dev Pack / NerdTools plugin
  --------------------------------------------------------------------
  Install the "Dev Pack" (or "NerdTools" -> python3 + pip) plugin from
  Community Apps, then:

      pip3 install psutil speedtest-cli

  NOTE: Dev Pack packages live under /boot and re-extract on boot, so they
  persist — but Option A is cleaner and version-pinned.

  Option C — none of the above
  --------------------------------------------------------------------
  The script still runs with zero extra packages; it just degrades:
  CPU/RAM come from /proc, and the speed test is skipped (the last cached
  result, if any, is reused).

  The Ookla CLI (optional, better speed tests than speedtest-cli):
      Install via the "speedtest" binary on PATH; the script auto-detects it.

----------------------------------------------------------------------------
 USER SCRIPTS PLUGIN — CRON SETUP
----------------------------------------------------------------------------
1. Install "User Scripts" from Community Apps.
2. Add a new script, paste this file (or have it `exec` the venv copy).
3. Set the schedule to run every 5 minutes — custom cron:  */5 * * * *
   (The internal 4-hour speed-test throttle is independent of this cadence.)
4. The script writes to OUTPUT_PATH below, which must be the host side of the
   read-only volume mounted into the dashboard container.

A convenient User Scripts wrapper that loads secrets from a separate env file
(so HEALTHCHECKS_API_KEY etc. never live inside this script) and runs the
collector from the venv:

    #!/bin/bash
    APP=/mnt/user/appdata/dashboard

    # Load secrets into the environment without echoing them. `set -a` marks
    # every variable sourced below as exported so the Python process inherits it.
    set -a
    [ -f "$APP/collector.env" ] && . "$APP/collector.env"
    set +a

    exec "$APP/venv/bin/python" "$APP/collector.py" >> "$APP/collector.log" 2>&1

Where collector.env (chmod 600, NOT committed to git) contains:

    HEALTHCHECKS_API_KEY=hcr_xxxxxxxxxxxxxxxxxxxxxxxx
    HEALTHCHECKS_UUID_RPI4B_RSYNC=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    HEALTHCHECKS_UUID_MEALIE_BACKUP=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    HEALTHCHECKS_UUID_CALIBRE_BACKUP=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx     # optional, raises the API rate limit

============================================================================
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Optional accelerators — imported defensively so the script still runs without them.
try:
    import psutil  # type: ignore
except Exception:
    psutil = None


# ===========================================================================
#  CONFIGURATION  —  EDIT THIS SECTION
# ===========================================================================

CONFIG = {
    # ---- Output -----------------------------------------------------------
    # Where to write metrics.json. This MUST be the host path that is mounted
    # read-only into the container at /usr/share/nginx/html/data/.
    "output_path": "/mnt/user/appdata/unraid-dashboard/data/metrics.json",

    # Small rolling-state file (network counters, sparkline history, speed-test
    # cache, latest-version cache). /tmp is tmpfs on Unraid (cleared on reboot),
    # which is fine — trends simply rebuild. Move it under appdata to persist.
    "state_path": "/tmp/dashboard_history.json",

    # ---- Identity ---------------------------------------------------------
    # Leave server_name None to auto-detect (ident.cfg -> var.ini -> hostname).
    "server_name": None,

    # ---- Unraid emhttp state files ---------------------------------------
    "var_ini": "/var/local/emhttp/var.ini",
    "disks_ini": "/var/local/emhttp/disks.ini",
    "ident_cfg": "/boot/config/ident.cfg",

    # ---- Network ----------------------------------------------------------
    # Primary interface for traffic stats. None = auto-detect the busiest
    # physical/bridge interface (skips lo/docker/veth/virbr/tailscale).
    "net_interface": None,

    # Keep this many sparkline samples (the frontend renders the last 24).
    "trend_points": 24,

    # ---- Speed test -------------------------------------------------------
    "speedtest_enabled": True,
    "speedtest_interval_hours": 4,   # only run a real test this often

    # ---- Tailscale --------------------------------------------------------
    "tailscale_enabled": True,
    "tailscale_bin": "tailscale",    # or full path, e.g. /usr/local/bin/tailscale

    # ---- Docker -----------------------------------------------------------
    "docker_bin": "docker",
    # Cache upstream "latest version" lookups this long to avoid hammering
    # GitHub/Docker Hub (GitHub unauth limit = 60 req/hr; we run every 5 min).
    "latest_cache_hours": 6,
    # Optional GitHub token to raise the rate limit (read-only, public scope).
    "github_token": os.environ.get("GITHUB_TOKEN", ""),

    # ---- HTTP -------------------------------------------------------------
    "http_timeout": 8,
    "user_agent": "unraid-monitor-dashboard-collector/1.0",
}


# ---------------------------------------------------------------------------
#  APPLICATIONS  —  the self-hosted apps to track.
#
#  For each app:
#    name           : display name (must be stable; used as cache key).
#    logo           : OPTIONAL image URL shown in the card instead of the name's
#                     first letter. A broken/unreachable URL falls back to the
#                     letter automatically. (Icons below are from
#                     https://github.com/homarr-labs/dashboard-icons)
#    match          : regex matched against running container names to find
#                     the PRIMARY container.
#    exclude        : regex; container names matching this are NOT the primary
#                     (keeps sidecars from being picked as the main app).
#    latest         : where to resolve the newest upstream version:
#                       {"type": "github",   "repo": "owner/name"}
#                       {"type": "dockerhub","repo": "library/name"}
#                       {"type": "none"}  -> never flag updates
#    sidecars       : list of supporting containers to surface, each:
#                       {"name": "Display", "match": "<regex on container name>"}
#
#  Version is read from the container's image tag, preferring the OCI label
#  org.opencontainers.image.version when present.
# ---------------------------------------------------------------------------
APPS = [
    {
        "name": "Immich",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/immich.png",
        "match": r"immich.*server|^immich$",
        "exclude": r"postgres|redis|valkey|machine.?learning|microservices|database",
        "latest": {"type": "github", "repo": "immich-app/immich"},
        "sidecars": [
            {"name": "PostgreSQL",       "match": r"immich.*(postgres|database|pgvecto)"},
            {"name": "Redis",            "match": r"immich.*(redis|valkey)"},
            {"name": "Machine Learning", "match": r"immich.*machine.?learning"},
        ],
    },
    {
        "name": "Mealie",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/mealie.png",
        "match": r"mealie",
        "exclude": r"postgres|database",
        "latest": {"type": "github", "repo": "mealie-recipes/mealie"},
        "sidecars": [
            {"name": "PostgreSQL", "match": r"mealie.*(postgres|database)"},
        ],
    },
    {
        "name": "SigNoz",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/signoz.png",
        "match": r"signoz.*(query|frontend)|^signoz$",
        "exclude": r"clickhouse|otel|collector|zookeeper|alertmanager|migrat",
        "latest": {"type": "github", "repo": "SigNoz/signoz"},
        "sidecars": [
            {"name": "ClickHouse",     "match": r"clickhouse"},
            {"name": "OTel Collector", "match": r"otel|collector"},
            {"name": "Query Service",  "match": r"signoz.*query"},
        ],
    },
    {
        # we-promise/sure (compose: web + worker share one image; db/redis/backup
        # are sidecars). \bsure\b avoids matching words like "treasure"/"measure".
        "name": "Sure",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/sure.png",
        "match": r"\bsure\b",
        "exclude": r"worker|sidekiq|db|postgres|redis|backup",
        "latest": {"type": "github", "repo": "we-promise/sure"},
        "sidecars": [
            {"name": "Sidekiq Worker", "match": r"sure.*(worker|sidekiq)"},
            {"name": "PostgreSQL",     "match": r"sure.*(db|postgres)"},
            {"name": "Redis",          "match": r"sure.*redis"},
            {"name": "DB Backup",      "match": r"sure.*backup"},
        ],
    },
    {
        "name": "Jellyfin",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/jellyfin.png",
        "match": r"jellyfin",
        "exclude": r"vue|web",
        "latest": {"type": "github", "repo": "jellyfin/jellyfin"},
        "sidecars": [],
    },
    {
        "name": "Calibre Web Automated",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/calibre-web.png",
        "match": r"calibre.?web.?automated|^cwa$",
        "exclude": r"book.?downloader",
        "latest": {"type": "github", "repo": "crocodilestick/Calibre-Web-Automated"},
        "sidecars": [
            {"name": "Book Downloader", "match": r"calibre.*downloader|cwa.*downloader"},
        ],
    },
    {
        "name": "qBittorrent",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/qbittorrent.png",
        "match": r"qbittorrent",
        "exclude": r"",
        "latest": {"type": "github", "repo": "qbittorrent/qBittorrent"},
        "sidecars": [
            {"name": "WireGuard / VPN", "match": r"(gluetun|wireguard).*qbit|qbit.*(gluetun|wireguard)"},
        ],
    },
    {
        "name": "copyparty",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/copyparty.png",
        "match": r"copyparty",
        "exclude": r"",
        "latest": {"type": "github", "repo": "9001/copyparty"},
        "sidecars": [],
    },
    {
        "name": "n8n",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/n8n.png",
        "match": r"n8n",
        "exclude": r"postgres|database|worker|redis",
        "latest": {"type": "github", "repo": "n8n-io/n8n"},
        "sidecars": [
            {"name": "PostgreSQL", "match": r"n8n.*(postgres|database)"},
        ],
    },
    {
        "name": "Homepage",
        "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/homepage.png",
        "match": r"homepage",
        "exclude": r"",
        "latest": {"type": "github", "repo": "gethomepage/homepage"},
        "sidecars": [],
    },
    {
        "name": "Paperclip",
        "logo": "https://cdn.jsdelivr.net/gh/selfhst/icons/png/paperclip-ai.png",
        "match": r"paperclip",
        "exclude": r"postgres|database|db",
        "latest": {"type": "github", "repo": "paperclipai/paperclip"},
        "sidecars": [
            {"name": "PostgreSQL", "match": r"paperclip.*(postgres|database|db)"},
        ],
    },
    {
        # This dashboard's own container. Its image ships an
        # org.opencontainers.image.version label (stamped at build time via the
        # VERSION build-arg, see the Dockerfile), so the running version resolves
        # cleanly; the floating ':latest' tag alone would not.
        "name": "Unraid Monitor",
        "logo": "https://raw.githubusercontent.com/loadingpage/unraid-icons/main/icons/dashboard.png",
        "match": r"server-dashboard|unraid.?monitor",
        "exclude": r"",
        "latest": {"type": "github", "repo": "dineiar/unraid_monitor_dashboard"},
        "sidecars": [],
    },
    # {
    #     "name": "Authentik",
    #     "logo": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/authentik.png",
    #     "match": r"authentik.*server|^authentik$",
    #     "exclude": r"postgres|redis|worker|database",
    #     "latest": {"type": "github", "repo": "goauthentik/authentik"},
    #     "sidecars": [
    #         {"name": "PostgreSQL", "match": r"authentik.*(postgres|database)"},
    #         {"name": "Redis",      "match": r"authentik.*(redis|valkey)"},
    #     ],
    # },
]


# ---------------------------------------------------------------------------
#  HEALTHCHECKS.IO  —  scheduled routines.
#
#  Instead of grepping local cron logs, we pull live status from the
#  Healthchecks.io management API (works with the hosted service OR a
#  self-hosted instance — just change "api_url").
#
#    api_key  : a project API key (read-only is enough). Settings -> API Access.
#               Sourced from $HEALTHCHECKS_API_KEY by default — keep the secret
#               in a separate env file, not in this script (see the README:
#               "Loading secrets from an env file").
#    api_url  : .../api/v3   (hosted default below; self-hosted = your URL)
#
#  Each routine maps a dashboard card to a Healthchecks check. Match by the
#  check's "slug" (preferred, stable) or its "name". Everything the API does
#  NOT provide (friendly schedule text, the link to open, optional destination
#  disk) is supplied here.
#
#  Per-routine keys:
#    name        : card title.
#    check       : HC "slug" or "name" — matched against the GET /checks list.
#    schedule    : friendly schedule text shown on the card.
#    uuid        : the check's UUID — read from a per-routine env var (e.g.
#                  os.environ.get("HEALTHCHECKS_UUID_RPI4B_RSYNC", "")) so it
#                  stays out of this script, like the API key. The "Read more"
#                  link is built from it ({web_base}/checks/<uuid>/details/) and
#                  it is used to fetch ping bodies for disk parsing. If the env
#                  var is unset the routine still resolves status via "check",
#                  but the link and disk parsing are skipped.
#    parse_disks : when True, and the check is currently successful, the latest
#                  ping body is fetched and parsed as `df -h` output. Every
#                  filesystem row found is rendered as its own disk bar — so a
#                  job that pings with `df -h /data /backup` shows two disks.
#                  Scope your `df` command to control exactly what appears.
#                  When omitted/False the routine simply shows no disks.
# ---------------------------------------------------------------------------
HEALTHCHECKS = {
    "enabled": True,
    "api_url": "https://healthchecks.io/api/v3",
    # Read from the HEALTHCHECKS_API_KEY environment variable by default, so the
    # secret can live in a separate env file rather than in this script. You may
    # hardcode a string here instead, but that is discouraged. See the README
    # ("Loading secrets from an env file") for the User Scripts setup.
    "api_key": os.environ.get("HEALTHCHECKS_API_KEY", ""),
    "routines": [
        {
            "name": "Off-Site Backup",
            "check": "rpi4b-rsync",            # HC slug or name
            "schedule": "Nightly · 02:00",
            "uuid": os.environ.get("HEALTHCHECKS_UUID_RPI4B_RSYNC", ""),
            "parse_disks": True,               # parse `df -h` from the latest ping
        },
        {
            "name": "Mealie backup",
            "check": "mealie-backup",
            "schedule": "Weekly · 04:30",
            "uuid": os.environ.get("HEALTHCHECKS_UUID_MEALIE_BACKUP", ""),
        },
        {
            "name": "Calibre backup",
            "check": "calibre-backup",
            "schedule": "Monthly · 04:20",
            "uuid": os.environ.get("HEALTHCHECKS_UUID_CALIBRE_BACKUP", ""),
        },
    ],
}


# ===========================================================================
#  LOGGING  (stderr; User Scripts captures it)
# ===========================================================================
def log(level, msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write("[{}] {:<5} {}\n".format(ts, level, msg))
    sys.stderr.flush()


def info(msg):  log("INFO", msg)
def warn(msg):  log("WARN", msg)
def err(msg):   log("ERROR", msg)


# ===========================================================================
#  GENERIC HELPERS
# ===========================================================================
def iso_utc(epoch=None):
    """ISO-8601 UTC string the frontend's `new Date()` can parse."""
    if epoch is None:
        dt = datetime.now(timezone.utc)
    else:
        dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_duration(seconds):
    """Seconds -> '8h 14m' / '3d 2h' / '45m' style string."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if seconds < 0:
        return "—"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return "{}d {}h {}m".format(d, h, m)
    if h:
        return "{}h {}m".format(h, m)
    return "{}m".format(m)


def kib_to_tb(kib):
    """Unraid reports sizes in KiB. Convert to decimal TB (what disks are sold as)."""
    try:
        return round(float(kib) * 1024 / 1_000_000_000_000, 2)
    except (TypeError, ValueError):
        return 0.0


def run_cmd(args, timeout=20):
    """Run a command, return (rc, stdout_str). Never raises."""
    try:
        p = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        warn("command timed out: {}".format(" ".join(args)))
        return 124, ""
    except Exception as e:  # noqa: BLE001 - defensive top-level guard
        warn("command failed {}: {}".format(args, e))
        return 1, ""


def http_get_json(url, headers=None, timeout=None):
    """GET a URL and parse JSON. Returns parsed object or None on any failure."""
    timeout = timeout or CONFIG["http_timeout"]
    hdrs = {"User-Agent": CONFIG["user_agent"], "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        warn("HTTP {} for {}".format(e.code, url))
    except Exception as e:  # noqa: BLE001
        warn("HTTP error for {}: {}".format(url, e))
    return None


def http_get_text(url, headers=None, timeout=None):
    """GET a URL and return the raw body as text. Returns None on any failure.

    Used for endpoints that return text/plain (e.g. a Healthchecks ping body
    containing `df -h` output) rather than JSON.
    """
    timeout = timeout or CONFIG["http_timeout"]
    hdrs = {"User-Agent": CONFIG["user_agent"], "Accept": "text/plain, */*"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        warn("HTTP {} for {}".format(e.code, url))
    except Exception as e:  # noqa: BLE001
        warn("HTTP error for {}: {}".format(url, e))
    return None


def parse_unraid_ini(path):
    """
    Parse Unraid's emhttp .ini files.

    Format quirks handled:
      * section headers look like  ["disk1"]  (quoted name inside brackets)
      * values are double-quoted:  device="sdb"
      * top-level keys (no section) are kept at the root (var.ini case)

    Returns a dict. Section dicts are nested under their (unquoted) name;
    root-level keys sit directly in the dict.
    """
    data = {}
    current = data
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line[0] in "#;":
                    continue
                if line.startswith("[") and line.endswith("]"):
                    name = line[1:-1].strip().strip('"')
                    current = {}
                    data[name] = current
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip().strip('"')
                val = val.strip()
                if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                    val = val[1:-1]
                current[key] = val
    except FileNotFoundError:
        warn("ini not found: {}".format(path))
    except Exception as e:  # noqa: BLE001
        warn("failed parsing {}: {}".format(path, e))
    return data


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ---- version normalisation -------------------------------------------------
_SEMVER_RE = re.compile(r"(\d+(?:\.\d+){1,3})")  # clean dotted version (used by version_tuple)

# A prerelease suffix we KEEP (alpha/beta/rc/...), so 'sure:0.7.0-alpha.6' stays
# 'v0.7.0-alpha.6'. Build-variant suffixes (-ubuntu, -alpine, -bookworm, -ls462,
# _v2.0.13, -vectorchord…) are NOT in this set and are therefore dropped.
_PRERELEASE = r"(?:[-.](?:alpha|beta|rc|pre|preview|dev|snapshot|nightly)[\w.]*)?"
_NORM_LEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,3}" + _PRERELEASE + r")", re.IGNORECASE)
_NORM_ANY_RE = re.compile(r"(\d+(?:\.\d+){1,3}" + _PRERELEASE + r")", re.IGNORECASE)
_VERSIONISH_RE = re.compile(r"^v\d")  # does a normalized string look like a real version?


def normalize_version(raw):
    """
    Turn an image tag / release name / version label into a tidy 'vX.Y.Z' for
    display + comparison.

    Strategy (order matters):
      1. Strip a leading 'v' so 'v1.2.3' and '1.2.3' behave identically.
      2. If the string STARTS with a number, take that leading number (plus any
         alpha/beta/rc prerelease suffix) — it is the primary version. Handles
         Postgres-style tags where extension versions trail the major, which the
         old "first dotted run" logic got wrong:
            '14-vectorchord0.4.3-pgvectors0.2.0' -> 'v14'   (was 'v0.4.3')
            '16'                                 -> 'v16'
            '1.106.4'                            -> 'v1.106.4'
            'v10.9.6-ubuntu'                     -> 'v10.9.6'
            '5.2.2_v2.0.13-ls462'                -> 'v5.2.2'
            '0.7.0-alpha.6'                      -> 'v0.7.0-alpha.6'
      3. Otherwise (a non-numeric prefix), fall back to the first dotted version
         found, so prefixed tags still resolve:
            'release-2024.6.1' -> 'v2024.6.1'
            'amd64-1.2.3-ls47' -> 'v1.2.3'
      4. Non-versiony tags ('latest', 'stable', a git sha) pass through unchanged.
    """
    if not raw:
        return "—"
    raw = str(raw).strip()
    # 1. drop a leading 'v' only when it actually prefixes a number
    s = raw[1:] if raw[:1] in ("v", "V") and raw[1:2].isdigit() else raw
    # 2. leading version wins
    m = _NORM_LEADING_RE.match(s)
    if m:
        return "v" + m.group(1)
    # 3. fall back to the first dotted version anywhere
    m = _NORM_ANY_RE.search(s)
    if m:
        return "v" + m.group(1)
    # 4. not version-like — show as-is
    return raw


def version_tuple(v):
    """'v1.10.2' -> (1, 10, 2) for ordering. Non-numeric -> empty tuple."""
    m = _SEMVER_RE.search(v or "")
    if not m:
        return tuple()
    try:
        return tuple(int(x) for x in m.group(1).split("."))
    except ValueError:
        return tuple()


# ===========================================================================
#  STATE  (rolling history / caches)
# ===========================================================================
def load_state():
    try:
        with open(CONFIG["state_path"], "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        warn("state unreadable ({}); starting fresh".format(e))
        return {}


def save_state(state):
    write_json_atomic(CONFIG["state_path"], state)


def write_json_atomic(path, obj):
    """Write JSON via temp file + os.replace so readers never see a partial file."""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            err("cannot create dir {}: {}".format(parent, e))
            return
    tmp = "{}.tmp.{}".format(path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        err("atomic write to {} failed: {}".format(path, e))
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ===========================================================================
#  1. SERVER IDENTITY  +  CPU / MEMORY
# ===========================================================================
def get_server_name(var):
    if CONFIG["server_name"]:
        return CONFIG["server_name"]
    # ident.cfg holds the user-configured name (Settings -> Identification).
    ident = parse_unraid_ini(CONFIG["ident_cfg"])
    for key in ("NAME", "name"):
        if ident.get(key):
            return ident[key]
        if var.get(key):
            return var[key]
    try:
        return socket.gethostname() or "UNRAID"
    except Exception:  # noqa: BLE001
        return "UNRAID"


def get_primary_ip():
    """Best-effort LAN IP. Opens (but never sends on) a UDP socket to learn the
    outbound source address — works offline too."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "0.0.0.0"
    finally:
        if s:
            s.close()


def get_uptime():
    try:
        with open("/proc/uptime", "r") as f:
            return fmt_duration(float(f.read().split()[0]))
    except Exception as e:  # noqa: BLE001
        warn("uptime read failed: {}".format(e))
        return "—"


def get_cpu_usage():
    if psutil:
        try:
            return int(round(psutil.cpu_percent(interval=1.0)))
        except Exception as e:  # noqa: BLE001
            warn("psutil cpu_percent failed: {}".format(e))
    # /proc/stat delta fallback (two reads ~0.5s apart).
    try:
        def snap():
            with open("/proc/stat") as f:
                parts = f.readline().split()[1:]
            vals = [int(x) for x in parts]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return sum(vals), idle
        t1, i1 = snap()
        time.sleep(0.5)
        t2, i2 = snap()
        dt, di = t2 - t1, i2 - i1
        if dt <= 0:
            return 0
        return int(round((1 - di / dt) * 100))
    except Exception as e:  # noqa: BLE001
        warn("cpu fallback failed: {}".format(e))
        return 0


def get_cpu_temp():
    if psutil and hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures() or {}
            # Prefer the package/Tdie reading; otherwise take the hottest core.
            for chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
                if chip in temps and temps[chip]:
                    entries = temps[chip]
                    for e in entries:
                        lbl = (e.label or "").lower()
                        if "package" in lbl or "tdie" in lbl or "tctl" in lbl:
                            return int(round(e.current))
                    return int(round(max(e.current for e in entries)))
            # Unknown chip naming: take the global max if anything exists.
            allv = [e.current for v in temps.values() for e in v if e.current]
            if allv:
                return int(round(max(allv)))
        except Exception as e:  # noqa: BLE001
            warn("psutil temp failed: {}".format(e))
    # hwmon fallback.
    try:
        import glob
        best = 0
        for p in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
            try:
                with open(p) as f:
                    best = max(best, int(f.read().strip()) // 1000)
            except Exception:  # noqa: BLE001
                continue
        return best
    except Exception:  # noqa: BLE001
        return 0


def get_memory():
    if psutil:
        try:
            vm = psutil.virtual_memory()
            return {
                "usage": int(round(vm.percent)),
                "used": "{:.1f}".format(vm.used / (1024 ** 3)),
                "total": str(int(round(vm.total / (1024 ** 3)))),
                "unit": "GB",
            }
        except Exception as e:  # noqa: BLE001
            warn("psutil memory failed: {}".format(e))
    # /proc/meminfo fallback (kB values).
    try:
        info_kb = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info_kb[k.strip()] = int(v.strip().split()[0])
        total = info_kb.get("MemTotal", 0)
        avail = info_kb.get("MemAvailable", info_kb.get("MemFree", 0))
        used = total - avail
        pct = int(round(used / total * 100)) if total else 0
        return {
            "usage": pct,
            "used": "{:.1f}".format(used / (1024 ** 2)),
            "total": str(int(round(total / (1024 ** 2)))),
            "unit": "GB",
        }
    except Exception as e:  # noqa: BLE001
        warn("meminfo fallback failed: {}".format(e))
        return {"usage": 0, "used": "0", "total": "0", "unit": "GB"}


# ===========================================================================
#  2. UNRAID ARRAY, DISK TEMPS, STORAGE POOLS
# ===========================================================================
def _disk_display_name(section, disk):
    """Friendly label for the temps card."""
    name = disk.get("name") or section
    m = re.match(r"disk(\d+)$", name)
    if m:
        return "Disk {}".format(m.group(1))
    if name == "parity":
        return "Parity"
    if name == "parity2":
        return "Parity 2"
    rot = disk.get("rotational", "")
    if name in ("cache",) or "cache" in name:
        suffix = " (NVMe)" if rot == "0" else ""
        return name.capitalize() + suffix
    return name.capitalize()


def collect_array_and_storage(var, disks):
    """
    Build:
      metrics.disks  -> [{name, temp}, ...]
      metrics.pools  -> [{name, used, total, unit}, ...]
      array          -> {started, parity:{lastCheck,duration,resErrors,sbSynced}}
    """
    disk_temps = []
    data_total_kib = 0
    data_used_kib = 0
    pools = []

    for section, d in disks.items():
        if not isinstance(d, dict):
            continue
        name = d.get("name") or section
        dtype = (d.get("type") or "").lower()

        # Skip the USB boot flash entirely.
        if name == "flash" or dtype == "flash":
            continue

        # ---- temperatures (skip spun-down "*"; report 0 only as last resort) --
        raw_temp = d.get("temp", "")
        if raw_temp not in ("", "*", "-"):
            disk_temps.append({"name": _disk_display_name(section, d),
                               "temp": to_int(raw_temp, 0)})

        # ---- storage accounting -----------------------------------------------
        fs_size = to_int(d.get("fsSize"), 0)   # KiB, only set on mounted members
        fs_used = to_int(d.get("fsUsed"), 0)

        if re.match(r"disk\d+$", name):
            # Array data disk -> aggregate into the single "array" pool.
            data_total_kib += fs_size
            data_used_kib += fs_used
        elif name.startswith("parity"):
            continue  # parity holds no filesystem
        elif fs_size > 0:
            # A pool (cache, appdata, nvme, etc.) — its mounted member carries fs*.
            pools.append({
                "name": name,
                "used": kib_to_tb(fs_used),
                "total": kib_to_tb(fs_size),
                "unit": "TB",
            })

    storage_pools = []
    if data_total_kib > 0:
        storage_pools.append({
            "name": "array",
            "used": kib_to_tb(data_used_kib),
            "total": kib_to_tb(data_total_kib),
            "unit": "TB",
        })
    storage_pools.extend(pools)

    # ---- array + parity state from var.ini ---------------------------------
    started = (var.get("mdState", "").upper() == "STARTED")
    res_errors = to_int(var.get("sbSyncErrs"), 0)

    sb_start = to_int(var.get("sbSynced"), 0)    # epoch of last sync start
    sb_end = to_int(var.get("sbSynced2"), 0)     # epoch of last sync finish
    resync_pos = to_int(var.get("mdResyncPos"), 0)
    resyncing = to_int(var.get("mdResync"), 0) > 0 and resync_pos >= 0 and resync_pos != 0

    last_check = iso_utc(sb_end) if sb_end else (iso_utc(sb_start) if sb_start else None)
    duration = fmt_duration(sb_end - sb_start) if (sb_end and sb_start and sb_end >= sb_start) else "—"

    # "In sync" = no sync errors and not mid-resync.
    sb_synced = (res_errors == 0) and not resyncing

    array = {
        "started": started,
        "parity": {
            "lastCheck": last_check,
            "duration": duration,
            "resErrors": res_errors,
            "sbSynced": sb_synced,
        },
    }
    return disk_temps, storage_pools, array


# ===========================================================================
#  3. NETWORK  (rates + sparkline trend)
# ===========================================================================
def read_net_counters():
    """Return {iface: (rx_bytes, tx_bytes)} from /proc/net/dev."""
    counters = {}
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]  # skip the two header rows
        for line in lines:
            name, _, rest = line.partition(":")
            name = name.strip()
            fields = rest.split()
            if len(fields) >= 9:
                counters[name] = (int(fields[0]), int(fields[8]))
    except Exception as e:  # noqa: BLE001
        warn("/proc/net/dev read failed: {}".format(e))
    return counters


def pick_interface(counters):
    if CONFIG["net_interface"]:
        return CONFIG["net_interface"]
    skip = re.compile(r"^(lo|docker|veth|virbr|tailscale|wg|br-[0-9a-f]{12}|tun)")
    candidates = [(rx, name) for name, (rx, _tx) in counters.items()
                  if not skip.match(name)]
    if not candidates:
        return None
    candidates.sort(reverse=True)  # busiest by rx bytes
    return candidates[0][1]


def collect_network(state):
    """
    Compute current in/out MB/s from the delta since the previous run, and
    maintain the rolling sparkline history in the state file.
    """
    now = time.time()
    counters = read_net_counters()
    iface = pick_interface(counters)

    current_in = current_out = 0.0
    if iface and iface in counters:
        rx, tx = counters[iface]
        prev = state.get("net_prev")
        if prev and prev.get("iface") == iface:
            dt = now - prev["ts"]
            d_rx = rx - prev["rx"]
            d_tx = tx - prev["tx"]
            if dt > 0 and d_rx >= 0 and d_tx >= 0:   # ignore counter resets/reboots
                current_in = round(d_rx / dt / 1_000_000, 2)   # MB/s
                current_out = round(d_tx / dt / 1_000_000, 2)
        state["net_prev"] = {"iface": iface, "rx": rx, "tx": tx, "ts": now}
    else:
        warn("no usable network interface found")

    # ---- rolling sparkline history -----------------------------------------
    n = CONFIG["trend_points"]
    trend_in = state.get("trend_in", [])
    trend_out = state.get("trend_out", [])
    trend_in.append(current_in)
    trend_out.append(current_out)
    trend_in = trend_in[-n:]
    trend_out = trend_out[-n:]
    state["trend_in"] = trend_in
    state["trend_out"] = trend_out

    return {
        "currentIn": "{:.1f}".format(current_in),
        "currentOut": "{:.1f}".format(current_out),
        "unit": "MB/s",
        "trendIn": trend_in,
        "trendOut": trend_out,
    }


# ===========================================================================
#  3b. SPEED TEST  (throttled to every N hours; result cached in state)
# ===========================================================================
def run_speedtest():
    """
    Try, in order:
      1. the `speedtest` python module (speedtest-cli pip package)
      2. the `speedtest-cli --json` binary
      3. the Ookla `speedtest --format=json` binary
    Returns {download, upload, ping} in Mbps/ms, or None.
    """
    # 1. python module
    try:
        import speedtest  # type: ignore
        st = speedtest.Speedtest(secure=True)
        st.get_best_server()
        down = st.download() / 1_000_000      # bits -> Mbps
        up = st.upload(pre_allocate=False) / 1_000_000
        ping = st.results.ping
        return {"download": round(down, 1), "upload": round(up, 1), "ping": int(round(ping))}
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        warn("speedtest module failed: {}".format(e))

    # 2. classic speedtest-cli binary
    rc, out = run_cmd(["speedtest-cli", "--json", "--secure"], timeout=120)
    if rc == 0 and out.strip():
        try:
            j = json.loads(out)
            return {
                "download": round(j.get("download", 0) / 1_000_000, 1),
                "upload": round(j.get("upload", 0) / 1_000_000, 1),
                "ping": int(round(j.get("ping", 0))),
            }
        except Exception as e:  # noqa: BLE001
            warn("speedtest-cli json parse failed: {}".format(e))

    # 3. Ookla CLI
    rc, out = run_cmd(["speedtest", "--format=json", "--accept-license", "--accept-gdpr"], timeout=120)
    if rc == 0 and out.strip():
        try:
            j = json.loads(out)
            return {
                "download": round(j["download"]["bandwidth"] * 8 / 1_000_000, 1),
                "upload": round(j["upload"]["bandwidth"] * 8 / 1_000_000, 1),
                "ping": int(round(j["ping"]["latency"])),
            }
        except Exception as e:  # noqa: BLE001
            warn("Ookla speedtest json parse failed: {}".format(e))

    warn("no working speedtest method available")
    return None


def collect_speedtest(state):
    """Return the speedTest object, running a fresh test only if throttle elapsed."""
    cached = state.get("speedtest_result")
    last_ts = state.get("speedtest_ts", 0)
    interval = CONFIG["speedtest_interval_hours"] * 3600
    now = time.time()

    due = CONFIG["speedtest_enabled"] and (now - last_ts >= interval)
    if due:
        info("speed test due (last {:.1f}h ago) — running…".format((now - last_ts) / 3600))
        result = run_speedtest()
        if result:
            result["lastRun"] = iso_utc(now)
            state["speedtest_result"] = result
            state["speedtest_ts"] = now
            return result
        warn("speed test failed; reusing last cached result")

    if cached:
        return cached
    # Never run yet / nothing cached — emit neutral placeholders.
    return {"download": 0, "upload": 0, "ping": 0, "lastRun": None}


# ===========================================================================
#  3c. TAILSCALE
# ===========================================================================
def collect_tailscale():
    if not CONFIG["tailscale_enabled"]:
        return {"connected": False, "hostname": None}
    rc, out = run_cmd([CONFIG["tailscale_bin"], "status", "--json"], timeout=15)
    if rc == 0 and out.strip():
        try:
            j = json.loads(out)
            self_node = j.get("Self", {}) or {}
            dns = (self_node.get("DNSName") or "").rstrip(".")
            backend = j.get("BackendState", "")
            online = self_node.get("Online", False)
            connected = (backend == "Running") and online
            return {"connected": bool(connected), "hostname": dns or None}
        except Exception as e:  # noqa: BLE001
            warn("tailscale json parse failed: {}".format(e))
    # Fallback: presence of a tailscale0 interface with an address.
    try:
        if psutil:
            addrs = psutil.net_if_addrs()
            if "tailscale0" in addrs:
                ip = next((a.address for a in addrs["tailscale0"]
                           if a.family == socket.AF_INET), None)
                return {"connected": bool(ip), "hostname": None}
    except Exception:  # noqa: BLE001
        pass
    return {"connected": False, "hostname": None}


# ===========================================================================
#  4. DOCKER APPLICATIONS  +  SUPPORTING CONTAINERS
# ===========================================================================
def docker_ps():
    """Running containers as [{name, image, id, labels(empty here)}]."""
    rc, out = run_cmd([CONFIG["docker_bin"], "ps", "--no-trunc",
                       "--format", "{{json .}}"], timeout=20)
    if rc != 0:
        warn("`docker ps` failed (rc={}) — Docker apps will be empty".format(rc))
        return []
    containers = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        # `Names` may be comma-separated; take the first.
        name = (j.get("Names") or "").split(",")[0].strip()
        containers.append({"name": name, "image": j.get("Image", ""), "id": j.get("ID", "")})
    return containers


_inspect_cache = {}


def _inspect(container):
    """Run `docker inspect` once per container id and cache the parsed bits we
    reuse: image labels, the configured image ref, the container state, and the
    healthcheck status. Returns a dict with keys: labels, image_cfg, state,
    health (state/health lowercased; health is "" when the image has no
    HEALTHCHECK)."""
    cid = container["id"]
    if cid in _inspect_cache:
        return _inspect_cache[cid]
    rc, out = run_cmd([CONFIG["docker_bin"], "inspect", cid], timeout=15)
    info = {"labels": {}, "image_cfg": "", "state": "", "health": ""}
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)[0]
            cfg = data.get("Config", {}) or {}
            info["labels"] = cfg.get("Labels") or {}
            info["image_cfg"] = cfg.get("Image", "")
            st = data.get("State", {}) or {}
            info["state"] = (st.get("Status") or "").lower()
            info["health"] = ((st.get("Health") or {}).get("Status") or "").lower()
        except Exception as e:  # noqa: BLE001
            warn("inspect parse failed for {}: {}".format(container["name"], e))
    _inspect_cache[cid] = info
    return info


def _image_tag(image_ref):
    """Extract just the tag from an image reference, or '' if untagged.
    Handles registries with ports ('host:5000/repo:tag') and digest pins
    ('repo:tag@sha256:...'). Examples:
      'clickhouse/clickhouse-server:25.5.6'            -> '25.5.6'
      'ghcr.io/immich-app/postgres:14-vchord@sha256:..'-> '14-vchord'
      'docker.n8n.io/n8nio/n8n'                        -> ''  (no tag)
    """
    if not image_ref:
        return ""
    if "@" in image_ref:                       # strip digest pin first
        image_ref = image_ref.split("@", 1)[0]
    last = image_ref.rsplit("/", 1)[-1]        # drop registry/namespace path
    return last.rsplit(":", 1)[-1] if ":" in last else ""


def container_version(container):
    """
    Resolve a running container's *deployed* version.

    Real-world images carry version info in inconsistent (and sometimes wrong)
    places, so we trust sources in this order and take the first that actually
    looks like a version:

      1. the image TAG — it is what was deployed, and beats labels that hold a
         base-OS version or a git sha. Seen in the wild:
            clickhouse  tag 25.5.6  vs  oci.version 22.04   (base image!)
            calibre     tag v4.0.6  vs  oci.version cd80d60b-ls59 (git sha)
      2. the OCI / label-schema / version labels — needed when the tag is a
         floating ':latest' (jellyfin, qbittorrent, homepage).
      3. otherwise the first non-empty source as-is, else '—'.
    """
    info = _inspect(container)
    labels, image_cfg = info["labels"], info["image_cfg"]

    # Candidate raw version strings, most-trustworthy first.
    candidates = []
    tag = _image_tag(image_cfg) or _image_tag(container.get("image", ""))
    if tag:
        candidates.append(tag)
    for key in ("org.opencontainers.image.version",
                "org.label-schema.version",
                "version"):
        if labels.get(key):
            candidates.append(labels[key])

    # First candidate that resolves to a real version wins.
    for cand in candidates:
        norm = normalize_version(cand)
        if _VERSIONISH_RE.match(norm):
            return norm
    # Nothing version-like (e.g. only ':latest'): show the first source as-is.
    return normalize_version(candidates[0]) if candidates else "—"


def container_health(container):
    """The container's 'exact status' for the dashboard indicator: the Docker
    healthcheck status when the image declares a HEALTHCHECK (healthy / starting
    / unhealthy), otherwise the raw container state (running, restarting, paused,
    …). The frontend renders a marker for anything that isn't healthy/running."""
    info = _inspect(container)
    return info["health"] or info["state"] or "unknown"


def find_container(containers, match_re, exclude_re=None):
    """First running container whose name matches `match_re` and not `exclude_re`."""
    inc = re.compile(match_re, re.IGNORECASE) if match_re else None
    exc = re.compile(exclude_re, re.IGNORECASE) if exclude_re else None
    for c in containers:
        nm = c["name"]
        if inc and not inc.search(nm):
            continue
        if exc and exc.pattern and exc.search(nm):
            continue
        return c
    return None


def fetch_github_latest(repo):
    base = "https://api.github.com/repos/{}".format(repo)
    headers = {"Accept": "application/vnd.github+json"}
    if CONFIG["github_token"]:
        headers["Authorization"] = "Bearer {}".format(CONFIG["github_token"])
    # Prefer the published "latest" release.
    j = http_get_json(base + "/releases/latest", headers=headers)
    if isinstance(j, dict) and j.get("tag_name"):
        return normalize_version(j["tag_name"])
    # Fall back to the newest non-prerelease in the releases list.
    j = http_get_json(base + "/releases?per_page=20", headers=headers)
    if isinstance(j, list):
        for rel in j:
            if not rel.get("prerelease") and not rel.get("draft") and rel.get("tag_name"):
                return normalize_version(rel["tag_name"])
    # Last resort: tags.
    j = http_get_json(base + "/tags?per_page=20", headers=headers)
    if isinstance(j, list):
        versions = [t["name"] for t in j if t.get("name")]
        versions = [v for v in versions if version_tuple(normalize_version(v))]
        if versions:
            versions.sort(key=lambda v: version_tuple(normalize_version(v)), reverse=True)
            return normalize_version(versions[0])
    return None


def fetch_dockerhub_latest(repo):
    if "/" not in repo:
        repo = "library/" + repo
    url = ("https://hub.docker.com/v2/repositories/{}/tags"
           "?page_size=100&ordering=last_updated".format(repo))
    j = http_get_json(url)
    if not isinstance(j, dict):
        return None
    best = None
    for t in j.get("results", []):
        name = t.get("name", "")
        vt = version_tuple(name)
        if not vt:
            continue
        if best is None or vt > best[0]:
            best = (vt, name)
    return normalize_version(best[1]) if best else None


def resolve_latest(app, state):
    """Latest upstream version, cached in state for CONFIG['latest_cache_hours']."""
    spec = app.get("latest", {"type": "none"})
    if spec.get("type") == "none":
        return None

    cache = state.setdefault("latest_cache", {})
    key = app["name"]
    entry = cache.get(key)
    ttl = CONFIG["latest_cache_hours"] * 3600
    now = time.time()
    if entry and (now - entry.get("ts", 0) < ttl) and entry.get("value"):
        return entry["value"]

    value = None
    if spec["type"] == "github":
        value = fetch_github_latest(spec["repo"])
    elif spec["type"] == "dockerhub":
        value = fetch_dockerhub_latest(spec["repo"])

    if value:
        cache[key] = {"value": value, "ts": now}
    elif entry and entry.get("value"):
        return entry["value"]      # keep stale-but-known value on lookup failure
    return value


def collect_apps(state):
    containers = docker_ps()
    if not containers:
        # No Docker visibility — emit the configured apps as "unknown" so the
        # grid stays stable rather than vanishing.
        out = []
        for a in APPS:
            entry = {"name": a["name"], "current": "—", "latest": "—",
                     "status": "ok", "health": None, "containers": []}
            if a.get("logo"):
                entry["logo"] = a["logo"]
            out.append(entry)
        return out

    apps_out = []
    for app in APPS:
        primary = find_container(containers, app["match"], app.get("exclude"))
        current = container_version(primary) if primary else "—"
        health = container_health(primary) if primary else None

        # Supporting containers.
        sidecars = []
        for sc in app.get("sidecars", []):
            c = find_container(containers, sc["match"])
            if c:
                sidecars.append({"name": sc["name"], "version": container_version(c),
                                 "health": container_health(c)})

        latest = resolve_latest(app, state)

        if not latest or current == "—":
            # Unknown upstream or app not running -> don't cry "update".
            status = "ok"
            latest_disp = latest or current
        else:
            status = "update" if normalize_version(current) != normalize_version(latest) else "ok"
            latest_disp = latest

        entry = {
            "name": app["name"],
            "current": current,
            "latest": latest_disp,
            "status": status,
            "health": health,
            "containers": sidecars,
        }
        if app.get("logo"):
            entry["logo"] = app["logo"]
        apps_out.append(entry)
        if not primary:
            warn("no running container matched app '{}'".format(app["name"]))
    return apps_out


# ===========================================================================
#  5. SCHEDULED ROUTINES  (Healthchecks.io)
# ===========================================================================
# HC API status -> dashboard status. The dashboard understands:
#   ok | warning | error | running
_HC_STATUS_MAP = {
    "up": "ok",
    "grace": "warning",
    "down": "error",
    "paused": "warning",
    "new": "warning",
}


def fetch_healthchecks_index():
    """Fetch all checks once and index by slug AND name for flexible matching."""
    cfg = HEALTHCHECKS
    url = cfg["api_url"].rstrip("/") + "/checks/"
    j = http_get_json(url, headers={"X-Api-Key": cfg["api_key"]})
    index = {}
    if isinstance(j, dict):
        for chk in j.get("checks", []):
            if chk.get("slug"):
                index[chk["slug"].lower()] = chk
            if chk.get("name"):
                index[chk["name"].lower()] = chk
    else:
        warn("Healthchecks API returned no data (check api_key / api_url)")
    return index


def healthchecks_web_base():
    """Derive the Healthchecks *web* base (for /checks/<uuid>/details/ links)
    from the configured *API* base, e.g.
        https://healthchecks.io/api/v3  ->  https://healthchecks.io
    Works for self-hosted instances too (strips the trailing /api/vN)."""
    api = HEALTHCHECKS.get("api_url", "https://healthchecks.io/api/v3").rstrip("/")
    return re.sub(r"/api/v\d+$", "", api)


# ---- `df -h` parsing -------------------------------------------------------
# df -h uses powers of 1024 (so "16T" means 16 TiB). We parse each size to
# bytes, then render used + total in one shared unit chosen from the total.
_DF_SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGTPEZ])?i?B?$", re.IGNORECASE)
_DF_FACTORS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
               "T": 1024 ** 4, "P": 1024 ** 5, "E": 1024 ** 6, "Z": 1024 ** 7}


def _parse_size_to_bytes(token):
    """'16T' -> 1.76e13, '800G' -> ..., '0' -> 0.0, junk -> None."""
    s = (token or "").strip()
    if s in ("", "-", "0"):
        return 0.0
    m = _DF_SIZE_RE.match(s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    return num * _DF_FACTORS.get((m.group(2) or "").upper(), 1)


def _bytes_to_disk(total_b, used_b, label):
    """Build a dashboard disk dict, picking one unit (TB/GB/MB) from the total."""
    if not total_b or total_b <= 0:
        return None
    tib, gib, mib = 1024 ** 4, 1024 ** 3, 1024 ** 2
    divisor, unit = (tib, "TB") if total_b >= tib else \
                    (gib, "GB") if total_b >= gib else (mib, "MB")
    return {
        "label": label,
        "used": round(used_b / divisor, 2),
        "total": round(total_b / divisor, 2),
        "unit": unit,
    }


def parse_df_disks(text):
    """Parse `df -h` output into a list of dashboard disk dicts (one per row).

    Columns expected:  Filesystem  Size  Used  Avail  Use%  Mounted on
    The mount point becomes the disk label. Handles df wrapping a long device
    name onto its own line. Every parseable row is returned — scope the `df`
    command upstream if you don't want pseudo-filesystems shown.
    """
    disks = []
    if not text:
        return disks
    pending_fs = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("filesystem") or "mounted on" in low:
            continue  # header row
        tokens = line.split()
        # df wraps long device names: a lone filesystem token on its own line,
        # with the figures on the following line.
        if len(tokens) == 1 and not any(ch.isdigit() for ch in tokens[0]):
            pending_fs = tokens[0]
            continue
        if pending_fs and len(tokens) == 5:
            tokens = [pending_fs] + tokens
        pending_fs = None
        if len(tokens) < 6:
            continue
        size_b = _parse_size_to_bytes(tokens[1])
        used_b = _parse_size_to_bytes(tokens[2])
        if size_b is None or used_b is None:
            continue
        mount = " ".join(tokens[5:])
        disk = _bytes_to_disk(size_b, used_b, mount or tokens[0])
        if disk:
            disks.append(disk)
    return disks


def fetch_latest_ping_disks(uuid, n_pings):
    """Fetch the body of the latest ping and parse it as `df -h`.

    Uses GET /checks/<uuid>/pings/<n>/body where n is the check's n_pings
    (the most recent ping). Returns a list of disk dicts (possibly empty).
    """
    if not uuid or not n_pings:
        return []
    api = HEALTHCHECKS["api_url"].rstrip("/")
    url = "{}/checks/{}/pings/{}/body".format(api, uuid, n_pings)
    body = http_get_text(url, headers={"X-Api-Key": HEALTHCHECKS["api_key"]})
    if body is None:
        warn("no ping body for check {} (ping #{})".format(uuid, n_pings))
        return []
    disks = parse_df_disks(body)
    if not disks:
        warn("ping #{} body for {} did not parse as `df -h`".format(n_pings, uuid))
    return disks


def collect_routines():
    if not HEALTHCHECKS.get("enabled"):
        return []
    if not HEALTHCHECKS.get("api_key") or "REPLACE" in HEALTHCHECKS.get("api_key", "") \
            or HEALTHCHECKS["api_key"].startswith("YOUR_"):
        warn("Healthchecks api_key not set (export HEALTHCHECKS_API_KEY) — skipping routines")
        return []

    index = fetch_healthchecks_index()
    web_base = healthchecks_web_base()
    routines = []
    for r in HEALTHCHECKS["routines"]:
        chk = index.get(str(r.get("check", "")).lower())
        if chk is None:
            warn("Healthchecks check '{}' not found".format(r.get("check")))
            status = "warning"
            last_run = None
        else:
            # `started` flips the card to the live "Running" state.
            if chk.get("started"):
                status = "running"
            else:
                status = _HC_STATUS_MAP.get(chk.get("status", ""), "warning")
            last_run = chk.get("last_ping") or chk.get("last_start")

        # Build the "Read more" link dynamically from the UUID (falls back to a
        # legacy "url" key if a routine still has one).
        uuid = r.get("uuid")
        url = "{}/checks/{}/details/".format(web_base, uuid) if uuid else r.get("url", "#")

        entry = {
            "name": r["name"],
            "schedule": r.get("schedule", ""),
            "status": status,
            "lastRun": last_run,
            "url": url,
        }

        # ---- disks (parsed live from the latest ping body) -----------------
        if r.get("parse_disks"):
            # Only spend a request when the check is currently successful.
            if status == "ok" and chk and uuid:
                disks = fetch_latest_ping_disks(uuid, chk.get("n_pings"))
                if disks:
                    entry["disks"] = disks
            elif not uuid:
                warn("routine '{}' has parse_disks but no uuid".format(r["name"]))
        routines.append(entry)
    return routines


# ===========================================================================
#  ASSEMBLE  +  MAIN
# ===========================================================================
def build_metrics(state):
    var = parse_unraid_ini(CONFIG["var_ini"])
    disks = parse_unraid_ini(CONFIG["disks_ini"])

    disk_temps, pools, array = collect_array_and_storage(var, disks)

    network = collect_network(state)
    network["speedTest"] = collect_speedtest(state)
    network["tailscale"] = collect_tailscale()

    apps = collect_apps(state)
    routines = collect_routines()

    server_status = "updates" if any(a["status"] != "ok" for a in apps) else "ok"

    return {
        "server": {
            "name": get_server_name(var),
            "ip": get_primary_ip(),
            "uptime": get_uptime(),
            "status": server_status,
        },
        "metrics": {
            "cpu": {"usage": get_cpu_usage(), "temp": get_cpu_temp()},
            "memory": get_memory(),
            "disks": disk_temps,
            "pools": pools,
        },
        "array": array,
        "network": network,
        "apps": apps,
        "routines": routines,
        # Collection timestamp (UTC ISO-8601). The dashboard shows it as
        # "data from X ago".
        "generatedAt": iso_utc(),
    }


def main():
    start = time.time()
    info("collector starting (psutil={})".format("yes" if psutil else "no"))

    state = load_state()
    try:
        metrics = build_metrics(state)
    except Exception as e:  # noqa: BLE001 - never let one failure kill the run
        err("fatal error building metrics: {}".format(e))
        save_state(state)
        sys.exit(1)

    write_json_atomic(CONFIG["output_path"], metrics)
    save_state(state)

    info("wrote {} in {:.1f}s ({} apps, {} routines, {} pools)".format(
        CONFIG["output_path"], time.time() - start,
        len(metrics["apps"]), len(metrics["routines"]), len(metrics["metrics"]["pools"]),
    ))


if __name__ == "__main__":
    main()
