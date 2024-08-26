"""
Tests for configuration backup normalisation and redaction.

Two properties matter here and both are easy to get wrong:

1. **Normalisation must be idempotent and must remove everything volatile.** If it
   misses one timestamp line, every nightly backup produces a diff, the diffs
   become meaningless, and the drift report — which is the whole point — gets
   ignored within a fortnight.

2. **Redaction must not miss anything.** A backup repository is a second copy of
   every device configuration, usually with broader read access than the devices
   themselves. A missed key is a credential leak with a long tail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "automation" / "python"))

from netops.backup import normalise  # noqa: E402


# --- Volatile line removal ---------------------------------------------------

@pytest.mark.parametrize("volatile_line", [
    "! Last configuration change at 14:23:11 CAT Thu Sep 25 2026 by netadmin",
    "! NVRAM config last updated at 09:00:02 CAT Thu Sep 25 2026",
    "Current configuration : 18432 bytes",
    "Building configuration...",
    "ntp clock-period 17179869",
    ": Written by netadmin at 14:23:11.123 CAT Thu Sep 25 2026",
])
def test_volatile_lines_are_removed(volatile_line: str) -> None:
    config = f"hostname test-switch\n{volatile_line}\ninterface Gi0/1\n"
    result = normalise(config)
    assert volatile_line not in result
    assert "hostname test-switch" in result
    assert "interface Gi0/1" in result


def test_two_reads_of_an_unchanged_device_produce_identical_output() -> None:
    """
    The property the whole drift report depends on: reading the same unchanged
    device twice must produce byte-identical normalised output.
    """
    first = """Building configuration...

Current configuration : 18432 bytes
!
! Last configuration change at 14:23:11 CAT Thu Sep 25 2026 by netadmin
! NVRAM config last updated at 09:00:02 CAT Thu Sep 25 2026
!
hostname core-dc1-01
!
ntp clock-period 17179869
!
interface GigabitEthernet1/0/1
 description Uplink
!
end
"""
    second = """Building configuration...

Current configuration : 18439 bytes
!
! Last configuration change at 16:45:02 CAT Thu Sep 25 2026 by netadmin
! NVRAM config last updated at 15:30:11 CAT Thu Sep 25 2026
!
hostname core-dc1-01
!
ntp clock-period 17179870
!
interface GigabitEthernet1/0/1
 description Uplink
!
end
"""
    assert normalise(first) == normalise(second)


def test_a_real_change_still_shows() -> None:
    """Normalisation must not be so aggressive that it hides actual changes."""
    before = normalise("hostname core-dc1-01\ninterface Gi1/0/1\n description Uplink\n")
    after = normalise("hostname core-dc1-01\ninterface Gi1/0/1\n description Uplink to access\n")
    assert before != after


def test_normalisation_is_idempotent() -> None:
    config = "! Last configuration change at 14:23:11\nhostname test\n\n\n\ninterface Gi0/1\n"
    once = normalise(config)
    assert normalise(once) == once


# --- Redaction ---------------------------------------------------------------

@pytest.mark.parametrize("secret_line,secret_value", [
    ("username admin secret 9 $9$abcdefghij", "$9$abcdefghij"),
    ("enable secret 9 $9$zyxwvutsrq", "$9$zyxwvutsrq"),
    (" key 7 070C285F4D06", "070C285F4D06"),
    (" neighbor 198.51.100.1 password 7 121A0C041104", "121A0C041104"),
    (" standby 20 authentication md5 key-string 7 08351F1B1D49", "08351F1B1D49"),
    ("ntp authentication-key 1 md5 15130A1E0A0F 7", "15130A1E0A0F"),
    (" pre-shared-key local MySharedSecret123", "MySharedSecret123"),
    (" failover key FailoverSecret99", "FailoverSecret99"),
])
def test_secrets_are_redacted(secret_line: str, secret_value: str) -> None:
    config = f"hostname test\n{secret_line}\n"
    result = normalise(config)
    assert secret_value not in result, f"leaked: {secret_value}"
    assert "<REDACTED>" in result


def test_snmpv3_keys_are_both_redacted() -> None:
    config = "snmp-server user netops NETOPS_RO v3 auth sha AuthKey12345 priv aes 256 PrivKey67890\n"
    result = normalise(config)
    assert "AuthKey12345" not in result
    assert "PrivKey67890" not in result


def test_certificate_blobs_are_removed() -> None:
    config = """hostname test
crypto pki certificate chain TP-self-signed
-----BEGIN CERTIFICATE-----
MIIDVzCCAj+gAwIBAgIBATANBgkqhkiG9w0BAQsFADA7MRkwFwYDVQQDExBJT1Mt
U2VsZi1TaWduZWQtQ2VydGlmaWNhdGUtMTIzNDU2Nzg5MB4XDTI2MDEwMTAwMDAw
-----END CERTIFICATE-----
interface Gi0/1
"""
    result = normalise(config)
    assert "MIIDVzCCAj" not in result
    assert "<CERTIFICATE REDACTED>" in result
    assert "interface Gi0/1" in result


def test_redaction_preserves_the_structure() -> None:
    """
    Redaction replaces the value, not the line. The presence of a key is itself
    information a compliance audit needs — a device with no key configured at all
    is a different finding from one whose key we cannot see.
    """
    config = " neighbor 198.51.100.1 password 7 121A0C041104\n"
    result = normalise(config)
    assert "neighbor 198.51.100.1 password 7" in result
    assert "121A0C041104" not in result


def test_non_secret_content_is_untouched() -> None:
    config = """hostname core-dc1-01
interface GigabitEthernet1/0/1
 description Uplink to acc-dc1-f1-01
 switchport mode trunk
 switchport trunk allowed vlan 10,20,30
ip access-list extended ACL-TEST
 permit tcp 10.30.20.0 0.0.0.255 host 10.30.30.11 eq 5432
 deny ip any any log
"""
    result = normalise(config)
    for line in config.strip().splitlines():
        assert line in result, f"normalisation removed a non-secret line: {line!r}"


def test_output_ends_with_exactly_one_newline() -> None:
    assert normalise("hostname test\n\n\n\n").endswith("test\n")
    assert not normalise("hostname test\n\n\n\n").endswith("\n\n")
