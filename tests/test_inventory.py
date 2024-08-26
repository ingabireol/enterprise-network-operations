"""
Tests for inventory loading and device handling.

The register is the input to every other tool here, so a malformed entry should
fail loudly at load time rather than producing a partial run that looks complete.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "automation" / "python"))

from netops.connection import Credentials, Device, DeviceError  # noqa: E402
from netops.inventory import load_inventory  # noqa: E402

INVENTORY = Path(__file__).parent.parent / "automation" / "ansible" / "inventory" / "devices.yml"


def test_the_real_inventory_loads() -> None:
    devices = load_inventory(INVENTORY)
    assert len(devices) >= 10


def test_site_filter_works() -> None:
    primary = load_inventory(INVENTORY, "dc-primary")
    secondary = load_inventory(INVENTORY, "dc-secondary")
    assert all(d.site == "dc-primary" for d in primary)
    assert all(d.site == "dc-secondary" for d in secondary)
    assert len(primary) + len(secondary) == len(load_inventory(INVENTORY))


def test_every_registered_device_has_a_known_platform() -> None:
    for device in load_inventory(INVENTORY):
        # Raises for an unknown platform; this asserts it does not.
        assert device.driver


def test_hostnames_are_unique() -> None:
    hostnames = [d.hostname for d in load_inventory(INVENTORY)]
    assert len(hostnames) == len(set(hostnames))


def test_addresses_are_unique() -> None:
    addresses = [d.address for d in load_inventory(INVENTORY)]
    assert len(addresses) == len(set(addresses)), "two devices share a management address"


def test_every_device_has_a_role_the_templates_know_about() -> None:
    known_roles = {"core", "distribution", "access", "wan", "datacenter", "firewall"}
    for device in load_inventory(INVENTORY):
        assert device.role in known_roles, f"{device.hostname} has unknown role {device.role!r}"


def test_critical_devices_come_in_pairs() -> None:
    """
    The design says the core, data centre and firewall layers are redundant.
    Assert the register actually reflects that at the primary site — a single
    device in a supposedly redundant role is a finding, not a configuration detail.
    """
    devices = load_inventory(INVENTORY, "dc-primary")
    for role in ("core", "datacenter", "firewall"):
        count = len([d for d in devices if d.role == role])
        assert count >= 2, f"only {count} {role} device(s) at the primary site — no redundancy"


def test_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_inventory(Path("/nonexistent/devices.yml"))


def test_unknown_site_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No devices"):
        load_inventory(INVENTORY, "no-such-site")


def test_incomplete_entry_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text(yaml.safe_dump({
        "devices": [{"hostname": "sw1", "address": "10.0.0.1"}]   # no platform/site/role
    }))
    with pytest.raises(ValueError, match="missing required field"):
        load_inventory(bad)


def test_unknown_platform_raises() -> None:
    device = Device(
        hostname="x", address="10.0.0.1", platform="juniper-junos",
        site="s", role="core",
    )
    with pytest.raises(DeviceError, match="unknown platform"):
        _ = device.driver


# --- Credentials -------------------------------------------------------------

def test_credentials_require_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    There is deliberately no default credential and no fallback. A tool that
    silently tries a default is a tool that will one day lock out an account
    across the whole estate.
    """
    monkeypatch.delenv("NETOPS_USERNAME", raising=False)
    monkeypatch.delenv("NETOPS_PASSWORD", raising=False)
    with pytest.raises(DeviceError, match="Missing credential"):
        Credentials.from_environment()


def test_credentials_load_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETOPS_USERNAME", "netops")
    monkeypatch.setenv("NETOPS_PASSWORD", "secret")
    monkeypatch.setenv("NETOPS_ENABLE_SECRET", "enable")
    credentials = Credentials.from_environment()
    assert credentials.username == "netops"
    assert credentials.enable_secret == "enable"


def test_error_names_the_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETOPS_USERNAME", "netops")
    monkeypatch.delenv("NETOPS_PASSWORD", raising=False)
    with pytest.raises(DeviceError, match="NETOPS_PASSWORD"):
        Credentials.from_environment()
