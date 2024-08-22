"""
Topology discovery from CDP and LLDP neighbours.

Builds a map of what is actually plugged into what, and emits it as Graphviz DOT
for rendering.

Worth doing for two reasons. The first is that the diagram on the wall is always
out of date and this one is not. The second, and more useful: comparing the
discovered topology against the intended one finds links that exist but should
not — the cable someone patched during a migration and never removed, which is
now a second path the spanning tree is quietly blocking.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .connection import Credentials, Device, DeviceError, connect, send_command
from .inventory import load_inventory

log = logging.getLogger(__name__)
console = Console()


@dataclass(frozen=True)
class Link:
    local_device: str
    local_interface: str
    remote_device: str
    remote_interface: str
    protocol: str

    def canonical(self) -> tuple[str, str, str, str]:
        """
        A link discovered from both ends produces two records. Order the endpoints
        consistently so the two collapse into one.
        """
        a = (self.local_device, self.local_interface)
        b = (self.remote_device, self.remote_interface)
        first, second = sorted([a, b])
        return (*first, *second)


def _parse_lldp(output: str, hostname: str) -> list[Link]:
    links: list[Link] = []
    for line in output.splitlines():
        # Local Intf | Chassis id/Device ID | ... | Port ID
        match = re.match(r"^(\S+)\s+(\S+)\s+\d+\s+[\w,\s]*?\s+(\S+)\s*$", line)
        if match and not line.startswith(("Device", "Local", "Capability", "Total")):
            links.append(
                Link(
                    local_device=hostname,
                    local_interface=match.group(1),
                    remote_device=match.group(2).split(".")[0],
                    remote_interface=match.group(3),
                    protocol="lldp",
                )
            )
    return links


def _parse_cdp(output: str, hostname: str) -> list[Link]:
    links: list[Link] = []
    device_id = local_interface = remote_interface = ""

    for line in output.splitlines():
        if match := re.match(r"^Device ID:\s*(\S+)", line):
            device_id = match.group(1).split(".")[0]
        elif match := re.search(r"Interface:\s*(\S+?),\s*Port ID \(outgoing port\):\s*(\S+)", line):
            local_interface, remote_interface = match.group(1), match.group(2)
            if device_id:
                links.append(
                    Link(hostname, local_interface, device_id, remote_interface, "cdp")
                )
                device_id = ""
    return links


def discover(device: Device, credentials: Credentials) -> list[Link]:
    links: list[Link] = []
    try:
        with connect(device, credentials) as conn:
            # LLDP first: it is the standard and runs on non-Cisco equipment too.
            try:
                links.extend(_parse_lldp(send_command(conn, "show lldp neighbors"), device.hostname))
            except DeviceError:
                log.debug("%s: LLDP not available", device.hostname)

            if not links:
                try:
                    links.extend(_parse_cdp(send_command(conn, "show cdp neighbors detail"),
                                            device.hostname))
                except DeviceError:
                    log.debug("%s: CDP not available", device.hostname)

    except DeviceError as exc:
        log.error("%s: %s", device.hostname, exc)

    return links


def to_dot(links: list[Link], known_devices: set[str]) -> str:
    """Render the topology as Graphviz DOT."""
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[Link] = []
    for link in links:
        key = link.canonical()
        if key not in seen:
            seen.add(key)
            unique.append(link)

    lines = [
        "graph network {",
        '  graph [rankdir=TB, splines=ortho, nodesep=0.7, ranksep=1.1, bgcolor="transparent"];',
        '  node  [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10,',
        '         fillcolor="#e8eefc", color="#4a7fe8", penwidth=1.5];',
        '  edge  [fontname="Helvetica", fontsize=8, color="#5a5a57"];',
        "",
    ]

    devices_in_graph = {link.local_device for link in unique} | {link.remote_device for link in unique}
    for name in sorted(devices_in_graph):
        if name not in known_devices:
            # A neighbour that is not in the register: either an unmanaged device
            # or a gap in the register. Either way it should be looked at, so it
            # is drawn differently rather than drawn the same and overlooked.
            lines.append(
                f'  "{name}" [fillcolor="#fdeee4", color="#c2531f", '
                f'label="{name}\\n(not in register)"];'
            )

    lines.append("")
    for link in sorted(unique, key=lambda lk: (lk.local_device, lk.local_interface)):
        lines.append(
            f'  "{link.local_device}" -- "{link.remote_device}" '
            f'[taillabel="{link.local_interface}", headlabel="{link.remote_interface}"];'
        )

    lines.append("}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover the topology from CDP/LLDP.")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--site", default="all")
    parser.add_argument("--output", type=Path, default=Path("reports/topology.dot"))
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(message)s",
    )

    devices = load_inventory(args.inventory, args.site)
    credentials = Credentials.from_environment()
    known = {d.hostname for d in devices}

    links: list[Link] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(discover, d, credentials) for d in devices]
        for future in as_completed(futures):
            links.extend(future.result())

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(to_dot(links, known))

    neighbours_by_device: dict[str, list[Link]] = defaultdict(list)
    for link in links:
        neighbours_by_device[link.local_device].append(link)

    console.print(f"\nTopology — {args.site}\n" + "─" * 60)
    for hostname in sorted(neighbours_by_device):
        console.print(f"\n  [bold]{hostname}[/bold]")
        for link in sorted(neighbours_by_device[hostname], key=lambda lk: lk.local_interface):
            unknown = "" if link.remote_device in known else "  [yellow]← not in the register[/yellow]"
            console.print(
                f"    {link.local_interface:<22} → {link.remote_device}:{link.remote_interface}{unknown}"
            )

    unknown_devices = (
        {link.remote_device for link in links} - known
    )
    console.print("\n" + "─" * 60)
    console.print(f"  {len(links)} adjacency record(s) across {len(neighbours_by_device)} device(s)")
    if unknown_devices:
        console.print(
            f"  [yellow]{len(unknown_devices)} neighbour(s) not in the register: "
            f"{', '.join(sorted(unknown_devices))}[/yellow]"
        )
    console.print(f"\n  DOT written to {args.output}")
    console.print(f"  Render with: dot -Tsvg {args.output} -o topology.svg\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
