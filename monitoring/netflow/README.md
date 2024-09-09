# Flow collection

NetFlow v9 from the IOS-XE routers and sFlow from the Nexus switches, collected
by nfdump/nfcapd and summarised into the reports below.

## Why flow data earns its keep

Interface counters tell you a link is full. Flow data tells you **what is filling
it**, which is the difference between a capacity discussion and a capacity
decision. The three questions it answers routinely:

1. **"The WAN is saturated — by what?"** Usually a backup job that moved, a
   replication catch-up after an outage, or one workstation doing something
   unexpected. All three look identical in an interface graph.
2. **"Is this traffic supposed to exist?"** A host in the user segment talking
   to the database segment on port 5432 should not be possible. Flow data is
   where that gets noticed, because the firewall denied it and nobody reads
   deny logs, but the attempt still shows.
3. **"What did the traffic look like before the incident?"** Retrospective
   questions are unanswerable without stored flows.

## Configuration

Collector: `nfcapd` listening on UDP 2055 (NetFlow) and 6343 (sFlow), writing
five-minute files, retained for 90 days.

Export configuration lives in the device templates:
- `cisco/templates/wan-router.j2` — the `flow record`, `flow exporter` and
  `flow monitor` blocks
- `cisco/templates/nexus-datacenter.j2` — the `sflow` block

## Reports

`automation/bash/flow-report.sh` produces the routine summaries:

```bash
# Top talkers across the WAN in the last hour
./automation/bash/flow-report.sh --top-talkers --since '1 hour ago'

# What is consuming a specific link
./automation/bash/flow-report.sh --interface GigabitEthernet0/0/0 --since '30 minutes ago'

# Conversations that crossed a segment boundary they should not have
./automation/bash/flow-report.sh --policy-violations --since '24 hours ago'
```

## Retention

| Data | Retention | Why |
| --- | --- | --- |
| Raw flows, 5-minute files | 90 days | Enough to answer a retrospective question about a quarter |
| Hourly aggregates | 1 year | Capacity trending |
| Daily aggregates | 3 years | Procurement and lifecycle planning |
