"""
Device inventory.

Loads the device register, and — with ``--verify`` — reconciles it against what
is actually reachable on the network.

The reconciliation matters more than it sounds. In every network assessment
worth the name, the register and reality differ: devices that were
decommissioned but never removed, devices that were added and never recorded,
and devices whose role in the register no longer matches what they are doing.
Each of those is a gap in whatever the register is used for — patching,
compliance, capacity, and the change process.
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from .connection import (
    Credentials,
    Device,
    DeviceError,
    connect,
    send_command,
)

log = logging.getLogger(__name__)
console = Console()


def load_inventory(path: Path, site: str = "all") -> list[Device]:
    """Load devices from the YAML register, optionally filtered by site."""
    if not path.exists():
        raise FileNotFoundError(f"Inventory not found: {path}")

    data = yaml.safe_load(path.read_text())
    devices: list[Device] = []

    for entry in data.get("devices", []):
        if site != "all" and entry.get("site") != site:
            continue
        try:
            devices.append(
                Device(
                    hostname=entry["hostname"],
                    address=entry["address"],
                    platform=entry["platform"],
                    site=entry["site"],
                    role=entry["role"],
                    model=entry.get("model", ""),
                    tags=entry.get("tags", []),
                    port=entry.get("port", 22),
                    enable_required=entry.get("enable_required", False),
                )
            )
        except KeyError as exc:
            raise ValueError(
                f"Device entry missing required field {exc}: {entry.get('hostname', entry)}"
            ) from exc

    if not devices:
        raise ValueError(f"No devices in {path} matching site={site!r}")

    return devices


VERSION_COMMANDS = {
    "ios": "show version",
    "iosxe": "show version",
    "nxos": "show version",
    "asa": "show version",
}


def probe(device: Device, credentials: Credentials) -> dict[str, object]:
    """Connect to a device and read back what it says about itself."""
    result: dict[str, object] = {
        "hostname": device.hostname,
        "address": device.address,
        "site": device.site,
        "role": device.role,
        "registered_platform": device.platform,
        "registered_model": device.model,
        "reachable": False,
        "discrepancies": [],
    }

    try:
        with connect(device, credentials) as conn:
            result["reachable"] = True

            output = send_command(conn, VERSION_COMMANDS.get(device.platform, "show version"))
            result["actual_hostname"] = conn.find_prompt().rstrip("#>").strip()
            result["version_raw"] = output

            parsed = _parse_version(output, device.platform)
            result.update(parsed)

            discrepancies: list[str] = []

            actual_hostname = str(result.get("actual_hostname", ""))
            if actual_hostname and actual_hostname.lower() != device.hostname.lower():
                discrepancies.append(
                    f"hostname: register says {device.hostname!r}, device says {actual_hostname!r}"
                )

            actual_model = str(parsed.get("model", ""))
            if device.model and actual_model and device.model.lower() not in actual_model.lower():
                discrepancies.append(
                    f"model: register says {device.model!r}, device reports {actual_model!r}"
                )

            result["discrepancies"] = discrepancies

    except DeviceError as exc:
        result["error"] = str(exc)
        log.warning("%s: %s", device.hostname, exc)

    return result


def _parse_version(output: str, platform: str) -> dict[str, str]:
    """
    Pull the interesting fields out of `show version`.

    Deliberately tolerant: a field that cannot be parsed comes back empty rather
    than raising, because a partially parsed inventory row is still useful and a
    crashed inventory run is not.
    """
    parsed = {"version": "", "model": "", "serial": "", "uptime": ""}

    for line in output.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()

        if platform in ("ios", "iosxe"):
            if "cisco ios" in lowered and "version" in lowered and not parsed["version"]:
                for token in stripped.split(","):
                    if "version" in token.lower():
                        parsed["version"] = token.split()[-1]
            elif lowered.startswith("cisco ") and " (" in stripped and not parsed["model"]:
                parsed["model"] = stripped.split()[1]
            elif "system serial number" in lowered:
                parsed["serial"] = stripped.split(":")[-1].strip()
            elif " uptime is " in lowered and not parsed["uptime"]:
                parsed["uptime"] = stripped.split(" uptime is ")[-1]

        elif platform == "nxos":
            if lowered.startswith("nxos: version") or lowered.startswith("  system version"):
                parsed["version"] = stripped.split()[-1]
            elif "cisco nexus" in lowered and not parsed["model"]:
                parsed["model"] = stripped.replace("cisco ", "").replace("Cisco ", "").strip()
            elif "processor board id" in lowered:
                parsed["serial"] = stripped.split()[-1]
            elif "kernel uptime" in lowered:
                parsed["uptime"] = stripped.split("is")[-1].strip()

        elif platform == "asa":
            if "software version" in lowered and not parsed["version"]:
                parsed["version"] = stripped.split()[-1]
            elif "hardware:" in lowered and not parsed["model"]:
                parsed["model"] = stripped.split(":")[1].split(",")[0].strip()
            elif "serial number" in lowered:
                parsed["serial"] = stripped.split(":")[-1].strip()

    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List the device register, and optionally reconcile it against the network.",
    )
    parser.add_argument("--inventory", type=Path, required=True, help="Path to the device register")
    parser.add_argument("--site", default="all", help="Restrict to one site")
    parser.add_argument("--verify", action="store_true", help="Connect to each device and compare")
    parser.add_argument("--workers", type=int, default=10, help="Parallel connections")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    devices = load_inventory(args.inventory, args.site)

    if not args.verify:
        table = Table(title=f"Device register — {args.site}")
        for column in ("Hostname", "Address", "Platform", "Model", "Site", "Role"):
            table.add_column(column)
        for device in devices:
            table.add_row(
                device.hostname, device.address, device.platform,
                device.model, device.site, device.role,
            )
        console.print(table)
        console.print(f"\n{len(devices)} device(s) registered.\n")
        return 0

    credentials = Credentials.from_environment()
    results: list[dict[str, object]] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(probe, d, credentials): d for d in devices}
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: str(r["hostname"]))

    if args.json:
        import json
        # Drop the raw version output: useful for parsing, noise in a report.
        for result in results:
            result.pop("version_raw", None)
        print(json.dumps(results, indent=2))
        return 0

    table = Table(title=f"Inventory reconciliation — {args.site}")
    for column in ("Hostname", "Reachable", "Version", "Model", "Serial", "Uptime", "Discrepancies"):
        table.add_column(column)

    unreachable = 0
    with_discrepancies = 0

    for result in results:
        reachable = bool(result["reachable"])
        discrepancies = result.get("discrepancies") or []
        if not reachable:
            unreachable += 1
        if discrepancies:
            with_discrepancies += 1

        table.add_row(
            str(result["hostname"]),
            "[green]yes[/green]" if reachable else "[red]no[/red]",
            str(result.get("version", "")),
            str(result.get("model", "")),
            str(result.get("serial", "")),
            str(result.get("uptime", ""))[:28],
            "[yellow]" + "; ".join(discrepancies) + "[/yellow]" if discrepancies
            else str(result.get("error", ""))[:50],
        )

    console.print(table)
    console.print(
        f"\n{len(results)} device(s): "
        f"{len(results) - unreachable} reachable, "
        f"{unreachable} unreachable, "
        f"{with_discrepancies} with register discrepancies.\n"
    )

    # Non-zero when the register does not match reality, so this can gate CI.
    return 1 if (unreachable or with_discrepancies) else 0


if __name__ == "__main__":
    sys.exit(main())
