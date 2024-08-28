#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# nightly-compliance.sh — scheduled baseline audit.
#
# Runs against the backups taken earlier in the night rather than against the
# devices. Two reasons: it does not add another round of connections to every
# device, and it audits exactly the configuration that was archived, so the
# report and the archive cannot disagree.
# ---------------------------------------------------------------------------

set -o errexit
set -o nounset
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT
VENV="${REPO_ROOT}/.venv"
INVENTORY="${REPO_ROOT}/automation/ansible/inventory/devices.yml"
BASELINE="${REPO_ROOT}/cisco/baselines/security-baseline.yml"
BACKUP_DIR="${REPO_ROOT}/backups"
REPORT="${REPO_ROOT}/reports/compliance-$(date +%Y%m%d).html"
LATEST="${REPO_ROOT}/reports/compliance.html"
LOG_FILE="/var/log/netops/compliance.log"
METRICS_DIR="${METRICS_DIR:-/var/lib/node_exporter/textfile_collector}"

log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "${*:2}" | tee -a "$LOG_FILE" >&2; }

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$REPORT")" "$METRICS_DIR"

log INFO "Starting nightly compliance audit"

RESULT=0
OUTPUT=""
if OUTPUT="$("${VENV}/bin/python" -m netops.compliance \
      --inventory "$INVENTORY" \
      --baseline "$BASELINE" \
      --from-backups "$BACKUP_DIR" \
      --report "$REPORT" \
      --fail-on high 2>&1)"; then
  log INFO "All checks at or above the 'high' threshold passed"
else
  RESULT=$?
  log WARN "Compliance failures present (exit ${RESULT})"
fi

printf '%s\n' "$OUTPUT" >> "$LOG_FILE"
ln -sf "$REPORT" "$LATEST"

# Parse the counts out of the summary line the tool prints.
PASSED="$(grep -oE '[0-9]+ passed' <<< "$OUTPUT" | grep -oE '[0-9]+' | head -1 || echo 0)"
FAILED="$(grep -oE '[0-9]+ failed' <<< "$OUTPUT" | grep -oE '[0-9]+' | head -1 || echo 0)"

tmp="${METRICS_DIR}/netops_compliance.prom.$$"
cat > "$tmp" <<METRICS
# HELP netops_compliance_checks_passed Baseline checks that passed
# TYPE netops_compliance_checks_passed gauge
netops_compliance_checks_passed ${PASSED:-0}
# HELP netops_compliance_checks_failed Baseline checks that failed
# TYPE netops_compliance_checks_failed gauge
netops_compliance_checks_failed ${FAILED:-0}
# HELP netops_compliance_timestamp_seconds When the audit last ran
# TYPE netops_compliance_timestamp_seconds gauge
netops_compliance_timestamp_seconds $(date +%s)
METRICS
mv -f "$tmp" "${METRICS_DIR}/netops_compliance.prom"

# Keep a quarter of reports: enough to show whether the posture is improving.
find "$(dirname "$REPORT")" -name 'compliance-*.html' -mtime +90 -delete

log INFO "Audit complete: ${PASSED:-0} passed, ${FAILED:-0} failed. Report: ${REPORT}"
exit "$RESULT"
