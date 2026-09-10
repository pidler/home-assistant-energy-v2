from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.energy_v2.charge_controller import ChargeTelemetry, DeyeChargeShadowController
from apps.energy_v2.models import ChargeShadowState

BASE = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def sample(
    seconds: int, *, soc: float = 91, pv: float = 1200, solax: float = 0, deye: float = 1000, voltage: float = 50
) -> ChargeTelemetry:
    return ChargeTelemetry(BASE + timedelta(seconds=seconds), soc, pv, solax, deye, voltage)


def enabled(controller: DeyeChargeShadowController, seconds: int, **kwargs: float) -> object:
    return controller.evaluate(sample(seconds, **kwargs), enabled=True)


def test_soc_below_stop_enters_solax_priority_with_zero_current() -> None:
    decision = enabled(DeyeChargeShadowController(), 0, soc=79)
    assert decision.state is ChargeShadowState.SOLAX_PRIORITY
    assert decision.recommended_current_a == 0


def test_waiting_between_soc_thresholds_and_for_low_pv() -> None:
    controller = DeyeChargeShadowController()
    assert enabled(controller, 0, soc=85).state is ChargeShadowState.WAITING_FOR_SOLAX
    assert enabled(controller, 5, soc=91, pv=999).state is ChargeShadowState.WAITING_FOR_SOLAX


def test_start_confirmation_requires_continuous_thirty_seconds() -> None:
    controller = DeyeChargeShadowController()
    assert enabled(controller, 0).state is ChargeShadowState.START_CONFIRMATION
    assert enabled(controller, 29).state is ChargeShadowState.START_CONFIRMATION
    assert enabled(controller, 30).state is ChargeShadowState.CHARGE_DEYE_FULL
    assert enabled(controller, 31, pv=999).recommended_current_a == 240  # low PV does not reduce full charge


def test_short_pv_drop_resets_start_confirmation() -> None:
    controller = DeyeChargeShadowController()
    enabled(controller, 0)
    assert enabled(controller, 20, pv=999).state is ChargeShadowState.WAITING_FOR_SOLAX
    assert enabled(controller, 21).state is ChargeShadowState.START_CONFIRMATION


def test_solax_discharge_without_actual_deye_charge_is_not_transfer() -> None:
    controller = DeyeChargeShadowController()
    enabled(controller, 0)
    enabled(controller, 30)
    decision = enabled(controller, 35, solax=-1000, deye=200)
    assert decision.state is ChargeShadowState.CHARGE_DEYE_FULL


def test_transfer_needs_ten_seconds_then_uses_real_charge_power() -> None:
    controller = DeyeChargeShadowController()
    enabled(controller, 0)
    enabled(controller, 30)
    assert (
        enabled(controller, 31, solax=-1200, deye=5100, voltage=52.7).state is ChargeShadowState.TRANSFER_CONFIRMATION
    )
    assert enabled(controller, 41, solax=-1200, deye=5100, voltage=52.7).state is ChargeShadowState.CHARGE_DEYE_LIMITED
    decision = enabled(controller, 42, solax=-1200, deye=5100, voltage=52.7)
    assert decision.recommended_current_a <= 240


def test_safe_power_below_minimum_recommends_zero() -> None:
    controller = DeyeChargeShadowController()
    enabled(controller, 0)
    enabled(controller, 30)
    enabled(controller, 31, solax=-900, deye=1000)
    decision = enabled(controller, 41, solax=-900, deye=1000)
    assert decision.recommended_current_a == 0


def test_normalized_deye_positive_is_used_without_second_sign_flip() -> None:
    controller = DeyeChargeShadowController()
    enabled(controller, 0)
    enabled(controller, 30)
    assert enabled(controller, 31, solax=-600, deye=-1000).state is ChargeShadowState.CHARGE_DEYE_FULL


def test_invalid_telemetry_faults_safely() -> None:
    controller = DeyeChargeShadowController()
    decision = controller.evaluate(sample(0, voltage=0), enabled=True)
    assert decision.state is ChargeShadowState.FAULT
    assert decision.recommended_current_a == 0


def test_fault_recovers_only_after_sixty_seconds_of_stable_telemetry() -> None:
    controller = DeyeChargeShadowController()
    enabled(controller, 0, voltage=0)
    assert enabled(controller, 59).state is ChargeShadowState.FAULT
    assert enabled(controller, 60).state is ChargeShadowState.START_CONFIRMATION


def test_irregular_window_samples_remain_time_weighted() -> None:
    controller = DeyeChargeShadowController()
    enabled(controller, 0)
    enabled(controller, 30)
    enabled(controller, 31, solax=-1000, deye=5000)
    enabled(controller, 34, solax=-1000, deye=5000)
    decision = enabled(controller, 41, solax=-1000, deye=5000)
    assert decision.state is ChargeShadowState.CHARGE_DEYE_LIMITED
