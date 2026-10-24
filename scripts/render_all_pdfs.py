#!/usr/bin/env python3
"""
Parallelized PDF renderer using all CPU cores (ProcessPoolExecutor + page-range chunking).
"""

import concurrent.futures
import json
import os
import re
import subprocess
from pathlib import Path
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

WORKSPACE = Path(__file__).resolve().parent.parent
SCRATCH_RENDER = WORKSPACE / "scratch" / "rendered"


def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()


def get_pdf_pages(pdf_path: Path) -> int:
    info = subprocess.check_output(["pdfinfo", str(pdf_path)], text=True)
    for line in info.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":")[1].strip())
    return 1


def render_page_range(args: tuple):
    pdf_path, out_dir, dpi, first_page, last_page = args
    subprocess.run(
        [
            "pdftoppm",
            "-png",
            "-r",
            str(dpi),
            "-f",
            str(first_page),
            "-l",
            str(last_page),
            str(pdf_path),
            str(out_dir / "page"),
        ],
        check=True,
    )


def process_color_wiring_diagram(pdf_path: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    existing_zones = [out_dir / f"page-{i}.png" for i in range(6)]
    if all(p.exists() for p in existing_zones):
        return {
            "pdf_path": str(pdf_path.relative_to(WORKSPACE)),
            "slug": "1978_color_wiring_diagram",
            "render_dir": str(out_dir),
            "page_count": 6,
            "pages": [str(p) for p in existing_zones],
            "type": "color_wiring_zones",
        }

    raw_prefix = out_dir / "extracted"
    subprocess.run(["pdfimages", "-png", str(pdf_path), str(raw_prefix)], check=True)
    extracted_files = sorted(out_dir.glob("extracted-*.png"))
    if not extracted_files:
        raise RuntimeError("Failed to extract image from 1978 Color Wiring Diagram.pdf")

    full_img_path = extracted_files[0]
    img = Image.open(full_img_path)
    w, h = img.size

    overview_w = 2400
    overview_h = int(h * (overview_w / w))
    overview_path = out_dir / "page-0.png"
    img.resize((overview_w, overview_h), Image.Resampling.LANCZOS).save(overview_path)

    zones = [
        ("zone_1_front_left_engine_ignition", 0.0, 0.25),
        ("zone_2_engine_ecu_relays", 0.18, 0.43),
        ("zone_3_center_dash_switches_meters", 0.36, 0.62),
        ("zone_4_cabin_harnesses_fuses", 0.55, 0.80),
        ("zone_5_rear_lighting_defogger_fuel", 0.74, 1.0),
    ]
    zone_files = [str(overview_path)]
    for idx, (zname, x0, x1) in enumerate(zones, start=1):
        crop = img.crop((int(x0 * w), 0, int(x1 * w), h))
        zpath = out_dir / f"page-{idx}.png"
        crop.save(zpath)
        zone_files.append(str(zpath))

    diagrams_target = WORKSPACE / "markdown" / "diagrams" / "1978 Color Wiring Diagram"
    diagrams_target.mkdir(parents=True, exist_ok=True)
    full_copy = diagrams_target / "full_1978_color_wiring_diagram.png"
    if not full_copy.exists():
        os.replace(full_img_path, full_copy)
    else:
        full_img_path.unlink(missing_ok=True)

    return {
        "pdf_path": str(pdf_path.relative_to(WORKSPACE)),
        "slug": "1978_color_wiring_diagram",
        "render_dir": str(out_dir),
        "page_count": len(zone_files),
        "pages": zone_files,
        "type": "color_wiring_zones",
    }


def main():
    SCRATCH_RENDER.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(WORKSPACE.glob("**/*.pdf"))
    cpu_count = os.cpu_count() or 8
    print(f"Parallel rendering across {cpu_count} CPU threads...")

    tasks = []
    pdf_meta = []

    for pdf_path in pdfs:
        rel_path = pdf_path.relative_to(WORKSPACE)
        slug = slugify(rel_path.stem)
        out_dir = SCRATCH_RENDER / slug
        out_dir.mkdir(parents=True, exist_ok=True)

        if pdf_path.name == "1978 Color Wiring Diagram.pdf":
            continue

        total_pages = get_pdf_pages(pdf_path)
        existing = sorted(out_dir.glob("page-*.png"))
        dpi = 250 if "Wiring Diagram" in pdf_path.name else 150

        if len(existing) != total_pages:
            # Clean any partial page renders and split into 4-page parallel jobs
            for p in existing:
                p.unlink(missing_ok=True)
            chunk = 4
            for start_p in range(1, total_pages + 1, chunk):
                end_p = min(total_pages, start_p + chunk - 1)
                tasks.append((pdf_path, out_dir, dpi, start_p, end_p))

        pdf_meta.append((pdf_path, rel_path, slug, out_dir, total_pages))

    print(f"Dispatching {len(tasks)} page-chunk render jobs across {cpu_count} workers...")
    with concurrent.futures.ProcessPoolExecutor(max_workers=cpu_count) as ex:
        list(ex.map(render_page_range, tasks))

    manifest = []
    for pdf_path in pdfs:
        rel_path = pdf_path.relative_to(WORKSPACE)
        slug = slugify(rel_path.stem)
        out_dir = SCRATCH_RENDER / slug
        if pdf_path.name == "1978 Color Wiring Diagram.pdf":
            manifest.append(process_color_wiring_diagram(pdf_path, out_dir))
        else:
            existing = sorted(out_dir.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[1]))
            manifest.append({
                "pdf_path": str(rel_path),
                "slug": slug,
                "render_dir": str(out_dir),
                "page_count": len(existing),
                "pages": [str(p) for p in existing],
                "type": "standard",
            })

    manifest_path = SCRATCH_RENDER / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Done! Rendered {len(manifest)} PDFs ({sum(m['page_count'] for m in manifest)} total page images).")


if __name__ == "__main__":
    main()
