"""
Configuration change detection.

Compares each device's current running configuration against its last backup and
reports what changed.

This answers the question that comes up in almost every incident review — *what
changed?* — without relying on anybody having remembered to record it. An
undocumented change is not usually malice; it is someone fixing something at
02:00 and intending to write it up. This makes the write-up unnecessary.
"""

from __future__ import annotations

import argparse
import difflib
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.syntax import Syntax

from .backup import RUNNING_CONFIG_COMMANDS, normalise
from .connection import Credentials, Device, DeviceError, connect, send_command
from .inventory import load_inventory

log = logging.getLogger(__name__)
console = Console()

# Changes worth drawing attention to. A diff of 200 lines where three of them
# touch an access list should not read as 200 equally interesting lines.
SIGNIFICANT_PATTERNS = [
    (re.compile(r"access-list|access-group|ip access-list", re.I), "security policy"),
    (re.compile(r"^\s*(no )?shutdown", re.I | re.M), "interface state"),
    (re.compile(r"router (ospf|bgp|eigrp)|neighbor |network ", re.I), "routing"),
    (re.compile(r"aaa |tacacs|radius|username |snmp-server", re.I), "management access"),
    (re.compile(r"crypto |ipsec|ikev2", re.I), "encryption"),
    (re.compile(r"vlan |switchport|spanning-tree", re.I), "layer 2"),
    (re.compile(r"vpc |standby |failover", re.I), "high availability"),
]


@dataclass
class ConfigDiff:
    hostname: str
    site: str
    has_baseline: bool = False
    changed: bool = False
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unified: str = ""
    categories: set[str] = field(default_factory=set)
    error: str = ""

    @property
    def summary(self) -> str:
        if self.error:
            return f"error: {self.error}"
        if not self.has_baseline:
            return "no baseline backup — this is the first capture"
        if not self.changed:
            return "unchanged"
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)}")
        if self.removed:
            parts.append(f"-{len(self.removed)}")
        category_note = f" [{', '.join(sorted(self.categories))}]" if self.categories else ""
        return " ".join(parts) + category_note


def compare(device: Device, credentials: Credentials, backup_dir: Path) -> ConfigDiff:
    """Compare a device's live configuration against its stored backup."""
    result = ConfigDiff(hostname=device.hostname, site=device.site)
    baseline_path = backup_dir / device.site / f"{device.hostname}.cfg"

    try:
        command = RUNNING_CONFIG_COMMANDS.get(device.platform, "show running-config")
        with connect(device, credentials, read_timeout_override=180) as conn:
            current = normalise(send_command(conn, command))

        if not baseline_path.exists():
            return result

        result.has_baseline = True

        # Drop the header the backup writes, which is not part of the configuration.
        baseline = baseline_path.read_text()
        baseline = "\n".join(
            line for line in baseline.splitlines()
            if not line.startswith("! Retrieved:")
            and not line.startswith("! Backup of")
            and not line.startswith("! Platform:")
            and not line.startswith("! Normalised")
        )

        baseline_lines = baseline.splitlines()
        current_lines = current.splitlines()

        if baseline_lines == current_lines:
            return result

        result.changed = True
        result.unified = "\n".join(
            difflib.unified_diff(
                baseline_lines, current_lines,
                fromfile=f"{device.hostname} (backup)",
                tofile=f"{device.hostname} (running)",
                lineterm="", n=3,
            )
        )

        for line in result.unified.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                result.added.append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                result.removed.append(line[1:])

        changed_text = "\n".join(result.added + result.removed)
        for pattern, category in SIGNIFICANT_PATTERNS:
            if pattern.search(changed_text):
                result.categories.add(category)

    except DeviceError as exc:
        result.error = str(exc)
        log.error("%s: %s", device.hostname, exc)

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show configuration changes since the last backup.",
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--site", default="all")
    parser.add_argument("--backups", type=Path, default=Path("backups"))
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--full", action="store_true", help="Print the complete diff for each device")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(message)s",
    )

    devices = load_inventory(args.inventory, args.site)
    credentials = Credentials.from_environment()

    results: list[ConfigDiff] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(compare, d, credentials, args.backups) for d in devices]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: (r.site, r.hostname))

    changed = [r for r in results if r.changed]
    errors = [r for r in results if r.error]

    console.print(f"\nConfiguration drift — {args.site}\n" + "─" * 66)
    for result in results:
        if result.changed:
            marker = "[yellow]CHANGED [/yellow]"
        elif result.error:
            marker = "[red]ERROR   [/red]"
        else:
            marker = "[green]unchanged[/green]"
        console.print(f"  {marker} {result.hostname:<24} {result.summary}")

    console.print("─" * 66)
    console.print(
        f"  {len(results)} device(s): {len(changed)} changed, {len(errors)} unreachable\n"
    )

    if changed:
        for result in changed:
            console.print(f"\n[bold]{result.hostname}[/bold] ({result.site})")
            if result.categories:
                console.print(f"  Areas touched: {', '.join(sorted(result.categories))}")

            if args.full:
                console.print(Syntax(result.unified, "diff", theme="ansi_dark"))
            else:
                # Show the security-relevant lines by default; the rest on --full.
                interesting = [
                    line for line in result.unified.splitlines()
                    if (line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
                    and any(p.search(line) for p, _ in SIGNIFICANT_PATTERNS)
                ]
                for line in interesting[:20]:
                    colour = "green" if line.startswith("+") else "red"
                    console.print(f"  [{colour}]{line}[/{colour}]")
                if len(interesting) > 20:
                    console.print(f"  … and {len(interesting) - 20} more; use --full for everything")

        console.print(
            "\nEach change above should correspond to an approved change record.\n"
            "One that does not is worth a conversation, not an accusation — but it is\n"
            "worth the conversation.\n"
        )

    if errors:
        console.print("[red]Devices that could not be compared:[/red]")
        for result in errors:
            console.print(f"  [red]{result.hostname}[/red]: {result.error}")
        console.print("")

    return 1 if (changed or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
