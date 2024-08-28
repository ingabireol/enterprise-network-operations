#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# nightly-backup.sh — scheduled configuration backup and drift report.
#
# Run from cron or a systemd timer at 02:00. Wraps the Python tooling so that the
# scheduled job has one entry point with one exit code, and so that credentials
# are sourced in exactly one place.
# ---------------------------------------------------------------------------

set -o errexit
set -o nounset
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT
VENV="${REPO_ROOT}/.venv"
INVENTORY="${REPO_ROOT}/automation/ansible/inventory/devices.yml"
BACKUP_DIR="${REPO_ROOT}/backups"
LOG_FILE="/var/log/netops/backup.log"
METRICS_DIR="${METRICS_DIR:-/var/lib/node_exporter/textfile_collector}"

log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "${*:2}" | tee -a "$LOG_FILE" >&2; }

mkdir -p "$(dirname "$LOG_FILE")" "$METRICS_DIR"

# Credentials come from a root-only file, never from the repository.
# shellcheck source=/dev/null
if [[ -r /etc/netops/credentials.env ]]; then
  . /etc/netops/credentials.env
else
  log ERROR "/etc/netops/credentials.env is not readable — cannot authenticate to devices"
  exit 78
fi

: "${NETOPS_USERNAME:?NETOPS_USERNAME is not set}"
: "${NETOPS_PASSWORD:?NETOPS_PASSWORD is not set}"
export NETOPS_USERNAME NETOPS_PASSWORD NETOPS_ENABLE_SECRET

START="$(date +%s)"
log INFO "Starting nightly configuration backup"

emit_metrics() {
  local success="$1" changed="$2" failed="$3" duration="$4"
  local tmp="${METRICS_DIR}/netops_backup.prom.$$"
  cat > "$tmp" <<METRICS
# HELP netops_backup_success Whether the nightly backup run completed without failures
# TYPE netops_backup_success gauge
netops_backup_success ${success}
# HELP netops_backup_devices_changed Devices whose configuration differed from the previous backup
# TYPE netops_backup_devices_changed gauge
netops_backup_devices_changed ${changed}
# HELP netops_backup_devices_failed Devices that could not be backed up
# TYPE netops_backup_devices_failed gauge
netops_backup_devices_failed ${failed}
# HELP netops_backup_duration_seconds Duration of the backup run
# TYPE netops_backup_duration_seconds gauge
netops_backup_duration_seconds ${duration}
# HELP netops_backup_timestamp_seconds When the backup last ran
# TYPE netops_backup_timestamp_seconds gauge
netops_backup_timestamp_seconds $(date +%s)
METRICS
  mv -f "$tmp" "${METRICS_DIR}/netops_backup.prom"
}

RESULT=0
OUTPUT=""
if OUTPUT="$("${VENV}/bin/python" -m netops.backup \
      --inventory "$INVENTORY" \
      --output "$BACKUP_DIR" \
      --commit 2>&1)"; then
  log INFO "Backup completed"
else
  RESULT=$?
  log ERROR "Backup reported failures (exit ${RESULT})"
fi

printf '%s\n' "$OUTPUT" >> "$LOG_FILE"

CHANGED="$(grep -oE '[0-9]+ changed' <<< "$OUTPUT" | grep -oE '[0-9]+' | head -1 || echo 0)"
FAILED="$(grep -oE '[0-9]+ failed' <<< "$OUTPUT" | grep -oE '[0-9]+' | head -1 || echo 0)"
DURATION=$(( $(date +%s) - START ))

emit_metrics "$([[ $RESULT -eq 0 ]] && echo 1 || echo 0)" "${CHANGED:-0}" "${FAILED:-0}" "$DURATION"

# A configuration that changed overnight without a change record is the thing
# this job exists to surface, so it is mailed rather than left in a log.
if [[ "${CHANGED:-0}" -gt 0 ]]; then
  log WARN "${CHANGED} device configuration(s) changed"
  if command -v mail >/dev/null 2>&1; then
    printf '%s\n\nEach change should match an approved change record.\nReview: make diff\n' "$OUTPUT" \
      | mail -s "[netops] ${CHANGED} device configuration(s) changed overnight" \
             "${NETOPS_ALERT_EMAIL:-network-ops@example.gov}"
  fi
fi

log INFO "Finished in ${DURATION}s (changed=${CHANGED:-0}, failed=${FAILED:-0})"
exit "$RESULT"
