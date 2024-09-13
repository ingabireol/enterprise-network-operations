# Runbook: Switch onboarding

Adding a new access switch, or replacing a failed one.

**Target: 30 minutes from unboxing to serving users, most of which is the
software upgrade.**

---

## Before touching anything

- [ ] The switch is in the register (`automation/ansible/inventory/devices.yml`)
      with its hostname, address, site and role
- [ ] A management IP is allocated from the plan and recorded
- [ ] The uplink ports on the core are identified and configured
- [ ] The software version matches the standard for its platform family
- [ ] Change record raised

The register entry comes first, not last. A device configured before it is
registered is a device that will be missed by backup, compliance and monitoring —
and nobody will notice until it matters.

---

## 1. Console and bootstrap

Connect over console. Do not put an unhardened switch on the network first.

```
enable
configure terminal

hostname acc-dc1-f4-01

! Management first, so the rest can be done over SSH
vrf definition MGMT
 address-family ipv4
 exit-address-family
!
interface GigabitEthernet0/0
 vrf forwarding MGMT
 ip address 10.30.1.34 255.255.255.0
 no shutdown
!
ip route vrf MGMT 0.0.0.0 0.0.0.0 10.30.1.1

! SSH before anything else is reachable
ip domain name example.gov
crypto key generate rsa modulus 4096 label SSH-KEY
ip ssh version 2
username bootstrap privilege 15 secret <temporary>
!
line vty 0 15
 transport input ssh
 login local
!
end
write memory
```

The bootstrap account is temporary and is removed at step 4, once TACACS+ works.

---

## 2. Software version

```bash
show version | include Version
dir flash: | include .bin
```

If the version does not match the standard, upgrade now — before the switch carries
traffic. Upgrading a switch that is already serving users is a separate change with
a separate outage.

```
copy scp://netops@10.30.1.18/images/cat9k_iosxe.17.09.04a.SPA.bin flash:
verify /md5 flash:cat9k_iosxe.17.09.04a.SPA.bin
boot system flash:cat9k_iosxe.17.09.04a.SPA.bin
write memory
reload
```

Verify the MD5. An image that transferred with a single corrupted bit will boot
into ROMMON, on a device you have to visit physically.

---

## 3. Render and apply the full configuration

```bash
# Add the device to the register if it is not already there, then:
ansible-playbook -i automation/ansible/inventory/devices.yml \
  automation/ansible/playbooks/deploy-config.yml \
  --limit acc-dc1-f4-01 --check --diff
```

Read the diff. Everything it proposes should be something you expected. Then:

```bash
ansible-playbook -i automation/ansible/inventory/devices.yml \
  automation/ansible/playbooks/deploy-config.yml \
  --limit acc-dc1-f4-01 -e change_reference=CHG0001234
```

---

## 4. Verify before connecting users

```bash
# The baseline audit, before the switch carries any traffic
.venv/bin/python -m netops.compliance \
  --inventory automation/ansible/inventory/devices.yml \
  --baseline cisco/baselines/security-baseline.yml \
  --site dc-primary
```

Every check must pass. Then remove the bootstrap account:

```
configure terminal
no username bootstrap
end
write memory
```

Confirm TACACS+ authentication works **before** removing it, from a second session.
Removing the only working credential is a lesson everybody learns once.

---

## 5. Uplink

Configure the core side first, then the switch side, then bring it up.

On the core:

```
interface range TenGigabitEthernet1/0/7-8
 description Uplink to acc-dc1-f4-01
 switchport mode trunk
 switchport trunk allowed vlan 10,11,12,20,999
 switchport trunk native vlan 999
 switchport nonegotiate
 spanning-tree guard root
 channel-group 14 mode active
 no shutdown
```

`spanning-tree guard root` on every core downlink. It is what stops a
misconfigured access switch from becoming the root bridge and pulling the VLAN's
traffic through a comms cupboard.

Then verify:

```bash
show etherchannel 14 summary        # both members bundled?
show spanning-tree vlan 10          # root is still the core?
show cdp neighbors                  # the new switch is visible
```

---

## 6. Access ports

Applied from the template, not by hand. Every user-facing port gets the same
configuration; there are no exceptions, because an exception is a port that is
never audited.

```
interface range GigabitEthernet1/0/1-48
 description User access
 switchport mode access
 switchport access vlan 10
 switchport voice vlan 11
 switchport nonegotiate
 switchport port-security
 switchport port-security maximum 3
 switchport port-security violation restrict
 spanning-tree portfast
 spanning-tree bpduguard enable
 storm-control broadcast level pps 500 250
 storm-control action shutdown
 ip verify source
 no shutdown
```

Unused ports into the parking VLAN and shut:

```
interface range GigabitEthernet1/0/37-48
 description UNUSED — shut down by policy
 switchport access vlan 999
 shutdown
```

---

## 7. Monitoring

```bash
# Add to the SNMP targets
# monitoring/prometheus/targets/network-devices.yml

# Verify it is being polled
curl -s 'http://prometheus:9090/api/v1/query?query=sysUpTime{instance="10.30.1.34"}'

# Verify it is logging centrally
ssh syslog-collector 'tail -f /var/log/network/acc-dc1-f4-01/*.log'

# Verify it is being backed up
.venv/bin/python -m netops.backup \
  --inventory automation/ansible/inventory/devices.yml --site dc-primary
```

A device that is configured but not monitored, logged and backed up is not
finished.

---

## 8. Close

- [ ] Register updated and committed
- [ ] SNMP target added
- [ ] Syslog confirmed arriving
- [ ] Configuration backup confirmed
- [ ] Compliance audit passing
- [ ] Topology map regenerated (`make topology`)
- [ ] Change record closed with the final configuration attached

---

## Replacing a failed switch

The same procedure, with two differences:

1. **Restore the configuration from backup**, do not re-render, until the
   replacement is known good. The backup is what the failed device was actually
   running:
   ```bash
   scp backups/dc-primary/acc-dc1-f4-01.cfg netops@10.30.1.34:flash:
   ```
   The backup is redacted, so the secrets have to be reapplied from the vault.

2. **Then converge from the template** and diff. Anything the template proposes
   that the backup did not have is either an improvement made since the backup, or
   an undocumented change on the failed device. Both are worth knowing about.
