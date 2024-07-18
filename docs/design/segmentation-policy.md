# Segmentation policy

What may talk to what, why, and how it is enforced.

**Principle: segment by trust, not by convenience.** A boundary exists where the
consequence of a compromise changes, not where the cabling happened to be easy.

---

## The matrix

Read as: **row** may initiate to **column**.

| From ↓ / To → | Mgmt | User | Voice | Guest | App | DB | Monitor | Backup | Internet |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Management** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **User** | ✗ | ✓ | ✗ | ✗ | 443 only | **✗** | ✗ | ✗ | ✓ |
| **Voice** | ✗ | ✗ | ✓ | ✗ | SIP/RTP | ✗ | ✗ | ✗ | ✗ |
| **Guest** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
| **Application** | ✗ | **✗** | ✗ | ✗ | ✓ | **5432 only** | ✗ | ✗ | 443 to named partners |
| **Database** | ✗ | **✗** | ✗ | ✗ | ✗ | 5432 (peer + DR) | ✗ | ✗ | ✗ |
| **Monitoring** | ✗ | read-only ports | ✗ | ✗ | read-only ports | 9187 | ✓ | ✓ | ✗ |
| **Backup** | ✗ | ✗ | ✗ | ✗ | 22 | 5432 | ✗ | ✓ | ✗ |

The cells in bold are the ones that do the work:

- **User → Database: denied.** A workstation compromise yields no database access.
  This is the single most valuable rule in the matrix.
- **Application → Database: one port.** An application compromise yields the
  database on PostgreSQL's protocol and nothing else — no SSH, no file shares, no
  lateral movement within the data centre.
- **Application → User: denied.** A compromised application server cannot reach
  back into the user population to move laterally.
- **Database → anywhere outbound: denied** except its replicas. A compromised
  database cannot exfiltrate directly; it has to come back through the application
  tier, where it is more likely to be noticed.

---

## Why each boundary exists

### Management

Reaches everything, because administration has to. Reached by nothing except the
jump hosts, because it is the segment that would give an attacker everything.

Access is key plus second factor, via a jump host, with session recording. Direct
connection from a workstation is blocked at the firewall — not discouraged by
policy, blocked.

### User and Guest

Separated because guests are untrusted by definition and users are trusted only to
the extent of reaching the platform's published interface. Guest reaches the
internet and nothing internal — not even DNS, which is provided separately.

Users reach the platform on 443 at the load balancer VIP, never at a server
address. That indirection means the application servers can be replaced,
renumbered or scaled without touching a firewall rule.

### Application

The tier most likely to be compromised, because it is the one exposed to users.
Everything about its policy assumes that will eventually happen:

- Outbound to the database on one port
- Outbound to named integration partners on 443, by host, not by subnet
- No inbound except from the load balancer
- No access to the user segment at all

### Database

The tier whose compromise would be most consequential, so it is the most
constrained. It accepts connections from the application segment, its replication
peers, the backup host and the monitoring host. It initiates almost nothing.

That last property is worth stating separately. A database with no legitimate
outbound traffic is one where *any* outbound connection is an alarm — which makes
detection far easier than in a segment where outbound traffic is normal.

### Monitoring

Reaches everything on read-only ports, which makes it a valuable target — a
compromise gives visibility into the whole estate. It is therefore reached only
from the jump hosts, and its credentials are scoped to read-only roles at the
database and SNMP layers.

### Backup

Reaches the database and the application hosts, holds a copy of everything, and is
reachable from nowhere except the jump hosts. A backup vault that anything can
reach is a ransomware target with a copy of all your data.

---

## Enforcement, in two independent layers

### Layer 1: the firewall

Stateful, logged, centrally managed. This is where the policy lives.

`cisco/templates/firewall-asa.j2`. Object groups rather than literal addresses, so
a rule is reviewable and a host change is made in one place.

### Layer 2: switch access lists

Stateless and coarser. Applied at the Nexus SVIs for the data centre segments.

**This duplication is deliberate.** The switch ACL is not a substitute for the
firewall — it cannot track state and it cannot inspect. It is there so that a
firewall misconfiguration, or a path that bypasses the firewall, does not silently
open the database segment. Two independent controls that must both fail.

`cisco/templates/nexus-datacenter.j2`, `ACL-DATABASE-IN`.

---

## Verifying it, rather than believing it

A segmentation policy that has never been tested is a diagram.

### In the lab, on every change

`lab/containerlab/validate.sh` asserts both directions:

```
segmentation_app_to_db     the application host CAN reach the database on 5432
segmentation_user_to_db    the user host CANNOT reach the database at all
segmentation_db_egress     the database CANNOT initiate to the user segment
```

The negative tests matter more than the positive ones. It is easy to write a
policy that permits what it should; verifying it *denies* what it should is what
catches the mistakes — particularly rule ordering, where a permissive entry above
a restrictive one silently defeats it.

### Against production, continuously

```bash
# What actually crossed the boundaries, from flow data
./automation/bash/flow-report.sh --policy-violations --since '7 days ago'
```

Flow data is where a gap is found. A firewall rule that is too broad produces no
error — it produces traffic that should not exist, and this is what sees it.

### On demand

```bash
ssh fw-dc1-01
packet-tracer input users tcp 10.30.10.50 45000 10.30.30.11 5432 detailed
```

Must show `DROP`. Run it after every firewall change; it is in
[`firewall-change.md`](../runbooks/firewall-change.md) as a required step.

---

## Changing the policy

1. **State the requirement precisely.** Source, destination, protocol, port. Not
   "the application needs to reach the reporting service".
2. **Justify the boundary crossing.** Which boundary, and why the alternative —
   putting the two things in the same segment — is wrong.
3. **Write the smallest rule.** A host, not a subnet. A port, not a range.
4. **Test it in the lab**, including the negative cases.
5. **Apply it with `packet-tracer` verification**, both what it opened and what it
   did not.
6. **Update this matrix.** A policy that has drifted from its documentation is a
   policy nobody can review.

---

## Known exceptions

Recorded rather than hidden, because an undocumented exception is indistinguishable
from a mistake.

| Exception | Reason | Review |
| --- | --- | --- |
| Monitoring reaches every segment | Polling requires it. Scoped to read-only ports and read-only credentials | Quarterly |
| Backup reaches the application tier on 22 | Configuration snapshots | Quarterly |
| Voice VLAN shares access ports with the user VLAN | IP phones with a pass-through port. Separated by 802.1Q, not physically | Accepted risk, documented |
| Management reaches everything | Administration requires it. Mitigated by jump host, MFA and session recording | Quarterly |

The voice VLAN exception is the weakest of these and is recorded as an accepted
risk rather than as a control: a device that can send tagged frames on an access
port can reach the voice VLAN. Mitigated by DHCP snooping and dynamic ARP
inspection on both VLANs, not eliminated.
