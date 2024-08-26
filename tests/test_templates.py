"""
Template rendering tests.

Parsing a template proves it is syntactically valid. Rendering it proves the
variables it references actually exist — which is the failure that otherwise
surfaces at deployment time, on a device, in a change window.

The assertions then check that the rendered output satisfies the security
baseline. This closes the loop: the template that generates a configuration and
the rule set that audits it are tested against each other, so they cannot drift
apart without CI noticing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

sys.path.insert(0, str(Path(__file__).parent.parent / "automation" / "python"))

from netops.compliance import Result, load_baseline  # noqa: E402

REPO = Path(__file__).parent.parent
TEMPLATES = REPO / "cisco" / "templates"
GROUP_VARS = REPO / "automation" / "ansible" / "group_vars"
HOST_VARS = REPO / "automation" / "ansible" / "host_vars"


def _resolve(value, context):
    """
    Resolve the `{{ vault_* }}` indirections that Ansible would.

    The test does not need real secrets — it needs the variable to be defined so
    that StrictUndefined does not fire. Substituting a placeholder is exactly
    right: it proves the template references a variable that exists, without any
    secret being involved.
    """
    if isinstance(value, str):
        return re.sub(r"\{\{\s*(vault_\w+)\s*\}\}", r"PLACEHOLDER_\1", value)
    if isinstance(value, dict):
        return {k: _resolve(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, context) for v in value]
    return value


@pytest.fixture(scope="module")
def env() -> Environment:
    # StrictUndefined is the point of this whole module: an undefined variable
    # must raise here rather than rendering as an empty string and producing a
    # configuration with a silently missing line.
    return Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
    )


@pytest.fixture(scope="module")
def base_context() -> dict:
    context = yaml.safe_load((GROUP_VARS / "all.yml").read_text())

    # Ansible resolves the inventory into these; the test supplies equivalents.
    context.update({
        "env_tier": "prod",
        "groups": {
            "loadbalancers": [], "appservers": [], "tomcat_nodes": [],
            "databases": [], "monitoring": [], "backup": [], "platform": [],
        },
        "hostvars": {},
        "group_names": ["core"],
        "ansible_host": "10.30.1.11",
        "ansible_managed": "Ansible managed",
        "ansible_date_time": {"epoch": "1759000000", "iso8601": "2026-09-25T12:00:00Z"},
    })
    return _resolve(context, {})


def _context_for(host: str, base: dict) -> dict:
    context = dict(base)
    context.update(_resolve(yaml.safe_load((HOST_VARS / f"{host}.yml").read_text()), {}))
    context["inventory_hostname"] = host
    return context


RENDER_CASES = [
    ("core-switch.j2", "core-dc1-01"),
    ("access-switch.j2", "acc-dc1-f1-01"),
    ("wan-router.j2", "wan-dc1-01"),
]


# --- Rendering ---------------------------------------------------------------

@pytest.mark.parametrize("template_name,host", RENDER_CASES)
def test_template_renders_without_undefined_variables(
    env: Environment, base_context: dict, template_name: str, host: str
) -> None:
    template = env.get_template(template_name)
    output = template.render(**_context_for(host, base_context))
    assert len(output) > 1500, f"{template_name} rendered suspiciously little output"


@pytest.mark.parametrize("template_name,host", RENDER_CASES)
def test_rendered_config_has_no_unresolved_jinja(
    env: Environment, base_context: dict, template_name: str, host: str
) -> None:
    output = env.get_template(template_name).render(**_context_for(host, base_context))
    assert "{{" not in output, "unresolved Jinja expression in the rendered configuration"
    assert "{%" not in output


@pytest.mark.parametrize("template_name,host", RENDER_CASES)
def test_rendered_config_has_no_empty_directives(
    env: Environment, base_context: dict, template_name: str, host: str
) -> None:
    """
    A line like `ip address  ` with a missing value is syntactically plausible to
    a template and rejected by the device. StrictUndefined catches a missing
    variable; this catches one that resolved to an empty string.
    """
    output = env.get_template(template_name).render(**_context_for(host, base_context))

    # NetFlow record fields legitimately end in these words — `match ipv4 source
    # address` is a complete directive, not a truncated one.
    complete_by_design = re.compile(r"^\s*(match|collect)\s")

    offenders = [
        line for line in output.splitlines()
        if line.strip()
        and not line.strip().startswith(("!", ":"))
        and not complete_by_design.match(line)
        and re.search(r"\b(address|vlan|key|priority|cost|description)\s*$", line)
    ]
    assert not offenders, f"directive with a missing value: {offenders[:3]}"


# --- The rendered output against the security baseline -----------------------

@pytest.fixture(scope="module")
def security_rules():
    return load_baseline(REPO / "cisco" / "baselines" / "security-baseline.yml")


def test_rendered_core_config_passes_its_own_baseline(
    env: Environment, base_context: dict, security_rules
) -> None:
    """
    The templates in this repository must satisfy the baseline this repository
    audits against. Without this test the two drift apart, and the first anyone
    knows is a nightly report full of failures on devices that were just built.
    """
    output = env.get_template("core-switch.j2").render(
        **_context_for("core-dc1-01", base_context)
    )

    # Rules scoped to other roles or platforms do not apply here.
    applicable = [
        r for r in security_rules
        if "iosxe" in r.platforms and (not r.roles or "core" in r.roles)
    ]

    failures = []
    for rule in applicable:
        result, detail = rule.evaluate(output)
        if result == Result.FAIL:
            failures.append(f"{rule.id} ({rule.title}): {detail}")

    assert not failures, "the core template fails its own baseline:\n  " + "\n  ".join(failures)


def test_rendered_access_config_passes_its_own_baseline(
    env: Environment, base_context: dict, security_rules
) -> None:
    output = env.get_template("access-switch.j2").render(
        **_context_for("acc-dc1-f1-01", base_context)
    )

    applicable = [
        r for r in security_rules
        if "iosxe" in r.platforms and (not r.roles or "access" in r.roles)
    ]

    failures = []
    for rule in applicable:
        result, detail = rule.evaluate(output)
        if result == Result.FAIL:
            failures.append(f"{rule.id} ({rule.title}): {detail}")

    assert not failures, "the access template fails its own baseline:\n  " + "\n  ".join(failures)


# --- Specific security properties of the rendered output ---------------------

@pytest.mark.parametrize("template_name,host", RENDER_CASES)
def test_no_telnet_in_rendered_config(
    env: Environment, base_context: dict, template_name: str, host: str
) -> None:
    output = env.get_template(template_name).render(**_context_for(host, base_context))
    assert "transport input telnet" not in output
    assert "transport input all" not in output
    assert "transport input ssh" in output


@pytest.mark.parametrize("template_name,host", RENDER_CASES)
def test_no_snmp_community_in_rendered_config(
    env: Environment, base_context: dict, template_name: str, host: str
) -> None:
    output = env.get_template(template_name).render(**_context_for(host, base_context))
    assert "snmp-server community" not in output


@pytest.mark.parametrize("template_name,host", RENDER_CASES)
def test_no_http_server_in_rendered_config(
    env: Environment, base_context: dict, template_name: str, host: str
) -> None:
    output = env.get_template(template_name).render(**_context_for(host, base_context))
    assert "no ip http server" in output
    assert not re.search(r"^ip http server\s*$", output, re.M)


def test_native_vlan_is_never_one(env: Environment, base_context: dict) -> None:
    for template_name, host in RENDER_CASES:
        output = env.get_template(template_name).render(**_context_for(host, base_context))
        assert not re.search(r"switchport trunk native vlan 1\s*$", output, re.M), \
            f"{template_name} leaves the native VLAN as 1 — VLAN hopping"


def test_access_template_enables_the_layer_two_protections(
    env: Environment, base_context: dict
) -> None:
    output = env.get_template("access-switch.j2").render(
        **_context_for("acc-dc1-f1-01", base_context)
    )
    for required in ("ip dhcp snooping", "ip arp inspection vlan",
                     "spanning-tree portfast bpduguard default",
                     "switchport port-security", "ip verify source"):
        assert required in output, f"access template is missing: {required}"


def test_access_template_never_becomes_spanning_tree_root(
    env: Environment, base_context: dict
) -> None:
    output = env.get_template("access-switch.j2").render(
        **_context_for("acc-dc1-f1-01", base_context)
    )
    assert "spanning-tree vlan 1-4094 priority 61440" in output


def test_wan_template_filters_inbound(env: Environment, base_context: dict) -> None:
    """
    A provider-facing interface without an inbound filter accepts spoofed source
    addresses from our own ranges, which defeats every internal control at once.
    """
    output = env.get_template("wan-router.j2").render(
        **_context_for("wan-dc1-01", base_context)
    )
    assert "ip access-list extended WAN_INBOUND" in output
    assert "ip access-group WAN_INBOUND in" in output
    assert "deny   ip 10.30.0.0 0.0.255.255 any log-input" in output
    assert "deny   ip any any log-input" in output


def test_wan_template_limits_bgp_prefixes(env: Environment, base_context: dict) -> None:
    output = env.get_template("wan-router.j2").render(
        **_context_for("wan-dc1-01", base_context)
    )
    assert output.count("maximum-prefix") >= 2, "not every BGP peer has a prefix limit"


def test_secrets_are_not_literal_in_the_templates() -> None:
    """
    Every credential in a template must come from a variable. A literal is a
    credential committed to the repository.
    """
    for template in TEMPLATES.rglob("*.j2"):
        text = template.read_text()
        for line_number, line in enumerate(text.splitlines(), 1):
            if not re.search(r"(secret|password|key-string|pre-shared-key|key 7)", line, re.I):
                continue
            if "{{" in line or line.strip().startswith(("!", "#", "{#")):
                continue
            # `key 7 ` with nothing after it, or a Jinja expression, is fine.
            if re.search(r"(secret|password|key)\s+\d*\s*[A-Za-z0-9$./]{8,}", line):
                pytest.fail(f"{template.name}:{line_number} may contain a literal secret: {line.strip()}")
