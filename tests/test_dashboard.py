from __future__ import annotations

from scripts.verify_dashboard import main as verify_dashboard


def test_dashboard_yaml_is_safe_and_structurally_valid() -> None:
    assert verify_dashboard() == 0
