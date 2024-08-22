"""
Device health polling.

Collects the handful of numbers that actually predict a network problem, across
the whole estate, in one pass: CPU, memory, temperature and power, interface
errors, routing adjacency counts, and high-availability state.

The selection is deliberate. A device exposes thousands of counters and almost
all of them are noise. These are the ones where a change means something is
about to go wrong, or already has:

* **CPU sustained high** — usually a process doing something it should not, or
  a traffic pattern being punted to the CPU rather than switched in hardware.
* **Memory falling steadily** — a leak. The device works until it abruptly does not.
* **Temperature and power** — the two hardware failures that give warning.
* **Interface errors** — a cable, an optic or a duplex mismatch, all of which
  present to users as "the network is slow" long before anything goes down.
* **Adjacency count changing** — a routing neighbour has been lost, which may
  have been absorbed by redundancy and therefore reported by nobody.
* **HA state** — a standby unit that is not actually ready is a redundancy that
  does not exist, and it is invisible until the day it is needed.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .connection import Credentials, Device, DeviceError, connect, send_command
from .inventory import load_inventory

log = logging.getLogger(__name__)
console = Console()

CPU_WARN, CPU_CRIT = 70, 85
MEM_WARN, MEM_CRIT = 80, 90


@dataclass
class Health:
    hostname: str
    site: str
    role: str
    reachable: bool = False
    cpu_5min: int | None = None
    memory_used_pct: int | None = None
    temperature_state: str = ""
    power_state: str = ""
    fan_state: str = ""
    uptime: str = ""
    interfaces_down: list[str] = field(default_factory=list)
    interfaces_with_errors: list[str] = field(default_factory=list)
    ospf_neighbours: int | None = None
    bgp_peers_established: int | None = None
    bgp_peers_total: int | None = None
    ha_state: str = ""
    problems: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def status(self) -> str:
        if not self.reachable:
            return "UNREACHABLE"
        if any(p.startswith("CRITICAL") for p in self.problems):
            return "CRITICAL"
        if self.problems:
            return "WARNING"
        return "OK"


COMMANDS: dict[str, dict[str, str]] = {
    "ios": {
        "cpu": "show processes cpu | include CPU utilization",
        "memory": "show processes memory sorted | include Processor Pool",
        "environment": "show environment all",
        "interfaces": "show interfaces | include line protocol|error",
        "interface_status": "show ip interface brief | exclude unassigned",
        "ospf": "show ip ospf neighbor",
        "bgp": "show ip bgp summary",
        "uptime": "show version | include uptime",
    },
    "nxos": {
        "cpu": "show system resources",
        "memory": "show system resources",
        "environment": "show environment",
        "interfaces": "show interface counters errors",
        "interface_status": "show interface brief",
        "ospf": "show ip ospf neighbors",
        "bgp": "show bgp ipv4 unicast summary",
        "uptime": "show version | include uptime",
        "ha": "show vpc brief",
    },
    "asa": {
        "cpu": "show cpu usage",
        "memory": "show memory",
        "environment": "show environment",
        "interface_status": "show interface ip brief",
        "uptime": "show version | include up",
        "ha": "show failover state",
    },
}
COMMANDS["iosxe"] = COMMANDS["ios"]


def _parse_cpu(output: str, platform: str) -> int | None:
    if platform in ("ios", "iosxe"):
        match = re.search(r"five minutes:\s*(\d+)%", output, re.I)
        return int(match.group(1)) if match else None
    if platform == "nxos":
        match = re.search(r"CPU states\s*:\s*[\d.]+% user,\s*[\d.]+% kernel,\s*([\d.]+)% idle", output, re.I)
        return int(100 - float(match.group(1))) if match else None
    if platform == "asa":
        match = re.search(r"5 minutes:\s*([\d.]+)%", output, re.I)
        return int(float(match.group(1))) if match else None
    return None


def _parse_memory(output: str, platform: str) -> int | None:
    if platform in ("ios", "iosxe"):
        match = re.search(r"Processor Pool Total:\s*(\d+)\s+Used:\s*(\d+)", output, re.I)
        if match:
            total, used = int(match.group(1)), int(match.group(2))
            return int(used / total * 100) if total else None
    elif platform == "nxos":
        match = re.search(r"Memory usage:\s*(\d+)K total,\s*(\d+)K used", output, re.I)
        if match:
            total, used = int(match.group(1)), int(match.group(2))
            return int(used / total * 100) if total else None
    elif platform == "asa":
        match = re.search(r"Used memory:\s*(\d+)", output, re.I)
        free = re.search(r"Free memory:\s*(\d+)", output, re.I)
        if match and free:
            used, freed = int(match.group(1)), int(free.group(1))
            total = used + freed
            return int(used / total * 100) if total else None
    return None


def poll(device: Device, credentials: Credentials) -> Health:
    """Poll one device."""
    health = Health(hostname=device.hostname, site=device.site, role=device.role)
    commands = COMMANDS.get(device.platform, COMMANDS["ios"])

    try:
        with connect(device, credentials) as conn:
            health.reachable = True

            # --- CPU -------------------------------------------------------
            if "cpu" in commands:
                health.cpu_5min = _parse_cpu(send_command(conn, commands["cpu"]), device.platform)
                if health.cpu_5min is not None:
                    if health.cpu_5min >= CPU_CRIT:
                        health.problems.append(f"CRITICAL: CPU at {health.cpu_5min}% (5-minute average)")
                    elif health.cpu_5min >= CPU_WARN:
                        health.problems.append(f"CPU at {health.cpu_5min}% (5-minute average)")

            # --- Memory ----------------------------------------------------
            if "memory" in commands:
                health.memory_used_pct = _parse_memory(
                    send_command(conn, commands["memory"]), device.platform
                )
                if health.memory_used_pct is not None:
                    if health.memory_used_pct >= MEM_CRIT:
                        health.problems.append(f"CRITICAL: memory at {health.memory_used_pct}%")
                    elif health.memory_used_pct >= MEM_WARN:
                        health.problems.append(f"memory at {health.memory_used_pct}%")

            # --- Environment -----------------------------------------------
            if "environment" in commands:
                env = send_command(conn, commands["environment"])
                lowered = env.lower()

                if re.search(r"(temperature|temp).*(alarm|critical|shutdown|fail)", lowered):
                    health.temperature_state = "alarm"
                    health.problems.append("CRITICAL: temperature alarm")
                elif "temperature" in lowered:
                    health.temperature_state = "normal"

                if re.search(r"(power|ps\d|psu).*(fail|not ok|absent|error)", lowered):
                    health.power_state = "fault"
                    # A redundant supply that has failed is not an outage today
                    # and is an outage the day the other one fails.
                    health.problems.append("CRITICAL: power supply fault — redundancy lost")
                elif "power" in lowered:
                    health.power_state = "ok"

                if re.search(r"fan.*(fail|not ok|error|shutdown)", lowered):
                    health.fan_state = "fault"
                    health.problems.append("CRITICAL: fan fault")
                elif "fan" in lowered:
                    health.fan_state = "ok"

            # --- Interfaces --------------------------------------------------
            if "interface_status" in commands:
                status = send_command(conn, commands["interface_status"])
                for line in status.splitlines():
                    # A configured interface that is administratively up but has
                    # no line protocol is a fault. One that is shut down is a
                    # decision, and is not reported here.
                    if re.search(r"\bup\s+down\b", line, re.I):
                        health.interfaces_down.append(line.split()[0])

                if health.interfaces_down:
                    health.problems.append(
                        f"{len(health.interfaces_down)} interface(s) up/down: "
                        + ", ".join(health.interfaces_down[:5])
                    )

            if "interfaces" in commands:
                errors = send_command(conn, commands["interfaces"])
                current_interface = ""
                for line in errors.splitlines():
                    if re.match(r"^\S+\s+is\s+", line):
                        current_interface = line.split()[0]
                    error_counts = re.findall(r"(\d+) (?:input|output) errors", line)
                    if error_counts and any(int(n) > 0 for n in error_counts) and current_interface:
                        health.interfaces_with_errors.append(current_interface)

                if health.interfaces_with_errors:
                    unique = sorted(set(health.interfaces_with_errors))
                    health.problems.append(
                        f"{len(unique)} interface(s) with errors: " + ", ".join(unique[:5])
                    )

            # --- Routing ------------------------------------------------------
            if "ospf" in commands:
                try:
                    ospf = send_command(conn, commands["ospf"])
                    health.ospf_neighbours = len(re.findall(r"\bFULL\b", ospf, re.I))
                    if health.ospf_neighbours == 0:
                        health.problems.append("CRITICAL: no OSPF neighbours in FULL state")
                except DeviceError:
                    pass   # OSPF is not configured on every device; not a problem

            if "bgp" in commands:
                try:
                    bgp = send_command(conn, commands["bgp"])
                    peers = re.findall(
                        r"^\d+\.\d+\.\d+\.\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\S+\s+(\S+)",
                        bgp, re.M,
                    )
                    health.bgp_peers_total = len(peers)
                    # A numeric value in the State/PfxRcd column means the
                    # session is established and that many prefixes are received.
                    health.bgp_peers_established = sum(1 for state in peers if state.isdigit())
                    if health.bgp_peers_total and health.bgp_peers_established < health.bgp_peers_total:
                        down = health.bgp_peers_total - health.bgp_peers_established
                        health.problems.append(
                            f"CRITICAL: {down} of {health.bgp_peers_total} BGP peer(s) not established"
                        )
                except DeviceError:
                    pass

            # --- High availability ---------------------------------------------
            if "ha" in commands:
                try:
                    ha = send_command(conn, commands["ha"])
                    health.ha_state = ha.strip().splitlines()[0][:60] if ha.strip() else ""

                    if device.platform == "asa":
                        if "failover off" in ha.lower():
                            health.problems.append("CRITICAL: failover is disabled")
                        elif "failed" in ha.lower():
                            health.problems.append("CRITICAL: failover peer has failed")
                    elif device.platform == "nxos":
                        if "peer adjacency formed ok" not in ha.lower():
                            health.problems.append("CRITICAL: vPC peer adjacency is not formed")
                except DeviceError:
                    pass

            # --- Uptime ---------------------------------------------------------
            if "uptime" in commands:
                uptime = send_command(conn, commands["uptime"])
                match = re.search(r"uptime is (.+)", uptime, re.I)
                if match:
                    health.uptime = match.group(1).strip()
                    # A device that has been up for minutes rebooted recently,
                    # and nobody may have noticed.
                    if re.match(r"^\d+ minutes?$", health.uptime):
                        health.problems.append(f"device rebooted recently (up {health.uptime})")

    except DeviceError as exc:
        health.error = str(exc)
        health.problems.append(f"CRITICAL: {exc}")
        log.error("%s: %s", device.hostname, exc)

    return health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll device health across the estate.")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--site", default="all")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--problems-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--metrics", type=Path, help="Write Prometheus textfile metrics here")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(message)s",
    )

    devices = load_inventory(args.inventory, args.site)
    credentials = Credentials.from_environment()

    results: list[Health] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(poll, d, credentials) for d in devices]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda h: (h.site, h.hostname))

    if args.json:
        import dataclasses
        import json
        print(json.dumps([dataclasses.asdict(h) for h in results], indent=2))
    else:
        table = Table(title=f"Device health — {args.site}")
        for column in ("Status", "Device", "CPU", "Mem", "Env", "OSPF", "BGP", "Problems"):
            table.add_column(column, overflow="fold")

        status_colour = {
            "OK": "green", "WARNING": "yellow",
            "CRITICAL": "red", "UNREACHABLE": "red",
        }

        for health in results:
            if args.problems_only and health.status == "OK":
                continue
            colour = status_colour[health.status]
            env = "ok"
            if "fault" in (health.power_state, health.fan_state) or health.temperature_state == "alarm":
                env = "[red]fault[/red]"

            table.add_row(
                f"[{colour}]{health.status}[/{colour}]",
                health.hostname,
                f"{health.cpu_5min}%" if health.cpu_5min is not None else "-",
                f"{health.memory_used_pct}%" if health.memory_used_pct is not None else "-",
                env,
                str(health.ospf_neighbours) if health.ospf_neighbours is not None else "-",
                f"{health.bgp_peers_established}/{health.bgp_peers_total}"
                if health.bgp_peers_total is not None else "-",
                "; ".join(health.problems)[:110] if health.problems else "",
            )

        console.print(table)

        critical = sum(1 for h in results if h.status in ("CRITICAL", "UNREACHABLE"))
        warning = sum(1 for h in results if h.status == "WARNING")
        console.print(
            f"\n{len(results)} device(s): "
            f"[green]{len(results) - critical - warning} healthy[/green], "
            f"[yellow]{warning} degraded[/yellow], "
            f"[red]{critical} critical or unreachable[/red]\n"
        )

    if args.metrics:
        _write_metrics(results, args.metrics)

    return 1 if any(h.status in ("CRITICAL", "UNREACHABLE") for h in results) else 0


def _write_metrics(results: list[Health], path: Path) -> None:
    """Write Prometheus textfile metrics, atomically."""
    lines = [
        "# HELP network_device_up Whether the device answered the poll",
        "# TYPE network_device_up gauge",
    ]
    for health in results:
        labels = f'device="{health.hostname}",site="{health.site}",role="{health.role}"'
        lines.append(f"network_device_up{{{labels}}} {1 if health.reachable else 0}")

    lines += ["# HELP network_device_cpu_percent Five-minute CPU utilisation",
              "# TYPE network_device_cpu_percent gauge"]
    for health in results:
        if health.cpu_5min is not None:
            labels = f'device="{health.hostname}",site="{health.site}"'
            lines.append(f"network_device_cpu_percent{{{labels}}} {health.cpu_5min}")

    lines += ["# HELP network_device_memory_percent Memory utilisation",
              "# TYPE network_device_memory_percent gauge"]
    for health in results:
        if health.memory_used_pct is not None:
            labels = f'device="{health.hostname}",site="{health.site}"'
            lines.append(f"network_device_memory_percent{{{labels}}} {health.memory_used_pct}")

    lines += ["# HELP network_device_problems Number of health problems detected",
              "# TYPE network_device_problems gauge"]
    for health in results:
        labels = f'device="{health.hostname}",site="{health.site}"'
        lines.append(f"network_device_problems{{{labels}}} {len(health.problems)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.replace(path)


if __name__ == "__main__":
    sys.exit(main())
