# Clampcount

**Plan every edge-glue-up before the glue goes on: how many clamps, where they go, whether they are strong and wide enough, and the clock schedule from dry fit to full cure.**

Clampcount reads one plain-text project file and works out the clamp count and spacing, the joint force, whether your clamp stock is strong and wide enough, and a timed schedule. It runs offline on the Python standard library. It uses exact fractions, so `6 3/8"` stays `6 3/8"` and nothing drifts into rounding error.

## Features

- **Plain-text project files** with `name`, `glue`, `start`, `clamps`, `board` and optional `diag` lines.
- **Line-numbered errors**: every mistake is reported as `file:line: message`, and all of them are shown at once.
- **Exact fraction input**: `6.5`, `3/4` and mixed numbers like `6 3/8` are all accepted.
- **Clamp count from spacing**: one clamp at each end plus enough between to keep spacing at or under your maximum, evenly spaced.
- **Clamp-stock check**: only clamps that open at least the full panel width count, and the strongest qualifying clamps are picked first.
- **Joint force**: pressure × glued area, split across the clamps, with a per-clamp force check against your stock.
- **Glue presets** for PVA, epoxy, hide glue and polyurethane (open time, clamp time, cure time, planning pressure), each overridable.
- **Temperature-adjusted open time**: cold shops stretch it and hot shops shorten it, in documented bands.
- **Clock schedule** from dry fit to full cure, with `+1d` day rollover and deadlines rounded down to the minute.
- **Assembly deadline check**: flags a glue-up where the clamps go on after the open time has run out.
- **Frame squareness** from measured diagonals, both in a project file and as a standalone `square` command.
- **Mismatch warnings** for boards of different thickness or length.
- **Inches or millimetres**: inches print as nearest-1/64 fractions, millimetres to 0.1 mm.
- **JSON output** with `plan --json` for scripts and spreadsheets.
- **CI-friendly exit codes**: `0` plan OK, `1` problems found, `2` the file could not be read or parsed.

## Quick start

```sh
python3 clampcount.py plan examples/walnut-tabletop.glue
```

The project file (`examples/walnut-tabletop.glue`) looks like this:

```text
name: Walnut tabletop
units: in
glue: pva
temp: 22
start: 09:00
dryfit: 10
assembly: 5
max_spacing: 8
pressure: 100

clamps: 8, 36, 1200

board: 6 1/2, 48, 3/4
board: 6 1/2, 48, 3/4
board: 6 3/8, 48, 3/4
```

Sample output:

```text
Walnut tabletop · PVA (aliphatic resin) · 22 °C
Panel     6 1/2" + 6 1/2" + 6 3/8" wide, 48" long, 3/4" thick
Joints    2 edge joint(s), 72.0 in² of glue line
Pressure  100 psi → 7,200 lbf total
Clamps    7 along the length, 8" apart (max 8")
          at 0", 8", 16", 24", 32", 40", 48"
          each needs 1,029 lbf; 8 of your clamps open wide enough
          using 7 × 1,200 lbf, opening 36"
Open time 10 min (×1 for 22 °C)
Schedule
  09:00        Start dry fit
  09:10        Apply glue
  09:15        Clamps on, assembly done
  09:20        Open time ends
  09:45        Clamps off
  09:10 +1d    Full cure
Result: OK
```

## Project file reference

| Key | Meaning | Default |
| --- | --- | --- |
| `name` | label for the plan | `Glue-up` |
| `units` | `in` or `mm` for lengths | `in` |
| `glue` | `pva`, `epoxy`, `hide` or `urethane` (required) | none |
| `start` | start of dry fit, `HH:MM` (required) | none |
| `temp` | shop temperature in °C | `20` |
| `dryfit` | minutes of dry fit before glue goes on | `0` |
| `assembly` | minutes from glue-up to clamps on | `5` |
| `max_spacing` | widest clamp gap along the length | 8 in / 200 mm |
| `pressure` | psi on the glue line | glue preset |
| `thick_tol` | allowed board thickness difference | 1/64 in |
| `square_tol` | allowed diagonal difference | 1/32 in |
| `clamps` | `count, opening, force` (lbf); repeat for each lot | at least one |
| `board` | `width, length, thickness`; at least two | at least two |
| `diag` | two measured diagonals of the frame | none |

Values on a line are separated by commas. Use `#` for comments.

## Commands

```sh
python3 clampcount.py plan FILE [--json]    # full plan: clamps, force, schedule, problems
python3 clampcount.py check FILE            # lint and problems only
python3 clampcount.py glues                 # glue presets and temperature factors
python3 clampcount.py square D1 D2 [--tol 1/32] [--units in|mm]
```

A door panel in millimetres with a frame-squareness check ships as `examples/cabinet-door.glue`.

## Notes

- The glue presets and temperature bands are **planning values, not manufacturer specifications**. Check your glue's data sheet and set `pressure:` to its figure.
- Clamp force is compared in lbf whatever the length unit is.
- The planner does not know the clock; you give it `start`, so the schedule is reproducible.

## Testing

```sh
python3 -m pytest
```

The suite lives in `tests/test_clampcount.py`. It checks parsing and error messages, exact fraction maths, clamp counts and positions, clamp-stock and force problems, open-time and temperature rules, squareness, mm output, and the CLI exit codes and JSON. It needs `pytest` and nothing else. You can also run it from any directory with `python3 -m pytest /path/to/clampcount/tests`, because `conftest.py` puts the project root on the import path.

## Requirements

Python 3.9 or newer. No third-party packages are needed to run the planner.
