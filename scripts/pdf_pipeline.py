#!/usr/bin/env python3
"""
Upgraded Knowledgebase Assembler & Global Index Compiler for 1978 Datsun 280Z.
Supports:
- Merging multi-chunk subagent JSON files for large PDFs.
- Seamless cross-page text stitching (keeps `<!-- Page X -->` anchors inline without breaking sentences/procedures).
- YAML frontmatter per Markdown file (systems covered, special tools, quick-jump section index, diagram count).
- Rich mechanic-focused diagram blocks (type, components, structured callouts, measurements/specs, wiring/connections, mechanic notes, tags).
- Global knowledgebase index compilation (`markdown/index/`):
  - `master_index.md`
  - `diagrams_catalog.md` & `diagrams_catalog.json`
  - `torque_and_specs.md` & `torque_and_specs.json`
  - `troubleshooting_matrix.md` & `troubleshooting_matrix.json`
  - `special_service_tools.md`
"""

import argparse
import glob
import json
import os
import re
from pathlib import Path
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

WORKSPACE = Path(__file__).resolve().parent.parent
MARKDOWN_ROOT = WORKSPACE / "markdown"


def crop_diagram(page_img: Image.Image, bbox_1000: list[float], out_path: Path, pad_px: int = 8) -> tuple[int, int]:
    """Crop a [ymin, xmin, ymax, xmax] (0-1000 scale) region from page_img and save to out_path."""
    w, h = page_img.size
    ymin, xmin, ymax, xmax = bbox_1000
    left = max(0, int(round((xmin / 1000.0) * w)) - pad_px)
    top = max(0, int(round((ymin / 1000.0) * h)) - pad_px)
    right = min(w, int(round((xmax / 1000.0) * w)) + pad_px)
    bottom = min(h, int(round((ymax / 1000.0) * h)) + pad_px)
    if right <= left or bottom <= top:
        left, top, right, bottom = 0, 0, w, h

    cropped = page_img.crop((left, top, right, bottom))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(out_path, format="PNG", optimize=True)
    return cropped.size


def find_page_image(page_images_dir: Path, page_prefix: str, page_num: int) -> Path:
    for pattern in [
        f"{page_prefix}-{page_num}.png",
        f"{page_prefix}-0{page_num}.png",
        f"{page_prefix}-00{page_num}.png",
    ]:
        candidate = page_images_dir / pattern
        if candidate.exists():
            return candidate
    candidates = sorted(page_images_dir.glob(f"{page_prefix}-*0*{page_num}.png"))
    for c in candidates:
        stem_num = c.stem.split("-")[-1]
        if int(stem_num) == page_num:
            return c
    raise FileNotFoundError(f"Could not find rendered image for page {page_num} in {page_images_dir}")


def format_diagram_block(diag: dict, rel_img_link: str) -> str:
    caption = diag.get("caption") or diag.get("fig_id") or diag["slug"]
    code = diag.get("code", "")
    dtype = diag.get("diagram_type", "technical_diagram")
    desc = diag.get("description", "")
    components = diag.get("components", [])
    callouts = diag.get("callouts", {})
    specs = diag.get("measurements_and_specs", [])
    wiring = diag.get("wiring_and_connections", [])
    notes = diag.get("mechanic_notes", "")
    tags = diag.get("tags", [])

    lines = [
        f"> 🖼️ **{caption}** {f'`[{code}]`' if code else ''} *(Type: `{dtype}`)*",
        ">",
        f"> ![{caption}]({rel_img_link})",
        ">",
    ]
    if desc:
        lines.append(f"> - **Summary:** {desc}")
    if components:
        lines.append(f"> - **Components:** {', '.join(components)}")
    if callouts and isinstance(callouts, dict):
        callout_items = [f"**({k})** {v}" for k, v in callouts.items()]
        lines.append(f"> - **Callouts:** {'; '.join(callout_items)}")
    elif callouts and isinstance(callouts, list):
        lines.append(f"> - **Callouts:** {'; '.join(str(c) for c in callouts)}")
    if specs:
        lines.append(f"> - **Specs / Measurements:** {'; '.join(str(s) for s in specs)}")
    if wiring:
        lines.append(f"> - **Wiring / Connections:** {'; '.join(str(w) for w in wiring)}")
    if notes:
        lines.append(f"> - **Mechanic Notes:** {notes}")
    if tags:
        tags_str = ", ".join(f"`{t}`" for t in tags)
        lines.append(f"> - **Tags:** {tags_str}")

    return "\n".join(lines)


def load_json_lenient(jp: Path):
    text = jp.read_text(encoding="utf-8", errors="replace").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fix invalid backslash escapes (e.g. LaTeX \pm, \Omega, \circ)
        fixed = re.sub(r'\\(?![/\"\\bfnrtu])', r'\\\\', text)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            dec = json.JSONDecoder()
            obj, _ = dec.raw_decode(fixed)
            return obj


def get_page_num(p: dict, fallback_idx: int = 1) -> int:
    val = p.get("page_num") or p.get("page_number") or p.get("page") or fallback_idx
    if isinstance(val, int):
        return val
    m = re.search(r"\d+", str(val))
    return int(m.group(0)) if m else fallback_idx


def merge_chunk_jsons(json_paths: list[Path]) -> dict:
    """Merge one or more chunk JSON files for a single PDF into one unified structure."""
    merged = {
        "section_title": "",
        "pages": [],
        "torques": [],
        "specs": [],
        "troubleshooting": [],
        "special_tools": [],
    }
    seen_pages = set()
    for jp in json_paths:
        data = load_json_lenient(jp)
        if isinstance(data, list):
            data = {"pages": data}
        title = data.get("section_title") or data.get("section") or data.get("title") or ""
        if not merged["section_title"] and title:
            merged["section_title"] = title
        for idx, p in enumerate(data.get("pages", []), start=1):
            pnum = get_page_num(p, idx)
            p["page_num"] = pnum
            if pnum not in seen_pages:
                seen_pages.add(pnum)
                merged["pages"].append(p)
        for key in ["torques", "specs", "troubleshooting", "special_tools"]:
            if isinstance(data.get(key), list):
                merged[key].extend(data[key])

    merged["pages"].sort(key=lambda x: x["page_num"])
    return merged


def build_markdown(
    json_paths: list[Path],
    page_images_dir: Path,
    page_prefix: str,
    md_out_path: Path,
    diagrams_dir: Path,
    rel_diagrams_dir: str,
    source_pdf_rel: str = "",
) -> dict:
    data = merge_chunk_jsons(json_paths)
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    md_out_path.parent.mkdir(parents=True, exist_ok=True)

    section_title = data.get("section_title") or md_out_path.stem
    all_diagrams_meta = []
    stitched_body_parts = []
    page_labels = []

    pages = data.get("pages", [])
    for idx, page in enumerate(pages):
        page_num = page["page_num"]
        page_label = page.get("page_label", f"Page {page_num}")
        page_labels.append(page_label)

        try:
            img_path = find_page_image(page_images_dir, page_prefix, page_num)
            page_img = Image.open(img_path)
        except FileNotFoundError:
            page_img = None
        page_md = page.get("markdown", "").strip()

        for diag in page.get("diagrams", []):
            slug = diag["slug"]
            fig_id = diag.get("fig_id", "")
            code = diag.get("code", "")
            caption = diag.get("caption", fig_id)
            bbox = diag.get("bbox_1000", [0, 0, 1000, 1000])

            diag_filename = f"{slug}.png"
            diag_out_path = diagrams_dir / diag_filename
            if diag.get("source_image_path") and Path(diag["source_image_path"]).exists():
                src_img = Image.open(diag["source_image_path"]).convert("RGBA")
                crop_w, crop_h = crop_diagram(src_img, bbox, diag_out_path, pad_px=0)
            elif page_img is not None:
                crop_w, crop_h = crop_diagram(page_img, bbox, diag_out_path)
            else:
                crop_w, crop_h = (0, 0)

            rel_img_link = f"{rel_diagrams_dir}/{diag_filename}"
            diag_block = format_diagram_block(diag, rel_img_link)

            placeholder = f"{{{{DIAGRAM:{slug}}}}}"
            if placeholder in page_md:
                page_md = page_md.replace(placeholder, "\n\n" + diag_block + "\n\n")
            else:
                page_md += "\n\n" + diag_block

            meta_entry = {
                "section_title": section_title,
                "source_md": str(md_out_path.relative_to(MARKDOWN_ROOT)) if md_out_path.is_relative_to(MARKDOWN_ROOT) else str(md_out_path),
                "slug": slug,
                "file": str(diag_out_path),
                "rel_link": rel_img_link,
                "page_num": page_num,
                "page_label": page_label,
                "fig_id": fig_id,
                "code": code,
                "caption": caption,
                "diagram_type": diag.get("diagram_type", "technical_diagram"),
                "description": diag.get("description", ""),
                "components": diag.get("components", []),
                "callouts": diag.get("callouts", {}),
                "measurements_and_specs": diag.get("measurements_and_specs", []),
                "wiring_and_connections": diag.get("wiring_and_connections", []),
                "mechanic_notes": diag.get("mechanic_notes", ""),
                "tags": diag.get("tags", []),
                "dimensions": [crop_w, crop_h],
            }
            all_diagrams_meta.append(meta_entry)

        # Seamless cross-page stitching
        prev_continues = idx > 0 and (
            pages[idx - 1].get("continues_to_next", False)
            or page.get("continues_from_prev", False)
        )
        page_marker = f"<!-- Page {page_num} ({page_label}) -->"
        if prev_continues and stitched_body_parts:
            # Join inline without breaking the sentence or list across a horizontal rule
            stitched_body_parts[-1] = stitched_body_parts[-1].rstrip() + f" {page_marker} " + page_md.lstrip()
        else:
            stitched_body_parts.append(f"{page_marker}\n{page_md}")

    body_text = "\n\n---\n\n".join(stitched_body_parts)

    # Extract headings for YAML frontmatter quick navigation
    headings = re.findall(r"^#{1,3}\s+(.+)$", body_text, flags=re.MULTILINE)
    unique_headings = []
    for h in headings:
        h_clean = h.strip()
        if h_clean not in unique_headings and h_clean != section_title:
            unique_headings.append(h_clean)

    tools_list = [f"{t.get('tool_number', '')} ({t.get('tool_name', '')})".strip() for t in data.get("special_tools", []) if t.get("tool_number")]

    # YAML Frontmatter
    frontmatter_lines = [
        "---",
        f'title: "{section_title}"',
        f'source_pdf: "{source_pdf_rel or md_out_path.stem + ".pdf"}"',
        f"pages_count: {len(pages)}",
        f"page_range: \"{page_labels[0] if page_labels else ''} to {page_labels[-1] if page_labels else ''}\"",
        f"diagrams_count: {len(all_diagrams_meta)}",
        f"torques_count: {len(data.get('torques', []))}",
        f"specs_count: {len(data.get('specs', []))}",
        f"troubleshooting_count: {len(data.get('troubleshooting', []))}",
    ]
    if tools_list:
        frontmatter_lines.append("special_service_tools:")
        for tl in tools_list:
            frontmatter_lines.append(f'  - "{tl}"')
    if unique_headings:
        frontmatter_lines.append("sections:")
        for hd in unique_headings[:25]:
            frontmatter_lines.append(f'  - "{hd.replace(chr(34), chr(39))}"')
    frontmatter_lines.append("---\n")

    md_lines = ["\n".join(frontmatter_lines), f"# {section_title}\n", body_text, "\n---\n"]

    if all_diagrams_meta:
        md_lines.append("## Segmented Diagrams Index\n")
        md_lines.append("| Figure | Code | Type | Page | Caption | Key Components / Callouts | File |")
        md_lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for d in all_diagrams_meta:
            comp_str = ", ".join(d["components"][:5]) if d["components"] else ", ".join(d["tags"][:5])
            md_lines.append(
                f"| **{d['fig_id'] or d['slug']}** | `{d['code'] or '-'}` | `{d['diagram_type']}` | {d['page_label']} | {d['caption']} | {comp_str} | [{Path(d['file']).name}]({d['rel_link']}) |"
            )
        md_lines.append("")

    with open(md_out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    section_meta = {
        "section_title": section_title,
        "source_pdf": source_pdf_rel,
        "md_path": str(md_out_path.relative_to(MARKDOWN_ROOT)) if md_out_path.is_relative_to(MARKDOWN_ROOT) else str(md_out_path),
        "pages_count": len(pages),
        "diagrams": all_diagrams_meta,
        "torques": data.get("torques", []),
        "specs": data.get("specs", []),
        "troubleshooting": data.get("troubleshooting", []),
        "special_tools": data.get("special_tools", []),
    }

    with open(diagrams_dir / "section_metadata.json", "w", encoding="utf-8") as f:
        json.dump(section_meta, f, indent=2)

    return section_meta


def compile_global_indexes(markdown_root: Path):
    """Scan all section_metadata.json files and compile master lookup indexes in markdown/index/."""
    index_dir = markdown_root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)

    meta_files = sorted(markdown_root.glob("**/section_metadata.json"))
    sections = []
    all_diagrams = []
    all_torques = []
    all_specs = []
    all_troubleshooting = []
    all_tools = []

    for mf in meta_files:
        with open(mf, "r", encoding="utf-8") as f:
            sm = json.load(f)
        sections.append(sm)
        md_rel = sm["md_path"]
        sec_title = sm["section_title"]

        for d in sm.get("diagrams", []):
            d["section_md"] = md_rel
            all_diagrams.append(d)
        for t in sm.get("torques", []):
            t["section_title"] = sec_title
            t["section_md"] = md_rel
            all_torques.append(t)
        for s in sm.get("specs", []):
            s["section_title"] = sec_title
            s["section_md"] = md_rel
            all_specs.append(s)
        for tr in sm.get("troubleshooting", []):
            tr["section_title"] = sec_title
            tr["section_md"] = md_rel
            all_troubleshooting.append(tr)
        for tl in sm.get("special_tools", []):
            tl["section_title"] = sec_title
            tl["section_md"] = md_rel
            all_tools.append(tl)

    # 1. Master Index
    master_lines = [
        "# 1978 Datsun 280Z Factory Service Manual & EFI Knowledgebase",
        "",
        "## Global Quick-Reference Indexes",
        "- [All Tightening Torques & Service Specifications](torque_and_specs.md)",
        "- [Master Troubleshooting & Symptom Matrix](troubleshooting_matrix.md)",
        "- [Master Segmented Diagrams & Schematics Catalog](diagrams_catalog.md)",
        "- [Special Service Tools Index](special_service_tools.md)",
        "",
        "## Manual Sections",
        "| Section | Source PDF | Pages | Diagrams | Torques | Specs | Troubleshooting | Markdown Link |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |",
    ]
    for s in sections:
        md_link = f"../{s['md_path']}"
        master_lines.append(
            f"| **{s['section_title']}** | `{s.get('source_pdf', '')}` | {s['pages_count']} | {len(s.get('diagrams', []))} | {len(s.get('torques', []))} | {len(s.get('specs', []))} | {len(s.get('troubleshooting', []))} | [{Path(s['md_path']).name}]({md_link}) |"
        )
    (index_dir / "master_index.md").write_text("\n".join(master_lines) + "\n", encoding="utf-8")

    # 2. Torques & Specs
    ts_lines = [
        "# Global Tightening Torques & Service Specifications (1978 Datsun 280Z)",
        "",
        "## 1. Tightening Torques",
        "| Section | Component / Fastener | Unit | Torque Value | Page | Source |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for t in all_torques:
        ts_lines.append(
            f"| {t['section_title']} | **{t.get('component', '')}** | `{t.get('unit', 'kg-m (ft-lb)')}` | **{t.get('value', '')}** | {t.get('page_label', '')} | [{Path(t['section_md']).stem}](../{t['section_md']}) |"
        )
    ts_lines.extend([
        "",
        "## 2. Service Data & Clearance Specifications",
        "| Section | System / Assembly | Item | Unit | Standard | Wear Limit / Note | Page |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ])
    for s in all_specs:
        ts_lines.append(
            f"| [{Path(s['section_md']).stem}](../{s['section_md']}) | {s.get('system', '')} | **{s.get('item', '')}** | `{s.get('unit', '')}` | {s.get('standard', '')} | {s.get('limit', '')} | {s.get('page_label', '')} |"
        )
    (index_dir / "torque_and_specs.md").write_text("\n".join(ts_lines) + "\n", encoding="utf-8")
    (index_dir / "torque_and_specs.json").write_text(
        json.dumps({"torques": all_torques, "specs": all_specs}, indent=2), encoding="utf-8"
    )

    # 3. Troubleshooting Matrix
    tr_lines = [
        "# Master Troubleshooting & Symptom Matrix (1978 Datsun 280Z)",
        "",
        "| Section | Symptom / Condition | Probable Cause | Corrective Action / Test | Page |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for tr in all_troubleshooting:
        tr_lines.append(
            f"| [{Path(tr['section_md']).stem}](../{tr['section_md']}) | **{tr.get('symptom', '')}** | {tr.get('probable_cause', '')} | {tr.get('corrective_action', '')} | {tr.get('page_label', '')} |"
        )
    (index_dir / "troubleshooting_matrix.md").write_text("\n".join(tr_lines) + "\n", encoding="utf-8")
    (index_dir / "troubleshooting_matrix.json").write_text(json.dumps(all_troubleshooting, indent=2), encoding="utf-8")

    # 4. Diagrams Catalog
    dc_lines = [
        "# Master Segmented Diagrams & Schematics Catalog",
        "",
        "| Section | Figure | Code | Type | Page | Caption | Components & Callouts | Specs / Notes |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for d in all_diagrams:
        comps = ", ".join(d.get("components", [])[:6])
        callouts_dict = d.get("callouts", {})
        callouts_str = "; ".join(f"({k}) {v}" for k, v in callouts_dict.items()) if isinstance(callouts_dict, dict) else ""
        comp_col = " — ".join(filter(None, [comps, callouts_str]))
        spec_col = "; ".join(d.get("measurements_and_specs", [])[:3])
        if d.get("mechanic_notes"):
            spec_col = (spec_col + " | " + d["mechanic_notes"]).strip(" |")
        dc_lines.append(
            f"| [{Path(d['section_md']).stem}](../{d['section_md']}) | **{d.get('fig_id') or d['slug']}** | `{d.get('code') or '-'}` | `{d.get('diagram_type', '')}` | {d.get('page_label', '')} | {d.get('caption', '')} | {comp_col} | {spec_col} |"
        )
    (index_dir / "diagrams_catalog.md").write_text("\n".join(dc_lines) + "\n", encoding="utf-8")
    (index_dir / "diagrams_catalog.json").write_text(json.dumps(all_diagrams, indent=2), encoding="utf-8")

    # 5. Special Service Tools
    st_lines = [
        "# Special Service Tools Index (Nissan ST & Kent-Moore J Numbers)",
        "",
        "| Section | Nissan Tool No. | Tool Name | Kent-Moore No. | Ref / Figure | Page |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for tl in all_tools:
        st_lines.append(
            f"| [{Path(tl['section_md']).stem}](../{tl['section_md']}) | **{tl.get('tool_number', '')}** | {tl.get('tool_name', '')} | `{tl.get('kent_moore_no', '')}` | {tl.get('ref', '')} | {tl.get('page_label', '')} |"
        )
    (index_dir / "special_service_tools.md").write_text("\n".join(st_lines) + "\n", encoding="utf-8")

    print(
        f"Compiled Global Indexes in {index_dir}: "
        f"{len(sections)} sections, {len(all_diagrams)} diagrams, "
        f"{len(all_torques)} torques, {len(all_specs)} specs, "
        f"{len(all_troubleshooting)} troubleshooting entries."
    )


def main():
    parser = argparse.ArgumentParser(description="Assemble KB Markdown & Global Indexes")
    parser.add_argument("--compile-global-only", action="store_true", help="Only recompile global indexes")
    parser.add_argument("--json", nargs="+", help="One or more extracted JSON files for this PDF")
    parser.add_argument("--images-dir", help="Directory with rendered page PNGs")
    parser.add_argument("--image-prefix", default="page", help="Filename prefix for rendered PNGs")
    parser.add_argument("--md-out", help="Output Markdown file path")
    parser.add_argument("--diagrams-dir", help="Output directory for cropped diagram PNGs")
    parser.add_argument("--rel-diagrams-dir", help="Relative path from Markdown file to diagrams dir")
    parser.add_argument("--source-pdf", default="", help="Relative path to original PDF")
    args = parser.parse_args()

    if not args.compile_global_only:
        res = build_markdown(
            [Path(j) for j in args.json],
            Path(args.images_dir),
            args.image_prefix,
            Path(args.md_out),
            Path(args.diagrams_dir),
            args.rel_diagrams_dir,
            args.source_pdf,
        )
        print(f"Assembled {res['md_path']} ({len(res['diagrams'])} diagrams, {len(res['torques'])} torques, {len(res['specs'])} specs).")

    compile_global_indexes(MARKDOWN_ROOT)


if __name__ == "__main__":
    main()
