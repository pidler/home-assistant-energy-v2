# ADR 0007: time-aware DEYE surplus-charge shadow state machine

Status: **APPROVED**, **IMPLEMENTED**, **NOT DEPLOYED**, **NOT VALIDATED**.

We use a separate pure state machine rather than embedding decisions in AppDaemon callbacks. This
makes SOC hysteresis, confirmation timers, irregular sampling, and the anti-transfer calculation
unit-testable. AppDaemon is only a read adapter and a writer of ENERGY V2 diagnostic helpers.

No executor is implemented. A future active-controller ADR must separately authorize and review any
physical service path.
