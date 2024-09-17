# Runbook: WAN failover

Two providers, BGP path control. Failover is automatic; this runbook covers
verifying it worked, forcing it deliberately, and what to do when it did not.

---

## Normal state

```
Provider A (AS 64512) ── preferred: local-pref 200 in, no prepend out
Provider B (AS 64513) ── standby:   local-pref 100 in, 2× prepend out
```

Inbound preference is set with local preference; outbound preference — which path
the rest of the internet uses to reach us — is set by prepending our AS on the
secondary advertisement. Both are needed. Setting only one produces asymmetric
routing, which breaks the stateful firewall in ways that look like an application
fault and take a long time to diagnose.

```bash
ssh wan-dc1-01
show ip bgp summary
show ip bgp 0.0.0.0/0
show ip route 0.0.0.0
```

Both sessions established, provider A's path marked best.

---

## Automatic failover

When provider A fails, BFD detects it in under a second, the BGP session drops,
and provider B's routes become best. Convergence is typically 15–30 seconds — most
of that is BGP reconvergence, not detection.

**What breaks during it:** existing TCP sessions through the firewall. The return
path changes, the firewall sees packets for a connection it has no state for, and
drops them. Users reconnect and it works. There is no way around this without
symmetric routing enforcement, and the complexity of that is not worth it at this
scale.

### Verifying it worked

```bash
ssh wan-dc1-01
show ip bgp summary                      # A down, B established
show ip bgp 0.0.0.0/0                    # B's path is now best
show ip route 0.0.0.0                    # next hop is B
show ip interface brief | include Gigabit

# From inside, confirm it actually works rather than just looks right
ping 8.8.8.8 source 10.30.20.1
traceroute 8.8.8.8 source 10.30.20.1     # should now show B's network

# The thing that actually matters: is inter-site replication still flowing?
ssh core-dc1-01 'ping 10.40.30.11 source 10.30.30.11'
```

---

## Forcing a failover

For a scheduled test, or to take a provider out of service.

```bash
ssh wan-dc1-01
configure terminal

! Preferred: shut the BGP session, not the interface. The link stays up for
! diagnosis and the provider can still test their side.
router bgp 65001
 neighbor 198.51.100.1 shutdown
end

! Watch it converge
show ip bgp summary
show ip route 0.0.0.0
```

Restore:

```bash
configure terminal
router bgp 65001
 no neighbor 198.51.100.1 shutdown
end

! Confirm the prefix count comes back to what it was
show ip bgp summary
```

---

## When failover did not happen

### Both sessions are down

```bash
show ip interface brief
show ip bgp neighbors 198.51.100.1 | include state|Last
show logging | include BGP
```

| Observation | Cause |
| --- | --- |
| Interfaces down | A physical problem affecting both — check whether both circuits share a duct or a provider |
| Interfaces up, sessions idle | Routing to the peer addresses, or an access list blocking TCP/179 |
| Sessions active but never established | Authentication mismatch, or the peer is not configured for us |
| `Connection reset by peer` | The provider's side has changed |

### Session B is up but traffic is not using it

```bash
show ip bgp 0.0.0.0/0                    # is B's path being received at all?
show ip bgp neighbors 198.51.100.5 received-routes
show route-map RM-SECONDARY-IN
```

Usually one of three things: B is not advertising a default route, a route map is
filtering it, or a static route with a lower administrative distance is still
pointing at A.

### Traffic goes out over B but nothing comes back

Asymmetric routing. Our prefixes are still being preferred over A by the wider
internet, because the prepending did not take effect or was not configured.

```bash
show ip bgp neighbors 198.51.100.5 advertised-routes
# Our prefix should show the prepended AS path on the B session
```

---

## Both providers are down

The sites are isolated from each other and from the internet.

**Immediate:**

1. Confirm it is genuinely both — from a third location if one is available.
2. Check whether the circuits share infrastructure. Two providers in one duct is
   one provider with extra paperwork, and this is when you find out.
3. Raise with both providers. A simultaneous failure of two independent providers
   is unusual enough that it is often a shared upstream.

**Consequences to manage:**

- Database replication to the DR site has stopped. The recovery point objective is
  breached for the duration and will remain breached until replication catches up.
  Record the start time.
- Each site is still internally functional. The platform at the primary site is
  serving users who can reach it.
- **Do not fail over to DR.** The DR site is also isolated, and promoting its
  database while the primary is still serving writes produces two divergent
  databases. That is considerably worse than the outage.

**On restoration:**

```bash
# Confirm both sessions come back
ssh wan-dc1-01 'show ip bgp summary'

# Confirm replication resumes and watch it catch up
ssh db-prod-01 "sudo -u postgres psql -c 'SELECT * FROM pg_stat_replication;'"
```

Replication lag will be large and will shrink. Watch it reach zero before
considering the incident closed — the outage is not over until the recovery point
objective is met again.

---

## Scheduled failover test

Quarterly, in a maintenance window.

```bash
# 1. Baseline
ssh wan-dc1-01 'show ip bgp summary' > /tmp/bgp-before.txt

# 2. Fail provider A
ssh wan-dc1-01 'configure terminal
router bgp 65001
 neighbor 198.51.100.1 shutdown
end'

# 3. Time the convergence
time (while ! ping -c1 -W1 8.8.8.8 >/dev/null 2>&1; do sleep 1; done)

# 4. Verify the service, not just the routing
curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' https://efp.example.gov/healthz
ssh core-dc1-01 'ping 10.40.30.11 source 10.30.30.11'

# 5. Restore and verify the path returns to A
ssh wan-dc1-01 'configure terminal
router bgp 65001
 no neighbor 198.51.100.1 shutdown
end'
sleep 60
ssh wan-dc1-01 'show ip bgp 0.0.0.0/0'
```

Record the measured convergence time. A drill without a measurement is an
assertion.

The same test runs against the lab topology as part of `lab/containerlab/validate.sh`,
which is where a configuration change that would break failover gets caught before
it reaches production.
