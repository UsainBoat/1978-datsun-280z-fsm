"""Monte Carlo fault-permutation explorer for the 1978 Datsun 280Z Digital Twin.

Samples combinations of electrical, sensor, hydraulic, and mechanical faults
against observable symptom signatures from STATE_OF_THE_Z.md:
  - Issue #5: Warm cruise OK -> coasting decel stall -> re-crank without OFF fails ->
              key OFF reset -> re-crank fires and idles.
  - Issue #2: Cold (10°C) first crank stalls or fails to stay running, takes extra crank
              (or stalls after CSV) while warm (80°C) restarts work on the first try.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import random
from typing import Dict, List, Tuple

from twin.z280_sim import SimFaults, Z280Car
from twin.z280_twin import ConnectorFault, Faults


# High-value harness connectors/splices for Monte Carlo corrosion & contact-fault sweeps
POWER_CORROSION_CONNS: List[str] = [
    "SPLICE_W_MAIN",
    "C2_ALT_CHG:1",
    "C9_C10_IGN_PWR:1",
    "C9_C10_IGN_PWR:3",
    "C9_C10_IGN_PWR:4",
    "C1_ENG_DASH:1",
    "C1_ENG_DASH:2",
    "C4_EFI_IGN:1",
    "C4_EFI_IGN:2",
    "C7_C8_REAR:6",
    "EFI_MAIN_RELAY:3",
    "EFI_MAIN_RELAY:4",
    "FP_RELAY:92",
    "FP_RELAY:93",
    "FUEL_PUMP:GND",
    "ECU:5",
    "HEI_MODULE:GND",
]

SIGNAL_CONNS: List[str] = [
    "C2_ALT_CHG:3",
    "C4_EFI_IGN:5",
    "FP_CONTROL_RELAY:94",
    "FP_CONTROL_RELAY:95",
    "FP_CONTROL_RELAY:96",
    "FP_CONTROL_RELAY:91",
    "FP_RELAY:90",
    "WATER_TEMP_SENSOR",
    "ECU:13",
    "ECU:4",
    "TPS:2",
    "TPS:18",
]


@dataclass
class SampleCase:
    faults: SimFaults
    battery_v: float
    cold_rail_psi: float

    def active_labels(self) -> Tuple[str, ...]:
        e = self.faults.elec
        labels: List[str] = []
        if e.fp_control_relay_lockout_fault:
            labels.append("fp_lockout_fault")
        if e.fp_ctrl_94_open:
            labels.append("fp_pin94_open")
        if e.fp_relay_stuck_open:
            labels.append("fp_relay_stuck_open")
        if e.alt_l_open:
            labels.append("alt_l_open")
        if e.alt_l_short_gnd:
            labels.append("alt_l_short_gnd")
        if e.cts_short:
            labels.append("cts_short")
        if e.cts_open:
            labels.append("cts_open")
        if abs(e.cts_offset_ohms) >= 500.0:
            labels.append("cts_offset_high" if e.cts_offset_ohms > 0 else "cts_offset_low")
        if e.tps_idle_stuck_closed:
            labels.append("tps_idle_stuck_closed")
        if e.w_splice_ohms >= 0.08:
            labels.append("w_splice_drop")
        if e.ign_switch_drop_ohms >= 0.10:
            labels.append("ign_sw_drop")
        if e.hei_ground_ohms >= 0.40:
            labels.append("hei_gnd_drop")
        if self.faults.check_valve_leak:
            labels.append("check_valve_leak")
        if self.faults.idle_undershoot_fault:
            labels.append("idle_undershoot")
        if self.battery_v <= 11.8:
            labels.append("weak_battery")
        if self.cold_rail_psi <= 10.0 and not self.faults.check_valve_leak:
            labels.append("low_overnight_rail")
        for cid, cf in sorted(e.connector_faults.items()):
            if cf.open:
                labels.append(f"conn:{cid}(open)")
            elif cf.short_gnd:
                labels.append(f"conn:{cid}(short_gnd)")
            elif cf.ohms > 0.0:
                labels.append(f"conn:{cid}(ohms)")
        return tuple(labels) if labels else ("nominal",)


def check_issue5(case: SampleCase) -> bool:
    """Check if a fault case reproduces Issue #5:
    1. Warm start and cruise at 3500 rpm succeed.
    2. Coasting decel (throttle 0, clutch in) stalls engine to 0 rpm.
    3. Re-crank straight from ON -> START (no OFF) fails to catch.
    4. Key OFF -> START fires right up and holds idle.
    """
    car = Z280Car(
        faults=case.faults,
        coolant_temp_c=80.0,
        air_temp_c=25.0,
        battery_rest_v=case.battery_v,
        initial_rail_psi=25.0,
    )
    # 1. Start and cruise
    car.run_phase(1.4, dt=0.2, key_pos="START", throttle_deg=0.0, clutch_in=True)
    idle1 = car.run_phase(1.0, dt=0.2, key_pos="ON", throttle_deg=0.0, clutch_in=True)
    if not idle1[-1].firing or idle1[-1].rpm < 600.0:
        return False

    cruise = car.run_phase(1.8, dt=0.2, key_pos="ON", throttle_deg=18.0, clutch_in=False)
    if not cruise[-1].firing or cruise[-1].rpm < 2800.0:
        return False

    # 2. Coast down with clutch in -> must stall to 0 rpm
    coast = car.run_phase(3.8, dt=0.2, key_pos="ON", throttle_deg=0.0, clutch_in=True)
    if coast[-1].rpm > 0.0:
        return False

    # 3. Re-crank straight from ON -> START -> must fail to catch/run
    recrank = car.run_phase(1.4, dt=0.2, key_pos="START", throttle_deg=0.0, clutch_in=True)
    after_recrank = car.run_phase(0.6, dt=0.2, key_pos="ON", throttle_deg=0.0, clutch_in=True)
    if after_recrank[-1].firing and after_recrank[-1].rpm >= 600.0:
        return False

    # 4. Key OFF reset -> re-crank must catch and idle cleanly
    car.run_phase(0.6, dt=0.2, key_pos="OFF", throttle_deg=0.0, clutch_in=True)
    car.run_phase(2.0, dt=0.2, key_pos="START", throttle_deg=0.0, clutch_in=True)
    final = car.run_phase(1.2, dt=0.2, key_pos="ON", throttle_deg=0.0, clutch_in=True)
    return final[-1].firing and final[-1].rpm >= 600.0


def check_issue2(case: SampleCase) -> Tuple[bool, str]:
    """Check if a fault case reproduces Issue #2:
    - Warm (80°C) restart succeeds on the first short crank and holds idle.
    - Cold (10°C) first short crank stalls after releasing to ON.
    Returns (matched, subtype) where subtype distinguishes multi-crank catch vs persistent cold stall.
    """
    # Control: warm restart must work cleanly on first crank
    warm_car = Z280Car(
        faults=case.faults,
        coolant_temp_c=80.0,
        air_temp_c=25.0,
        battery_rest_v=case.battery_v,
        initial_rail_psi=25.0,
    )
    warm_car.run_phase(1.0, dt=0.2, key_pos="START", throttle_deg=0.0)
    warm_on = warm_car.run_phase(1.4, dt=0.2, key_pos="ON", throttle_deg=0.0)
    if not (warm_on[-1].firing and warm_on[-1].rpm >= 600.0):
        return False, ""

    # Cold test (10°C / 50°F): first short crank (0.8s) -> release to ON
    cold_car = Z280Car(
        faults=case.faults,
        coolant_temp_c=10.0,
        air_temp_c=10.0,
        battery_rest_v=case.battery_v,
        initial_rail_psi=0.0 if case.faults.check_valve_leak else case.cold_rail_psi,
    )
    crank1 = cold_car.run_phase(0.8, dt=0.2, key_pos="START", throttle_deg=0.0)
    on1 = cold_car.run_phase(1.2, dt=0.2, key_pos="ON", throttle_deg=0.0)
    # Must fail to stay running after first crank
    if on1[-1].firing and on1[-1].rpm >= 600.0:
        return False, ""

    # Second crank (1.4s) -> release to ON
    cold_car.run_phase(1.4, dt=0.2, key_pos="START", throttle_deg=0.0)
    on2 = cold_car.run_phase(1.4, dt=0.2, key_pos="ON", throttle_deg=0.0)
    if on2[-1].firing and on2[-1].rpm >= 800.0:
        return True, "catches_on_2nd_crank"
    if any(fr.firing for fr in crank1):
        return True, "fires_on_csv_then_stalls"
    return True, "cold_no_catch_warm_ok"


def sample_case(rng: random.Random) -> SampleCase:
    """Draw a random fault/parameter permutation from realistic 1978 280Z failure priors."""
    # Mutually exclusive CTS discrete faults
    cts_roll = rng.random()
    cts_short = cts_roll < 0.10
    cts_open = 0.10 <= cts_roll < 0.15
    cts_offset = 0.0
    if not cts_short and not cts_open and rng.random() < 0.25:
        cts_offset = rng.choice([-2500.0, -1800.0, 1500.0, 3500.0, 5000.0])

    # Mutually exclusive Alt L faults
    alt_roll = rng.random()
    alt_l_open = alt_roll < 0.08
    alt_l_short_gnd = 0.08 <= alt_roll < 0.12

    # Sample 0 or 1 harness connector/splice fault (corrosion, open, or short_gnd)
    conn_faults: Dict[str, ConnectorFault] = {}
    conn_roll = rng.random()
    if conn_roll < 0.30:
        cid = rng.choice(POWER_CORROSION_CONNS)
        conn_faults[cid] = ConnectorFault(ohms=round(rng.uniform(0.15, 1.4), 2))
    elif conn_roll < 0.48:
        cid = rng.choice(SIGNAL_CONNS)
        if cid in ("WATER_TEMP_SENSOR", "ECU:13"):
            if not cts_short and not cts_open:
                mode_r = rng.random()
                if mode_r < 0.45:
                    conn_faults[cid] = ConnectorFault(ohms=rng.choice([1500.0, 3500.0, 5000.0]))
                elif mode_r < 0.75:
                    conn_faults[cid] = ConnectorFault(short_gnd=True)
                else:
                    conn_faults[cid] = ConnectorFault(open=True)
        elif cid == "C2_ALT_CHG:3":
            if not alt_l_open and not alt_l_short_gnd:
                conn_faults[cid] = ConnectorFault(open=True) if rng.random() < 0.6 else ConnectorFault(short_gnd=True)
        elif cid in ("TPS:2", "TPS:18"):
            conn_faults[cid] = ConnectorFault(open=True) if rng.random() < 0.7 else ConnectorFault(short_gnd=True)
        else:
            conn_faults[cid] = ConnectorFault(open=True) if rng.random() < 0.5 else ConnectorFault(ohms=round(rng.uniform(0.5, 4.0), 2))

    elec = Faults(
        w_splice_ohms=round(rng.uniform(0.05, 0.30), 3) if rng.random() < 0.20 else 0.0,
        ign_switch_drop_ohms=round(rng.uniform(0.08, 0.40), 3) if rng.random() < 0.20 else 0.0,
        fp_control_relay_lockout_fault=rng.random() < 0.30,
        fp_ctrl_94_open=rng.random() < 0.18,
        alt_l_open=alt_l_open,
        alt_l_short_gnd=alt_l_short_gnd,
        cts_short=cts_short,
        cts_open=cts_open,
        cts_offset_ohms=cts_offset,
        tps_idle_stuck_closed=rng.random() < 0.08,
        hei_ground_ohms=round(rng.uniform(0.3, 1.3), 2) if rng.random() < 0.15 else 0.0,
        headlamp_relayed=True,
        connector_faults=conn_faults,
    )
    sim_faults = SimFaults(
        elec=elec,
        check_valve_leak=rng.random() < 0.25,
        idle_undershoot_fault=rng.random() < 0.35,
    )
    battery_v = round(rng.uniform(11.4, 12.7), 2) if rng.random() < 0.35 else 12.6
    cold_rail_psi = round(rng.uniform(0.0, 15.0), 1) if rng.random() < 0.25 else 25.0
    return SampleCase(faults=sim_faults, battery_v=battery_v, cold_rail_psi=cold_rail_psi)


def run_monte_carlo(n_samples: int = 3000, seed: int = 42) -> Dict[str, object]:
    rng = random.Random(seed)
    issue5_sigs: Counter[Tuple[str, ...]] = Counter()
    issue2_sigs: Counter[Tuple[str, Tuple[str, ...]]] = Counter()
    both_sigs: Counter[Tuple[str, Tuple[str, ...]]] = Counter()
    param_ranges: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))

    n_i5 = 0
    n_i2 = 0
    n_both = 0

    for _ in range(n_samples):
        case = sample_case(rng)
        sig = case.active_labels()
        m5 = check_issue5(case)
        m2, sub2 = check_issue2(case)

        if m5:
            n_i5 += 1
            issue5_sigs[sig] += 1
            param_ranges["issue5"]["battery_v"].append(case.battery_v)
            param_ranges["issue5"]["w_splice_ohms"].append(case.faults.elec.w_splice_ohms)
            param_ranges["issue5"]["ign_switch_drop_ohms"].append(case.faults.elec.ign_switch_drop_ohms)
        if m2:
            n_i2 += 1
            issue2_sigs[(sub2, sig)] += 1
            param_ranges["issue2"]["battery_v"].append(case.battery_v)
            param_ranges["issue2"]["cold_rail_psi"].append(
                0.0 if case.faults.check_valve_leak else case.cold_rail_psi
            )
            param_ranges["issue2"]["cts_offset_ohms"].append(case.faults.elec.cts_offset_ohms)
        if m5 and m2:
            n_both += 1
            both_sigs[(sub2, sig)] += 1

    return {
        "n_samples": n_samples,
        "n_issue5": n_i5,
        "n_issue2": n_i2,
        "n_both": n_both,
        "issue5_sigs": issue5_sigs,
        "issue2_sigs": issue2_sigs,
        "both_sigs": both_sigs,
        "param_ranges": param_ranges,
    }


def _minimal_signatures(counter: Counter[Tuple[str, ...]]) -> List[Tuple[Tuple[str, ...], int]]:
    """Return signatures that are not strict supersets of another matching signature."""
    sets = [(set(k), k, v) for k, v in counter.items()]
    minimal: List[Tuple[Tuple[str, ...], int]] = []
    for s_i, k_i, v_i in sets:
        if not any(s_j < s_i for s_j, _, _ in sets):
            minimal.append((k_i, v_i))
    minimal.sort(key=lambda item: (len(item[0]), -item[1]))
    return minimal


def print_report(res: Dict[str, object], top_n: int = 8) -> None:
    n = res["n_samples"]
    print(f"=== Monte Carlo Fault-Permutation Sweep ({n} samples) ===")
    print(
        f"Matches -> Issue #5: {res['n_issue5']} ({100.0 * res['n_issue5'] / n:.1f}%) | "
        f"Issue #2: {res['n_issue2']} ({100.0 * res['n_issue2'] / n:.1f}%) | "
        f"BOTH simultaneously: {res['n_both']} ({100.0 * res['n_both'] / n:.1f}%)"
    )

    print("\n-- Issue #5: Minimal Matching Fault Combinations --")
    i5_min = _minimal_signatures(res["issue5_sigs"])
    for sig, cnt in i5_min[:top_n]:
        print(f"  [{cnt:4d} hits] {' + '.join(sig)}")

    print("\n-- Issue #5: Most Frequent Permutations --")
    for sig, cnt in res["issue5_sigs"].most_common(top_n):
        print(f"  [{cnt:4d} hits] {' + '.join(sig)}")

    print("\n-- Issue #2 (Cold-Only Fail, Warm OK): Top Permutations by Subtype --")
    by_sub: Dict[str, Counter[Tuple[str, ...]]] = defaultdict(Counter)
    for (sub, sig), cnt in res["issue2_sigs"].items():
        by_sub[sub][sig] += cnt
    for sub, cntr in sorted(by_sub.items()):
        print(f"  Subtype: {sub}")
        for sig, cnt in _minimal_signatures(cntr)[:5]:
            print(f"    [{cnt:4d} hits] {' + '.join(sig)}")

    print("\n-- Combined (Reproduces BOTH Issue #5 AND Issue #2 on the Same Car) --")
    both_flat: Counter[Tuple[str, ...]] = Counter()
    for (_, sig), cnt in res["both_sigs"].items():
        both_flat[sig] += cnt
    for sig, cnt in _minimal_signatures(both_flat)[:top_n]:
        print(f"  [{cnt:4d} hits] {' + '.join(sig)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monte Carlo fault-permutation explorer for 1978 280Z")
    parser.add_argument("--samples", type=int, default=3000, help="Number of Monte Carlo samples")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--top", type=int, default=8, help="Max signatures to display per section")
    args = parser.parse_args()
    res = run_monte_carlo(n_samples=args.samples, seed=args.seed)
    print_report(res, top_n=args.top)


if __name__ == "__main__":
    main()
