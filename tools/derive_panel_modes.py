#!/usr/bin/env python3
"""Derive per-panel drive info from bb_epaper's panel table, for preset enrichment.

PURPOSE. `simple-config-presets.json` is hand-maintained and carries only geometry and colour
scheme per display. The new driver work needs per-panel DRIVE information (which modes exist,
whether BUSY is usable, the colour code map, the controller family). Most of that is already
encoded in bb_epaper's panel table -- it just lives in a C initialiser instead of data.

This extracts it mechanically so the presets can be enriched WITHOUT hand-transcription. Hand
transcription is exactly what produced the third copy of panel data this work is trying to
delete (py-opendisplay/display_palettes.py mirrors these same tables by hand).

WHAT IT DOES NOT DO. It cannot invent what bb_epaper does not know:
  * custom LUTs / 16-colour modes  -- measured per panel, 3 panels so far
  * lut_placement                  -- measured, and it DIFFERS between those 3
  * temp_gate                      -- measured, 1 panel so far
  * per-unit colour calibration    -- measured, 2 units of 1 panel
Those are emitted only from the MEASURED table below, and every other panel is marked
`characterised: false`. A plausible-looking guess is worse than an absent field, because
downstream code cannot tell a guess from a measurement.

SOURCE
    bb_epaper/src/bb_ep.inl  -- the `panel_defs[]`-style initialiser, one row per panel enum:
        {w, h, ?, pInitFull, pInitFast, pInitPart, flags, chip, colorLookup}
    Firmware_Unified/.../opendisplay_epd_map.c -- panel_ic_type -> bb_epaper enum

USAGE
    derive_panel_modes.py --inl PATH --map PATH --presets PATH [--out FILE]
    (read-only by default; --out writes the enriched preset JSON to a NEW file)
"""
import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# MEASURED data. Everything here was established on hardware in this repo and is
# cited so a reader can find the evidence rather than trusting the table.
# ---------------------------------------------------------------------------
MEASURED = {
    # 2.9" BWRY GDEY029F51H -- the 16-colour reference panel.
    "EP29YR_168x384": {
        "characterised": True,
        "lut": {
            "supported": True,
            "enable": "PSR byte2 bit7",
            "placement": "pre_pon",
            "size_bytes": 535,
        },
        "extra_modes": [{
            "name": "colour16",
            "passes": 3,
            "lut": "wave_colour+edit",
            "note": "3-pass OEPL 16-colour; pass0 colour wave, passes1-2 edit wave "
                    "with vsn1 patched to 0x14/0x41",
            "measured_drf_ms": [20637, 1823, 1823],
        }],
        "temp_gate": {
            "min_c": 18, "max_c": 32, "optimal_c": 25, "policy": "advise",
            "evidence": "gamut hull peaks near 25C: 3828 at 25C, 2796 at 38.3C "
                        "(-27%), 3289 at 16.6C. Non-monotonic.",
        },
        "unit_variation": {
            "de00_mean": 6.40,
            "note": "two units differ by dE00 6.40 +-0.003 (~1855x repeat noise), "
                    "14% gamut gap; a palette tuned on one measured 19% worse on "
                    "the other. Calibrate per unit.",
        },
    },
    # 4.2" BWRY GDEM042F52
    "EP42YR_400x300": {
        "characterised": True,
        "lut": {
            "supported": True,
            "enable": "PSR byte2 bit7",
            "placement": "pre_pon",
            "size_bytes": 535,
            "evidence": "4-arm control: PSR bit alone inert (chroma <=3.1); two "
                        "different waves render differently (chroma 23.5/49.6/15.0)",
        },
        "extra_modes": [{"name": "colour16", "passes": 3, "lut": "wave_colour+edit"}],
        "busy_override": {
            "usable": False,
            "fixed_delay_ms": 35000,
            "evidence": "reads busy immediately after power-up with nothing sent, "
                        "and never releases after DRF. Matches BBEP_SKIP_BUSY_WAIT.",
        },
    },
    # 7.5" BWRY GDEM075F52 -- the placement exception.
    "EP75YR_800x480": {
        "characterised": True,
        "lut": {
            "supported": True,
            "enable": "PSR byte2 bit7",
            "placement": "post_init",
            "size_bytes": 535,
            "evidence": "SILENTLY DISCARDED if uploaded pre-PON like the 2.9/4.2. "
                        "Must go after the whole init incl. the 0xE0/0xE6/0xA5 "
                        "booster tail. Proof: each wave plays at its own intrinsic "
                        "length (internal 20244ms, colour 20909ms, edit 1862ms).",
        },
        "extra_modes": [{"name": "colour16", "passes": 3, "lut": "wave_colour+edit",
                         "measured_drf_ms": [20895, 1862, 1862]}],
    },
    # 7.3" Spectra 6 -- custom LUTs proven NOT available.
    "EP73_800x480": {
        "characterised": True,
        "lut": {
            "supported": False,
            "evidence": "SPD1657A: LUT_SEL inert, proven by a 2x2 control (LUT_SEL=1 "
                        "alone gives a normal refresh, 22336 vs 22332 ms). The hang "
                        "tracks bank WRITES, not the bit. Do not re-attempt.",
        },
    },
}

ROW_RE = re.compile(
    r"\{\s*(\d+)\s*,\s*(\d+)\s*,\s*[^,]+,\s*"      # w, h, ?
    r"([A-Za-z0-9_]+|NULL)\s*,\s*"                   # pInitFull
    r"([A-Za-z0-9_]+|NULL)\s*,\s*"                   # pInitFast
    r"([A-Za-z0-9_]+|NULL)\s*,\s*"                   # pInitPart
    r"([^,]+),\s*"                                   # flags
    r"([A-Za-z0-9_]+)\s*,\s*"                        # chip
    r"([A-Za-z0-9_]+)\s*\}\s*,\s*//\s*([A-Za-z0-9_]+)"  # colorLookup, // ENUM
)
MAP_RE = re.compile(r"case\s+(0x[0-9A-Fa-f]+)\s*:\s*return\s+([A-Za-z0-9_]+)\s*;")


def parse_panels(inl_path):
    text = Path(inl_path).read_text(errors="replace")
    out = {}
    for m in ROW_RE.finditer(text):
        w, h, full, fast, part, flags, chip, lookup, enum = m.groups()
        flags = flags.strip()
        modes = [{"name": "full", "init": "full"}]
        if fast != "NULL":
            modes.append({"name": "fast", "init": "fast"})
        if part != "NULL":
            modes.append({"name": "partial", "init": "partial"})
        out[enum] = {
            "geometry": {"w": int(w), "h": int(h)},
            "chip": chip.replace("BBEP_CHIP_", ""),
            "colour": {"code_map_symbol": lookup},
            "busy": {"usable": "SKIP_BUSY_WAIT" not in flags},
            "flags": [f.strip() for f in flags.split("|") if f.strip()],
            "modes": modes,
            "characterised": False,
        }
    return out


def parse_map(map_path):
    text = Path(map_path).read_text(errors="replace")
    return {enum: int(pid, 16) for pid, enum in MAP_RE.findall(text)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inl", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--presets", required=True)
    ap.add_argument("--out", default=None, help="write enriched presets to a NEW file")
    a = ap.parse_args()

    panels = parse_panels(a.inl)
    enum_to_pid = parse_map(a.map)
    pid_to_enum = {v: k for k, v in enum_to_pid.items()}

    print(f"  parsed {len(panels)} panel rows from bb_epaper")
    print(f"  parsed {len(enum_to_pid)} panel_ic_type -> enum mappings")

    presets = json.loads(Path(a.presets).read_text())
    displays = presets["displays"]
    enriched = matched = measured_hits = 0
    unmatched = []

    for d in displays:
        pid = d.get("panelIcType")
        if pid is None:
            unmatched.append((d["id"], "no panelIcType"))
            continue
        pid = int(str(pid), 0)
        enum = pid_to_enum.get(pid)
        if not enum or enum not in panels:
            unmatched.append((d["id"], f"pid {pid}=0x{pid:04X} -> {enum or 'unmapped'}"))
            continue
        matched += 1
        info = json.loads(json.dumps(panels[enum]))   # deep copy
        info["bb_epaper_enum"] = enum
        meas = MEASURED.get(enum)
        if meas:
            measured_hits += 1
            info.update({k: v for k, v in meas.items() if k != "busy_override"})
            if "busy_override" in meas:
                info["busy"] = meas["busy_override"]
            for em in meas.get("extra_modes", []):
                info["modes"].append(em)
            info.pop("extra_modes", None)
        d["drive"] = info
        enriched += 1

    print(f"  matched {matched}/{len(displays)} presets to a bb_epaper row")
    print(f"  of those, {measured_hits} have MEASURED drive data; "
          f"{enriched - measured_hits} are derived-only (characterised: false)")
    if unmatched:
        print(f"  {len(unmatched)} unmatched:")
        for i, (pid, why) in enumerate(unmatched):
            if i < 12:
                print(f"    {pid:<32} {why}")
        if len(unmatched) > 12:
            print(f"    ... and {len(unmatched)-12} more")

    if a.out:
        Path(a.out).write_text(json.dumps(presets, indent=1) + "\n")
        print(f"  wrote {a.out}")
    else:
        print("  (read-only; pass --out to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
