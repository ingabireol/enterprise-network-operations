# Routing design

OSPF inside each site, BGP between sites and to the providers.

---

## Why two protocols

They are good at different things, and using one for both jobs means doing one of
them badly.

**OSPF inside.** Fast convergence — sub-second with BFD — and it reacts to a link
failure without anyone's involvement. Minimal configuration for a topology that
changes only when hardware does.

**BGP between.** Policy control. Prefix filtering. The ability to express "prefer
provider A unless it is degraded" as configuration rather than as a cost value
that also affects everything else.

Keeping them separate has a second benefit that is easy to overlook: an internal
topology change cannot leak into WAN policy. A flapping access switch uplink stays
an OSPF event and never becomes a BGP announcement to the internet.

---

## OSPF

### Areas

| Area | Contains | Type |
| --- | --- | --- |
| 0 | Core, data centre, WAN routers at both sites | Backbone |
| 10 | Primary site access layer | Normal |
| 20 | Secondary site access layer | Normal |

Three areas for fifteen devices is arguably more than necessary. It is done this
way so that an access-layer flap is contained — a link going up and down in area 10
produces intra-area LSAs that never reach area 20, and the backbone recalculates a
summary rather than a full topology.

### Configuration decisions

**`passive-interface default`, with explicit exceptions.** Hosts have no business
receiving OSPF hellos. The default is inverted from the vendor default because the
safe option should require no action.

**Message-digest authentication on every area.** An unauthenticated OSPF process
accepts routes from anything on the segment that can send a hello. Injecting a more
specific route redirects traffic through an attacker's host, and the routing table
looks entirely normal afterwards.

**BFD on every adjacency.** OSPF's own dead interval is 40 seconds by default.
BFD detects a failure in under a second and tells OSPF, so convergence is bounded
by SPF rather than by hello timers.

```
router ospf 1
 router-id 10.30.255.11
 area 0 authentication message-digest
 passive-interface default
 no passive-interface TenGigabitEthernet1/0/1
 auto-cost reference-bandwidth 100000
 timers throttle spf 50 200 5000
 bfd all-interfaces
 max-lsa 12000
```

**`auto-cost reference-bandwidth 100000`** — 100 Gb. The default reference is
100 Mb, which makes every link of 100 Mb or faster cost exactly 1, so a 1 Gb link
and a 40 Gb link are treated as equal. Setting it must be done consistently on
every router or the costs disagree and traffic takes paths nobody intended.

**`max-lsa 12000`** — a guard against an LSA flood from a misconfigured or
compromised neighbour exhausting memory. The router complains and stops rather
than falling over.

**SPF throttling** — 50 ms initial, 200 ms increment, 5 s maximum. Fast for the
first recalculation, backing off if the topology is unstable, so a flapping link
does not consume the CPU with continuous recalculation.

---

## BGP

### Autonomous systems

| AS | Owner |
| --- | --- |
| 65001 | Us (private) |
| 64512 | Provider A |
| 64513 | Provider B |

### Path preference

Two levers, both needed:

**Inbound (which path we send over):** local preference. Provider A's routes get
200, provider B's get 100. Higher wins, and local preference is compared before
AS path length, so it is decisive.

**Outbound (which path the internet sends to us over):** AS path prepending. Our
prefixes are advertised to provider B with our AS repeated twice, making that path
look longer and therefore less attractive.

**Both are required.** Setting only local preference produces asymmetric routing —
we send over A and receive over B — which breaks the stateful firewall in ways that
present as intermittent application faults and take a long time to diagnose.

```
route-map RM-PRIMARY-IN permit 10
 set local-preference 200

route-map RM-SECONDARY-IN permit 10
 set local-preference 100

route-map RM-PRIMARY-OUT permit 10
 match ip address prefix-list PL-OUR-PREFIXES

route-map RM-SECONDARY-OUT permit 10
 match ip address prefix-list PL-OUR-PREFIXES
 set as-path prepend 65001 65001
```

### Safety configuration

**`no bgp default ipv4-unicast`.** Older IOS activates every neighbour for IPv4
unicast automatically, which means a newly configured peer starts exchanging
routes before its policy is applied. This makes activation explicit.

**`maximum-prefix` on every peer.** A misconfigured or compromised provider that
advertises the full internet table would exhaust the router's memory and take the
device down. A prefix limit turns that into a dropped session, which is
recoverable within seconds.

**`ttl-security hops 1` on provider sessions.** The peer must be exactly one hop
away. A spoofed BGP packet from anywhere else arrives with a lower TTL and is
dropped in hardware.

**Authentication on every session.** MD5 is dated, but it is what the providers
support, and it raises the cost of both session hijacking and blind TCP reset
attacks substantially.

**Prefix lists in both directions.** We advertise only our own prefixes, and we
accept only a default route plus the partner prefixes we expect. A provider
leaking their full table to us should be a non-event.

---

## Redistribution

Deliberately minimal. Redistribution between routing protocols is where routing
loops come from, and the only thing that needs to cross the boundary here is the
default route.

```
router ospf 1
 default-information originate route-map ORIGINATE-DEFAULT-IF-WAN-UP
```

**Conditional on the WAN actually being up.** A router advertising a default route
it cannot honour creates a black hole that is worse than having no default — every
host sends its internet traffic to a router that discards it, silently.

The condition is a tracked object on the provider-facing interface. When both
providers are down, the default is withdrawn and the hosts at least fail fast.

---

## Convergence

| Event | Detection | Convergence | Mechanism |
| --- | --- | --- | --- |
| Link failure, OSPF | < 1 s | < 1 s | BFD + SPF throttle |
| Link failure, no BFD | 40 s | 40 s + SPF | OSPF dead interval — why BFD is on everything |
| Provider failure | < 1 s | 15–30 s | BFD + BGP reconvergence |
| Core switch failure | < 1 s | < 1 s | StackWise Virtual — a link event, not a topology change |
| Site failure | manual | up to 2 h | Documented DR failover |

BGP reconvergence is slower than OSPF and that is inherent: it has to re-run best
path selection across the whole table and propagate the result. 15–30 seconds is
normal and is accounted for in the platform's timeout settings.

---

## Verification

```bash
# Adjacencies
ssh core-dc1-01 'show ip ospf neighbor'
ssh wan-dc1-01 'show ip bgp summary'

# Is the path what the design intends?
ssh wan-dc1-01 'show ip bgp 0.0.0.0/0'          # provider A's path should be best

# Are we advertising what we mean to, and nothing else?
ssh wan-dc1-01 'show ip bgp neighbors 198.51.100.1 advertised-routes'

# Does failover actually work?
lab/containerlab/validate.sh --test wan_failover
```

The last one is the only real answer. The first three confirm the configuration
matches the intent; only the failover test confirms the intent was correct.
