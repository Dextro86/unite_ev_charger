# Real-Hardware Safety Hardening Design

**Date:** 2026-07-15

## Purpose

Close the remaining ownership gaps found before commissioning the integration
against a real Webasto Unite. This hardens the existing EMS lifecycle; it does
not add a second control architecture or any Victron-specific behavior.

Where this document differs from the earlier EMS Ownership Lifecycle Design,
this hardening design supersedes it. In particular, new entries now default to
automatic control off and DLB input failure now commands 0 A.

## Chosen approach

Use the coordinator's existing ownership lock as the single write gate. Every
charger write that belongs to automatic control runs while holding that lock
and rechecks that ownership is Active. Suspension takes the same lock, changes
state to Suspending before restoration, restores and verifies the snapshot,
then closes the connection. This makes a controller write and suspension
mutually exclusive without adding a command queue or background worker.

Rejected alternatives:

- A dedicated command queue would serialize operations, but duplicates the
  coordinator lifecycle and adds shutdown/error states of its own.
- Per-call state checks without a shared lock leave a check/write race.

## Safe first setup

New config entries persist `automatic_control: false`. Setup must not open a
Modbus connection until the user explicitly enables Automatic charger control.
Existing entries retain their persisted choice. Writable entities remain
unavailable while control is disabled.

## Activation and first-connect baseline

The current code comments say the Unite resets EMS-owned registers when a
Modbus connection opens. The manufacturer's protocol wording only requires a
master to set failsafe current, failsafe timeout, and charging current
immediately after connecting; it does not establish exactly when or how their
readable values change. Therefore a value read after the integration opens its
first connection is not automatically proven to be the charger's pre-connect
autonomous value.

The integration must not silently claim otherwise. Initial activation requires
one of these trusted baselines:

1. A complete durable snapshot from an interrupted ownership session.
2. Explicit user confirmation that the charger is currently in a known
   autonomous configuration, followed by capture before the first integration
   write.

For a new entry, enabling Automatic charger control is that explicit
confirmation. The UI and documentation must say that the vehicle should be
unplugged and the charger returned to the desired autonomous configuration
before enabling it. Captured values remain labelled as the session baseline,
not as proof that opening the socket was passive.

Hardware commissioning must independently observe registers 5004, 2000, 2002,
and 405 across connect/close. A reset-aware fake-client test will prove that a
reconnect never replaces an existing dirty snapshot with reset values. The UI,
logs, and documentation must not claim that first-connect passivity has been
proven before that hardware check.

## Atomic charger writes

Add one coordinator method for owned writes. It:

- acquires the lifecycle lock;
- requires state Active;
- performs the Modbus write before releasing the lock.

All controller, entity, reconnect-handshake, phase-recovery, and heartbeat
writes use coordinator-owned operations or execute inside a coordinator-owned
critical section. Internal controller paths receive the same protection as
external evcc paths. No caller may check Active and later write outside the
lock.

The periodic update must not hold the lock while waiting on Home Assistant
sensor reads or doing non-I/O calculations. It enters a bounded owned section
for the reconnect handshake, controller application, and heartbeat. Suspension
waits for that section, then prevents later writes by moving to Suspending.

## Setup rollback

Once activation has produced a dirty snapshot, every later setup failure uses
the normal suspension path before raising `ConfigEntryNotReady` or propagating
the error. This includes device-info reads, first refresh, platform forwarding,
and listener registration. Cleanup is registered as early as Home Assistant
allows.

If rollback cannot restore, retain the durable dirty snapshot and log a
critical error containing the baseline values. Never report a clean setup
failure while modified registers may remain.

## Unload, removal, and restart recovery

Unload continues to restore before platform teardown. A failed unload keeps the
dirty journal. Because Home Assistant can remove a config entry even when
unload returns false, recovery data must not depend solely on the config entry.
The existing Store journal remains authoritative and must survive entry
removal until restoration succeeds.

Removal performs restoration while the coordinator still exists. Journal
deletion happens only after verified restoration. If Home Assistant cannot
finish restoration during removal or shutdown, logs and the retained journal
must provide manual/restart recovery evidence rather than claiming success.

No software can restore an unreachable or powered-off charger. The hardware
failsafe remains the immediate protection after a crash; the journal provides
the next-start recovery path.

## DLB input failure

Missing, stale, non-finite, or implausible required meter input commands 0 A
and withholds Alive. It must not command the configurable operational failsafe
current, because that value may be as high as 32 A and is not proof of spare
grid capacity. The configured failsafe current remains the wallbox watchdog
setting for communication loss outside a known DLB-input failure.

Recovery from healthy input resumes the selected control strategy through the
normal ramp rules.

## Restoration heartbeat

Restoration refreshes Alive as needed while writing and verifying registers so
a short configured timeout cannot expire midway through an otherwise healthy
release. Alive stops only after every captured register has been restored and
read back successfully.

## Home Assistant compatibility

Avoid config-entry update patterns that trigger duplicate reloads. Internal
ownership journal mirroring must not reload the integration. User option
changes perform one suspension and one reload through supported Home Assistant
entry APIs.

The unit suite may retain lightweight Home Assistant stubs, but lifecycle tests
must model real ordering for setup failure, unload, removal, and stop events.
Where practical, CI should add Home Assistant's integration validation tooling;
this hardening must not claim real-version compatibility solely from stubs.

## Automated test contract

Tests must prove:

- a new entry defaults to automatic control off and performs no Modbus I/O;
- explicit enable captures a complete baseline before any write;
- reset-on-open simulation cannot overwrite an already-owned dirty session's
  persisted snapshot;
- a controller write racing disable finishes before restoration, and no write
  occurs after restoration begins;
- built-in, evcc, phase, reconnect, and heartbeat writes reject inactive
  ownership;
- every failure after activation attempts normal restoration;
- first-refresh and platform-forwarding failures leave either a verified clean
  charger or a retained dirty journal;
- unload, shutdown, and removal restore before teardown;
- removal failure retains recoverable ownership data outside entry memory;
- repeated enable/disable remains idempotent;
- restoration refreshes Alive during a long successful restore;
- invalid DLB input writes 0 A and withholds Alive;
- a restart with a dirty record never recaptures integration-modified values.

## Acceptance criteria

- Adding the integration cannot claim the charger.
- No automatic-control write can interleave after restoration starts.
- Any orderly stop restores and verifies every captured register before Alive
  or the connection is released.
- Any incomplete stop retains durable recovery evidence.
- Unsafe DLB input cannot command positive charging current.
- The implementation remains generic and adds no dependency or competing
  control subsystem.
