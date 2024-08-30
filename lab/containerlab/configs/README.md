# Lab startup configurations

Rendered from the production templates in `cisco/templates/` with the lab
variable set, so that the lab runs the same configuration the production devices
do — with lab addresses substituted.

Regenerate after changing a template:

```bash
ansible-playbook -i lab/containerlab/lab-inventory.yml \
  automation/ansible/playbooks/deploy-config.yml --check --diff
cp reports/rendered/*.cfg lab/containerlab/configs/
```

Rendering from the same templates is the point. A lab running hand-written
configurations tests the lab, not the change.
