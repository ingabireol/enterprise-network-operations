# Network architecture

Two sites, dual-homed WAN, segmented data centre. The design decisions that were
genuine choices are set out with their alternatives, because the reasoning is the
part that stops being obvious after eighteen months.

---

## Physical topology

```
                    Provider A (AS 64512)      Provider B (AS 64513)
                            │                          │
              ┌─────────────┼──────────────────────────┼─────────────┐
              │             │                          │             │
        ┌─────▼─────┐ ┌─────▼─────┐              ┌─────▼─────┐ ┌─────▼─────┐
        │ wan-dc1-01│ │           │              │           │ │ wan-dc2-01│
        │  ISR 4451 │ │           │              │           │ │  ISR 4451 │
        └─────┬─────┘ └───────────┘              └───────────┘ └─────┬─────┘
              │            ═══ IPsec over both providers ═══         │
        ┌─────▼─────────────┐                        ┌───────────────▼─────┐
        │  fw-dc1-01/02     │                        │  fw-dc2-01          │
        │  ASA active/stby  │                        │  ASA                │
        └─────┬─────────────┘                        └───────────────┬─────┘
              │                                                      │
    ┌─────────▼──────────┐                              ┌────────────▼────────┐
    │ core-dc1-01/02     │                              │ core-dc2-01         │
    │ C9500 StackWise    │                              │ C9500               │
    │ Virtual pair       │                              │                     │
    └──┬──────────────┬──┘                              └──┬───────────────┬──┘
       │              │                                    │               │
┌──────▼─────┐  ┌─────▼──────────┐                  ┌──────▼─────┐  ┌──────▼──────┐
│ acc-dc1-*  │  │ nex-dc1-01/02  │                  │ acc-dc2-01 │  │ nex-dc2-01  │
│ C9300      │  │ N9K vPC pair   │                  │ C9300      │  │ N9K         │
│ access     │  │ data centre    │                  │ access     │  │ data centre │
└────────────┘  └───────┬────────┘                  └────────────┘  └──────┬──────┘
                        │                                                  │
              ┌─────────┴──────────┐                            ┌──────────┴───────┐
              │ App, DB, backup,   │                            │ DR database,     │
              │ monitoring servers │                            │ DR app tier      │
              └────────────────────┘                            └──────────────────┘
```

---

## Layer choices

### StackWise Virtual for the core, not VSS or a plain pair

Two physical C9500s presenting as one logical switch. The access switches dual-home
into what they see as a single device, so both uplinks forward — no spanning tree
blocking half the capacity — and losing a core switch is a link event rather than a
topology change.

**Alternative considered: two independent cores with spanning tree.** Simpler to
reason about and one fewer proprietary feature. Rejected because half the uplink
capacity sits idle waiting for a failure, and because spanning tree reconvergence
is measured in seconds where a link-aggregation failover is measured in
milliseconds. On a platform with financial transactions in flight, that difference
is visible to users.

### vPC in the data centre, not a stack

The Nexus pair stays as two control planes with vPC providing the multi-chassis
port channel. The servers see one logical switch and can be patched around.

**Why not stack them like the core?** Because a stack has one control plane, so a
software upgrade takes both switches. vPC allows one switch to be upgraded while
the other carries the servers — which is exactly the case the data centre pair
exists for.

The vPC peer keepalive deliberately runs over the management network, physically
separate from the peer link. If both fail together the switches cannot distinguish
a peer failure from a link failure, and that is how a split brain starts.

### OSPF inside, BGP between

OSPF within each site: fast convergence, minimal configuration, and it reacts to
a link failure without anyone's involvement. BGP between the sites and to the
providers: policy control, prefix filtering, and the ability to influence path
selection deliberately.

**Why not one protocol throughout?** Because they are good at different things.
BGP inside a site converges too slowly for a link failure. OSPF between sites gives
no policy control — every route is equal and the only lever is cost, which is a
blunt instrument for expressing "prefer provider A unless it is degraded".

Keeping them separate also means an internal topology change cannot leak into WAN
policy, which is a failure mode that produces a very confusing morning.

### Two providers, BGP-controlled preference

Provider A is preferred by local preference inbound and by AS-path prepending
outbound. Provider B carries traffic when A is down, and the switch is automatic.

The prepending matters and is often forgotten: without it, inbound traffic arrives
over whichever path the wider internet happens to prefer, which may not be the one
you are sending over. Asymmetric routing then breaks the stateful firewall in ways
that look like an application fault.

### Firewalls between segments, ACLs as defence in depth

The segmentation policy is enforced on the ASA pair, where it is stateful,
loggable and centrally managed. The Nexus access lists enforce the same boundaries
at the switch.

**This is deliberate duplication.** The switch ACL is not a substitute for the
firewall — it is stateless and coarser. It is there so that a firewall
misconfiguration, or a path that bypasses the firewall, does not silently open the
database segment to the whole estate. Two independent controls that must both fail.

---

## Segmentation

The design principle: **segment by trust, not by convenience.** A segment boundary
exists where the consequence of a compromise changes, not where the cabling was
easy.

| Segment | VLAN | Network | Reaches | Reached by |
| --- | --- | --- | --- | --- |
| Management | 1 | 10.30.1.0/24 | Everything (SSH, SNMP) | Jump hosts only |
| User | 10 | 10.30.10.0/24 | Platform VIP (443), DNS, NTP, internet | — |
| Application | 20 | 10.30.20.0/24 | Database (5432), LDAP, partners (443) | Load balancer |
| Database | 30 | 10.30.30.0/24 | Its replica, DR replica, syslog, NTP | Application, backup, monitoring |
| Monitoring | 40 | 10.30.40.0/24 | Everything (read-only ports) | Jump hosts |
| Backup | 50 | 10.30.50.0/24 | Database, application | Jump hosts |
| Parking | 999 | unrouted | Nothing | Nothing |

The rule that does most of the work: **the database segment accepts connections
from the application segment on one port and from the backup and monitoring hosts,
and from nothing else — and it initiates almost nothing.** An application-tier
compromise then yields database access on a single protocol, not a foothold in
the data centre.

The parking VLAN is where every unused access port lives, shut down. A port that is
merely unconfigured is an open connection into whatever VLAN 1 reaches.

---

## High availability

| Layer | Mechanism | Failover time | Exercised by |
| --- | --- | --- | --- |
| Core switch | StackWise Virtual | Sub-second | Patch cycle |
| Data centre switch | vPC | Sub-second | Patch cycle |
| Gateway | HSRPv2, sub-second timers | ~750 ms | `lab/containerlab/validate.sh` |
| Access uplink | LACP port channel, dual-homed | Sub-second | Patch cycle |
| Firewall | ASA active/standby, stateful | 3–5 s | Quarterly |
| WAN | Dual provider, BGP | 15–30 s | Quarterly, `validate.sh` |
| Site | Manual DR failover | Up to 2 hours | Quarterly drill |

Everything down to the WAN row recovers without human involvement. The site row
does not, and that is a decision rather than an omission — see
[ADR 0003](../../../enterprise-java-platform-automation/docs/adr/0003-streaming-replication-for-dr.md)
in the platform repository for why automatic cross-site failover was rejected.

**HSRP timers.** Sub-second hellos (250 ms / 750 ms) rather than the defaults
(3 s / 10 s). The default means ten seconds without a gateway on every failover,
which is long enough for application connections to time out and for users to
notice. The cost is more control-plane traffic, which the CoPP policy accounts for.

---

## Quality of service

The WAN is the scarce resource, so that is where QoS actually decides anything.

| Class | DSCP | WAN share | Contents |
| --- | --- | --- | --- |
| Voice | EF | 15% priority | Real-time audio |
| Critical | AF31 | 35% guaranteed | The financial platform's own traffic |
| Replication | — (ACL-matched) | 25% guaranteed | Database replication to DR |
| Bulk | CS1 | 5% | Backups, file transfers |
| Default | — | 15% + fair queue | Everything else |

**Replication is guaranteed but not prioritised.** A replica a few seconds behind
is survivable; a platform nobody can use is not. But replication is also what the
recovery point objective depends on, so it gets a floor that a backup job cannot
push it below.

Marking happens at the access edge, not at the endpoint. Trusting a workstation's
own DSCP marking lets any workstation claim priority for itself.

---

## Management plane

Out-of-band management VRF on every device, reachable only from the jump hosts.
TACACS+ for authentication with a documented local break-glass account whose use
raises an alert — a successful local login means TACACS+ is unreachable, which is
itself worth knowing.

SNMPv3 with authPriv only. No community strings anywhere: a community string is a
password sent in clear text, and read access to a device's MIB tree gives away the
topology, the routing table, the interface list and the ARP cache.

Syslog over TCP 601 rather than UDP 514. UDP syslog drops silently under load, and
the load spikes exactly when something is going wrong — losing the record of an
event because of the event is the worst failure mode a logging system can have.

---

## What this design does not do

Stated plainly, because a design document that implies more than it delivers is
worse than a modest one:

- **No automatic cross-site failover.** Deliberate: without a third site or a
  cloud witness there is no way to distinguish a site failure from a network
  partition, and getting that wrong produces two live primaries.
- **No micro-segmentation within a segment.** Hosts within the application segment
  can reach each other freely. Closing that would need host-level policy, which is
  a platform-repository concern.
- **No SD-WAN.** Two providers with BGP is sufficient at this scale and has far
  fewer moving parts. Worth revisiting at more than four sites.
- **IPv6 is not deployed.** The addressing plan reserves space for it; nothing
  else is in place.
