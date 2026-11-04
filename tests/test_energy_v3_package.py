"""Static boundaries for the preview package (no Home Assistant installation needed)."""

from pathlib import Path

import yaml

PACKAGE = Path(__file__).resolve().parents[1] / "homeassistant/packages/energy_v3.yaml"


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys rather than silently replacing safety checks."""


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_package():
    return yaml.load(PACKAGE.read_text(encoding="utf-8"), Loader=UniqueLoader)


def action_nodes(sequence):
    """Walk executable actions, never mistake returned plan data for an action."""
    for node in sequence:
        yield node
        for branch in ("then", "else", "default"):
            yield from action_nodes(node.get(branch, []))
        for choice in node.get("choose", []):
            yield from action_nodes(choice["sequence"])


def test_only_four_scripts_and_two_automations():
    package = load_package()
    assert set(package["script"]) == {
        "energy_v3_submit",
        "energy_v3_reconcile",
        "energy_v3_apply_solax",
        "energy_v3_apply_deye",
    }
    assert {a["id"] for a in package["automation"]} == {"energy_v3_supervisor", "energy_v3_safety"}
    assert package["input_boolean"]["energy_v3_enabled"]["initial"] is False
    assert package["input_boolean"]["energy_v3_topology_verified"]["initial"] is False
    assert package["input_select"]["energy_v3_topology"]["initial"] == "current"
    assert "initial" not in package["input_text"]["energy_v3_command"]
    assert package["input_text"]["energy_v3_command"]["max"] == 255
    assert package["input_text"]["energy_v3_applied"]["initial"] == '{"m":"UNKNOWN","p":0,"u":0}'
    assert "energy_v3_preview_mode" not in package["input_select"]
    assert sum(len(package[domain]) for domain in ("input_boolean", "input_select", "input_text")) == 6


def test_executable_graph_cannot_send_physical_commands():
    package = load_package()
    allowed = {"input_text.set_value", "input_select.select_option", "input_boolean.turn_on", "input_boolean.turn_off"}
    allowed |= {f"script.{s}" for s in package["script"]}
    sequences = [s["sequence"] for s in package["script"].values()]
    sequences += [a["actions"] for a in package["automation"]]
    for sequence in sequences:
        for node in action_nodes(sequence):
            assert not ({"service", "service_template", "action_template", "parallel", "repeat"} & node.keys())
            if "action" in node:
                assert node["action"] in allowed  # Includes rejection of templated/dynamic services.
                target = node.get("target", {}).get("entity_id", "")
                assert not target or target.startswith(
                    ("input_boolean.energy_v3_", "input_text.energy_v3_", "input_select.energy_v3_")
                )
            if "event" in node:
                assert node["event"] == "energy_v3_preview_result"
    for adapter in ("energy_v3_apply_solax", "energy_v3_apply_deye"):
        assert all("action" not in n and "event" not in n for n in action_nodes(package["script"][adapter]["sequence"]))


def test_state_ownership_and_startup_does_not_reset_replay_watermark():
    package = load_package()
    assert package["script"]["energy_v3_submit"]["mode"] == "queued"
    assert package["script"]["energy_v3_reconcile"]["mode"] == "queued"
    for automation in package["automation"]:
        calls = [n["action"] for n in action_nodes(automation["actions"]) if "action" in n]
        assert "script.energy_v3_reconcile" in calls
        assert not any("apply_" in action for action in calls)
    startup = package["automation"][0]["actions"][0]
    assert "x=true" in str(startup)
    assert "dict(prior" in str(startup)


def test_every_template_parses():
    from jinja2 import Environment

    environment = Environment()

    def visit(value):
        if isinstance(value, str) and ("{{" in value or "{%" in value):
            environment.parse(value)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(load_package())
