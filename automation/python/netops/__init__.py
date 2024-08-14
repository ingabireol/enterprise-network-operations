"""
netops — network operations toolkit.

A small set of tools for the recurring jobs on a Cisco estate: back up
configurations, audit them against a baseline, show what changed, poll health,
reconcile the inventory, and map the topology.

Design constraints, applied throughout:

* **Read-only by default.** Nothing in this package changes a device
  configuration. Change is the job of the Ansible playbooks, where it goes
  through review and check mode first.
* **Fail visibly, never silently.** A device that cannot be reached is reported
  as unreachable, not omitted from the results. A report with a quiet gap in it
  is worse than no report.
* **Credentials never touch the source.** They come from the environment or from
  Ansible Vault, and the tools refuse to start without them rather than falling
  back to a default.
* **Every tool is usable by hand.** These run on a schedule, but they are also
  what an engineer reaches for at 02:00, so the human-readable output matters as
  much as the JSON.
"""

__version__ = "1.3.0"
__all__ = ["inventory", "backup", "compliance", "diff", "health", "interfaces", "topology"]
