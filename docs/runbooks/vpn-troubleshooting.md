# Runbook: Site-to-site VPN troubleshooting

IPsec tunnels between the primary and secondary sites, one over each provider.
Database replication rides these, so a tunnel problem is a recovery point
objective problem.

---

## Quick status

```bash
ssh wan-dc1-01
show crypto ikev2 sa
show crypto ipsec sa | include peer|pkts|status
show interface Tunnel1 | include line protocol|packets
show ip route 10.40.0.0
```

| State | Meaning |
| --- | --- |
| IKEv2 SA `READY`, IPsec SAs present, tunnel up/up | Working |
| No IKEv2 SA | Phase 1 has not completed — [Phase 1](#phase-1-ikev2) |
| IKEv2 `READY` but no IPsec SA | Phase 2 has not completed — [Phase 2](#phase-2-ipsec) |
| Both SAs present, tunnel up, no traffic | Routing or MTU — [Traffic](#tunnel-up-no-traffic) |
| `pkts encaps` rising, `pkts decaps` flat | One direction only — [One-way](#one-way-traffic) |

---

## Phase 1 (IKEv2)

```bash
debug crypto ikev2 error
debug crypto ikev2 packet          # verbose; turn off promptly
show crypto ikev2 diagnose error
```

| Symptom | Cause |
| --- | --- |
| No response from the peer | UDP/500 and UDP/4500 blocked, or the peer is unreachable |
| `AUTHENTICATION_FAILED` | Pre-shared key mismatch |
| `NO_PROPOSAL_CHOSEN` | The proposals do not overlap — encryption, integrity or DH group |
| SA forms then drops after ~10 s | Dead peer detection failing; often one side has DPD and the other does not |

```bash
# Is the peer reachable at all?
ping 198.51.100.10 source GigabitEthernet0/0/0

# Is anything blocking the IKE ports?
show access-list WAN_INBOUND | include isakmp|4500
```

The pre-shared key mismatch is the most common and the least obvious, because the
error appears on whichever side is responding. Check both.

---

## Phase 2 (IPsec)

Phase 1 completed, so the peers can talk and authenticate. Phase 2 negotiates what
is actually protected.

```bash
show crypto ipsec sa peer 198.51.100.10
debug crypto ipsec error
```

| Symptom | Cause |
| --- | --- |
| `INVALID_ID_INFORMATION` | The proxy identities do not match — each side expects a different traffic selector |
| `NO_PROPOSAL_CHOSEN` | Transform sets do not overlap |
| SA forms and rekeys constantly | Lifetime mismatch, or a PFS group mismatch |

With route-based (VTI) tunnels as configured here, the traffic selectors are
`any/any` and identity mismatches are much rarer than with policy-based tunnels —
which is one of the reasons the design uses VTI.

---

## Tunnel up, no traffic

```bash
show ip route 10.40.0.0
show ip ospf neighbor | include Tunnel
show interface Tunnel1 | include MTU|packets
```

**No route through the tunnel.** OSPF is not forming over it. Check that the tunnel
interface is in the right area and is not passive.

**Route present but traffic still fails.** Almost certainly MTU:

```bash
# This MUST succeed. 1372 payload + 28 header = 1400.
ping 10.40.30.11 source 10.30.30.11 size 1372 df-bit

# This should fail cleanly with "packet needs fragmentation"
ping 10.40.30.11 source 10.30.30.11 size 1500 df-bit
```

The IPsec MTU story is the most common cause of a tunnel that "works but large
transfers hang":

- Physical MTU 1500
- IPsec overhead ~100 bytes
- Tunnel `ip mtu 1400`
- `ip tcp adjust-mss 1360`

Both settings are needed. `ip mtu` handles packets the router originates or
forwards with the DF bit clear. `ip tcp adjust-mss` rewrites the MSS in the TCP
handshake so the endpoints never generate an oversized packet in the first place —
which is what saves you when path MTU discovery is broken by something dropping
ICMP along the way, as it frequently is.

The symptom is characteristic: ping works, SSH connects and then hangs at the
banner, HTTP returns headers and stalls on the body.

---

## One-way traffic

`pkts encaps` rising, `pkts decaps` flat: this side is sending, the far side is
not sending back.

```bash
show crypto ipsec sa | include encaps|decaps|encrypt|decrypt
```

| Pattern | Meaning |
| --- | --- |
| encaps rising, decaps flat | The far side is not sending, or its traffic is blocked before reaching us |
| encrypt rising, decrypt flat | Same, one layer down |
| Both rising, application still fails | Not a tunnel problem — check routing or the firewall at the far end |

Check from the other end. It is common for one side to have a route through the
tunnel while the other side sends the return traffic over the internet, where it is
dropped as an unexpected source.

---

## Replication over the tunnel

The tunnel exists chiefly to carry database replication, so check what actually
matters:

```bash
# Is replication flowing, and how far behind is it?
ssh db-prod-01 "sudo -u postgres psql -c \
  'SELECT application_name, state,
          pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS lag_bytes
   FROM pg_stat_replication;'"

# Is the tunnel the bottleneck?
ssh wan-dc1-01 'show policy-map interface GigabitEthernet0/0/0 | section REPLICATION'
```

Non-zero drops in the replication class mean the QoS policy is shaping it. That is
the policy working as designed — replication yields to interactive traffic — but if
the lag is growing rather than oscillating, the WAN is undersized for the write
volume and that is a capacity finding.

---

## Rekeying and lifetimes

```
Phase 1 (IKEv2): 24 hours
Phase 2 (IPsec): 1 hour, PFS group 20
```

A rekey is invisible when both sides agree. Symptoms of disagreement:

- Brief drops at a regular interval matching the lifetime
- `%CRYPTO-4-RECVD_PKT_INV_SPI` in the log — packets arriving for a security
  association that has already been replaced

```bash
show crypto ikev2 sa detail | include Life
show crypto ipsec sa | include lifetime
```

Set both sides to the same lifetime. Where the peer is a provider or partner who
will not change theirs, set ours to the shorter of the two — the shorter side
drives the rekey and the mismatch stops mattering.

---

## Recovering a stuck tunnel

Clear the security associations and let them rebuild. Brief interruption, but a
stuck tunnel is already an interruption.

```bash
clear crypto ikev2 sa remote 198.51.100.10
clear crypto ipsec sa peer 198.51.100.10

# Then force it to rebuild by sending traffic
ping 10.40.30.11 source 10.30.30.11 repeat 5

show crypto ikev2 sa
show crypto ipsec sa peer 198.51.100.10 | include pkts
```

Clear the specific peer, never `clear crypto sa` with no arguments — that drops
every tunnel on the device, including the ones that were working.
