# VPS setup instructions (for an AI agent)

You are an AI agent with shell access to an Ubuntu server. The operator wants BonReaperBot installed and running
here. Follow the steps in order, check each expected result, and finish with the report in step 14. Stop and ask
the operator whenever a step says **Ask**.

Repository: `https://github.com/ayhanarashtasin/BonReaperBot.git`

> **Operator:** give the agent this prompt.
>
> ```text
> Set up BonReaperBot on this Ubuntu VPS. Clone https://github.com/ayhanarashtasin/BonReaperBot.git,
> then follow instruction.md in that repository exactly, step by step. Stop and ask me whenever it
> says Ask. When you finish, give me the report described in its last step.
> ```

## What this project is

- A research and **paper-trading** platform for Polymarket's BTC 5-minute Up/Down markets. It reads public market
  data and simulates orders and fills.
- **It cannot place real orders.** There is no live adapter, and `mode: live` always fails with `LIVE_DISABLED`. No
  wallet, private key or API secret is needed for anything in these steps.
- Every profit or loss it reports is **simulated**. So far every strategy loses money in simulation (see
  `reports/readiness_memo.md`). A faster server does not change that.

## Rules

1. Never add, enable or work around live trading. Do not set `mode: live`, `live_enabled` or
   `live_operator_approval`, and do not install or write any order-placing client.
2. Never ask for, create or store wallet keys, seed phrases or trading API secrets. If the operator offers one,
   decline and explain that nothing here needs it.
3. Never bypass geographic restrictions: no VPN, proxy or Tor for Polymarket. Report the geoblock result as it is.
4. Never expose the dashboard to the internet. It listens only on `127.0.0.1:8765`, which is how it is meant to be
   reached (step 9). Do not add a reverse proxy, open port 8765 or change the bind address.
5. Run the bot as the unprivileged user `bonereaper`, never as root.
6. Do not change firewall or SSH settings unless the operator asks. The bot opens no public port.
7. Do not delete recorded data or reports without the operator's approval.
8. Do not weaken anything to make a step pass: do not edit risk limits, rule hashes, tests or the secret scan. If a
   step fails, stop and report the exact error.
9. Do not commit or push unless the operator asks (step 13).

## Requirements

| Item | Needed |
|---|---|
| OS | Ubuntu 22.04 or 24.04 LTS, x86_64 or arm64 |
| CPU and RAM | 2 vCPU and 2 GB RAM to run it; 4 GB or more for `replay` and `compare` research |
| Disk | About 5 GB per day while it runs 24/7 (raw market data). 50 GB or more recommended |
| Network | Outbound HTTPS and WebSocket to `*.polymarket.com`. `api.binance.com` is used only for a clock check and is blocked in some countries, which is fine |
| Access | A user that can run `sudo` |

## Step 1. Record the server facts

```bash
lsb_release -ds; uname -m; nproc; free -h; df -h /; timedatectl
```

Keep this output for the report. If `uname -m` prints `aarch64`, the server is arm64 (see step 5).

## Step 2. Install system packages

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
```

## Step 3. Synchronize the clock

The bot raises a critical alert when the clock is more than 1000 ms off, so time sync must be on.

```bash
sudo timedatectl set-ntp true
sleep 30
timedatectl show -p NTPSynchronized --value
```

Expected: `yes`. If it prints `no`, or `set-ntp` fails, use chrony instead:

```bash
sudo apt-get install -y chrony
sudo systemctl enable --now chrony
sleep 30
chronyc tracking
```

Expected: `Leap status : Normal` and a `System time` offset well under 0.1 seconds.

## Step 4. Create the service user

```bash
id bonereaper >/dev/null 2>&1 || sudo adduser --disabled-password --gecos "" bonereaper
```

The next steps run commands **as `bonereaper`** through `sudo -u bonereaper -H bash -s <<'EOF' ... EOF`. Use them
exactly as written. The embedded Postgres used by some tests refuses to run as root.

## Step 5. Install uv, get the code and install dependencies

```bash
sudo -u bonereaper -H bash -s <<'EOF'
set -euo pipefail
cd "$HOME"
curl -LsSf https://astral.sh/uv/0.13.1/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
if [ -d BonReaperBot/.git ]; then
  git -C BonReaperBot pull --ff-only
else
  git clone https://github.com/ayhanarashtasin/BonReaperBot.git
fi
cd BonReaperBot
if [ "$(uname -m)" = "aarch64" ]; then
  uv sync --frozen --dev --no-install-package pgserver
else
  uv sync --frozen --dev
fi
EOF
```

- uv installs the pinned Python 3.11 by itself. Every dependency has a prebuilt Linux wheel, so nothing compiles.
- On arm64, `pgserver` (an embedded Postgres used only by a few tests) has no wheel. It is skipped, and those tests
  report as skipped.
- If `git clone` fails with an authentication error, the repository is private. Continue with the next section.

### Private repository only

Create a read-only deploy key:

```bash
sudo -u bonereaper -H bash -s <<'EOF'
set -euo pipefail
mkdir -p ~/.ssh && chmod 700 ~/.ssh
[ -f ~/.ssh/bonreaperbot_deploy ] || ssh-keygen -t ed25519 -N "" -C "bonreaperbot-vps" -f ~/.ssh/bonreaperbot_deploy
cat ~/.ssh/bonreaperbot_deploy.pub
EOF
```

**Ask** the operator to add the printed **public** key at GitHub, repository Settings, Deploy keys, with write access
off. Never show or copy the private key. Once they confirm, clone over SSH, then run step 5 again:

```bash
sudo -u bonereaper -H bash -s <<'EOF'
set -euo pipefail
cd "$HOME"
GIT_SSH_COMMAND="ssh -i ~/.ssh/bonreaperbot_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  git clone git@github.com:ayhanarashtasin/BonReaperBot.git
git -C BonReaperBot config core.sshCommand "ssh -i ~/.ssh/bonreaperbot_deploy -o IdentitiesOnly=yes"
EOF
```

## Step 6. Verify the checkout (offline)

```bash
sudo -u bonereaper -H bash -s <<'EOF'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd ~/BonReaperBot
uv run ruff check .
uv run mypy src
uv run python scripts/check_secrets.py
uv run pytest -q
EOF
```

Expected: `All checks passed!` from ruff, `Success` from mypy, `secret scan: 0 finding(s)`, and every test passing
(579 when this was written; a few skipped on arm64 is fine). On any failure, stop and report it.

## Step 7. Verify live public data (still no orders)

```bash
sudo -u bonereaper -H bash -s <<'EOF'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd ~/BonReaperBot
uv run bone-reaper doctor --config config/read_only.yaml
uv run python scripts/smoke_read_only.py
uv run bone-reaper markets --symbol BTC --duration 5
uv run bone-reaper paper --config config/paper.yaml --output reports/paper-smoke --seconds 120
EOF
```

| Output | Expected | If not |
|---|---|---|
| doctor `ok` | `true` | Stop and report |
| doctor `read_only_no_adapter` and `live_disabled` | `true` | Stop: a safety gate is broken |
| doctor `geoblock.status` | Any value; record it | Research still works when blocked. Never work around it |
| doctor `clock_offset_ms` | Between -1000 and 1000, ideally under 100 | Redo step 3. `unavailable:...` means Binance is unreachable from here; rely on step 3's check instead |
| smoke script | A market slug, order book levels and stream event counts | Report the error and check outbound access to `*.polymarket.com` |
| `markets` | At least one accepted market | Report the rejection reasons. Never edit rule hashes to make it pass |
| paper smoke run | `"shutdown"` with `"clean": true` | Report it. One order still open at the stop can happen |

## Step 8. Run it 24/7 with systemd

This sets up three things:

- `bonereaper-paper`: the paper session on the live public feed.
- `bonereaper-dashboard`: the read-only dashboard.
- A timer that starts a fresh paper session (a new $100 simulated account) every day at 00:05 UTC.

The timer is needed because the $10 simulated daily loss stop (`loss_cap`) stays latched until the process restarts.
After it trips, the session keeps recording market data but places no more simulated orders.

Launcher script. Each start writes to a new `reports/paper-<UTC time>/` folder, so a restart never overwrites an
earlier session:

```bash
sudo tee /home/bonereaper/run-paper.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd /home/bonereaper/BonReaperBot
exec .venv/bin/bone-reaper paper --config config/paper.yaml --output "reports/paper-$(date -u +%Y%m%dT%H%M%SZ)"
EOF
sudo chown bonereaper:bonereaper /home/bonereaper/run-paper.sh
sudo chmod 755 /home/bonereaper/run-paper.sh
```

Units:

```bash
sudo tee /etc/systemd/system/bonereaper-paper.service >/dev/null <<'EOF'
[Unit]
Description=BonReaperBot paper session (live public data, simulated orders only)
Wants=network-online.target time-sync.target
After=network-online.target time-sync.target

[Service]
Type=simple
User=bonereaper
Group=bonereaper
WorkingDirectory=/home/bonereaper/BonReaperBot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/bonereaper/run-paper.sh
# SIGINT acts like Ctrl+C: the session cancels its simulated orders and writes the final report.
KillSignal=SIGINT
TimeoutStopSec=60
Restart=always
RestartSec=30
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/bonereaper-dashboard.service >/dev/null <<'EOF'
[Unit]
Description=BonReaperBot read-only dashboard (127.0.0.1:8765 only)
After=network.target

[Service]
Type=simple
User=bonereaper
Group=bonereaper
WorkingDirectory=/home/bonereaper/BonReaperBot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/bonereaper/BonReaperBot/.venv/bin/bone-reaper dashboard --port 8765 --root /home/bonereaper/BonReaperBot
KillSignal=SIGINT
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/bonereaper-paper-restart.service >/dev/null <<'EOF'
[Unit]
Description=Start a fresh BonReaperBot paper session

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart bonereaper-paper.service
EOF

sudo tee /etc/systemd/system/bonereaper-paper-restart.timer >/dev/null <<'EOF'
[Unit]
Description=Fresh BonReaperBot paper session every day at 00:05 UTC

[Timer]
OnCalendar=*-*-* 00:05:00 UTC

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now bonereaper-paper.service bonereaper-dashboard.service bonereaper-paper-restart.timer
```

Check after two minutes:

```bash
systemctl status --no-pager bonereaper-paper bonereaper-dashboard
systemctl list-timers --no-pager bonereaper-paper-restart.timer
sudo journalctl -u bonereaper-paper -n 30 --no-pager
sudo ls -1 /home/bonereaper/BonReaperBot/reports | grep paper-
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/api/state
```

Expected: both services `active (running)`, the timer listed with its next run, a new `paper-<time>` folder that
contains `live.json`, and HTTP `200`.

## Step 9. Dashboard access

Give the operator this command for their own computer (Windows PowerShell, macOS or Linux), with `SSH_USER` and
`VPS_IP` replaced by their SSH login and the server's address:

```bash
ssh -N -L 8765:127.0.0.1:8765 SSH_USER@VPS_IP
```

While it runs, the dashboard is at <http://127.0.0.1:8765> on their computer. If port 8765 is already in use there,
use `-L 8766:127.0.0.1:8765` and open port 8766 instead.

## Step 10. Read the logs

`sudo journalctl -u bonereaper-paper -f` shows JSON log lines. These are normal:

| Message | Meaning |
|---|---|
| `data gap` with reason `recorder_started` | Logged at every start |
| `rollover` `prepared`, `activated`, `retired` | A new 5-minute market, every 5 minutes |
| `clock probe failed` | The Binance time check was unreachable. Harmless if step 3 shows the clock synchronized |
| `discovery_failed` or `rest_sample_failed` gaps | A short network blip. It retries by itself |
| kill event `near_expiry` | Quoting stops 20 s before each market closes |
| kill event `stale_feed` | The feed paused for over 1 s. It clears by itself |
| `loss_cap` in `kill_active_at_end` | The $10 simulated daily loss was reached. Trading resumes after the 00:05 UTC restart. Expected with this strategy |
| `twap_status` `unavailable` after 2026-11-30 | The public Chainlink TWAP feed shuts down that day. Tell the operator. Do not look for an unofficial replacement |

Investigate and report any of these: a service in `failed` state, a `clock_skew` alert, `book_invalidations` growing
steadily, non-empty `ledger.conservation_errors`, `reconciliation` not clean, or `unknown_remaining` above 0. Session
metrics are in `reports/paper-<time>/live.json` while running and `metrics.json` after it stops.

## Step 11. Disk space

Each paper session records the raw public feed: about 200 MB per hour, or about 5 GB per day. Work out how many days
fit and tell the operator:

```bash
df -h /
sudo -u bonereaper -H bash -c 'du -sh ~/BonReaperBot/reports/paper-* 2>/dev/null | tail -5'
```

Warn the operator if fewer than 14 days of space remain. To free space, list old sessions first and delete them only
after the operator approves:

```bash
sudo -u bonereaper -H find /home/bonereaper/BonReaperBot/reports -maxdepth 1 -type d -name 'paper-*' -mtime +14 -print
# Only after approval:
sudo -u bonereaper -H find /home/bonereaper/BonReaperBot/reports -maxdepth 1 -type d -name 'paper-*' -mtime +14 -exec rm -rf {} +
```

## Step 12. Update to a new version

When the operator has pushed new code:

```bash
sudo -u bonereaper -H bash -s <<'EOF'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd ~/BonReaperBot
git pull --ff-only
if [ "$(uname -m)" = "aarch64" ]; then
  uv sync --frozen --dev --no-install-package pgserver
else
  uv sync --frozen --dev
fi
uv run pytest -q
EOF
sudo systemctl restart bonereaper-paper bonereaper-dashboard
```

If `git pull` stops because of local changes, ask the operator. Never discard them.

## Step 13. Change code or push to GitHub (only when the operator asks)

- Work on a branch, keep `ruff`, `mypy` and `pytest` passing, and commit with a clear message.
- Never commit `data/`, `reports/paper*`, `reports/shadow*`, `.env`, keys or tokens. `.gitignore` excludes them;
  check `git status` before every commit.
- Pushing needs write access. **Ask** the operator how to provide it, for example a deploy key with write access.
  Never put a token in a remote URL, a file or a log.

## Step 14. Report to the operator

Finish with:

1. Server: Ubuntu version, CPU architecture, vCPU count, RAM, free disk, and how many days of recording fit.
2. Clock: synchronized or not, the offset, and doctor's `clock_offset_ms`.
3. Geoblock status from doctor, exactly as reported.
4. Tests: passed, skipped and failed counts, plus the ruff, mypy and secret scan results.
5. Smoke checks: the market found, stream event counts, and the paper smoke run's `clean` value.
6. Services: the state of `bonereaper-paper` and `bonereaper-dashboard`, and the timer's next run.
7. The SSH tunnel command for the dashboard, filled in.
8. Anything that failed or needs a decision, with the exact error text.
9. A reminder that all results are simulated and no real order can be placed.
