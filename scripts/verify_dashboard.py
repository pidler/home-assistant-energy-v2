from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "homeassistant" / "dashboards" / "energy_v2.yaml"
DOCS = ROOT / "docs" / "dashboard.md"

ALLOWED_CARD_TYPES = {
    "conditional",
    "entities",
    "entity",
    "gauge",
    "grid",
    "history-graph",
    "horizontal-stack",
    "markdown",
    "statistics-graph",
    "tile",
    "vertical-stack",
}

REQUIRED_VIEWS = ("Přehled", "Diagnostika", "Nastavení")
FORBIDDEN_KEYS = {"tap_action", "hold_action", "double_tap_action", "service"}
FORBIDDEN_STRINGS = {
    "call-service",
    "button.press",
    "switch.turn_on",
    "switch.turn_off",
    "input_boolean.turn_on",
    "input_boolean.turn_off",
    "input_select.select_option",
    "input_number.set_value",
    "modbus.write_register",
    "solarman.write",
}
READ_ONLY_ACTUATOR_CARD_TYPES = {"history-graph", "markdown"}
ENTITY_RE = re.compile(r"\b[a-zA-Z_]+\.[a-zA-Z0-9_]+\b")
VIEW_TITLE_RE = re.compile(r"^  - title: (?P<title>.+)$", re.MULTILINE)
CARD_TYPE_RE = re.compile(r"^\s+- type: (?P<type>.+)$", re.MULTILINE)
FORBIDDEN_KEY_RE = re.compile(r"^\s*(tap_action|hold_action|double_tap_action|service):", re.MULTILINE)


def load_config_entities() -> tuple[set[str], set[str]]:
    sys.path.insert(0, str(ROOT))
    from apps.energy_v2.config import DEFAULT_CONFLICTING_AUTOMATIONS, ENTITY_IDS, OWNED_ACTUATORS

    known = set(ENTITY_IDS.values())
    known.update(DEFAULT_CONFLICTING_AUTOMATIONS)
    known.update(OWNED_ACTUATORS)
    return known, set(OWNED_ACTUATORS)


def walk(value: Any) -> list[Any]:
    nodes = [value]
    if isinstance(value, dict):
        for child in value.values():
            nodes.extend(walk(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(walk(child))
    return nodes


def collect_entities(value: Any) -> set[str]:
    entities: set[str] = set()
    for node in walk(value):
        if isinstance(node, dict):
            entity = node.get("entity")
            if isinstance(entity, str):
                entities.add(entity)
        elif isinstance(node, str):
            entities.update(ENTITY_RE.findall(node))
    return entities


def card_type(card: Any) -> str | None:
    if isinstance(card, dict) and isinstance(card.get("type"), str):
        return card["type"]
    return None


def validate_cards(value: Any, errors: list[str], parent_card_type: str | None = None) -> None:
    if isinstance(value, dict):
        current_card_type = card_type(value) or parent_card_type
        if card_type(value):
            if value["type"].startswith("custom:"):
                errors.append(f"custom card is not allowed: {value['type']}")
            elif value["type"] not in ALLOWED_CARD_TYPES:
                errors.append(f"unsupported card type: {value['type']}")
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                errors.append(f"forbidden action key: {key}")
            if key == "entity" and isinstance(child, str) and child in owned_actuators:
                if current_card_type not in READ_ONLY_ACTUATOR_CARD_TYPES:
                    errors.append(f"owned actuator {child} used in active-capable card {current_card_type}")
            validate_cards(child, errors, current_card_type)
    elif isinstance(value, list):
        for child in value:
            validate_cards(child, errors, parent_card_type)
    elif isinstance(value, str):
        lowered = value.lower()
        for forbidden in FORBIDDEN_STRINGS:
            if forbidden in lowered:
                errors.append(f"forbidden service/action reference: {forbidden}")


def load_yaml_with_optional_pyyaml(text: str) -> dict[str, Any] | None:
    try:
        import yaml
    except ModuleNotFoundError:
        return None
    data = yaml.safe_load(text)
    if isinstance(data, dict):
        return data
    return {}


def validate_text_fallback(text: str, errors: list[str], known_entities: set[str], actuator_entities: set[str]) -> None:
    view_titles = tuple(match.group("title") for match in VIEW_TITLE_RE.finditer(text))
    if view_titles != REQUIRED_VIEWS:
        errors.append(f"dashboard views must be exactly {REQUIRED_VIEWS!r}, got {view_titles!r}")

    for match in CARD_TYPE_RE.finditer(text):
        card = match.group("type").strip()
        if card.startswith("custom:"):
            errors.append(f"custom card is not allowed: {card}")
        elif card not in ALLOWED_CARD_TYPES:
            errors.append(f"unsupported card type: {card}")

    for match in FORBIDDEN_KEY_RE.finditer(text):
        errors.append(f"forbidden action key: {match.group(1)}")

    lowered = text.lower()
    for forbidden in FORBIDDEN_STRINGS:
        if forbidden in lowered:
            errors.append(f"forbidden service/action reference: {forbidden}")

    unknown_entities = sorted(set(ENTITY_RE.findall(text)) - known_entities)
    for entity in unknown_entities:
        errors.append(f"unknown entity referenced by dashboard: {entity}")

    for actuator in actuator_entities:
        if actuator in text:
            line_no = text[: text.index(actuator)].count("\n") + 1
            nearby = "\n".join(text.splitlines()[max(line_no - 8, 0) : line_no + 2])
            if "type: history-graph" not in nearby and "{{ states('" not in nearby:
                errors.append(f"owned actuator {actuator} is not clearly read-only near line {line_no}")


def main() -> int:
    errors: list[str] = []
    if not DASHBOARD.exists():
        print(f"missing dashboard: {DASHBOARD}", file=sys.stderr)
        return 1
    text = DASHBOARD.read_text(encoding="utf-8")
    known_entities, actuator_entities = load_config_entities()
    data = load_yaml_with_optional_pyyaml(text)
    if data is None:
        validate_text_fallback(text, errors, known_entities, actuator_entities)
    elif not isinstance(data, dict):
        errors.append("dashboard YAML root must be a mapping")
        data = {}

    if data is not None:
        views = data.get("views")
        if not isinstance(views, list):
            errors.append("dashboard must contain views list")
            views = []
        view_titles = tuple(view.get("title") for view in views if isinstance(view, dict))
        if view_titles != REQUIRED_VIEWS:
            errors.append(f"dashboard views must be exactly {REQUIRED_VIEWS!r}, got {view_titles!r}")

        global owned_actuators
        owned_actuators = actuator_entities
        entities = collect_entities(data)
        unknown_entities = sorted(entities - known_entities)
        for entity in unknown_entities:
            errors.append(f"unknown entity referenced by dashboard: {entity}")

        validate_cards(data, errors)

    if DOCS.exists():
        docs_text = DOCS.read_text(encoding="utf-8")
        if "homeassistant/dashboards/energy_v2.yaml" not in docs_text:
            errors.append("docs/dashboard.md does not reference dashboard YAML path")
    else:
        errors.append("missing docs/dashboard.md")

    if errors:
        for error in sorted(set(errors)):
            print(error, file=sys.stderr)
        return 1
    print(f"Dashboard is structurally valid: {DASHBOARD}")
    return 0


owned_actuators: set[str] = set()


if __name__ == "__main__":
    raise SystemExit(main())
