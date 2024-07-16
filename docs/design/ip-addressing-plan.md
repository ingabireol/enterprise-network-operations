# IP addressing plan

Addresses are allocated so that the address itself tells you what a host is and
where it is. That property is what makes a firewall rule readable, a route summary
possible, and an unfamiliar address identifiable during an incident without
looking anything up.

All addresses are RFC 1918 examples for a documented reference topology.

---

## Structure

```
10 . S . F . H
     │   │   └── Host
     │   └────── Function
     └────────── Site
```

| Octet | Meaning | Values |
| --- | --- | --- |
| Second | Site | 30 = primary, 40 = secondary/DR, 10 = development, 20 = test |
| Third | Function | 1 = management, 10 = user, 20 = application, 30 = database, 40 = monitoring, 50 = backup |
| Fourth | Host | Allocated by the conventions below |

The consequence: `10.40.30.11` reads as *secondary site, database function, host 11*
without reference to anything. And because each site's allocation is contiguous,
each site summarises to a single prefix in BGP.

---

## Site allocations

| Site | Supernet | Advertised as |
| --- | --- | --- |
| Primary data centre | 10.30.0.0/16 | 10.30.0.0/16 |
| Secondary / DR | 10.40.0.0/16 | 10.40.0.0/16 |
| Development | 10.10.0.0/16 | not advertised externally |
| Test | 10.20.0.0/16 | not advertised externally |

---

## Primary site (10.30.0.0/16)

| Function | Network | VLAN | Gateway | DHCP | Notes |
| --- | --- | --- | --- | --- | --- |
| Management | 10.30.1.0/24 | 1 | 10.30.1.1 | No | Devices, jump hosts, AAA, syslog |
| User access | 10.30.10.0/24 | 10 | 10.30.10.1 | .100–.250 | Workstations |
| Voice | 10.30.11.0/24 | 11 | 10.30.11.1 | .50–.250 | IP telephony |
| Guest | 10.30.12.0/24 | 12 | 10.30.12.1 | .50–.250 | Internet only, no internal access |
| Application | 10.30.20.0/24 | 20 | 10.30.20.1 | No | App servers, load balancers |
| Database | 10.30.30.0/24 | 30 | 10.30.30.1 | No | PostgreSQL |
| Monitoring | 10.30.40.0/24 | 40 | 10.30.40.1 | No | Prometheus, Grafana, Loki |
| Backup | 10.30.50.0/24 | 50 | 10.30.50.1 | No | Backup vault |
| Point-to-point | 10.30.254.0/24 | — | — | No | /31 routed links |
| Loopbacks | 10.30.255.0/24 | — | — | No | /32 router IDs |
| Parking | — | 999 | none | No | Unused ports; unrouted by design |

### Host conventions within a subnet

| Range | Purpose |
| --- | --- |
| .1 | Gateway (HSRP virtual address) |
| .2–.3 | Physical gateway addresses on the HSRP pair |
| .10–.49 | Infrastructure servers |
| .50–.99 | Static allocations |
| .100–.250 | DHCP pool, where applicable |
| .251–.254 | Reserved |

Keeping .1 as the virtual address and .2/.3 as the physical ones means the
gateway is always .1 on every subnet in the estate. During an incident nobody has
to remember which subnet does it differently.

---

## Point-to-point links

/31 for every routed link. A /30 wastes two addresses of four and there has been
no reason to use them since RFC 3021.

| Link | Network |
| --- | --- |
| core-dc1-01 ↔ wan-dc1-01 | 10.30.254.0/31 |
| core-dc1-02 ↔ wan-dc1-01 | 10.30.254.2/31 |
| core-dc1-01 ↔ nex-dc1-01 | 10.30.254.4/31 |
| core-dc1-01 ↔ nex-dc1-02 | 10.30.254.6/31 |
| core-dc1-02 ↔ nex-dc1-01 | 10.30.254.8/31 |
| core-dc1-02 ↔ nex-dc1-02 | 10.30.254.10/31 |
| core-dc1-01 ↔ fw-dc1-01 | 10.30.254.12/31 |

## Loopbacks

One /32 per device, used as the OSPF and BGP router ID and as the source for
management traffic. A loopback is always up, so the router ID does not change when
a physical interface fails.

| Device | Loopback |
| --- | --- |
| core-dc1-01 | 10.30.255.11/32 |
| core-dc1-02 | 10.30.255.12/32 |
| nex-dc1-01 | 10.30.255.21/32 |
| nex-dc1-02 | 10.30.255.22/32 |
| wan-dc1-01 | 10.30.255.41/32 |
| core-dc2-01 | 10.40.255.11/32 |
| wan-dc2-01 | 10.40.255.41/32 |

---

## Secondary site (10.40.0.0/16)

The same structure with the site octet changed. `10.40.30.11` is the DR database,
mirroring `10.30.30.11` in the primary site — which means a firewall rule about
"the database" can be written once and read correctly at both sites.

---

## What is reserved and why

| Range | Reserved for |
| --- | --- |
| 10.30.60.0/24 – 10.30.99.0/24 | Future segments at the primary site |
| 10.50.0.0/16 | A third site |
| 10.60.0.0/16 | Cloud interconnect |
| fd00:30::/48 | IPv6 at the primary site, when it is deployed |
| fd00:40::/48 | IPv6 at the secondary site |

Reserving space costs nothing now and avoids the renumbering exercise later.
Renumbering a live segment is among the most disruptive changes a network team can
undertake, because every firewall rule, monitoring target and hard-coded address
in an application has to move with it.

---

## Rules

1. **Never allocate outside this plan.** A subnet that does not fit the structure
   breaks route summarisation and makes every firewall rule about it a special case.
2. **A subnet is a segment boundary.** If two things do not need the same security
   policy, they do not share a subnet.
3. **/31 for point-to-point links.** Always.
4. **Document before allocating**, not after. An allocation that exists only on a
   device is an allocation that will be duplicated.
5. **The gateway is .1 on every subnet.** No exceptions.
