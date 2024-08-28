#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# flow-report.sh — summarise NetFlow data.
#
#   flow-report.sh --top-talkers --since '1 hour ago'
#   flow-report.sh --interface GigabitEthernet0/0/0 --since '30 minutes ago'
#   flow-report.sh --policy-violations --since '24 hours ago'
#
# Interface counters tell you a link is full. This tells you what is filling it,
# which is the difference between a capacity discussion and a capacity decision.
# ---------------------------------------------------------------------------

set -o errexit
set -o nounset
set -o pipefail

FLOW_DIR="${FLOW_DIR:-/var/lib/nfdump}"
SINCE="1 hour ago"
MODE=""
INTERFACE=""
LIMIT=20

# The segments the policy says must not talk to each other. A flow matching one
# of these is either a misconfiguration or something worse; either way it should
# not be discovered by accident months later.
USER_NET="10.30.10.0/24"
APP_NET="10.30.20.0/24"
DB_NET="10.30.30.0/24"
MGMT_NET="10.30.1.0/24"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --top-talkers)       MODE=top; shift ;;
    --policy-violations) MODE=policy; shift ;;
    --interface)         MODE=interface; INTERFACE="$2"; shift 2 ;;
    --since)             SINCE="$2"; shift 2 ;;
    --limit)             LIMIT="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^# ---/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$MODE" ]] || { echo "One of --top-talkers, --interface or --policy-violations is required" >&2; exit 2; }
command -v nfdump >/dev/null 2>&1 || { echo "nfdump is not installed" >&2; exit 127; }

WINDOW_START="$(date -d "$SINCE" '+%Y/%m/%d.%H:%M:%S')"
WINDOW_END="$(date '+%Y/%m/%d.%H:%M:%S')"

case "$MODE" in
  top)
    printf '\nTop talkers — %s to now\n' "$SINCE"
    printf '%s\n' "──────────────────────────────────────────────────────────"
    nfdump -R "${FLOW_DIR}:${WINDOW_START}:${WINDOW_END}" \
           -s srcip/bytes -n "$LIMIT" -o extended

    printf '\nTop conversations\n'
    printf '%s\n' "──────────────────────────────────────────────────────────"
    nfdump -R "${FLOW_DIR}:${WINDOW_START}:${WINDOW_END}" \
           -s record/bytes -n "$LIMIT"

    printf '\nTop applications by port\n'
    printf '%s\n' "──────────────────────────────────────────────────────────"
    nfdump -R "${FLOW_DIR}:${WINDOW_START}:${WINDOW_END}" \
           -s dstport/bytes -n "$LIMIT"
    ;;

  interface)
    printf '\nTraffic on %s — %s to now\n' "$INTERFACE" "$SINCE"
    printf '%s\n' "──────────────────────────────────────────────────────────"
    nfdump -R "${FLOW_DIR}:${WINDOW_START}:${WINDOW_END}" \
           "in if ${INTERFACE}" -s record/bytes -n "$LIMIT"
    ;;

  policy)
    printf '\nSegmentation policy violations — %s to now\n' "$SINCE"
    printf '%s\n' "──────────────────────────────────────────────────────────"
    printf 'Flows below crossed a boundary the design says they should not.\n'
    printf 'Each one is either a firewall rule that is wrong or an access\n'
    printf 'attempt that succeeded when it should not have.\n\n'

    printf '  User segment → database segment (should be none):\n'
    nfdump -R "${FLOW_DIR}:${WINDOW_START}:${WINDOW_END}" \
           "src net ${USER_NET} and dst net ${DB_NET}" -o line -n 20 || echo "    none"

    printf '\n  User segment → management segment (should be none):\n'
    nfdump -R "${FLOW_DIR}:${WINDOW_START}:${WINDOW_END}" \
           "src net ${USER_NET} and dst net ${MGMT_NET}" -o line -n 20 || echo "    none"

    printf '\n  Database segment → user segment (should be none):\n'
    nfdump -R "${FLOW_DIR}:${WINDOW_START}:${WINDOW_END}" \
           "src net ${DB_NET} and dst net ${USER_NET}" -o line -n 20 || echo "    none"

    printf '\n  Application → database on a port other than 5432 (should be none):\n'
    nfdump -R "${FLOW_DIR}:${WINDOW_START}:${WINDOW_END}" \
           "src net ${APP_NET} and dst net ${DB_NET} and not dst port 5432" -o line -n 20 || echo "    none"
    ;;
esac

printf '\n'
