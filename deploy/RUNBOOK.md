# Orbit on AWS EC2: runbook

> **Prepared, not executed — run by a human.** No agent has run any of this, created AWS resources or
> spent money. Every command below is for you to run yourself.

One EC2 instance runs the same Docker Compose stack as local dev (`orbit` + `postgres` with pgvector),
plus `deploy/docker-compose.prod.yml` (restart always, log rotation, required Postgres password, no
published Postgres port, small-instance tuning). Telegram uses long polling and OpenAI is called
outbound, so **the instance needs no inbound port except SSH**.

Files: `deploy/ec2_deploy.sh` (install + update, idempotent), `deploy/backup.sh` (pg_dump),
`deploy/docker-compose.prod.yml`, this runbook.

## 1. Instance

| Choice | Recommendation | Notes |
|---|---|---|
| Region | `ca-central-1` (Montreal) or `ca-west-1` (Calgary) | Any region works; nothing is latency-sensitive. |
| Type | **t4g.small** (2 vCPU, 2 GiB, ARM/Graviton) | About 20% cheaper than x86. `python:3.12-slim` and `pgvector/pgvector:pg16` are multi-arch (arm64 included), and every dependency ships aarch64 wheels. If a future dependency has no arm64 wheel, the build fails (the slim image has no compiler); then use **t3.small** (x86_64) with the x86 AMI. |
| AMI | Amazon Linux 2023, matching the architecture (arm64 for t4g, x86_64 for t3) | `ec2_deploy.sh` installs Docker with `dnf` and only supports AL2023. |
| Storage | **gp3, 20 GB** (default 3000 IOPS / 125 MB/s) | Image + build cache ~3 GB, Postgres is small, backups are MBs. |
| Key pair | ed25519, private key kept on your laptop only | |
| IAM role | **None.** Orbit calls no AWS API. | Leave "IAM instance profile" empty. Keep IMDSv2 required (the AL2023 default). |
| Public IP | Auto-assigned public IPv4 (needed for outbound traffic) | No Elastic IP needed; the IP changes after stop/start, so just ssh to the new one. |

t4g.micro (1 GiB) can work with a swap file, but `docker compose up --build` is slow and tight.

**Security group** (`orbit-sg`):

| Direction | Port | Source | Why |
|---|---|---|---|
| Inbound | 22/tcp | **My IP** (`x.x.x.x/32`) only | SSH. Update it when your IP changes. |
| Inbound | nothing else | | **No 8765, no 5432**, not even temporarily. Use an SSH tunnel. |
| Outbound | all | 0.0.0.0/0 | Telegram, OpenAI, job-board APIs, GitHub, Docker Hub, dnf. |

## 2. First deploy

1. **Launch** the instance with the settings above (console: EC2 → Launch instance).
2. **Stop the local Orbit first.** One Telegram bot token allows only one long-poller (a second one
   gets `409 Conflict`), and two Orbits with separate databases would each send every radar match.
   On Windows: `scripts\uninstall_task.ps1` (or stop the task) and/or `docker compose down`.
3. **SSH in:** `ssh -i ~/.ssh/orbit-ec2 ec2-user@<public-ip>`
4. **Optional but recommended on 2 GiB: a 2 GB swap file** (keeps the image build from OOM-killing):
   ```bash
   sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 && sudo chmod 600 /swapfile
   sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab
   ```
5. **Clone, then copy `.env`:**
   ```bash
   sudo dnf install -y git
   git clone --branch main https://github.com/xy9iao/orbit.git ~/orbit
   ```
   If the repo is private, use a read-only deploy key (`git@github.com:xy9iao/orbit.git`) or a
   fine-grained token with read access to this repo only.

   On your laptop, write a **fresh** EC2 `.env` (don't copy the dev one: it has a Windows
   `ORBIT_HOME`, a `127.0.0.1` proxy and CRLF line endings):
   ```dotenv
   OPENAI_API_KEY=sk-...
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_OWNER_CHAT_ID=123456789
   # openssl rand -hex 24  (URL-safe; it goes inside DATABASE_URL)
   POSTGRES_PASSWORD=<48 hex chars>
   TZ=America/Toronto
   # makes plain `docker compose ...` in ~/orbit use the prod override too
   COMPOSE_FILE=docker-compose.yml:deploy/docker-compose.prod.yml
   # No PROXY_URL on EC2. Inside Docker 127.0.0.1 is the container itself (QUESTIONS.md, M1.8);
   # ec2_deploy.sh refuses a loopback PROXY_URL.
   ```
   Then copy it over and lock it down:
   ```bash
   scp -i ~/.ssh/orbit-ec2 .env.ec2 ec2-user@<public-ip>:~/orbit/.env
   ssh -i ~/.ssh/orbit-ec2 ec2-user@<public-ip> "sed -i 's/\r$//' ~/orbit/.env && chmod 600 ~/orbit/.env"
   ```
6. **Preview, then deploy:**
   ```bash
   bash ~/orbit/deploy/ec2_deploy.sh --dry-run   # prints every command, runs nothing
   bash ~/orbit/deploy/ec2_deploy.sh
   ```
   The script installs git, Docker, the compose and buildx plugins (pinned, sha256-checked), enables
   Docker at boot, adds `ec2-user` to the `docker` group, checks `.env`, creates `data/ logs/ memory/`
   owned by uid 1000 (the container user), builds and starts the stack, waits up to 120 s for
   `/health` to report `"status":"ok"`, then prints a summary. The first build takes a few minutes.
   Migrations run on container start (`alembic upgrade head`).
7. **Log out and back in** once, so `docker` works without `sudo`.
8. **Copy personal files and set up the radar:**
   ```bash
   # laptop:
   scp -i ~/.ssh/orbit-ec2 data/resume.md ec2-user@<ip>:~/orbit/data/resume.md
   scp -i ~/.ssh/orbit-ec2 -r memory/ ec2-user@<ip>:~/orbit/        # MEMORY.md, if you have one
   # instance:
   sudo chown -R 1000:1000 ~/orbit/data ~/orbit/memory
   cd ~/orbit && docker compose exec orbit orbit radar resume
   docker compose exec orbit orbit radar verify-sources
   ```
   Skip `radar resume` if you are restoring a local database (section 3); the profile is in it.
9. **Install the nightly backup** (section 6) and check `/health` through a tunnel (section 9).

## 3. Migrating existing local data

The prod Postgres port isn't published, so the data moves as a `pg_dump` file.

**Old SQLite data** (`data/orbit.db` from before M1): first convert it into your **local** Docker
Postgres, then continue below:
```bash
docker compose up -d postgres
python scripts/migrate_sqlite_to_pg.py        # data/orbit.db -> DATABASE_URL (127.0.0.1:5432)
```

**Local Postgres → EC2.** On Windows, don't redirect `pg_dump` with PowerShell's `>` (it re-encodes
binary output and corrupts the dump). Write it inside the container and copy it out:
```bash
# laptop, in the repo:
docker compose exec postgres pg_dump -U orbit -d orbit -Fc -f /tmp/orbit-local.dump
docker compose cp postgres:/tmp/orbit-local.dump ./orbit-local.dump
scp -i ~/.ssh/orbit-ec2 orbit-local.dump ec2-user@<ip>:~/orbit/backups/
```
Then on the instance, follow **Restore** (section 6) with `backups/orbit-local.dump`. On the next
start, `alembic upgrade head` brings an older schema up to date.

## 4. Updating

```bash
cd ~/orbit && bash deploy/ec2_deploy.sh            # pulls main (fast-forward only), rebuilds, waits for /health
ORBIT_BRANCH=radar-mvp bash deploy/ec2_deploy.sh   # another branch
```
If Postgres is already running, the script takes a pre-deploy `pg_dump` first (`--no-backup` skips it).
It refuses to run if tracked files were edited on the instance; change them upstream instead.
After a successful start it removes dangling images so repeated builds don't fill the disk.
OS patches: `sudo dnf upgrade --releasever=latest -y && sudo reboot`. Docker starts at boot and both
containers have `restart: always`.

## 5. Logs and status

```bash
cd ~/orbit
docker compose ps                               # state + health of both containers
docker compose logs -f orbit                    # container stdout (json-file, 5 x 10 MB per container)
docker compose logs --tail=200 postgres
tail -f logs/orbit.log                          # Orbit's own DEBUG log, rotated daily, kept 14 days
tail -n 50 backups/backup.log                   # nightly backup output
docker compose exec orbit orbit radar stats     # companies, postings, accuracy, latency, uptime
docker compose exec orbit orbit status
```
(Without `COMPOSE_FILE` in `.env`, add `-f docker-compose.yml -f deploy/docker-compose.prod.yml`.)

## 6. Backups and restore

**Install the nightly backup** (AL2023 has no cron by default):
```bash
sudo dnf install -y cronie && sudo systemctl enable --now crond
crontab -e
```
```cron
# 03:15 UTC (instance clock) = 23:15 Toronto (EDT); keeps the newest 14 dumps
15 3 * * * bash /home/ec2-user/orbit/deploy/backup.sh --keep 14 >> /home/ec2-user/orbit/backups/backup.log 2>&1
```
Test it once by hand: `bash ~/orbit/deploy/backup.sh` (also `--dry-run`). Each run writes
`backups/orbit-YYYYmmdd-HHMM.dump` (custom format, mode 600), checks it with `pg_restore --list`, then
prunes older dumps. A failure exits non-zero and keeps every older dump.

Backups on the same disk don't survive losing the instance. Copy one off the box now and then:
`scp -i ~/.ssh/orbit-ec2 'ec2-user@<ip>:~/orbit/backups/orbit-*.dump' .` (S3 or EBS snapshots would
need an IAM role or extra cost; not set up).

**Restore** (replaces the whole `orbit` database):
```bash
cd ~/orbit
bash deploy/backup.sh                                  # safety copy of the current state first
docker compose stop orbit
docker compose exec -T postgres dropdb -U orbit --if-exists orbit
docker compose exec -T postgres createdb -U orbit -O orbit orbit
docker compose exec -T postgres pg_restore -U orbit -d orbit --no-owner --exit-on-error --single-transaction \
  < backups/orbit-YYYYmmdd-HHMM.dump
docker compose start orbit                             # runs alembic upgrade head, then serves
curl -fsS http://127.0.0.1:8765/health
```
The dump contains `CREATE EXTENSION vector`, so pgvector comes back with it.

## 7. Rollback

1. Find the last good commit: `git -C ~/orbit log --oneline -15` (or a tag).
2. **Only if the bad release added an Alembic migration**, downgrade while the new image (which still
   has that migration file) is running: compare `docker compose exec orbit alembic current` with
   `git show <good>:orbit/storage/migrations/versions/`, then run
   `docker compose exec orbit alembic downgrade -1` (once per extra revision). The old code can't
   downgrade a revision it doesn't know. If a downgrade isn't possible, restore the pre-deploy dump
   (section 6) instead.
3. Deploy the old commit (detached; the script takes a backup first):
   `ORBIT_REF=<good-sha-or-tag> bash ~/orbit/deploy/ec2_deploy.sh`
4. Back to normal later: `bash ~/orbit/deploy/ec2_deploy.sh` checks out the branch again and pulls.

## 8. Rotating secrets

After any `.env` change: `docker compose up -d --force-recreate orbit` (an env change needs a new container).

- **Telegram bot token:** @BotFather → `/revoke` → pick the bot → put the new token in `.env` → recreate orbit.
- **OpenAI key:** create a new key in the OpenAI dashboard → `.env` → recreate orbit → check
  `docker compose logs --tail=50 orbit` → delete the old key.
- **Postgres password** (`POSTGRES_PASSWORD` only applies on the first init of the volume, so it has to
  change inside Postgres too):
  ```bash
  cd ~/orbit && bash deploy/backup.sh
  openssl rand -hex 24                                   # new password
  docker compose exec postgres psql -U orbit -d orbit    # then: \password orbit   (stays out of shell history)
  # equivalent: ALTER USER orbit WITH PASSWORD '<new>';
  sed -i 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=<new>/' .env
  docker compose up -d --force-recreate                  # orbit picks up the new DATABASE_URL
  ```
  Connections that are already open stay valid. New ones fail only between the ALTER and the recreate.
- **SSH key:** add the new public key to `~/.ssh/authorized_keys`, log in with it, then remove the old one.

## 9. Monitoring

- **Health through an SSH tunnel** (never open 8765):
  ```bash
  ssh -i ~/.ssh/orbit-ec2 -N -L 8765:127.0.0.1:8765 ec2-user@<ip>
  curl http://127.0.0.1:8765/health     # {"status":"ok","db":"ok",...}; 503 "degraded" if the DB is down
  ```
  With the tunnel open (and the local Orbit stopped), the `orbit` CLI on your laptop talks to the EC2
  instance: `orbit status`, `orbit chat`.
- The Docker healthcheck polls `/health` every 30 s; `docker compose ps` shows `healthy`/`unhealthy`.
- Orbit's startup notice on Telegram doubles as a "restarted" signal.
- Optional: an EC2 status-check alarm with the "recover" action (CloudWatch, about USD 0.10/month).

## 10. Cost (rough, on-demand; check the AWS Pricing Calculator)

| Item | t4g.small | t3.small |
|---|---|---|
| Instance, 730 h (ca-central-1) | ~USD 13.50 | ~USD 17 |
| gp3 20 GB | ~USD 1.80 | ~USD 1.80 |
| Public IPv4 (USD 0.005/h) | ~USD 3.65 | ~USD 3.65 |
| Data transfer out (well under 100 GB free) | ~0 | ~0 |
| **Total / month** | **~USD 19 (~CAD 26)** | **~USD 22 (~CAD 31)** |

OpenAI usage is billed separately (see `orbit radar stats` and the `usage` table). A 1-year savings plan
cuts the instance cost by about 30%. New accounts may have free-tier credits; don't count on them.

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Script stops: `.env not found` | Section 2, step 5. The script never creates secrets. |
| Script stops: loopback `PROXY_URL` | Remove `PROXY_URL` from `.env` on EC2. |
| `permission denied` on `logs/orbit.log` | `sudo chown -R 1000:1000 ~/orbit/data ~/orbit/logs ~/orbit/memory` |
| Telegram `409 Conflict` | Another Orbit (your laptop) is polling the same bot token. Stop it. |
| Build killed / very slow | Add the swap file (section 2, step 4) or use a bigger instance. |
| `/health` 503 `degraded` | Postgres is down, or the password in `.env` doesn't match the DB (section 8). |
| Times look 4–5 h off | `TZ` in `.env` (default America/Toronto), then recreate orbit. |

## 12. Never

- Never open 5432 or 8765 in the security group or publish them on `0.0.0.0`. Use `docker compose exec` and SSH tunnels.
- Never commit `.env`, dumps from `backups/`, or `data/` and `memory/`. Never paste secrets into issues or chat.
- Never run two Orbits against the same Telegram bot token.
- Never `docker compose down -v` in prod: `-v` deletes the `pgdata` volume (the database).
- Never put a `127.0.0.1` `PROXY_URL` in the EC2 `.env`.
- Never attach an IAM role or AWS keys to the instance; Orbit needs none.
- Never edit tracked files on the instance; change them upstream and redeploy.
