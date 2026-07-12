# Unite EV Charger

[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![GitHub release](https://img.shields.io/github/v/release/Dextro86/unite_ev_charger?display_name=tag)](https://github.com/Dextro86/unite_ev_charger/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-local%20polling-blue.svg)](https://www.home-assistant.io/)

A Home Assistant integration to monitor and control the **Webasto Unite**
(a rebadged Vestel EVC04) over local **Modbus TCP** — with solar-surplus charging,
dynamic load balancing, evcc support and an optional web-UI restart button.

> Built from the ground up for stability: block reads, one persistent connection,
> and a heartbeat/failsafe watchdog, so it stays reliable where simpler setups drift.

**What it does:** local monitoring + charge control, solar/DLB, phase control, evcc
passthrough, and a web-UI reboot.
**What it does not do:** no cloud, no OCPP; it talks only to the charger on your LAN,
and targets the Vestel EVC04 family (Webasto Unite).

Available in **English and Dutch** — Home Assistant picks the user's language.

## Features

- **Monitoring** — status, power, per-phase current & voltage, session energy &
  duration, total energy, plus diagnostics (connection, raw registers 404/405).
- **Charge modes**
  - **Fast** — charge at the maximum allowed current.
  - **Manual** — charge at a current you set.
  - **Solar** — charge from PV surplus only; pauses when there is too little sun.
  - **Minimum plus Solar** — always at least a minimum, follow surplus above it
    (keeps charging at the minimum if the sensor is briefly unavailable).
- **Charging toggle** — a master on/off switch (default on).
- **Default mode** — each new session starts in your configured default
  (reverts when the car is unplugged, evcc-style).
- **Dynamic Load Balancing (DLB)** — caps charging per phase so you never exceed
  the main fuse, accounting for the charger's own draw.
- **Phase control (1↔3)** via a *Phases* preference: **Auto** (solar follows
  surplus with hysteresis/dwell, Fast/Manual use all phases), or force **1**/**3**.
  A switch is normally a single register write — the Unite runs its own IEC CP
  interruption (the approach evcc uses).
- **Adaptive 1→3 phase recovery** *(opt-in, off by default; internal and evcc
  modes)* — some cars cache their 1p/3p choice per session and ignore a live 1→3
  upshift. When enabled, it writes 3-phase and observes; if the car is still
  physically single-phase, it forces a long charging pause (0 A, default 121 s)
  so the car re-negotiates, then resumes. See [Phase switching](#phase-switching).
- **External control (evcc)** — a faithful passthrough that exposes exactly the
  entities evcc's Home Assistant charger expects. See [evcc](#evcc-support).
- **Restart button (web UI)** *(opt-in)* — reboot the wallbox from HA over its
  local web UI, since Modbus has no reboot register. See [Restart](#restart-button-web-ui).
- **Safety & resilience** — failsafe current/timeout, an alive heartbeat, and a
  full ownership handshake re-asserted on every reconnect. See
  [Modbus ownership](#modbus-ownership-failsafe--reconnect).

## Requirements

- A Webasto Unite with **Modbus TCP enabled** (in the charger's web UI).
- The charger's IP address. Default port `502`, Modbus unit id `255`.
- Only **one** Modbus master may talk to the charger at a time — do not run this
  integration and evcc's Modbus charger against the same wallbox simultaneously.

## Installation

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Dextro86&repository=unite_ev_charger&category=integration)

1. HACS → ⋯ → *Custom repositories* → add this repository as an *Integration*
   (or use the button above).
2. Install **Unite EV Charger**, then restart Home Assistant.

### Manual

Copy `custom_components/unite_ev_charger` into your Home Assistant
`config/custom_components/` directory and restart.

## Setup

1. *Settings → Devices & Services → Add Integration → Unite EV Charger*.
2. Enter a name and the charger's IP address (port and unit id are pre-filled).

That gets you monitoring and Fast/Manual control. Solar, DLB, evcc and the restart
button are configured afterwards via **Configure**.

## Configuration (Configure → Settings)

The options are a menu — edit a section, return, and **Save & close** once.

| Screen | What you set |
|---|---|
| **Connection** | Charger IP, port, Modbus unit id |
| **Charging** | Charging control (built-in / external evcc), default mode, min/max current, enable phase switching, optional 1→3 phase recovery |
| **Power meter** | How you measure grid/solar power — HomeWizard P1 (signed), DSMR (import+export), a ready-made surplus sensor, or none. Shared by Solar and DLB. |
| **Dynamic Load Balancing** | Enable, main fuse (A), safety margin, per-phase grid current sensors (L1 required; L2/L3 for a 3-phase connection) |
| **Solar** | Minimum solar current, automatic phase-switch dwell |
| **Advanced** | Polling interval, failsafe current & timeout |
| **Restart (web UI)** | Enable the web-UI login (username/password) to expose the Restart button |

> **Power vs energy:** the meter screens only accept **power** sensors (W/kW).
> Energy sensors (kWh) are rejected — both in the picker and on save.

### Solar surplus calculation
Available surplus is `grid export + the charger's current power`, so the setpoint
is a stable reference and does not chase itself as the car ramps. Charge current
uses the charger's measured voltage (falling back to a configurable nominal).

### DLB
On each phase the room for the car is `fuse − margin − (grid current − charger
current)`; the cap is the lowest across the phases the car charges on. This is the
same approach evcc uses.

## Phase switching

A phase change is normally a **single write** to register `405` — the Unite runs
its own IEC 61851 CP interruption, so no external stop/hold is needed. Whether a
**live** 1→3 switch takes effect mid-session is **car-dependent**: some cars pick
up the extra phases immediately, others cache their 1p/3p choice for the whole
session and **ignore a live upshift** — a plain `405` write (from us *or* evcc)
does not move them.

The **adaptive 1→3 recovery** (opt-in) is for those cars: it writes 3-phase,
observes for a while, and only if the car is still physically single-phase does it
force a long pause (charge current 0 A for the configured dwell, default 121 s) so
the car re-initialises on 3 phases, then resumes. It does **not** write `405` a
second time — the register is already 3-phase; only the pause matters. Two
diagnostic sensors show the recovery state and remaining time.

> **Known Unite bug — stuck on 1-phase after a new session.** Separate from the
> car-dependent behaviour above, the Unite firmware itself sometimes gets stuck:
> after one charging session ends and a new one starts, the charger can begin on a
> single phase even though 3-phase is requested (`405 = 3`) and stay locked that
> way. A live phase switch does not clear it — the only reliable fix is a
> **soft reset** of the charger, which you can trigger from the
> [Restart button](#restart-button-web-ui).

Note: register `405` resets to its `404` default on **every** Modbus disconnect
(see below), so the integration re-asserts the desired phase after each reconnect.

## Modbus ownership, failsafe & reconnect

The Vestel firmware expects the master to *own* the connection, and the
integration follows that contract:

- **Failsafe** — on every new connection it writes the failsafe current + timeout,
  then an **alive** heartbeat each cycle. If the heartbeat lapses past the timeout,
  the wallbox drops to its failsafe current and **closes the socket** itself.
- **Heartbeat cadence** — the alive register must be refreshed faster than
  `failsafe_timeout / 2`, so the effective poll interval is clamped to
  `min(poll, max(3 s, timeout/2))`, whatever you configure.
- **Register 405 resets on disconnect** — per the spec, the phase register returns
  to its `404` default on any TCP disconnect, power cycle or reset. After each
  reconnect the integration re-asserts the intended phase (and the charging
  current), so a brief drop never silently changes your phase.
- **Reconnect handshake** — a power-cycled or still-booting wallbox is picked up
  automatically: on the next successful poll the integration re-claims ownership
  (failsafe + charging current + phase + alive).
- **Robust 404 read** — the phase-capability register is re-read every cycle (a
  Unite can report it wrong while booting), so a single bad read never disables
  phase switching for the session.

## Restart button (web UI)

Modbus has no reboot register, so a restart goes over the charger's local **web
UI**. Different Unite firmware/interfaces expose different web UIs, so the button
**auto-detects** the right one: the modern JSON API over HTTPS (on port `443` or
`4443`, self-signed certificate) or the legacy "webconfig" portal over HTTP. If the
JSON API is present but has no restart endpoint on that firmware, it automatically
falls back to the webconfig soft-reset — so the button works across Unite variants.

It is **opt-in**: enable it under *Settings → Restart (web UI)* and enter the web-UI
username (default `admin`) and password; only then does the **Restart** button
appear on the device. Credentials are validated with a test login when you enable it.

The button is fully isolated from the Modbus control path — if REST fails, charging
is unaffected. After a reboot the wallbox drops Modbus and the reconnect handshake
re-claims it automatically. Measured on real hardware: Modbus returns in ~4–5 min
and the web UI in ~5 min, so repeat presses are ignored for a short cooldown.

## Use in evcc

Set *Charging control* to **External** and the built-in loop goes passive; the
entities become a faithful passthrough for evcc's
[Home Assistant charger](https://docs.evcc.io/en/docs/devices/chargers#home-assistant).
The entity IDs are **fixed and language-independent** (they don't change with your
Home Assistant language), so you can copy this straight into your `evcc.yaml`:

```yaml
chargers:
  - name: unite
    type: template
    template: homeassistant
    baseurl: http://homeassistant.local:8123   # or http://<HA-IP>:8123
    token: <long-lived-access-token>            # HA -> profile -> Long-lived access tokens
    status:     sensor.unite_ev_charger_evcc_status
    enabled:    switch.unite_ev_charger_charging_enabled
    enable:     switch.unite_ev_charger_charging_enabled
    maxcurrent: number.unite_ev_charger_charge_current
    # optional telemetry:
    power:      sensor.unite_ev_charger_active_power
    energy:     sensor.unite_ev_charger_meter_energy
    # optional 1p/3p phase switching:
    phases1p3p: select.unite_ev_charger_phase_select
```

The IDs above are what a single charger gets. If you added a **second** charger,
Home Assistant appends a suffix (`..._2`) — check yours under
*Developer Tools → States* (filter `unite_ev_charger`).

The heartbeat keeps running so the wallbox never drops to failsafe; evcc owns all
charging decisions. Mode/Solar/DLB entities are unavailable in this mode.
