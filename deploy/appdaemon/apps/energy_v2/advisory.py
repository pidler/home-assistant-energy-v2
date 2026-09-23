"""Advisory-only input adaptation, baseline planning and explanations.
No controller or actuator API is imported. State reads and diagnostic publication
are separate capabilities; a plan is never an execution schedule.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from .deye_state import DeyeOperatingState
from .load_model import estimate_whole_site_load
from .observations import observation_time
from .telemetry import TelemetryReader
from .trading.inputs import assemble_slots, build_time_of_day_load_profile
from .trading.models import BatteryParameters, BatteryRole, PlannerConfig, PlannerInput
from .trading.planner import plan_trading_schedule

SLOT = timedelta(minutes=15)
OUTPUT_KEYS = (
    "status",
    "plan_id",
    "generated_at",
    "valid_until",
    "current_action",
    "next_action",
    "next_action_time",
    "net_result",
    "import_kwh",
    "export_kwh",
    "terminal_deye_soc",
    "terminal_solax_soc",
    "reserve_status",
    "degraded_inputs",
    "replan_reason",
    "summary",
)
OUTPUTS = {key: f"sensor.energy_v2_phase5a_{key}" for key in OUTPUT_KEYS}
EXECUTOR_INTENT_ENTITY = "sensor.energy_v2_phase5a_executor_intent"
_INTENT_EPSILON_KWH = 1e-5
PRICE_ENTITIES = ("sensor.current_buy_electricity_price_15min", "sensor.current_sell_electricity_price_15min")
PV_ENTITIES = ("sensor.energy_production_today_2", "sensor.energy_production_tomorrow_2")


def number(value):
    result = float(value)
    if not isfinite(result):
        raise ValueError("non-finite input")
    return result


def stamp(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(UTC)


def digest(value):
    def canonical(item):
        if isinstance(item, dict):
            return {str(k): canonical(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [canonical(v) for v in item]
        return item

    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, default=str).encode()).hexdigest()[:20]


@dataclass(frozen=True)
class AdvisoryConfig:
    timezone: str = "Europe/Prague"
    minimum_interval_s: float = 120
    debounce_s: float = 20
    soc_delta_pct: float = 3
    load_delta_w: float = 300
    horizon_refresh_s: float = 1800
    profile_max_age_s: float = 21600
    soc_max_age_s: float = 1800
    pv_sum_tolerance_kwh: float = 0.2
    batteries: tuple = field(
        default_factory=lambda: (
            BatteryParameters("DEYE", 32),
            BatteryParameters("SolaX", 24, terminal_reserve_soc_pct=30, role=BatteryRole.HOUSE_RESERVE_BATTERY),
        )
    )
    planner: PlannerConfig = field(default_factory=lambda: PlannerConfig(solax_evening_checkpoint_local_time=time(18)))

    def __post_init__(self):
        ZoneInfo(self.timezone)
        for key in (
            "minimum_interval_s",
            "debounce_s",
            "soc_delta_pct",
            "load_delta_w",
            "horizon_refresh_s",
            "profile_max_age_s",
            "soc_max_age_s",
            "pv_sum_tolerance_kwh",
        ):
            if number(getattr(self, key)) <= 0:
                raise ValueError(f"{key} must be positive")
        if {b.name for b in self.batteries} != {"DEYE", "SolaX"}:
            raise ValueError("DEYE and SolaX battery policies are required")
        for battery in self.batteries:
            battery.validate()
        self.planner.validate()


def price_profile(attributes, start):
    """HA timestamp-keyed attributes; reject malformed timestamp entries, never fill gaps."""
    result = {}
    for key, value in attributes.items():
        if not str(key)[:4].isdigit():
            continue  # unit/friendly_name metadata
        at = stamp(key)
        if at.second or at.microsecond or at.minute % 15:
            raise ValueError("price slot alignment")
        price = number(value)
        if at in result:
            raise ValueError("duplicate price timestamp")
        result[at] = price
    result = dict(sorted((at, value) for at, value in result.items() if at >= start))
    if not result or next(iter(result)) != start:
        raise ValueError("price horizon missing first future slot")
    keys = list(result)
    if any(b - a != SLOT for a, b in zip(keys, keys[1:], strict=False)):
        raise ValueError("missing price slot")
    return result


def pv_profile(raw, day, timezone, tolerance):
    attributes = raw.get("attributes", {})
    profile = attributes.get("wh_period_15m")
    if not isinstance(profile, dict) or len(profile) != 96:
        raise ValueError("PV wh_period_15m must contain 96 timestamp-keyed Wh slots")
    result = {}
    for key, value in profile.items():
        at = stamp(key)
        if at.astimezone(timezone).date() != day or at.minute % 15 or at.second or at.microsecond:
            raise ValueError("PV date/alignment mismatch")
        energy = number(value) / 1000
        if energy < 0 or at in result:
            raise ValueError("PV negative/duplicate slot")
        result[at] = energy
    result = dict(sorted(result.items()))
    keys = list(result)
    if (
        keys[0].astimezone(timezone).time() != time(0)
        or any(b - a != SLOT for a, b in zip(keys, keys[1:], strict=False))
        or (keys[-1] + SLOT).astimezone(timezone).date() != day + timedelta(days=1)
    ):
        raise ValueError("PV incomplete day (DST requires a future adapter)")
    unit = attributes.get("unit_of_measurement")
    if unit not in ("kWh", "Wh"):
        raise ValueError("PV total unit must be kWh or Wh")
    total = number(raw.get("state")) / (1000 if unit == "Wh" else 1)
    if total < 0 or abs(sum(result.values()) - total) > tolerance:
        raise ValueError("PV profile sum disagrees with daily total")
    return result


@dataclass
class LiveInputs:
    planner_input: PlannerInput
    load_w: float
    deye_state: str
    prices_hash: str
    pv_hash: str
    sources: dict
    pv_totals: dict
    warnings: list


class LiveAdapter:
    def __init__(self, reader, config):
        self.reader = reader
        self.config = config
        self.telemetry = TelemetryReader(reader)
        self.last_sources = {}
        self.last_deye_state = "UNAVAILABLE"

    def read_profile(self, entity, now):
        raw = self.reader.get_state(entity, attribute="all")
        if not isinstance(raw, dict):
            raise ValueError(f"{entity}: missing")
        at = observation_time(raw, now)
        self.last_sources[entity] = {
            "last_changed": raw.get("last_changed"),
            "last_updated": raw.get("last_updated"),
            "last_reported": raw.get("last_reported"),
            "observed_at": at,
            "fresh": at is not None and (now - at).total_seconds() <= self.config.profile_max_age_s,
        }
        if not self.last_sources[entity]["fresh"]:
            raise ValueError(f"{entity}: stale/invalid report evidence")
        return raw

    def collect(self, now):
        self.last_sources = {}
        snap = self.telemetry.control_snapshot(now=now)
        state = snap.deye_operating.state
        self.last_deye_state = state.value
        for sample in (
            snap.solax_soc,
            snap.deye_soc,
            snap.solax_inverter_power,
            snap.deye_inverter_power,
            snap.whole_site_grid_power,
        ):
            self.last_sources[sample.entity_id] = asdict(sample)
        if state not in (DeyeOperatingState.READY, DeyeOperatingState.INTENTIONAL_OFF):
            raise ValueError(f"DEYE_NOT_READY: {state.value}; {snap.deye_operating.reason}")
        soc = {}
        for name, key in (("DEYE", "deye_soc"), ("SolaX", "solax_soc")):
            sample = self.telemetry.numeric_sample(key, now, self.config.soc_max_age_s)
            if not sample.fresh or sample.value is None or not 0 <= sample.value <= 100:
                raise ValueError(f"{name}: stale/invalid SOC")
            self.last_sources[sample.entity_id] = asdict(sample)
            soc[name] = sample.value  # never clamp measured SOC to a floor
        load = estimate_whole_site_load(snap.solax_inverter_power, snap.deye_inverter_power, snap.whole_site_grid_power)
        if load.load_w is None:
            raise ValueError(load.reason)
        # Full future quarters only. No invented prices or partial-slot SOC trajectory.
        start = now.astimezone(UTC).replace(second=0, microsecond=0)
        start = start.replace(minute=(start.minute // 15) * 15)
        if start < now:
            start += SLOT
        raw_prices = [self.read_profile(entity, now).get("attributes", {}) for entity in PRICE_ENTITIES]
        prices = [price_profile(raw, start) for raw in raw_prices]
        if prices[0].keys() != prices[1].keys():
            raise ValueError("buy/sell horizon coverage differs")
        zone = ZoneInfo(self.config.timezone)
        day = now.astimezone(zone).date()
        pv, totals = {}, {}
        for entity, expected_day in zip(PV_ENTITIES, (day, day + timedelta(days=1)), strict=True):
            profile = pv_profile(self.read_profile(entity, now), expected_day, zone, self.config.pv_sum_tolerance_kwh)
            pv.update(profile)
            totals[entity] = sum(profile.values())
        if not prices[0].keys() <= pv.keys():
            raise ValueError("PV does not cover full economic horizon")
        loads, quality = build_time_of_day_load_profile((), prices[0], fallback_power_w=load.load_w)
        slots = assemble_slots(*prices, pv, loads)
        # Local timezone is needed by the planner's local-time reserve checkpoints.
        from dataclasses import replace

        slots = tuple(replace(slot, timestamp=slot.timestamp.astimezone(zone)) for slot in slots)
        data = PlannerInput(slots, self.config.batteries, soc, quality, now)
        data.validate()
        warnings = [
            "LOAD_FALLBACK: constant latest validated whole-site load; low confidence; all slots",
            "BASELINE_ONLY: no execution or dispatch; startup timing/energy not priced",
            "NO_GUARD_HORIZON: reserve beyond available prices is not guaranteed",
            "NEXT_FULL_QUARTER: measured SOC assumed unchanged until valid_from; baseline only",
        ]
        if state is DeyeOperatingState.INTENTIONAL_OFF:
            warnings.append("DEYE_START_REQUIRED if economic plan uses DEYE; not currently dispatchable")
        return LiveInputs(
            data, load.load_w, state.value, digest(raw_prices), digest(pv), self.last_sources.copy(), totals, warnings
        )


def action_for(slot):
    actions = []
    for name, battery in slot.batteries.items():
        if battery.charge_from_pv_kwh > 1e-5:
            actions.append("PV_CHARGE_" + name.upper())
        if battery.discharge_to_export_kwh > 1e-5:
            actions.append("EXPORT_" + name.upper())
    return " + ".join(actions) or "HOUSE / IDLE"


def describe(result, inputs, export_limit_w):
    timeline, trace = [], []
    before = result.initial_soc_pct.copy()
    for index, slot in enumerate(result.slots):
        action = action_for(slot)
        deye = slot.batteries["DEYE"]
        required = sum((deye.charge_from_pv_kwh, deye.discharge_to_load_kwh, deye.discharge_to_export_kwh)) > 1e-5
        startup = required and inputs.deye_state != "READY"
        if startup:
            action = "DEYE_START_REQUIRED | " + action
        if not timeline or timeline[-1]["action"] != action:
            timeline.append({"start": slot.timestamp, "end": slot.timestamp + SLOT, "action": action})
            trace.append(
                {
                    "time": slot.timestamp,
                    "action": action,
                    "sell_price": slot.sell_price_czk_per_kwh,
                    "buy_price": slot.buy_price_czk_per_kwh,
                    "soc_before": before.copy(),
                    "expected_pv_remaining": sum(s.pv_forecast_kwh for s in result.slots[index:]),
                    "expected_load_remaining": sum(s.load_forecast_kwh for s in result.slots[index:]),
                    "solax_reserve_ok": slot.batteries["SolaX"].projected_soc_pct
                    >= slot.batteries["SolaX"].active_soc_floor_pct,
                    "export_cap_ok": slot.planned_grid_export_kwh <= export_limit_w / 4000 + 1e-6,
                    "deye_availability": inputs.deye_state,
                    "deye_start_required": startup,
                    "reason_code": "DEYE_NOT_READY" if startup else "PLANNER_" + slot.action.value,
                    "reason_text": slot.reason,
                }
            )
        else:
            timeline[-1]["end"] = slot.timestamp + SLOT
        before = {name: battery.projected_soc_pct for name, battery in slot.batteries.items()}
    return timeline, trace


def publication_id(now, value=None):
    candidate = value if value is not None else digest(["phase5a-publication", now.astimezone(UTC).isoformat()])
    bounded = str(candidate).strip()
    if not bounded or len(bounded) > 160:
        raise ValueError("publication_id must be bounded nonempty text")
    return bounded


def safe_none_intent(
    now,
    *,
    publication_id_value=None,
    plan_id="NO_VALID_PLAN",
    reason_code="NO_VALID_CURRENT_SLOT",
    reason="No valid current advisory slot",
):
    start = now.astimezone(UTC).replace(second=0, microsecond=0)
    start = start.replace(minute=(start.minute // 15) * 15)
    end = start + SLOT
    bounded_plan = str(plan_id).strip()[:160] or "NO_VALID_PLAN"
    bounded_code = str(reason_code).strip()[:80] or "NO_VALID_CURRENT_SLOT"
    bounded_reason = str(reason).replace("\n", " ").strip()[:160] or "No valid current advisory slot"
    bounded_publication = publication_id(now, publication_id_value)
    attributes = {
        "schema_version": 1,
        "publication_id": bounded_publication,
        "plan_id": bounded_plan,
        "intent_id": digest([bounded_plan, start, "NONE", bounded_code]),
        "intent_type": "NONE",
        "target_w": None,
        "slot_start": start.isoformat(),
        "slot_end": end.isoformat(),
        "deadline": end.isoformat(),
        "created_at": now.astimezone(UTC).isoformat(),
        "hypothetical": False,
        "degraded": False,
        "confidence": "LOW",
        "reason_code": bounded_code,
        "reason": bounded_reason,
    }
    return "NONE", attributes


def executor_intent_payload(runtime, now, *, publication_id_value=None):
    """Return one bounded economics-only current-slot intent for Pass 4."""
    current_publication_id = publication_id(now, publication_id_value)
    plan = runtime.plan
    if not runtime.valid or not isinstance(plan, dict):
        return safe_none_intent(
            now, publication_id_value=current_publication_id, reason_code="ADVISORY_NOT_VALID", reason=runtime.error
        )
    result = plan.get("result")
    slots = getattr(result, "slots", ())
    horizon_end = getattr(result, "economic_horizon_end", None)
    if not isinstance(horizon_end, datetime) or now >= horizon_end:
        return safe_none_intent(
            now,
            publication_id_value=current_publication_id,
            plan_id=plan.get("id", "NO_VALID_PLAN"),
            reason_code="PLAN_EXPIRED",
        )
    slot = next((candidate for candidate in slots if candidate.timestamp <= now < candidate.timestamp + SLOT), None)
    if slot is None or slot.guard_only:
        return safe_none_intent(
            now,
            publication_id_value=current_publication_id,
            plan_id=plan.get("id", "NO_VALID_PLAN"),
            reason_code="NO_VALID_CURRENT_SLOT",
        )

    active = []
    for battery_name, battery in slot.batteries.items():
        charging = number(battery.charge_from_pv_kwh) > _INTENT_EPSILON_KWH
        discharging = (
            number(battery.discharge_to_load_kwh) + number(battery.discharge_to_export_kwh) > _INTENT_EPSILON_KWH
        )
        power = number(battery.planned_power_w)
        if charging and discharging:
            return safe_none_intent(
                now,
                publication_id_value=current_publication_id,
                plan_id=plan.get("id", "NO_VALID_PLAN"),
                reason_code="AMBIGUOUS_BATTERY_SLOT",
            )
        if charging:
            if power <= 0:
                return safe_none_intent(
                    now,
                    publication_id_value=current_publication_id,
                    plan_id=plan.get("id", "NO_VALID_PLAN"),
                    reason_code="INCOHERENT_BATTERY_POWER",
                )
            active.append((battery_name.upper(), "CHARGE", power))
        elif discharging:
            if power >= 0:
                return safe_none_intent(
                    now,
                    publication_id_value=current_publication_id,
                    plan_id=plan.get("id", "NO_VALID_PLAN"),
                    reason_code="INCOHERENT_BATTERY_POWER",
                )
            active.append((battery_name.upper(), "DISCHARGE", -power))
    if len(active) > 1:
        return safe_none_intent(
            now,
            publication_id_value=current_publication_id,
            plan_id=plan.get("id", "NO_VALID_PLAN"),
            reason_code="SIMULTANEOUS_BATTERIES",
        )

    if active:
        battery_name, operation, target_w = active[0]
        intent_type = f"{operation}_{battery_name}"
        if intent_type not in {"CHARGE_SOLAX", "DISCHARGE_SOLAX", "CHARGE_DEYE", "DISCHARGE_DEYE"}:
            return safe_none_intent(
                now,
                publication_id_value=current_publication_id,
                plan_id=plan.get("id", "NO_VALID_PLAN"),
                reason_code="UNSUPPORTED_BATTERY",
            )
    else:
        battery_name, intent_type, target_w = None, "NORMAL", None

    plan_id = str(plan.get("id", "")).strip()[:160]
    generated_at = plan.get("generated_at")
    if not plan_id or not isinstance(generated_at, datetime) or generated_at.tzinfo is None or generated_at > now:
        return safe_none_intent(now, publication_id_value=current_publication_id, reason_code="INVALID_PLAN_IDENTITY")
    hypothetical = battery_name == "DEYE" and plan.get("deye_operating_state") != "READY"
    confidence = str(plan.get("load_confidence", "LOW")).upper()
    if confidence not in {"LOW", "MEDIUM", "HIGH"}:
        confidence = "LOW"
    reason = str(slot.reason).replace("\n", " ").strip()[:160] or "Economic planner slot"
    reason_code = ("PLANNER_" + intent_type)[:80]
    end = slot.timestamp + SLOT
    attributes = {
        "schema_version": 1,
        "publication_id": current_publication_id,
        "plan_id": plan_id,
        "intent_id": digest([plan_id, slot.timestamp, intent_type]),
        "intent_type": intent_type,
        "target_w": target_w,
        "slot_start": slot.timestamp.isoformat(),
        "slot_end": end.isoformat(),
        "deadline": end.isoformat(),
        "created_at": generated_at.astimezone(UTC).isoformat(),
        "hypothetical": hypothetical,
        "degraded": False,
        "confidence": confidence,
        "reason_code": reason_code,
        "reason": reason,
    }
    return intent_type, attributes


def render_report(plan):
    result = plan["result"]
    lines = [
        f"ENERGY V2 ADVISORY — {plan['generated_at'].date()}",
        f"SOC: {result.initial_soc_pct}",
        f"PV kWh: {plan['pv_totals']}",
        "Load: FALLBACK / LOW confidence (constant validated load)",
        f"Import/export: {result.expected_import_kwh:.2f}/{result.expected_export_kwh:.2f} kWh",
        f"Purchase/sale/net: {result.expected_import_cost_czk:.2f}/{result.expected_revenue_czk:.2f}/"
        f"{result.expected_net_grid_value_czk:.2f} CZK",
        f"Terminal SOC: {result.terminal_soc_pct}",
        f"Reserve: {plan['reserve_status']}",
    ]
    lines.extend(f"{p['start'].isoformat()} — {p['end'].isoformat()}: {p['action']}" for p in plan["timeline"])
    lines.extend(plan["warnings"])
    return "\n".join(lines)


class AdvisoryRuntime:
    def __init__(self, adapter, config=None, planner=plan_trading_schedule):
        self.adapter = adapter
        self.config = config or adapter.config
        self.planner = planner
        self.plan = None
        self.baseline_inputs = None
        self.valid = False
        self.error = "NOT_EVALUATED"
        self.last_attempt = None
        self.pending_since = None
        self.pending_reason = ""
        self.events = []
        self.replan_reason = "INITIAL"

    def tick(self, now):
        try:
            inputs = self.adapter.collect(now)
        except Exception as error:
            self.valid = False
            self.error = str(error)
            self.pending_since = None
            return self.diagnostics(now)
        reasons = []
        old = self.baseline_inputs
        if old is None:
            reasons.append("INITIAL")
        else:
            if not self.valid:
                reasons.append("INPUT_RECOVERED_OR_RETRY")
            if inputs.prices_hash != old.prices_hash:
                reasons.append("PRICE_HORIZON")
            if inputs.pv_hash != old.pv_hash:
                reasons.append("PV_PROFILE")
            if any(
                abs(inputs.planner_input.initial_soc_pct[k] - old.planner_input.initial_soc_pct[k])
                >= self.config.soc_delta_pct
                for k in old.planner_input.initial_soc_pct
            ):
                reasons.append("SOC_DEVIATION")
            if abs(inputs.load_w - old.load_w) >= self.config.load_delta_w:
                reasons.append("LOAD_DEVIATION")
            if inputs.deye_state != old.deye_state:
                reasons.append("DEYE_STATE")
            if (self.plan["result"].economic_horizon_end - now).total_seconds() <= self.config.horizon_refresh_s:
                reasons.append("HORIZON_EXPIRY")
        if reasons:
            reason = ",".join(reasons)
            if self.pending_since is None:
                self.pending_since = now
            self.pending_reason = reason
            # Any meaningful input change invalidates current advisory action until replanned.
            self.valid = False
            self.error = "REPLAN_PENDING: " + reason
            elapsed = float("inf") if self.last_attempt is None else (now - self.last_attempt).total_seconds()
            if elapsed >= self.config.minimum_interval_s and (
                old is None or (now - self.pending_since).total_seconds() >= self.config.debounce_s
            ):
                self.last_attempt = now
                self.replan_reason = reason
                try:
                    result = self.planner(inputs.planner_input, self.config.planner)
                    timeline, trace = describe(result, inputs, self.config.planner.site_export_limit_w)
                    reserve_ok = all(v <= 1e-5 for v in result.terminal_reserve_shortfall_kwh.values()) and all(
                        c.shortfall_pct <= 1e-5 for c in result.checkpoints
                    )
                    plan = {
                        "id": digest([now, inputs.prices_hash, inputs.pv_hash, inputs.planner_input.initial_soc_pct]),
                        "generated_at": now,
                        "schema_version": 1,
                        "valid_from": result.horizon_start,
                        "economic_horizon_end": result.economic_horizon_end,
                        "guard_horizon_end": None,
                        "expected_load_kwh": sum(s.load_forecast_kwh for s in result.slots),
                        "load_source": "latest_validated_whole_site_load_constant",
                        "load_confidence": "LOW",
                        "fallback_periods": [(result.horizon_start, result.horizon_end)],
                        "deye_operating_state": inputs.deye_state,
                        "deye_dispatch_eligible": inputs.deye_state == "READY",
                        "planner_version": "phase5a-lp-v1",
                        "sources": inputs.sources,
                        "pv_totals": inputs.pv_totals,
                        "result": result,
                        "timeline": timeline,
                        "trace": trace,
                        "warnings": inputs.warnings,
                        "reserve_status": "SOFT_TARGETS_MET" if reserve_ok else "RESERVE_SHORTFALL",
                    }
                    previous = self.plan
                    self.events.append(
                        {
                            "trigger": reason,
                            "previous_plan_id": previous["id"] if previous else None,
                            "new_plan_id": plan["id"],
                            "generated_at": now,
                            "economic_delta": result.expected_net_grid_value_czk
                            - previous["result"].expected_net_grid_value_czk
                            if previous
                            else None,
                            "terminal_soc_delta": {
                                k: v - previous["result"].terminal_soc_pct[k]
                                for k, v in result.terminal_soc_pct.items()
                            }
                            if previous
                            else None,
                            "important_action_changes": timeline != previous["timeline"] if previous else True,
                        }
                    )
                    self.events = self.events[-100:]
                    self.plan, self.baseline_inputs = plan, inputs
                    self.valid, self.error = True, ""
                    self.pending_since = None
                except Exception as error:
                    self.valid, self.error = False, "PLANNER_ERROR: " + str(error)
        return self.diagnostics(now)

    def diagnostics(self, now):
        values = dict.fromkeys(OUTPUT_KEYS, "unavailable")
        values.update(
            status="DEGRADED",
            current_action="HOLD / DEGRADED",
            degraded_inputs=self.error,
            replan_reason=self.replan_reason,
            summary="No valid advisory plan",
        )
        plan = self.plan
        if plan:
            result = plan["result"]
            usable = self.valid and now < result.economic_horizon_end
            current = next((p for p in plan["timeline"] if p["start"] <= now < p["end"]), None)
            upcoming = next((p for p in plan["timeline"] if p["start"] > now), None)
            values.update(
                status="ADVISORY" if usable else "LAST_KNOWN",
                plan_id=plan["id"],
                generated_at=plan["generated_at"].isoformat(),
                valid_until=result.economic_horizon_end.isoformat(),
                current_action=current["action"] if usable and current else "HOLD / DEGRADED",
                next_action=upcoming["action"] if usable and upcoming else "unavailable",
                next_action_time=upcoming["start"].isoformat() if usable and upcoming else "unavailable",
                net_result=result.expected_net_grid_value_czk if usable else "unavailable",
                import_kwh=result.expected_import_kwh if usable else "unavailable",
                export_kwh=result.expected_export_kwh if usable else "unavailable",
                terminal_deye_soc=result.terminal_soc_pct["DEYE"] if usable else "unavailable",
                terminal_solax_soc=result.terminal_soc_pct["SolaX"] if usable else "unavailable",
                reserve_status=plan["reserve_status"] if usable else "LAST_KNOWN",
                degraded_inputs=self.error or "; ".join(plan["warnings"]),
                summary="ADVISORY BASELINE; load FALLBACK; no dispatch" if usable else "LAST_KNOWN: " + self.error,
            )
        return values
