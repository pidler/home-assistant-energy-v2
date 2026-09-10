"""Phase 4 shadow control orchestration. No physical execution dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .adapters.deye import DeyeShadowAdapter
from .adapters.solax import SolaxShadowAdapter
from .allocator import AllocationResult, BatteryAvailability, ShadowPowerAllocator
from .export_budget import ExportBudget, FixedQuarterExportTracker, trailing_window_budget
from .flow import RollingExportAverageTracker
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

    def evaluate(
        self,
        telemetry: ControlTelemetrySnapshot,
        command: SiteCommand,
        *,
        pv_power_w: float,
        deye: BatteryAvailability,
        solax: BatteryAvailability,
    ) -> ShadowControlResult:
        now = telemetry.sampled_at
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
        if load.quality is not TelemetryQuality.VALID or load.load_w is None:
            allocation = self.allocator.allocate(
                command,
                now=now,
                site_load_w=float("nan"),
                pv_power_w=pv_power_w,
                deye=deye,
                solax=solax,
                budget_power_w=None,
            )
        else:
            allocation = self.allocator.allocate(
                command,
                now=now,
                site_load_w=load.load_w,
                pv_power_w=pv_power_w,
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
        grid_error = None if grid_actual is None else allocation.allowed_grid_target_w - grid_actual
        deye_result = self.deye_adapter.translate(allocation.deye, telemetry.deye_battery_power.value)
        solax_result = self.solax_adapter.translate(
            allocation.solax,
            telemetry.solax_battery_power.value,
            grid_error_w=grid_error or 0.0,
        )
        status = allocation.status
        reasons = [allocation.saturation_reason]
        if runtime_transfer != "CLEAR":
            status = CommandStatus.FAULT
            reasons.append(runtime_transfer)
        if load.quality is not TelemetryQuality.VALID:
            status = CommandStatus.FAULT
            reasons.append(load.reason)
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
        )

    def _runtime_transfer(self, telemetry: ControlTelemetrySnapshot, transfer_allowed: bool) -> str:
        if transfer_allowed:
            return "CLEAR"
        deye = telemetry.deye_battery_power.value
        solax = telemetry.solax_battery_power.value
        if deye is None or solax is None:
            return "UNVERIFIED"
        if deye < -self.transfer_threshold_w and solax > self.transfer_threshold_w:
            return "FAULT: DEYE discharge plus SolaX charge detected"
        if solax < -self.transfer_threshold_w and deye > self.transfer_threshold_w:
            return "FAULT: SolaX discharge plus DEYE charge detected"
        return "CLEAR"
