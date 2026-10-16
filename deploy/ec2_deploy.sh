#!/usr/bin/env bash
# Deploy Orbit on one Amazon Linux 2023 EC2 instance.
#
# Prepared, not executed by agents: a human runs this ON the instance, as ec2-user, never from a dev
# machine. See deploy/RUNBOOK.md. Idempotent: re-running it is how you update.
#
#   bash ~/orbit/deploy/ec2_deploy.sh                 # install/update and (re)start
#   bash ~/orbit/deploy/ec2_deploy.sh --dry-run       # print every command, execute nothing
#   ORBIT_REF=<tag-or-sha> bash ~/orbit/deploy/ec2_deploy.sh   # roll back to an older commit
#
# Steps: install git, Docker, the compose and buildx plugins (if missing) -> enable docker, add the
# user to the docker group -> clone or fast-forward the repo -> check the existing .env (never created
# here) -> mkdir + chown 1000 data/ logs/ memory/ -> pg_dump backup if Postgres already runs ->
# docker compose up -d --build with the prod override -> wait for /health -> status summary.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ec2_deploy.sh [--dry-run] [--repo URL] [--branch NAME] [--ref REF] [--dir PATH]
                     [--no-backup] [-h|--help]

  --dry-run      Print every command instead of running it (no dnf, git, docker or curl calls).
  --repo URL     Git URL to clone           (env ORBIT_REPO,   default https://github.com/xy9iao/orbit.git)
  --branch NAME  Branch to deploy            (env ORBIT_BRANCH, default main)
  --ref REF      Deploy this tag/commit instead of the branch head, detached (env ORBIT_REF); for rollback
  --dir PATH     Checkout directory          (env ORBIT_DIR,    default $HOME/orbit)
  --no-backup    Skip the pre-deploy pg_dump (env ORBIT_SKIP_BACKUP=1)

Other env: HEALTH_TIMEOUT (seconds, default 120), COMPOSE_VERSION (default v2.29.7),
BUILDX_VERSION (default v0.17.1). Requires an existing <dir>/.env; see deploy/RUNBOOK.md.
EOF
}

DRY_RUN=0
ORBIT_REPO=${ORBIT_REPO:-https://github.com/xy9iao/orbit.git}
ORBIT_BRANCH=${ORBIT_BRANCH:-main}
ORBIT_REF=${ORBIT_REF:-}
ORBIT_DIR=${ORBIT_DIR:-$HOME/orbit}
ORBIT_SKIP_BACKUP=${ORBIT_SKIP_BACKUP:-0}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-120}
COMPOSE_VERSION=${COMPOSE_VERSION:-v2.29.7}
BUILDX_VERSION=${BUILDX_VERSION:-v0.17.1}
MIN_COMPOSE=2.24.4 # deploy/docker-compose.prod.yml uses the `!reset` YAML tag
MIN_BUILDX=0.17.0  # recent compose refuses `up --build` with an older buildx
PLUGIN_DIR=/usr/local/lib/docker/cli-plugins # searched before the distro's /usr/libexec/docker/cli-plugins
TARGET_USER=${SUDO_USER:-$(id -un)}
DOCKER=(docker)
GROUP_ADDED=0
HEALTH_BODY=

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

# Every command that changes something goes through run, so --dry-run shows it instead.
run() {
  if ((DRY_RUN)); then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

as_root() {
  if ((EUID == 0)); then run "$@"; else run sudo "$@"; fi
}

compose() {
  run "${DOCKER[@]}" compose -f "$ORBIT_DIR/docker-compose.yml" -f "$ORBIT_DIR/deploy/docker-compose.prod.yml" "$@"
}

# True if version $1 >= $2 (both without a leading "v").
version_ge() {
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --repo) ORBIT_REPO=${2:?--repo needs a URL}; shift ;;
      --branch) ORBIT_BRANCH=${2:?--branch needs a name}; shift ;;
      --ref) ORBIT_REF=${2:?--ref needs a tag or commit}; shift ;;
      --dir) ORBIT_DIR=${2:?--dir needs a path}; shift ;;
      --no-backup) ORBIT_SKIP_BACKUP=1 ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
  [[ $HEALTH_TIMEOUT =~ ^[0-9]+$ ]] || die "HEALTH_TIMEOUT must be a number of seconds"
}

require_al2023() {
  local id="" version=""
  if [[ -r /etc/os-release ]]; then
    id=$(. /etc/os-release && printf '%s' "${ID:-}")
    version=$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")
  fi
  [[ $id == amzn && $version == 2023 ]] ||
    die "this host is not Amazon Linux 2023 (ID=$id VERSION_ID=$version); install Docker + compose yourself, then re-run"
}

# --- 1. packages -------------------------------------------------------------------------------

ensure_packages() {
  if ((DRY_RUN)); then
    log "dry-run: probes are skipped, so every conditional install step below is shown"
  fi
  if ((DRY_RUN)) || ! command -v git >/dev/null 2>&1; then
    ((DRY_RUN)) || require_al2023
    log "installing git"
    as_root dnf install -y git
  fi
  if ((DRY_RUN)) || ! command -v docker >/dev/null 2>&1; then
    ((DRY_RUN)) || require_al2023
    log "installing docker"
    as_root dnf install -y docker
  fi
  log "enabling the docker service (starts now and on boot)"
  as_root systemctl enable --now docker
  if ((EUID != 0)); then
    if ((DRY_RUN)) || ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx docker; then
      log "adding $TARGET_USER to the docker group"
      as_root usermod -aG docker "$TARGET_USER"
      GROUP_ADDED=1
    fi
  fi
}

install_plugin() { # name url checksums_url asset
  local name=$1 url=$2 sums_url=$3 asset=$4 tmp expected
  if ((DRY_RUN)); then
    run curl -fsSL --retry 3 -o "/tmp/$asset" "$url"
    run curl -fsSL --retry 3 "$sums_url"
    printf '[dry-run] (verify sha256 of /tmp/%s against %s)\n' "$asset" "$sums_url"
    as_root install -D -m 0755 "/tmp/$asset" "$PLUGIN_DIR/docker-$name"
    return 0
  fi
  tmp=$(mktemp -d)
  curl -fsSL --retry 3 -o "$tmp/$asset" "$url" || die "download failed: $url"
  expected=$(curl -fsSL --retry 3 "$sums_url" | awk -v a="$asset" '$2 == a || $2 == "*" a { print $1; exit }') ||
    die "checksum download failed: $sums_url"
  [[ $expected =~ ^[0-9a-f]{64}$ ]] || die "no sha256 for $asset in $sums_url"
  (cd "$tmp" && printf '%s  %s\n' "$expected" "$asset" | sha256sum -c --quiet -) ||
    die "sha256 mismatch for $asset; not installing it"
  as_root install -D -m 0755 "$tmp/$asset" "$PLUGIN_DIR/docker-$name"
  rm -rf "$tmp"
}

ensure_plugins() {
  local arch compose_arch buildx_arch current
  arch=$(uname -m)
  case "$arch" in
    x86_64 | amd64) compose_arch=x86_64 buildx_arch=amd64 ;;
    aarch64 | arm64) compose_arch=aarch64 buildx_arch=arm64 ;;
    *) die "unsupported CPU architecture: $arch" ;;
  esac

  current=""
  ((DRY_RUN)) || current=$(docker compose version --short 2>/dev/null || true)
  current=${current#v}
  if [[ -z $current ]] || ! version_ge "$current" "$MIN_COMPOSE"; then
    log "installing docker compose plugin $COMPOSE_VERSION (found: ${current:-none}, need >= $MIN_COMPOSE)"
    local base=https://github.com/docker/compose/releases/download/$COMPOSE_VERSION
    install_plugin compose "$base/docker-compose-linux-$compose_arch" \
      "$base/docker-compose-linux-$compose_arch.sha256" "docker-compose-linux-$compose_arch"
  fi

  current=""
  ((DRY_RUN)) || current=$(docker buildx version 2>/dev/null | awk '{ print $2 }' || true)
  current=${current#v}
  if [[ -z $current ]] || ! version_ge "$current" "$MIN_BUILDX"; then
    log "installing docker buildx plugin $BUILDX_VERSION (found: ${current:-none}, need >= $MIN_BUILDX)"
    local base=https://github.com/docker/buildx/releases/download/$BUILDX_VERSION
    install_plugin buildx "$base/buildx-$BUILDX_VERSION.linux-$buildx_arch" \
      "$base/checksums.txt" "buildx-$BUILDX_VERSION.linux-$buildx_arch"
  fi
}

# A freshly added docker group only applies to new logins, so fall back to sudo for this run.
detect_docker_access() {
  DOCKER=(docker)
  ((DRY_RUN)) && return 0
  docker info >/dev/null 2>&1 && return 0
  if ((EUID != 0)) && sudo docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
    warn "using 'sudo docker' for this run; log out and back in to use docker without sudo"
    return 0
  fi
  die "cannot reach the Docker daemon (sudo systemctl status docker)"
}

# --- 2. repository -----------------------------------------------------------------------------

sync_repo() {
  if ((!DRY_RUN)) && [[ -e $ORBIT_DIR && ! -d $ORBIT_DIR/.git ]]; then
    die "$ORBIT_DIR exists but is not a git clone. Move it away (keep its .env!) and re-run."
  fi
  if [[ ! -d $ORBIT_DIR/.git ]]; then
    log "cloning $ORBIT_REPO ($ORBIT_BRANCH) into $ORBIT_DIR"
    run git clone --branch "$ORBIT_BRANCH" "$ORBIT_REPO" "$ORBIT_DIR"
    [[ -n $ORBIT_REF ]] && run git -C "$ORBIT_DIR" checkout --detach "$ORBIT_REF"
    return 0
  fi
  if ((!DRY_RUN)) && [[ -n "$(git -C "$ORBIT_DIR" status --porcelain --untracked-files=no)" ]]; then
    git -C "$ORBIT_DIR" status --short --untracked-files=no >&2
    die "tracked files in $ORBIT_DIR were edited on the instance; commit them upstream or 'git checkout -- .' first"
  fi
  log "updating $ORBIT_DIR"
  run git -C "$ORBIT_DIR" fetch --prune --tags origin
  if [[ -n $ORBIT_REF ]]; then
    run git -C "$ORBIT_DIR" checkout --detach "$ORBIT_REF"
  else
    run git -C "$ORBIT_DIR" checkout "$ORBIT_BRANCH"
    run git -C "$ORBIT_DIR" pull --ff-only origin "$ORBIT_BRANCH"
  fi
}

# --- 3. .env checks ----------------------------------------------------------------------------

ENV_FILE=""

# Last assignment of $1 in .env, without quotes or a trailing " # comment". Empty if absent.
env_get() {
  { grep -E "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" "$ENV_FILE" || true; } |
    tail -n1 |
    sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+#.*$//; s/[[:space:]]+$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/'
}

check_env() {
  ENV_FILE=$ORBIT_DIR/.env
  if [[ ! -f $ENV_FILE ]]; then
    if ((DRY_RUN)); then
      warn "no $ENV_FILE (a real run stops here; see deploy/RUNBOOK.md 'First deploy')"
      return 0
    fi
    die "$ENV_FILE not found. This script never creates secrets: write it on your machine and copy it
       with scp as described in deploy/RUNBOOK.md ('First deploy', step 5), then re-run."
  fi
  if grep -q $'\r' "$ENV_FILE"; then
    die "$ENV_FILE has Windows (CRLF) line endings, which end up inside the secrets. Fix: sed -i 's/\r\$//' $ENV_FILE"
  fi

  local proxy pw key loopback='://([^@/]*@)?(127\.|localhost|\[::1\]|0\.0\.0\.0)'
  proxy=$(env_get PROXY_URL)
  if [[ -n $proxy ]]; then
    shopt -s nocasematch
    if [[ $proxy =~ $loopback ]]; then
      shopt -u nocasematch
      die "PROXY_URL=$proxy points at the container itself inside Docker, so OpenAI and Telegram calls fail.
       On EC2 remove PROXY_URL from .env (no proxy is needed); see QUESTIONS.md (M1.8)."
    fi
    shopt -u nocasematch
    warn "PROXY_URL is set ($proxy); EC2 normally needs no proxy"
  fi

  pw=$(env_get POSTGRES_PASSWORD)
  [[ -n $pw ]] || die "set POSTGRES_PASSWORD in $ENV_FILE (generate one: openssl rand -hex 24)"
  [[ $pw != orbit ]] || die "POSTGRES_PASSWORD is the dev default 'orbit'; generate one: openssl rand -hex 24"
  [[ $pw =~ ^[A-Za-z0-9._~-]+$ ]] ||
    die "POSTGRES_PASSWORD is embedded in DATABASE_URL; use URL-safe characters only (openssl rand -hex 24)"
  ((${#pw} >= 16)) || warn "POSTGRES_PASSWORD is shorter than 16 characters"

  for key in OPENAI_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_OWNER_CHAT_ID; do
    [[ -n "$(env_get "$key")" ]] || warn "$key is not set in .env; Orbit starts without that feature"
  done
  [[ -n "$(env_get COMPOSE_FILE)" ]] ||
    warn "add COMPOSE_FILE=docker-compose.yml:deploy/docker-compose.prod.yml to .env so plain 'docker compose ...' uses prod settings"

  run chmod 600 "$ENV_FILE"
}

# --- 4. host directories -----------------------------------------------------------------------

# The image runs as uid 1000. Folders Docker creates for bind mounts are owned by root, and Orbit
# could not write its logs (docs/DECISIONS.md, "Container details (M1.8)").
prepare_dirs() {
  log "preparing data/ logs/ memory/ backups/"
  run mkdir -p "$ORBIT_DIR/data" "$ORBIT_DIR/logs" "$ORBIT_DIR/memory" "$ORBIT_DIR/backups"
  as_root chown 1000:1000 "$ORBIT_DIR/data" "$ORBIT_DIR/logs" "$ORBIT_DIR/memory"
  run chmod 700 "$ORBIT_DIR/backups"
}

# --- 5. pre-deploy backup and start ------------------------------------------------------------

pre_deploy_backup() {
  if [[ $ORBIT_SKIP_BACKUP == 1 ]]; then
    warn "skipping the pre-deploy backup (--no-backup)"
    return 0
  fi
  if ((DRY_RUN)); then
    log "dry-run: a real run takes a backup first if the postgres service is running"
    run bash "$ORBIT_DIR/deploy/backup.sh"
    return 0
  fi
  local running
  running=$(compose ps --status running --services 2>/dev/null || true)
  if grep -qx postgres <<<"$running"; then
    log "taking a pre-deploy backup"
    bash "$ORBIT_DIR/deploy/backup.sh" || die "pre-deploy backup failed; fix it or re-run with --no-backup"
  else
    log "postgres is not running yet (first deploy?); no pre-deploy backup"
  fi
}

show_failure() {
  ((DRY_RUN)) && return 0
  warn "Orbit is not healthy. Container state and recent logs:"
  compose ps || true
  compose logs --no-color --tail=80 postgres orbit || true
  warn "more: tail -n 200 $ORBIT_DIR/logs/orbit.log; deploy/RUNBOOK.md 'Rollback'"
}

wait_for_health() {
  local port url deadline ok_re='"status"[[:space:]]*:[[:space:]]*"ok"'
  port=""
  [[ -f $ENV_FILE ]] && port=$(env_get ORBIT_PORT)
  url="http://127.0.0.1:${port:-8765}/health"
  if ((DRY_RUN)); then
    run curl -fsS --max-time 5 "$url"
    printf '[dry-run] (repeat every 3 s until it reports "status":"ok", up to %s s)\n' "$HEALTH_TIMEOUT"
    return 0
  fi
  log "waiting up to ${HEALTH_TIMEOUT}s for $url"
  deadline=$((SECONDS + HEALTH_TIMEOUT))
  while ((SECONDS < deadline)); do
    if HEALTH_BODY=$(curl -fsS --max-time 5 "$url" 2>/dev/null) &&
      [[ $HEALTH_BODY =~ $ok_re ]]; then
      return 0
    fi
    sleep 3
  done
  return 1
}

summary() {
  ((DRY_RUN)) && { log "dry-run complete: nothing was executed"; return 0; }
  log "status"
  printf '  commit : %s\n' "$(git -C "$ORBIT_DIR" log -1 --format='%h %s (%cr)')"
  printf '  ref    : %s\n' "$(git -C "$ORBIT_DIR" rev-parse --abbrev-ref HEAD)"
  printf '  health : %s\n' "$HEALTH_BODY"
  printf '  disk   : %s\n' "$(df -h "$ORBIT_DIR" | awk 'NR == 2 { print $4 " free of " $2 }')"
  local last
  last=$(find "$ORBIT_DIR/backups" -maxdepth 1 -name 'orbit-*.dump' 2>/dev/null | sort | tail -n1)
  printf '  backup : %s\n' "${last:-none yet}"
  compose ps
  if ! { command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q 'deploy/backup.sh'; }; then
    warn "no nightly backup cron entry found; see deploy/RUNBOOK.md 'Backups'"
  fi
  ((GROUP_ADDED)) && warn "$TARGET_USER was added to the docker group: log out and back in before using docker"
  log "Orbit is up. Logs: docker compose logs -f orbit   (in $ORBIT_DIR)"
}

main() {
  parse_args "$@"
  log "Orbit deploy: repo=$ORBIT_REPO branch=$ORBIT_BRANCH ref=${ORBIT_REF:-<branch head>} dir=$ORBIT_DIR dry_run=$DRY_RUN"
  ensure_packages
  ensure_plugins
  detect_docker_access
  sync_repo
  check_env
  prepare_dirs
  pre_deploy_backup
  log "building and starting the stack"
  if ! compose up -d --build --remove-orphans; then
    show_failure
    die "docker compose up failed"
  fi
  if ! wait_for_health; then
    show_failure
    die "Orbit did not report healthy within ${HEALTH_TIMEOUT}s"
  fi
  log "removing dangling images from earlier builds"
  run "${DOCKER[@]}" image prune -f
  summary
}

# One line on purpose: bash has parsed all of main before git can replace this file mid-run.
main "$@"; exit $?
