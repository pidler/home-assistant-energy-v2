from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.energy_v2.config import ENTITY_IDS
from apps.energy_v2.load_model import estimate_whole_site_load
from apps.energy_v2.models import TelemetryClass, TelemetryQuality
from apps.energy_v2.telemetry import (
    TelemetryFreshnessConfig,
    TelemetryReader,
    parse_bool_state,
    parse_float_state,
    validate_freshness_config,
)


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


def test_parse_float_nan_rejected() -> None:
    assert parse_float_state("NaN") is None


def test_parse_float_positive_infinity_rejected() -> None:
    assert parse_float_state("inf") is None


def test_parse_float_negative_infinity_rejected() -> None:
    assert parse_float_state("-inf") is None


def test_parse_bool_on_off() -> None:
    assert parse_bool_state("on") is True
    assert parse_bool_state("off") is False


class SampleReader:
    def __init__(self, value: object) -> None:
        self.value = value

    def get_state(self, entity_id: str, **kwargs: object) -> object:
        return self.value


class MappingReader:
    def __init__(self, states: dict[str, object]) -> None:
        self.states = states

    def get_state(self, entity_id: str, **kwargs: object) -> object:
        value = self.states.get(entity_id)
        if kwargs.get("attribute") == "all":
            return value
        if isinstance(value, dict):
            return value.get("state")
        return value


NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def timestamped(value: object, age_s: float) -> dict[str, object]:
    return {"state": str(value), "last_updated": (NOW - timedelta(seconds=age_s)).isoformat()}


def control_states() -> dict[str, object]:
    return {
        ENTITY_IDS["solax_inverter_power"]: timestamped(1000, 15),
        ENTITY_IDS["deye_inverter_power"]: timestamped(200, 5),
        ENTITY_IDS["solax_measured_power"]: timestamped(0, 3600),
        ENTITY_IDS["solax_battery_power"]: timestamped(0, 15),
        ENTITY_IDS["deye_battery_power"]: timestamped(0, 5),
        ENTITY_IDS["solax_soc"]: timestamped(80, 1800),
        ENTITY_IDS["deye_soc"]: timestamped(70, 1800),
        ENTITY_IDS["solax_pv_power"]: timestamped(0, 7200),
        ENTITY_IDS["deye_pv_power"]: timestamped(0, 7200),
        ENTITY_IDS["deye_connection"]: "on",
    }


def control_snapshot(states: dict[str, object] | None = None):
    return TelemetryReader(MappingReader(states or control_states())).control_snapshot(now=NOW)


def test_numeric_sample_reports_timestamp_age_and_freshness() -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    reader = TelemetryReader(
        SampleReader({"state": "123", "last_updated": (now - timedelta(seconds=4)).isoformat()}),
        {"power": "sensor.power"},
    )
    result = reader.numeric_sample("power", now, 5)
    assert result.value == 123
    assert result.age_s == 4
    assert result.fresh
    assert result.quality is TelemetryQuality.VALID


def test_numeric_sample_reports_stale_quality() -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    reader = TelemetryReader(
        SampleReader({"state": "123", "last_updated": (now - timedelta(seconds=30)).isoformat()}),
        {"power": "sensor.power"},
    )
    result = reader.numeric_sample("power", now, 5)
    assert not result.fresh
    assert result.quality is TelemetryQuality.STALE


def test_timestamped_dict_without_timestamp_is_invalid_not_fresh() -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    reader = TelemetryReader(SampleReader({"state": "123", "attributes": {}}), {"power": "sensor.power"})

    result = reader.numeric_sample("power", now, 5)

    assert result.value == 123
    assert result.timestamp is None
    assert result.age_s is None
    assert not result.fresh
    assert result.quality is TelemetryQuality.INVALID


def test_solax_pv_stable_zero_is_valid_with_healthy_source() -> None:
    sample = control_snapshot().solax_pv_power
    assert sample is not None
    assert sample.telemetry_class is TelemetryClass.STABLE_ZERO
    assert sample.age_s == 7200
    assert sample.source_healthy
    assert sample.effective_fresh
    assert sample.quality is TelemetryQuality.VALID


def test_deye_pv_stable_zero_is_valid_with_healthy_source() -> None:
    sample = control_snapshot().deye_pv_power
    assert sample is not None
    assert sample.age_s == 7200
    assert sample.effective_fresh
    assert sample.quality is TelemetryQuality.VALID


def test_old_nonzero_pv_is_stale_even_with_healthy_source() -> None:
    states = control_states()
    states[ENTITY_IDS["deye_pv_power"]] = timestamped(500, 120)
    pv = control_snapshot(states).deye_pv_power
    assert pv is not None
    assert pv.source_healthy
    assert not pv.effective_fresh
    assert pv.quality is TelemetryQuality.STALE


def test_unchanged_soc_is_valid_when_corresponding_source_is_healthy() -> None:
    snapshot = control_snapshot()
    assert snapshot.solax_soc.age_s == 1800
    assert snapshot.deye_soc.age_s == 1800
    assert snapshot.solax_soc.effective_fresh
    assert snapshot.deye_soc.effective_fresh


def test_old_zero_grid_is_valid_when_solax_source_is_healthy() -> None:
    grid = control_snapshot().whole_site_grid_power
    assert grid.age_s == 3600
    assert grid.effective_fresh
    assert grid.effective_timestamp == NOW - timedelta(seconds=15)


def test_zero_grid_at_age_limit_uses_newer_solax_health_timestamp() -> None:
    states = control_states()
    states[ENTITY_IDS["solax_inverter_power"]] = timestamped(1000, 10)
    states[ENTITY_IDS["solax_measured_power"]] = timestamped(0, 60)

    grid = control_snapshot(states).whole_site_grid_power

    assert grid.quality is TelemetryQuality.VALID
    assert grid.effective_timestamp == NOW - timedelta(seconds=10)


def test_five_minute_old_zero_grid_uses_recent_solax_health_timestamp() -> None:
    states = control_states()
    states[ENTITY_IDS["solax_inverter_power"]] = timestamped(1000, 10)
    states[ENTITY_IDS["solax_measured_power"]] = timestamped(0, 300)

    grid = control_snapshot(states).whole_site_grid_power

    assert grid.quality is TelemetryQuality.VALID
    assert grid.effective_timestamp == NOW - timedelta(seconds=10)


def test_old_nonzero_grid_is_stale_even_with_healthy_solax_source() -> None:
    states = control_states()
    states[ENTITY_IDS["solax_measured_power"]] = timestamped(100, 120)
    grid = control_snapshot(states).whole_site_grid_power
    assert grid.source_healthy
    assert not grid.effective_fresh
    assert grid.quality is TelemetryQuality.STALE


def test_nonzero_export_at_age_limit_is_stale_despite_healthy_solax_source() -> None:
    states = control_states()
    states[ENTITY_IDS["solax_measured_power"]] = timestamped(3000, 60)

    grid = control_snapshot(states).whole_site_grid_power

    assert grid.source_healthy
    assert not grid.effective_fresh
    assert grid.quality is TelemetryQuality.STALE
    assert grid.effective_timestamp == NOW - timedelta(seconds=60)


def test_nonzero_import_at_age_limit_is_stale_despite_healthy_solax_source() -> None:
    states = control_states()
    states[ENTITY_IDS["solax_measured_power"]] = timestamped(-1000, 60)

    grid = control_snapshot(states).whole_site_grid_power

    assert grid.source_healthy
    assert not grid.effective_fresh
    assert grid.quality is TelemetryQuality.STALE
    assert grid.effective_timestamp == NOW - timedelta(seconds=60)


def test_whole_site_load_uses_stable_zero_grid_effective_timestamp() -> None:
    states = control_states()
    states[ENTITY_IDS["solax_inverter_power"]] = timestamped(1000, 10)
    states[ENTITY_IDS["deye_inverter_power"]] = timestamped(200, 5)
    states[ENTITY_IDS["solax_measured_power"]] = timestamped(0, 60)
    snapshot = control_snapshot(states)

    result = estimate_whole_site_load(
        snapshot.solax_inverter_power,
        snapshot.deye_inverter_power,
        snapshot.whole_site_grid_power,
    )

    assert result.quality is TelemetryQuality.VALID
    assert result.timestamp_skew_s == 5
    assert result.load_w == 1200


def test_whole_site_load_rejects_zero_grid_when_solax_source_is_dead() -> None:
    states = control_states()
    states[ENTITY_IDS["solax_inverter_power"]] = timestamped(1000, 61)
    states[ENTITY_IDS["solax_battery_power"]] = timestamped(0, 61)
    snapshot = control_snapshot(states)

    result = estimate_whole_site_load(
        snapshot.solax_inverter_power,
        snapshot.deye_inverter_power,
        snapshot.whole_site_grid_power,
    )

    assert result.quality is TelemetryQuality.STALE
    assert result.load_w is None


def test_frozen_solax_source_makes_dependent_states_stale() -> None:
    states = control_states()
    states[ENTITY_IDS["solax_inverter_power"]] = timestamped(1000, 61)
    states[ENTITY_IDS["solax_battery_power"]] = timestamped(0, 61)
    snapshot = control_snapshot(states)
    assert not snapshot.solax_source_healthy
    assert snapshot.solax_pv_power is not None
    assert snapshot.solax_pv_power.quality is TelemetryQuality.STALE
    assert snapshot.solax_soc.quality is TelemetryQuality.STALE
    assert snapshot.whole_site_grid_power.quality is TelemetryQuality.STALE


def test_frozen_deye_source_makes_dependent_states_stale() -> None:
    states = control_states()
    states[ENTITY_IDS["deye_inverter_power"]] = timestamped(200, 31)
    states[ENTITY_IDS["deye_battery_power"]] = timestamped(0, 31)
    snapshot = control_snapshot(states)
    assert not snapshot.deye_source_healthy
    assert snapshot.deye_pv_power is not None
    assert snapshot.deye_pv_power.quality is TelemetryQuality.STALE
    assert snapshot.deye_soc.quality is TelemetryQuality.STALE


def test_stale_battery_feedback_remains_unverified_even_if_sibling_is_fresh() -> None:
    states = control_states()
    states[ENTITY_IDS["deye_battery_power"]] = timestamped(0, 31)
    battery = control_snapshot(states).deye_battery_power
    assert battery.source_healthy
    assert not battery.effective_fresh
    assert battery.quality is TelemetryQuality.STALE
    assert battery.effective_timestamp == NOW - timedelta(seconds=31)


def test_soc_outside_zero_to_one_hundred_is_invalid() -> None:
    for value in (-1, 101):
        states = control_states()
        states[ENTITY_IDS["solax_soc"]] = timestamped(value, 1)
        assert control_snapshot(states).solax_soc.quality is TelemetryQuality.INVALID


def test_negative_pv_is_invalid() -> None:
    states = control_states()
    states[ENTITY_IDS["deye_pv_power"]] = timestamped(-1, 1)
    pv = control_snapshot(states).deye_pv_power
    assert pv is not None
    assert pv.quality is TelemetryQuality.INVALID


def test_unavailable_state_is_invalid() -> None:
    states = control_states()
    states[ENTITY_IDS["deye_pv_power"]] = timestamped("unavailable", 1)
    pv = control_snapshot(states).deye_pv_power
    assert pv is not None
    assert pv.quality is TelemetryQuality.INVALID


def test_non_numeric_control_state_is_invalid() -> None:
    states = control_states()
    states[ENTITY_IDS["deye_inverter_power"]] = timestamped("not-a-number", 1)
    assert control_snapshot(states).deye_inverter_power.quality is TelemetryQuality.INVALID


def test_missing_state_is_missing() -> None:
    states = control_states()
    del states[ENTITY_IDS["deye_pv_power"]]
    pv = control_snapshot(states).deye_pv_power
    assert pv is not None
    assert pv.quality is TelemetryQuality.MISSING


def test_no_timestamp_and_no_source_health_is_invalid() -> None:
    states = control_states()
    states[ENTITY_IDS["solax_inverter_power"]] = {"state": "1000"}
    states[ENTITY_IDS["solax_battery_power"]] = {"state": "0"}
    snapshot = control_snapshot(states)
    assert not snapshot.solax_source_healthy
    assert snapshot.solax_inverter_power.quality is TelemetryQuality.INVALID


def test_explicit_deye_disconnect_overrides_recent_power_samples() -> None:
    states = control_states()
    states[ENTITY_IDS["deye_connection"]] = "off"
    snapshot = control_snapshot(states)
    assert not snapshot.deye_source_healthy
    assert snapshot.deye_battery_power.quality is TelemetryQuality.STALE


def test_freshness_configuration_defaults_match_production_cadence() -> None:
    config = TelemetryFreshnessConfig()
    assert config.solax_fast_power_max_age_s == 60
    assert config.deye_fast_power_max_age_s == 30
    assert config.fast_input_max_skew_s == 20


def test_freshness_configuration_rejects_non_positive_or_non_finite_windows() -> None:
    config = TelemetryFreshnessConfig(solax_fast_power_max_age_s=0, deye_source_health_window_s=float("inf"))
    errors = validate_freshness_config(config)
    assert "solax_fast_power_max_age_s must be finite and greater than zero" in errors
    assert "deye_source_health_window_s must be finite and greater than zero" in errors
