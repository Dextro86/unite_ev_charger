#!/usr/bin/env python3
"""Behavioral Webasto Unite simulator.

A tiny self-contained Modbus TCP server + web control panel that mimics a
Webasto Unite, so the Unite EV Charger integration can be tested in Home
Assistant (or in automated tests) without the real wallbox.

Dev tool only - pure asyncio + stdlib, no pymodbus dependency, so it cannot
break when pymodbus reshuffles its server API.

Run:  python tools/unite_simulator.py
  Modbus TCP : 0.0.0.0:5020
  Web panel  : http://localhost:8080

The register map mirrors custom_components/unite_ev_charger/registers.py:
  INPUT  (fc4): identity + telemetry (1000.., 1502.., 100/190/210/230, 404)
  HOLDING(fc3/6/16): control (405, 2000, 2002, 5004, 6000)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOGGER = logging.getLogger("unite_sim")

NOMINAL_V = 230
MIN_A = 6


class Scenario:
    """Knobs driven by the web panel (plain attributes; mutated from the HTTP thread)."""

    plugged: bool = True
    car_max_current: int = 16
    car_max_phases: int = 3
    phase_mismatch: bool = False  # register says 3 phases, the car only draws 1
    fault: bool = False


class UniteState:
    """The simulated register file + behavioural state."""

    def __init__(self) -> None:
        self.input: dict[int, int] = {}
        self.holding: dict[int, int] = {}
        self.last_heartbeat = time.monotonic()
        self.session_wh = 0.0
        self.meter_wh = 1234_000.0  # start with some lifetime energy
        self.session_start: float | None = None
        self._last_tick = time.monotonic()

        # Control defaults (holding).
        self.holding[5004] = 0   # set charge current (A)
        self.holding[405] = 1    # phase setting: 1 = three-phase
        self.holding[2000] = 6   # failsafe current (A)
        self.holding[2002] = 30  # failsafe timeout (s)
        self.holding[6000] = 0   # alive

        # Static identity (input).
        self._set_string(100, "SIM-UNITE-0001", 25)
        self._set_string(190, "Webasto", 10)
        self._set_string(210, "Unite", 5)
        self._set_string(230, "1.2.3-sim", 50)
        # Phase capability per spec: 0 = 1-phase, 1 = 3-phase - NOT a phase count.
        # Confirmed on real hardware: a 3-phase charger reports 1 here.
        self.input[404] = 1
        self.input[1102] = MIN_A   # min hw current
        self.input[1106] = 32      # cable max current
        for v_addr in (1014, 1016, 1018):
            self.input[v_addr] = NOMINAL_V

    # -- register helpers ---------------------------------------------------
    def _set_string(self, addr: int, text: str, count: int) -> None:
        data = text.encode("ascii", "ignore").ljust(count * 2, b"\x00")
        for i in range(count):
            self.input[addr + i] = (data[2 * i] << 8) | data[2 * i + 1]

    def _set_u16(self, addr: int, value: int) -> None:
        self.input[addr] = max(0, min(0xFFFF, int(value)))

    def _set_u32(self, addr: int, value: int) -> None:
        value = max(0, int(value))
        self.input[addr] = (value >> 16) & 0xFFFF
        self.input[addr + 1] = value & 0xFFFF

    # -- behaviour ----------------------------------------------------------
    def tick(self, scn: Scenario) -> None:
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now

        set_a = self.holding.get(5004, 0)
        failsafe_a = self.holding.get(2000, 6)
        timeout = self.holding.get(2002, 30)

        # Heartbeat watchdog: fall back to the failsafe current when stale.
        stale = (now - self.last_heartbeat) > timeout
        effective = failsafe_a if stale else set_a

        reg_phases = 3 if self.holding.get(405, 1) == 1 else 1

        if not scn.plugged:
            cp, cable, currents = 0, 0, [0.0, 0.0, 0.0]
            self.session_wh = 0.0
            self.session_start = None
        elif scn.fault:
            cp, cable, currents = 8, 2, [0.0, 0.0, 0.0]
        elif effective < MIN_A:
            cp, cable, currents = 3, 2, [0.0, 0.0, 0.0]  # suspended
        else:
            cp, cable = 2, 2
            draw = min(effective, scn.car_max_current)
            active = min(reg_phases, scn.car_max_phases)
            if scn.phase_mismatch:
                active = 1
            currents = [float(draw) if i < active else 0.0 for i in range(3)]

        charging = cp == 2 and any(currents)
        power = sum(currents) * NOMINAL_V
        if charging:
            if self.session_start is None:
                self.session_start = now
            self.meter_wh += power * dt / 3600.0
            self.session_wh += power * dt / 3600.0

        # Publish telemetry (input registers).
        self.input[1000] = cp
        self.input[1001] = 1 if charging else 0
        self.input[1004] = cable
        self._set_u16(1008, int(currents[0] * 1000))
        self._set_u16(1010, int(currents[1] * 1000))
        self._set_u16(1012, int(currents[2] * 1000))
        self._set_u32(1020, int(power))
        self._set_u32(1036, int(self.meter_wh / 100))   # scale 0.1 kWh
        self._set_u32(1502, int(self.session_wh))        # scale 0.001 kWh
        duration = int(now - self.session_start) if self.session_start else 0
        self._set_u32(1508, duration)

    def snapshot(self, scn: Scenario) -> dict:
        return {
            "cp_state": self.input.get(1000),
            "cable_state": self.input.get(1004),
            "set_current": self.holding.get(5004),
            "phase_reg": "3p" if self.holding.get(405) == 1 else "1p",
            "currents_a": [
                self.input.get(1008, 0) / 1000,
                self.input.get(1010, 0) / 1000,
                self.input.get(1012, 0) / 1000,
            ],
            "power_w": (self.input.get(1020, 0) << 16) | self.input.get(1021, 0),
            "heartbeat_age_s": round(time.monotonic() - self.last_heartbeat, 1),
            "scenario": {
                "plugged": scn.plugged,
                "car_max_current": scn.car_max_current,
                "car_max_phases": scn.car_max_phases,
                "phase_mismatch": scn.phase_mismatch,
                "fault": scn.fault,
            },
        }


# --- Modbus TCP server ------------------------------------------------------
def _read_block(store: dict[int, int], addr: int, qty: int) -> bytes:
    vals = [store.get(addr + i, 0) for i in range(qty)]
    body = b"".join(int(v).to_bytes(2, "big") for v in vals)
    return body


def _process_pdu(pdu: bytes, state: UniteState) -> bytes:
    fc = pdu[0]
    try:
        if fc in (3, 4):  # read holding / input
            addr = int.from_bytes(pdu[1:3], "big")
            qty = int.from_bytes(pdu[3:5], "big")
            store = state.holding if fc == 3 else state.input
            body = _read_block(store, addr, qty)
            return bytes([fc, len(body)]) + body
        if fc == 6:  # write single holding
            addr = int.from_bytes(pdu[1:3], "big")
            val = int.from_bytes(pdu[3:5], "big")
            state.holding[addr] = val
            if addr == 6000:
                state.last_heartbeat = time.monotonic()
            return pdu  # echo
        if fc == 16:  # write multiple holding
            addr = int.from_bytes(pdu[1:3], "big")
            qty = int.from_bytes(pdu[3:5], "big")
            data = pdu[6:]
            for i in range(qty):
                state.holding[addr + i] = int.from_bytes(data[2 * i : 2 * i + 2], "big")
            if addr <= 6000 < addr + qty:
                state.last_heartbeat = time.monotonic()
            return bytes([fc]) + pdu[1:5]
    except Exception:  # noqa: BLE001
        pass
    return bytes([fc | 0x80, 0x01])  # illegal function / error


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: UniteState) -> None:
    try:
        while True:
            header = await reader.readexactly(6)  # tid(2) pid(2) length(2)
            tid = header[0:2]
            length = int.from_bytes(header[4:6], "big")
            rest = await reader.readexactly(length)
            uid = rest[0]
            pdu = rest[1:]
            resp_pdu = _process_pdu(pdu, state)
            frame = tid + b"\x00\x00" + (len(resp_pdu) + 1).to_bytes(2, "big") + bytes([uid]) + resp_pdu
            writer.write(frame)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


async def _tick_loop(state: UniteState, scenario: Scenario, interval: float = 1.0) -> None:
    while True:
        state.tick(scenario)
        await asyncio.sleep(interval)


async def start(state: UniteState, scenario: Scenario, host: str, port: int, with_tick: bool = True):
    """Start the Modbus server (+ optional tick loop). Returns (server, tick_task).

    Tests pass with_tick=False and call ``state.tick()`` themselves for
    determinism; the standalone server uses the background tick loop.
    """
    server = await asyncio.start_server(lambda r, w: _handle_client(r, w, state), host, port)
    tick_task = asyncio.create_task(_tick_loop(state, scenario)) if with_tick else None
    return server, tick_task


# --- Web control panel (stdlib HTTP, runs in a thread) ----------------------
_PANEL_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Unite simulator</title><style>
body{font-family:system-ui;margin:2rem;max-width:540px}
button{font-size:1rem;padding:.5rem 1rem;margin:.25rem 0;cursor:pointer}
.row{margin:.6rem 0}.on{background:#1b8a3a;color:#fff}.off{background:#aaa;color:#fff}
pre{background:#111;color:#7CFC9A;padding:1rem;border-radius:8px;overflow:auto}
input[type=range]{width:220px;vertical-align:middle}
</style></head><body>
<h2>Unite EV Charger simulator</h2>
<div class="row"><button id="plug"></button> car plugged in</div>
<div class="row"><button id="fault"></button> fault</div>
<div class="row"><button id="mismatch"></button> phase mismatch (reg 3p, car 1p)</div>
<div class="row">Car max current: <input type="range" id="cur" min="6" max="16" step="1">
<span id="curv"></span> A</div>
<div class="row">Car max phases:
<button data-ph="1" class="ph">1</button><button data-ph="3" class="ph">3</button></div>
<h3>Live state</h3><pre id="state">...</pre>
<script>
async function post(k,v){await fetch('/set?'+k+'='+v,{method:'POST'});refresh();}
async function refresh(){
 const s=await (await fetch('/state')).json();const sc=s.scenario;
 document.getElementById('plug').textContent=sc.plugged?'ON':'OFF';
 document.getElementById('plug').className=sc.plugged?'on':'off';
 document.getElementById('fault').textContent=sc.fault?'ON':'OFF';
 document.getElementById('fault').className=sc.fault?'on':'off';
 document.getElementById('mismatch').textContent=sc.phase_mismatch?'ON':'OFF';
 document.getElementById('mismatch').className=sc.phase_mismatch?'on':'off';
 document.getElementById('cur').value=sc.car_max_current;
 document.getElementById('curv').textContent=sc.car_max_current;
 document.getElementById('state').textContent=JSON.stringify(s,null,2);
}
document.getElementById('plug').onclick=()=>post('plugged',document.getElementById('plug').textContent=='ON'?0:1);
document.getElementById('fault').onclick=()=>post('fault',document.getElementById('fault').textContent=='ON'?0:1);
document.getElementById('mismatch').onclick=()=>post('phase_mismatch',document.getElementById('mismatch').textContent=='ON'?0:1);
document.getElementById('cur').oninput=(e)=>post('car_max_current',e.target.value);
document.querySelectorAll('.ph').forEach(b=>b.onclick=()=>post('car_max_phases',b.dataset.ph));
setInterval(refresh,1000);refresh();
</script></body></html>"""


def _run_panel(state: UniteState, scenario: Scenario, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, code, body, ctype="text/html"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode() if isinstance(body, str) else body)

        def do_GET(self):
            if self.path.startswith("/state"):
                self._send(200, json.dumps(state.snapshot(scenario)), "application/json")
            else:
                self._send(200, _PANEL_HTML)

        def do_POST(self):
            from urllib.parse import urlparse, parse_qs

            params = parse_qs(urlparse(self.path).query)
            for key, vals in params.items():
                raw = vals[0]
                if key in ("plugged", "fault", "phase_mismatch"):
                    setattr(scenario, key, raw in ("1", "true", "True"))
                elif key in ("car_max_current", "car_max_phases"):
                    setattr(scenario, key, int(raw))
            self._send(200, "ok")

    ThreadingHTTPServer((host, port), Handler).serve_forever()


async def _main_async(args) -> None:
    state = UniteState()
    scenario = Scenario()
    server, _ = await start(state, scenario, args.host, args.modbus_port)
    threading.Thread(
        target=_run_panel, args=(state, scenario, args.host, args.panel_port), daemon=True
    ).start()
    _LOGGER.info("Modbus TCP on %s:%s  |  panel http://localhost:%s", args.host, args.modbus_port, args.panel_port)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Webasto Unite simulator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--modbus-port", type=int, default=5020)
    parser.add_argument("--panel-port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
