# SuperMega Local Workcell — Linux/VPS runbook

A single-box deployment guide for one founder running the workcell on a rented
Linux VPS instead of the ROG Ally. Everything here assumes you are the only
operator and there is no ops team behind you.

Read **"What does NOT work on Linux"** and **"Known gaps"** at the bottom
before you promise anything to anybody. This packaging makes the coordinator
*runnable* on Linux. It does not make it *sellable* from Linux.

---

## 1. The box

Start with a private CPU VPS for the coordinator, queue and bounded inference.
8 GiB is the existing Compose floor (2 GiB coordinator, 5 GiB Ollama, 1 GiB
host reserve); 16 GiB is a planning target for headroom, not authorization to
increase concurrency or admit larger models. Recheck the provider quote,
region, tax, storage and backup charges before purchase. Old CX32/Contabo
price estimates have been removed. Current provider references:
[Hetzner plans](https://www.hetzner.com/cloud/cost-optimized/) and
[2026 price changes](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/).

### Operating architecture

- Keep the Ally as the operator console and retain existing product hosting.
- Put one coordinator, durable SQLite ledger, evidence store and one Ollama
  worker on the private VPS. No public model/dashboard ports.
- Engineering, QA, research and commercial roles share the queue; they are
  task roles, not one resident model per employee. Deterministic jobs run
  without inference; generated work stays draft until its acceptance checks pass.
- Reuse OpenCode as the coding interface. The Windows launcher is not a proven
  Linux coding worker: validate sandboxed file changes and protected tests on
  Linux before assigning unattended coding jobs.
- Start with the currently admitted 1B/3B model policy. Record task quality,
  latency, peak memory and cost per accepted result before changing models or
  adding a separate GPU inference host. No unmeasured throughput promises.
- Keep customer product data and production credentials out of the worker by
  default. Supply only task-scoped inputs; preserve explicit external-action gates.

Self-hosted tools avoid per-token API billing; server, storage and backup costs
remain. Cloud allocation, digest-pinned images, a restored state rehearsal and
one accepted evidence-bound job are required before calling this operational.

Pick Ubuntu 24.04 LTS. Add your SSH public key at provisioning time so the box
never has a password-authenticated window.

---

## 2. First boot hardening

Do this before you install Docker, in one sitting, from the provider console or
your first SSH session.

```bash
# --- a non-root operator account -------------------------------------------
adduser --disabled-password --gecos "" founder
usermod -aG sudo founder
install -d -m 700 -o founder -g founder /home/founder/.ssh
install -m 600 -o founder -g founder /root/.ssh/authorized_keys \
    /home/founder/.ssh/authorized_keys

# --- key-only SSH ----------------------------------------------------------
cat >/etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PermitEmptyPasswords no
MaxAuthTries 3
AllowUsers founder
X11Forwarding no
EOF
sshd -t && systemctl reload ssh
```

**Open a second SSH session and confirm you can still get in as `founder`
before you close the first one.** Locking yourself out of a fresh VPS is
recoverable via the provider console; locking yourself out of a VPS with a
month of company state on it at 2am is not.

```bash
# --- firewall --------------------------------------------------------------
apt-get update && apt-get install -y ufw fail2ban unattended-upgrades
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp        # SSH
# ufw allow 443/tcp     # ONLY if you enable the reverse proxy. You should not.
ufw --force enable

# --- automatic security patches -------------------------------------------
dpkg-reconfigure -plow unattended-upgrades   # answer "Yes"
systemctl enable --now unattended-upgrades

# --- SSH brute-force throttling -------------------------------------------
cat >/etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
mode = aggressive
maxretry = 3
bantime = 1h
findtime = 10m
EOF
systemctl enable --now fail2ban
```

> **Docker bypasses ufw.** Docker writes its own `DOCKER` iptables chain that is
> evaluated *before* ufw's rules, so any container port published with `-p` or a
> compose `ports:` stanza is reachable from the internet even with
> `ufw default deny incoming` in force. This is the single most common way a
> "firewalled" VPS ends up serving an unauthenticated dashboard to the world.
> The compose file in this repo publishes **no ports at all**, which is why. If
> you ever add a `ports:` entry, bind it explicitly to loopback
> (`"127.0.0.1:8765:8765"`), never the bare form.

Install Docker last:

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker founder     # log out and back in for this to take effect
```

---

## 3. Deploy

```bash
su - founder
git clone <your-remote> ~/local-agent-company
cd ~/local-agent-company
docker compose up -d --build
docker compose ps          # ollama should reach "healthy" within ~30s
```

The build has exactly one network fetch (pip pulling setuptools for build
isolation) and produces **zero** runtime dependencies — `pyproject.toml`
declares `dependencies = []` and that is not an accident. If you want a fully
offline build, the Dockerfile documents how to drop the pip step entirely and
run `python -m local_company.cli` off `PYTHONPATH=/app/src`.

### Pull the model

The `ollama` binary lives in the **ollama** service, not the coordinator:

```bash
docker compose exec ollama ollama pull llama3.2:1b
docker compose exec ollama ollama list
```

`llama3.2:3b` is also admitted by policy, but on an 8 GB box run one or the
other, never both resident.

### Initialise the store

```bash
docker compose exec coordinator local-company init
```

State lands in the `company-state` volume at `/state` — `company.db` plus
`outputs/`. Nothing stateful is ever written into an image layer.

---

## 4. Verify the port before you trust it

```bash
docker compose exec coordinator python /app/deploy/verify_linux_port.py
```

One JSON object, exit 0 on pass and 1 on anything else. It checks:

- Python ≥ 3.11 and that you are actually on Linux, in a container;
- **`capacity.observe_memory()` returns `"ready"` with a plausible byte
  count** — see below;
- `LOCAL_COMPANY_HOME` exists, is writable, is on a real mount (not the
  container's ephemeral writable layer), and is not inside a cloud-sync
  directory;
- the Ollama endpoint answers and has a policy-supported model installed;
- `computer_use` and `local_company.cli` both import, but a desktop entrypoint
  fails closed with `computer_use_requires_windows` before any UI operation;
- the configured Ollama host is either exact loopback or the admitted Compose
  sidecar (`http://ollama:11434`), not an arbitrary network endpoint.

**Why the memory check is first among equals.** Before its POSIX branch landed,
`observe_memory()` returned `{"status": "unavailable"}` on Linux. Nothing
crashed and nothing logged. The MCP execution path just answered
`{"status": "blocked", "reason": "available_memory_unavailable"}` to every
request, forever, and `machine_capacity_snapshot` reported `"indeterminate"`
rather than a failure. A port can look completely healthy and be structurally
incapable of running a single mission. That is why the byte count is asserted
to be *plausible*, not merely non-crashing.

The container also gives that gate something real to read: the compose memory
limits mean `observe_cgroup_memory()` finds `/sys/fs/cgroup/memory.max` and the
gate takes the minimum of the cgroup ceiling and the host reading. Without a
limit the coordinator would see the whole 8 GB host and admit work the
container cannot run. If `verify_linux_port.py` reports the advisory
`no_cgroup_memory_limit_visible`, your limits are not being applied — check
that you are on cgroup v2 (`stat -fc %T /sys/fs/cgroup` → `cgroup2fs`).

Then run readiness the usual way:

```bash
docker compose exec coordinator python scripts/check_readiness.py
```

---

## 5. Run the test suite in the container

```bash
docker compose exec coordinator python scripts/run_tests.py
docker compose exec coordinator python scripts/run_tests.py --verbose   # names
```

Emits a `local-company.tests.v3` summary object; exit 0 on pass.

Two things to expect:

1. The Windows-only entrypoints must import safely and then fail closed at
   call time. Repository Ubuntu CI has run the full suite, but that does not
   verify this particular container, model sidecar, state volume, or mission.
   Require exit 0 and inspect the reported test count on this exact build.
2. `/app` is root-owned and read-only to the runtime user by design (the source
   tree is immutable so the build-manifest digest cannot drift). Tests write
   through `TMPDIR`, which is a 512 MB tmpfs. If a test ever needs to write to
   its working directory it will fail with `EACCES` — that is the guard doing
   its job, not a packaging bug.

---

## 6. Reaching the dashboard safely

**Never expose the dashboard.** It has no user authentication of any kind. Its
only defences are binding `127.0.0.1` and a Host-header allowlist that accepts
exactly `127.0.0.1:<port>` and `localhost:<port>`. Its mutation token is
embedded in the rendered HTML, so anyone who can *read* the page can also
*write* whenever `LOCAL_COMPANY_SERVICE_TOKEN` is set.

There is a second, mechanical reason not to try: the dashboard binds loopback
*inside the container's network namespace*. A compose `ports:` entry publishes a
port that nothing is listening on. You would need a sidecar sharing the
coordinator's netns plus a Host-header rewrite to get a single byte out — see
the commented `caddy` block in `docker-compose.yml` for exactly how much
machinery that takes, and treat the length of that comment as the argument
against doing it.

### Option 0 — skip the dashboard (what you should actually do)

Everything the dashboard shows read-only is available as JSON over SSH, with
your shell history as the audit trail:

```bash
ssh founder@your-vps
cd ~/local-agent-company
docker compose exec coordinator local-company health
docker compose exec coordinator local-company brief --project <id>
docker compose exec coordinator local-company capacity --project <id>
```

No listener, no Host allowlist, no token in any HTML. Start here and only move
on if you genuinely need the rendered view.

### Option 1 — SSH tunnel to a netns-sharing relay

The port arithmetic here is the whole trick, so read it once carefully. The
allowlist is built from the **dashboard's own listening port** (`8765`), and
the `Host` header your browser sends is built from the **local** end of the
tunnel. Make the local end `8765` and the two agree no matter what ports sit in
between.

```bash
# 1. Publish a relay port on the coordinator (compose, coordinator service):
#      ports:
#        - "127.0.0.1:8766:8766"      # loopback-bound: ufw-safe, see §2
#
# 2. Relay 8766 -> the dashboard's loopback 8765, inside the coordinator's
#    own network namespace. 8765 is already taken in that namespace, which is
#    why the relay uses a different port.
docker run --rm -d --name workcell-tap \
  --network "container:$(docker compose ps -q coordinator)" \
  alpine/socat TCP-LISTEN:8766,fork,reuseaddr TCP:127.0.0.1:8765

# 3. From your laptop. Local port 8765 is the part that matters:
ssh -N -L 8765:127.0.0.1:8766 founder@your-vps
```

Open `http://127.0.0.1:8765` locally. The browser sends `Host: 127.0.0.1:8765`,
which is exactly what the allowlist expects, and the traffic lands on the
dashboard through the relay. Tear the tap down (`docker rm -f workcell-tap`)
when you are done; it is a debugging tool, not a service.

### Option 2 — `network_mode: host` for the coordinator

Simpler and legitimate for a single-box deploy: the dashboard binds the
**host's** loopback directly, so `ssh -N -L 8765:127.0.0.1:8765 founder@your-vps`
works with no relay at all. The cost is that you give up bridge DNS — point
`LOCAL_COMPANY_OLLAMA_HOST` at `http://127.0.0.1:11434` and publish the ollama
service on `127.0.0.1:11434` — and you give up network isolation between the
two services. Fine for one operator; do not ship it as the default.

### Tailscale (better if you check in from more than one machine)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh --accept-routes=false
```

Then run the same `ssh -L` over the tailnet. Do **not** use `tailscale serve`
to publish the dashboard: a tailnet is a smaller blast radius, not an
authentication system, and the mutation token is still sitting in the HTML for
every device you own — including the phone you will eventually lose.

**Read-only is the only safe mode.** Start the coordinator *without*
`LOCAL_COMPANY_SERVICE_TOKEN`; no `LocalQueueWorker` is constructed and every
mutating route answers `405 Dashboard is read-only`. Queue and approve through
`docker compose exec coordinator local-company ...` over SSH, where the audit
trail is your shell history and the approval is a keystroke you typed.

---

## 7. Backups

The `company-state` volume is the whole business: every mission, receipt,
approval, quality record, and export. `ollama-models` is regenerable from
`ollama pull` — back it up only to save bandwidth.

**Never `cp` a live `company.db`.** SQLite with a hot WAL copies to a torn file
that restores clean and is missing the last transactions. Use the backup API:

```bash
# Create a private destination, then a new verified snapshot (never overwrite).
docker compose exec -T coordinator mkdir -p /state/backup
docker compose exec -T coordinator python deploy/snapshot_database.py \
  /state/company.db /state/backup/company-20260928.db
# Choose a unique dated filename for each run. A PASS verifies SQLite integrity
# only; outputs/evidence files still require the encrypted full-volume backup.
```

**restic — full volume, off-box, encrypted:**

```bash
sudo apt-get install -y restic
export RESTIC_REPOSITORY="s3:s3.eu-central-1.amazonaws.com/your-bucket/workcell"
export RESTIC_PASSWORD_FILE=/root/.restic-pass   # chmod 600, and keep a copy
                                                 # somewhere that is not this box
restic init
restic backup /var/lib/docker/volumes/local-workcell_company-state/_data
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
```

Put it on a timer:

```bash
# /etc/systemd/system/workcell-backup.timer -> OnCalendar=daily, Persistent=true
systemctl enable --now workcell-backup.timer
```

**Litestream — continuous replication of `company.db` specifically:** streams
the WAL to object storage so your worst case is seconds of loss instead of a
day. Worth adding once the box is doing real work; restic alone is an
acceptable start.

> ### Test a restore. Actually do it.
> An untested backup is a belief, not a backup, and the failure mode is
> discovering on the worst day of the quarter that you have been dutifully
> archiving a torn database for six months. Once a month:
>
> ```bash
> (
> set -eu
> restic restore latest --target /tmp/restore-test
> # Restic preserves the original absolute path beneath its target.
> restored=/tmp/restore-test/var/lib/docker/volumes/local-workcell_company-state/_data
> scratch=$(mktemp -d /tmp/workcell-recovery.XXXXXX)
> cp -a "$restored/." "$scratch/"
> # Use the accepted snapshot, not the raw database captured during live writes.
> rm -f "$scratch/company.db-wal" "$scratch/company.db-shm"
> cp "$scratch/backup/company-20260928.db" "$scratch/company.db"
> sudo chown -R 10001:10001 "$scratch"
> docker run --rm -v "$scratch:/state" local-workcell/coordinator:0.1.0 \
>     local-company health
> # Compare restored store identity and receipt/evidence references with the
> # recorded source. Never point this drill at the live company-state volume.
> )
> ```
>
> A restore that does not produce a readable store with the identity you expect
> is a failed backup, regardless of what the backup log said.

---

## 8. Updating

Order matters. The build manifest is stamped **on the host, in the git
checkout, before the image is built** — the running service compares its
compiled-in digest against the on-disk manifest, and a rebuild that skips the
stamp will report `restart_required` or `build_identity_conflict` at the next
readiness check.

```bash
cd ~/local-agent-company
docker compose exec coordinator local-company health    # confirm idle first
git pull

python scripts/stamp_build_manifest.py --check          # what drifted?
python scripts/stamp_build_manifest.py --write --build-id local-build-$(date +%Y%m%d).1
git add src/local_company/build_info.py && git commit -m "Stamp build manifest"

docker compose build coordinator
docker compose up -d
docker compose exec coordinator python /app/deploy/verify_linux_port.py
docker compose exec coordinator python scripts/check_readiness.py
```

Note that `deploy/` and the container files are **outside** the release digest —
it covers `src/local_company/*.py` plus a fixed tuple of `scripts/*` paths — so
editing this runbook or the Dockerfile does not require a re-stamp. Editing
anything under `src/local_company/` does.

Roll back by checking out the previous tag and repeating from
`docker compose build`. The state volume is untouched by either direction;
schema migrations are the only thing that would make a rollback lossy, so read
the diff before you pull.

---

## 9. What does NOT work on Linux

Be specific about this with yourself. Two whole capabilities do not cross over.

### The Windows desktop automation workcell — excluded by platform

`src/local_company/computer_use.py` is ctypes/WinAPI throughout and is
**excluded from this deployment by design**. It cannot be ported without being
rewritten against a different automation substrate.

Until `local-build-20260817.2` this was worse than unavailable — it was
**fatal at import time**, which is worth recording because it is the single
thing that kept this project on one Windows handheld. `computer_use.py` did
`from ctypes import wintypes` at module scope, and `ctypes.wintypes` declares
`VARIANT_BOOL` with `_type_ = "v"`, a format code CPython registers only on
Windows builds — so that import raised `ValueError: _type_ 'v' not supported`
on Linux. Because `cli.py` imports `.computer_use` unconditionally, **every**
subcommand died before `main()` ran, `workflow_pilot` was unimportable, and
`mcp_server._execute_next()` — which shells out to
`python -m local_company.cli` — meant MCP-driven execution stayed dead even
once the memory gate passed.

**That import is now guarded** (`try/except (ImportError, ValueError)` with
`wintypes = None`), which fixes all of those consumers at once rather than one
call site. All 28 test modules now import on a host without `ctypes.wintypes`,
verified by `tests/test_computer_use.py::NonWindowsImportTests`, which blocks
the module through a `sys.meta_path` finder in a subprocess.

What remains true: the desktop workcell itself still **does nothing on Linux**.
Its entry points fail closed at call time through the existing `os.name`
checks, raising `computer_use_requires_windows`. That is the correct behaviour
— you cannot drive Windows UI Automation from a headless container — but do
not mistake "imports cleanly" for "works".

> The tests **do pass** on Linux at runtime: `.github/workflows/ci.yml` runs
> the full suite on `ubuntu-latest`, Python 3.11 through 3.13, and as of
> 2026-08-19 all three legs are green and required for merge (the earlier
> `continue-on-error` excusal has been removed). That verifies import and
> runtime behavior under CI's container, not this runbook's specific
> deployment target - re-run `python deploy/verify_linux_port.py` after any
> change to this Dockerfile or the VPS steps below.

### Browser QA — needs a substrate swap, then a fresh acceptance rehearsal

`browser_operator.py` drives **installed Microsoft Edge** through the pinned
`agent-browser` npm CLI. It also now looks for a Linux `npm install`'s
`agent-browser-linux-x64` binary and falls back to `google-chrome`/`chromium`
on PATH, so discovery itself is no longer Windows-only - but that alone does
not make browser QA work on a VPS. There is still no headless browser and no
`DISPLAY` there.

Substituting Playwright/Chromium is the obvious path and is a real project, not
a config change: the pinned-version check, the sealed suite manifest, and the
evidence format all assume the current CLI's output shape.

**And the substitution invalidates the acceptance evidence.** The browser QA
lane's acceptance rehearsal — fresh-install, deliberate-failure, and
10-consecutive-run — was performed against Edge + agent-browser on Windows.
A different browser, a different driver, and a different OS is a different
system under test. **That rehearsal must be re-done end to end on the new
substrate before a single customer-facing QA report is sold off it.** Selling a
report produced by an unrehearsed lane is selling evidence you have not earned.

---

## 10. Known gaps to close before exposing anything to a customer

Ordered by what stops you first.

1. ~~**`cli.py` imports the Windows-only module eagerly.**~~ **Landed
   (`local-build-20260817.2`).** The cause was narrower than the call site:
   `computer_use.py` did `from ctypes import wintypes` at module scope, and
   `ctypes.wintypes` raises `ValueError` on Linux (it declares `VARIANT_BOOL`
   with the Windows-only `"v"` format code). That import is now wrapped in
   `try/except (ImportError, ValueError)` with `wintypes = None`, which fixes
   every importer at once — `cli`, `workflow_pilot`, and therefore
   `mcp_server._execute_next()`'s `python -m local_company.cli` subprocess —
   rather than only the one call site. Windows entry points still fail closed
   at call time via the existing `os.name` checks
   (`computer_use_requires_windows`). Regression test:
   `tests/test_computer_use.py::NonWindowsImportTests`, which blocks
   `ctypes.wintypes` through a `sys.meta_path` finder in a subprocess and
   asserts all four modules still import.
2. ~~**`OllamaModel` has no configurable host.**~~ **Landed.**
   `core.default_ollama_host()` now reads `LOCAL_COMPANY_OLLAMA_HOST`,
   validates scheme/host/port and rejects credentials and paths, and
   `OllamaModel(host=None)` defaults through it. `docker-compose.yml` sets
   `http://ollama:11434`, which satisfies that validator. Verify with
   `docker compose exec coordinator python -c "from local_company.core import
   default_ollama_host; print(default_ollama_host())"`.
3. ~~**The readiness gate rejects the Compose Ollama sidecar.**~~ **Landed
   (`local-build-20260914.1`).** Runtime attestation now distinguishes exact
   `http://ollama:11434` from arbitrary nonlocal hosts. The readiness checker
   requires its local configuration to agree with that live attestation and
   probes the same sidecar instead of its old hard-coded loopback. The Linux
   port verifier no longer emits the obsolete mismatch advisory. These are
   contract tests, not a passed mission in a real Linux container.
4. **The dashboard has no authentication and cannot safely be exposed.** Its
   mutation token is in the HTML; read access is write access whenever a
   service token is set. SSH tunnel or Tailscale only, read-only mode only. A
   customer-visible view needs actual authentication, not a reverse proxy in
   front of the current design.
5. **The dashboard binds loopback by hardcode**, so it is unreachable from
   outside the container's netns even for legitimate local access. A
   `LOCAL_COMPANY_DASHBOARD_BIND` env var (defaulting to `127.0.0.1`, with the
   Host allowlist extended to match the configured authority) is the clean fix.
6. **The exact Linux container still needs a full acceptance run.** The
   import guards and Ubuntu CI are in place; run the full suite, Linux port
   verifier, authoritative readiness, and one evidence-bound queued mission
   against this specific built image before calling it an Ally replacement.
7. **Browser QA acceptance must be re-rehearsed** on the replacement substrate
   before anything produced by it is sold. See §9.
8. **`ollama/ollama:latest` is unpinned.** Pin a digest before you call any of
   this production; a silent base-image bump is a silent change to the thing
   generating customer deliverables.
9. **No restore has been tested yet.** Until you have restored into a scratch
   container and seen `local-company health` come back with the identity you
   expect, you do not have backups. See §7.
10. **Single box, no redundancy, no monitoring.** A €6.80/mo VPS with one
    volume is a fine place to run your own work and a poor place to hold data
    a customer expects to still exist next quarter. Decide which one this is
    before you invite anybody onto it.
