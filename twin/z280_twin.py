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


def simulate(inp: CarInputs, faults: Optional[Faults] = None) -> TwinState:
    """Evaluate 1978 280Z electrical nodes, relay states, and Bosch L-Jetronic ECU mode."""
    f = faults or Faults()
    if f.cts_open and f.cts_short:
        raise ValueError("Conflicting faults: cts_open and cts_short cannot both be True.")
    if f.tps_idle_stuck_closed and f.tps_idle_open:
        raise ValueError("Conflicting faults: tps_idle_stuck_closed and tps_idle_open cannot both be True.")
    if f.alt_l_open and f.alt_l_short_gnd:
        raise ValueError("Conflicting faults: alt_l_open and alt_l_short_gnd cannot both be True.")

    notes: List[str] = []
    nodes: Dict[str, float] = {}

    # 1. Battery & Charging Bus
    # Open L wire prevents field excitation at startup; shorted L wire also prevents normal charging.
    alt_active = inp.rpm >= 450 and (not f.alt_l_open) and (not f.alt_l_short_gnd)
    sys_v = 14.1 if alt_active else inp.battery_v
    nodes["BATTERY_+"] = round(sys_v, 2)
    nodes["STARTER_B"] = round(sys_v, 2)

    # 2. Upstream Heavy W Feed Splice & Fusible Links (BE-6 / HANDOFF Issue #1)
    # Estimate dynamic current drawn through the fusible link block.
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
    efi_link_v = 0.0 if "EFI_40A" in f.blown_fusible_links else fusible_in_v
    main_br_v = 0.0 if "MAIN_BR_30A" in f.blown_fusible_links else fusible_in_v
    main_bk_v = 0.0 if "MAIN_BK_40A" in f.blown_fusible_links else fusible_in_v

    nodes["FUSIBLE_EFI_72R"] = round(efi_link_v, 2)
    nodes["FUSIBLE_MAIN_BR_W"] = round(main_br_v, 2)
    nodes["FUSIBLE_MAIN_BK_WR"] = round(main_bk_v, 2)

    # 3. Ignition Switch (B=W, ACC=L, ON=BW, STA=BY)
    sw_load_a = ign_load_a + wiper_load_a
    sw_drop = f.ign_switch_drop_ohms * sw_load_a if main_br_v > 0 else 0.0
    ign_sw_out_v = max(0.0, main_br_v - sw_drop)
    acc_v = ign_sw_out_v if inp.key_pos in ("ACC", "ON") else 0.0
    on_v = ign_sw_out_v if inp.key_pos in ("ON", "START") else 0.0
    sta_v = ign_sw_out_v if inp.key_pos == "START" else 0.0

    nodes["IGN_SW_ACC_L"] = round(acc_v, 2)
    nodes["IGN_SW_ON_BW"] = round(on_v, 2)
    nodes["IGN_SW_STA_BY"] = round(sta_v, 2)

    # 4. Ignition Relay & Fuse Box (Zone 4: Fuses 4=RH Headlamp, 5=LH Headlamp)
    ign_relay_v = on_v if on_v >= 8.5 else 0.0
    nodes["IGN_RELAY_OUT_BW"] = round(ign_relay_v, 2)

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

    # 5. Headlight Relay Upgrade (As-Built) vs Factory
    if f.headlamp_relayed:
        # As-built dual-relay harness taps factory C-3 trigger leads and feeds lamps directly from Battery (+).
        head_trig = (fuse4_v >= 9.0) or (fuse5_v >= 9.0)
        rh_low = sys_v if (head_trig and inp.dimmer == "LOW") else 0.0
        lh_low = sys_v if (head_trig and inp.dimmer == "LOW") else 0.0
        rh_high = sys_v if (head_trig and inp.dimmer == "HIGH") else 0.0
        lh_high = sys_v if (head_trig and inp.dimmer == "HIGH") else 0.0
    else:
        rh_v = max(0.0, fuse4_v - 1.4) if fuse4_v > 0 else 0.0
        lh_v = max(0.0, fuse5_v - 1.4) if fuse5_v > 0 else 0.0
        rh_low = rh_v if inp.dimmer == "LOW" else 0.0
        lh_low = lh_v if inp.dimmer == "LOW" else 0.0
        rh_high = rh_v if inp.dimmer == "HIGH" else 0.0
        lh_high = lh_v if inp.dimmer == "HIGH" else 0.0

    nodes["HEADLAMP_RH_LOW"] = round(rh_low, 2)
    nodes["HEADLAMP_LH_LOW"] = round(lh_low, 2)
    nodes["HEADLAMP_RH_HIGH"] = round(rh_high, 2)
    nodes["HEADLAMP_LH_HIGH"] = round(lh_high, 2)

    # 5b. Civic Wiper Motor Conversion (As-Built, powered by Fuse 7 ACC L)
    wiper_powered = fuse7_v >= 9.0 and inp.wiper_switch != "OFF"
    wiper_mode = inp.wiper_switch if wiper_powered else "OFF"
    nodes["WIPER_MOTOR_RUN"] = round(fuse7_v if wiper_powered else 0.0, 2)

    # 6. Ignition Coil & GM 4-Pin HEI Module (EF-4)
    # Coil (+) stays at BW bus voltage; poor HEI ground lifts module ground / Coil (-) during dwell.
    coil_plus_v = ign_relay_v
    hei_gnd_drop_v = min(ign_relay_v, f.hei_ground_ohms * 4.0) if ign_relay_v > 0 else 0.0
    coil_eff_v = max(0.0, coil_plus_v - hei_gnd_drop_v)
    spark_ok = coil_eff_v >= 9.0 and inp.rpm > 0
    nodes["COIL_+_BW"] = round(coil_plus_v, 2)
    nodes["HEI_GND_DROP"] = round(hei_gnd_drop_v, 2)
    # ECU Pin 1 taps Coil (-) via 2.2 kΩ resistor: rests near battery V at 0 rpm, averages lower when pulsing.
    if ign_relay_v <= 0.0:
        coil_minus_v = 0.0
    elif inp.rpm > 0 and spark_ok:
        coil_minus_v = round(hei_gnd_drop_v + 0.7 * coil_eff_v, 2)
    else:
        coil_minus_v = round(coil_plus_v, 2)
    nodes["ECU_PIN_1_RPM"] = coil_minus_v

    # 7. EFI Main Relay (Coil: BW, Contact In: 72R -> Out: 71L / 43/2L)
    efi_main_on = ign_relay_v >= 8.5 and efi_link_v >= 9.0
    efi_pwr_v = efi_link_v if efi_main_on else 0.0
    nodes["EFI_MAIN_OUT_71L"] = round(efi_pwr_v, 2)
    nodes["DROPPING_RES_43_2"] = round(efi_pwr_v, 2)

    # 8. 1978 Dual-Relay Fuel Pump Safety Circuit (EF-35 / EF-36, Figs. EF-63 & EF-64)
    # Terminal 95 = BW (IGN ON), Terminal 94 = BY (START), Terminal 96 = Alt L || Oil Sw to ground.
    fp_ctrl_95 = ign_relay_v
    fp_ctrl_94 = 0.0 if f.fp_ctrl_94_open else sta_v
    if f.alt_l_open:
        alt_l_v = 0.0
        alt_l_grounding = False
        notes.append("Alternator L wire open: alternator will not self-excite; pin 96 not grounded by Alt L.")
    elif f.alt_l_short_gnd:
        alt_l_v = 0.0
        alt_l_grounding = True
        notes.append("Alternator L wire shorted to ground: pin 96 held grounded (safety shutoff active).")
    else:
        alt_l_v = 13.8 if alt_active else (1.5 if ign_relay_v > 0 else 0.0)
        alt_l_grounding = not alt_active

    # Oil pressure switch is closed to ground at rest (< 5 psi) and open when running (>= 5 psi).
    oil_sw_grounding = inp.oil_pressure_psi < 5.0
    t96_grounded = alt_l_grounding or oil_sw_grounding
    run_interlock_ok = not t96_grounded

    if fp_ctrl_95 < 8.0:
        # Key cycled OFF or BW dropped: resets lockout latch
        lockout_tripped = False
        fp_latched = False
    else:
        lockout_tripped = inp.prev_fp_lockout_tripped
        if f.fp_control_relay_lockout_fault and inp.prev_fp_latched and t96_grounded and fp_ctrl_94 < 8.0:
            lockout_tripped = True
            notes.append("FP Control Relay safety lockout tripped; requires BW power drop (key cycle) to reset.")
        if f.fp_ctrl_94_open and sta_v >= 8.5 and t96_grounded:
            notes.append("FP Control Relay pin 94 (BY) open: cranking cannot release 95-96 safety coil.")
        fp_latched = (not lockout_tripped) and ((fp_ctrl_94 >= 8.5) or run_interlock_ok)

    # Per Fig. EF-63, Fuel Pump Relay power contacts (92-93) feed directly from the EFI fusible link bus.
    fp_relay_on = fp_latched and (not f.fp_relay_stuck_open) and (efi_link_v >= 8.5)
    fuel_pump_v = efi_link_v if fp_relay_on else 0.0
    fuel_pump_ok = fuel_pump_v >= 9.0

    nodes["FP_CTRL_95_BW"] = round(fp_ctrl_95, 2)
    nodes["FP_CTRL_94_BY"] = round(fp_ctrl_94, 2)
    nodes["ALT_L_WB"] = round(alt_l_v, 2)
    nodes["FP_RELAY_OUT_GY"] = round(fuel_pump_v, 2)
    nodes["FUEL_PUMP_+"] = round(fuel_pump_v, 2)
    nodes["AIR_REGULATOR_34"] = round(fuel_pump_v, 2)

    # 9. ECU Sensors (CTS Pin 13, TPS Pins 2/18/3, Start Pin 4, Altitude)
    if f.cts_open:
        cts_r = 999999.0
        ecu_coolant_c = -40.0
    elif f.cts_short:
        cts_r = 0.0
        ecu_coolant_c = 120.0
    else:
        base_cts_r = cts_resistance_ohms(inp.coolant_temp_c)
        cts_r = round(max(0.0, base_cts_r + f.cts_offset_ohms), 1)
        ecu_coolant_c = cts_temp_c_from_ohms(cts_r) if f.cts_offset_ohms != 0.0 else inp.coolant_temp_c
        if abs(ecu_coolant_c - inp.coolant_temp_c) >= 5.0:
            notes.append(
                f"CTS resistance offset ({f.cts_offset_ohms:+.0f} Ω) shifts ECU perceived coolant temp "
                f"from {inp.coolant_temp_c:.1f}°C to {ecu_coolant_c:.1f}°C."
            )

    tps_idle = True if f.tps_idle_stuck_closed else (False if f.tps_idle_open else inp.throttle_deg <= 1.0)
    raw_wot = inp.throttle_deg > 34.0
    tps_wot = raw_wot and (not tps_idle)
    if raw_wot and f.tps_idle_stuck_closed:
        notes.append("TPS idle contact stuck closed: masks WOT signal and risks decel fuel cut above 3200 rpm.")
    nodes["ECU_PIN_4_START"] = round(sta_v, 2)
    nodes["ECU_PIN_2_TPS_IDLE"] = 0.0 if tps_idle else 5.0
    nodes["ECU_PIN_3_TPS_WOT"] = 0.0 if tps_wot else 5.0

    # 10. Bosch L-Jetronic Operating Mode (EF-10 to EF-12)
    # Fuel cut hysteresis: stops >= 3200 rpm with idle closed, recovers < 2800 rpm
    if inp.prev_fuel_cut:
        fuel_cut = tps_idle and (inp.rpm >= 2800)
    else:
        fuel_cut = tps_idle and (inp.rpm >= 3200)

    enrichment = 1.0
    if inp.baro_mmhg <= 660.0:
        enrichment *= 0.94  # California altitude compensation (-6%)

    if efi_pwr_v < 8.5:
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
    elif sta_v >= 8.5:
        mode = "CRANK_COLD_START" if ecu_coolant_c < 15.0 else "CRANK_START"
        enrichment *= 1.8 if f.cts_open else (1.5 if ecu_coolant_c < 20.0 else (1.2 if ecu_coolant_c < 70.0 else 1.0))
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

    if f.cts_open:
        notes.append("CTS open circuit: ECU sees extreme cold (-40°C), causing severe over-fueling/flooding.")
    elif f.cts_short:
        notes.append("CTS shorted: ECU sees hot engine, starving cold starts.")

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
    parser.add_argument("--key", default="ON", choices=["OFF", "ACC", "ON", "START"])
    parser.add_argument("--rpm", type=int, default=800)
    parser.add_argument("--throttle", type=float, default=0.0)
    parser.add_argument("--coolant", type=float, default=75.0)
    parser.add_argument("--oil", type=float, default=35.0)
    parser.add_argument("--w-splice-ohms", type=float, default=0.0)
    args = parser.parse_args()

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
        run_sequence(steps, Faults(fp_control_relay_lockout_fault=True))
        return

    if args.scenario == "issue2":
        print("=== Issue #2: Cold-Only Start & Stall (Healthy vs. Corroded/Open/Shorted CTS) ===")
        for name, flt in [
            ("Healthy (10°C)", Faults()),
            ("CTS +5kΩ Offset", Faults(cts_offset_ohms=5000.0)),
            ("CTS Shorted", Faults(cts_short=True)),
            ("CTS Open", Faults(cts_open=True)),
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
        Faults(w_splice_ohms=args.w_splice_ohms),
    )
    print(f"ECU Mode: {st.ecu_mode} (Enrichment: {st.ecu_enrichment_factor}x) | Fuel Pump: {'ON' if st.fuel_pump_on else 'OFF'} | Spark: {'OK' if st.spark_available else 'NO'}")
    print(f"CTS: {st.cts_ohms} Ω (ECU: {st.ecu_coolant_c}°C) | TPS Idle: {st.tps_idle_closed} | TPS WOT: {st.tps_wot_closed} | Wiper: {st.wiper_mode}")
    for k, v in st.nodes.items():
        print(f"  {k:22s}: {v:5.2f} V")


if __name__ == "__main__":
    main()
