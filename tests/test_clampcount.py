import json
from fractions import Fraction as F
from pathlib import Path

import pytest

import clampcount as cc

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
TABLETOP = EXAMPLES / "walnut-tabletop.glue"
DOOR = EXAMPLES / "cabinet-door.glue"


def build(text: str) -> cc.Project:
    project, errors = cc.parse_project(text, source="test.glue")
    assert errors == []
    assert project is not None
    return project


BASE = """\
name: Test
glue: pva
start: 09:00
clamps: 8, 36, 1200
board: 6, 48, 3/4
board: 6, 48, 3/4
"""


def test_parse_number_accepts_decimals_fractions_and_mixed_numbers():
    assert cc.parse_number("6.5") == F(13, 2)
    assert cc.parse_number("3/4") == F(3, 4)
    assert cc.parse_number("1 1/2") == F(3, 2)
    assert cc.parse_number("6 3/8") == F(51, 8)


@pytest.mark.parametrize("text", ["2 3", "1/0", "abc", ""])
def test_parse_number_rejects_bad_input(text):
    with pytest.raises(ValueError):
        cc.parse_number(text)


def test_clock_parse_and_format_with_day_rollover():
    assert cc.parse_clock("09:30") == 570
    assert cc.fmt_clock(F(570)) == "09:30"
    assert cc.fmt_clock(F(1450)) == "00:10 +1d"
    assert cc.fmt_clock(F(1119, 2)) == "09:19"  # 559.5 minutes floors to 09:19
    with pytest.raises(ValueError):
        cc.parse_clock("24:00")


def test_fmt_len_uses_sixty_fourths_and_millimetres():
    assert cc.fmt_len(F(13, 2)) == '6 1/2"'
    assert cc.fmt_len(F(3, 4)) == '3/4"'
    assert cc.fmt_len(F(0)) == '0"'
    assert cc.fmt_len(F(1), "mm") == "25.4 mm"


@pytest.mark.parametrize(
    "temp, factor",
    [(9, F(3, 2)), (10, F(5, 4)), (14, F(5, 4)), (15, F(1)), (25, F(1)), (26, F(3, 4)), (31, F(1, 2))],
)
def test_open_time_factor_bands(temp, factor):
    assert cc.open_time_factor(F(temp)) == factor


def test_clamp_count_puts_one_at_each_end_and_keeps_spacing_under_max():
    assert cc.clamp_count(F(48), F(8)) == 7
    assert cc.clamp_count(F(5), F(8)) == 2
    assert cc.clamp_count(F(16), F(8)) == 3
    assert cc.clamp_count(F(17), F(8)) == 4


def test_tabletop_example_parses():
    project, errors = cc.parse_project(TABLETOP.read_text(encoding="utf-8"), source=str(TABLETOP))
    assert errors == []
    assert project.glue == "pva"
    assert project.start_min == 540
    assert len(project.boards) == 3
    assert project.boards[2].width == F(51, 8)
    assert project.clamps[0].count == 8


def test_tabletop_plan_numbers_are_exact():
    plan = cc.build_plan(cc.parse_project(TABLETOP.read_text(encoding="utf-8"))[0])
    assert plan.ok
    assert plan.total_width == F(155, 8)
    assert plan.glued_area == 72
    assert plan.required_force == 7200
    assert plan.n_clamps == 7
    assert plan.spacing == 8
    assert plan.force_per_clamp == F(7200, 7)
    assert plan.open_min == 10
    assert [minutes for _, minutes in plan.schedule] == [540, 550, 555, 560, 585, 1990]


def test_tabletop_render_shows_expected_lines():
    plan = cc.build_plan(cc.parse_project(TABLETOP.read_text(encoding="utf-8"))[0])
    text = cc.render_plan(plan)
    assert "7,200 lbf total" in text
    assert "7 along the length, 8\" apart" in text
    assert "7 × 1,200 lbf, opening 36\"" in text
    assert "09:10 +1d" in text and "Full cure" in text
    assert text.endswith("Result: OK")


def test_too_few_clamps_is_a_problem():
    plan = cc.build_plan(build(BASE.replace("clamps: 8, 36, 1200", "clamps: 3, 36, 1200")))
    assert not plan.ok
    assert any("need 7 clamps" in problem for problem in plan.problems)


def test_clamps_that_do_not_open_wide_enough_do_not_count():
    plan = cc.build_plan(build(BASE.replace("clamps: 8, 36, 1200", "clamps: 8, 10, 1200")))
    assert any("need 7 clamps" in problem for problem in plan.problems)


def test_weak_clamps_are_a_problem():
    # one joint: 48 in × 3/4 in × 100 psi = 3600 lbf; over 7 clamps that is 514.29 lbf each
    plan = cc.build_plan(build(BASE.replace("clamps: 8, 36, 1200", "clamps: 8, 36, 500")))
    assert any("weakest clamp" in problem for problem in plan.problems)


def test_assembly_running_past_open_time_is_a_problem():
    text = BASE.replace("start: 09:00\n", "start: 09:00\ndryfit: 15\nassembly: 20\n")
    plan = cc.build_plan(build(text))
    assert any("open time ends" in problem for problem in plan.problems)


def test_cold_shop_stretches_open_time_enough_for_the_same_assembly():
    cold = cc.build_plan(build(BASE.replace("start: 09:00\n", "start: 09:00\ntemp: 9\nassembly: 12\n")))
    hot = cc.build_plan(build(BASE.replace("start: 09:00\n", "start: 09:00\ntemp: 22\nassembly: 12\n")))
    assert cold.open_min == 15 and cold.ok
    assert hot.open_min == 10 and not hot.ok


def test_thickness_mismatch_warns():
    text = BASE.replace(
        "board: 6, 48, 3/4\nboard: 6, 48, 3/4\n",
        "board: 6, 48, 3/4\nboard: 6, 48, 13/16\n",
    )
    plan = cc.build_plan(build(text))
    assert any("thickness" in warning for warning in plan.warnings)


def test_parse_errors_carry_file_and_line():
    project, errors = cc.parse_project("glue: pva\ncolour: red\nstart: 09:00\n", source="x.glue")
    assert project is None
    assert "x.glue:2: unknown key 'colour'" in errors
    assert any("need at least 2 'board:' lines" in e for e in errors)


def test_missing_glue_is_reported():
    _, errors = cc.parse_project("start: 09:00\nclamps: 2, 36, 300\nboard: 6, 48, 3/4\nboard: 6, 48, 3/4\n", source="x")
    assert "x: missing required key 'glue'" in errors


def test_square_check_uses_tolerance_inclusively():
    assert cc.square_check(F(40), F(40) + F(1, 4), F(1, 32)) == (F(1, 4), False)
    assert cc.square_check(F(40), F(40) + F(1, 64), F(1, 32)) == (F(1, 64), True)


def test_out_of_square_frame_is_a_problem():
    text = BASE + "diag: 40 1/4, 40 1/2\n"
    plan = cc.build_plan(build(text))
    assert any("out of square" in problem for problem in plan.problems)


def test_metric_door_example_is_exact_and_reports_in_mm():
    plan = cc.build_plan(cc.parse_project(DOOR.read_text(encoding="utf-8"))[0])
    assert plan.ok
    assert plan.n_clamps == 6
    assert plan.glued_area == F(274500, 16129)
    assert plan.required_force == F(21960000, 16129)
    text = cc.render_plan(plan)
    assert "610.0 mm long" in text
    assert "Squareness  diagonals differ by 0.4 mm" in text


def test_cli_plan_exit_codes(tmp_path, capsys):
    assert cc.main(["plan", str(TABLETOP)]) == 0
    assert "Walnut tabletop" in capsys.readouterr().out

    short = tmp_path / "short.glue"
    short.write_text(BASE.replace("clamps: 8, 36, 1200", "clamps: 3, 36, 1200"), encoding="utf-8")
    assert cc.main(["plan", str(short)]) == 1

    assert cc.main(["plan", str(tmp_path / "missing.glue")]) == 2


def test_cli_check_and_json_output(capsys):
    assert cc.main(["check", str(DOOR)]) == 0
    assert "OK" in capsys.readouterr().out

    assert cc.main(["plan", "--json", str(TABLETOP)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["clamps_needed"] == 7
    assert data["ok"] is True
    assert data["schedule"]["Full cure"] == "09:10 +1d"


def test_cli_square_command(capsys):
    assert cc.main(["square", "40 1/4", "40 1/2"]) == 1
    assert "OUT OF SQUARE" in capsys.readouterr().out
    assert cc.main(["square", "40 1/4", "40 1/4"]) == 0
    assert "IN SQUARE" in capsys.readouterr().out


def test_cli_glues_lists_presets(capsys):
    assert cc.main(["glues"]) == 0
    out = capsys.readouterr().out
    assert "pva" in out and "epoxy" in out
