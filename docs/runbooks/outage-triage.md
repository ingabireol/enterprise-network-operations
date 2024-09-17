# Runbook: Network outage triage

**Read this first when something is broken and you do not yet know what.**

---

## First five minutes

The single most useful thing you can do early is establish **scope**. One user,
one segment, one site or everything — each points at a completely different layer,
and the time spent establishing it is repaid several times over.

```bash
# 1. What does monitoring already know?
#    Grafana → Network Overview. Look for the moment things changed, not the
#    current state.

# 2. Scope: can you reach each layer?
ping -c 3 10.30.10.1     # user gateway
ping -c 3 10.30.20.1     # application gateway
ping -c 3 10.30.30.1     # database gateway
ping -c 3 10.30.1.11     # core switch management
ping -c 3 10.40.1.11     # secondary site core  ← tells you if this is site-wide

# 3. What changed?
make diff                # configuration drift since last night's backup
```

**Declare an incident** if more than one user is affected, or if you cannot
establish the scope within five minutes.

---

## Scope tells you where to look

| Scope | Look at | Section |
| --- | --- | --- |
| One user, one port | Access port, cable, endpoint | [Access port](#access-port) |
| One VLAN, one switch | Access switch, its uplink | [Access switch](#access-switch) |
| One VLAN, everywhere | SVI, HSRP, the VLAN's ACL | [Gateway](#gateway) |
| Everything at one site | Core, or the path to it | [Core](#core) |
| Between sites only | WAN, tunnels, BGP | [wan-failover.md](wan-failover.md) |
| One service only | Firewall policy, or it is not a network fault | [Firewall](#firewall) |
| Intermittent, "slow" | Errors, duplex, congestion | [Slow](#slow-not-down) |

---

## Access port

```bash
ssh acc-dc1-f1-01
show interface GigabitEthernet1/0/12
show interface GigabitEthernet1/0/12 status
show mac address-table interface GigabitEthernet1/0/12
show port-security interface GigabitEthernet1/0/12
show errdisable recovery
```

| Observation | Meaning |
| --- | --- |
| `err-disabled` | A protection triggered. `show interface ... | include reason` names which |
| Down/down | Physical: cable, port, or the device is off |
| Up/down | Layer 1 is fine, layer 2 negotiation is not — often a VLAN or duplex problem |
| Up/up but no MAC learned | The endpoint is not transmitting — check the endpoint |
| Port security violation | More MAC addresses than permitted. Usually an unmanaged switch under a desk |

Recover an err-disabled port after fixing the cause:

```bash
configure terminal
interface GigabitEthernet1/0/12
shutdown
no shutdown
end
```

---

## Access switch

```bash
ssh acc-dc1-f1-01
show interface status | exclude connected      # what is down
show etherchannel summary                       # is the uplink port channel healthy?
show spanning-tree vlan 10                      # where is the root, is anything blocking?
show cdp neighbors                              # is the core visible?
show logging | include LINEPROTO|LINK|SPANTREE
```

**The uplink is down.** Check the far end on the core first. If the port channel has
lost one member the switch is still up on reduced capacity — that is a repair, not
an outage.

**Spanning tree root is somewhere unexpected.** A loop has formed, or a switch has
been added. `show spanning-tree vlan <x> root` names the current root; if it is not
the core, that is the fault.

**Ports flapping.** `show logging | include LINEPROTO` and look at the interval. A
regular cycle points at UDLD or a failing optic; irregular points at a cable.

---

## Gateway

One whole VLAN has lost connectivity but the switches are fine.

```bash
ssh core-dc1-01
show standby brief                       # who holds the HSRP virtual address?
show ip interface brief | include Vlan
show ip interface Vlan20 | include access list
show ip route 10.30.20.0
```

| Observation | Meaning |
| --- | --- |
| No device is Active for the group | Both gateways think the other is active, or both are down |
| Both devices are Active | The HSRP peers cannot see each other — split brain |
| SVI is down/down | No port in that VLAN is up, so the SVI has no reason to be |
| SVI up, no traffic passes | Almost always an access list. Check what is applied inbound |

An access list applied to an SVI is the most common cause of "the VLAN suddenly
stopped working after a change". Check it before anything else:

```bash
show ip access-lists ACL-DATABASE-IN
show ip interface Vlan30 | include access list
```

---

## Core

```bash
ssh core-dc1-01
show switch                              # StackWise Virtual: are both members up?
show stackwise-virtual
show ip ospf neighbor                    # adjacencies
show ip route summary
show processes cpu sorted | exclude 0.00
show platform hardware ... | include drop
```

**One StackWise member is down.** The survivor carries everything. Not an outage;
it is an urgent repair, because redundancy is now gone.

**Both members up but traffic is not passing.** Check the routing table first, then
CPU. Very high CPU on a core switch usually means traffic is being punted to the
CPU rather than switched in hardware — a routing problem, a loop, or an access list
that cannot be offloaded.

**OSPF adjacency down.** `show ip ospf neighbor` and look at the state. Stuck in
EXSTART or EXCHANGE is almost always an MTU mismatch between the two ends. Stuck in
INIT means hellos are arriving in one direction only.

---

## Firewall

One service is unreachable while everything else works.

```bash
ssh fw-dc1-01
show failover                            # is the pair healthy?
show conn address 10.30.20.11            # is there a connection at all?
packet-tracer input application tcp 10.30.20.11 45000 10.30.30.11 5432 detailed
```

`packet-tracer` is the fastest diagnosis available on the ASA. It simulates a packet
through the whole policy and names the exact rule that permitted or dropped it. Use
it before reading access lists by eye.

```bash
# What is actually being denied right now?
show logging | include Deny
```

**Both units think they are active.** The failover link has failed. Both are now
writing to the network with the same addresses, and this needs immediate attention.

---

## Slow, not down

The hardest case, and the one where the layer is most often guessed wrong.

```bash
# 1. Errors — the most common cause and the most overlooked
make interfaces          # netops.interfaces --errors-only across the estate

# 2. Utilisation — is a link actually full?
#    Grafana → Network Overview → WAN and core uplink utilisation

# 3. What is filling it?
./automation/bash/flow-report.sh --top-talkers --since '30 minutes ago'

# 4. Is it queueing?
ssh wan-dc1-01 'show policy-map interface GigabitEthernet0/0/0'
#    Non-zero drops in a class means that class is being shaped
```

Take them in that order. CRC errors are physical and always real; high utilisation
is visible in a graph; queue drops explain "some traffic is fine and some is not".
If all three are clean, it is probably not the network — and being able to say that
with evidence is itself valuable.

---

## High CPU

```bash
show processes cpu sorted | exclude 0.00
show processes cpu history
```

| Process | Meaning |
| --- | --- |
| `IP Input` | Traffic is being process-switched rather than hardware-switched. Check for an ACL or feature that cannot be offloaded |
| `ARP Input` | An ARP storm, or a scan sweeping a subnet |
| `Spanning Tree` | Topology instability — something is flapping |
| `BGP Router` | A large reconvergence, or a peer flapping |
| `SNMP Engine` | A monitoring system polling too aggressively |

---

## vPC

```bash
ssh nex-dc1-01
show vpc brief
show vpc consistency-parameters global
show vpc peer-keepalive
```

| Observation | Action |
| --- | --- |
| Peer link down, keepalive up | The secondary suspends its vPC ports. Servers lose half their links. Restore the peer link urgently |
| Peer link down, keepalive down | Both switches may believe they are primary. This is a split brain — check both before doing anything |
| Consistency parameter mismatch | vPC suspends the affected VLANs. `show vpc consistency-parameters` names which parameter |

---

## Before you close

1. **Confirm from a user's position**, not from a switch.
2. **Check nothing is left in a temporary state** — a shut interface, a disabled
   BGP session, a removed access list entry.
   ```bash
   make diff        # anything changed during the incident shows here
   ```
3. **Write it down while it is fresh.** What was observed, what was tried, what
   worked.
4. **Reconcile any emergency change into the templates** so the next converge does
   not revert the fix.

The review afterwards asks what made the fault hard to find, not who caused it.
