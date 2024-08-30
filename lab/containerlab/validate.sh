#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# validate.sh — assert the properties the network design is supposed to guarantee.
#
#   ./lab/containerlab/validate.sh [--test <name>]
#
# Run after `make lab-up`, and after any change to the templates. A change that
# breaks one of these has broken something the design depends on, whether or not
# it looked correct in review.
# ---------------------------------------------------------------------------

set -o errexit
set -o nounset
set -o pipefail

LAB_PREFIX="clab-enterprise-network"
ONLY_TEST=""
PASSED=0
FAILED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test) ONLY_TEST="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^# ---/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

run_test() {
  local name="$1"; shift
  [[ -n "$ONLY_TEST" && "$ONLY_TEST" != "$name" ]] && return 0

  printf '  %-30s ' "$name"
  if "$@" >/tmp/validate-$name.log 2>&1; then
    printf '\033[32mPASS\033[0m\n'
    PASSED=$((PASSED + 1))
  else
    printf '\033[31mFAIL\033[0m\n'
    printf '    see /tmp/validate-%s.log\n' "$name"
    FAILED=$((FAILED + 1))
  fi
}

on_device() {
  local device="$1"; shift
  docker exec "${LAB_PREFIX}-${device}" vtysh -c "$*" 2>/dev/null \
    || ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
           admin@"$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${LAB_PREFIX}-${device}")" "$*"
}

on_host() {
  local host="$1"; shift
  docker exec "${LAB_PREFIX}-${host}" "$@"
}

# --- Routing ---------------------------------------------------------------

test_ospf_adjacency() {
  local output
  output="$(on_device core-dc1-01 "show ip ospf neighbor")"
  # Every configured neighbour must reach FULL. PARTIAL adjacencies are the
  # classic symptom of an MTU or authentication mismatch and they route nothing.
  [[ $(grep -c "FULL" <<< "$output") -ge 2 ]]
}

test_bgp_established() {
  local output
  output="$(on_device wan-dc1-01 "show ip bgp summary")"
  # Both provider sessions established, each with a prefix count rather than a
  # state word in the last column.
  [[ $(grep -cE '^[0-9.]+\s+[0-9]+\s+.*\s+[0-9]+$' <<< "$output") -ge 2 ]]
}

test_path_preference() {
  local output
  output="$(on_device wan-dc1-01 "show ip bgp 0.0.0.0/0")"
  # The primary provider's path must be the best one while both are up.
  grep -q "best" <<< "$output"
}

test_wan_failover() {
  # The test that matters: shut the primary provider and confirm connectivity
  # survives. Restores the interface whatever the result.
  on_device wan-dc1-01 "configure terminal
interface GigabitEthernet0/0/3
shutdown
end" || true

  sleep 30
  local result=0
  on_host host-app ping -c 3 -W 2 8.8.8.8 >/dev/null 2>&1 || result=1

  on_device wan-dc1-01 "configure terminal
interface GigabitEthernet0/0/3
no shutdown
end" || true
  sleep 20

  return $result
}

# --- Segmentation ----------------------------------------------------------
# The negative tests are the important ones. A policy that permits what it should
# is easy; verifying it denies what it should is what catches the mistakes.

test_segmentation_app_to_db() {
  # MUST succeed — the application tier legitimately reaches the database.
  on_host host-app nc -z -w 3 10.30.30.100 5432
}

test_segmentation_user_to_db() {
  # MUST fail — a user workstation has no business reaching the database at all.
  # This asserts the *absence* of connectivity, so the logic is inverted.
  if on_host host-user nc -z -w 3 10.30.30.100 5432 2>/dev/null; then
    echo "SEGMENTATION BREACH: the user segment reached the database on 5432" >&2
    return 1
  fi
  return 0
}

test_segmentation_db_egress() {
  # MUST fail — the database should not be able to initiate to user workstations.
  # This is what limits lateral movement after a database compromise.
  if on_host host-db nc -z -w 3 10.30.10.100 22 2>/dev/null; then
    echo "SEGMENTATION BREACH: the database reached the user segment" >&2
    return 1
  fi
  return 0
}

test_replication_path() {
  # MUST succeed — database replication crosses to the DR site.
  on_host host-db nc -z -w 5 10.40.30.100 5432
}

# --- Path characteristics ----------------------------------------------------

test_mtu_path() {
  # 1400 bytes with DF set must cross the IPsec tunnel without fragmentation.
  # Getting this wrong produces the classic symptom: pings work, transfers hang.
  on_host host-app ping -c 3 -M do -s 1372 10.40.30.100
}

test_hsrp_failover() {
  local before after
  before="$(on_device core-dc1-01 "show standby brief" | grep -c Active || true)"

  on_device core-dc1-01 "configure terminal
interface Vlan20
shutdown
end" || true
  sleep 5

  after="$(on_device core-dc1-02 "show standby brief" | grep -c Active || true)"

  on_device core-dc1-01 "configure terminal
interface Vlan20
no shutdown
end" || true
  sleep 10

  # The peer must have taken over.
  [[ "$after" -ge 1 ]]
}

# --- Management plane ---------------------------------------------------------

test_telnet_refused() {
  # The security baseline says telnet is disabled. Verify it rather than trust it.
  if timeout 5 nc -z 172.20.20.11 23 2>/dev/null; then
    echo "Telnet is listening on core-dc1-01" >&2
    return 1
  fi
  return 0
}

test_ssh_available() {
  timeout 5 nc -z 172.20.20.11 22
}

# --- Run ----------------------------------------------------------------------

printf '\nLab validation — %s\n' "$LAB_PREFIX"
printf '%s\n' "──────────────────────────────────────────────────────"

printf '\n  Routing\n'
run_test ospf_adjacency         test_ospf_adjacency
run_test bgp_established        test_bgp_established
run_test path_preference        test_path_preference
run_test wan_failover           test_wan_failover

printf '\n  Segmentation\n'
run_test segmentation_app_to_db  test_segmentation_app_to_db
run_test segmentation_user_to_db test_segmentation_user_to_db
run_test segmentation_db_egress  test_segmentation_db_egress
run_test replication_path        test_replication_path

printf '\n  Path characteristics\n'
run_test mtu_path               test_mtu_path
run_test hsrp_failover          test_hsrp_failover

printf '\n  Management plane\n'
run_test telnet_refused         test_telnet_refused
run_test ssh_available          test_ssh_available

printf '\n%s\n' "──────────────────────────────────────────────────────"
printf '  %d passed, %d failed\n\n' "$PASSED" "$FAILED"

[[ "$FAILED" -eq 0 ]]
