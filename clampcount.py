#!/usr/bin/env python3
"""Clampcount: an offline glue-up planner for edge-glued panels and frames.

Reads a plain-text project file that describes the boards, the glue, the
clamps you own and the start time. It works out how many clamps the glue-up
needs and where to put them, whether your clamps are strong and wide enough,
the force the joints need, and a clock schedule from dry fit to full cure.
Everything is exact (fractions), offline and stdlib only.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

MM_PER_IN = Fraction(127, 5)  # 25.4 mm per inch, exact
DEFAULT_SPACING_IN = Fraction(8)
DEFAULT_THICK_TOL_IN = Fraction(1, 64)
DEFAULT_SQUARE_TOL_IN = Fraction(1, 32)
GLUE_LENGTH_TOL_IN = Fraction(1, 32)
DEFAULT_TEMP_C = Fraction(20)
TEMP_RANGE_C = (Fraction(5), Fraction(35))


@dataclass(frozen=True)
class Glue:
    label: str
    open_min: Fraction  # open time at 15-25 °C, minutes
    clamp_min: Fraction  # minimum clamp time, minutes
    cure_h: Fraction  # time to full strength, hours
    psi: Fraction  # planning pressure on the glue line, psi


# Planning defaults. Check each glue's data sheet and override with `pressure:`.
GLUES = {
    "pva": Glue("PVA (aliphatic resin)", Fraction(10), Fraction(30), Fraction(24), Fraction(100)),
    "epoxy": Glue("Epoxy", Fraction(20), Fraction(60), Fraction(24), Fraction(50)),
    "hide": Glue("Hide glue", Fraction(8), Fraction(60), Fraction(24), Fraction(80)),
    "urethane": Glue("Polyurethane", Fraction(15), Fraction(60), Fraction(24), Fraction(60)),
}


@dataclass
class Board:
    width: Fraction
    length: Fraction
    thickness: Fraction
    line: int


@dataclass
class ClampLot:
    count: int
    opening: Fraction
    force: Fraction
    line: int


@dataclass
class Project:
    name: str
    glue: str
    start_min: Fraction
    units: str = "in"
    temp_c: Fraction = DEFAULT_TEMP_C
    dryfit_min: Fraction = Fraction(0)
    assembly_min: Fraction = Fraction(5)
    max_spacing: Fraction | None = None
    pressure: Fraction | None = None
    thick_tol: Fraction | None = None
    square_tol: Fraction | None = None
    boards: list[Board] = field(default_factory=list)
    clamps: list[ClampLot] = field(default_factory=list)
    diag: tuple[Fraction, Fraction] | None = None


@dataclass
class Plan:
    project: Project
    total_width: Fraction
    length: Fraction
    joints: int
    glued_area: Fraction
    pressure: Fraction
    required_force: Fraction
    n_clamps: int
    spacing: Fraction
    positions: list[Fraction]
    force_per_clamp: Fraction
    chosen: list[tuple[Fraction, Fraction]]  # (force lbf, opening in)
    qualifying: int
    open_factor: Fraction
    open_min: Fraction
    schedule: list[tuple[str, Fraction]]
    squareness: Fraction | None
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# ---------------------------------------------------------------- parsing helpers

_CLOCK = re.compile(r"^(\d{2}):(\d{2})$")


def parse_number(text: str) -> Fraction:
    """Parse '6.5', '3/4' or a mixed number like '1 1/2' exactly."""
    parts = text.split()
    try:
        if len(parts) == 1:
            return Fraction(parts[0])
        if len(parts) == 2 and parts[0].isdigit() and "/" in parts[1]:
            return int(parts[0]) + Fraction(parts[1])
    except (ValueError, ZeroDivisionError):
        pass
    raise ValueError(f"not a number: '{text}'")


def parse_clock(text: str) -> Fraction:
    """Parse HH:MM into minutes since midnight."""
    match = _CLOCK.match(text.strip())
    if not match:
        raise ValueError(f"expected HH:MM, got '{text}'")
    hours, minutes = int(match[1]), int(match[2])
    if hours > 23 or minutes > 59:
        raise ValueError(f"time out of range: '{text}'")
    return Fraction(hours * 60 + minutes)


def fmt_clock(minutes: Fraction) -> str:
    """Show a clock time, rounded down to the minute, with a +Nd day marker."""
    whole = math.floor(minutes)
    days, rem = divmod(whole, 1440)
    hours, mins = divmod(rem, 60)
    suffix = f" +{days}d" if days else ""
    return f"{hours:02d}:{mins:02d}{suffix}"


def to_in(value: Fraction, units: str) -> Fraction:
    return value if units == "in" else value / MM_PER_IN


def from_in(value: Fraction, units: str) -> Fraction:
    return value if units == "in" else value * MM_PER_IN


def fmt_len(value_in: Fraction, units: str = "in") -> str:
    """Inches as nearest 1/64 fraction (e.g. 6 1/2"), or millimetres to 0.1."""
    if units == "mm":
        return f"{float(from_in(value_in, 'mm')):.1f} mm"
    sixtyfourths = round(value_in * 64)
    whole, rem = divmod(sixtyfourths, 64)
    frac = Fraction(rem, 64)
    if frac == 0:
        return f'{whole}"'
    if whole == 0:
        return f'{frac.numerator}/{frac.denominator}"'
    return f'{whole} {frac.numerator}/{frac.denominator}"'


def fmt_force(value: Fraction) -> str:
    return f"{float(value):,.0f} lbf"


def open_time_factor(temp_c: Fraction) -> Fraction:
    """Multiplier on open time. Cold stretches it, heat shortens it."""
    if temp_c < 10:
        return Fraction(3, 2)
    if temp_c < 15:
        return Fraction(5, 4)
    if temp_c <= 25:
        return Fraction(1)
    if temp_c <= 30:
        return Fraction(3, 4)
    return Fraction(1, 2)


def clamp_count(length: Fraction, max_spacing: Fraction) -> int:
    """Clamps along the length: one at each end plus enough between to keep spacing <= max."""
    return math.ceil(length / max_spacing) + 1


def square_check(diag1: Fraction, diag2: Fraction, tol: Fraction) -> tuple[Fraction, bool]:
    diff = abs(diag1 - diag2)
    return diff, diff <= tol


# ---------------------------------------------------------------- project file

LIST_KEYS = {"board", "clamps"}
TEXT_KEYS = {"name", "glue", "units"}
NUMBER_KEYS = {"temp", "dryfit", "assembly", "max_spacing", "pressure", "thick_tol", "square_tol"}
ALL_KEYS = TEXT_KEYS | NUMBER_KEYS | LIST_KEYS | {"start", "diag"}


def _values(text: str, names: list[str]) -> list[Fraction]:
    items = [item.strip() for item in text.split(",")]
    if len(items) != len(names):
        raise ValueError(f"expected {len(names)} comma-separated values ({', '.join(names)}), got {len(items)}")
    return [parse_number(item) for item in items]


def _positive(value: Fraction, what: str) -> Fraction:
    if value <= 0:
        raise ValueError(f"{what} must be greater than zero")
    return value


def _nonnegative(value: Fraction, what: str) -> Fraction:
    if value < 0:
        raise ValueError(f"{what} cannot be negative")
    return value


def parse_project(text: str, source: str = "<input>") -> tuple[Project | None, list[str]]:
    """Parse a project file. Returns (project, errors); errors are 'file:line: message'."""
    errors: list[str] = []
    seen: set[str] = set()
    single: dict[str, object] = {}
    boards: list[Board] = []
    clamps: list[ClampLot] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            errors.append(f"{source}:{lineno}: expected 'key: value'")
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key not in ALL_KEYS:
            errors.append(f"{source}:{lineno}: unknown key '{key}'")
            continue
        if key not in LIST_KEYS:
            if key in seen:
                errors.append(f"{source}:{lineno}: duplicate key '{key}'")
                continue
            seen.add(key)
        try:
            if key == "name":
                if not value:
                    raise ValueError("name cannot be empty")
                single[key] = value
            elif key == "units":
                if value.lower() not in ("in", "mm"):
                    raise ValueError("units must be 'in' or 'mm'")
                single[key] = value.lower()
            elif key == "glue":
                if value.lower() not in GLUES:
                    raise ValueError(f"unknown glue '{value}' (run 'clampcount glues')")
                single[key] = value.lower()
            elif key == "start":
                single[key] = parse_clock(value)
            elif key == "temp":
                single[key] = parse_number(value)
            elif key in ("dryfit", "assembly"):
                single[key] = _nonnegative(parse_number(value), key)
            elif key in NUMBER_KEYS:
                single[key] = _positive(parse_number(value), key)
            elif key == "board":
                width, length, thick = (_positive(v, "board dimension") for v in _values(value, ["width", "length", "thickness"]))
                boards.append(Board(width, length, thick, lineno))
            elif key == "clamps":
                count, opening, force = _values(value, ["count", "opening", "force"])
                if count != int(count) or count < 1:
                    raise ValueError("clamp count must be a whole number, at least 1")
                clamps.append(ClampLot(int(count), _positive(opening, "clamp opening"), _positive(force, "clamp force"), lineno))
            elif key == "diag":
                first, second = _values(value, ["diagonal", "diagonal"])
                single[key] = (_positive(first, "diagonal"), _positive(second, "diagonal"))
        except ValueError as exc:
            errors.append(f"{source}:{lineno}: {exc}")

    for required in ("glue", "start"):
        if required not in single:
            errors.append(f"{source}: missing required key '{required}'")
    if len(boards) < 2:
        errors.append(f"{source}: need at least 2 'board:' lines to glue, found {len(boards)}")
    if not clamps:
        errors.append(f"{source}: need at least one 'clamps:' line")
    if errors:
        return None, errors

    return Project(
        name=str(single.get("name", "Glue-up")),
        glue=str(single["glue"]),
        start_min=single["start"],
        units=str(single.get("units", "in")),
        temp_c=single.get("temp", DEFAULT_TEMP_C),
        dryfit_min=single.get("dryfit", Fraction(0)),
        assembly_min=single.get("assembly", Fraction(5)),
        max_spacing=single.get("max_spacing"),
        pressure=single.get("pressure"),
        thick_tol=single.get("thick_tol"),
        square_tol=single.get("square_tol"),
        boards=boards,
        clamps=clamps,
        diag=single.get("diag"),
    ), []


# ---------------------------------------------------------------- planning

def build_plan(project: Project) -> Plan:
    glue = GLUES[project.glue]
    units = project.units
    problems: list[str] = []
    warnings: list[str] = []

    widths = [to_in(b.width, units) for b in project.boards]
    lengths = [to_in(b.length, units) for b in project.boards]
    thicknesses = [to_in(b.thickness, units) for b in project.boards]
    total_width = sum(widths)
    length = min(lengths)

    if max(lengths) - length > GLUE_LENGTH_TOL_IN:
        warnings.append(
            f"boards differ in length by {fmt_len(max(lengths) - length, units)}; "
            f"the glue line uses the shortest, {fmt_len(length, units)}"
        )
    thick_tol = to_in(project.thick_tol, units) if project.thick_tol is not None else DEFAULT_THICK_TOL_IN
    if max(thicknesses) - min(thicknesses) > thick_tol:
        warnings.append(
            f"boards differ in thickness by {fmt_len(max(thicknesses) - min(thicknesses), units)} "
            f"(tolerance {fmt_len(thick_tol, units)}); expect a step at the joints"
        )
    if not (TEMP_RANGE_C[0] <= project.temp_c <= TEMP_RANGE_C[1]):
        warnings.append(f"temperature {float(project.temp_c):g} °C is outside the usual 5–35 °C glue-up range")

    joints = len(project.boards) - 1
    glued_area = sum(length * min(thicknesses[i], thicknesses[i + 1]) for i in range(joints))
    pressure = project.pressure if project.pressure is not None else glue.psi
    required = pressure * glued_area

    max_spacing = to_in(project.max_spacing, units) if project.max_spacing is not None else DEFAULT_SPACING_IN
    n_clamps = clamp_count(length, max_spacing)
    spacing = length / (n_clamps - 1)
    positions = [spacing * i for i in range(n_clamps)]
    per_clamp = required / n_clamps

    candidates = [
        (lot.force, to_in(lot.opening, units))
        for lot in project.clamps
        for _ in range(lot.count)
    ]
    qualifying = sorted((c for c in candidates if c[1] >= total_width), key=lambda c: (-c[0], c[1]))
    chosen = qualifying[:n_clamps]
    if len(chosen) < n_clamps:
        problems.append(
            f"need {n_clamps} clamps that open at least {fmt_len(total_width, units)}; "
            f"you have {len(qualifying)} that do"
        )
    elif chosen[-1][0] < per_clamp:
        problems.append(
            f"weakest clamp you could use gives {fmt_force(chosen[-1][0])}, "
            f"but each clamp needs {fmt_force(per_clamp)}"
        )

    open_factor = open_time_factor(project.temp_c)
    open_min = glue.open_min * open_factor
    glue_at = project.start_min + project.dryfit_min
    clamp_on = glue_at + project.assembly_min
    open_deadline = glue_at + open_min
    clamp_off = clamp_on + glue.clamp_min
    cure_done = glue_at + glue.cure_h * 60
    if clamp_on > open_deadline:
        problems.append(
            f"clamps go on at {fmt_clock(clamp_on)} but the open time ends at {fmt_clock(open_deadline)}"
        )
    schedule = [
        ("Start dry fit", project.start_min),
        ("Apply glue", glue_at),
        ("Clamps on, assembly done", clamp_on),
        ("Open time ends", open_deadline),
        ("Clamps off", clamp_off),
        ("Full cure", cure_done),
    ]

    squareness = None
    if project.diag is not None:
        first, second = (to_in(d, units) for d in project.diag)
        tol = to_in(project.square_tol, units) if project.square_tol is not None else DEFAULT_SQUARE_TOL_IN
        squareness, square_ok = square_check(first, second, tol)
        if not square_ok:
            longer = "first" if first > second else "second"
            problems.append(
                f"frame out of square by {fmt_len(squareness, units)} (tolerance {fmt_len(tol, units)}); "
                f"pull the ends of the {longer} diagonal toward each other"
            )

    return Plan(
        project=project,
        total_width=total_width,
        length=length,
        joints=joints,
        glued_area=glued_area,
        pressure=pressure,
        required_force=required,
        n_clamps=n_clamps,
        spacing=spacing,
        positions=positions,
        force_per_clamp=per_clamp,
        chosen=chosen,
        qualifying=len(qualifying),
        open_factor=open_factor,
        open_min=open_min,
        schedule=schedule,
        squareness=squareness,
        warnings=warnings,
        problems=problems,
    )


def _clamp_summary(chosen: list[tuple[Fraction, Fraction]], units: str) -> list[str]:
    counts = Counter(chosen)
    lines = []
    for (force, opening), count in sorted(counts.items(), key=lambda kv: -kv[0][0]):
        lines.append(f"{count} × {fmt_force(force)}, opening {fmt_len(opening, units)}")
    return lines


def render_plan(plan: Plan) -> str:
    p = plan.project
    units = p.units
    glue = GLUES[p.glue]
    max_spacing = to_in(p.max_spacing, units) if p.max_spacing is not None else DEFAULT_SPACING_IN
    out = [
        f"{p.name} · {glue.label} · {float(p.temp_c):g} °C",
        f"Panel     {' + '.join(fmt_len(to_in(b.width, units), units) for b in p.boards)} wide, "
        f"{fmt_len(plan.length, units)} long, {fmt_len(to_in(p.boards[0].thickness, units), units)} thick",
        f"Joints    {plan.joints} edge joint(s), {float(plan.glued_area):.1f} in² of glue line",
        f"Pressure  {float(plan.pressure):g} psi → {fmt_force(plan.required_force)} total",
        f"Clamps    {plan.n_clamps} along the length, {fmt_len(plan.spacing, units)} apart "
        f"(max {fmt_len(max_spacing, units)})",
        "          at " + ", ".join(fmt_len(pos, units) for pos in plan.positions),
        f"          each needs {fmt_force(plan.force_per_clamp)}; {plan.qualifying} of your clamps open wide enough",
    ]
    out += [f"          using {line}" for line in _clamp_summary(plan.chosen, units)]
    out.append(
        f"Open time {float(plan.open_min):g} min "
        f"(×{plan.open_factor} for {float(p.temp_c):g} °C)"
    )
    out.append("Schedule")
    for label, minutes in plan.schedule:
        out.append(f"  {fmt_clock(minutes):<12} {label}")
    if plan.squareness is not None:
        out.append(f"Squareness  diagonals differ by {fmt_len(plan.squareness, units)}")
    if plan.warnings:
        out.append("Warnings")
        out += [f"  - {w}" for w in plan.warnings]
    if plan.problems:
        out.append("Problems")
        out += [f"  - {msg}" for msg in plan.problems]
    out.append("Result: OK" if plan.ok else f"Result: {len(plan.problems)} problem(s)")
    return "\n".join(out)


def plan_to_dict(plan: Plan) -> dict:
    p = plan.project
    units = p.units
    return {
        "name": p.name,
        "glue": p.glue,
        "temp_c": float(p.temp_c),
        "total_width_in": float(plan.total_width),
        "length_in": float(plan.length),
        "joints": plan.joints,
        "glued_area_in2": float(plan.glued_area),
        "pressure_psi": float(plan.pressure),
        "required_force_lbf": float(plan.required_force),
        "clamps_needed": plan.n_clamps,
        "clamp_spacing_in": float(plan.spacing),
        "clamp_positions_in": [float(pos) for pos in plan.positions],
        "force_per_clamp_lbf": float(plan.force_per_clamp),
        "qualifying_clamps": plan.qualifying,
        "open_time_min": float(plan.open_min),
        "schedule": {label: fmt_clock(minutes) for label, minutes in plan.schedule},
        "squareness_in": None if plan.squareness is None else float(plan.squareness),
        "warnings": plan.warnings,
        "problems": plan.problems,
        "ok": plan.ok,
        "units": units,
    }


# ---------------------------------------------------------------- CLI

def _load(path: str) -> Project | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"clampcount: cannot read {path}: {exc.strerror}", file=sys.stderr)
        return None
    project, errors = parse_project(text, source=path)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return None
    return project


def cmd_plan(args: argparse.Namespace) -> int:
    project = _load(args.file)
    if project is None:
        return 2
    plan = build_plan(project)
    if args.json:
        print(json.dumps(plan_to_dict(plan), indent=2))
    else:
        print(render_plan(plan))
    return 0 if plan.ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    project = _load(args.file)
    if project is None:
        return 2
    plan = build_plan(project)
    for warning in plan.warnings:
        print(f"{args.file}: warning: {warning}")
    for problem in plan.problems:
        print(f"{args.file}: error: {problem}")
    if plan.ok:
        print(f"{args.file}: OK")
        return 0
    return 1


def cmd_glues(_args: argparse.Namespace) -> int:
    print(f"{'key':<9}{'glue':<26}{'open':>8}{'clamp':>9}{'cure':>7}{'psi':>6}")
    for key, glue in GLUES.items():
        print(
            f"{key:<9}{glue.label:<26}{float(glue.open_min):>5g} min"
            f"{float(glue.clamp_min):>5g} min{float(glue.cure_h):>5g} h{float(glue.psi):>6g}"
        )
    print()
    print("Open time factor by temperature (°C): <10 ×1.5, 10–14 ×1.25, 15–25 ×1, 26–30 ×0.75, >30 ×0.5")
    return 0


def _number_arg(text: str) -> Fraction:
    try:
        return parse_number(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def cmd_square(args: argparse.Namespace) -> int:
    units = args.units
    first = to_in(args.diag1, units)
    second = to_in(args.diag2, units)
    tol = to_in(args.tol, units) if args.tol is not None else DEFAULT_SQUARE_TOL_IN
    diff, ok = square_check(first, second, tol)
    print(f"diagonals {fmt_len(first, units)} and {fmt_len(second, units)} differ by {fmt_len(diff, units)}")
    if ok:
        print(f"IN SQUARE (tolerance {fmt_len(tol, units)})")
        return 0
    longer = "first" if first > second else "second"
    print(f"OUT OF SQUARE (tolerance {fmt_len(tol, units)}); pull the ends of the {longer} diagonal toward each other")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clampcount", description="Plan glue-up clamps, force and timing from a plain-text file.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="plan a glue-up from a project file")
    p_plan.add_argument("file")
    p_plan.add_argument("--json", action="store_true", help="print the plan as JSON")
    p_plan.set_defaults(func=cmd_plan)

    p_check = sub.add_parser("check", help="lint a project file and its clamp plan (exit 1 on problems)")
    p_check.add_argument("file")
    p_check.set_defaults(func=cmd_check)

    p_glues = sub.add_parser("glues", help="list glue presets and temperature factors")
    p_glues.set_defaults(func=cmd_glues)

    p_sq = sub.add_parser("square", help="check a frame's diagonals for squareness")
    p_sq.add_argument("diag1", type=_number_arg)
    p_sq.add_argument("diag2", type=_number_arg)
    p_sq.add_argument("--tol", type=_number_arg, default=None, help="tolerance in --units (default 1/32 in)")
    p_sq.add_argument("--units", choices=("in", "mm"), default="in")
    p_sq.set_defaults(func=cmd_square)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
