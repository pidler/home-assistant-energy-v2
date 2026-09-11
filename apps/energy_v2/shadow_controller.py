"""Phase 4 shadow control orchestration. No physical execution dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import isfinite

from .adapters.deye import DeyeShadowAdapter
from .adapters.solax import SolaxShadowAdapter
from .allocator import AllocationResult, BatteryAvailability, ShadowPowerAllocator
from .export_budget import ExportBudget, FixedQuarterExportTracker, trailing_window_budget
from .flow import FlowDebouncer, FlowSnapshot, FlowState, FlowThresholds, RollingExportAverageTracker
from .load_model import LoadEstimate, LoadModelParameters, estimate_whole_site_load
from .models import (
    BatteryAction,
    CommandResult,
    CommandStatus,
    ControlTelemetrySnapshot,
    SiteCommand,
    TelemetryQuality,
)


@dataclass(frozen=True)
class ShadowControlResult:
    load: LoadEstimate
    fixed_quarter_budget: ExportBudget
    trailing_budget: ExportBudget
    allocation: AllocationResult
    deye: CommandResult
    solax: CommandResult
    grid_actual_w: float | None
    grid_error_w: float | None
    runtime_anti_transfer_state: str
    command_status: CommandStatus
    reason: str
    budget_diagnostic_target_w: float
    total_pv_power_w: float | None
    pv_quality: TelemetryQuality
    pv_reason: str


@dataclass(frozen=True)
class PvEstimate:
    power_w: float | None
    quality: TelemetryQuality
    reason: str


@dataclass
class ShadowControlCore:
    load_parameters: LoadModelParameters = field(default_factory=LoadModelParameters)
    allocator: ShadowPowerAllocator = field(default_factory=ShadowPowerAllocator)
    deye_adapter: DeyeShadowAdapter = field(default_factory=DeyeShadowAdapter)
    solax_adapter: SolaxShadowAdapter = field(default_factory=SolaxShadowAdapter)
    fixed_tracker: FixedQuarterExportTracker = field(default_factory=FixedQuarterExportTracker)
    trailing_tracker: RollingExportAverageTracker = field(
        default_factory=lambda: RollingExportAverageTracker(window_s=900.0, max_sample_age_s=30.0)
    )
    transfer_threshold_w: float = 300.0
    transfer_confirmation_s: float = 10.0
    runtime_flow_debouncer: FlowDebouncer = field(init=False)

    def __post_init__(self) -> None:
        self.runtime_flow_debouncer = self._new_runtime_flow_debouncer()

    def evaluate(
        self,
        telemetry: ControlTelemetrySnapshot,
        command: SiteCommand,
        *,
        deye: BatteryAvailability,
        solax: BatteryAvailability,
    ) -> ShadowControlResult:
        now = telemetry.sampled_at
        pv = self._total_pv(telemetry)
        load = estimate_whole_site_load(
            telemetry.solax_inverter_power,
            telemetry.deye_inverter_power,
            telemetry.whole_site_grid_power,
            self.load_parameters,
        )
        grid_actual = telemetry.whole_site_grid_power.value
        if telemetry.whole_site_grid_power.quality is TelemetryQuality.VALID and grid_actual is not None:
            export_w = max(grid_actual, 0.0)
            fixed = self.fixed_tracker.add_sample(now, export_w)
            rolling = self.trailing_tracker.add_sample(now, export_w)
        else:
            fixed = self.fixed_tracker.budget(now)
            rolling = self.trailing_tracker.average(now)
        trailing = trailing_window_budget(rolling)
        budget_w = min(fixed.budget_power_w, trailing.budget_power_w, command.max_export_w)
        if (
            load.quality is not TelemetryQuality.VALID
            or load.load_w is None
            or pv.quality is not TelemetryQuality.VALID
            or pv.power_w is None
        ):
            allocation = self.allocator.allocate(
                command,
                now=now,
                site_load_w=float("nan"),
                pv_power_w=float("nan"),
                deye=deye,
                solax=solax,
                budget_power_w=None,
            )
        else:
            allocation = self.allocator.allocate(
                command,
                now=now,
                site_load_w=load.load_w,
                pv_power_w=pv.power_w,
                deye=deye,
                solax=solax,
                budget_power_w=None,
            )

        runtime_transfer = self._runtime_transfer(telemetry, command.transfer_allowed)
        if runtime_transfer.startswith("FAULT"):
            allocation = replace(
                allocation,
                deye=replace(allocation.deye, action=BatteryAction.HOLD, target_power_w=0.0),
                solax=replace(allocation.solax, action=BatteryAction.HOLD, target_power_w=0.0),
                status=CommandStatus.FAULT,
                anti_transfer_state="FAULT",
                break_before_make_state="RUNTIME_TRANSFER_RAMP_DOWN",
            )
        elif runtime_transfer != "CLEAR":
            allocation = replace(
                allocation,
                deye=replace(allocation.deye, action=BatteryAction.HOLD, target_power_w=0.0),
                solax=replace(allocation.solax, action=BatteryAction.HOLD, target_power_w=0.0),
                status=CommandStatus.UNVERIFIED,
                anti_transfer_state="UNVERIFIED",
                break_before_make_state="FEEDBACK_UNVERIFIED",
            )
        grid_error = None if grid_actual is None else allocation.allowed_grid_target_w - grid_actual
        deye_actual = (
            telemetry.deye_battery_power.value
            if telemetry.deye_battery_power.quality is TelemetryQuality.VALID
            else None
        )
        solax_actual = (
            telemetry.solax_battery_power.value
            if telemetry.solax_battery_power.quality is TelemetryQuality.VALID
            else None
        )
        deye_result = self.deye_adapter.translate(allocation.deye, deye_actual)
        solax_result = self.solax_adapter.translate(
            allocation.solax,
            solax_actual,
            grid_error_w=grid_error or 0.0,
            grid_target_w=allocation.allowed_grid_target_w,
        )
        status = allocation.status
        reasons = [allocation.saturation_reason]
        if runtime_transfer.startswith("FAULT"):
            status = CommandStatus.FAULT
            reasons.append(runtime_transfer)
        elif runtime_transfer != "CLEAR":
            status = CommandStatus.UNVERIFIED
            reasons.append(runtime_transfer)
        if load.quality is not TelemetryQuality.VALID:
            status = CommandStatus.FAULT
            reasons.append(load.reason)
        if pv.quality is not TelemetryQuality.VALID:
            status = CommandStatus.FAULT
            reasons.append(pv.reason)
        if status is not CommandStatus.FAULT and (
            deye_result.status is CommandStatus.UNVERIFIED or solax_result.status is CommandStatus.UNVERIFIED
        ):
            status = CommandStatus.UNVERIFIED
        return ShadowControlResult(
            load,
            fixed,
            trailing,
            allocation,
            deye_result,
            solax_result,
            grid_actual,
            grid_error,
            runtime_transfer,
            status,
            "; ".join(reason for reason in reasons if reason) or "Shadow command evaluated",
            min(command.grid_target_w, budget_w),
            pv.power_w,
            pv.quality,
            pv.reason,
        )

    def _runtime_transfer(self, telemetry: ControlTelemetrySnapshot, transfer_allowed: bool) -> str:
        if transfer_allowed:
            return "CLEAR"
        samples = (telemetry.deye_battery_power, telemetry.solax_battery_power)
        if any(sample.quality is not TelemetryQuality.VALID or not sample.effective_fresh for sample in samples):
            self.runtime_flow_debouncer = self._new_runtime_flow_debouncer()
            details = ", ".join(f"{sample.entity_id}={sample.quality.value}" for sample in samples)
            return f"UNVERIFIED: battery-power feedback is not fresh ({details})"
        deye = telemetry.deye_battery_power.value
        solax = telemetry.solax_battery_power.value
        if deye is None or solax is None or not isfinite(deye) or not isfinite(solax):
            self.runtime_flow_debouncer = self._new_runtime_flow_debouncer()
            return "UNVERIFIED: battery-power feedback is missing or non-finite"
        assessment = self.runtime_flow_debouncer.assess(
            FlowSnapshot(
                grid_import_w=0.0,
                grid_export_w=0.0,
                solax_battery_charging_w=max(solax, 0.0),
                solax_battery_discharging_w=max(-solax, 0.0),
                deye_battery_charging_w=max(deye, 0.0),
                deye_battery_discharging_w=max(-deye, 0.0),
                pv_power_w=0.0,
                house_load_w=0.0,
            ),
            telemetry.sampled_at,
        )
        if assessment.state in (FlowState.DEYE_TO_SOLAX, FlowState.SOLAX_TO_DEYE):
            if assessment.violations:
                return f"FAULT: {assessment.violations[0]}"
            return f"TRANSFER_SUSPECTED: {assessment.state.value} awaiting {self.transfer_confirmation_s:.0f} s"
        return "CLEAR"

    def _new_runtime_flow_debouncer(self) -> FlowDebouncer:
        return FlowDebouncer(
            FlowThresholds(
                battery_flow_warning_w=self.transfer_threshold_w,
                violation_persistence_s=self.transfer_confirmation_s,
            )
        )

    def _total_pv(self, telemetry: ControlTelemetrySnapshot) -> PvEstimate:
        samples = (telemetry.solax_pv_power, telemetry.deye_pv_power)
        if any(sample is None for sample in samples):
            return PvEstimate(None, TelemetryQuality.MISSING, "Missing total-PV telemetry sample")
        present = tuple(sample for sample in samples if sample is not None)
        invalid = tuple(
            sample.entity_id
            for sample in present
            if sample.value is None or not isfinite(sample.value) or sample.value < 0 or sample.timestamp is None
        )
        if invalid:
            return PvEstimate(None, TelemetryQuality.INVALID, f"Invalid PV inputs: {', '.join(invalid)}")
        stale = tuple(
            sample.entity_id
            for sample in present
            if sample.quality is not TelemetryQuality.VALID or not sample.effective_fresh
        )
        if stale:
            return PvEstimate(None, TelemetryQuality.STALE, f"Stale PV inputs: {', '.join(stale)}")
        timestamps = [
            sample.effective_timestamp or sample.timestamp for sample in present if sample.timestamp is not None
        ]
        skew_s = (max(timestamps) - min(timestamps)).total_seconds()
        allowed_skew_s = self.load_parameters.allowed_timestamp_skew_s
        if skew_s > allowed_skew_s:
            return PvEstimate(
                None,
                TelemetryQuality.SKEWED,
                f"PV input timestamp skew {skew_s:.1f} s exceeds {allowed_skew_s:.1f} s",
            )
        return PvEstimate(
            sum(sample.value for sample in present if sample.value is not None),
            TelemetryQuality.VALID,
            "Validated SolaX plus DEYE PV power",
        )
