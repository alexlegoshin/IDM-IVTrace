import math

import pytest

from sweep import Branch, DirectionPreset, SweepPoint, _raw_pass, plan_sweep


def xs(points):
    return [p.x_set for p in points]


def relays(points):
    return [p.relay for p in points]


# ----------------------------------------------------------------------
# _raw_pass — строительный блок: один буквальный проход
# ----------------------------------------------------------------------

def test_raw_pass_simple_ascending():
    assert _raw_pass(0, 10, 5) == [0, 5, 10]


def test_raw_pass_simple_descending():
    assert _raw_pass(10, 0, 5) == [10, 5, 0]


def test_raw_pass_single_point_when_start_equals_stop():
    assert _raw_pass(5, 5, 1) == [5]


def test_raw_pass_step_larger_than_range_still_hits_both_ends():
    assert _raw_pass(0, 5, 10) == [0.0, 5]


def test_raw_pass_non_dividing_step_forces_exact_endpoint():
    result = _raw_pass(0, 10, 3)
    assert result[-1] == 10
    assert result == [0.0, 3.0, 6.0, 10]


def test_raw_pass_long_sweep_endpoint_is_exact_despite_float_step():
    # 0.1 не представим точно в float — именно такой шаг проверяет п.18.
    result = _raw_pass(0, 10, 0.1)
    assert result[-1] == 10.0
    assert len(result) == 101


def test_raw_pass_injects_zero_when_crossing_ascending():
    result = _raw_pass(-25, 25, 10)
    assert result == [-25, -15, -5, 0.0, 5, 15, 25]


def test_raw_pass_injects_zero_when_crossing_descending():
    result = _raw_pass(25, -25, 10)
    assert result == [25, 15, 5, 0.0, -5, -15, -25]


def test_raw_pass_does_not_inject_zero_when_already_present():
    # 0 -> 10 step 5 естественно содержит 0 первой точкой — доп. вставки нет.
    result = _raw_pass(0, 10, 5)
    assert result.count(0.0) == 1


def test_raw_pass_does_not_inject_zero_when_range_does_not_cross_it():
    result = _raw_pass(150, 250, 50)
    assert 0 not in result


def test_raw_pass_rejects_nonpositive_step():
    with pytest.raises(ValueError):
        _raw_pass(0, 10, 0)
    with pytest.raises(ValueError):
        _raw_pass(0, 10, -1)


# ----------------------------------------------------------------------
# plan_sweep — интервал уже сам по себе двуполярный (spans both signs)
# ----------------------------------------------------------------------

def test_plan_ascending_through_zero_single_continuous_pass():
    # -25 -> 25: то, что раньше "не давали ввести" (п.17).
    pts = plan_sweep(-25, 25, 10)
    assert xs(pts) == [-25, -15, -5, 0, 5, 15, 25]
    assert relays(pts) == ['reverse', 'reverse', 'reverse', None, 'forward', 'forward', 'forward']


def test_plan_descending_through_zero_single_continuous_pass():
    # 25 -> -25: раньше буквально отклонялось валидацией.
    pts = plan_sweep(25, -25, 10)
    assert xs(pts) == [25, 15, 5, 0, -5, -15, -25]
    assert relays(pts) == ['forward', 'forward', 'forward', None, 'reverse', 'reverse', 'reverse']


def test_plan_wide_bipolar_range():
    pts = plan_sweep(-250, 250, 100)
    assert xs(pts)[0] == -250
    assert xs(pts)[-1] == 250
    assert 0 in xs(pts)


def test_plan_branch_filter_on_already_bipolar_range_positive():
    # "какая бы ветка ни была включена — both особенно" (п.17): для уже
    # двуполярного интервала branch работает как фильтр, не порождает точек.
    pts = plan_sweep(-25, 25, 10, branch=Branch.POSITIVE)
    assert all(p.x_set >= 0 for p in pts)
    assert xs(pts) == [0, 5, 15, 25]


def test_plan_branch_filter_on_already_bipolar_range_negative():
    pts = plan_sweep(-25, 25, 10, branch=Branch.NEGATIVE)
    assert all(p.x_set <= 0 for p in pts)
    assert xs(pts) == [-25, -15, -5, 0]


def test_plan_branch_both_on_bipolar_range_is_unfiltered():
    filtered = plan_sweep(-25, 25, 10, branch=Branch.BOTH)
    literal = _raw_pass(-25, 25, 10)
    assert xs(filtered) == literal


def test_plan_bipolar_range_ignores_direction_preset():
    # Пресет не участвует для буквально двуполярного интервала — с любым
    # пресетом результат одинаковый.
    a = plan_sweep(-25, 25, 10, preset=DirectionPreset.DIVERGING)
    b = plan_sweep(-25, 25, 10, preset=DirectionPreset.FULL_CYCLE)
    assert xs(a) == xs(b)


# ----------------------------------------------------------------------
# plan_sweep — односторонняя развёртка, заякоренная в нуле, branch=BOTH
# ----------------------------------------------------------------------

def test_diverging_default_matches_legacy_v14ae_shape():
    # Пресет по умолчанию — тот же порядок, что был в v1.4ae/Ф0: общий
    # ноль один раз в начале, затем полный положительный проход, затем
    # полный отрицательный.
    pts = plan_sweep(0, 10, 5)
    assert xs(pts) == [0, 5, 10, -5, -10]
    assert [p.is_zero for p in pts] == [True, False, False, False, False]
    assert xs(pts).count(0.0) == 1


def test_converging_meets_at_zero_continuously():
    pts = plan_sweep(0, 10, 5, preset=DirectionPreset.CONVERGING)
    assert xs(pts) == [10, 5, 0, -5, -10]
    zero_idx = xs(pts).index(0)
    assert pts[zero_idx].relay is None
    # Ноль встречается ровно один раз — это общий пивот, а не две раздельные точки.
    assert xs(pts).count(0.0) == 1


def test_descending_returns_to_zero_from_each_side_separately():
    pts = plan_sweep(0, 10, 5, preset=DirectionPreset.DESCENDING)
    assert xs(pts) == [10, 5, 0, -10, -5, 0]
    # Два РАЗНЫХ нуля — после +экскурсии и после -экскурсии, не общий пивот.
    zero_positions = [i for i, v in enumerate(xs(pts)) if v == 0]
    assert len(zero_positions) == 2
    assert zero_positions[1] - zero_positions[0] > 1  # не соседние


def test_full_cycle_visits_zero_three_times():
    pts = plan_sweep(0, 10, 5, preset=DirectionPreset.FULL_CYCLE)
    assert xs(pts) == [0, 5, 10, 5, 0, -5, -10, -5, 0]
    assert xs(pts).count(0.0) == 3


def test_full_cycle_shape_is_symmetric_positive_then_negative():
    pts = plan_sweep(0, 100, 25, preset=DirectionPreset.FULL_CYCLE)
    values = xs(pts)
    assert values == [0, 25, 50, 75, 100, 75, 50, 25, 0, -25, -50, -75, -100, -75, -50, -25, 0]


def test_preset_choreography_ignores_raw_direction_when_anchored():
    # И "0->10", и "10->0" под одним пресетом дают одну и ту же
    # хореографию — пресет сам решает направление внутри и порядок ветвей.
    a = plan_sweep(0, 10, 5, preset=DirectionPreset.DESCENDING)
    b = plan_sweep(10, 0, 5, preset=DirectionPreset.DESCENDING)
    assert xs(a) == xs(b)


# ----------------------------------------------------------------------
# plan_sweep — односторонняя развёртка, НЕ заякоренная в нуле (150 -> 250)
# ----------------------------------------------------------------------

def test_unanchored_range_never_touches_zero():
    pts = plan_sweep(150, 250, 50)
    assert 0 not in xs(pts)


def test_unanchored_both_mirrors_the_literal_range_without_shared_zero():
    pts = plan_sweep(150, 250, 50, branch=Branch.BOTH)
    assert xs(pts) == [150, 200, 250, -150, -200, -250]


def test_unanchored_single_branch_is_the_literal_range():
    pts = plan_sweep(150, 250, 50, branch=Branch.POSITIVE)
    assert xs(pts) == [150, 200, 250]


def test_unanchored_opposite_branch_mirrors_the_whole_range():
    # Удобство: можно ввести дружелюбный положительный диапазон и попросить
    # отрицательную ветвь, не переписывая числа со знаком.
    pts = plan_sweep(150, 250, 50, branch=Branch.NEGATIVE)
    assert xs(pts) == [-150, -200, -250]


# ----------------------------------------------------------------------
# plan_sweep — одна полярность для развёртки, заякоренной в нуле
# ----------------------------------------------------------------------

def test_anchored_positive_branch_only_no_mirror():
    pts = plan_sweep(0, 10, 5, branch=Branch.POSITIVE)
    assert xs(pts) == [0, 5, 10]
    assert all(p.relay in (None, 'forward') for p in pts)


def test_anchored_negative_branch_mirrors_positive_input():
    # X_start/X_stop даны положительными, но запрошена отрицательная ветвь.
    pts = plan_sweep(0, 10, 5, branch=Branch.NEGATIVE)
    assert xs(pts) == [0, -5, -10]
    assert all(p.relay in (None, 'reverse') for p in pts)


def test_anchored_branch_matching_raw_sign_is_not_mirrored():
    pts = plan_sweep(0, -10, 5, branch=Branch.NEGATIVE)
    assert xs(pts) == [0, -5, -10]


# ----------------------------------------------------------------------
# is_endpoint / magnitude
# ----------------------------------------------------------------------

def test_is_endpoint_marks_the_farthest_point_of_each_run():
    pts = plan_sweep(0, 10, 5)
    endpoints = {p.x_set for p in pts if p.is_endpoint}
    assert endpoints == {10, -10}


def test_magnitude_is_absolute_value_of_x_set():
    pts = plan_sweep(0, 10, 5, preset=DirectionPreset.CONVERGING)
    for p in pts:
        assert p.magnitude == pytest.approx(abs(p.x_set))


def test_zero_point_is_not_marked_endpoint_even_in_zero_only_sweep():
    pts = plan_sweep(0, 0, 1)
    assert len(pts) == 1
    assert pts[0].is_zero
    assert not pts[0].is_endpoint


# ----------------------------------------------------------------------
# Отрицательный ноль не просачивается наружу
# ----------------------------------------------------------------------

def test_negative_branch_zero_point_is_positive_zero_not_negative_zero():
    pts = plan_sweep(0, 10, 5, branch=Branch.NEGATIVE)
    assert math.copysign(1.0, pts[0].x_set) == 1.0


def test_descending_second_zero_is_positive_zero_not_negative_zero():
    pts = plan_sweep(0, 10, 5, preset=DirectionPreset.DESCENDING)
    zero_points = [p for p in pts if p.is_zero]
    for p in zero_points:
        assert math.copysign(1.0, p.x_set) == 1.0


# ----------------------------------------------------------------------
# Ошибки ввода
# ----------------------------------------------------------------------

def test_plan_sweep_rejects_zero_step():
    with pytest.raises(ValueError):
        plan_sweep(0, 10, 0)


def test_plan_sweep_rejects_negative_step():
    with pytest.raises(ValueError):
        plan_sweep(0, 10, -1)


def test_plan_sweep_rejects_nan_endpoints():
    with pytest.raises(ValueError):
        plan_sweep(float('nan'), 10, 1)
    with pytest.raises(ValueError):
        plan_sweep(0, float('nan'), 1)


def test_plan_sweep_rejects_unknown_preset_only_when_anchored_both():
    # Пресет проверяется только там, где реально используется — на
    # двуполярном интервале он не участвует вовсе (см. тест выше), поэтому
    # ошибка возможна только в ветке, которая пресет действительно берёт.
    class NotAPreset:
        pass
    with pytest.raises(ValueError):
        plan_sweep(0, 10, 5, preset=NotAPreset())


# ----------------------------------------------------------------------
# Единственная точка X=0 (весь свип — это только ноль)
# ----------------------------------------------------------------------

def test_zero_only_sweep_gives_a_single_zero_point():
    pts = plan_sweep(0, 0, 1)
    assert len(pts) == 1
    assert pts[0].x_set == 0
    assert pts[0].is_zero
    assert pts[0].relay is None


# ----------------------------------------------------------------------
# SweepPoint — неизменяемость
# ----------------------------------------------------------------------

def test_sweep_point_is_frozen():
    p = SweepPoint(x_set=1.0, magnitude=1.0, relay='forward', is_zero=False, is_endpoint=True)
    with pytest.raises(Exception):
        p.x_set = 2.0
