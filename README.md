# Unite EV Charger

[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![GitHub release](https://img.shields.io/github/v/release/Dextro86/unite_ev_charger?display_name=tag)](https://github.com/Dextro86/unite_ev_charger/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-local%20polling-blue.svg)](https://www.home-assistant.io/)

A Home Assistant integration to monitor and control the **Webasto Unite**
(a rebadged Vestel EVC04) over local **Modbus TCP** — with solar-surplus charging,
fuse guarding, 1↔3 phase control, evcc support and an optional web-UI restart button.

> Built for stability: block reads, one persistent connection, and a
> heartbeat/failsafe watchdog — plus firmware-tolerant handling of optional
> registers, so old wallboxes stay online too.

**What it does:** local monitoring + charge control, solar/fuse guard, phase control,
evcc passthrough, and a web-UI reboot. When the integration leaves, it puts the
charger's registers back the way it found them.

**What it does not do:** cloud and OCPP; it talks only to the charger on your LAN,
and targets the Vestel EVC04 family (Webasto Unite).

Available in **English and Dutch** — Home Assistant picks the user's language.

## Features

- **Monitoring** — status, power, per-phase current & voltage, session energy &
  duration, total energy, plus diagnostics (connection, raw registers 404/405).
- **Charge modes**
  - **Fast** — charge at the maximum allowed current.
  - **Manual** — charge at a current you set.
  - **Solar** — charge from PV surplus only; pauses when there is too little sun.
  - **Minimum + Solar** — always at least a minimum, follow surplus above it.
- **Charging toggle** — a master on/off switch (default on).
- **Starting mode** — each new session starts with your configured mode.
- **Fuse guard (DLB)** — caps charging per phase so you never exceed
  the main fuse, accounting for the charger's own draw. Fails closed: on
  missing or stale meter data charging pauses instead of guessing.
- **Phase control (1↔3)** — switch between 1-phase (efficient with little sun)
  and 3-phase (fast). A switch is normally a single register write — the Unite
  runs its own IEC CP interruption (the approach evcc uses).
- **Help for cars stuck on 1 phase** *(opt-in, off by default)* — some cars
  cache their 1p/3p choice per session and ignore a live 1→3 upshift. When
  enabled, the integration observes and, only if the car is still physically
  single-phase, forces a long charging pause (0 A) so the car re-negotiates,
  then resumes.
- **Restore for Unite bug (stuck on 1 phase)** *(opt-in, off by default)* —
  works around a charger bug where a new session starts on 1 phase even though
  3 phases are configured. After every unplug the charger is briefly set to
  1 phase and back to 3 phases. Requires the web UI login and a three-phase
  connection.
- **External control (evcc)** — a faithful passthrough that exposes exactly the
  entities evcc's Home Assistant charger expects, or a full custom charger
  definition with RFID identification and phase switching. See
  [Use in evcc](#use-in-evcc).
- **Restart button (web UI)** *(opt-in)* — reboot the wallbox from HA over its
  local web UI, since Modbus has no reboot register. See
  [Web UI details](#web-ui-details).
- **Safety & resilience** — failsafe current/timeout, an alive heartbeat, a full
  ownership handshake on every reconnect, and a register baseline that is
  captured before the first write and restored on exit. See
  [Leaving the integration](#leaving-the-integration).

## Requirements

- A Webasto Unite with **Modbus TCP enabled** (in the charger's web UI).
- The charger's IP address. Default port `502`, Modbus unit id `255`.
- Only **one** Modbus master may talk to the charger at a time — do not run this
  integration and evcc's Modbus charger against the same wallbox simultaneously.
- Older firmware is supported: registers your firmware lacks (such as the RFID
  tag) are detected once and then left alone. See
  [Firmware differences](#firmware-differences).

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

That gets you monitoring and Fast/Manual control. Everything else is configured
afterwards via **Configure**, screen by screen below.

## Settings

### Charging

How the charger charges. Most people only need *Who controls charging* and
*Charger connection* here.

- **Who controls charging?** — Built-in means this integration decides
  (solar/fuse guard/manual). External means evcc (or another controller)
  decides and this integration only passes it on.
- **Starting mode** — every session starts with this when you plug in.
- **Minimum current** — normally 6 A, adjustable 6–32 A.
- **Maximum current** — normally 16 A (about 11 kW on 3 phases); never lower
  than the minimum.
- **Charger connection** — single- or three-phase, as your electrician wired
  it. This also keeps the phase fixes below from running on the wrong
  installation.
- **Switching between 1 and 3 phases** — only on a three-phase connection.
- **Help cars stuck on 1 phase** — for cars that don't pick up 3 phases by
  themselves; briefly interrupts charging, so off by default.
- **Restore for Unite bug (stuck on 1 phase)** — pushes the 3-phase setting to
  the charger again after every unplug. Requires the web UI login and a
  three-phase connection.

### Power meter

Tells the charger how much solar surplus you have. Used for solar charging and
the fuse guard. Pick the meter you have: none, HomeWizard P1 / signed grid
power, DSMR (import + export), or a ready-made surplus sensor — then pick the
corresponding sensor(s) on the follow-up screen. The nominal voltage (usually
230 V) is only used to convert watts to amps.

### Fuse guard (DLB)

Watches your main fuse: charging is throttled when your home uses a lot of
power. Set the main fuse (usually 25 A — check your meter cupboard), a small
safety margin (normally 2 A), and the home-usage current sensor(s): phase 1 is
required, phases 2 and 3 only on a three-phase connection.

### Solar charging

Fine-tuning for the Solar and Minimum + Solar modes: the minimum solar current
(charging starts when the surplus covers it, normally 6 A) and the pause
between phase switches (so a passing cloud doesn't cause constant switching,
normally 300 s).

### Advanced

Only change these if you know why. Wrong values can stall charging.

- **Check-in frequency** — how often the charger is polled (normally 10 s).
- **Backup current if contact is lost** — what the charger falls back to after
  silence (normally 6 A; 0 A stops charging entirely).
- **Waiting time before backup** — how long the silence may last (normally 30 s).
- **Watching time before 1-phase help** (normally 60 s), **pause length for
  1-phase help** (normally 121 s) and **waiting time after unplugging**
  (normally 5 s) — these only apply when their switch on the Charging screen
  is on.

### Web UI

Logs in to the charger's web interface. This adds a Restart button — only use
it when the charger is stuck, it stops an ongoing charging session. The login
is also required for *Restore for Unite bug* on the Charging screen. The
password is stored locally only.

### Status & diagnostics

The integration exposes a diagnostic **Connection** binary sensor with
reconnect counters, Modbus failure counters, timeout counters, heartbeat
failures and response timing.

The **Charger state** sensor interprets the charger state as one of: idle,
connected, charging, phase mismatch, recovery, restarting, disconnected or
fault. The diagnostic **Phase mismatch** binary sensor turns on when 3-phase
was explicitly requested while measured current shows the vehicle is still
effectively charging on L1 only. The diagnostic **Last phase recovery** sensor
stores the timestamp and result of the latest recovery attempt.

## Phase switching

A phase change is normally a **single write** to register `405` — the Unite runs
its own IEC 61851 CP interruption, so no external stop/hold is needed. Whether a
**live** 1→3 switch takes effect mid-session is **car-dependent**: some cars pick
up the extra phases immediately, others cache their 1p/3p choice for the whole
session and **ignore a live upshift** — a plain `405` write (from us *or* evcc)
does not move them.

The **1-phase help** (opt-in) is for those cars: it writes 3-phase, observes
for a while, and only if the car is still physically single-phase does it
force a long pause (charge current 0 A for the configured time) so the car
re-initialises on 3 phases, then resumes. It does **not** write `405` a
second time — the register is already 3-phase; only the pause matters. Two
diagnostic sensors show the recovery state and remaining time.

Note: register `405` resets to its `404` default on **every** Modbus disconnect,
so the integration re-asserts the desired phase after each reconnect.

## Restore for Unite bug (stuck on 1 phase)

Separate from the car-dependent behaviour above, the Unite firmware itself
sometimes gets stuck: after one charging session ends and a new one starts, the
charger can begin on a single phase even though 3-phase is requested and stay
locked that way. A live phase switch does not clear it.

When enabled, the integration re-applies the installation phase config after
**every** unplug: it waits a few seconds (so the charger can finish the session;
plugging back in within this time cancels the restore), then briefly sets the
charger to 1 phase and back to 3 phases over the web UI, forcing the firmware
to apply its own default. This runs over the **web UI** (Modbus has no such
register), so it needs the Web UI login, and it never runs on a genuine
1-phase installation.

If the charger itself is thoroughly stuck (a plain switch doesn't clear it),
the reliable fix remains a **restart** — see [Web UI details](#web-ui-details).

## Modbus ownership, failsafe & reconnect

The Vestel firmware expects the master to *own* the connection, and the
integration follows that contract:

- **Failsafe** — on every new connection it writes the failsafe current +
  timeout, then an **alive** heartbeat each cycle. If the heartbeat lapses past
  the timeout, the wallbox drops to its failsafe current and **closes the
  socket** itself.
- **Heartbeat cadence** — the alive register must be refreshed faster than
  `failsafe_timeout / 2`, so the effective poll interval is clamped to
  `min(poll, max(3 s, timeout/2))`, whatever you configure.
- **Register 405 resets on disconnect** — per the spec, the phase register
  returns to its `404` default on any TCP disconnect, power cycle or reset.
  After each reconnect the integration re-asserts the intended phase (and the
  charging current), so a brief drop never silently changes your phase.
- **Reconnect handshake** — a power-cycled or still-booting wallbox is picked
  up automatically: on the next successful poll the integration re-claims
  ownership (failsafe + charging current + phase + alive).
- **Robust 404 read** — the phase-capability register is re-read every cycle
  (a Unite can report it wrong while booting), so a single bad read never
  disables phase switching for the session.

## Leaving the integration

Register `2000` (failsafe current) is persistent user-visible configuration:
it survives a Modbus disconnect and even a power cycle, and a stale value
actively drives behaviour — the charger overwrites the charge current with it
once Alive lapses. Whatever an integration leaves in `2000` is what the
charger applies on every future communication loss, indefinitely.

So before its first write, this integration captures the registers it manages
(charge current, failsafe current/timeout, and the phase selection when phase
control is enabled) and stores them durably. On unload, removal and Home
Assistant shutdown it writes them back and verifies by read-back. Anything
that cannot be restored is logged with its value for manual recovery. The
baseline answers "before us", never a guessed factory default — and changing
your settings later does not touch it.

## Web UI details

Modbus has no reboot register, so a restart goes over the charger's local **web
UI**. Different Unite firmware/interfaces expose different web UIs, so the
integration **auto-detects** the right one: the modern JSON API over HTTPS (on
port `443` or `4443`, self-signed certificate) or the legacy "webconfig" portal
over HTTP. If the JSON API is present but has no restart endpoint on that
firmware, it automatically falls back to the webconfig reset — so it works
across Unite variants.

It performs a **hard reset** (restart immediately, regardless of state), so it
is the same on every firmware. Note that this **interrupts an active charging
session** — it's meant as a deliberate manual action.

It is **opt-in**: enable it under *Settings → Web UI* and enter the web-UI
username (usually `admin`) and password; only then do the restart controls
appear. Credentials are validated with a test login when you enable it.

The restart is fully isolated from the Modbus control path — if the web UI
fails, charging is unaffected. After a reboot the wallbox drops Modbus and the
reconnect handshake re-claims it automatically. The restart button has a 300
second cooldown because the charger web UI can stay offline for several minutes
while Modbus is already back. The diagnostic Last restart sensor stores the
timestamp and result of the latest restart request without polling the REST
API periodically.

## Use in evcc

Set *Who controls charging* to **External** and the built-in loop goes passive.
Three ways to connect evcc; no evcc sponsor token is needed for any of them.

### Option 1 — evcc web UI (easiest)

In the evcc web UI go to Configuration, add a charger of type Home Assistant,
pick your instance (auto-discovered) and select the entities from the
dropdowns — use the entity reference below to pick the right ones. evcc
handles the Home Assistant login itself; no token to copy.

### Option 2 — template in evcc.yaml

The entities for evcc's
[Home Assistant charger](https://docs.evcc.io/en/chargers/home-assistant-charger/).
The entity IDs are **fixed and language-independent** (they don't change with your
Home Assistant language), so you can copy this straight into your `evcc.yaml`.
Needs a Home Assistant long-lived access token
(HA → profile → Long-lived access tokens):

```yaml
chargers:
  - name: unite
    type: template
    template: homeassistant
    uri: http://homeassistant.local:8123     # or http://<HA-IP>:8123
    token: <long-lived-access-token>          # HA -> profile -> Long-lived access tokens
    status: sensor.unite_ev_charger_evcc_status
    enabled: switch.unite_ev_charger_charging_enabled
    enable: switch.unite_ev_charger_charging_enabled
    setMaxCurrent: number.unite_ev_charger_charge_current
    # optional telemetry:
    power: sensor.unite_ev_charger_active_power
    energy: sensor.unite_ev_charger_meter_energy
    currentL1: sensor.unite_ev_charger_current_l1
    currentL2: sensor.unite_ev_charger_current_l2
    currentL3: sensor.unite_ev_charger_current_l3
    voltageL1: sensor.unite_ev_charger_voltage_l1
    voltageL2: sensor.unite_ev_charger_voltage_l2
    voltageL3: sensor.unite_ev_charger_voltage_l3
    # optional 1p/3p phase switching:
    phaseswitch: select.unite_ev_charger_phase_preference
```

The IDs above are what a single charger gets. If you added a **second** charger,
Home Assistant appends a suffix (`..._2`) — check yours under
*Developer Tools → States* (filter `unite_ev_charger`).

Full entity reference:

| evcc field | Entity ID |
|---|---|
| `status` | `sensor.unite_ev_charger_evcc_status` |
| `enabled` / `enable` | `switch.unite_ev_charger_charging_enabled` |
| `setMaxCurrent` | `number.unite_ev_charger_charge_current` |
| `power` | `sensor.unite_ev_charger_active_power` |
| `energy` | `sensor.unite_ev_charger_meter_energy` |
| `currentL1` / `L2` / `L3` | `sensor.unite_ev_charger_current_l1` / `_l2` / `_l3` |
| `voltageL1` / `L2` / `L3` | `sensor.unite_ev_charger_voltage_l1` / `_l2` / `_l3` |
| `phaseswitch` | `select.unite_ev_charger_phase_preference` |

The heartbeat keeps running so the wallbox never drops to failsafe; evcc owns all
charging decisions. Mode/Solar/DLB entities are unavailable in this mode.

### Option 3 — custom charger (RFID + phases via the evcc UI)

The template has no `identify` field, so RFID vehicle identification needs a
user-defined (`type: custom`) charger — which can also be built in the evcc web
UI. Like option 2 this talks to the Home Assistant API directly, so it needs
the URI and a long-lived access token. Replace `http://homeassistant.local:8123`
and `<TOKEN>` below (and add the `_2` suffix if you have a second charger):

```yaml
status:
  source: http
  uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_evcc_status
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state
enabled:
  source: http
  uri: http://homeassistant.local:8123/api/states/switch.unite_ev_charger_charging_enabled
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state == "on"
enable:
  source: ifelse
  if:
    source: http
    uri: http://homeassistant.local:8123/api/services/switch/turn_on
    method: POST
    headers:
      - Authorization: Bearer <TOKEN>
      - Content-Type: application/json
    body: '{"entity_id": "switch.unite_ev_charger_charging_enabled"}'
  else:
    source: http
    uri: http://homeassistant.local:8123/api/services/switch/turn_off
    method: POST
    headers:
      - Authorization: Bearer <TOKEN>
      - Content-Type: application/json
    body: '{"entity_id": "switch.unite_ev_charger_charging_enabled"}'
maxcurrent:
  source: http
  uri: http://homeassistant.local:8123/api/services/number/set_value
  method: POST
  headers:
    - Authorization: Bearer <TOKEN>
    - Content-Type: application/json
  body: '{"entity_id": "number.unite_ev_charger_charge_current", "value": ${maxcurrent}}'
power:
  source: http
  uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_active_power
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state | tonumber
energy:
  source: http
  uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_meter_energy
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state | tonumber
currents:
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_current_l1
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_current_l2
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_current_l3
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
voltages:
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_voltage_l1
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_voltage_l2
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_voltage_l3
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
identify:
  source: http
  uri: http://homeassistant.local:8123/api/states/sensor.unite_ev_charger_session_rfid
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state
phases1p3p:
  source: http
  uri: http://homeassistant.local:8123/api/services/select/select_option
  method: POST
  headers:
    - Authorization: Bearer <TOKEN>
    - Content-Type: application/json
  body: '{"entity_id": "select.unite_ev_charger_phase_preference", "value": "${phases1p3p}"}'
tos: true
```

The charger reports the RFID tag of the running session
(`sensor.unite_ev_charger_session_rfid`). It is empty when charging freely.
evcc matches it against the `identifiers` of your vehicles (see
[vehicle identification](https://docs.evcc.io/en/reference/configuration/vehicles/))
to assign a session to a vehicle. `evcc charger` in a terminal shows per
attribute whether it works.

## Firmware differences

Not every Unite firmware serves the same registers. The integration handles
that by construction:

- **Required** (telemetry, session, control path): these exist on all known
  firmware. A failure here is treated as a real outage (reconnect, retry).
- **Optional** (currently only the session RFID tag, Modbus `1516-1530`,
  firmware from Vestel spec v1.9 / 2023 onward): probed once per connection.
  A clean refusal disables it until the next reconnect; repeated
  timeouts disable it for the session. The sensor reads "unknown" and
  everything else keeps working — no log ping-pong, no retry storms, and a
  failed probe never drops the connection.
