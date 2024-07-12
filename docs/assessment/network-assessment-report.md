# Network Assessment Report

**Scope:** Campus, data centre, WAN and security layers supporting an enterprise
application platform
**Method:** Configuration capture, live inspection, flow analysis, log review,
interviews, and a controlled failover test
**Status:** Reference document — fictional environment, real methodology

---

## 1. Purpose

Before any of the automation in this repository is applied to a network, the
network has to be understood as it actually is rather than as documented. This is
the template that assessment follows and the shape its findings take.

Five questions:

1. What devices exist, running what, and does the register agree?
2. Where is the network one failure away from an outage?
3. Does the segmentation actually segment anything?
4. What would a failover actually do, tested rather than described?
5. Which gaps are worth closing first?

Nothing here relies on being told something works. Where a claim mattered, it was
tested.

---

## 2. Method

| Activity | Evidence |
| --- | --- |
| Configuration capture, every device | `netops.backup`, normalised and redacted |
| Register reconciliation | `netops.inventory --verify` |
| Baseline audit | `netops.compliance` against `cisco/baselines/` |
| Topology discovery | `netops.topology` from CDP/LLDP |
| Interface error survey | `netops.interfaces --errors-only` |
| Flow analysis, 7 days | nfdump, focused on cross-segment traffic |
| Log review, 30 days | Syslog, focused on link, routing and authentication events |
| Controlled failover test | WAN, firewall and HSRP, in a maintenance window |

---

## 3. Findings

### N-01 — Segmentation is designed but not enforced · **CRITICAL**

**Observed.** The design document describes separate application, database and
user segments. In the running configuration, the segments exist as VLANs but the
inter-VLAN access lists permit `ip any any`. Flow analysis over seven days found
2,400 flows from workstations in the user VLAN directly to the database VLAN on
port 5432, and 180 flows from the database segment to workstations.

**Why it matters.** This is the single most consequential finding. The segmentation
is the control that limits the blast radius of a compromise, and it is the control
that every risk assessment of the platform assumes is present. A workstation
compromise currently yields direct network access to the financial database. The
VLANs give the appearance of segmentation without any of the effect, which is worse
than having none, because it is believed.

**Recommendation.** Enforce the documented policy on the firewall, with switch
access lists as a second, independent control. Start in log-only mode for a week to
find the flows that are legitimate but undocumented — there are always some, and
discovering them by breaking them is the wrong way. The policy is in
`cisco/templates/firewall-asa.j2` and `cisco/templates/nexus-datacenter.j2`; the
lab validation suite asserts both the permits and the denies.

---

### N-02 — SNMPv2c with a default community string · **CRITICAL**

**Observed.** Every device is configured with `snmp-server community public RO`
and, on four devices, `private RW`. The read-write community was reachable from the
user VLAN.

**Why it matters.** A read community string is a clear-text password that grants
the complete topology, routing table, interface list and ARP cache — a finished
map for anyone planning lateral movement. A **write** community allows
configuration changes, including downloading the running configuration to an
arbitrary TFTP server. The string `private` is the vendor default.

**Recommendation.** Remove all community strings. SNMPv3 with authPriv and an
access list restricting it to the monitoring stations. This is a same-day change,
not a project.

---

### N-03 — Telnet enabled on 11 of 15 devices · **CRITICAL**

**Observed.** `transport input all` on the VTY lines of eleven devices. Log review
found 340 successful telnet sessions in 30 days, from the user VLAN.

**Why it matters.** Telnet transmits administrative credentials in clear text.
Anyone with a span port, a compromised host on the path, or the ability to ARP
spoof — which the absence of dynamic ARP inspection (N-07) makes trivial — obtains
administrative credentials for the network.

**Recommendation.** `transport input ssh` everywhere, with SSH version 2 and modern
ciphers. Verify by attempting a telnet connection afterwards rather than by reading
the configuration.

---

### N-04 — No configuration backup or change control · **HIGH**

**Observed.** No device configuration has been archived. Changes are made directly
on devices. There is no record of what changed, when, or by whom. Two devices carry
configuration that nobody present could explain.

**Why it matters.** A failed device cannot be replaced without its configuration; a
device rebuilt from memory is a device that differs from the one it replaced. And
without a change record, every incident investigation starts by establishing what
changed, which is the slowest possible start.

**Recommendation.** Nightly automated backup into git, with a drift report each
morning — `automation/python/netops/backup.py` and `automation/bash/nightly-backup.sh`.
Then command accounting through TACACS+, so each change is attributable.

---

### N-05 — Spanning tree root is elected by accident · **HIGH**

**Observed.** No bridge priority is configured anywhere. The root bridge is an
access switch in a third-floor comms room, elected by having the lowest MAC address.
All inter-VLAN traffic transits it.

**Why it matters.** The layer 2 topology is an accident rather than a design, and
it will change the next time a switch is replaced. Traffic between servers in the
data centre is currently routed through an access switch three floors away — which
explains an unresolved latency complaint the team had attributed to the application.

**Recommendation.** Set priority explicitly: core as root, secondary core as backup
root, access switches at 61440 so they can never become root. Add root guard on the
core's downlinks.

---

### N-06 — No redundancy at the WAN edge · **HIGH**

**Observed.** A single WAN router with a single provider circuit. The provider has
had three outages in twelve months, each between two and six hours, during which
both sites were isolated from each other and from the internet.

**Why it matters.** Database replication to the DR site stops during these outages,
so the recovery point objective is breached for the whole duration — and the DR
site is exactly what would be needed if the outage were something worse than a
provider fault.

**Recommendation.** A second provider with BGP path control, and a second router if
budget allows. Dual-homing to one router removes the circuit as a single point of
failure; a second router removes the device too.

---

### N-07 — No layer 2 protections on access ports · **HIGH**

**Observed.** No DHCP snooping, no dynamic ARP inspection, no port security, no
BPDU guard. 240 of 384 access ports are administratively up with nothing connected.

**Why it matters.** Each of these is a distinct, easy attack: a rogue DHCP server
becomes the default gateway and sees everything leaving the subnet; ARP spoofing
gives an on-path position with no privileges beyond a port; a switch plugged in
under a desk can become the spanning tree root. The open ports mean any of them
needs only physical access to a socket.

**Recommendation.** DHCP snooping first, because dynamic ARP inspection and IP
source guard both depend on the binding table it builds. Then port security with a
restrict violation action, BPDU guard by default, and shut every unused port into
the parking VLAN.

---

### N-08 — Unauthenticated routing protocols · **MEDIUM**

**Observed.** OSPF runs without authentication across all areas. The process is
active on user-facing SVIs, so a host on a user VLAN can form an adjacency.

**Why it matters.** An unauthenticated OSPF process accepts routes from anything
that can send a hello on the segment. Injecting a more specific route redirects
traffic through an attacker's host, and the routing table looks entirely normal
afterwards.

**Recommendation.** Message-digest authentication on every area, and
`passive-interface default` with explicit exceptions — hosts have no business
receiving OSPF hellos at all.

---

### N-09 — Interface errors across the access layer · **MEDIUM**

**Observed.** 34 interfaces accumulating CRC errors, the worst at 2,400 per hour.
Six ports at half duplex on 1 Gb links. Users on two of those ports had open
tickets about application slowness, attributed to the application.

**Why it matters.** This is how interface errors present. They do not take a link
down — they corrupt a fraction of frames, TCP retransmits, and the user reports
that the system is slow. Weeks get spent looking at the application.

**Recommendation.** Replace the optics and cables showing CRC errors; set speed and
duplex explicitly on both ends of the mismatched links. Then alert on the error
rate so the next one is found in hours rather than months.

---

### N-10 — No monitoring of the network itself · **MEDIUM**

**Observed.** No SNMP polling, no syslog collection, no flow data. Device failures
are discovered when users report them. Three devices had failed power supplies that
nobody knew about.

**Why it matters.** The failed power supplies are the illustrative case: nothing
was down, redundancy was silently gone, and the next failure would have been an
outage that appeared to come from nowhere.

**Recommendation.** SNMPv3 polling into Prometheus, syslog to a central collector,
flow export from the WAN routers. The configuration is in `monitoring/`.

---

### N-11 — Software versions span four releases · **LOW**

**Observed.** Four distinct IOS-XE versions across nine switches; the oldest is
past its end of software maintenance date.

**Why it matters.** Every additional version is another set of advisories to
assess and another set of behaviours to remember during an incident. A version past
end of maintenance receives no security fixes at all.

**Recommendation.** Standardise on one release per platform family. Upgrade the
unsupported devices first.

---

## 4. Gap register

| ID | Finding | Severity | Effort | Sequence |
| --- | --- | --- | --- | --- |
| N-02 | SNMP community strings | Critical | Low | 1 |
| N-03 | Telnet enabled | Critical | Low | 1 |
| N-01 | Segmentation not enforced | Critical | High | 3 |
| N-04 | No configuration backup | High | Low | 1 |
| N-07 | No layer 2 protections | High | Medium | 3 |
| N-05 | Spanning tree root by accident | High | Low | 2 |
| N-06 | No WAN redundancy | High | High | 4 |
| N-10 | No monitoring | Medium | Medium | 2 |
| N-08 | Unauthenticated routing | Medium | Medium | 3 |
| N-09 | Interface errors | Medium | Low | 2 |
| N-11 | Version spread | Low | Medium | 5 |

Sequencing follows one rule: **each step makes the next one safer.** Configuration
backup comes first despite being low severity, because every subsequent change is
riskier without a rollback point. Monitoring comes before the segmentation work,
because enforcing a policy you cannot observe is how a remediation becomes an
incident.

---

## 5. Recommended sequence

**Phase 1 — Stop the bleeding (week 1).** Remove telnet and SNMP community
strings. Stand up configuration backup. All three are same-day changes with no
service impact, and together they close the two critical exposures that need no
skill to exploit.

**Phase 2 — See the network (weeks 2–4).** SNMP polling, syslog collection, flow
export. Fix the spanning tree root and the interface errors, both of which are
quick wins that visibly improve things for users.

**Phase 3 — Enforce the design (weeks 4–10).** Segmentation, in log-only mode
first. Layer 2 protections, DHCP snooping before the controls that depend on it.
Routing authentication. This is the largest phase and it is deliberately not first:
it is the one most likely to break something, and it needs Phase 2's visibility.

**Phase 4 — Remove the single points of failure (weeks 8–16).** Second WAN
provider, BGP path control, tested failover.

**Phase 5 — Lifecycle (ongoing).** Version standardisation, then a regular patch
cycle so that this never accumulates again.

---

## 6. Testing performed

| Test | Result |
| --- | --- |
| Telnet from the user VLAN to a core switch | **Succeeded** — confirms N-03 |
| SNMP walk with `public` from the user VLAN | **Succeeded** — full topology retrieved, confirms N-02 |
| TCP connection from a workstation to the database on 5432 | **Succeeded** — confirms N-01 |
| WAN failover | **Not possible** — single circuit, confirms N-06 |
| Firewall failover | **Succeeded**, 4 s, sessions preserved |
| HSRP failover | **Succeeded**, 9 s — default timers, slower than the application tolerates |
| Core switch reload | **Succeeded**, 45 s reconvergence — spanning tree, as predicted by N-05 |

The first three tests were performed with written authorisation, from a controlled
host, and are the evidence behind the three critical findings. A finding that says
"this appears possible" is an opinion; one that says "this was done, at this time,
from this host" is a fact.

---

## 7. Out of scope

- Application and server configuration — see the companion
  [`enterprise-java-platform-automation`](../../../enterprise-java-platform-automation) repository
- Wireless infrastructure
- Physical security of the comms rooms
- Provider contracts and service levels
- Penetration testing beyond the confirmatory tests listed above
