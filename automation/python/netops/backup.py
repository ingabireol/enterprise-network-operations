"""
Configuration backup.

Retrieves the running configuration from every device, normalises it, and stores
it in a git-tracked directory so that the history of the network's configuration
is a commit log rather than a folder of dated files nobody diffs.

Two details that matter more than they look:

* **Normalisation.** Device output contains lines that change on every read —
  uptime in a comment, NVRAM checksums, certificate blobs. Left in, every backup
  produces a diff and the diffs become meaningless. They are stripped, and what
  is stripped is documented below.
* **Secrets.** Encrypted keys and hashes are replaced with a placeholder before
  the file is written. The backup's job is to record structure and intent, not
  to become a second, less protected copy of every credential in the estate.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from .connection import Credentials, Device, DeviceError, connect, send_command
from .inventory import load_inventory

log = logging.getLogger(__name__)
console = Console()

RUNNING_CONFIG_COMMANDS = {
    "ios": "show running-config",
    "iosxe": "show running-config",
    "nxos": "show running-config",
    "asa": "show running-config",
    "iosxr": "show running-config",
}

# Lines that change between reads without the configuration having changed.
# Left in place, they turn every nightly backup into a diff and train everyone
# to ignore the diffs.
VOLATILE_PATTERNS = [
    re.compile(r"^! Last configuration change at .*$", re.M),
    re.compile(r"^! NVRAM config last updated at .*$", re.M),
    re.compile(r"^! Time: .*$", re.M),
    re.compile(r"^Current configuration : \d+ bytes$", re.M),
    re.compile(r"^Building configuration\.\.\..*$", re.M),
    re.compile(r"^ntp clock-period \d+$", re.M),
    re.compile(r"^: Written by \S+ at .*$", re.M),
    re.compile(r"^: Hardware:.*$", re.M),
    re.compile(r"^:Written by .*$", re.M),
]

# Secret material, redacted before anything is written to disk.
SECRET_PATTERNS = [
    # Covers `username <x> secret 9 <hash>`, `enable secret 9 <hash>` and the bare
    # `password 7 <value>` form. The leading keyword is optional-but-enumerated
    # rather than `.*`, so the pattern cannot accidentally swallow a whole line.
    (re.compile(r"^(\s*(?:username \S+ |enable )?(?:secret|password) \d+ )\S+", re.M),
     r"\1<REDACTED>"),
    (re.compile(r"^(\s*key 7 )\S+", re.M), r"\1<REDACTED>"),
    (re.compile(r"^(\s*neighbor \S+ password 7 )\S+", re.M), r"\1<REDACTED>"),
    (re.compile(r"^(\s*standby \d+ authentication md5 key-string 7 )\S+", re.M), r"\1<REDACTED>"),
    (re.compile(r"^(\s*ntp authentication-key \d+ md5 )\S+", re.M), r"\1<REDACTED>"),
    (re.compile(r"^(snmp-server user \S+ \S+ v3 auth sha )\S+( priv aes \d+ )\S+", re.M),
     r"\1<REDACTED>\2<REDACTED>"),
    (re.compile(r"^(\s*pre-shared-key (?:local|remote) )\S+", re.M), r"\1<REDACTED>"),
    (re.compile(r"^(\s*failover key )\S+", re.M), r"\1<REDACTED>"),
    # Certificate and key blobs, removed entirely rather than redacted line by line.
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S), "<CERTIFICATE REDACTED>"),
]


def normalise(config: str) -> str:
    """Strip volatile lines and redact secrets. Idempotent."""
    for pattern in VOLATILE_PATTERNS:
        config = pattern.sub("", config)

    for pattern, replacement in SECRET_PATTERNS:
        config = pattern.sub(replacement, config)

    # Collapse the blank runs left behind by the removals, and normalise line endings.
    config = re.sub(r"\n{3,}", "\n\n", config)
    config = config.replace("\r\n", "\n").rstrip() + "\n"
    return config


def backup_device(
    device: Device,
    credentials: Credentials,
    output_dir: Path,
) -> dict[str, object]:
    """Retrieve, normalise and write one device's configuration."""
    result: dict[str, object] = {
        "hostname": device.hostname,
        "site": device.site,
        "success": False,
        "changed": False,
        "bytes": 0,
    }

    command = RUNNING_CONFIG_COMMANDS.get(device.platform, "show running-config")

    try:
        with connect(device, credentials, read_timeout_override=180) as conn:
            raw = send_command(conn, command)

        if len(raw) < 200:
            raise DeviceError(
                f"{device.hostname}: configuration is only {len(raw)} bytes — "
                "almost certainly a truncated read rather than a small device. "
                "Refusing to overwrite the previous backup with it."
            )

        config = normalise(raw)

        site_dir = output_dir / device.site
        site_dir.mkdir(parents=True, exist_ok=True)
        target = site_dir / f"{device.hostname}.cfg"

        previous = target.read_text() if target.exists() else ""
        changed = previous != config

        header = (
            f"! Backup of {device.hostname} ({device.address})\n"
            f"! Platform: {device.platform}  Role: {device.role}  Site: {device.site}\n"
            f"! Retrieved: {datetime.now(timezone.utc).isoformat()}\n"
            f"! Normalised and redacted by netops.backup — see the module docstring\n"
            f"!\n"
        )
        target.write_text(header + config)

        result.update(success=True, changed=changed, bytes=len(config))
        log.info("%s: %d bytes%s", device.hostname, len(config), " (changed)" if changed else "")

    except DeviceError as exc:
        result["error"] = str(exc)
        log.error("%s: %s", device.hostname, exc)

    return result


def commit_backups(output_dir: Path, message: str) -> bool:
    """
    Commit the backups, so that the configuration history is a git log.

    Returns False when there is nothing to commit, which is the normal case on a
    quiet night and is not an error.
    """
    try:
        status = subprocess.run(
            ["git", "-C", str(output_dir), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        if not status.stdout.strip():
            log.info("No configuration changes to commit")
            return False

        subprocess.run(["git", "-C", str(output_dir), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(output_dir), "commit", "-m", message],
            check=True, capture_output=True,
        )
        log.info("Committed configuration changes")
        return True

    except subprocess.CalledProcessError as exc:
        log.error("git commit failed: %s", exc.stderr if hasattr(exc, "stderr") else exc)
        return False
    except FileNotFoundError:
        log.warning("git is not available; backups written but not committed")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up device configurations.")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--site", default="all")
    parser.add_argument("--output", type=Path, default=Path("backups"))
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--commit", action="store_true", help="git-commit the results")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )

    devices = load_inventory(args.inventory, args.site)
    credentials = Credentials.from_environment()
    args.output.mkdir(parents=True, exist_ok=True)

    console.print(f"Backing up {len(devices)} device(s) to {args.output}/\n")

    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(backup_device, d, credentials, args.output) for d in devices]
        for future in as_completed(futures):
            results.append(future.result())

    succeeded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    changed = [r for r in succeeded if r["changed"]]

    console.print(f"\n[green]{len(succeeded)} succeeded[/green]", end="")
    if changed:
        console.print(f", [yellow]{len(changed)} changed[/yellow]", end="")
    if failed:
        console.print(f", [red]{len(failed)} failed[/red]")
        for result in failed:
            console.print(f"  [red]{result['hostname']}[/red]: {result.get('error', 'unknown')}")
    else:
        console.print("")

    if changed:
        console.print("\nChanged since the previous backup:")
        for result in sorted(changed, key=lambda r: str(r["hostname"])):
            console.print(f"  {result['hostname']}")
        console.print("\nReview with: make diff")

    if args.commit:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        commit_backups(
            args.output,
            f"Configuration backup {timestamp} — {len(changed)} device(s) changed",
        )

    # Non-zero if anything failed: a backup run that half worked must not report
    # success to whatever scheduled it.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
