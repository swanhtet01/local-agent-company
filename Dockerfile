# syntax=docker/dockerfile:1
#
# SuperMega Local Workcell - coordinator image.
#
# Design constraints this file honours:
#   * The package declares `dependencies = []`. There is no third-party runtime
#     dependency to install, so there is no requirements.txt layer and no wheel
#     cache to warm. The single network fetch in this build is pip's own
#     build-isolation download of setuptools, which produces NO runtime
#     dependency. See the fully offline alternative below the pip line.
#   * The image carries source only. All state lives under LOCAL_COMPANY_HOME,
#     which is /state here and is expected to be a volume. Nothing stateful is
#     copied in (see .dockerignore).
#   * The process runs as a non-root user (uid/gid 10001).
#   * HEALTHCHECK is model-free: it proves the state directory is writable and
#     the POSIX memory reading that gates admission is readable. It never calls
#     Ollama and never loads a model.

FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE matters here, not just as hygiene: /app is owned by
# root and the runtime user cannot create __pycache__ next to the sources.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONPATH=/app/src \
    LOCAL_COMPANY_HOME=/state

# Fixed uid/gid so a bind-mounted host directory can be chowned predictably:
#   sudo chown -R 10001:10001 /srv/workcell-state
RUN groupadd --gid 10001 company \
 && useradd --uid 10001 --gid 10001 --home-dir /home/company \
            --create-home --shell /usr/sbin/nologin company

WORKDIR /app

# Copy the source tree. tests/ and scripts/ are included on purpose: the
# operator is expected to run `python scripts/run_tests.py` and
# `python scripts/check_readiness.py` inside this container.
COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
COPY tests ./tests
COPY deploy ./deploy
COPY examples ./examples
COPY AGENTS.md ACCEPTANCE.md OPERATOR.md PRODUCT.md README.md ./

# Install the package so the `local-company` console entry point exists on PATH.
# PYTHONPATH=/app/src still wins at import time, so the repo tree at /app is the
# authoritative copy; the site-packages copy is byte-identical and only exists
# to provide the entry-point shim.
#
# Fully offline alternative (no pip, no network at all): delete the RUN below
# and invoke the CLI as `python -m local_company.cli` instead of
# `local-company`. PYTHONPATH=/app/src is sufficient on its own.
RUN pip install --no-cache-dir . \
 && rm -rf /app/*.egg-info /root/.cache \
 && find /app -type d -name '__pycache__' -prune -exec rm -rf {} +

# /state is created root-owned then handed to the runtime user. Docker seeds a
# fresh NAMED volume from the image's directory, ownership included, so the
# volume comes up writable for uid 10001 without an entrypoint chown. A BIND
# mount does not inherit this - chown it on the host first.
RUN mkdir -p /state && chown company:company /state && chmod 700 /state
VOLUME ["/state"]

# /app stays root-owned and read-only to the runtime user. That is deliberate:
# the source tree is immutable at runtime and the build manifest digest over it
# cannot drift. Tests write through tempfile, i.e. TMPDIR, not cwd.
ENV TMPDIR=/tmp

# serve_dashboard() shuts down cleanly on KeyboardInterrupt. Docker's default
# SIGTERM does not raise that in CPython, so ask for SIGINT and let the server
# run its server_close() path instead of being killed mid-request.
STOPSIGNAL SIGINT

# Model-free liveness: state dir writable + /proc/meminfo MemAvailable present
# and positive. MemAvailable is exactly the reading capacity.observe_memory()
# depends on; if it disappears, the admission gate silently blocks every
# execution, so it is worth failing the container over.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,sys; home=os.environ.get('LOCAL_COMPANY_HOME','/state'); probe=os.path.join(home,'.healthprobe'); os.close(os.open(probe,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)); os.unlink(probe); raw=open('/proc/meminfo','rb').read(65536).decode('utf-8','replace'); rows=[r.split() for r in raw.splitlines() if r.startswith('MemAvailable:')]; sys.exit(0 if len(rows)==1 and len(rows[0])==3 and int(rows[0][1])>0 else 1)"]

USER company

EXPOSE 8765

# The dashboard binds 127.0.0.1 by hardcode (dashboard.py) and its Host-header
# allowlist accepts only 127.0.0.1:<port> and localhost:<port>. Inside a
# container that loopback belongs to the container's own network namespace, so
# `-p 8765:8765` publishes a port that nothing is listening on. Reach it with
# `docker compose exec`, or via a sidecar that shares this container's netns.
# See deploy/README.md, "Reaching the dashboard safely".
CMD ["local-company", "dashboard", "--port", "8765"]
