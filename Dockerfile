# =============================================================================
#  Dashboard web container
#
#  Design goals:
#    * Zero privileges. No docker.sock, no host network, no discovery daemons.
#    * Just an unprivileged nginx that serves one static HTML file and reads a
#      metrics.json that is dropped in by the host-side collector.
#
#  We use the *unprivileged* variant of the official nginx image. It already:
#    - runs the master + workers as uid/gid 101 (the "nginx" user),
#    - listens on 8080 (a non-privileged port, so NET_BIND_SERVICE is NOT
#      needed and every Linux capability can be dropped),
#    - writes its pid to /tmp and caches to /var/cache/nginx (both can be
#      backed by tmpfs so the root filesystem can be mounted read-only).
# =============================================================================
FROM nginxinc/nginx-unprivileged:alpine

# Image version, stamped at build time (e.g. --build-arg VERSION=v1.2.3). This
# lets the dashboard report its own running version in the monitored-apps card;
# the floating ':latest' deploy tag carries no version on its own. Empty when
# unset (harmless: the collector simply ignores a blank version label).
ARG VERSION=

LABEL org.opencontainers.image.title="unraid-monitor-dashboard" \
      org.opencontainers.image.description="Static home-server dashboard. Reads metrics.json produced by the host collector." \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/dineiar/unraid_monitor_dashboard"

# The base image drops to USER 101, which cannot write under the root-owned
# web root. Briefly become root to lay down our files + the data mountpoint,
# then drop straight back to the unprivileged user the container runs as.
USER root

# Replace the stock vhost with our hardened, no-cache-for-metrics config.
COPY nginx/default.conf /etc/nginx/conf.d/default.conf

# Ship the dashboard shell. The unprivileged image's web root is the same
# /usr/share/nginx/html path as the standard image.
COPY --chown=101:101 index.html /usr/share/nginx/html/index.html

# The live data lands at /usr/share/nginx/html/data/metrics.json at runtime
# via a read-only bind mount. We pre-create the directory (owned by uid 101)
# so the path resolves even before the very first collector run.
RUN mkdir -p /usr/share/nginx/html/data \
    && chown 101:101 /usr/share/nginx/html/data

# Back to the unprivileged user for the actual runtime.
USER 101

EXPOSE 8080

# Liveness: the page itself must be retrievable. busybox wget ships in alpine.
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
