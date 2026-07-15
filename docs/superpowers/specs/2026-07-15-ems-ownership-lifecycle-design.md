# EMS Ownership Lifecycle Design

**Date:** 2026-07-15

## Purpose

Make charger ownership explicit and reversible. The integration must control
the Webasto Unite only while automatic charger control is enabled. Releasing
control must restore the charger configuration that existed immediately before
that ownership session.

Fast, Manual, Solar, and external evcc control remain valid control strategies.
DLB remains an optional safety ceiling over built-in strategies, not the master
ownership switch.

## Goals

- Add a persistent `Automatic charger control` switch.
- Model ownership with the required Disabled, Initializing, Active, Suspending,
  Suspended, and Error states.
- Capture original registers before the first write of each ownership session.
- Restore and verify every register the integration changed before releasing
  ownership.
- Keep the Alive watchdog running until restoration has been verified.
- Resume automatically after a Home Assistant restart when ownership was on.
- Retain enough persisted state to recover after an interrupted restoration.
- Make every lifecycle transition and original value visible in logs and
  diagnostics.

## Non-goals

- Treating DLB as the ownership switch.
- Running built-in control and evcc concurrently.
- Adding Victron-specific behavior.
- Inventing an undocumented EMS-disable register.
- Claiming successful restoration while the charger is unreachable.
- Keeping a Modbus monitoring session open while ownership is suspended.

## User-facing model

Two switches have separate meanings:

- `Automatic charger control` claims or releases EMS ownership.
- Existing `Charging` switch allows or pauses charging while ownership is
  active.

When automatic control is active, one owner drives the charger:

- Built-in Fast, Manual, Solar, or Min+Solar control, optionally capped by DLB.
- External evcc passthrough, with evcc responsible for its load-management
  policy.

Control entities that can write charger registers are unavailable outside the
Active state. Monitoring entities are also unavailable while suspended because
opening or retaining the Unite Modbus connection cannot currently be proven to
be passive.

New config entries default automatic control to on, preserving current product
behavior. Existing entries created by a version that did not capture registers
2000 and 2002 require the legacy migration described below before control can
resume. A prior explicit switch state is persisted and wins over the default.

## Persistent ownership record

Persist lifecycle data in config-entry data, separate from user options:

- requested automatic-control state
- ownership-session dirty flag
- original register 5004 charging-current limit
- original register 2000 failsafe current
- original register 2002 failsafe timeout
- original register 405 phase selection when phase control may modify it

The update listener must distinguish internal ownership-record updates from
option changes. Internal persistence must not trigger a config-entry reload.

The dirty flag is written with the complete snapshot before any charger write.
It remains set for the whole ownership session. It is cleared, and the snapshot
removed, only after successful restore verification and connection closure.

A normal Suspended-to-Initializing transition captures a fresh snapshot. This
allows users to change the autonomous charger configuration while suspended.
An interrupted or active session never recaptures values, because charger
registers may already contain integration-owned settings.

## State machine

### Disabled

Initial state when automatic control is off and no unfinished ownership record
exists. No Modbus connection, heartbeat, polling, or control writes occur.

Turning automatic control on enters Initializing.

### Initializing

Open and validate Modbus communication. When starting a new ownership session,
read registers 5004, 2000, and 2002, plus register 405 when phase control can
write it. Validate each value and persist the complete dirty snapshot before
writing anything.

When resuming an interrupted active session, reuse the persisted snapshot.
Never redefine originals from live, possibly integration-modified registers.

Program the configured failsafe registers, establish the initial controlled
current and any required phase intent, then start Alive writes. Enter Active
only after this handshake succeeds.

Any failure before the first write closes the connection without restoration.
Any failure after a write invokes the same restoration path as suspension.

### Active

The selected built-in strategy or external evcc owns register 5004. Phase
control may own register 405. The integration maintains registers 2000, 2002,
and Alive register 6000.

DLB may cap built-in modes. DLB input failure may intentionally withhold Alive
so the configured hardware failsafe takes effect; this does not release the
ownership session.

Turning automatic control off enters Suspending. Unload, removal, option reload,
and Home Assistant shutdown invoke the same transition.

### Suspending

For a user-requested disable, persist requested automatic control as off before
restoration begins. Temporary lifecycle suspension for reload, unload, or Home
Assistant shutdown preserves the requested state so a later setup can resume.
Continue Alive writes while communication permits, then perform this exact
sequence:

1. Restore register 5004.
2. Restore register 2000.
3. Restore register 2002.
4. Restore register 405 if it was captured and changed.
5. Read back every restored register and require exact equality.
6. Stop Alive writes.
7. Close the Modbus connection.
8. Clear the dirty snapshot.
9. Enter Suspended.

Repeated suspension calls share one serialized operation. Once Suspended, they
perform no writes.

### Suspended

The charger is autonomous. No Modbus socket, polling, heartbeat, or writes are
active. Writable entities and telemetry are unavailable.

Turning automatic control on enters Initializing and captures a fresh snapshot.

### Error

Error always retains enough context to distinguish an active-control failure
from a failed release.

- Active control-path failure while Modbus writes still work: continue Alive
  only when the existing DLB safety policy permits it. A true Modbus link
  failure cannot send Alive, so allow the hardware watchdog to use the
  configured failsafe. Reconnect using the persisted snapshot and resume Active
  because requested ownership remains on.
- Initialization failure after a write: attempt immediate restoration. Enter
  Suspended if it verifies; otherwise remain Error with the dirty snapshot.
- Suspension failure: retain both the requested state chosen by the trigger and
  the dirty snapshot, then retry restoration while Home Assistant is running.
  A user disable therefore remains requested off; reload, unload, or shutdown
  keeps its prior requested state. Never expose Suspended or clear the snapshot
  before verification.

On startup, a dirty snapshot with requested ownership on resumes control. A
dirty snapshot with requested ownership off performs restoration before any
normal setup or polling.

## Lifecycle integration

### Setup and restart

Setup reads the persistent record before claiming the connection:

- Requested on with no dirty snapshot: start a new activation.
- Requested on with a dirty snapshot: resume the interrupted active session.
- Requested off with a dirty snapshot: finish restoration.
- Requested off without a dirty snapshot: remain Disabled without connecting.

The controller and writable entities become available only in Active.

### Options changes

An option change first suspends and verifies restoration using the old
configuration. The entry then reloads. Because requested ownership remains on
for a reload, the new instance starts a fresh ownership session and applies the
new configuration.

### Unload and removal

Suspension runs before platform teardown so heartbeat cannot stop before
restoration. A failed restoration makes unload return false where Home
Assistant permits this, preserving the running integration and recovery record.
Removal uses the same path and deletes the ownership record only after verified
restoration.

### Home Assistant shutdown

A one-shot shutdown listener starts suspension while coordinator and Modbus
client are still usable. If shutdown ends before restoration succeeds, log a
critical error and retain the dirty record for next startup.

### Unexpected process crash

No software can write restoration registers after its process has died. The
configured hardware watchdog therefore remains the immediate protection. On
restart, requested ownership on causes automatic resumption using the persisted
pre-crash snapshot, as selected for this design.

### Legacy migration

A version that already programmed failsafe registers without first persisting
their originals cannot reconstruct the pre-integration values. Reading 2000 and
2002 during upgrade may only capture values left by the old integration.

Such config entries must not silently label those live values as originals.
Migration marks ownership as requiring baseline recovery, closes Modbus, and
creates a persistent repair issue with the recorded current values and recovery
instructions. Automatic control remains unavailable until the user returns the
charger to a known autonomous configuration and explicitly confirms baseline
capture. This one-time confirmation starts a new ownership session by reading
all originals before any write.

No default original values are invented. A hardware-tested method that proves a
fresh connection resets all affected registers to autonomous values may replace
manual baseline recovery in a later change, but is not assumed here.

## Concurrency and idempotency

Use one async lifecycle lock in the coordinator. Every activation, suspension,
reload, reconnect, and shutdown path enters through coordinator lifecycle
methods. Controller and entity writes require Active state.

Duplicate enable calls in Active do nothing. Duplicate disable calls in
Suspended do nothing. Calls arriving during Initializing or Suspending await the
in-flight transition rather than starting another register sequence.

## Logging and diagnostics

Log at info level:

- every state transition with reason
- each captured original register and value
- successful ownership claim and effective failsafe values
- each restored register and verified value
- automatic recovery or resumption from a dirty snapshot

Log at warning or error level:

- communication loss while Active
- initialization rollback
- failed write or read-back verification
- retained dirty snapshot and planned retry

Log at critical level when unload, removal, or shutdown cannot restore the
charger. Include original register values so an operator can recover manually
from logs.

Diagnostics expose current ownership state, requested state, dirty status,
failsafe status, and original values. Host credentials and unrelated sensitive
configuration remain redacted by existing diagnostics behavior.

## Automated test contract

Tests must cover:

- enable then disable restores and verifies 5004, 2000, 2002, and captured 405
- Alive starts only after initialization completes
- Alive continues until every restoration read-back succeeds
- repeated enable and disable cycles are serialized and idempotent
- a new cycle captures new autonomous values after a successful suspension
- failure during snapshot reads performs no writes
- failure during initialization writes rolls back changed registers
- communication loss during restoration retains requested-off and dirty state
- restored communication retries and completes the pending release
- Home Assistant shutdown restores before client closure
- config-entry unload restores before platform teardown
- option reload restores with old settings, then starts a new session
- integration removal clears recovery data only after restoration
- restart after an unexpected crash reuses originals and resumes control
- requested-off restart with dirty data restores before polling
- a legacy entry never treats previously programmed failsafe values as proven
  originals and remains inactive pending baseline recovery
- Disabled and Suspended states open no connection and write no heartbeat
- built-in and evcc writable entities reject writes outside Active
- logs contain captured values, transition reasons, and failed-restoration data

State-machine tests use a deterministic fake Modbus client with ordered read,
write, connection, and failure recording. Existing pure control and Modbus
tests remain unchanged except where availability now depends on Active state.

## Physical limitation

Restoration cannot be guaranteed while the charger is powered off, the network
is unavailable, or Home Assistant has already terminated. The integration must
retain recovery state and refuse to claim success, but it cannot make an
unreachable device change registers. Likewise, no passive monitoring guarantee
is made without hardware evidence that a Modbus session does not claim or reset
EMS state.

## Acceptance criteria

- Charger-owned values are captured before every new ownership session.
- No integration write occurs before the complete snapshot is durable.
- Every normal control-release path uses one restoration implementation.
- Heartbeat never stops before successful restore verification during an
  orderly release.
- No writable entity can bypass the lifecycle state.
- Crash recovery never recaptures modified registers as originals.
- Legacy migration never invents or silently guesses missing originals.
- A failed restoration stays visible, retryable, and fully logged.
- DLB remains a safety cap rather than an ownership toggle.
