# Enterprise Network Operations — operator entry points.

PY        := python3
VENV      := .venv
PYTHON    := $(VENV)/bin/python
PIP       := $(VENV)/bin/pip
INVENTORY ?= automation/ansible/inventory/devices.yml
SITE      ?= all
DEVICE    ?=

.DEFAULT_GOAL := help
.PHONY: help deps lint test inventory backup compliance diff health interfaces \
        topology lab-up lab-down lab-validate ansible-facts ansible-backup clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Variables: SITE=<site>  DEVICE=<hostname>  INVENTORY=<path>"

deps: ## Create the virtualenv and install dependencies
	$(PY) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r automation/python/requirements.txt
	$(VENV)/bin/ansible-galaxy collection install -r automation/ansible/requirements.yml

lint: ## Lint Python, YAML and shell
	$(VENV)/bin/ruff check automation/python
	$(VENV)/bin/yamllint -c .yamllint.yml automation monitoring lab
	find automation/bash -name '*.sh' -print0 | xargs -0 -r shellcheck -x

test: ## Run the Python test suite
	$(VENV)/bin/pytest tests/ -v

inventory: ## Discover devices and reconcile against the register
	$(PYTHON) -m netops.inventory --inventory $(INVENTORY) --site $(SITE)

backup: ## Back up every device configuration
	$(PYTHON) -m netops.backup --inventory $(INVENTORY) --site $(SITE)

compliance: ## Audit every device against the security baseline
	$(PYTHON) -m netops.compliance --inventory $(INVENTORY) --site $(SITE) \
	  --baseline cisco/baselines/security-baseline.yml --report reports/compliance.html

diff: ## Show configuration changes since the previous backup
	$(PYTHON) -m netops.diff --inventory $(INVENTORY) --site $(SITE)

health: ## Poll device health across the estate
	$(PYTHON) -m netops.health --inventory $(INVENTORY) --site $(SITE)

interfaces: ## Report interface errors, discards and utilisation
	$(PYTHON) -m netops.interfaces --inventory $(INVENTORY) --site $(SITE) --errors-only

topology: ## Build a topology map from CDP/LLDP neighbours
	$(PYTHON) -m netops.topology --inventory $(INVENTORY) --output reports/topology.dot

ansible-facts: ## Gather structured facts with Ansible
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) automation/ansible/playbooks/gather-facts.yml

ansible-backup: ## Back up configurations via Ansible
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) automation/ansible/playbooks/backup-configs.yml

lab-up: ## Start the containerlab topology
	sudo containerlab deploy -t lab/containerlab/enterprise.clab.yml

lab-down: ## Destroy the containerlab topology
	sudo containerlab destroy -t lab/containerlab/enterprise.clab.yml --cleanup

lab-validate: ## Run the validation suite against the lab
	$(PYTHON) -m netops.compliance --inventory lab/containerlab/lab-inventory.yml \
	  --baseline cisco/baselines/security-baseline.yml

clean: ## Remove generated artefacts
	rm -rf reports/ __pycache__ .pytest_cache automation/python/**/__pycache__
