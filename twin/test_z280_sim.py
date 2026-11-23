"""Unit tests for the closed-loop 1978 Datsun 280Z transient simulator."""

import unittest

from twin.z280_mc import run_monte_carlo
from twin.z280_sim import SimFaults, Z280Car, run_scenario
from twin.z280_twin import Faults


class TestZ280Sim(unittest.TestCase):
    def test_healthy_start_cranks_catches_and_idles(self) -> None:
        frames = run_scenario("start")
        # Starts at 0 rpm with key ON
        self.assertEqual(frames[0].rpm, 0.0)
        self.assertFalse(frames[0].state.fuel_pump_on)
        # Catches during START and holds fast idle (~1150 rpm at 20°C) in ON
        final = frames[-1]
        self.assertEqual(final.key_pos, "ON")
        self.assertTrue(final.firing)
        self.assertTrue(final.state.fuel_pump_on)
        self.assertGreaterEqual(final.rpm, 800.0)
        self.assertGreaterEqual(final.fuel_rail_psi, 35.0)

    def test_issue5_decel_undershoot_lockout_and_key_off_reset(self) -> None:
        frames = run_scenario("issue5")
        # Must hit DECEL_FUEL_CUT, then stall to 0 rpm with lockout tripped
        modes = [fr.state.ecu_mode for fr in frames]
        self.assertIn("DECEL_FUEL_CUT", modes)
        self.assertTrue(any(fr.state.fp_lockout_tripped for fr in frames))

        # During re-crank without OFF (around t=7.8..9.0), pump stays OFF and engine does not catch
        recrank_no_off = [fr for fr in frames if 7.8 <= fr.time_s <= 9.0]
        self.assertTrue(all(not fr.state.fuel_pump_on for fr in recrank_no_off))
        self.assertTrue(all(not fr.firing for fr in recrank_no_off))

        # After cycling OFF and re-cranking, engine catches and holds 800 rpm idle
        final = frames[-1]
        self.assertEqual(final.state.ecu_mode, "IDLE")
        self.assertTrue(final.firing)
        self.assertTrue(final.state.fuel_pump_on)
        self.assertEqual(final.rpm, 800.0)

    def test_issue2_check_valve_leak_needs_second_crank(self) -> None:
        frames = run_scenario("issue2-leak")
        # After first short crank (t=1.0..1.6), engine stalls back to 0 rpm
        after_first = [fr for fr in frames if 1.0 <= fr.time_s <= 1.6]
        self.assertTrue(any(fr.rpm == 0.0 for fr in after_first))
        # After second crank, engine catches and holds cold fast idle
        final = frames[-1]
        self.assertTrue(final.firing)
        self.assertGreaterEqual(final.rpm, 1100.0)
        self.assertEqual(final.state.ecu_mode, "AFTER_START_WARMUP")

    def test_issue2_shorted_cts_fires_on_csv_then_stalls_in_on(self) -> None:
        frames = run_scenario("issue2-cts")
        crank_frames = [fr for fr in frames if fr.key_pos == "START"]
        self.assertTrue(any(fr.csv_active and fr.firing for fr in crank_frames))
        # Once key returns to ON, engine starves and stalls to 0 rpm
        final = frames[-1]
        self.assertEqual(final.key_pos, "ON")
        self.assertEqual(final.rpm, 0.0)
        self.assertFalse(final.firing)

    def test_dead_starter_stays_at_zero_rpm(self) -> None:
        car = Z280Car(faults=SimFaults(starter_dead=True))
        frames = car.run_phase(1.0, dt=0.2, key_pos="START")
        self.assertTrue(all(fr.rpm == 0.0 for fr in frames))
        self.assertEqual(frames[-1].state.ecu_mode, "CRANK_NO_RPM")

    def test_idle_air_leak_elevates_warm_idle_issue3(self) -> None:
        car = Z280Car(
            faults=SimFaults(idle_air_leak_rpm=200.0),
            coolant_temp_c=80.0,
        )
        car.run_phase(1.2, dt=0.2, key_pos="START")
        frames = car.run_phase(1.6, dt=0.2, key_pos="ON")
        self.assertEqual(frames[-1].rpm, 1000.0)

    def test_crank_voltage_sag_with_bad_w_splice(self) -> None:
        # Marginal W splice (0.25 Ω) holds at idle (~14.1V) but sags under START load
        car = Z280Car(faults=SimFaults(elec=Faults(w_splice_ohms=0.25)), coolant_temp_c=80.0)
        fr = car.step(dt=0.2, key_pos="START")
        self.assertLess(fr.state.nodes["FP_CTRL_95_BW"], 8.5)
        self.assertFalse(fr.state.fuel_pump_on)

    def test_monte_carlo_finds_issue5_issue2_and_combined(self) -> None:
        res = run_monte_carlo(n_samples=250, seed=42)
        self.assertGreater(res["n_issue5"], 0)
        self.assertGreater(res["n_issue2"], 0)
        self.assertGreater(res["n_both"], 0)


if __name__ == "__main__":
    unittest.main()
