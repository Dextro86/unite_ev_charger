# Real-Hardware Safety Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing EMS ownership lifecycle safe enough for an unplugged real-hardware commissioning run before live charging.

**Architecture:** Keep `WebastoCoordinator` as the only ownership authority. Add one coordinator-owned Modbus write gate backed by the existing lifecycle lock, route every asynchronous controller write through it, and make setup/removal use the existing durable restore journal. Preserve the current controller and Modbus client; add no queue, dependency, Victron behavior, or competing integration.

**Tech Stack:** Python 3.12+, Home Assistant config-entry APIs, pymodbus 3.8, asyncio, pytest.

## Global Constraints

- New config entries persist `automatic_control: false` and perform no Modbus I/O until explicit enable.
- Never replace a complete dirty ownership snapshot with values read after reconnect.
- No charger write may start after ownership enters Suspending.
- Restore and verify 5004, 2000, 2002, and captured 405 before closing or clearing recovery data.
- Missing, stale, non-finite, or implausible DLB input commands 0 A and withholds Alive.
- Use no Victron-specific logic and add no dependency or second command worker.
- Every production behavior change starts with a focused failing test.

---

### Task 1: Default new entries to autonomous operation

**Files:**
- Modify: `custom_components/unite_ev_charger/config_flow.py`
- Modify: `custom_components/unite_ev_charger/coordinator.py`
- Modify: `custom_components/unite_ev_charger/strings.json`
- Modify: `custom_components/unite_ev_charger/translations/en.json`
- Modify: `custom_components/unite_ev_charger/translations/nl.json`
- Modify: `tests/test_ownership_lifecycle.py`

**Interfaces:**
- Consumes: `CONF_AUTOMATIC_CONTROL` and the existing ownership switch.
- Produces: absent/new ownership state means OFF; only explicit switch enable calls `async_activate()`.

- [ ] **Step 1: Write failing default-off tests**

Add a coordinator test with no ownership key and a config-flow entry-data test using a lightweight flow fake:

```python
def test_missing_automatic_control_key_defaults_off():
    coordinator, _client, _events = _coordinator(data={})
    assert coordinator.automatic_control_requested is False


def test_new_entry_persists_automatic_control_off(monkeypatch):
    flow = integration_config_flow()
    result = asyncio.run(flow.async_step_user({
        "name": "Unite",
        "host": "charger",
        "port": 502,
        "unit_id": 255,
    }))
    assert result["data"]["automatic_control"] is False
```

- [ ] **Step 2: Run and verify RED**

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q tests/test_ownership_lifecycle.py -k "defaults_off or persists_automatic"`

Expected: the coordinator assertion sees `True`, and created entry data lacks `automatic_control`.

- [ ] **Step 3: Implement the minimum safe default**

Import `CONF_AUTOMATIC_CONTROL` in `config_flow.py`, persist it on creation, and change both coordinator journal defaults:

```python
return self.async_create_entry(
    title=title,
    data={**user_input, CONF_AUTOMATIC_CONTROL: False},
)

@property
def automatic_control_requested(self) -> bool:
    return bool(self.entry.data.get(CONF_AUTOMATIC_CONTROL, False))

record = {
    CONF_AUTOMATIC_CONTROL: bool(data.get(CONF_AUTOMATIC_CONTROL, False)),
    CONF_OWNERSHIP_DIRTY: bool(data.get(CONF_OWNERSHIP_DIRTY, False)),
}
```

Add English/Dutch commissioning text explaining that first enable claims EMS control and should be done with the vehicle unplugged after restoring the desired autonomous charger configuration.

- [ ] **Step 4: Run focused and full tests**

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q tests/test_ownership_lifecycle.py`

Expected: all ownership lifecycle tests pass.

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q`

Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add custom_components/unite_ev_charger/config_flow.py custom_components/unite_ev_charger/coordinator.py custom_components/unite_ev_charger/strings.json custom_components/unite_ev_charger/translations/en.json custom_components/unite_ev_charger/translations/nl.json tests/test_ownership_lifecycle.py
git commit -m "fix(control)!: default new chargers to autonomous mode" -m "Require an explicit Automatic charger control enable before opening Modbus or claiming EMS ownership. This prevents adding the integration from changing a real charger." -m "BREAKING CHANGE: Newly added chargers remain uncontrolled until Automatic charger control is explicitly enabled."
```

---

### Task 2: Serialize all automatic-control writes with suspension

**Files:**
- Modify: `custom_components/unite_ev_charger/coordinator.py`
- Modify: `custom_components/unite_ev_charger/controller.py`
- Modify: `tests/test_ownership_lifecycle.py`
- Modify: `tests/test_controller_external.py`
- Modify: `tests/test_controller_phase_switch.py`
- Modify: `tests/test_controller_recovery.py`
- Modify: `tests/test_controller_dlb_safety.py`

**Interfaces:**
- Produces: `WebastoCoordinator.async_write_owned(register, value) -> None`.
- Produces: private `_async_claim_connection_locked(phase_switch_raw=None) -> None`; caller holds `_ownership_lock` in Initializing or Active.
- Consumes: existing `OwnershipState`, `_ownership_lock`, `WebastoModbusError`, and controller write paths.

- [ ] **Step 1: Write a failing suspend-versus-write race test**

Extend `FakeClient` with two `asyncio.Event` objects that pause a 5004 control write. Start the write, start suspension, release the write, and assert restoration is last:

```python
async def exercise() -> list[tuple]:
    coordinator, client, events = _coordinator()
    await coordinator.async_activate()
    client.pause_next_setpoint = True
    control = asyncio.create_task(
        coordinator.async_write_owned(R.SET_CURRENT_A, 16)
    )
    await client.write_started.wait()
    suspend = asyncio.create_task(
        coordinator.async_suspend(preserve_requested=False)
    )
    client.allow_write.set()
    await control
    assert await suspend is True
    return events

writes = [event for event in asyncio.run(exercise()) if event[0] == "write"]
assert writes[-4:] == [
    ("write", R.SET_CURRENT_A.name, 20),
    ("write", R.FAILSAFE_CURRENT_A.name, 12),
    ("write", R.FAILSAFE_TIMEOUT_S.name, 45),
    ("write", R.PHASE_SWITCH.name, 0),
]
```

Also test a write that starts after suspension obtains the lock:

```python
with pytest.raises(WebastoModbusError, match="not active"):
    await coordinator.async_write_owned(R.SET_CURRENT_A, 16)
assert client.values[R.SET_CURRENT_A.name] == 20
```

- [ ] **Step 2: Run and verify RED**

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q tests/test_ownership_lifecycle.py -k "write_race or write_after"`

Expected: `async_write_owned` does not exist.

- [ ] **Step 3: Add one coordinator write gate**

Implement the public Active-only gate:

```python
async def async_write_owned(self, register: R.RegisterDef, value: int) -> None:
    self._ensure_ownership_runtime()
    async with self._ownership_lock:
        if self.ownership_state is not OwnershipState.ACTIVE:
            raise WebastoModbusError("EMS ownership is not active; charger write rejected")
        await self.client.write_register(register, value)
```

Split the claim handshake so activation does not recursively acquire the lock:

```python
async def async_claim_connection(self, phase_switch_raw: int | None = None) -> None:
    async with self._ownership_lock:
        if self.ownership_state not in (OwnershipState.INITIALIZING, OwnershipState.ACTIVE):
            raise WebastoModbusError("EMS ownership is not active; reconnect rejected")
        await self._async_claim_connection_locked(phase_switch_raw)
```

`async_activate()` calls `_async_claim_connection_locked()` because it already owns the lifecycle lock. The reconnect callback may use direct client writes only inside that locked method; document that invariant beside the callback.

- [ ] **Step 4: Route independent controller writes through the gate**

Replace every controller write that can run outside the locked reconnect callback:

```python
await self.coordinator.async_write_owned(R.SET_CURRENT_A, value)
await self.coordinator.async_write_owned(R.PHASE_SWITCH, desired_raw)
```

This includes external current/phase, phase recovery pause/resume, internal phase changes, DLB stops, and `_write_setpoint`. Update controller test fakes with:

```python
async def async_write_owned(self, register, value: int) -> None:
    if not self.ownership_active:
        raise WebastoModbusError("EMS ownership is not active; charger write rejected")
    await self.client.write_register(register, value)
```

Add a source-level guard test that rejects new unprotected controller calls except inside the explicitly locked reconnect helpers.

- [ ] **Step 5: Keep orderly restoration alive throughout verification**

In `_async_restore_locked`, refresh Alive before each restore write and each verification read while the lifecycle lock is held:

```python
for register, value in registers:
    await write_heartbeat(self.client)
    await self.client.write_register(register, value)
for register, expected in registers:
    await write_heartbeat(self.client)
    actual = int(await self.client.read_register(register))
```

Update the ordered restoration expectation and add a slow-operation fake test proving an Alive precedes every restore/verify operation.

- [ ] **Step 6: Avoid turning a completed suspension back into Error**

When an owned write is rejected because suspension won the race, `_async_update_data()` raises `UpdateFailed` without changing Suspended to Error:

```python
except WebastoModbusError as err:
    if not self.ownership_active:
        raise UpdateFailed("Automatic charger control stopped during update") from err
    self._set_ownership_state(OwnershipState.ERROR, "Modbus communication failed while control was active")
    raise UpdateFailed(str(err)) from err
```

- [ ] **Step 7: Run focused and full tests**

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q tests/test_ownership_lifecycle.py tests/test_controller_external.py tests/test_controller_phase_switch.py tests/test_controller_recovery.py tests/test_controller_dlb_safety.py`

Expected: focused suite passes, including both race orderings.

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q`

Expected: full suite passes.

- [ ] **Step 8: Commit**

```bash
git add custom_components/unite_ev_charger/coordinator.py custom_components/unite_ev_charger/controller.py tests/test_ownership_lifecycle.py tests/test_controller_external.py tests/test_controller_phase_switch.py tests/test_controller_recovery.py tests/test_controller_dlb_safety.py
git commit -m "fix(control)!: serialize charger writes with ownership release" -m "Make the coordinator lifecycle lock the final authority for every asynchronous charger write. Suspension now waits for in-flight writes, blocks later writes, refreshes Alive during restoration, then restores and verifies originals." -m "BREAKING CHANGE: Charger writes are rejected unless Automatic charger control owns an Active EMS session."
```

---

### Task 3: Roll back failed setup and preserve removal recovery

**Files:**
- Modify: `custom_components/unite_ev_charger/__init__.py`
- Modify: `custom_components/unite_ev_charger/coordinator.py`
- Modify: `custom_components/unite_ev_charger/config_flow.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_ownership_lifecycle.py`

**Interfaces:**
- Produces: `WebastoCoordinator.async_remove_clean_ownership_record() -> None`.
- Produces: integration `async_remove_entry(hass, entry) -> None`.
- Consumes: `async_suspend(preserve_requested=True)` and Home Assistant's setup/unload callbacks.

- [ ] **Step 1: Write failing post-activation setup rollback tests**

Parameterize first-refresh and platform-forwarding failures. Each fake activates a dirty lease; setup must suspend before propagating:

```python
@pytest.mark.parametrize("failure", ["refresh", "platforms"])
def test_setup_failure_after_activation_restores(monkeypatch, failure):
    hass, entry, events, _bus = _integration_fakes(
        monkeypatch, requested=True, fail_at=failure
    )
    with pytest.raises(Exception, match=failure):
        asyncio.run(integration.async_setup_entry(hass, entry))
    assert "activate" in events
    assert "suspend:True" in events
    assert events.index("suspend:True") > events.index("activate")
    assert entry.entry_id not in hass.data.get("unite_ev_charger", {})
```

Add a failed rollback case asserting the critical log and retained dirty journal.

- [ ] **Step 2: Run and verify RED**

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q tests/test_ownership_lifecycle.py -k "setup_failure"`

Expected: suspension is absent after refresh/platform setup failure.

- [ ] **Step 3: Wrap all post-activation setup work in cleanup**

Track whether the coordinator became dirty and use one exception path:

```python
try:
    if coordinator.automatic_control_requested:
        await coordinator.async_activate()
        await coordinator.async_read_device_info()
        await coordinator.async_config_entry_first_refresh()
    elif coordinator.ownership_dirty:
        if not await coordinator.async_suspend(preserve_requested=True):
            raise WebastoModbusError("Could not finish pending EMS ownership restoration")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
except (Exception, asyncio.CancelledError) as err:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator.ownership_dirty:
        restored = await coordinator.async_suspend(preserve_requested=True)
        if not restored:
            _LOGGER.critical("Setup failed and charger restoration also failed; ownership journal retained")
    if isinstance(err, WebastoModbusError):
        raise ConfigEntryNotReady(f"Could not reach the charger: {err}") from err
    raise
```

Register unload callbacks only after successful platform forwarding; Home Assistant invokes those callbacks for `ConfigEntryNotReady` setup failure, but charger restoration must not depend on that later callback.

- [ ] **Step 4: Test unload and removal semantics**

Add tests proving successful unload restores before platform teardown, `async_remove_entry` retries a retained coordinator once, and a failed removal keeps Store data:

```python
asyncio.run(integration.async_remove_entry(hass, entry))
assert events[:1] == ["suspend:True"]
assert ("remove_journal",) not in events  # restore failed
```

Add `async_remove()` to the Store stub. Implement coordinator cleanup only when not dirty:

```python
async def async_remove_clean_ownership_record(self) -> None:
    if self.ownership_dirty:
        raise WebastoModbusError("Cannot remove a dirty EMS ownership journal")
    await self._ownership_store.async_remove()
```

`async_remove_entry()` retries restoration if the coordinator still exists, removes a clean journal, and logs a critical manual-recovery message without deleting a dirty journal. Do not claim that Home Assistant can restore an unreachable charger after its config entry has been deleted.

- [ ] **Step 5: Remove the 2026.12 double-reload path**

Keep the existing update listener and change the reconfigure helper:

```python
return self.async_update_and_abort(
    entry,
    data_updates=user_input,
)
```

Add a config-flow unit assertion that reconfigure updates once and relies on the listener for reload.

- [ ] **Step 6: Run focused and full tests**

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q tests/test_ownership_lifecycle.py`

Expected: setup, unload, shutdown, and removal lifecycle tests pass.

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q`

Expected: full suite passes.

- [ ] **Step 7: Commit**

```bash
git add custom_components/unite_ev_charger/__init__.py custom_components/unite_ev_charger/coordinator.py custom_components/unite_ev_charger/config_flow.py tests/conftest.py tests/test_ownership_lifecycle.py
git commit -m "fix(lifecycle): restore after setup and removal failures" -m "Route every post-activation setup exception through verified suspension and retain dirty Store data when Home Assistant removal cannot restore an unreachable charger. Also remove the deprecated double-reload config-flow path."
```

---

### Task 4: Stop charging on invalid DLB input

**Files:**
- Modify: `custom_components/unite_ev_charger/controller.py`
- Modify: `tests/test_controller_dlb_safety.py`

**Interfaces:**
- Consumes: existing `_apply_dlb_failsafe()` and `heartbeat_allowed` behavior.
- Produces: every known DLB-input failure sets register 5004 to 0 A, computed setpoint 0, and withholds Alive.

- [ ] **Step 1: Change DLB safety expectations to 0 A**

Update missing/stale/implausible/non-finite tests and transition logging:

```python
assert client.writes == [("set_current_a", 0)]
assert control.computed_setpoint == 0
assert control.heartbeat_allowed is False
assert "applying 0 A stop and withholding Alive" in caplog.text
```

Add one test with `failsafe_current=32` proving invalid meter data still writes 0.

- [ ] **Step 2: Run and verify RED**

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q tests/test_controller_dlb_safety.py`

Expected: current code writes and logs 6 A or 32 A.

- [ ] **Step 3: Implement the zero-current DLB stop**

Keep the method name to minimize churn, but remove the configurable current from this path:

```python
async def _apply_dlb_failsafe(self, data: WallboxData, reason: str) -> None:
    self._set_dlb_health(False, reason)
    self._heartbeat_allowed = False
    self._increase_since = None
    self.computed_setpoint = 0
    await self._write_setpoint(0, current_limit=data.set_current_a, bypass_quiet=True)
```

Change the transition log to say `applying 0 A stop and withholding Alive`. Do not change the wallbox communication-loss failsafe configuration.

- [ ] **Step 4: Run focused and full tests**

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q tests/test_controller_dlb_safety.py`

Expected: all DLB safety tests pass.

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q`

Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add custom_components/unite_ev_charger/controller.py tests/test_controller_dlb_safety.py
git commit -m "fix(dlb)!: stop charging on invalid meter input" -m "A configurable communication failsafe is not proof of grid headroom. Missing, stale, non-finite, or implausible DLB measurements now command 0 A while withholding Alive." -m "BREAKING CHANGE: Invalid DLB input now stops charging instead of applying the configured positive failsafe current."
```

---

### Task 5: Preserve dirty snapshots and publish a commissioning build

**Files:**
- Modify: `custom_components/unite_ev_charger/modbus.py`
- Modify: `custom_components/unite_ev_charger/coordinator.py`
- Modify: `custom_components/unite_ev_charger/controller.py`
- Modify: `custom_components/unite_ev_charger/manifest.json`
- Modify: `README.md`
- Modify: `tests/test_ownership_lifecycle.py`

**Interfaces:**
- Consumes: dirty snapshot persistence and reconnect claim behavior.
- Produces: reset-aware reconnect regression test, accurate protocol comments, commissioning instructions, version `0.2.0-dev.2`.

- [ ] **Step 1: Add a reset-aware snapshot characterization test**

Create an already-dirty coordinator whose fake client changes live registers when opened. Reconnect/activate must reuse persisted originals:

```python
coordinator, client, _events = _coordinator(data={
    "automatic_control": True,
    "ownership_dirty": True,
    "original_current_limit": 20,
    "original_failsafe_current": 12,
    "original_failsafe_timeout": 45,
    "original_phase_switch": 0,
})
client.values.update({
    R.SET_CURRENT_A.name: 6,
    R.FAILSAFE_CURRENT_A.name: 6,
    R.FAILSAFE_TIMEOUT_S.name: 30,
})
client.events.clear()
asyncio.run(coordinator.async_activate())
assert coordinator.original_configuration == OriginalChargerConfig(20, 12, 45, 0)
assert not any(event[0] == "read" for event in client.events)
```

- [ ] **Step 2: Run and verify the existing dirty-session guarantee**

Run: `/tmp/unite-pr-venv/bin/python -m pytest -q tests/test_ownership_lifecycle.py -k reset_values`

Expected: PASS. This is a characterization test for existing persistence behavior;
Task 5 changes protocol claims and release metadata, not runtime snapshot logic.

- [ ] **Step 3: Remove unsupported reset assertions**

Replace comments saying the Vestel specification proves register resets with precise wording:

```python
# Vestel requires a master to program failsafe current, failsafe timeout,
# charging current, and Alive immediately after each new connection. Hardware
# behavior for readable pre-connect values still requires commissioning proof.
```

Retain reconnect reassertion because it is conservative and already expected by the integration; do not claim the official document proves register 405 reset behavior.

- [ ] **Step 4: Document the unplugged commissioning sequence**

Update README:

1. Install branch manually; HACS is optional.
2. Keep vehicle unplugged.
3. Add integration; verify Automatic charger control stays OFF and no Modbus session opens.
4. Return charger to desired autonomous configuration.
5. Enable control once; record `Captured original charger configuration` log values.
6. Disable twice, restart Home Assistant, unload once, and compare 5004/2000/2002/405 after every release.
7. Do not test live charging unless all restores match exactly.

State the physical limitation: TCP first-connect passivity is not yet proven, and an unreachable charger cannot be restored by software.

- [ ] **Step 5: Bump the branch build version and validate artifacts**

Set:

```json
"version": "0.2.0-dev.2"
```

Run:

```bash
/tmp/unite-pr-venv/bin/python -m pytest -q
PYENV_VERSION=3.13.1 python -m compileall -q custom_components tools tests
python -m json.tool custom_components/unite_ev_charger/manifest.json >/dev/null
python -m json.tool custom_components/unite_ev_charger/strings.json >/dev/null
python -m json.tool custom_components/unite_ev_charger/translations/en.json >/dev/null
python -m json.tool custom_components/unite_ev_charger/translations/nl.json >/dev/null
git diff --check
```

Expected: every command exits 0; pytest reports the new total with no failures.

- [ ] **Step 6: Commit and push**

```bash
git add custom_components/unite_ev_charger/modbus.py custom_components/unite_ev_charger/coordinator.py custom_components/unite_ev_charger/controller.py custom_components/unite_ev_charger/manifest.json README.md tests/test_ownership_lifecycle.py
git commit -m "chore(release): prepare 0.2.0-dev.2 hardware trial" -m "Correct unsupported protocol claims, document an unplugged commissioning sequence, and identify this safety-hardened fork build distinctly from upstream."
git push origin codex/dlb-safety-hardening
```

---

## Final review gate

After all tasks, inspect `git diff origin/main...HEAD`, rerun the full verification block, and review specifically for:

- direct `coordinator.client.write_register` calls outside coordinator-locked restoration/claim helpers;
- any default `CONF_AUTOMATIC_CONTROL` value of `True`;
- setup exceptions after a dirty snapshot without suspension;
- DLB-invalid paths selecting a positive current;
- logs or docs claiming official proof of connection-time register resets.

Do not call the build safe for live charging. Call it ready for an EV-unplugged commissioning run; live charging requires successful register restoration evidence from the user's Unite.
