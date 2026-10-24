#!/usr/bin/env python3
"""
Batch assembler: checks scratch/extracted/
for completed JSON files and builds the Markdown + Segmented Diagrams + Global Indexes for all ready sections.
"""

from pathlib import Path
from pdf_pipeline import build_markdown, compile_global_indexes, MARKDOWN_ROOT

EXTRACTED_DIR = Path(__file__).resolve().parent.parent / "scratch" / "extracted"
RENDERED_DIR = Path(__file__).resolve().parent.parent / "scratch" / "rendered"

SECTIONS_CONFIG = [
    # Single-JSON FSM sections
    ("covers_and_forward", ["covers_and_forward.json"], "1978 280z FSM/Covers and forward.md", "1978 280z FSM/diagrams/Covers and forward", "diagrams/Covers and forward", "1978 280z FSM/Covers and forward.pdf"),
    ("gi_general_information", ["gi_general_information.json"], "1978 280z FSM/GI General Information.md", "1978 280z FSM/diagrams/GI General Information", "diagrams/GI General Information", "1978 280z FSM/GI General Information.pdf"),
    ("er_engine_removal", ["er_engine_removal.json"], "1978 280z FSM/ER Engine removal.md", "1978 280z FSM/diagrams/ER Engine removal", "diagrams/ER Engine removal", "1978 280z FSM/ER Engine removal.pdf"),
    ("co_cooling_system", ["co_cooling_system.json"], "1978 280z FSM/CO Cooling System.md", "1978 280z FSM/diagrams/CO Cooling System", "diagrams/CO Cooling System", "1978 280z FSM/CO Cooling System.pdf"),
    ("wt_wheel_and_tire", ["wt_wheel_and_tire.json"], "1978 280z FSM/WT Wheel and Tire.md", "1978 280z FSM/diagrams/WT Wheel and Tire", "diagrams/WT Wheel and Tire", "1978 280z FSM/WT Wheel and Tire.pdf"),
    ("cl_clutch", ["cl_clutch.json"], "1978 280z FSM/CL Clutch.md", "1978 280z FSM/diagrams/CL Clutch", "diagrams/CL Clutch", "1978 280z FSM/CL Clutch.pdf"),
    ("fe_engine_control_fuel_and_exhaust", ["fe_engine_control_fuel_and_exhaust.json"], "1978 280z FSM/FE Engine Control fuel and Exhaust.md", "1978 280z FSM/diagrams/FE Engine Control fuel and Exhaust", "diagrams/FE Engine Control fuel and Exhaust", "1978 280z FSM/FE Engine Control fuel and Exhaust.pdf"),
    ("ra_rear_axle_and_suspension", ["ra_rear_axle_and_suspension.json"], "1978 280z FSM/RA Rear axle and Suspension.md", "1978 280z FSM/diagrams/RA Rear axle and Suspension", "diagrams/RA Rear axle and Suspension", "1978 280z FSM/RA Rear axle and Suspension.pdf"),
    ("st_steering", ["st_steering.json"], "1978 280z FSM/ST Steering.md", "1978 280z FSM/diagrams/ST Steering", "diagrams/ST Steering", "1978 280z FSM/ST Steering.pdf"),
    ("fa_front_axle_and_suspension", ["fa_front_axle_and_suspension.json"], "1978 280z FSM/FA Front Axle and Suspension.md", "1978 280z FSM/diagrams/FA Front Axle and Suspension", "diagrams/FA Front Axle and Suspension", "1978 280z FSM/FA Front Axle and Suspension.pdf"),
    ("wiring_diagram", ["wiring_diagram.json"], "1978 280z FSM/Wiring Diagram.md", "1978 280z FSM/diagrams/Wiring Diagram", "diagrams/Wiring Diagram", "1978 280z FSM/Wiring Diagram.pdf"),
    ("et_engine_tune_up", ["et_engine_tune_up.json"], "1978 280z FSM/ET Engine Tune-up.md", "1978 280z FSM/diagrams/ET Engine Tune-up", "diagrams/ET Engine Tune-up", "1978 280z FSM/ET Engine Tune-up.pdf"),
    ("mt_manual_transmission", ["mt_manual_transmission.json"], "1978 280z FSM/MT Manual Transmission.md", "1978 280z FSM/diagrams/MT Manual Transmission", "diagrams/MT Manual Transmission", "1978 280z FSM/MT Manual Transmission.pdf"),
    ("br_brake_system", ["br_brake_system.json"], "1978 280z FSM/BR Brake System.md", "1978 280z FSM/diagrams/BR Brake System", "diagrams/BR Brake System", "1978 280z FSM/BR Brake System.pdf"),
    ("ec_emission_control_system", ["ec_emission_control_system.json"], "1978 280z FSM/EC Emission Control System.md", "1978 280z FSM/diagrams/EC Emission Control System", "diagrams/EC Emission Control System", "1978 280z FSM/EC Emission Control System.pdf"),
    ("pd_propeller_shaft_and_differential", ["pd_propeller_shaft_and_differential.json"], "1978 280z FSM/PD Propeller Shaft and Differential.md", "1978 280z FSM/diagrams/PD Propeller Shaft and Differential", "diagrams/PD Propeller Shaft and Differential", "1978 280z FSM/PD Propeller Shaft and Differential.pdf"),
    ("bf_body", ["bf_body.json"], "1978 280z FSM/BF Body.md", "1978 280z FSM/diagrams/BF Body", "diagrams/BF Body", "1978 280z FSM/BF Body.pdf"),
    # Multi-chunk FSM sections
    ("em_engine_mechanical", ["em_engine_mechanical_part1.json", "em_engine_mechanical_part2.json"], "1978 280z FSM/EM Engine Mechanical.md", "1978 280z FSM/diagrams/EM Engine Mechanical", "diagrams/EM Engine Mechanical", "1978 280z FSM/EM Engine Mechanical.pdf"),
    ("ee_engine_electrical", ["ee_engine_electrical_part1.json", "ee_engine_electrical_part2.json"], "1978 280z FSM/EE Engine Electrical.md", "1978 280z FSM/diagrams/EE Engine Electrical", "diagrams/EE Engine Electrical", "1978 280z FSM/EE Engine Electrical.pdf"),
    ("ef_engine_fuel", ["ef_engine_fuel_part1.json", "ef_engine_fuel_part2.json"], "1978 280z FSM/EF Engine Fuel.md", "1978 280z FSM/diagrams/EF Engine Fuel", "diagrams/EF Engine Fuel", "1978 280z FSM/EF Engine Fuel.pdf"),
    ("at_automatic_transmission", ["at_automatic_transmission_part1.json", "at_automatic_transmission_part2.json", "at_automatic_transmission_part3.json"], "1978 280z FSM/AT Automatic Transmission.md", "1978 280z FSM/diagrams/AT Automatic Transmission", "diagrams/AT Automatic Transmission", "1978 280z FSM/AT Automatic Transmission.pdf"),
    ("ac_air_conditioner", ["ac_air_conditioner_part1.json", "ac_air_conditioner_part2.json", "ac_air_conditioner_part3.json"], "1978 280z FSM/AC Air Conditioner.md", "1978 280z FSM/diagrams/AC Air Conditioner", "diagrams/AC Air Conditioner", "1978 280z FSM/AC Air Conditioner.pdf"),
    ("be_body_electrical", ["be_body_electrical_part1.json", "be_body_electrical_part2.json", "be_body_electrical_part3.json", "be_body_electrical_part4.json"], "1978 280z FSM/BE Body Electrical.md", "1978 280z FSM/diagrams/BE Body Electrical", "diagrams/BE Body Electrical", "1978 280z FSM/BE Body Electrical.pdf"),
    # Root level manuals & guides
    ("1978_color_wiring_diagram", ["1978_color_wiring_diagram.json"], "1978 Color Wiring Diagram.md", "diagrams/1978 Color Wiring Diagram", "diagrams/1978 Color Wiring Diagram", "1978 Color Wiring Diagram.pdf"),
    ("280zfuelinjectionbook", ["280zfuelinjectionbook_part1.json", "280zfuelinjectionbook_part2.json", "280zfuelinjectionbook_part3.json"], "280zfuelinjectionbook.md", "diagrams/280zfuelinjectionbook", "diagrams/280zfuelinjectionbook", "280zfuelinjectionbook.pdf"),
    ("afm_tuning_and_rebuild", ["afm_tuning_and_rebuild.json"], "afm tuning and rebuild.md", "diagrams/afm tuning and rebuild", "diagrams/afm tuning and rebuild", "afm tuning and rebuild.html"),
]


def main():
    built = 0
    pending = []
    for render_slug, json_files, md_rel, diag_rel, rel_link, src_pdf in SECTIONS_CONFIG:
        json_paths = [EXTRACTED_DIR / jf for jf in json_files]
        if all(jp.exists() for jp in json_paths):
            md_out = MARKDOWN_ROOT / md_rel
            diag_dir = MARKDOWN_ROOT / diag_rel
            img_dir = RENDERED_DIR / render_slug
            try:
                res = build_markdown(
                    json_paths=json_paths,
                    page_images_dir=img_dir,
                    page_prefix="page",
                    md_out_path=md_out,
                    diagrams_dir=diag_dir,
                    rel_diagrams_dir=rel_link,
                    source_pdf_rel=src_pdf,
                )
                built += 1
                print(f"[OK] {md_rel} ({len(res['diagrams'])} diagrams)")
            except Exception as e:
                print(f"[ERR] {md_rel}: {e}")
        else:
            missing = [jf for jf, jp in zip(json_files, json_paths) if not jp.exists()]
            pending.append((md_rel, missing))

    compile_global_indexes(MARKDOWN_ROOT)
    print(f"\nSummary: Built {built} + 1 (EL) = {built + 1}/27 sections. Pending: {len(pending)}")
    for m_rel, miss in pending:
        print(f"  - Waiting on {m_rel}: {miss}")


if __name__ == "__main__":
    main()
