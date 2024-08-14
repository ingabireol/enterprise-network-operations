"""
Device connection handling.

Everything that talks to a device goes through here, so that retry behaviour,
timeouts, credential handling and error classification are consistent across
every tool rather than being re-implemented slightly differently in each.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

log = logging.getLogger(__name__)

# Netmiko device types for the platforms in this estate.
PLATFORM_DRIVERS = {
    "ios": "cisco_ios",
    "iosxe": "cisco_xe",
    "nxos": "cisco_nxos",
    "asa": "cisco_asa",
    "iosxr": "cisco_xr",
}


class DeviceError(Exception):
    """Base class for anything that goes wrong talking to a device."""


class DeviceUnreachable(DeviceError):
    """The device did not answer. Distinct from a rejected credential."""


class DeviceAuthFailed(DeviceError):
    """The device answered and refused the credentials."""


class CommandFailed(DeviceError):
    """The device accepted the command and returned an error."""


@dataclass
class Device:
    """A single managed device."""

    hostname: str
    address: str
    platform: str
    site: str
    role: str
    model: str = ""
    tags: list[str] = field(default_factory=list)
    port: int = 22
    enable_required: bool = False

    @property
    def driver(self) -> str:
        driver = PLATFORM_DRIVERS.get(self.platform)
        if driver is None:
            raise DeviceError(
                f"{self.hostname}: unknown platform {self.platform!r}. "
                f"Known platforms: {', '.join(sorted(PLATFORM_DRIVERS))}"
            )
        return driver

    def __str__(self) -> str:
        return f"{self.hostname} ({self.address}, {self.platform})"


@dataclass
class Credentials:
    """
    Device credentials.

    Read from the environment. There is deliberately no default and no prompt
    fallback in the non-interactive path: a tool that silently tries a default
    credential set is a tool that will one day lock out an account across the
    whole estate.
    """

    username: str
    password: str
    enable_secret: str = ""

    @classmethod
    def from_environment(cls) -> "Credentials":
        username = os.environ.get("NETOPS_USERNAME")
        password = os.environ.get("NETOPS_PASSWORD")

        missing = [
            name
            for name, value in (("NETOPS_USERNAME", username), ("NETOPS_PASSWORD", password))
            if not value
        ]
        if missing:
            raise DeviceError(
                f"Missing credential environment variable(s): {', '.join(missing)}. "
                "Set them from your password manager or Ansible Vault; they are "
                "deliberately not read from any file in this repository."
            )

        return cls(
            username=username,           # type: ignore[arg-type]
            password=password,           # type: ignore[arg-type]
            enable_secret=os.environ.get("NETOPS_ENABLE_SECRET", ""),
        )


@contextmanager
def connect(
    device: Device,
    credentials: Credentials,
    *,
    timeout: int = 30,
    retries: int = 2,
    read_timeout_override: int | None = None,
) -> Iterator[Any]:
    """
    Open a connection to a device, retrying transient failures.

    An authentication failure is never retried: repeating a rejected credential
    is how an account gets locked out across an entire estate in under a minute.
    """
    params: dict[str, Any] = {
        "device_type": device.driver,
        "host": device.address,
        "port": device.port,
        "username": credentials.username,
        "password": credentials.password,
        "timeout": timeout,
        "auth_timeout": timeout,
        "banner_timeout": 20,
        "fast_cli": False,
        "session_log": None,
    }
    if device.enable_required and credentials.enable_secret:
        params["secret"] = credentials.enable_secret

    last_error: Exception | None = None
    connection = None

    for attempt in range(1, retries + 2):
        try:
            log.debug("Connecting to %s (attempt %d)", device.hostname, attempt)
            connection = ConnectHandler(**params)
            if device.enable_required and credentials.enable_secret:
                connection.enable()
            break

        except NetmikoAuthenticationException as exc:
            # Do not retry. See the docstring.
            raise DeviceAuthFailed(
                f"{device.hostname}: authentication rejected. Not retrying — "
                f"repeated attempts risk locking the account out estate-wide."
            ) from exc

        except (NetmikoTimeoutException, socket.error, OSError) as exc:
            last_error = exc
            if attempt <= retries:
                backoff = 2 ** attempt
                log.warning(
                    "%s: connection failed (%s); retrying in %ds",
                    device.hostname, exc.__class__.__name__, backoff,
                )
                time.sleep(backoff)
            continue

    if connection is None:
        raise DeviceUnreachable(
            f"{device.hostname} ({device.address}): unreachable after "
            f"{retries + 1} attempt(s). Last error: {last_error}"
        )

    try:
        if read_timeout_override:
            connection.read_timeout_override = read_timeout_override
        yield connection
    finally:
        try:
            connection.disconnect()
        except Exception:  # noqa: BLE001 - a failed disconnect must never mask the real result
            log.debug("%s: error while disconnecting (ignored)", device.hostname)


def send_command(connection: Any, command: str, *, expect_string: str | None = None) -> str:
    """
    Run a command and return its output, raising on a device-side error.

    Netmiko returns the error text as ordinary output, so a caller that does not
    check would happily record `% Invalid input detected` as a configuration
    backup. This checks.
    """
    output = connection.send_command(
        command,
        expect_string=expect_string,
        read_timeout=120,
    )

    error_markers = (
        "% Invalid input",
        "% Incomplete command",
        "% Ambiguous command",
        "% Authorization failed",
        "Command authorization failed",
        "% Permission denied",
    )
    for marker in error_markers:
        if marker.lower() in output.lower():
            raise CommandFailed(f"{command!r} rejected by the device: {output.strip()[:200]}")

    return output
