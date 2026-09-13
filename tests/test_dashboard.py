from __future__ import annotations

from pathlib import Path

import yaml

from scripts.verify_dashboard import main as verify_dashboard

ROOT = Path(__file__).parents[1]
DASHBOARD = ROOT / "homeassistant" / "dashboards" / "energy_v2.yaml"


def test_dashboard_yaml_is_safe_and_structurally_valid() -> None:
    assert verify_dashboard() == 0


def test_dashboard_has_six_redesigned_views() -> None:
    dashboard = yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))

    assert [view["title"] for view in dashboard["views"]] == [
        "Přehled",
        "Trading / Planner",
        "Baterie",
        "Grid / Export",
        "Diagnostika",
        "Pokročilé",
    ]


def test_overview_uses_authoritative_whole_site_load_and_phase5_placeholder() -> None:
    dashboard = yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))
    overview = dashboard["views"][0]
    text = str(overview)

    assert "input_number.energy_v2_whole_site_load_w" in text
    assert "Phase 5A planner" in text
    assert "Runtime:** NOT CONNECTED" in text
    assert "ENERGY V2 Phase 2" not in text


def test_overview_does_not_treat_unavailable_soc_as_zero_below_floor() -> None:
    dashboard = yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))
    reserve_card = next(card for card in dashboard["views"][0]["cards"] if card.get("title") == "Stav rezerv baterií")
    template = reserve_card["content"]

    assert "states('sensor.deye_battery') | float(0)" not in template
    assert "states('sensor.solax_battery_capacity') | float(0)" not in template
    assert "{% if is_number(deye_soc_raw) %}" in template
    assert "{% if is_number(solax_soc_raw) %}" in template
    assert "deye_soc_raw == 'unknown'" in template
    assert "deye_soc_raw == 'unavailable'" in template
    assert "solax_soc_raw == 'unknown'" in template
    assert "solax_soc_raw == 'unavailable'" in template
    assert "DEYE SOC není dostupné" in template
    assert "SolaX SOC není dostupné" in template
    numeric_guard = template.index("{% if is_number(deye_soc_raw) %}")
    below_floor_check = template.index("{% if deye_soc < 10 %}")
    assert numeric_guard < below_floor_check


def test_dashboard_keeps_all_physical_entities_read_only() -> None:
    text = DASHBOARD.read_text(encoding="utf-8")

    assert "number.deye_battery_max_charging_current" in text
    assert "switch.deye_battery_grid_charging" in text
    assert "switch.deye_export_surplus" in text
    assert "tap_action:" not in text
    assert "hold_action:" not in text
    assert "service:" not in text


def test_dashboard_has_no_direct_control_rows() -> None:
    dashboard = yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))
    controllable_domains = {
        "automation",
        "button",
        "input_boolean",
        "input_number",
        "input_select",
        "number",
        "script",
        "select",
        "switch",
    }

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") in {"entity", "entities", "tile"}:
                rows = [value.get("entity"), *value.get("entities", [])]
                for row in rows:
                    entity = row.get("entity") if isinstance(row, dict) else row
                    if isinstance(entity, str):
                        assert entity.split(".", 1)[0] not in controllable_domains
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(dashboard)
