# Unite simulator — test bench

A behavioral fake Webasto Unite: a small Modbus TCP server + web control panel.
Use it to test the integration in Home Assistant (and in automated tests)
without the real wallbox.

> It mirrors the **verified** register map and plausible behaviour. It validates
> the integration and the round-trip — not undocumented firmware quirks. The real
> charger stays the final check.

## Run it (on your Mac)

No dependencies — pure Python 3.11+ standard library:

```bash
python tools/unite_simulator.py
#   Modbus TCP : 0.0.0.0:5020
#   Web panel  : http://localhost:8080
```

Open **http://localhost:8080** for the control panel.

## Point Home Assistant at it (Home Assistant OS)

Your HA runs on a separate box/VM, so the simulator runs on your Mac and HA
connects over the LAN.

1. **Find your Mac's LAN IP:** `ipconfig getifaddr en0` (Wi-Fi) or `en1`.
2. **Allow incoming connections** if macOS firewall is on
   (System Settings → Network → Firewall), or temporarily turn it off.
3. **Install the integration into HA OS:** use the **Samba share** or
   **Studio Code Server** add-on to copy `custom_components/unite_ev_charger`
   into `/config/custom_components/`, then restart Home Assistant.
4. **Add the integration:** Settings → Devices & Services → Add → *Unite EV
   Charger* → host = your Mac's IP, **port = 5020**.

## Control panel

- **Car plugged in** — connect/disconnect the vehicle.
- **Fault** — put the charger in a fault state.
- **Phase mismatch** — register says 3 phases, the car draws 1 (reproduces the
  classic "says 3, charges 1" problem; the *Phases in use* sensor should show 1).
- **Car max current / max phases** — what the vehicle itself accepts.

The panel also shows the live simulated state (charge point state, currents,
power, heartbeat age) so you can watch the integration drive it.

## Testing Solar (closed loop)

The grid/solar surplus comes from a Home Assistant sensor, so add a helper that
reacts to the charger's own power — a realistic feedback loop:

```yaml
# configuration.yaml
input_number:
  fake_solar_surplus:
    name: Fake solar surplus
    min: -2000
    max: 8000
    step: 100
    unit_of_measurement: W

template:
  - sensor:
      - name: Fake grid power
        unit_of_measurement: W
        device_class: power
        # Net grid power with export negative: charger draw minus available surplus.
        state: >
          {{ (states('sensor.unite_ev_charger_power') | float(0))
             - (states('input_number.fake_solar_surplus') | float(0)) }}
```

Then in the integration options → **Power meter** pick *HomeWizard P1 / signed
grid power*, sensor = `sensor.fake_grid_power`, *export is negative* = on.
Set the mode to **Solar** and slide `fake_solar_surplus` up: the charger should
follow it, and (thanks to adding the charger's own power back) settle without
oscillating. Adjust the `sensor.unite_ev_charger_power` entity id if yours
differs.

## Automated tests

```bash
.venv/bin/pip install 'pymodbus>=3.8,<4'   # client side, for the e2e tests
.venv/bin/pytest tests/ -q
```

`tests/test_modbus_e2e.py` starts the simulator in-process and drives the real
Modbus client against it (identity, charging telemetry, phase-mismatch,
failsafe fallback, write path).
