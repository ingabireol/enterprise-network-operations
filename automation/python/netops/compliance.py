"""
Configuration compliance auditing.

Checks every device's running configuration against the security baseline in
``cisco/baselines/``, and produces both a terminal report and an HTML one.

The reason this exists rather than a periodic manual review: a network drifts.
Someone adds a temporary access-list entry during an incident, an engineer
enables a service to test something, a device is replaced from an old backup.
None of those is malicious and all of them are invisible until something goes
wrong. An audit that runs nightly turns each of them into a line in a report the
next morning.

Each rule states what it checks and why it matters, because a compliance report
that reads as a list of arbitrary demands gets ignored, and an ignored report is
worse than no report — it produces the paperwork of assurance without the
assurance.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from .connection import Credentials, Device, DeviceError, connect, send_command
from .inventory import load_inventory

log = logging.getLogger(__name__)
console = Console()


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}[self.value]


class Result(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "n/a"
    ERROR = "error"


@dataclass
class Rule:
    """
    One compliance rule.

    A rule either requires a pattern to be present (``must_match``) or requires
    it to be absent (``must_not_match``). Both accept a list, and all entries
    must be satisfied — a rule that passes when any one of several patterns
    matches is almost always a rule that has been quietly weakened.
    """

    id: str
    title: str
    severity: Severity
    rationale: str
    remediation: str
    platforms: list[str] = field(default_factory=lambda: ["ios", "iosxe", "nxos", "asa"])
    roles: list[str] = field(default_factory=list)   # empty = every role
    must_match: list[str] = field(default_factory=list)
    must_not_match: list[str] = field(default_factory=list)

    def applies_to(self, device: Device) -> bool:
        if device.platform not in self.platforms:
            return False
        if self.roles and device.role not in self.roles:
            return False
        return True

    def evaluate(self, config: str) -> tuple[Result, str]:
        """Return the result and, on failure, what specifically was wrong."""
        problems: list[str] = []

        for pattern in self.must_match:
            if not re.search(pattern, config, re.M | re.I):
                problems.append(f"required configuration not found: /{pattern}/")

        for pattern in self.must_not_match:
            matches = re.findall(pattern, config, re.M | re.I)
            if matches:
                sample = str(matches[0])[:80]
                problems.append(
                    f"prohibited configuration present: /{pattern}/"
                    + (f" (e.g. {sample!r})" if sample else "")
                )

        if problems:
            return Result.FAIL, "; ".join(problems)
        return Result.PASS, ""


@dataclass
class Finding:
    device: str
    site: str
    role: str
    rule: Rule
    result: Result
    detail: str = ""


def load_baseline(path: Path) -> list[Rule]:
    """Load the rule set from YAML."""
    data = yaml.safe_load(path.read_text())
    rules: list[Rule] = []

    for entry in data.get("rules", []):
        rules.append(
            Rule(
                id=entry["id"],
                title=entry["title"],
                severity=Severity(entry["severity"]),
                rationale=entry["rationale"],
                remediation=entry["remediation"],
                platforms=entry.get("platforms", ["ios", "iosxe", "nxos", "asa"]),
                roles=entry.get("roles", []),
                must_match=entry.get("must_match", []),
                must_not_match=entry.get("must_not_match", []),
            )
        )

    if not rules:
        raise ValueError(f"No rules loaded from {path}")

    log.info("Loaded %d rule(s) from %s", len(rules), path)
    return rules


def audit_device(
    device: Device,
    credentials: Credentials,
    rules: list[Rule],
    config_dir: Path | None = None,
) -> list[Finding]:
    """
    Audit one device.

    If ``config_dir`` is given, the most recent backup is audited instead of
    connecting — useful for auditing an estate offline, and for auditing a device
    that is currently unreachable.
    """
    findings: list[Finding] = []
    applicable = [r for r in rules if r.applies_to(device)]

    try:
        if config_dir:
            path = config_dir / device.site / f"{device.hostname}.cfg"
            if not path.exists():
                raise DeviceError(f"No backup at {path}")
            config = path.read_text()
        else:
            command = "show running-config"
            with connect(device, credentials, read_timeout_override=180) as conn:
                config = send_command(conn, command)

    except DeviceError as exc:
        # An unauditable device is a finding in its own right, not an omission.
        return [
            Finding(
                device=device.hostname, site=device.site, role=device.role,
                rule=rule, result=Result.ERROR, detail=str(exc),
            )
            for rule in applicable
        ]

    for rule in applicable:
        result, detail = rule.evaluate(config)
        findings.append(
            Finding(
                device=device.hostname, site=device.site, role=device.role,
                rule=rule, result=result, detail=detail,
            )
        )

    return findings


def render_html(findings: list[Finding], output: Path) -> None:
    """Write an HTML report. Self-contained, so it can be attached to an email."""
    failures = [f for f in findings if f.result == Result.FAIL]
    errors = [f for f in findings if f.result == Result.ERROR]
    passes = [f for f in findings if f.result == Result.PASS]

    by_severity: dict[Severity, list[Finding]] = {}
    for finding in failures:
        by_severity.setdefault(finding.rule.severity, []).append(finding)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = len(findings)
    rate = (len(passes) / total * 100) if total else 0.0

    rows: list[str] = []
    for severity in sorted(by_severity, key=lambda s: s.rank):
        for finding in sorted(by_severity[severity], key=lambda f: (f.site, f.device)):
            rows.append(
                "<tr>"
                f'<td><span class="sev sev-{html.escape(severity.value)}">'
                f"{html.escape(severity.value.upper())}</span></td>"
                f"<td>{html.escape(finding.device)}</td>"
                f"<td>{html.escape(finding.site)}</td>"
                f"<td><code>{html.escape(finding.rule.id)}</code></td>"
                f"<td>{html.escape(finding.rule.title)}</td>"
                f"<td>{html.escape(finding.detail)}</td>"
                f"<td>{html.escape(finding.rule.remediation)}</td>"
                "</tr>"
            )

    error_rows = "".join(
        f"<tr><td>{html.escape(f.device)}</td><td>{html.escape(f.detail)}</td></tr>"
        for f in {f.device: f for f in errors}.values()
    )

    # Light and dark are both defined from the same tokens, so the report is
    # readable whichever way it is opened.
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network compliance report</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #fcfcfb; --surface: #ffffff; --ink: #1a1a19; --muted: #5a5a57;
    --line: #e3e3e0;
    --critical: #b3261e; --high: #c2531f; --medium: #8a6a00; --low: #4a7fe8; --info: #5a5a57;
    --good: #10a17a;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1a1a19; --surface: #232321; --ink: #f0f0ee; --muted: #a0a09c;
      --line: #35352f;
      --critical: #e8615a; --high: #e08a4a; --medium: #d4b02a; --low: #7aa5f0; --info: #a0a09c;
      --good: #3ec79a;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 2rem 1rem; background: var(--bg); color: var(--ink);
         font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }}
  .wrap {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 .25rem; letter-spacing: -0.01em; }}
  .meta {{ color: var(--muted); margin-bottom: 2rem; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 1rem; margin-bottom: 2rem; }}
  .tile {{ background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
           padding: 1rem 1.1rem; }}
  .tile .n {{ font-size: 2rem; font-weight: 600; letter-spacing: -0.02em; }}
  .tile .l {{ color: var(--muted); font-size: .8rem; text-transform: uppercase;
              letter-spacing: .06em; margin-top: .15rem; }}
  .ok .n {{ color: var(--good); }}
  .bad .n {{ color: var(--critical); }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface);
           border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
  th {{ text-align: left; padding: .7rem .8rem; background: var(--bg);
        border-bottom: 1px solid var(--line); font-size: .78rem;
        text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }}
  td {{ padding: .7rem .8rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
  tr:last-child td {{ border-bottom: 0; }}
  code {{ font: 12px ui-monospace, SFMono-Regular, Menlo, monospace;
          background: var(--bg); padding: .1rem .35rem; border-radius: 4px; }}
  .sev {{ display: inline-block; padding: .12rem .45rem; border-radius: 4px;
          font-size: .7rem; font-weight: 700; letter-spacing: .04em; color: #fff; }}
  .sev-critical {{ background: var(--critical); }}
  .sev-high {{ background: var(--high); }}
  .sev-medium {{ background: var(--medium); }}
  .sev-low {{ background: var(--low); }}
  .sev-info {{ background: var(--info); }}
  h2 {{ font-size: 1.05rem; margin: 2rem 0 .75rem; }}
  .scroll {{ overflow-x: auto; }}
  .empty {{ padding: 2rem; text-align: center; color: var(--good); font-weight: 600;
            background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }}
</style></head><body><div class="wrap">
  <h1>Network compliance report</h1>
  <div class="meta">Generated {html.escape(generated)} · baseline: cisco/baselines/security-baseline.yml</div>

  <div class="tiles">
    <div class="tile"><div class="n">{len({f.device for f in findings})}</div><div class="l">Devices</div></div>
    <div class="tile"><div class="n">{total}</div><div class="l">Checks run</div></div>
    <div class="tile ok"><div class="n">{rate:.0f}%</div><div class="l">Passing</div></div>
    <div class="tile bad"><div class="n">{len(failures)}</div><div class="l">Failures</div></div>
    <div class="tile bad"><div class="n">{len(by_severity.get(Severity.CRITICAL, []))}</div><div class="l">Critical</div></div>
  </div>

  <h2>Failures</h2>
  <div class="scroll">
  {'<table><thead><tr><th>Severity</th><th>Device</th><th>Site</th><th>Rule</th><th>Title</th><th>Detail</th><th>Remediation</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>' if rows else '<div class="empty">Every applicable check passed on every device.</div>'}
  </div>

  {'<h2>Devices that could not be audited</h2><div class="scroll"><table><thead><tr><th>Device</th><th>Reason</th></tr></thead><tbody>' + error_rows + '</tbody></table></div>' if error_rows else ''}

</div></body></html>"""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document)
    log.info("HTML report written to %s", output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit device configurations against the baseline.")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--site", default="all")
    parser.add_argument("--from-backups", type=Path,
                        help="Audit the stored backups instead of connecting to devices")
    parser.add_argument("--report", type=Path, help="Write an HTML report here")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--fail-on", default="high",
                        choices=[s.value for s in Severity],
                        help="Exit non-zero if any failure at or above this severity")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )

    devices = load_inventory(args.inventory, args.site)
    rules = load_baseline(args.baseline)

    credentials = Credentials("offline", "offline")
    if not args.from_backups:
        credentials = Credentials.from_environment()

    findings: list[Finding] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(audit_device, d, credentials, rules, args.from_backups)
            for d in devices
        ]
        for future in as_completed(futures):
            findings.extend(future.result())

    failures = [f for f in findings if f.result == Result.FAIL]
    errors = [f for f in findings if f.result == Result.ERROR]
    passes = [f for f in findings if f.result == Result.PASS]

    table = Table(title=f"Compliance failures — {args.site}")
    for column in ("Severity", "Device", "Rule", "Title", "Detail"):
        table.add_column(column, overflow="fold")

    severity_colour = {
        Severity.CRITICAL: "red", Severity.HIGH: "red",
        Severity.MEDIUM: "yellow", Severity.LOW: "blue", Severity.INFO: "dim",
    }

    for finding in sorted(failures, key=lambda f: (f.rule.severity.rank, f.device)):
        colour = severity_colour[finding.rule.severity]
        table.add_row(
            f"[{colour}]{finding.rule.severity.value.upper()}[/{colour}]",
            finding.device,
            finding.rule.id,
            finding.rule.title,
            finding.detail[:90],
        )

    if failures:
        console.print(table)
    else:
        console.print("\n[green]Every applicable check passed on every device.[/green]")

    if errors:
        console.print("\n[red]Devices that could not be audited:[/red]")
        for device_name in sorted({f.device for f in errors}):
            detail = next(f.detail for f in errors if f.device == device_name)
            console.print(f"  [red]{device_name}[/red]: {detail}")

    total = len(findings)
    console.print(
        f"\n{len({f.device for f in findings})} device(s), {total} check(s): "
        f"[green]{len(passes)} passed[/green], "
        f"[red]{len(failures)} failed[/red]"
        + (f", [red]{len({f.device for f in errors})} unauditable[/red]" if errors else "")
        + "\n"
    )

    if args.report:
        render_html(findings, args.report)
        console.print(f"HTML report: {args.report}\n")

    threshold = Severity(args.fail_on).rank
    blocking = [f for f in failures if f.rule.severity.rank <= threshold]
    if blocking or errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
