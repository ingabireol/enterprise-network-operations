"""
Tests for the compliance rule engine.

The rules themselves are data, and data can be wrong in ways that pass silently:
a regex that matches nothing, a rule that passes on an empty configuration, a
must_not_match that never fires. Those are the failures worth testing, because
a compliance report that says "pass" because a rule is broken is worse than no
report at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "automation" / "python"))

from netops.compliance import Result, Rule, Severity, load_baseline  # noqa: E402
from netops.connection import Device  # noqa: E402

BASELINE_DIR = Path(__file__).parent.parent / "cisco" / "baselines"


@pytest.fixture
def security_rules() -> list[Rule]:
    return load_baseline(BASELINE_DIR / "security-baseline.yml")


@pytest.fixture
def compliant_config() -> str:
    """A configuration that should pass the rules exercised below."""
    return """
hostname core-dc1-01
service password-encryption
service timestamps log datetime msec localtime show-timezone
no service pad
aaa new-model
aaa authentication login default group TACACS_SERVERS local
aaa accounting commands 15 default start-stop group TACACS_SERVERS
ip ssh version 2
no ip http server
no ip source-route
no ip bootp server
no cdp run
snmp-server group NETOPS_RO v3 priv read READ_VIEW access ACL-MGMT-ACCESS
snmp-server user netops-monitor NETOPS_RO v3 auth sha AUTHKEY priv aes 256 PRIVKEY
logging host 10.30.1.16
ntp authenticate
ntp server 10.30.1.10 key 1
banner login ^
Authorised use only.
^
spanning-tree portfast bpduguard default
spanning-tree vlan 1-4094 priority 4096
ip dhcp snooping
ip dhcp snooping vlan 10,11,20
ip arp inspection vlan 10,11,20
udld enable aggressive
errdisable recovery interval 300
control-plane
 service-policy input PM-CoPP
archive
 log config
  logging enable
  write-memory
router ospf 1
 area 0 authentication message-digest
router bgp 65001
 neighbor 198.51.100.1 password 7 ENCRYPTED
 neighbor 198.51.100.1 maximum-prefix 1000 85 restart 30
interface GigabitEthernet1/0/1
 description User access
 switchport nonegotiate
 switchport trunk native vlan 999
 no ip proxy-arp
 load-interval 30
line vty 0 15
 exec-timeout 15 0
 access-class ACL-MGMT-ACCESS in
 transport input ssh
ip access-list extended TEST
 permit tcp any any eq 443
 deny ip any any log
"""


@pytest.fixture
def vulnerable_config() -> str:
    """A configuration with the problems the baseline exists to catch."""
    return """
hostname old-switch
enable password cisco
username admin password 0 cisco
enable secret 5 $1$abcd$hashvalue
snmp-server community public RO
snmp-server community private RW
ip http server
service pad
ip bootp server
line vty 0 4
 exec-timeout 0 0
 transport input all
 password cisco
 login
router ospf 1
 network 10.0.0.0 0.255.255.255 area 0
interface GigabitEthernet0/1
 switchport trunk native vlan 1
"""


@pytest.fixture
def ios_device() -> Device:
    return Device(
        hostname="core-dc1-01", address="10.30.1.11", platform="iosxe",
        site="dc-primary", role="core",
    )


# --- The baseline itself -----------------------------------------------------

def test_security_baseline_loads(security_rules: list[Rule]) -> None:
    assert len(security_rules) > 20


def test_every_rule_has_a_rationale(security_rules: list[Rule]) -> None:
    # A rule without a stated reason gets argued about rather than fixed.
    for rule in security_rules:
        assert rule.rationale.strip(), f"{rule.id} has no rationale"
        assert len(rule.rationale) > 60, f"{rule.id} rationale is too thin to be useful"


def test_every_rule_has_remediation(security_rules: list[Rule]) -> None:
    for rule in security_rules:
        assert rule.remediation.strip(), f"{rule.id} has no remediation guidance"


def test_every_rule_actually_checks_something(security_rules: list[Rule]) -> None:
    # A rule with neither must_match nor must_not_match always passes, which is
    # the silent failure this whole test module exists for.
    for rule in security_rules:
        assert rule.must_match or rule.must_not_match, f"{rule.id} checks nothing"


def test_rule_ids_are_unique(security_rules: list[Rule]) -> None:
    ids = [rule.id for rule in security_rules]
    assert len(ids) == len(set(ids)), "duplicate rule IDs"


def test_severities_are_valid(security_rules: list[Rule]) -> None:
    for rule in security_rules:
        assert isinstance(rule.severity, Severity)


def test_critical_rules_are_genuinely_critical(security_rules: list[Rule]) -> None:
    """
    Severity inflation destroys a baseline's usefulness. If everything is
    critical, nothing is prioritised. Pin the critical set explicitly.
    """
    critical = {r.id for r in security_rules if r.severity == Severity.CRITICAL}
    expected = {"MGMT-001", "AAA-001", "SNMP-001", "SEG-001"}
    assert expected.issubset(critical), f"expected critical rules missing: {expected - critical}"
    assert len(critical) <= 8, f"too many critical rules ({len(critical)}) — severity inflation"


# --- Rule evaluation ---------------------------------------------------------

def test_compliant_config_passes_key_rules(
    security_rules: list[Rule], compliant_config: str
) -> None:
    key_rules = {"MGMT-001", "MGMT-002", "MGMT-005", "AAA-001", "AAA-002",
                 "SNMP-001", "LOG-001", "HARD-001", "HARD-003"}
    for rule in security_rules:
        if rule.id not in key_rules:
            continue
        result, detail = rule.evaluate(compliant_config)
        assert result == Result.PASS, f"{rule.id} ({rule.title}) failed: {detail}"


def test_vulnerable_config_fails_the_right_rules(
    security_rules: list[Rule], vulnerable_config: str
) -> None:
    should_fail = {"MGMT-001", "MGMT-002", "MGMT-005", "AAA-001", "AAA-003",
                   "SNMP-001", "LOG-001", "HARD-001", "HARD-003"}
    failed = {
        rule.id for rule in security_rules
        if rule.evaluate(vulnerable_config)[0] == Result.FAIL
    }
    missing = should_fail - failed
    assert not missing, f"the baseline did not catch: {missing}"


def test_telnet_rule_catches_every_form(security_rules: list[Rule]) -> None:
    rule = next(r for r in security_rules if r.id == "MGMT-001")
    for form in ("transport input all", "transport input telnet",
                 "transport input ssh telnet", "transport input telnet ssh"):
        config = f"line vty 0 15\n {form}\n"
        result, _ = rule.evaluate(config)
        assert result == Result.FAIL, f"{form!r} was not caught"


def test_snmp_rule_catches_any_community(security_rules: list[Rule]) -> None:
    rule = next(r for r in security_rules if r.id == "SNMP-001")
    for community in ("public", "private", "s3cr3t-c0mmun1ty", "NotTheDefault"):
        result, _ = rule.evaluate(f"snmp-server community {community} RO\n")
        assert result == Result.FAIL, f"community {community!r} was not caught"


def test_empty_config_does_not_pass(security_rules: list[Rule]) -> None:
    """
    An empty configuration must not pass a must_match rule. This guards against
    the most dangerous failure mode: a device that could not be read producing a
    clean report.
    """
    must_match_rules = [r for r in security_rules if r.must_match]
    for rule in must_match_rules:
        result, _ = rule.evaluate("")
        assert result == Result.FAIL, f"{rule.id} passed against an empty configuration"


# --- Rule applicability ------------------------------------------------------

def test_rule_applies_to_matching_platform(ios_device: Device) -> None:
    rule = Rule(
        id="TEST-001", title="t", severity=Severity.LOW, rationale="r",
        remediation="x", platforms=["iosxe"], must_match=["^hostname"],
    )
    assert rule.applies_to(ios_device)


def test_rule_skips_other_platforms(ios_device: Device) -> None:
    rule = Rule(
        id="TEST-002", title="t", severity=Severity.LOW, rationale="r",
        remediation="x", platforms=["nxos"], must_match=["^hostname"],
    )
    assert not rule.applies_to(ios_device)


def test_role_scoped_rule_skips_other_roles(ios_device: Device) -> None:
    rule = Rule(
        id="TEST-003", title="t", severity=Severity.LOW, rationale="r",
        remediation="x", platforms=["iosxe"], roles=["access"], must_match=["^hostname"],
    )
    assert not rule.applies_to(ios_device)     # ios_device is role=core


def test_all_patterns_must_be_satisfied() -> None:
    """A rule with several must_match patterns requires all of them, not any."""
    rule = Rule(
        id="TEST-004", title="t", severity=Severity.LOW, rationale="r",
        remediation="x", must_match=["^aaa new-model", "^ip ssh version 2"],
    )
    assert rule.evaluate("aaa new-model\n")[0] == Result.FAIL
    assert rule.evaluate("aaa new-model\nip ssh version 2\n")[0] == Result.PASS


# --- The operational baseline ------------------------------------------------

def test_operational_baseline_loads() -> None:
    rules = load_baseline(BASELINE_DIR / "operational-baseline.yml")
    assert len(rules) >= 5


def test_operational_rules_are_not_critical() -> None:
    """
    The operational baseline is about supportability, not exposure. Nothing in it
    should be critical — if something is, it belongs in the security baseline.
    """
    rules = load_baseline(BASELINE_DIR / "operational-baseline.yml")
    for rule in rules:
        assert rule.severity != Severity.CRITICAL, \
            f"{rule.id} is critical but lives in the operational baseline"


# --- The baseline files as YAML ----------------------------------------------

@pytest.mark.parametrize("filename", ["security-baseline.yml", "operational-baseline.yml"])
def test_baseline_is_valid_yaml(filename: str) -> None:
    data = yaml.safe_load((BASELINE_DIR / filename).read_text())
    assert "metadata" in data
    assert "rules" in data
    assert isinstance(data["rules"], list)
