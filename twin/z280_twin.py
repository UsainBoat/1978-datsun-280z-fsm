"""1978 Datsun 280Z (L28E / Bosch L-Jetronic) Diagnostic Digital Twin.

Models factory 1978 power distribution, dual-relay fuel pump safety interlock,
35-pin Bosch L-Jetronic ECU operating modes, and vehicle-specific as-built
modifications (GM 4-pin HEI, dual-relay headlamps, Civic wiper motor).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Set


# Factory NTC thermistor curve (EF-32 / EF-33 / STATE_OF_THE_Z):
#   -10°C (14°F)  -> 9,000 Ω (spec 7.6–10.8 kΩ)
#    10°C (50°F)  -> 3,700 Ω (spec 3.25–4.15 kΩ)
#    20°C (68°F)  -> 2,500 Ω (spec 2.25–2.75 kΩ)
#    50°C (122°F) ->   840 Ω (spec 0.74–0.94 kΩ)
#    80°C (176°F) ->   325 Ω (spec 0.29–0.36 kΩ)
CTS_CURVE: List[tuple[float, float]] = [
    (-20.0, 15000.0),
    (-10.0, 9000.0),
    (10.0, 3700.0),
    (20.0, 2500.0),
    (50.0, 840.0),
    (80.0, 325.0),
    (100.0, 185.0),
]


def cts_resistance_ohms(temp_c: float) -> float:
    """Interpolate factory NTC Water Temperature Sensor resistance (EF-32 / EF-33)."""
    if temp_c <= CTS_CURVE[0][0]:
        return CTS_CURVE[0][1]
    if temp_c >= CTS_CURVE[-1][0]:
        return CTS_CURVE[-1][1]
    for (t0, r0), (t1, r1) in zip(CTS_CURVE, CTS_CURVE[1:]):
        if t0 <= temp_c <= t1:
            frac = (temp_c - t0) / (t1 - t0)
            return round(r0 + frac * (r1 - r0), 1)
    return 2500.0


def cts_temp_c_from_ohms(ohms: float) -> float:
    """Invert the NTC Water Temperature Sensor curve to perceived ECU coolant temperature (°C)."""
    if ohms <= 0.0:
        return 120.0
    if ohms >= CTS_CURVE[0][1]:
        return -40.0 if ohms >= 30000.0 else CTS_CURVE[0][0]
    if ohms <= CTS_CURVE[-1][1]:
        return CTS_CURVE[-1][0]
    for (t0, r0), (t1, r1) in zip(CTS_CURVE, CTS_CURVE[1:]):
        if r1 <= ohms <= r0:
            frac = (r0 - ohms) / (r0 - r1)
            return round(t0 + frac * (t1 - t0), 1)
    return 20.0


@dataclass
class CarInputs:
    key_pos: str = "OFF"          # OFF, ACC, ON, START
    rpm: int = 0
    throttle_deg: float = 0.0     # 0 = closed, >34 = WOT
    coolant_temp_c: float = 20.0
    air_temp_c: float = 20.0
    oil_pressure_psi: float = 0.0
    baro_mmhg: float = 760.0      # <660 mmHg triggers California altitude switch (0.94)
    light_switch: str = "OFF"     # OFF, PARK, HEAD
    dimmer: str = "LOW"           # LOW, HIGH
    wiper_switch: str = "OFF"     # OFF, INT, LOW, HIGH
    battery_v: float = 12.6
    # Latch memory for 1978 Fuel Pump Control Relay / fuel cut hysteresis
    prev_fuel_cut: bool = False
    prev_fp_latched: bool = False
    prev_fp_lockout_tripped: bool = False


@dataclass
class ConnectorFault:
    """Fault injected at a specific harness connector pin or splice."""

    ohms: float = 0.0        # Added corrosion / loose-crimp resistance (Ω)
    open: bool = False       # Broken wire / backed-out pin / unplugged connector
    short_gnd: bool = False  # Pin or splice shorted to chassis ground

    def __post_init__(self) -> None:
        if self.ohms < 0.0:
            raise ValueError(f"ConnectorFault.ohms must be >= 0, got {self.ohms}")
        if self.open and self.short_gnd:
            raise ValueError("ConnectorFault cannot be both open and short_gnd.")


# WireViz / 1978 Color Wiring Diagram connector & splice catalog (interconnects.yml & zone2_engine_efi.yml)
CONNECTOR_CATALOG: Dict[str, str] = {
    "SPLICE_W_MAIN": "Heavy W battery/alternator feed splice upstream of Fusible Link Block (Issue #1)",
    "C2_ALT_CHG:1": "C-2 Pin 1 (WR/W) Alternator BAT charging output",
    "C2_ALT_CHG:3": "C-2 Pin 3 (WB) Alternator L terminal / FP interlock lead",
    "C9_C10_IGN_PWR:1": "C-9/C-10 Pin 1 (W) BAT feed to Ignition Switch",
    "C9_C10_IGN_PWR:2": "C-9/C-10 Pin 2 (L) ACC bus from Ignition Switch",
    "C9_C10_IGN_PWR:3": "C-9/C-10 Pin 3 (BW) IGN ON bus from Ignition Switch",
    "C9_C10_IGN_PWR:4": "C-9/C-10 Pin 4 (BY) STA crank bus from Ignition Switch",
    "C1_ENG_DASH:1": "C-1 Pin 1 (BW) IGN ON bus bulkhead feed to engine bay",
    "C1_ENG_DASH:2": "C-1 Pin 2 (BY) Starter S / crank feed bulkhead to engine bay",
    "C1_ENG_DASH:5": "C-1 Pin 5 (L) Coil (-) / Tachometer signal",
    "C1_ENG_DASH:7": "C-1 Pin 7 (L) Wiper / Washer feed bulkhead to engine bay",
    "C3_FWD_LIGHT:1": "C-3 Pin 1 (R) Headlamp feed",
    "C3_FWD_LIGHT:2": "C-3 Pin 2 (RW) RH Low beam / relay trigger",
    "C3_FWD_LIGHT:4": "C-3 Pin 4 (RL) LH Low beam / relay trigger",
    "C4_EFI_IGN:1": "C-4 Pin 1 (L/BW) EFI Main Power",
    "C4_EFI_IGN:2": "C-4 Pin 2 (GY/BW) Fuel Pump Relay output to cabin/rear",
    "C4_EFI_IGN:5": "C-4 Pin 5 (BY) Starter crank signal to ECU Pin 4",
    "C7_C8_REAR:6": "C-7/C-8 Pin 6 (BW/GY) Fuel Pump feed to rear tank pump",
    "EFI_MAIN_RELAY:1": "EFI Main Relay Pin 1 (BW) Coil (+)",
    "EFI_MAIN_RELAY:2": "EFI Main Relay Pin 2 (B) Coil ground",
    "EFI_MAIN_RELAY:3": "EFI Main Relay Pin 3 (72 R) Contact in from EFI 40A link",
    "EFI_MAIN_RELAY:4": "EFI Main Relay Pin 4 (71 L) Contact out to ECU / Dropping Resistors",
    "FP_CONTROL_RELAY:95": "FP Control Relay Pin 95 (BW) IGN ON feed (EF-36)",
    "FP_CONTROL_RELAY:94": "FP Control Relay Pin 94 (BY) Crank feed (EF-36)",
    "FP_CONTROL_RELAY:96": "FP Control Relay Pin 96 (WB/Oil Sw) Safety ground interlock (EF-36)",
    "FP_CONTROL_RELAY:91": "FP Control Relay Pin 91 Output to Fuel Pump Relay coil (EF-36)",
    "FP_RELAY:90": "Fuel Pump Relay Pin 90 Coil input (EF-36)",
    "FP_RELAY:92": "Fuel Pump Relay Pin 92 Contact input from EFI fusible link (EF-36)",
    "FP_RELAY:93": "Fuel Pump Relay Pin 93 (GY) Contact output to Fuel Pump + Air Reg (EF-36)",
    "FUEL_PUMP:GND": "Rear Fuel Pump chassis ground lug (B)",
    "DROPPING_RES_1:1": "Dropping Resistor Block 1 Common 12V (43/2 L) to Injectors 1-4",
    "DROPPING_RES_2:1": "Dropping Resistor Block 2 Common 12V (L) to Injectors 5-6",
    "WATER_TEMP_SENSOR": "Water Temperature Sensor (CTS) 2-pin connector at thermostat housing",
    "TPS:2": "Throttle Valve Switch Pin 2 (Idle contact)",
    "TPS:3": "Throttle Valve Switch Pin 3 (WOT contact)",
    "TPS:18": "Throttle Valve Switch Pin 18 (Common ground)",
    "ECU:1": "ECU Pin 1 Coil (-) RPM trigger input",
    "ECU:4": "ECU Pin 4 (BY) Crank enrichment input",
    "ECU:5": "ECU Pin 5 Intake manifold ground splice",
    "ECU:13": "ECU Pin 13 Water Temp Sensor input",
    "HEI_MODULE:GND": "GM 4-Pin HEI ignition module ground lug/wire",
}


def parse_connector_fault_arg(spec: str) -> tuple[str, ConnectorFault]:
    """Parse CLI string like 'C7_C8_REAR:6=open' or 'C9_C10_IGN_PWR:3=0.3ohm'."""
    if "=" not in spec:
        raise ValueError(f"Expected ID=FAULT (e.g. C7_C8_REAR:6=open), got '{spec}'")
    cid, val = spec.split("=", 1)
    cid = cid.strip()
    val_low = val.strip().lower()
    if cid not in CONNECTOR_CATALOG:
        raise ValueError(f"Unknown connector/splice ID '{cid}'. Use --list-connectors to see valid IDs.")
    if val_low == "open":
        return cid, ConnectorFault(open=True)
    if val_low in ("short", "short_gnd", "gnd"):
        return cid, ConnectorFault(short_gnd=True)
    num_str = val_low.removesuffix("ohms").removesuffix("ohm").removesuffix("ω").strip()
    try:
        return cid, ConnectorFault(ohms=float(num_str))
    except ValueError as exc:
        raise ValueError(f"Invalid connector fault value '{val}' for '{cid}' (use open, short_gnd, or <N>ohm)") from exc


@dataclass
class Faults:
    w_splice_ohms: float = 0.0
    ign_switch_drop_ohms: float = 0.0
    blown_fusible_links: Set[str] = field(default_factory=set)  # EFI_40A, MAIN_BR_30A, MAIN_BK_40A
    blown_fuses: Set[int] = field(default_factory=set)          # 1..12
    fp_control_relay_lockout_fault: bool = False  # Latches off on transient dip until key cycle
    fp_ctrl_94_open: bool = False                 # Open BY crank feed at FP Control Relay pin 94
    fp_relay_stuck_open: bool = False
    alt_l_open: bool = False                      # Open WB Alternator L wire (stops grounding pin 96)
    alt_l_short_gnd: bool = False                 # Shorted-to-ground WB Alternator L wire (trips safety)
    cts_open: bool = False
    cts_short: bool = False
    cts_offset_ohms: float = 0.0
    tps_idle_stuck_closed: bool = False
    tps_idle_open: bool = False
    hei_ground_ohms: float = 0.0
    headlamp_relayed: bool = True  # As-built mod
    connector_faults: Dict[str, ConnectorFault] = field(default_factory=dict)


@dataclass
class TwinState:
    nodes: Dict[str, float]
    ecu_mode: str
    ecu_enrichment_factor: float
    fuel_pump_on: bool
    spark_available: bool
    fuel_cut_active: bool
    fp_latched: bool
    fp_lockout_tripped: bool
    cts_ohms: float
    ecu_coolant_c: float
    tps_idle_closed: bool
    tps_wot_closed: bool
    wiper_mode: str
    notes: List[str]


def _apply_path_faults(
    v_in: float,
    load_a: float,
    conn_ids: List[str],
    cf_map: Dict[str, ConnectorFault],
    notes: List[str],
    noted: Set[str],
) -> float:
    """Propagate voltage through one or more series harness connectors/splices."""
    v = v_in
    for cid in conn_ids:
        cf = cf_map.get(cid)
        if cf is None:
            continue
        if cf.open:
            if v > 0.0 and cid not in noted:
                notes.append(f"Connector/splice open: {cid} ({CONNECTOR_CATALOG[cid]}).")
                noted.add(cid)
            return 0.0
        if cf.short_gnd:
            if v > 0.0 and cid not in noted:
                notes.append(f"Connector/splice shorted to ground: {cid} ({CONNECTOR_CATALOG[cid]}).")
                noted.add(cid)
            return 0.0
        if cf.ohms > 0.0 and v > 0.0:
            drop = cf.ohms * load_a
            v = max(0.0, v - drop)
            if drop >= 0.5 and cid not in noted:
                notes.append(
                    f"High resistance at {cid} ({cf.ohms:.2f} Ω, -{drop:.2f} V at {load_a:.1f} A)."
                )
                noted.add(cid)
    return v


def simulate(inp: CarInputs, faults: Optional[Faults] = None) -> TwinState:
    """Evaluate 1978 280Z electrical nodes, relay states, and Bosch L-Jetronic ECU mode."""
    f = faults or Faults()
    if f.cts_open and f.cts_short:
        raise ValueError("Conflicting faults: cts_open and cts_short cannot both be True.")
    if f.tps_idle_stuck_closed and f.tps_idle_open:
        raise ValueError("Conflicting faults: tps_idle_stuck_closed and tps_idle_open cannot both be True.")
    if f.alt_l_open and f.alt_l_short_gnd:
        raise ValueError("Conflicting faults: alt_l_open and alt_l_short_gnd cannot both be True.")
    for cid in f.connector_faults:
        if cid not in CONNECTOR_CATALOG:
            raise ValueError(f"Unknown connector/splice ID '{cid}'.")

    cf_map = f.connector_faults
    notes: List[str] = []
    noted: Set[str] = set()
    nodes: Dict[str, float] = {}

    def cf_get(cid: str) -> ConnectorFault:
        return cf_map.get(cid, ConnectorFault())

    # 1. Battery & Charging Bus (C-2 Alternator BAT & L leads)
    alt_l_cf = cf_get("C2_ALT_CHG:3")
    alt_bat_cf = cf_get("C2_ALT_CHG:1")
    eff_alt_l_open = f.alt_l_open or alt_l_cf.open
    eff_alt_l_short = f.alt_l_short_gnd or alt_l_cf.short_gnd
    if eff_alt_l_open and eff_alt_l_short:
        raise ValueError("Conflicting faults on Alternator L circuit (open and short_gnd).")

    alt_bat_ok = (not alt_bat_cf.open) and (not alt_bat_cf.short_gnd)
    alt_active = inp.rpm >= 450 and (not eff_alt_l_open) and (not eff_alt_l_short) and alt_bat_ok
    if alt_active:
        alt_out_v = max(inp.battery_v, 14.1 - alt_bat_cf.ohms * 15.0)
        if alt_bat_cf.ohms * 15.0 >= 0.5 and "C2_ALT_CHG:1" not in noted:
            notes.append(f"High resistance at C2_ALT_CHG:1 ({alt_bat_cf.ohms:.2f} Ω) reducing charging voltage.")
            noted.add("C2_ALT_CHG:1")
        sys_v = alt_out_v
    else:
        if (alt_bat_cf.open or alt_bat_cf.short_gnd) and inp.rpm >= 450 and "C2_ALT_CHG:1" not in noted:
            notes.append("C2_ALT_CHG:1 (Alt BAT) faulted: alternator cannot charge battery bus.")
            noted.add("C2_ALT_CHG:1")
        sys_v = inp.battery_v
    nodes["BATTERY_+"] = round(sys_v, 2)
    nodes["STARTER_B"] = round(sys_v, 2)

    # 2. Upstream Heavy W Feed Splice & Fusible Links (BE-6 / HANDOFF Issue #1)
    ign_load_a = 10.0 if inp.key_pos in ("ON", "START") else (2.0 if inp.key_pos == "ACC" else 0.0)
    light_load_a = 0.0
    if inp.light_switch in ("PARK", "HEAD"):
        light_load_a += 3.0
    if inp.light_switch == "HEAD":
        # Relayed headlamps draw main load directly from Battery (+), only ~0.3A trigger via Fuse 4/5.
        light_load_a += 0.3 if f.headlamp_relayed else 10.0
    wiper_load_a = 4.0 if (inp.key_pos in ("ACC", "ON") and inp.wiper_switch != "OFF" and 7 not in f.blown_fuses) else 0.0
    bus_load_a = ign_load_a + light_load_a + wiper_load_a

    fusible_in_v = max(0.0, sys_v - f.w_splice_ohms * bus_load_a)
    fusible_in_v = _apply_path_faults(fusible_in_v, bus_load_a, ["SPLICE_W_MAIN"], cf_map, notes, noted)
    efi_link_v = 0.0 if "EFI_40A" in f.blown_fusible_links else fusible_in_v
    main_br_v = 0.0 if "MAIN_BR_30A" in f.blown_fusible_links else fusible_in_v
    main_bk_v = 0.0 if "MAIN_BK_40A" in f.blown_fusible_links else fusible_in_v

    nodes["FUSIBLE_EFI_72R"] = round(efi_link_v, 2)
    nodes["FUSIBLE_MAIN_BR_W"] = round(main_br_v, 2)
    nodes["FUSIBLE_MAIN_BK_WR"] = round(main_bk_v, 2)

    # 3. Ignition Switch via C-9/C-10 (B=W, ACC=L, ON=BW, STA=BY)
    sw_load_a = ign_load_a + wiper_load_a
    ign_sw_in_v = _apply_path_faults(main_br_v, sw_load_a, ["C9_C10_IGN_PWR:1"], cf_map, notes, noted)
    sw_drop = f.ign_switch_drop_ohms * sw_load_a if ign_sw_in_v > 0 else 0.0
    ign_sw_out_v = max(0.0, ign_sw_in_v - sw_drop)
    acc_v = _apply_path_faults(
        ign_sw_out_v if inp.key_pos in ("ACC", "ON") else 0.0,
        wiper_load_a + 1.0,
        ["C9_C10_IGN_PWR:2"],
        cf_map,
        notes,
        noted,
    )
    on_v = _apply_path_faults(
        ign_sw_out_v if inp.key_pos in ("ON", "START") else 0.0,
        ign_load_a,
        ["C9_C10_IGN_PWR:3"],
        cf_map,
        notes,
        noted,
    )
    sta_v = _apply_path_faults(
        ign_sw_out_v if inp.key_pos == "START" else 0.0,
        4.0,
        ["C9_C10_IGN_PWR:4"],
        cf_map,
        notes,
        noted,
    )

    nodes["IGN_SW_ACC_L"] = round(acc_v, 2)
    nodes["IGN_SW_ON_BW"] = round(on_v, 2)
    nodes["IGN_SW_STA_BY"] = round(sta_v, 2)

    # 4. Ignition Relay & Fuse Box (Zone 4: Fuses 4=RH Headlamp, 5=LH Headlamp)
    ign_relay_v = on_v if on_v >= 8.5 else 0.0
    nodes["IGN_RELAY_OUT_BW"] = round(ign_relay_v, 2)

    # Bulkhead C-1 feeds engine bay IGN ON (BW) and Starter/Crank (BY)
    eng_bw_v = _apply_path_faults(ign_relay_v, 6.0, ["C1_ENG_DASH:1"], cf_map, notes, noted)
    eng_by_v = _apply_path_faults(sta_v, 4.0, ["C1_ENG_DASH:2"], cf_map, notes, noted)

    fuse1_v = main_br_v if 1 not in f.blown_fuses else 0.0
    fuse3_v = main_bk_v if (3 not in f.blown_fuses and inp.light_switch in ("PARK", "HEAD")) else 0.0
    fuse4_v = main_bk_v if (4 not in f.blown_fuses and inp.light_switch == "HEAD") else 0.0
    fuse5_v = main_bk_v if (5 not in f.blown_fuses and inp.light_switch == "HEAD") else 0.0
    fuse7_v = acc_v if 7 not in f.blown_fuses else 0.0
    fuse10_v = ign_relay_v if 10 not in f.blown_fuses else 0.0

    nodes["FUSE_1_BAT_GW"] = round(fuse1_v, 2)
    nodes["FUSE_3_LIGHT_RL"] = round(fuse3_v, 2)
    nodes["FUSE_4_HEAD_RH_R"] = round(fuse4_v, 2)
    nodes["FUSE_5_HEAD_LH_R"] = round(fuse5_v, 2)
    nodes["FUSE_7_WIPER_L"] = round(fuse7_v, 2)
    nodes["FUSE_10_GAUGES_BW"] = round(fuse10_v, 2)

    # 5. Headlight Relay Upgrade (As-Built) vs Factory (via C-3 Forward Lighting)
    c3_rh_feed = _apply_path_faults(fuse4_v, 0.3 if f.headlamp_relayed else 5.0, ["C3_FWD_LIGHT:1", "C3_FWD_LIGHT:2"], cf_map, notes, noted)
    c3_lh_feed = _apply_path_faults(fuse5_v, 0.3 if f.headlamp_relayed else 5.0, ["C3_FWD_LIGHT:1", "C3_FWD_LIGHT:4"], cf_map, notes, noted)
    if f.headlamp_relayed:
        head_trig = (c3_rh_feed >= 9.0) or (c3_lh_feed >= 9.0)
        rh_low = sys_v if (head_trig and inp.dimmer == "LOW") else 0.0
        lh_low = sys_v if (head_trig and inp.dimmer == "LOW") else 0.0
        rh_high = sys_v if (head_trig and inp.dimmer == "HIGH") else 0.0
        lh_high = sys_v if (head_trig and inp.dimmer == "HIGH") else 0.0
    else:
        rh_v = max(0.0, c3_rh_feed - 1.4) if c3_rh_feed > 0 else 0.0
        lh_v = max(0.0, c3_lh_feed - 1.4) if c3_lh_feed > 0 else 0.0
        rh_low = rh_v if inp.dimmer == "LOW" else 0.0
        lh_low = lh_v if inp.dimmer == "LOW" else 0.0
        rh_high = rh_v if inp.dimmer == "HIGH" else 0.0
        lh_high = lh_v if inp.dimmer == "HIGH" else 0.0

    nodes["HEADLAMP_RH_LOW"] = round(rh_low, 2)
    nodes["HEADLAMP_LH_LOW"] = round(lh_low, 2)
    nodes["HEADLAMP_RH_HIGH"] = round(rh_high, 2)
    nodes["HEADLAMP_LH_HIGH"] = round(lh_high, 2)

    # 5b. Civic Wiper Motor Conversion (As-Built, powered by Fuse 7 ACC L via C-1 Pin 7)
    wiper_v = _apply_path_faults(fuse7_v, 4.0, ["C1_ENG_DASH:7"], cf_map, notes, noted)
    wiper_powered = wiper_v >= 9.0 and inp.wiper_switch != "OFF"
    wiper_mode = inp.wiper_switch if wiper_powered else "OFF"
    nodes["WIPER_MOTOR_RUN"] = round(wiper_v if wiper_powered else 0.0, 2)

    # 6. Ignition Coil & GM 4-Pin HEI Module (EF-4)
    hei_gnd_cf = cf_get("HEI_MODULE:GND")
    c1_tach_cf = cf_get("C1_ENG_DASH:5")
    ecu1_cf = cf_get("ECU:1")
    coil_plus_v = eng_bw_v
    if hei_gnd_cf.open:
        hei_gnd_drop_v = coil_plus_v
        if coil_plus_v > 0.0 and "HEI_MODULE:GND" not in noted:
            notes.append("Connector/splice open: HEI_MODULE:GND (no ignition spark).")
            noted.add("HEI_MODULE:GND")
    else:
        eff_hei_ohms = f.hei_ground_ohms + hei_gnd_cf.ohms
        hei_gnd_drop_v = min(coil_plus_v, eff_hei_ohms * 4.0) if coil_plus_v > 0 else 0.0
    coil_eff_v = max(0.0, coil_plus_v - hei_gnd_drop_v)
    # Shorted tach lead on C-1 pin 5 shorts Coil (-) to ground and kills spark
    tach_shorted = c1_tach_cf.short_gnd or ecu1_cf.short_gnd
    if tach_shorted and coil_plus_v > 0.0 and "COIL_MINUS_SHORT" not in noted:
        notes.append("Coil (-) / tach trigger shorted to ground: ignition primary cannot collapse (no spark).")
        noted.add("COIL_MINUS_SHORT")
    spark_ok = coil_eff_v >= 9.0 and inp.rpm > 0 and (not tach_shorted)
    nodes["COIL_+_BW"] = round(coil_plus_v, 2)
    nodes["HEI_GND_DROP"] = round(hei_gnd_drop_v, 2)

    # ECU Pin 1 taps Coil (-) via 2.2 kΩ resistor
    if coil_plus_v <= 0.0 or tach_shorted or ecu1_cf.open:
        coil_minus_v = 0.0
        if ecu1_cf.open and coil_plus_v > 0.0 and "ECU:1" not in noted:
            notes.append("Connector/splice open: ECU:1 (ECU receives no RPM trigger pulse).")
            noted.add("ECU:1")
    elif inp.rpm > 0 and spark_ok:
        coil_minus_v = round(hei_gnd_drop_v + 0.7 * coil_eff_v, 2)
    else:
        coil_minus_v = round(coil_plus_v, 2)
    nodes["ECU_PIN_1_RPM"] = coil_minus_v
    ecu_rpm_trigger_ok = spark_ok and (not ecu1_cf.open)

    # 7. EFI Main Relay (Coil: BW via pin 1/2, Contact In: 72R pin 3 -> Out: 71L pin 4 / C-4 pin 1)
    efi_coil_v = _apply_path_faults(eng_bw_v, 0.2, ["EFI_MAIN_RELAY:1", "EFI_MAIN_RELAY:2"], cf_map, notes, noted)
    efi_contact_in_v = _apply_path_faults(efi_link_v, 6.0, ["EFI_MAIN_RELAY:3"], cf_map, notes, noted)
    efi_main_on = efi_coil_v >= 8.5 and efi_contact_in_v >= 9.0
    efi_raw_out_v = efi_contact_in_v if efi_main_on else 0.0
    efi_pwr_v = _apply_path_faults(efi_raw_out_v, 6.0, ["EFI_MAIN_RELAY:4", "C4_EFI_IGN:1"], cf_map, notes, noted)
    # ECU Pin 5 intake manifold ground splice
    ecu_gnd_v = _apply_path_faults(efi_pwr_v, 4.0, ["ECU:5"], cf_map, notes, noted)
    efi_ecu_ok = ecu_gnd_v >= 8.5

    drop_res1_v = _apply_path_faults(efi_pwr_v, 3.0, ["DROPPING_RES_1:1"], cf_map, notes, noted)
    drop_res2_v = _apply_path_faults(efi_pwr_v, 1.5, ["DROPPING_RES_2:1"], cf_map, notes, noted)
    nodes["EFI_MAIN_OUT_71L"] = round(efi_pwr_v, 2)
    nodes["DROPPING_RES_43_2"] = round(drop_res1_v, 2)
    nodes["DROPPING_RES_2_L"] = round(drop_res2_v, 2)

    # 8. 1978 Dual-Relay Fuel Pump Safety Circuit (EF-35 / EF-36, Figs. EF-63 & EF-64)
    fp_ctrl_95 = _apply_path_faults(eng_bw_v, 0.3, ["FP_CONTROL_RELAY:95"], cf_map, notes, noted)
    fp94_cf = cf_get("FP_CONTROL_RELAY:94")
    fp96_cf = cf_get("FP_CONTROL_RELAY:96")
    fp_ctrl_94 = 0.0 if f.fp_ctrl_94_open else _apply_path_faults(eng_by_v, 0.3, ["FP_CONTROL_RELAY:94"], cf_map, notes, noted)
    eff_pin94_open = f.fp_ctrl_94_open or fp94_cf.open

    if eff_alt_l_open:
        alt_l_v = 0.0
        alt_l_grounding = False
        notes.append("Alternator L wire open: alternator will not self-excite; pin 96 not grounded by Alt L.")
    elif eff_alt_l_short:
        alt_l_v = 0.0
        alt_l_grounding = True
        notes.append("Alternator L wire shorted to ground: pin 96 held grounded (safety shutoff active).")
    else:
        alt_l_v = 13.8 if alt_active else (1.5 if eng_bw_v > 0 else 0.0)
        alt_l_grounding = not alt_active

    oil_sw_grounding = inp.oil_pressure_psi < 5.0
    if fp96_cf.open:
        t96_grounded = False
        if "FP_CONTROL_RELAY:96" not in noted:
            notes.append("Connector/splice open: FP_CONTROL_RELAY:96 (safety ground interlock bypassed).")
            noted.add("FP_CONTROL_RELAY:96")
    elif fp96_cf.short_gnd:
        t96_grounded = True
        if "FP_CONTROL_RELAY:96" not in noted:
            notes.append("Connector/splice shorted to ground: FP_CONTROL_RELAY:96 (safety shutoff active).")
            noted.add("FP_CONTROL_RELAY:96")
    else:
        t96_grounded = alt_l_grounding or oil_sw_grounding
    run_interlock_ok = not t96_grounded

    if fp_ctrl_95 < 8.0:
        lockout_tripped = False
        fp_latched = False
    else:
        lockout_tripped = inp.prev_fp_lockout_tripped
        if f.fp_control_relay_lockout_fault and inp.prev_fp_latched and t96_grounded and fp_ctrl_94 < 8.0:
            lockout_tripped = True
            notes.append("FP Control Relay safety lockout tripped; requires BW power drop (key cycle) to reset.")
        if eff_pin94_open and eng_by_v >= 8.5 and t96_grounded:
            notes.append("FP Control Relay pin 94 (BY) open: cranking cannot release 95-96 safety coil.")
        fp_latched = (not lockout_tripped) and ((fp_ctrl_94 >= 8.5) or run_interlock_ok)

    # Pin 91 -> Fuel Pump Relay Coil Pin 90; Contact In Pin 92 -> Contact Out Pin 93
    fp_coil_drive_v = _apply_path_faults(
        fp_ctrl_95 if fp_latched else 0.0,
        0.2,
        ["FP_CONTROL_RELAY:91", "FP_RELAY:90"],
        cf_map,
        notes,
        noted,
    )
    fp_contact_in_v = _apply_path_faults(efi_link_v, 5.5, ["FP_RELAY:92"], cf_map, notes, noted)
    fp_relay_on = (fp_coil_drive_v >= 8.5) and (not f.fp_relay_stuck_open) and (fp_contact_in_v >= 8.5)
    fp_relay_out_v = _apply_path_faults(
        fp_contact_in_v if fp_relay_on else 0.0,
        5.5,
        ["FP_RELAY:93"],
        cf_map,
        notes,
        noted,
    )
    # Air Regulator taps engine-bay side of FP Relay out; rear fuel pump passes through C-4, C-7/C-8, and rear ground
    air_reg_v = fp_relay_out_v
    fuel_pump_v = _apply_path_faults(
        fp_relay_out_v,
        5.0,
        ["C4_EFI_IGN:2", "C7_C8_REAR:6", "FUEL_PUMP:GND"],
        cf_map,
        notes,
        noted,
    )
    fuel_pump_ok = fuel_pump_v >= 9.0

    nodes["FP_CTRL_95_BW"] = round(fp_ctrl_95, 2)
    nodes["FP_CTRL_94_BY"] = round(fp_ctrl_94, 2)
    nodes["ALT_L_WB"] = round(alt_l_v, 2)
    nodes["FP_RELAY_OUT_GY"] = round(fp_relay_out_v, 2)
    nodes["FUEL_PUMP_+"] = round(fuel_pump_v, 2)
    nodes["AIR_REGULATOR_34"] = round(air_reg_v, 2)

    # 9. ECU Sensors (CTS Pin 13, TPS Pins 2/18/3, Start Pin 4, Altitude)
    cts_cf = cf_get("WATER_TEMP_SENSOR")
    ecu13_cf = cf_get("ECU:13")
    eff_cts_open = f.cts_open or cts_cf.open or ecu13_cf.open
    eff_cts_short = f.cts_short or cts_cf.short_gnd or ecu13_cf.short_gnd
    if eff_cts_open and eff_cts_short:
        raise ValueError("Conflicting faults on CTS circuit (open and short_gnd).")
    eff_cts_offset = f.cts_offset_ohms + cts_cf.ohms + ecu13_cf.ohms

    if eff_cts_open:
        cts_r = 999999.0
        ecu_coolant_c = -40.0
    elif eff_cts_short:
        cts_r = 0.0
        ecu_coolant_c = 120.0
    else:
        base_cts_r = cts_resistance_ohms(inp.coolant_temp_c)
        cts_r = round(max(0.0, base_cts_r + eff_cts_offset), 1)
        ecu_coolant_c = cts_temp_c_from_ohms(cts_r) if eff_cts_offset != 0.0 else inp.coolant_temp_c
        if abs(ecu_coolant_c - inp.coolant_temp_c) >= 5.0:
            notes.append(
                f"CTS resistance offset ({eff_cts_offset:+.0f} Ω) shifts ECU perceived coolant temp "
                f"from {inp.coolant_temp_c:.1f}°C to {ecu_coolant_c:.1f}°C."
            )

    tps18_cf = cf_get("TPS:18")
    tps2_cf = cf_get("TPS:2")
    tps3_cf = cf_get("TPS:3")
    if tps18_cf.open:
        tps_idle = False
        raw_wot = False
        if "TPS:18" not in noted:
            notes.append("Connector/splice open: TPS:18 (common ground open; idle and WOT contacts both dead).")
            noted.add("TPS:18")
    else:
        if f.tps_idle_stuck_closed or tps2_cf.short_gnd:
            tps_idle = True
        elif f.tps_idle_open or tps2_cf.open:
            tps_idle = False
        else:
            tps_idle = inp.throttle_deg <= 1.0
        if tps3_cf.short_gnd:
            raw_wot = True
        elif tps3_cf.open:
            raw_wot = False
        else:
            raw_wot = inp.throttle_deg > 34.0

    tps_wot = raw_wot and (not tps_idle)
    if raw_wot and (f.tps_idle_stuck_closed or tps2_cf.short_gnd):
        notes.append("TPS idle contact stuck closed: masks WOT signal and risks decel fuel cut above 3200 rpm.")

    ecu_pin4_v = _apply_path_faults(eng_by_v, 0.1, ["C4_EFI_IGN:5", "ECU:4"], cf_map, notes, noted)
    nodes["ECU_PIN_4_START"] = round(ecu_pin4_v, 2)
    nodes["ECU_PIN_2_TPS_IDLE"] = 0.0 if tps_idle else 5.0
    nodes["ECU_PIN_3_TPS_WOT"] = 0.0 if tps_wot else 5.0

    # 10. Bosch L-Jetronic Operating Mode (EF-10 to EF-12)
    if inp.prev_fuel_cut:
        fuel_cut = tps_idle and (inp.rpm >= 2800)
    else:
        fuel_cut = tps_idle and (inp.rpm >= 3200)

    enrichment = 1.0
    if inp.baro_mmhg <= 660.0:
        enrichment *= 0.94  # California altitude compensation (-6%)

    if not efi_ecu_ok:
        mode = "OFF"
        enrichment = 0.0
    elif inp.rpm == 0 and inp.key_pos == "ON":
        mode = "KEY_ON_READY"
        enrichment = 0.0
    elif inp.rpm == 0 and inp.key_pos == "START":
        mode = "CRANK_NO_RPM"
        enrichment = 0.0
    elif not fuel_pump_ok:
        mode = "STALLED_NO_FUEL"
        enrichment = 0.0
    elif not spark_ok:
        mode = "STALLED_NO_SPARK"
        enrichment = 0.0
    elif not ecu_rpm_trigger_ok:
        mode = "STALLED_NO_RPM_SIG"
        enrichment = 0.0
    elif drop_res1_v < 8.5 and drop_res2_v < 8.5:
        mode = "STALLED_NO_INJ_PWR"
        enrichment = 0.0
    elif ecu_pin4_v >= 8.5:
        mode = "CRANK_COLD_START" if ecu_coolant_c < 15.0 else "CRANK_START"
        enrichment *= 1.8 if eff_cts_open else (1.5 if ecu_coolant_c < 20.0 else (1.2 if ecu_coolant_c < 70.0 else 1.0))
    elif fuel_cut:
        mode = "DECEL_FUEL_CUT"
        enrichment = 0.0
    elif tps_wot:
        mode = "FULL_LOAD_WOT"
        enrichment *= 1.27  # +27% full-load enrichment (EF-11)
        if ecu_coolant_c < 70.0:
            enrichment *= 1.15
    elif tps_idle:
        mode = "AFTER_START_WARMUP" if ecu_coolant_c < 70.0 else "IDLE"
        if ecu_coolant_c < 70.0:
            enrichment *= 1.15
    else:
        mode = "PART_THROTTLE_CRUISE"
        if ecu_coolant_c < 70.0:
            enrichment *= 1.15  # Warmup enrichment active until ~70°C (158°F) (EF-10/EF-11)
        if inp.air_temp_c < 20.0:
            enrichment *= 1.05

    if eff_cts_open:
        notes.append("CTS open circuit: ECU sees extreme cold (-40°C), causing severe over-fueling/flooding.")
    elif eff_cts_short:
        notes.append("CTS shorted: ECU sees hot engine, starving cold starts.")
    if (drop_res1_v < 8.5) ^ (drop_res2_v < 8.5):
        dead_cyls = "1-4" if drop_res1_v < 8.5 else "5-6"
        notes.append(f"Dropping resistor feed open: injectors {dead_cyls} unpowered (severe misfire).")

    return TwinState(
        nodes=nodes,
        ecu_mode=mode,
        ecu_enrichment_factor=round(enrichment, 2),
        fuel_pump_on=fuel_pump_ok,
        spark_available=spark_ok,
        fuel_cut_active=fuel_cut,
        fp_latched=fp_latched,
        fp_lockout_tripped=lockout_tripped,
        cts_ohms=cts_r,
        ecu_coolant_c=round(ecu_coolant_c, 1),
        tps_idle_closed=tps_idle,
        tps_wot_closed=tps_wot,
        wiper_mode=wiper_mode,
        notes=notes,
    )


def run_sequence(steps: List[tuple[str, CarInputs]], faults: Faults) -> List[TwinState]:
    prev_cut = False
    prev_latch = False
    prev_lockout = False
    results: List[TwinState] = []
    for label, inp in steps:
        step_inp = replace(
            inp,
            prev_fuel_cut=prev_cut,
            prev_fp_latched=prev_latch,
            prev_fp_lockout_tripped=prev_lockout,
        )
        st = simulate(step_inp, faults)
        results.append(st)
        prev_cut = st.fuel_cut_active
        prev_latch = st.fp_latched
        prev_lockout = st.fp_lockout_tripped
        print(
            f"[{label:22s}] Mode={st.ecu_mode:20s} | FP={'ON ' if st.fuel_pump_on else 'OFF'} "
            f"| Spark={'OK' if st.spark_available else 'NO'} "
            f"| BW(95)={st.nodes['FP_CTRL_95_BW']:5.2f}V "
            f"| BY(94)={st.nodes['FP_CTRL_94_BY']:5.2f}V "
            f"| AltL={st.nodes['ALT_L_WB']:5.2f}V "
            f"| FuelPump={st.nodes['FUEL_PUMP_+']:5.2f}V"
        )
        for n in st.notes:
            print(f"    - {n}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="1978 Datsun 280Z Diagnostic Digital Twin")
    parser.add_argument("--scenario", choices=["issue5", "issue2"], help="Run a diagnostic scenario")
    parser.add_argument("--list-connectors", action="store_true", help="List supported WireViz/FSM connector and splice IDs")
    parser.add_argument(
        "--conn-fault",
        action="append",
        default=[],
        metavar="ID=FAULT",
        help="Inject connector/splice fault (e.g. C7_C8_REAR:6=open, C9_C10_IGN_PWR:3=0.3ohm, C2_ALT_CHG:3=short_gnd)",
    )
    parser.add_argument("--key", default="ON", choices=["OFF", "ACC", "ON", "START"])
    parser.add_argument("--rpm", type=int, default=800)
    parser.add_argument("--throttle", type=float, default=0.0)
    parser.add_argument("--coolant", type=float, default=75.0)
    parser.add_argument("--oil", type=float, default=35.0)
    parser.add_argument("--w-splice-ohms", type=float, default=0.0)
    args = parser.parse_args()

    if args.list_connectors:
        print("=== Supported 1978 280Z Harness Connectors & Splices (WireViz / FSM) ===")
        for cid, desc in CONNECTOR_CATALOG.items():
            print(f"  {cid:22s} : {desc}")
        return

    conn_faults: Dict[str, ConnectorFault] = {}
    for spec in args.conn_fault:
        cid, cf = parse_connector_fault_arg(spec)
        conn_faults[cid] = cf

    if args.scenario == "issue5":
        print("=== Issue #5: Coasting Decel Stall & Key-Cycle Reset (1978 FP Control Relay Lockout Fault) ===")
        steps = [
            ("1. Crank (START)", CarInputs(key_pos="START", rpm=220, coolant_temp_c=80, oil_pressure_psi=10)),
            ("2. Cruise 3500rpm", CarInputs(key_pos="ON", rpm=3500, throttle_deg=18, coolant_temp_c=80, oil_pressure_psi=45)),
            ("3. Decel >3200rpm", CarInputs(key_pos="ON", rpm=3400, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=40)),
            ("4. Coast <2800 (dip)", CarInputs(key_pos="ON", rpm=400, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=4)),
            ("5. Re-crank w/o OFF", CarInputs(key_pos="START", rpm=220, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=10)),
            ("6. Key OFF reset", CarInputs(key_pos="OFF", rpm=0, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=0)),
            ("7. Re-crank after OFF", CarInputs(key_pos="START", rpm=220, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=10)),
        ]
        run_sequence(steps, Faults(fp_control_relay_lockout_fault=True, connector_faults=conn_faults))
        return

    if args.scenario == "issue2":
        print("=== Issue #2: Cold-Only Start & Stall (Healthy vs. Corroded/Open/Shorted CTS) ===")
        for name, flt in [
            ("Healthy (10°C)", Faults(connector_faults=conn_faults)),
            ("CTS +5kΩ Offset", Faults(cts_offset_ohms=5000.0, connector_faults=conn_faults)),
            ("CTS Shorted", Faults(cts_short=True, connector_faults=conn_faults)),
            ("CTS Open", Faults(cts_open=True, connector_faults=conn_faults)),
        ]:
            print(f"\n-- {name} --")
            st = simulate(CarInputs(key_pos="START", rpm=200, coolant_temp_c=10.0, air_temp_c=10.0, oil_pressure_psi=10), flt)
            print(
                f"  Mode={st.ecu_mode} | CTS={st.cts_ohms} Ω (ECU sees {st.ecu_coolant_c}°C) "
                f"| Enrichment={st.ecu_enrichment_factor}x | Pin4={st.nodes['ECU_PIN_4_START']}V"
            )
            for n in st.notes:
                print(f"  - {n}")
        return

    st = simulate(
        CarInputs(key_pos=args.key, rpm=args.rpm, throttle_deg=args.throttle, coolant_temp_c=args.coolant, oil_pressure_psi=args.oil),
        Faults(w_splice_ohms=args.w_splice_ohms, connector_faults=conn_faults),
    )
    print(f"ECU Mode: {st.ecu_mode} (Enrichment: {st.ecu_enrichment_factor}x) | Fuel Pump: {'ON' if st.fuel_pump_on else 'OFF'} | Spark: {'OK' if st.spark_available else 'NO'}")
    print(f"CTS: {st.cts_ohms} Ω (ECU: {st.ecu_coolant_c}°C) | TPS Idle: {st.tps_idle_closed} | TPS WOT: {st.tps_wot_closed} | Wiper: {st.wiper_mode}")
    for n in st.notes:
        print(f"  - {n}")
    for k, v in st.nodes.items():
        print(f"  {k:22s}: {v:5.2f} V")


if __name__ == "__main__":
    main()
