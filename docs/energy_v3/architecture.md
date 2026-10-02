# ENERGY V3 architecture

ENERGY V3 is a small, deterministic execution bridge around HAEO. HAEO remains
the economic planner. V3 validates one current power target, checks whether it is
safe and physically feasible, selects at most one verified inverter control path,
and otherwise requests the known-normal configuration.

```text
HAEO
  -> CurrentTarget validation
  -> safety / feasibility
  -> single-owner arbitration
  -> future SolaXAdapter or DeyeAdapter
  -> measured feedback
  -> return to known normal
```

## Current target

`CurrentTarget` contains only:

- timezone-aware source `timestamp`;
- exclusive `valid_until` boundary;
- signed `solax_target_w`;
- signed `deye_target_w`.

HAEO power is positive for battery discharge and negative for battery charge.
V3 follows the current power target without integrating delivered energy or
attempting to catch up after a late start.

## Arbitration

- Two zero targets produce `IDLE`.
- A single nonzero target may produce `CONTROL_SOLAX` or `CONTROL_DEYE` only
  when the exact bounded-power capability is verified.
- Opposite directions produce `RETURN_TO_NORMAL` with `CROSS_TRANSFER_RISK`.
- Two active targets in the same direction produce `RETURN_TO_NORMAL` with
  `MULTI_OWNER_UNSUPPORTED`.
- There is no DEYE-first policy and no economic decision in V3.

## Safety

The pure decision requires fresh, finite telemetry; both inverters available and
fault-free; no writer conflict; SOC above the protected discharge threshold;
projected export within the 9.8 kW operational target and a valid export authorization;
and no measured battery-to-battery transfer. Normalized measured battery power is
positive for charge and negative for discharge.

The protected discharge threshold is the 10% physical floor plus a conservative
5 percentage-point guard margin by default. Discharge is blocked at or below the
resulting 15% threshold. The margin is configurable, finite and nonnegative;
charging does not require SOC evidence for discharge-floor protection.

Export projection remains intentionally instantaneous. For a positive discharge
target, V3 adds the requested discharge to the current normalized battery power.
This accounts for both removing existing charging load and changing existing
discharge, without introducing a full power-flow model.

## Export authorization

V3 does not calculate or predict the contractual rolling 15-minute average. A
separate authoritative export limiter owns PCC history, the 10 kW contractual
protection and the permitted export calculation. It supplies an immutable
`ExportAuthorization` containing:

- a timezone-aware evidence `timestamp`;
- an exclusive timezone-aware `valid_until` boundary;
- the current `max_pcc_export_w` ceiling.

Active discharge requires a present, structurally valid and current authorization.
The effective ceiling is the lower of the authorization and V3's independent
9.8 kW operational limit. The controller compares the complete projected PCC
export against that ceiling even when incremental discharge is zero. Missing,
future-dated, expired, nonfinite, negative or boolean authorization evidence fails
closed. Charging does not require export authorization.

Authorization must be refreshed continuously by a future runtime. Pre-command
validation cannot guarantee ongoing compliance after the authorization expires or
is reduced. If the existing ENERGY V2 limiter cannot provide this contract, the V3
runtime will need a dedicated limiter adapter; this foundation assumes no existing
Home Assistant entity or API.

All invalid, stale, unsafe, conflicting, or unsupported requests return
`RETURN_TO_NORMAL`. V3.0 contains no code that performs that future physical
restore.

## Capability baseline

| Capability | Status |
|---|---|
| SolaX normal | VERIFIED |
| SolaX hold | VERIFIED |
| SolaX bounded charge/discharge | UNSUPPORTED |
| DEYE normal | VERIFIED |
| DEYE enter export mode | VERIFIED |
| DEYE bounded export | UNSUPPORTED |
| DEYE bounded charge | UNSUPPORTED |
| DEYE hold | UNSUPPORTED |

Entering DEYE export mode does not prove bounded power following, so it is not
sufficient to authorize a `CONTROL_DEYE` decision.

## Deliberate exclusions

V3.0 does **not** contain:

- physical Home Assistant execution;
- physical service calls;
- inverter adapters with real writes;
- energy-budget or delivered-energy accounting;
- dual-inverter simultaneous control;
- economic or spot-price logic;
- plan IDs or execution epochs;
- physical ownership persistence;
- any dependency on ENERGY V2 runtime code.

The mirrored `deploy/appdaemon/apps/energy_v3/` package exists only to satisfy
repository source/deployment consistency checks.

## V3.1 read-only shadow runtime

V3.1 adds a deliberately read-only AppDaemon observation layer. It converts the
current HAEO 15-minute forecast interval to `CurrentTarget`, converts verified live
entities to `SafetySnapshot`, calls the pure controller and publishes diagnostic
sensor states with `set_state()`. It has no inverter service-call path, does not
register a physical writer and does not modify ENERGY V2 or existing manual/legacy
control.

No authoritative `ExportAuthorization` producer exists, so the runtime always
passes `None`. Discharge consequently remains blocked by the V3 safety rules.
Charging remains independently evaluable, but bounded-power capabilities are still
`UNSUPPORTED` and therefore cannot result in physical control.

There is no verified SolaX fault entity. The telemetry adapter represents that
missing evidence as `solax_fault=None`; the controller treats missing fault evidence
as `TELEMETRY_MISSING`, never as confirmed health.

The checked-in `energy_v3_shadow.yaml.disabled` file is an inactive deployment
template. It must not be renamed or copied into an active AppDaemon configuration
outside a supervised shadow-only deployment.
