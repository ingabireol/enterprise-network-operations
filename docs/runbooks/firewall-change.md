# Runbook: Firewall change

**The failure mode to avoid: a rule that works, and also permits more than anyone
realises.**

Firewall changes are the ones most likely to be made under time pressure — an
application is broken, a rule is needed now — and that is exactly when an overly
broad rule gets written and never revisited.

---

## Before writing anything

Answer these four. If you cannot, the change is not ready.

1. **What exactly needs to talk to what?** Source address, destination address,
   protocol, port. "The application server needs to reach the payment gateway" is
   not an answer; `10.30.20.11 → 203.0.113.45 TCP/443` is.
2. **Is this permanent or temporary?** Temporary rules need an expiry date in the
   remark and a calendar entry, or they are permanent.
3. **What is the smallest rule that satisfies it?** An object group of one host is
   better than a subnet. A single port is better than a range.
4. **Does an existing rule already cover it?** Duplicated rules with slightly
   different scopes are how a policy becomes unreviewable.

---

## Verify the requirement before changing anything

Confirm that the traffic is genuinely being denied, and by which rule. Often it is
not the firewall at all.

```bash
ssh fw-dc1-01

packet-tracer input application tcp 10.30.20.11 45000 203.0.113.45 443 detailed
```

`packet-tracer` walks the packet through the whole policy and names the phase that
dropped it. It answers the question in seconds and it is right, which reading
access lists by eye is not.

```bash
# Is it actually being denied right now?
show logging | include 10.30.20.11 | include Deny
```

If `packet-tracer` shows the packet permitted, the problem is not the firewall.
Check routing, NAT, and the destination itself before writing a rule that will do
nothing except widen the policy.

---

## Writing the rule

```
! Always into an object group, never a bare address. A group can be reviewed,
! reused and changed in one place; a scattered set of literal addresses cannot.
object-group network PAYMENT_GATEWAY
 description Payment gateway — CHG0001234, added 2026-09-25
 network-object host 203.0.113.45

object-group service SVC_PAYMENT tcp
 description Payment gateway API
 port-object eq 443

! A remark on every rule: what, why, and the change reference. In two years this
! is the only thing that will tell anyone whether the rule can be removed.
access-list APP_IN extended permit tcp object-group APPLICATION_SEGMENT object-group PAYMENT_GATEWAY object-group SVC_PAYMENT
```

### Placement matters

Access lists are first-match. A rule placed after a broader one that already
matches will never be evaluated, and a permissive rule placed above a restrictive
one silently defeats it.

```bash
# Where will it land?
show access-list APP_IN | include line

# Insert at a specific position rather than appending blindly
access-list APP_IN line 15 extended permit tcp ...
```

This is the single most common firewall mistake and it is invisible in review —
the rule reads correctly, it is just never reached. The lab validation suite tests
for it by asserting both what must be permitted and what must be denied.

---

## Applying

```bash
# 1. Capture the current policy — this is the rollback
ssh fw-dc1-01 'show running-config access-list APP_IN' > /tmp/apl-before.txt

# 2. Apply
ssh fw-dc1-01
configure terminal
<the change>
end
write memory

# 3. Confirm it replicated to the standby. A change on the active unit that did
#    not reach the standby is a change that vanishes at the next failover.
show failover | include Config

# 4. Verify with packet-tracer, not by asking the application team
packet-tracer input application tcp 10.30.20.11 45000 203.0.113.45 443 detailed
```

---

## Verify what it did NOT open

This is the step that gets skipped, and it is the one that catches the real
mistakes.

```bash
# The intended flow works
packet-tracer input application tcp 10.30.20.11 45000 203.0.113.45 443

# These MUST still be denied. Run them every time.
packet-tracer input users tcp 10.30.10.50 45000 10.30.30.11 5432        # user → database
packet-tracer input application tcp 10.30.20.11 45000 10.30.10.50 22    # application → user
packet-tracer input application tcp 10.30.20.11 45000 203.0.113.45 22   # SSH to the partner
packet-tracer input application tcp 10.30.20.11 45000 203.0.113.46 443  # a neighbouring address
```

The last one matters more than it looks. If the rule was written against a subnet
rather than a host, this succeeds — and that is the difference between opening one
partner and opening their entire hosting provider.

---

## Rolling back

```bash
ssh fw-dc1-01
configure terminal
no access-list APP_IN extended permit tcp object-group APPLICATION_SEGMENT object-group PAYMENT_GATEWAY object-group SVC_PAYMENT
end
write memory
```

Remove the specific line. Never `clear configure access-list` — that removes the
entire policy, on a device that is currently the only thing between the internet
and the platform.

---

## Temporary rules

A temporary rule with no expiry is a permanent rule that nobody decided on.

```
access-list APP_IN extended permit tcp host 10.30.20.11 host 203.0.113.99 eq 8080
! TEMPORARY — CHG0001234 — REMOVE BY 2026-10-15 — vendor diagnostic access
```

Add a calendar entry. Then, when reviewing:

```bash
ssh fw-dc1-01 'show running-config access-list | include TEMPORARY'
```

---

## Quarterly review

```bash
# Rules that have never matched anything since the last clear
ssh fw-dc1-01 'show access-list | include \(hitcnt=0\)'

# Expired temporary rules
ssh fw-dc1-01 'show running-config access-list | include TEMPORARY'

# What the flow data says actually crosses the boundaries
./automation/bash/flow-report.sh --policy-violations --since '90 days ago'
```

A rule with zero hits is either unnecessary or the thing it permits has moved.
Both are worth resolving: an unnecessary rule is unnecessary exposure, and a rule
whose purpose has moved means the actual traffic is being permitted by something
else — probably something broader.
