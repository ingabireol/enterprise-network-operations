# Enterprise Network Operations

Network automation, configuration management, compliance auditing and
observability for a **two-site enterprise network** carrying a mission-critical
application platform — the campus, data centre, WAN and security layers that an
IFMIS / ERP / institutional service-delivery system depends on.

| Layer | Platform |
| --- | --- |
| Core / distribution | Cisco Catalyst 9500 (StackWise Virtual pair) |
| Access | Cisco Catalyst 9300 (stacked) |
| Data centre | Cisco Nexus 9300 (vPC pair) |
| WAN edge | Cisco ISR 4451 / Catalyst 8300 |
| Firewall | Cisco ASA 5555-X / Firepower (active/standby) |
| Automation | Python (Netmiko, NAPALM, Nornir), Ansible `cisco.ios` / `cisco.nxos` |
| Observability | SNMP + syslog + NetFlow → Prometheus, Loki, Grafana |
| Lab | containerlab topology reproducing the design for testing |

> This repository is **portfolio / reference** work. Every hostname, IP address,
> ASN, community string and credential in it is fictitious, and the running
> configurations are sanitised reference designs. Nothing here contains or
> discloses the configuration of a real production network.

---

So the structure follows how that work actually happens:

1. **Assess** what is there → [`docs/assessment/`](docs/assessment/)
2. **Design** the segmentation and addressing → [`docs/design/`](docs/design/)
3. **Template** the configuration → [`cisco/templates/`](cisco/templates/)
4. **Automate** the repetitive parts → [`automation/`](automation/)
5. **Audit** continuously against the standard → [`automation/python/netops/compliance.py`](automation/python/netops/compliance.py)
6. **Watch** it → [`monitoring/`](monitoring/)
7. **Troubleshoot** it methodically → [`docs/runbooks/`](docs/runbooks/)
8. **Test changes before they reach production** → [`lab/`](lab/)

---

## Repository map

```
cisco/
  templates/            Jinja2 templates: core, access, WAN, Nexus, ASA
  golden-configs/       The reference configuration each device is audited against
  baselines/            Security and operational standards, as machine-readable rules

automation/
  python/netops/        backup, compliance, inventory, health, diff, interface reporting
  ansible/              Playbooks and roles for cisco.ios / cisco.nxos
  bash/                 Small wrappers for the scheduled jobs

monitoring/
  prometheus/           SNMP exporter targets + alert rules
  snmp/                 snmp_exporter generator config for Cisco MIBs
  syslog/               rsyslog routing, severity handling, promtail pipeline
  netflow/              Flow collector configuration and top-talker reporting
  grafana/dashboards/   Network overview, WAN health, interface errors

docs/
  design/               Segmentation, addressing, routing, QoS, high availability
  assessment/           Network assessment report and gap register
  runbooks/             Outage triage, switch onboarding, firewall change, VPN, WAN failover

lab/
  containerlab/         Reproducible topology for validating changes before production
```

---

## Quick start

```bash
# Install the Python tooling
make deps

# Inventory: what is actually out there, and does it match the register?
make inventory

# Back up every device configuration (scheduled nightly in production)
make backup

# Audit every device against the security baseline
make compliance

# What changed since the last backup?
make diff

# Health poll across the estate
make health
```

Every target is a thin wrapper over an explicit command — run `make help`, or
read the [`Makefile`](Makefile).

---

## Design summary

**Two sites.** A primary data centre and a secondary site that also serves as the
disaster recovery location, connected by dual WAN links from different providers.

**Segmentation by trust, not by convenience.** The application, database and
management tiers are separate VLANs in separate VRFs where appropriate, with the
firewall — not an access list on a switch — enforcing the policy between them.
The database segment accepts connections from the application segment and from
nothing else.

**Routing.** OSPF inside each site for speed of convergence, BGP between sites
and to the providers for policy control. The two are deliberately separated: an
internal topology change should never leak into WAN policy.

**High availability at every layer that can fail.** StackWise Virtual for the
core, vPC in the data centre, HSRP for gateway redundancy, active/standby
firewalls, dual WAN with BGP-controlled failover.

Full detail in [`docs/design/`](docs/design/).

---

## Related repository

The platform that runs on this network — servers, middleware, application
deployment, backup and disaster recovery — is in the companion repository
**[`enterprise-java-platform-automation`](../enterprise-java-platform-automation)**.

## Licence

[MIT](LICENSE) — reuse freely.
