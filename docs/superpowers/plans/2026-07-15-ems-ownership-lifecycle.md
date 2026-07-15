# EMS Ownership Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Webasto EMS ownership explicit, persistent, reversible, and safe across disable, reload, unload, shutdown, removal, communication failure, and restart.

**Architecture:** `WebastoCoordinator` owns one serialized lifecycle state machine and persists one per-session register snapshot in config-entry data. One always-available switch changes requested ownership; every other write and poll requires Active state. Existing DLB and charge modes stay controller policies inside an active ownership session.

**Tech Stack:** Python 3.12, Home Assistant custom integration APIs, pymodbus, pytest, asyncio.

## Global Constraints

- Never invent original values for registers 5004, 2000, 2002, or 405.
- Persist a complete dirty snapshot before the first charger write.
- Keep Alive active until orderly restoration has been read-verified.
- Use no Victron-specific logic and add no dependency.
- Preserve requested ON across reload, unload, and shutdown; user disable persists OFF.
- Existing version-1 entries require explicit baseline confirmation.
- Use one lifecycle lock and one restoration implementation.

---

### Task 1: Persistent ownership state and register lease

**Files:**
- Modify: `custom_components/unite_ev_charger/const.py`
- Modify: `custom_components/unite_ev_charger/coordinator.py`
- Replace tests: `tests/test_coordinator_ownership.py`

**Interfaces:**
- Produces: `OwnershipState`, `OriginalChargerConfig`, `ownership_state`, `automatic_control_requested`, `async_activate()`, and `async_suspend(preserve_requested: bool)`.
- Consumes: existing register definitions and `program_failsafe()`/`write_heartbeat()`.

- [ ] **Step 1: Write failing lease tests**

Add deterministic fake-register tests proving capture order, durable dirty state before writes, exact restore order, read-back verification, fresh snapshot per cycle, idempotency, initialization rollback, and retained dirty state after restore failure.

```python
asyncio.run(coordinator.async_activate())
assert client.events[:4] == [
    ("read", "set_current_a", 20),
    ("read", "failsafe_current_a", 12),
    ("read", "failsafe_timeout_s", 45),
    ("read", "phase_switch", 0),
]
assert persisted_before_first_write is True

assert asyncio.run(coordinator.async_suspend(preserve_requested=False)) is True
assert restored_names == [
    "set_current_a", "failsafe_current_a", "failsafe_timeout_s", "phase_switch"
]
assert verified_names == restored_names
assert close_index > verification_index
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `pytest -q tests/test_coordinator_ownership.py`

Expected: failures because lifecycle state, full snapshot, and serialized restoration do not exist.

- [ ] **Step 3: Implement minimum coordinator lifecycle**

Add string enum states and frozen snapshot model. Persist keys through one helper. Activation captures and validates the complete snapshot, marks it dirty, programs EMS, and transitions Active. Suspension cancels controller recovery, sends a final Alive, restores 5004/2000/2002/conditional 405, verifies each value, closes, clears snapshot, and transitions Suspended. Failed restoration keeps Error plus dirty data.

```python
class OwnershipState(StrEnum):
    DISABLED = "disabled"
    INITIALIZING = "initializing"
    ACTIVE = "active"
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"
    ERROR = "error"

@dataclass(frozen=True, slots=True)
class OriginalChargerConfig:
    current_limit: int
    failsafe_current: int
    failsafe_timeout: int
    phase_switch: int | None = None
```

- [ ] **Step 4: Run focused and full tests**

Run: `pytest -q tests/test_coordinator_ownership.py`

Expected: all ownership tests pass.

Run: `pytest -q`

Expected: existing tests pass or reveal only callers that still use removed single-register methods.

- [ ] **Step 5: Commit**

```bash
git add custom_components/unite_ev_charger/const.py custom_components/unite_ev_charger/coordinator.py tests/test_coordinator_ownership.py
git commit -m "fix(control): add persistent EMS ownership lease" -m "Capture every charger value before claiming control and restore verified originals through one serialized lifecycle. Preserve dirty recovery data whenever communication prevents a clean release."
```

### Task 2: Lifecycle hooks, inactive polling, and writable-entity gate

**Files:**
- Modify: `custom_components/unite_ev_charger/__init__.py`
- Modify: `custom_components/unite_ev_charger/controller.py`
- Modify: `custom_components/unite_ev_charger/entity.py`
- Modify: `custom_components/unite_ev_charger/switch.py`
- Modify: `tests/test_coordinator_ownership.py`
- Modify: `tests/test_controller_external.py`

**Interfaces:**
- Consumes: Task 1 coordinator lifecycle.
- Produces: persistent `Automatic charger control` entity and unified setup/unload/shutdown behavior.

- [ ] **Step 1: Write failing hook and gate tests**

Cover setup requested-on activation, requested-off zero-connect setup, dirty-off startup restoration, unload restoration before platform teardown, shutdown restoration, option reload preserving requested ON, and external writes rejected outside Active.

```python
assert events == ["suspend", "platforms", "close"]

coordinator.ownership_state = OwnershipState.SUSPENDED
with pytest.raises(WebastoModbusError, match="not active"):
    asyncio.run(controller.async_external_set_current(16))
assert client.writes == []
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `pytest -q tests/test_coordinator_ownership.py tests/test_controller_external.py`

Expected: failures because setup claims unconditionally, unload restores too late, and external writes bypass ownership.

- [ ] **Step 3: Implement lifecycle integration and switch**

Create controller before ownership setup. Activate only when requested. Keep polling network-free outside Active. Suspend before unloading platforms. Register one Home Assistant stop listener. Add an always-available ownership switch; keep existing Charging switch and every other coordinator entity unavailable unless Active. Gate external direct-write methods in controller.

```python
class UniteAutomaticControlSwitch(UniteEntity, SwitchEntity):
    _attr_translation_key = "automatic_control"

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.automatic_control_requested
```

- [ ] **Step 4: Run focused and full tests**

Run: `pytest -q tests/test_coordinator_ownership.py tests/test_controller_external.py`

Expected: focused tests pass.

Run: `pytest -q`

Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add custom_components/unite_ev_charger/__init__.py custom_components/unite_ev_charger/controller.py custom_components/unite_ev_charger/entity.py custom_components/unite_ev_charger/switch.py tests/test_coordinator_ownership.py tests/test_controller_external.py
git commit -m "fix(control): release EMS ownership on every stop path" -m "Route user disable, unload, reload, removal, and Home Assistant shutdown through verified suspension before polling or heartbeat stops. Block controller writes whenever ownership is inactive."
```

### Task 3: Legacy baseline recovery, diagnostics, translations, and docs

**Files:**
- Modify: `custom_components/unite_ev_charger/config_flow.py`
- Modify: `custom_components/unite_ev_charger/__init__.py`
- Modify: `custom_components/unite_ev_charger/diagnostics.py`
- Modify: `custom_components/unite_ev_charger/strings.json`
- Modify: `custom_components/unite_ev_charger/translations/en.json`
- Modify: `custom_components/unite_ev_charger/translations/nl.json`
- Modify: `README.md`
- Modify: `tests/test_coordinator_ownership.py`

**Interfaces:**
- Consumes: requested ownership and lifecycle snapshot.
- Produces: config-entry version 2 migration marker, repair issue, explicit switch confirmation, and diagnostic evidence.

- [ ] **Step 1: Write failing migration and diagnostic tests**

Prove version-1 migration disables automatic control, marks baseline recovery required, never guesses failsafe originals, and explicit switch activation clears the marker only after capturing live originals.

```python
assert asyncio.run(integration.async_migrate_entry(hass, entry)) is True
assert entry.data[CONF_BASELINE_REQUIRED] is True
assert entry.data[CONF_AUTOMATIC_CONTROL] is False
assert entry.version == 2
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `pytest -q tests/test_coordinator_ownership.py`

Expected: migration API and baseline marker are absent.

- [ ] **Step 3: Implement migration and operator evidence**

Bump config-flow version to 2. Mark legacy entries baseline-required and inactive. Create a persistent repair issue at setup. Treat turning Automatic charger control on as explicit confirmation: capture fresh values before clearing the marker or writing. Add state, requested state, dirty flag, baseline flag, and originals to diagnostics. Add English/Dutch switch and repair text. Update README with mode-versus-ownership behavior and physical offline limitation.

- [ ] **Step 4: Validate behavior and JSON**

Run: `pytest -q`

Expected: all tests pass.

Run: `python -m json.tool custom_components/unite_ev_charger/strings.json >/dev/null`

Run: `python -m json.tool custom_components/unite_ev_charger/translations/en.json >/dev/null`

Run: `python -m json.tool custom_components/unite_ev_charger/translations/nl.json >/dev/null`

Expected: each command exits 0.

- [ ] **Step 5: Commit**

```bash
git add custom_components/unite_ev_charger/config_flow.py custom_components/unite_ev_charger/__init__.py custom_components/unite_ev_charger/diagnostics.py custom_components/unite_ev_charger/strings.json custom_components/unite_ev_charger/translations/en.json custom_components/unite_ev_charger/translations/nl.json README.md tests/test_coordinator_ownership.py
git commit -m "feat(control): expose safe automatic-control ownership" -m "Add a persistent ownership switch, legacy baseline recovery, diagnostics, translations, and operator guidance without coupling control to DLB or any meter vendor."
```

### Task 4: Final verification and publication

**Files:**
- Verify all changed files.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: verified pushed feature branch ready for draft PR review.

- [ ] **Step 1: Run full verification**

Run: `pytest -q`

Expected: zero failures.

Run: `python -m compileall -q custom_components tests`

Expected: exit 0.

Run: `git diff --check origin/main...HEAD`

Expected: exit 0.

- [ ] **Step 2: Review commit and scope**

Run: `git status --short --branch`

Expected: clean feature branch.

Run: `git log --oneline origin/main..HEAD`

Expected: Conventional Commits only, including design and implementation commits.

- [ ] **Step 3: Push branch**

Run: `git push origin codex/dlb-safety-hardening`

Expected: remote branch advances to final implementation commit.
