# Lab

A containerlab topology reproducing the production design in miniature, so that
a configuration change can be proven before it reaches a real device.

## Why bother

Reading a configuration change carefully is not the same as testing it, and the
gap between the two is where outages come from. Three classes of mistake are
almost impossible to spot by reading and trivial to catch here:

- **An access list that is correct but in the wrong order.** Access lists are
  first-match, and a permissive entry above a restrictive one silently defeats it.
- **A route map or prefix list that matches nothing.** Syntactically valid,
  applied without error, and completely inert.
- **A change that works but breaks convergence.** An MTU mismatch, a
  timer change, an authentication key that matches on one side only — all of them
  work fine until something needs to fail over.

## Running it

```bash
make lab-up          # sudo containerlab deploy -t lab/containerlab/enterprise.clab.yml
make lab-validate    # run the compliance baseline against the lab devices
make lab-down
```

Connect to a device:

```bash
ssh admin@172.20.20.11        # core-dc1-01
docker exec -it clab-enterprise-network-host-app sh
```

## Images

Cisco does not redistribute IOS-XE container images. You supply your own under a
valid licence and set `IOSXE_IMAGE`, built with
[vrnetlab](https://github.com/hellt/vrnetlab):

```bash
export IOSXE_IMAGE=vrnetlab/cisco_c8000v:17.09.04a
```

Without a licensed image, `alternative-frr.clab.yml` substitutes FRR nodes. That
exercises the routing design, the addressing plan and the segmentation policy
faithfully; it does not exercise Cisco-specific syntax, so it validates the
design rather than the configuration.

## Validation suite

`validate.sh` asserts the properties that the design is supposed to guarantee:

| Test | Asserts |
| --- | --- |
| `ospf_adjacency` | Every expected OSPF neighbour reaches FULL |
| `bgp_established` | Both provider sessions establish and exchange prefixes |
| `path_preference` | Traffic prefers provider A while both are up |
| `wan_failover` | Shutting provider A moves traffic to B within 30 seconds |
| `segmentation_app_to_db` | The application host **can** reach the database on 5432 |
| `segmentation_user_to_db` | The user host **cannot** reach the database at all |
| `segmentation_db_egress` | The database host cannot initiate to the user segment |
| `mtu_path` | A 1400-byte packet with DF set crosses the IPsec tunnel |
| `hsrp_failover` | Killing the active gateway moves the VIP within 3 seconds |

The negative tests matter as much as the positive ones. It is easy to write a
segmentation policy that permits what it should; verifying it *denies* what it
should is the part that catches the mistakes.
