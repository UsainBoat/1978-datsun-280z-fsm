"""Unit tests for the 1978 Datsun 280Z Diagnostic Digital Twin."""

import unittest

from twin.z280_twin import (
    CONNECTOR_CATALOG,
    CarInputs,
    ConnectorFault,
    Faults,
    cts_resistance_ohms,
    cts_temp_c_from_ohms,
    parse_connector_fault_arg,
    run_sequence,
    simulate,
)


class TestZ280Twin(unittest.TestCase):
    def test_cts_curve_and_inverse(self) -> None:
        self.assertEqual(cts_resistance_ohms(10.0), 3700.0)
        self.assertEqual(cts_resistance_ohms(20.0), 2500.0)
        self.assertEqual(cts_resistance_ohms(80.0), 325.0)
        self.assertAlmostEqual(cts_temp_c_from_ohms(3700.0), 10.0, places=1)
        self.assertAlmostEqual(cts_temp_c_from_ohms(2500.0), 20.0, places=1)
        self.assertAlmostEqual(cts_temp_c_from_ohms(325.0), 80.0, places=1)

    def test_cts_offset_shifts_ecu_temp_and_enrichment(self) -> None:
        # Warm 80°C engine (~325 Ω) with +3500 Ω corroded connector looks like ~9°C to ECU
        inp = CarInputs(key_pos="ON", rpm=800, coolant_temp_c=80.0, oil_pressure_psi=35.0)
        healthy = simulate(inp, Faults())
        corroded = simulate(inp, Faults(cts_offset_ohms=3500.0))

        self.assertEqual(healthy.ecu_mode, "IDLE")
        self.assertEqual(healthy.ecu_enrichment_factor, 1.0)
        self.assertEqual(corroded.ecu_mode, "AFTER_START_WARMUP")
        self.assertLess(corroded.ecu_coolant_c, 15.0)
        self.assertGreater(corroded.ecu_enrichment_factor, 1.0)

    def test_fuel_pump_threshold_consistency(self) -> None:
        # Between 8.5V and 8.99V, fuel_pump_on and ecu_mode must agree
        inp = CarInputs(key_pos="START", rpm=200, battery_v=8.7, coolant_temp_c=75.0, oil_pressure_psi=10.0)
        st = simulate(inp, Faults())
        self.assertFalse(st.fuel_pump_on)
        self.assertIn(st.ecu_mode, ("OFF", "STALLED_NO_FUEL"))

    def test_w_splice_drops_all_fusible_links_and_fuel_pump(self) -> None:
        inp = CarInputs(key_pos="ON", rpm=800, coolant_temp_c=80.0, oil_pressure_psi=35.0)
        st = simulate(inp, Faults(w_splice_ohms=0.2))
        self.assertLess(st.nodes["FUSIBLE_EFI_72R"], st.nodes["BATTERY_+"])
        self.assertEqual(st.nodes["FUSIBLE_EFI_72R"], st.nodes["FUSIBLE_MAIN_BR_W"])
        self.assertEqual(st.nodes["FUSIBLE_EFI_72R"], st.nodes["FUSIBLE_MAIN_BK_WR"])
        self.assertEqual(st.nodes["FUEL_PUMP_+"], st.nodes["FUSIBLE_EFI_72R"])

    def test_fp_control_relay_alt_l_open_vs_short(self) -> None:
        running = CarInputs(key_pos="ON", rpm=2000, throttle_deg=15.0, coolant_temp_c=80.0, oil_pressure_psi=40.0)
        # Open WB wire stops grounding pin 96; with oil pressure up, pump stays on (battery V only)
        st_open = simulate(running, Faults(alt_l_open=True))
        self.assertTrue(st_open.fuel_pump_on)
        self.assertEqual(st_open.nodes["BATTERY_+"], running.battery_v)

        # Shorted-to-ground WB wire holds pin 96 grounded and trips safety shutoff
        st_short = simulate(running, Faults(alt_l_short_gnd=True))
        self.assertFalse(st_short.fuel_pump_on)
        self.assertEqual(st_short.ecu_mode, "STALLED_NO_FUEL")

    def test_fp_ctrl_94_open_blocks_crank_fuel_pump(self) -> None:
        crank = CarInputs(key_pos="START", rpm=200, coolant_temp_c=10.0, oil_pressure_psi=2.0)
        st = simulate(crank, Faults(fp_ctrl_94_open=True))
        self.assertEqual(st.nodes["FP_CTRL_94_BY"], 0.0)
        self.assertFalse(st.fuel_pump_on)
        self.assertEqual(st.ecu_mode, "STALLED_NO_FUEL")

    def test_hei_ground_drop_does_not_drop_coil_plus(self) -> None:
        inp = CarInputs(key_pos="ON", rpm=800, coolant_temp_c=80.0, oil_pressure_psi=35.0)
        st = simulate(inp, Faults(hei_ground_ohms=1.5))
        self.assertEqual(st.nodes["COIL_+_BW"], st.nodes["IGN_RELAY_OUT_BW"])
        self.assertEqual(st.nodes["HEI_GND_DROP"], 6.0)
        self.assertFalse(st.spark_available)
        self.assertEqual(st.ecu_mode, "STALLED_NO_SPARK")

    def test_ecu_pin_1_coil_minus_resting_high_at_zero_rpm(self) -> None:
        inp = CarInputs(key_pos="ON", rpm=0, coolant_temp_c=80.0, oil_pressure_psi=0.0)
        st = simulate(inp, Faults())
        self.assertEqual(st.ecu_mode, "KEY_ON_READY")
        self.assertEqual(st.nodes["ECU_PIN_1_RPM"], st.nodes["COIL_+_BW"])

    def test_start_with_zero_rpm_reports_crank_no_rpm(self) -> None:
        inp = CarInputs(key_pos="START", rpm=0, coolant_temp_c=80.0, oil_pressure_psi=0.0)
        st = simulate(inp, Faults())
        self.assertEqual(st.ecu_mode, "CRANK_NO_RPM")

    def test_headlamp_fuses_4_and_5_independent_in_factory_mode(self) -> None:
        inp = CarInputs(key_pos="ON", rpm=800, light_switch="HEAD", dimmer="LOW", oil_pressure_psi=35.0)
        st = simulate(inp, Faults(blown_fuses={4}, headlamp_relayed=False))
        self.assertEqual(st.nodes["HEADLAMP_RH_LOW"], 0.0)
        self.assertGreater(st.nodes["HEADLAMP_LH_LOW"], 10.0)

    def test_wiper_switch_models_civic_wiper_motor(self) -> None:
        inp_on = CarInputs(key_pos="ON", rpm=800, wiper_switch="HIGH", oil_pressure_psi=35.0)
        st_on = simulate(inp_on, Faults())
        self.assertEqual(st_on.wiper_mode, "HIGH")
        self.assertGreater(st_on.nodes["WIPER_MOTOR_RUN"], 12.0)

        st_blown = simulate(inp_on, Faults(blown_fuses={7}))
        self.assertEqual(st_blown.wiper_mode, "OFF")
        self.assertEqual(st_blown.nodes["WIPER_MOTOR_RUN"], 0.0)

    def test_part_throttle_warmup_enrichment(self) -> None:
        cold_cruise = CarInputs(key_pos="ON", rpm=2500, throttle_deg=15.0, coolant_temp_c=30.0, air_temp_c=25.0, oil_pressure_psi=40.0)
        warm_cruise = CarInputs(key_pos="ON", rpm=2500, throttle_deg=15.0, coolant_temp_c=80.0, air_temp_c=25.0, oil_pressure_psi=40.0)
        self.assertEqual(simulate(cold_cruise).ecu_enrichment_factor, 1.15)
        self.assertEqual(simulate(warm_cruise).ecu_enrichment_factor, 1.0)

    def test_tps_stuck_closed_masks_wot_and_conflicting_faults_raise(self) -> None:
        inp = CarInputs(key_pos="ON", rpm=2500, throttle_deg=45.0, coolant_temp_c=80.0, oil_pressure_psi=40.0)
        st = simulate(inp, Faults(tps_idle_stuck_closed=True))
        self.assertTrue(st.tps_idle_closed)
        self.assertFalse(st.tps_wot_closed)

        with self.assertRaises(ValueError):
            simulate(inp, Faults(cts_open=True, cts_short=True))
        with self.assertRaises(ValueError):
            simulate(inp, Faults(tps_idle_stuck_closed=True, tps_idle_open=True))
        with self.assertRaises(ValueError):
            simulate(inp, Faults(alt_l_open=True, alt_l_short_gnd=True))

    def test_run_sequence_does_not_mutate_inputs_and_issue5_lockout(self) -> None:
        steps = [
            ("1. Crank", CarInputs(key_pos="START", rpm=220, coolant_temp_c=80, oil_pressure_psi=10)),
            ("2. Cruise", CarInputs(key_pos="ON", rpm=3500, throttle_deg=18, coolant_temp_c=80, oil_pressure_psi=45)),
            ("3. Decel", CarInputs(key_pos="ON", rpm=3400, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=40)),
            ("4. Dip", CarInputs(key_pos="ON", rpm=400, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=4)),
            ("5. Re-crank", CarInputs(key_pos="START", rpm=220, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=10)),
            ("6. Key OFF", CarInputs(key_pos="OFF", rpm=0, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=0)),
            ("7. Restart", CarInputs(key_pos="START", rpm=220, throttle_deg=0, coolant_temp_c=80, oil_pressure_psi=10)),
        ]
        results = run_sequence(steps, Faults(fp_control_relay_lockout_fault=True))
        self.assertEqual(results[2].ecu_mode, "DECEL_FUEL_CUT")
        self.assertEqual(results[3].ecu_mode, "STALLED_NO_FUEL")
        self.assertTrue(results[3].fp_lockout_tripped)
        self.assertFalse(results[4].fuel_pump_on)
        self.assertFalse(results[5].fp_lockout_tripped)
        self.assertTrue(results[6].fuel_pump_on)
        # Verify caller's CarInputs objects were not mutated
        for _, inp in steps:
            self.assertFalse(inp.prev_fuel_cut)
            self.assertFalse(inp.prev_fp_latched)
            self.assertFalse(inp.prev_fp_lockout_tripped)

    def test_connector_fault_c7_c8_rear_kills_pump_but_keeps_air_regulator(self) -> None:
        inp = CarInputs(key_pos="ON", rpm=800, coolant_temp_c=80.0, oil_pressure_psi=35.0)
        st = simulate(inp, Faults(connector_faults={"C7_C8_REAR:6": ConnectorFault(open=True)}))
        self.assertGreater(st.nodes["FP_RELAY_OUT_GY"], 12.0)
        self.assertGreater(st.nodes["AIR_REGULATOR_34"], 12.0)
        self.assertEqual(st.nodes["FUEL_PUMP_+"], 0.0)
        self.assertFalse(st.fuel_pump_on)
        self.assertEqual(st.ecu_mode, "STALLED_NO_FUEL")

    def test_connector_fault_fuel_pump_ground_corrosion_starves_pump(self) -> None:
        inp = CarInputs(key_pos="ON", rpm=800, coolant_temp_c=80.0, oil_pressure_psi=35.0)
        # 1.2 Ω on 5A fuel pump ground drops 6.0V (14.1V -> 8.1V < 9.0V threshold)
        st = simulate(inp, Faults(connector_faults={"FUEL_PUMP:GND": ConnectorFault(ohms=1.2)}))
        self.assertAlmostEqual(st.nodes["FUEL_PUMP_+"], 8.1, places=1)
        self.assertFalse(st.fuel_pump_on)
        self.assertEqual(st.ecu_mode, "STALLED_NO_FUEL")

    def test_connector_fault_ecu_pin1_and_ecu_pin5_and_tps_pin18(self) -> None:
        inp = CarInputs(key_pos="ON", rpm=800, coolant_temp_c=80.0, oil_pressure_psi=35.0)
        st_rpm = simulate(inp, Faults(connector_faults={"ECU:1": ConnectorFault(open=True)}))
        self.assertTrue(st_rpm.spark_available)
        self.assertEqual(st_rpm.ecu_mode, "STALLED_NO_RPM_SIG")

        st_gnd = simulate(inp, Faults(connector_faults={"ECU:5": ConnectorFault(open=True)}))
        self.assertEqual(st_gnd.ecu_mode, "OFF")

        st_tps = simulate(inp, Faults(connector_faults={"TPS:18": ConnectorFault(open=True)}))
        self.assertFalse(st_tps.tps_idle_closed)
        self.assertFalse(st_tps.tps_wot_closed)

    def test_connector_fault_dropping_resistor_and_c1_tach_short(self) -> None:
        inp = CarInputs(key_pos="ON", rpm=800, coolant_temp_c=80.0, oil_pressure_psi=35.0)
        st_drop = simulate(inp, Faults(connector_faults={"DROPPING_RES_1:1": ConnectorFault(open=True)}))
        self.assertEqual(st_drop.nodes["DROPPING_RES_43_2"], 0.0)
        self.assertGreater(st_drop.nodes["DROPPING_RES_2_L"], 12.0)
        self.assertTrue(any("1-4" in n for n in st_drop.notes))

        st_tach = simulate(inp, Faults(connector_faults={"C1_ENG_DASH:5": ConnectorFault(short_gnd=True)}))
        self.assertFalse(st_tach.spark_available)
        self.assertEqual(st_tach.ecu_mode, "STALLED_NO_SPARK")

    def test_parse_connector_fault_arg_and_validation(self) -> None:
        cid, cf = parse_connector_fault_arg("C9_C10_IGN_PWR:3=0.35ohm")
        self.assertEqual(cid, "C9_C10_IGN_PWR:3")
        self.assertAlmostEqual(cf.ohms, 0.35)
        self.assertEqual(parse_connector_fault_arg("C2_ALT_CHG:3=short_gnd")[1], ConnectorFault(short_gnd=True))
        self.assertEqual(parse_connector_fault_arg("ECU:5=open")[1], ConnectorFault(open=True))
        self.assertGreaterEqual(len(CONNECTOR_CATALOG), 35)

        with self.assertRaises(ValueError):
            parse_connector_fault_arg("NOT_A_REAL_CONN=open")
        with self.assertRaises(ValueError):
            ConnectorFault(open=True, short_gnd=True)
        with self.assertRaises(ValueError):
            simulate(CarInputs(), Faults(connector_faults={"BOGUS:1": ConnectorFault(open=True)}))


if __name__ == "__main__":
    unittest.main()
