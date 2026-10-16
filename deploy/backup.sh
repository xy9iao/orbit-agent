#!/usr/bin/env bash
# Nightly Postgres backup for the EC2 deploy: pg_dump (custom format) through `docker compose exec`,
# written to backups/orbit-YYYYmmdd-HHMM.dump, keeping the newest N dumps. Exits non-zero on failure.
#
# Prepared, not executed by agents: a human installs it in cron (deploy/RUNBOOK.md, "Backups").
#
#   bash ~/orbit/deploy/backup.sh               # one backup now
#   bash ~/orbit/deploy/backup.sh --keep 30     # keep 30 instead of 14
#   bash ~/orbit/deploy/backup.sh --dry-run     # print the commands, execute nothing
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: backup.sh [--dry-run] [--keep N] [--dir PATH] [-h|--help]

  --dry-run   Print the commands instead of running them (no docker calls, no files written).
  --keep N    How many dumps to keep (env BACKUP_KEEP, default 14).
  --dir PATH  Orbit checkout (env ORBIT_DIR, default: the repo this script lives in).
Dumps go to <dir>/backups (env BACKUP_DIR). Restore steps: deploy/RUNBOOK.md, "Restore".
EOF
}

DRY_RUN=0
KEEP=${BACKUP_KEEP:-14}
ORBIT_DIR=${ORBIT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}
BACKUP_DIR=${BACKUP_DIR:-}
DOCKER=(docker)

log() { printf '%s backup: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() {
  printf '%s backup: ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
  exit 1
}

run() {
  if ((DRY_RUN)); then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

compose() {
  run "${DOCKER[@]}" compose -f "$ORBIT_DIR/docker-compose.yml" -f "$ORBIT_DIR/deploy/docker-compose.prod.yml" "$@"
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --keep) KEEP=${2:?--keep needs a number}; shift ;;
      --dir) ORBIT_DIR=${2:?--dir needs a path}; shift ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
  [[ $KEEP =~ ^[1-9][0-9]*$ ]] || die "--keep must be a positive integer, got '$KEEP'"
  BACKUP_DIR=${BACKUP_DIR:-$ORBIT_DIR/backups}
}

# Cron can't answer a sudo prompt, so only passwordless sudo is tried (ec2-user has it).
detect_docker_access() {
  ((DRY_RUN)) && return 0
  docker info >/dev/null 2>&1 && return 0
  if ((EUID != 0)) && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
    return 0
  fi
  die "cannot reach the Docker daemon"
}

main() {
  parse_args "$@"
  detect_docker_access

  local final tmp
  final=$BACKUP_DIR/orbit-$(date +%Y%m%d-%H%M).dump
  tmp=$final.partial
  umask 077 # dumps hold every message, reminder and posting
  run mkdir -p "$BACKUP_DIR"

  log "dumping database orbit to $final"
  if ((DRY_RUN)); then
    printf '[dry-run] (stdout of the next command goes to %s)\n' "$tmp"
  else
    # shellcheck disable=SC2064 # expand now: $tmp is local to main and gone when the EXIT trap runs
    trap "rm -f -- $(printf '%q' "$tmp")" EXIT
  fi
  if ((DRY_RUN)); then
    compose exec -T postgres pg_dump -U orbit -d orbit -Fc
  else
    compose exec -T postgres pg_dump -U orbit -d orbit -Fc >"$tmp" || die "pg_dump failed (is the postgres service running?)"
    [[ -s $tmp ]] || die "pg_dump wrote an empty file"
  fi

  # Proves the archive is readable before older dumps are pruned.
  if ((DRY_RUN)); then
    printf '[dry-run] (stdin of the next command is %s)\n' "$tmp"
    compose exec -T postgres pg_restore --list
  else
    compose exec -T postgres pg_restore --list <"$tmp" >/dev/null || die "the new dump is not a readable archive"
  fi
  run mv -f -- "$tmp" "$final"
  ((DRY_RUN)) || log "wrote $final ($(du -h "$final" | cut -f1))"

  # Names sort chronologically, so everything before the newest KEEP is old.
  if ((DRY_RUN)); then
    printf '[dry-run] (delete all but the newest %s of %s/orbit-*.dump)\n' "$KEEP" "$BACKUP_DIR"
    log "dry-run complete: nothing was executed"
    return 0
  fi
  local dumps=() old
  shopt -s nullglob
  dumps=("$BACKUP_DIR"/orbit-*.dump)
  shopt -u nullglob
  if ((${#dumps[@]} > KEEP)); then
    for old in "${dumps[@]:0:${#dumps[@]}-KEEP}"; do
      rm -f -- "$old"
      log "pruned $old"
    done
  fi
  log "done; $((${#dumps[@]} > KEEP ? KEEP : ${#dumps[@]})) dump(s) kept"
}

main "$@"; exit $?
