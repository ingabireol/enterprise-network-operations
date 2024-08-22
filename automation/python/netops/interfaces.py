"""
Interface reporting.

Errors, discards, duplex mismatches and utilisation across the estate.

Interface errors are the most under-monitored thing in most networks, and they
matter because of how they present: a failing optic or a duplex mismatch does
not take a link down. It corrupts a small fraction of frames, TCP retransmits,
and the user reports that "the system is slow". Weeks are then spent looking at
the application.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .connection import Credentials, Device, DeviceError, connect, send_command
from .inventory import load_inventory

log = logging.getLogger(__name__)
console = Console()


@dataclass
class Interface:
    device: str
    site: str
    name: str
    description: str = ""
    status: str = ""
    protocol: str = ""
    speed: str = ""
    duplex: str = ""
    input_errors: int = 0
    output_errors: int = 0
    crc_errors: int = 0
    input_drops: int = 0
    output_drops: int = 0
    collisions: int = 0
    input_rate_bps: int = 0
    output_rate_bps: int = 0
    bandwidth_bps: int = 0

    @property
    def total_errors(self) -> int:
        return self.input_errors + self.output_errors + self.crc_errors

    @property
    def utilisation_pct(self) -> float:
        if not self.bandwidth_bps:
            return 0.0
        return max(self.input_rate_bps, self.output_rate_bps) / self.bandwidth_bps * 100

    @property
    def diagnosis(self) -> str:
        """
        A short interpretation, because a counter without an interpretation just
        gets ignored.
        """
        notes: list[str] = []

        if self.crc_errors > 0:
            # CRC errors are physical, essentially always: the frame arrived
            # corrupted. Software does not cause this.
            notes.append("CRC errors — cable, optic or connector")

        if self.duplex.lower() == "half" and "10" not in self.speed:
            notes.append("half duplex on a fast link — almost certainly a mismatch")

        if self.collisions > 0 and self.duplex.lower() != "half":
            notes.append("collisions on a full-duplex link — duplex mismatch")

        if self.output_drops > 0 and self.utilisation_pct > 70:
            notes.append("output drops with high utilisation — congestion")
        elif self.output_drops > 0:
            notes.append("output drops without congestion — check the queue policy")

        if self.input_errors > 0 and self.crc_errors == 0:
            notes.append("input errors without CRC — check MTU and framing")

        if self.utilisation_pct > 80:
            notes.append(f"{self.utilisation_pct:.0f}% utilised — capacity")

        return "; ".join(notes)


def _parse_ios_interfaces(output: str, device: Device) -> list[Interface]:
    """Parse `show interfaces` output."""
    interfaces: list[Interface] = []
    current: Interface | None = None

    for line in output.splitlines():
        header = re.match(r"^(\S+) is (.+?), line protocol is (\S+)", line)
        if header:
            if current:
                interfaces.append(current)
            current = Interface(
                device=device.hostname, site=device.site,
                name=header.group(1),
                status=header.group(2).strip(),
                protocol=header.group(3).strip(),
            )
            continue

        if current is None:
            continue

        if match := re.search(r"Description:\s*(.+)", line):
            current.description = match.group(1).strip()
        elif match := re.search(r"BW (\d+) Kbit", line):
            current.bandwidth_bps = int(match.group(1)) * 1000
        elif match := re.search(r"(\S+)-duplex,\s*(\S+)", line):
            current.duplex = match.group(1)
            current.speed = match.group(2)
        elif match := re.search(r"input rate (\d+) bits/sec", line):
            current.input_rate_bps = int(match.group(1))
        elif match := re.search(r"output rate (\d+) bits/sec", line):
            current.output_rate_bps = int(match.group(1))
        elif match := re.search(r"(\d+) input errors, (\d+) CRC", line):
            current.input_errors = int(match.group(1))
            current.crc_errors = int(match.group(2))
        elif match := re.search(r"(\d+) output errors, (\d+) collisions", line):
            current.output_errors = int(match.group(1))
            current.collisions = int(match.group(2))
        elif match := re.search(r"Input queue: \d+/\d+/(\d+)/", line):
            current.input_drops = int(match.group(1))
        elif match := re.search(r"Total output drops: (\d+)", line):
            current.output_drops = int(match.group(1))

    if current:
        interfaces.append(current)

    return interfaces


def collect(device: Device, credentials: Credentials) -> list[Interface]:
    try:
        with connect(device, credentials, read_timeout_override=180) as conn:
            if device.platform in ("ios", "iosxe"):
                return _parse_ios_interfaces(send_command(conn, "show interfaces"), device)
            if device.platform == "nxos":
                # NX-OS output differs enough to warrant its own parser; the
                # counters command is used here as the closest equivalent.
                return _parse_ios_interfaces(send_command(conn, "show interface"), device)
            log.info("%s: interface collection not implemented for %s",
                     device.hostname, device.platform)
            return []
    except DeviceError as exc:
        log.error("%s: %s", device.hostname, exc)
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report interface errors and utilisation.")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--site", default="all")
    parser.add_argument("--errors-only", action="store_true",
                        help="Only interfaces with errors, drops or high utilisation")
    parser.add_argument("--min-errors", type=int, default=1)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(message)s",
    )

    devices = load_inventory(args.inventory, args.site)
    credentials = Credentials.from_environment()

    all_interfaces: list[Interface] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(collect, d, credentials) for d in devices]
        for future in as_completed(futures):
            all_interfaces.extend(future.result())

    interesting = all_interfaces
    if args.errors_only:
        interesting = [
            i for i in all_interfaces
            if i.total_errors >= args.min_errors
            or i.output_drops > 0
            or i.utilisation_pct > 70
            or (i.duplex.lower() == "half" and "10" not in i.speed)
        ]

    interesting.sort(key=lambda i: (-i.total_errors, -i.utilisation_pct))

    table = Table(title=f"Interfaces — {args.site}"
                        + (" (errors, drops and high utilisation only)" if args.errors_only else ""))
    for column in ("Device", "Interface", "Description", "Status", "Errors", "CRC",
                   "Drops", "Util", "Diagnosis"):
        table.add_column(column, overflow="fold")

    for interface in interesting[:120]:
        error_style = "red" if interface.total_errors > 0 else ""
        table.add_row(
            interface.device,
            interface.name,
            interface.description[:28],
            f"{interface.status}/{interface.protocol}",
            f"[{error_style}]{interface.total_errors}[/{error_style}]" if error_style
            else str(interface.total_errors),
            str(interface.crc_errors),
            str(interface.output_drops),
            f"{interface.utilisation_pct:.0f}%" if interface.bandwidth_bps else "-",
            interface.diagnosis[:60],
        )

    console.print(table)
    console.print(
        f"\n{len(all_interfaces)} interface(s) across {len(devices)} device(s); "
        f"{len([i for i in all_interfaces if i.total_errors > 0])} with errors.\n"
    )

    return 1 if any(i.crc_errors > 0 for i in all_interfaces) else 0


if __name__ == "__main__":
    sys.exit(main())
