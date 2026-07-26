from __future__ import annotations

from apps.energy_v2.telemetry import parse_bool_state, parse_float_state


def test_parse_float_numeric_value() -> None:
    assert parse_float_state("12.5") == 12.5


def test_parse_float_unknown() -> None:
    assert parse_float_state("unknown") is None


def test_parse_float_unavailable() -> None:
    assert parse_float_state("unavailable") is None


def test_parse_float_none() -> None:
    assert parse_float_state(None) is None


def test_parse_float_non_numeric_text() -> None:
    assert parse_float_state("not-a-number") is None


def test_parse_float_negative_power() -> None:
    assert parse_float_state("-10482") == -10482.0


def test_parse_bool_on_off() -> None:
    assert parse_bool_state("on") is True
    assert parse_bool_state("off") is False

