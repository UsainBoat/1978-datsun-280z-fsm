"""Time-stepped closed-loop simulator for the 1978 Datsun 280Z Digital Twin.

Wraps twin.z280_twin.simulate() with starter cranking, RPM inertia, oil pressure
lag, fuel rail pressure build/bleed-down (36.3 psi / 2.55 kg/cm²), and cold-start
valve timing so transient driving and starting faults can be traced over time.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from twin.z280_twin import (
    CarInputs,
    ConnectorFault,
    Faults,
    TwinState,
    parse_connector_fault_arg,
    simulate,
)


FACTORY_RAIL_PSI = 36.3  # 2.55 kg/cm² (EF-5)


@dataclass
class SimFaults:
    elec: Faults = field(default_factory=Faults)
    check_valve_leak: bool = False       # Overnight rail bleed-down & rapid pressure drop when pump off
    idle_undershoot_fault: bool = False  # Clutch-in decel RPM undershoot (BCDD / idle bypass dip)
    idle_air_leak_rpm: float = 0.0       # Elevated warm idle creep (e.g., +200 rpm for Issue #3)
    starter_dead: bool = False           # Starter motor / solenoid fails to turn engine


@dataclass
class SimFrame:
    time_s: float
    key_pos: str
    throttle_deg: float
    clutch_in: bool
    rpm: float
    oil_pressure_psi: float
    fuel_rail_psi: float
    csv_active: bool
    firing: bool
    state: TwinState


class Z280Car:
    """Closed-loop stateful 1978 280Z vehicle simulator."""

    def __init__(
        self,
        faults: Optional[SimFaults] = None,
        coolant_temp_c: float = 20.0,
        air_temp_c: float = 20.0,
        battery_rest_v: float = 12.6,
        initial_rail_psi: Optional[float] = None,
        baro_mmhg: float = 760.0,
    ) -> None:
        self.faults = faults or SimFaults()
        self.coolant_temp_c = coolant_temp_c
        self.air_temp_c = air_temp_c
        self.battery_rest_v = battery_rest_v
        self.baro_mmhg = baro_mmhg

        self.time_s: float = 0.0
        self.rpm: float = 0.0
        self.oil_pressure_psi: float = 0.0
        if initial_rail_psi is not None:
            self.fuel_rail_psi = initial_rail_psi
        else:
            self.fuel_rail_psi = 0.0 if self.faults.check_valve_leak else 25.0

        self.csv_elapsed_s: float = 0.0
        self._undershoot_active: bool = False
        self._prev_fuel_cut: bool = False
        self._prev_fp_latched: bool = False
        self._prev_fp_lockout_tripped: bool = False

    def _target_oil_psi(self, rpm: float) -> float:
        if rpm <= 10.0:
            return 0.0
        cold_bonus = 6.0 if self.coolant_temp_c < 35.0 else 0.0
        if rpm < 300.0:
            # Cranking oil pressure (~9-11 psi at 220 rpm)
            return round((rpm / 220.0) * 10.0 + cold_bonus * 0.5, 1)
        if rpm < 500.0:
            # Hot dip below 450 rpm drops near/below the 5 psi oil switch threshold
            base = 3.5 + ((rpm - 300.0) / 200.0) * 10.0
            return round(base + cold_bonus, 1)
        # Normal running curve: ~35 psi at 800 rpm hot, ~55 psi at 3500+ rpm
        base = 25.0 + min(30.0, (rpm - 500.0) * 0.01)
        return round(base + cold_bonus, 1)

    def step(
        self,
        dt: float = 0.1,
        key_pos: str = "OFF",
        throttle_deg: float = 0.0,
        clutch_in: bool = True,
        light_switch: str = "OFF",
        dimmer: str = "LOW",
        wiper_switch: str = "OFF",
    ) -> SimFrame:
        """Advance vehicle electrical, hydraulic, and engine state by dt seconds."""
        # 1. Check starter engagement and battery sag during START
        cranking = key_pos == "START" and (not self.faults.starter_dead) and (self.battery_rest_v >= 9.5)
        eff_batt_v = max(0.0, self.battery_rest_v - 1.8) if cranking else self.battery_rest_v

        # Starter spins engine up to ~220 rpm when engaged
        if cranking and self.rpm < 220.0:
            self.rpm = min(220.0, self.rpm + 800.0 * dt)

        # 2. Evaluate electrical & ECU snapshot
        inp = CarInputs(
            key_pos=key_pos,
            rpm=int(round(self.rpm)),
            throttle_deg=throttle_deg,
            coolant_temp_c=self.coolant_temp_c,
            air_temp_c=self.air_temp_c,
            oil_pressure_psi=self.oil_pressure_psi,
            baro_mmhg=self.baro_mmhg,
            light_switch=light_switch,
            dimmer=dimmer,
            wiper_switch=wiper_switch,
            battery_v=eff_batt_v,
            prev_fuel_cut=self._prev_fuel_cut,
            prev_fp_latched=self._prev_fp_latched,
            prev_fp_lockout_tripped=self._prev_fp_lockout_tripped,
        )
        st = simulate(inp, self.faults.elec)

        # Verify starter actually has voltage on BY and STARTER_B
        starter_powered = (
            cranking
            and st.nodes["STARTER_B"] >= 8.5
            and st.nodes["IGN_SW_STA_BY"] >= 8.5
        )

        # 3. Cold Start Valve / Thermotime Switch (active on START when < 35°C, up to 8s)
        max_csv_s = 8.0 if self.coolant_temp_c < 15.0 else (4.0 if self.coolant_temp_c < 35.0 else 0.0)
        csv_active = (
            key_pos == "START"
            and st.nodes["IGN_SW_STA_BY"] >= 8.5
            and self.fuel_rail_psi >= 15.0
            and self.csv_elapsed_s < max_csv_s
        )
        if csv_active:
            self.csv_elapsed_s += dt
        elif key_pos != "START":
            self.csv_elapsed_s = max(0.0, self.csv_elapsed_s - 0.5 * dt)

        # 4. Fuel rail pressure dynamics (36.3 psi / 2.55 kg/cm² regulated)
        # ECU does not sense fuel pump relay dropout; injectors keep pulsing while EFI has power & RPM.
        has_efi_pulse = (
            st.nodes["EFI_MAIN_OUT_71L"] >= 8.5
            and (not st.fuel_cut_active)
            and self.rpm >= 140.0
        )
        if st.fuel_pump_on:
            self.fuel_rail_psi = min(FACTORY_RAIL_PSI, self.fuel_rail_psi + 24.0 * dt)
        else:
            consuming = has_efi_pulse or csv_active
            if consuming:
                bleed_rate = 24.0 if self.faults.check_valve_leak else 18.0
            else:
                bleed_rate = 3.0 if self.faults.check_valve_leak else 0.1
            self.fuel_rail_psi = max(0.0, self.fuel_rail_psi - bleed_rate * dt)

        # 5. Combustion check (spark + fuel rail pressure + viable cold mixture)
        # Residual rail pressure can sustain brief firing/sputtering until rail decays below ~20 psi.
        cold_starved = (
            self.coolant_temp_c < 15.0
            and st.ecu_enrichment_factor < 1.15
            and (not csv_active)
        )
        severe_flood = self.faults.elec.cts_open and self.coolant_temp_c > 50.0

        sputtering = (
            st.spark_available
            and has_efi_pulse
            and (12.0 <= self.fuel_rail_psi < 20.0)
            and (not cold_starved)
        )
        firing = (
            st.spark_available
            and ((has_efi_pulse and self.fuel_rail_psi >= 20.0 and not cold_starved and not severe_flood) or csv_active)
        )

        # 6. RPM dynamics
        fast_idle_bonus = 350.0 if self.coolant_temp_c < 40.0 else 0.0
        target_idle_rpm = 800.0 + fast_idle_bonus + self.faults.idle_air_leak_rpm

        # Detect clutch-in decel from high RPM for undershoot fault
        if (
            self.faults.idle_undershoot_fault
            and clutch_in
            and throttle_deg <= 1.0
            and self.rpm >= 2600.0
        ):
            self._undershoot_active = True
        elif throttle_deg > 1.0 or key_pos == "OFF" or self.rpm <= 10.0:
            self._undershoot_active = False

        if firing:
            if self._undershoot_active and clutch_in and throttle_deg <= 1.0:
                # Undershoot down to ~350 rpm before idle air catches
                self.rpm = max(350.0, self.rpm - 2600.0 * dt)
                if self.rpm <= 360.0:
                    self._undershoot_active = False
            else:
                target_rpm = min(6500.0, target_idle_rpm + throttle_deg * 150.0)
                if self.rpm < target_rpm:
                    rate = 2400.0 if key_pos != "START" else 1600.0
                    self.rpm = min(target_rpm, self.rpm + rate * dt)
                else:
                    rate = 2000.0 if clutch_in else 900.0
                    self.rpm = max(target_rpm, self.rpm - rate * dt)
        elif sputtering:
            # Sputters weakly around 280-320 rpm on residual rail pressure
            target_rpm = 300.0
            if self.rpm > target_rpm:
                self.rpm = max(target_rpm, self.rpm - 1800.0 * dt)
            else:
                self.rpm = min(target_rpm, self.rpm + 600.0 * dt)
        else:
            if starter_powered:
                target_rpm = 220.0
                if self.rpm < target_rpm:
                    self.rpm = min(target_rpm, self.rpm + 800.0 * dt)
                else:
                    self.rpm = max(target_rpm, self.rpm - 1500.0 * dt)
            else:
                decay = 1600.0 if clutch_in else 750.0
                self.rpm = max(0.0, self.rpm - decay * dt)

        # 7. Oil pressure lag (first-order filter toward RPM-dependent target)
        target_oil = self._target_oil_psi(self.rpm)
        oil_alpha = min(1.0, dt / 0.35)
        self.oil_pressure_psi = round(
            self.oil_pressure_psi + oil_alpha * (target_oil - self.oil_pressure_psi), 1
        )

        # 8. Slow coolant warmup while firing
        if firing and self.coolant_temp_c < 85.0:
            self.coolant_temp_c = min(85.0, self.coolant_temp_c + 0.15 * dt)

        # 9. Update latches and advance clock
        self._prev_fuel_cut = st.fuel_cut_active
        self._prev_fp_latched = st.fp_latched
        self._prev_fp_lockout_tripped = st.fp_lockout_tripped
        self.time_s = round(self.time_s + dt, 2)

        return SimFrame(
            time_s=self.time_s,
            key_pos=key_pos,
            throttle_deg=throttle_deg,
            clutch_in=clutch_in,
            rpm=round(self.rpm, 0),
            oil_pressure_psi=self.oil_pressure_psi,
            fuel_rail_psi=round(self.fuel_rail_psi, 1),
            csv_active=csv_active,
            firing=firing,
            state=st,
        )

    def run_phase(
        self,
        duration_s: float,
        dt: float = 0.2,
        key_pos: str = "ON",
        throttle_deg: float = 0.0,
        clutch_in: bool = True,
        light_switch: str = "OFF",
        dimmer: str = "LOW",
        wiper_switch: str = "OFF",
    ) -> List[SimFrame]:
        steps = max(1, int(round(duration_s / dt)))
        frames: List[SimFrame] = []
        for _ in range(steps):
            frames.append(
                self.step(
                    dt=dt,
                    key_pos=key_pos,
                    throttle_deg=throttle_deg,
                    clutch_in=clutch_in,
                    light_switch=light_switch,
                    dimmer=dimmer,
                    wiper_switch=wiper_switch,
                )
            )
        return frames


def print_trace(title: str, frames: List[SimFrame], stride: int = 1) -> None:
    print(f"=== {title} ===")
    print(
        f"{'t(s)':>5s} | {'Key':5s} | {'Thr':>4s} | {'RPM':>5s} | {'Oil':>5s} | {'Rail':>5s} | "
        f"{'FP':3s} | {'Fire':4s} | {'Mode':20s} | {'BW95':>5s} | {'AltL':>5s}"
    )
    print("-" * 100)
    for idx, fr in enumerate(frames):
        if idx % stride != 0 and idx != len(frames) - 1:
            continue
        st = fr.state
        print(
            f"{fr.time_s:5.1f} | {fr.key_pos:5s} | {fr.throttle_deg:4.0f} | {fr.rpm:5.0f} | "
            f"{fr.oil_pressure_psi:5.1f} | {fr.fuel_rail_psi:5.1f} | "
            f"{'ON ' if st.fuel_pump_on else 'OFF'} | {'YES ' if fr.firing else 'NO  '} | "
            f"{st.ecu_mode:20s} | {st.nodes['FP_CTRL_95_BW']:5.2f} | {st.nodes['ALT_L_WB']:5.2f}"
        )


def run_scenario(name: str, conn_faults: Optional[Dict[str, ConnectorFault]] = None) -> List[SimFrame]:
    cf = dict(conn_faults) if conn_faults else {}
    if name == "start":
        car = Z280Car(faults=SimFaults(elec=Faults(connector_faults=cf)), coolant_temp_c=20.0, air_temp_c=20.0)
        frames: List[SimFrame] = []
        frames += car.run_phase(0.4, dt=0.2, key_pos="ON", throttle_deg=0.0)
        frames += car.run_phase(1.4, dt=0.2, key_pos="START", throttle_deg=0.0)
        frames += car.run_phase(2.0, dt=0.2, key_pos="ON", throttle_deg=0.0)
        return frames

    if name == "issue5":
        car = Z280Car(
            faults=SimFaults(
                elec=Faults(fp_control_relay_lockout_fault=True, connector_faults=cf),
                idle_undershoot_fault=True,
            ),
            coolant_temp_c=80.0,
            air_temp_c=25.0,
        )
        frames = []
        # 1. Start and settle
        frames += car.run_phase(1.2, dt=0.2, key_pos="START", throttle_deg=0.0, clutch_in=True)
        frames += car.run_phase(1.0, dt=0.2, key_pos="ON", throttle_deg=0.0, clutch_in=True)
        # 2. Cruise at ~3500 rpm
        frames += car.run_phase(1.8, dt=0.2, key_pos="ON", throttle_deg=18.0, clutch_in=False)
        # 3. Lift off + clutch in -> decel fuel cut -> undershoot dip -> lockout trips & stalls
        frames += car.run_phase(3.6, dt=0.2, key_pos="ON", throttle_deg=0.0, clutch_in=True)
        # 4. Re-crank straight from ON without turning OFF (lockout stays tripped)
        frames += car.run_phase(1.4, dt=0.2, key_pos="START", throttle_deg=0.0, clutch_in=True)
        # 5. Turn key OFF to reset BW (pin 95)
        frames += car.run_phase(0.6, dt=0.2, key_pos="OFF", throttle_deg=0.0, clutch_in=True)
        # 6. Re-crank after OFF -> pump runs, rail rebuilds, engine fires and idles
        frames += car.run_phase(1.8, dt=0.2, key_pos="START", throttle_deg=0.0, clutch_in=True)
        frames += car.run_phase(1.4, dt=0.2, key_pos="ON", throttle_deg=0.0, clutch_in=True)
        return frames

    if name == "issue2-leak":
        car = Z280Car(
            faults=SimFaults(elec=Faults(connector_faults=cf), check_valve_leak=True),
            coolant_temp_c=10.0,
            air_temp_c=10.0,
            initial_rail_psi=0.0,
        )
        frames = []
        # First short crank: rail builds from 0 psi, barely reaches ~19 psi
        frames += car.run_phase(0.8, dt=0.2, key_pos="START", throttle_deg=0.0)
        frames += car.run_phase(0.8, dt=0.2, key_pos="ON", throttle_deg=0.0)
        # Second crank: rail reaches full 36.3 psi, catches and holds fast idle
        frames += car.run_phase(1.4, dt=0.2, key_pos="START", throttle_deg=0.0)
        frames += car.run_phase(1.6, dt=0.2, key_pos="ON", throttle_deg=0.0)
        return frames

    if name == "issue2-cts":
        car = Z280Car(
            faults=SimFaults(elec=Faults(cts_short=True, connector_faults=cf)),
            coolant_temp_c=10.0,
            air_temp_c=10.0,
        )
        frames = []
        # Fires briefly on Cold Start Valve during START, stalls once key returns to ON
        frames += car.run_phase(1.4, dt=0.2, key_pos="START", throttle_deg=0.0)
        frames += car.run_phase(2.0, dt=0.2, key_pos="ON", throttle_deg=0.0)
        return frames

    raise ValueError(f"Unknown scenario: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="1978 Datsun 280Z Closed-Loop Transient Simulator")
    parser.add_argument(
        "--scenario",
        default="start",
        choices=["start", "issue5", "issue2-leak", "issue2-cts"],
        help="Transient scenario to simulate",
    )
    parser.add_argument(
        "--conn-fault",
        action="append",
        default=[],
        metavar="ID=FAULT",
        help="Inject connector/splice fault (e.g. C7_C8_REAR:6=open, C1_ENG_DASH:1=0.4ohm)",
    )
    args = parser.parse_args()
    conn_faults: Dict[str, ConnectorFault] = {}
    for spec in args.conn_fault:
        cid, cf = parse_connector_fault_arg(spec)
        conn_faults[cid] = cf
    frames = run_scenario(args.scenario, conn_faults=conn_faults)
    print_trace(f"Scenario: {args.scenario}", frames)


if __name__ == "__main__":
    main()
