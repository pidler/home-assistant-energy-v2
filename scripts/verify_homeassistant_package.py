from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "homeassistant" / "packages" / "energy_v2_helpers.yaml"

REQUIRED_HELPERS: dict[str, set[str]] = {
    "input_boolean": {
        "energy_v2_enabled",
        "energy_v2_shadow_mode",
        "energy_v2_export_enabled",
        "energy_v2_service_mode",
        "energy_v2_safe_to_enable",
    },
    "input_select": {
        "energy_v2_strategy",
        "energy_v2_requested_mode",
        "energy_v2_actual_mode",
        "energy_v2_app_status",
        "energy_v2_flow_state",
        "energy_v2_export_limit_state",
    },
    "input_text": {
        "energy_v2_last_fault",
        "energy_v2_last_decision",
        "energy_v2_active_conflicts",
        "energy_v2_last_evaluation_error",
        "energy_v2_flow_summary",
        "energy_v2_flow_warning",
        "energy_v2_flow_violation",
        "energy_v2_export_limit_summary",
    },
    "input_datetime": {
        "energy_v2_heartbeat",
        "energy_v2_last_successful_evaluation",
        "energy_v2_last_flow_violation",
        "energy_v2_last_export_average_violation",
        "energy_v2_last_valid_export_sample",
    },
    "input_number": {
        "energy_v2_deye_fv_ledger",
        "energy_v2_solax_fv_ledger",
        "energy_v2_instant_grid_export_w",
        "energy_v2_rolling_15min_export_w",
        "energy_v2_export_window_covered_s",
        "energy_v2_export_sample_age_s",
    },
}

EXPECTED_INITIALS: dict[tuple[str, str], str] = {
    ("input_boolean", "energy_v2_enabled"): "false",
    ("input_boolean", "energy_v2_shadow_mode"): "true",
    ("input_boolean", "energy_v2_export_enabled"): "false",
    ("input_boolean", "energy_v2_service_mode"): "false",
    ("input_boolean", "energy_v2_safe_to_enable"): "false",
    ("input_select", "energy_v2_strategy"): "SUMMER_NO_GRID_CHARGE",
    ("input_select", "energy_v2_requested_mode"): "DISABLED",
    ("input_select", "energy_v2_actual_mode"): "DISABLED",
    ("input_select", "energy_v2_app_status"): "STARTING",
    ("input_select", "energy_v2_flow_state"): "UNKNOWN",
    ("input_select", "energy_v2_export_limit_state"): "UNKNOWN",
    ("input_number", "energy_v2_deye_fv_ledger"): "0",
    ("input_number", "energy_v2_solax_fv_ledger"): "0",
    ("input_number", "energy_v2_instant_grid_export_w"): "0",
    ("input_number", "energy_v2_rolling_15min_export_w"): "0",
    ("input_number", "energy_v2_export_window_covered_s"): "0",
    ("input_number", "energy_v2_export_sample_age_s"): "0",
}

KEY_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_]+):(?: +(?P<value>.*))?$")


def parse_package(text: str) -> tuple[dict[str, set[str]], dict[tuple[str, str], str], list[str]]:
    sections: dict[str, set[str]] = {}
    initials: dict[tuple[str, str], str] = {}
    errors: list[str] = []
    current_section: str | None = None
    current_helper: str | None = None
    seen_at_indent: dict[tuple[int, str | None], set[str]] = {}

    for line_no, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = KEY_RE.match(raw_line)
        if not match:
            continue
        indent = len(match.group("indent"))
        key = match.group("key")
        value = (match.group("value") or "").strip()

        if indent == 0:
            duplicate_scope = (indent, None)
        elif indent == 2:
            duplicate_scope = (indent, current_section)
        else:
            duplicate_scope = (indent, f"{current_section}.{current_helper}")
        seen = seen_at_indent.setdefault(duplicate_scope, set())
        if key in seen:
            errors.append(f"duplicate key at line {line_no}: {key}")
        seen.add(key)

        if indent == 0:
            current_section = key
            current_helper = None
            sections.setdefault(key, set())
        elif indent == 2 and current_section:
            current_helper = key
            sections.setdefault(current_section, set()).add(key)
        elif indent == 4 and key == "initial" and current_section and current_helper:
            initials[(current_section, current_helper)] = value

    return sections, initials, errors


def main() -> int:
    text = PACKAGE.read_text(encoding="utf-8")
    sections, initials, errors = parse_package(text)

    for section, helpers in REQUIRED_HELPERS.items():
        missing = sorted(helpers - sections.get(section, set()))
        for helper in missing:
            errors.append(f"missing helper: {section}.{helper}")

    for helper, expected in EXPECTED_INITIALS.items():
        actual = initials.get(helper)
        if actual != expected:
            errors.append(f"invalid initial for {helper[0]}.{helper[1]}: expected {expected!r}, got {actual!r}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"Home Assistant package is structurally valid: {PACKAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
