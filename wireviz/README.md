# 1978 Datsun 280Z — WireViz Wiring Diagrams

Modular [WireViz](https://github.com/wireviz/WireViz) (`0.4.1`) harness definitions transcribed from `1978 Color Wiring Diagram.pdf` and `markdown/1978 Color Wiring Diagram.md`.

## Files

| Zone | File | Coverage |
|---|---|---|
| Explorer | `index.html` | Local interactive HTML viewer (pan/zoom SVG + BOM links for all diagrams) |
| Interconnects | `interconnects.yml` | Cross-zone bulkhead/harness interconnect map (`C-1`–`C-10`, fusible links, fuse box) |
| Full Car | `full_1978_wiring_diagram.yml` | Combined end-to-end schematic merging Zones 1–5 |
| Zone 1 | `zone1_front_chassis.yml` | Headlamps, front turn/clearance lamps, side markers, horns, alternator, starter, distributor, coil, check connector, engine senders |
| Zone 2 | `zone2_engine_efi.yml` | Battery, fusible links, transistor ignition, EFI main relay, 1978 dual fuel pump relays, 35-pin ECU, dropping resistors, injectors 1–6, cold start/thermotime/CTS/air regulator/AFM/TPS, wiper & washer motors |
| Zone 3 | `zone3_center_dash.yml` | Combination meter gauges & warning lamps, dimmer rheostat, ignition switch, steering column combination switch, intermittent wiper amp, horn/defogger/ignition relays, flasher units |
| Zone 4 | `zone4_cabin_fuse_box.yml` | 12-circuit fuse box (Fuses 1–12), heater blower/resistor/switch, A/C switches, stop & parking brake switches, radio/antenna, hazard switch, seat belt warning, C-7/C-8 rear pass-through |
| Zone 5 | `zone5_rear_chassis.yml` | Rear combination lamps, side markers, license plate lamps, electric fuel pump, tank sender, rear defogger, room lamp, power antenna motor, rear speakers |

## Nissan-to-WireViz Color Code Mapping

Two-letter Nissan codes (`Base` + `Stripe`) map to IEC 60757 WireViz codes:

- `B` → `BK` (Black)
- `W` → `WH` (White)
- `R` → `RD` (Red)
- `G` → `GN` (Green)
- `L` → `BU` (Blue)
- `Y` → `YE` (Yellow)
- `Br` → `BN` (Brown)
- `Lg` → `GN` (Light Green, mapped to `GN` in WireViz)
- Examples: `RW` → `RDWH`, `RB` → `RDBK`, `RL` → `RDBU`, `BW` → `BKWH`, `BY` → `BKYE`, `GW` → `GNWH`

## Rendering Diagrams

WireViz is installed in `.venv/bin/wireviz`.

To generate Graphviz `.gv` and BOM `.bom.tsv` files without system `dot`:

```bash
.venv/bin/wireviz -f gt wireviz/zone1_front_chassis.yml
```

To render full `.html`, `.png`, `.svg`, and `.bom.tsv` outputs, install system Graphviz (`dot`) first:

```bash
sudo dnf install graphviz
.venv/bin/wireviz wireviz/zone*.yml
```
