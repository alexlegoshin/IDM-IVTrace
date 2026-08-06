import math

import pytest

from measurement import (
    run_measurement, _read_attempts, _read_averaged, _adaptive_cooling_delay,
    EXCITATION_UNITS, OUTPUT_UNITS,
)
from sweep import Branch, DirectionPreset


class FakeDMM:
    """Заглушка Multimeter: отдаёт заготовленные значения тока по порядку вызовов."""

    def __init__(self, readings):
        self.readings = list(readings)  # каждый элемент — либо float, либо Exception-класс/инстанс
        self.ranges = [0.002, 0.02, 0.2, 2.0]
        self.current_range_idx = len(self.ranges) - 1
        self.set_range_calls = []
        self.auto_range_calls = []

    def measure(self) -> float:
        item = self.readings.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def auto_range(self, measured_current, is_first=False):
        self.auto_range_calls.append((measured_current, is_first))

    def set_range(self, r):
        self.set_range_calls.append(r)


class FakeSource:
    def __init__(self):
        self.calls = []
        self.current_setpoints = []
        self.voltage_setpoints = []

    def setup(self, voltage_limit, current_limit=1.0):
        self.calls.append(('setup', voltage_limit))

    def set_current(self, current):
        self.current_setpoints.append(current)
        self.calls.append(('set_current', current))

    def set_voltage(self, voltage):
        self.voltage_setpoints.append(voltage)
        self.calls.append(('set_voltage', voltage))

    def output_on(self):
        self.calls.append(('output_on',))

    def output_off(self):
        self.calls.append(('output_off',))

    def shutdown(self):
        self.calls.append(('shutdown',))


class FakeRelay:
    def __init__(self):
        self.calls = []

    def forward(self):
        self.calls.append('forward')
        return 'OK'

    def reverse(self):
        self.calls.append('reverse')
        return 'OK'

    def off(self):
        self.calls.append('off')
        return 'OK'


def _run(dmm, src, relay, **kwargs):
    defaults = dict(
        excitation_type='current',
        X_start=0, X_stop=1, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
    )
    defaults.update(kwargs)
    return run_measurement(dmm, src, relay, **defaults)


# ----------------------------------------------------------------------
# _read_attempts / _read_averaged — сентинел переполнения и усреднение (п.29)
# ----------------------------------------------------------------------

def test_read_attempts_excludes_overflow_and_jumps_straight_to_max_range():
    dmm = FakeDMM(readings=[9.9e37, 0.02, 0.021, 0.019])
    dmm.current_range_idx = 0

    valid, overflowed = _read_attempts(dmm, count=4, delay=0)

    assert valid == [0.02, 0.021, 0.019]
    assert overflowed is True
    assert dmm.current_range_idx == len(dmm.ranges) - 1
    assert dmm.set_range_calls[0] == dmm.ranges[-1]


def test_read_attempts_excludes_negative_overflow_sentinel_too():
    dmm = FakeDMM(readings=[-9.9e37, 1.0, 1.0, 1.0])
    valid, overflowed = _read_attempts(dmm, count=4, delay=0)
    assert valid == [1.0, 1.0, 1.0]
    assert overflowed is True


def test_read_averaged_discards_first_reading_by_default():
    dmm = FakeDMM(readings=[100.0, 1.0, 1.1, 0.9])  # первое — заведомо выброс
    result = _read_averaged(dmm, count=4, delay=0, discard_first=True)
    assert result == [1.0, 1.1, 0.9]


def test_read_averaged_keeps_first_reading_when_discard_first_is_false():
    dmm = FakeDMM(readings=[1.0, 1.0, 1.0, 1.0])
    result = _read_averaged(dmm, count=4, delay=0, discard_first=False)
    assert len(result) == 4


def test_read_averaged_does_not_discard_when_only_one_reading_survived():
    dmm = FakeDMM(readings=[Exception("x"), Exception("x"), Exception("x"), 1.0])
    result = _read_averaged(dmm, count=4, delay=0, discard_first=True)
    assert result == [1.0]  # нечего отбрасывать — иначе точка осталась бы без данных вовсе


def test_read_averaged_retries_once_after_full_overflow():
    dmm = FakeDMM(readings=[9.9e37, 9.9e37, 9.9e37, 9.9e37,  0.5, 0.51, 0.49, 0.50])
    result = _read_averaged(dmm, count=4, delay=0, discard_first=False)
    assert result == [0.5, 0.51, 0.49, 0.50]


def test_read_averaged_gives_up_after_second_round_still_overflowing():
    dmm = FakeDMM(readings=[9.9e37] * 8)
    assert _read_averaged(dmm, count=4, delay=0) == []


def test_read_averaged_custom_count():
    dmm = FakeDMM(readings=[1.0] * 10)
    result = _read_averaged(dmm, count=10, delay=0, discard_first=False)
    assert len(result) == 10


# ----------------------------------------------------------------------
# run_measurement — базовая форма плана (DIVERGING, оба знака)
# ----------------------------------------------------------------------

def test_default_sweep_measures_zero_once_then_both_polarities():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    results, aborted = _run(dmm, src, relay, X_start=0, X_stop=2, X_step=1)

    assert aborted is None
    assert [r['X_set'] for r in results] == [0.0, 1.0, 2.0, -1.0, -2.0]
    assert [r['Branch'] for r in results] == ['zero', 'forward', 'forward', 'reverse', 'reverse']
    assert relay.calls == ['forward', 'reverse', 'off']


def test_zero_only_sweep_never_touches_relay_or_source_output():
    dmm = FakeDMM(readings=[0.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    results, aborted = _run(dmm, src, relay, X_start=0, X_stop=0, X_step=1)

    assert [r['Branch'] for r in results] == ['zero']
    assert relay.calls == []  # даже cleanup-'off' не нужен, реле не трогали вовсе
    assert ('output_on',) not in src.calls


def test_source_output_is_off_during_zero_point():
    dmm = FakeDMM(readings=[0.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    _run(dmm, src, relay, X_start=0, X_stop=1, X_step=1)
    assert ('output_off',) in src.calls  # output_off() вызывается на всякий случай для X=0


def test_relay_switches_exactly_once_per_polarity_change():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    _run(dmm, src, relay, X_start=0, X_stop=3, X_step=1)
    # 3 положительных точки одним forward(), 3 отрицательных одним reverse() —
    # не по разу на каждую точку.
    assert relay.calls == ['forward', 'reverse', 'off']


def test_range_reset_to_max_on_relay_switch():
    dmm = FakeDMM(readings=[0.1] * 100)
    dmm.current_range_idx = 0
    src = FakeSource()
    relay = FakeRelay()
    _run(dmm, src, relay, X_start=0, X_stop=1, X_step=1)
    # После переключения на reverse диапазон должен были сброшен на максимум.
    assert dmm.ranges[-1] in dmm.set_range_calls


def test_voltage_excitation_calls_set_voltage_not_set_current():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    _run(dmm, src, relay, excitation_type='voltage', X_start=0, X_stop=1, X_step=1)
    assert src.voltage_setpoints
    assert not src.current_setpoints


def test_current_excitation_setup_uses_v_limit():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    _run(dmm, src, relay, excitation_type='current', V_limit=7.5, X_start=0, X_stop=1, X_step=1)
    assert ('setup', 7.5) in src.calls


def test_voltage_excitation_setup_uses_x_stop_as_limit():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    _run(dmm, src, relay, excitation_type='voltage', X_start=0, X_stop=42.0, X_step=42, V_limit=999)
    assert ('setup', 42.0) in src.calls


def test_unknown_excitation_type_raises_value_error():
    dmm = FakeDMM(readings=[])
    src = FakeSource()
    relay = FakeRelay()
    with pytest.raises(ValueError):
        _run(dmm, src, relay, excitation_type='bogus')


def test_source_and_relay_shut_down_in_finally_even_on_failure():
    class DyingSource(FakeSource):
        def set_current(self, current):
            raise RuntimeError("сессия источника закрыта")

    dmm = FakeDMM(readings=[1.0] * 100)
    src = DyingSource()
    relay = FakeRelay()
    with pytest.raises(RuntimeError):
        _run(dmm, src, relay, X_start=0, X_stop=1, X_step=1)
    assert ('shutdown',) in src.calls
    assert 'off' in relay.calls


def test_should_stop_halts_between_points():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    calls = {'n': 0}

    def should_stop():
        calls['n'] += 1
        return calls['n'] > 2  # остановиться после пары точек

    results, aborted = _run(dmm, src, relay, X_start=0, X_stop=5, X_step=1, should_stop=should_stop)
    assert aborted is None
    assert len(results) < 6


# ----------------------------------------------------------------------
# run_measurement — branch (п.8)
# ----------------------------------------------------------------------

def test_branch_positive_never_touches_reverse():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(dmm, src, relay, X_start=0, X_stop=2, X_step=1, branch=Branch.POSITIVE)
    assert 'reverse' not in relay.calls
    assert all(r['X_set'] >= 0 for r in results)


def test_branch_negative_never_touches_forward():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(dmm, src, relay, X_start=0, X_stop=2, X_step=1, branch=Branch.NEGATIVE)
    assert 'forward' not in relay.calls
    assert all(r['X_set'] <= 0 for r in results)


# ----------------------------------------------------------------------
# run_measurement — пресеты направления (п.19)
# ----------------------------------------------------------------------

def test_converging_preset_shares_a_single_zero_at_the_pivot():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(
        dmm, src, relay, X_start=0, X_stop=2, X_step=1, preset=DirectionPreset.CONVERGING,
    )
    assert [r['X_set'] for r in results] == [2.0, 1.0, 0.0, -1.0, -2.0]
    assert [r['X_set'] for r in results].count(0.0) == 1


def test_full_cycle_preset_visits_zero_three_times():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(
        dmm, src, relay, X_start=0, X_stop=2, X_step=1, preset=DirectionPreset.FULL_CYCLE,
    )
    assert [r['X_set'] for r in results].count(0.0) == 3


# ----------------------------------------------------------------------
# run_measurement — витки (п.37)
# ----------------------------------------------------------------------

def test_turns_multiply_x_real_but_not_the_wire_setpoint():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(dmm, src, relay, X_start=0, X_stop=2, X_step=1, turns=4.0)

    by_x_set = {r['X_set']: r for r in results}
    assert by_x_set[2.0]['X_real'] == pytest.approx(8.0)   # 2 А * 4 витка = 8 "видимых" ампервитков
    # Но в провод (уставка источника) уходит именно X_set, не X_real:
    assert 2.0 in src.current_setpoints
    assert 8.0 not in src.current_setpoints


def test_turns_default_to_one_when_not_specified():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(dmm, src, relay, X_start=0, X_stop=1, X_step=1)
    for r in results:
        assert r['X_real'] == r['X_set']


def test_expected_value_for_error_checks_uses_real_input_not_wire_setpoint():
    # X_set=1, turns=1000, ratio=1000 -> вход датчика 1000 А, ожидаемый выход
    # 1000/1000 = 1.0 А. Показание 1.0 А должно приниматься без брака.
    dmm = FakeDMM(readings=[1.0] * 12)
    src = FakeSource()
    relay = FakeRelay()
    results, aborted = _run(
        dmm, src, relay, X_start=0, X_stop=1, X_step=1,
        turns=1000.0, ratio=1000.0, stop_on_error=True, error_threshold=1.0,
    )
    assert aborted is None
    assert not any(r['Rejected'] for r in results)


# ----------------------------------------------------------------------
# run_measurement — контрольные промеры (п.9) и отсечка по погрешности (п.7)
# ----------------------------------------------------------------------

def test_deviation_on_first_attempt_is_retried_and_accepted_if_it_recovers():
    # ratio=1000 -> ожидание при X=1 равно 0.001. Первая попытка (среднее
    # усреднения = 0.5) грубо мимо, вторая (0.001) — в допуске.
    dmm = FakeDMM(readings=[0.5, 0.5, 0.5, 0.5,  0.001, 0.001, 0.001, 0.001])
    src = FakeSource()
    relay = FakeRelay()
    results, aborted = _run(
        dmm, src, relay, X_start=0, X_stop=0, X_step=1,  # X=0 -> сразу вторая точка ниже
    )
    # Проверим напрямую вторую (ненулевую) развёртку отдельно — нагляднее:
    dmm2 = FakeDMM(readings=[0.5, 0.5, 0.5, 0.5,  0.001, 0.001, 0.001, 0.001])
    results2, aborted2 = _run(
        dmm2, src, relay, X_start=1, X_stop=1, X_step=1,
        ratio=1000.0, stop_on_error=True, error_threshold=1.0, branch=Branch.POSITIVE,
    )
    assert aborted2 is None
    assert results2[0]['Rejected'] is False


def test_point_rejected_after_three_consistent_deviations():
    dmm = FakeDMM(readings=[0.5, 0.5, 0.5, 0.5] * 3)  # три захода по 4 отсчёта, всё время 0.5
    src = FakeSource()
    relay = FakeRelay()
    results, aborted = _run(
        dmm, src, relay, X_start=1, X_stop=1, X_step=1, branch=Branch.POSITIVE,
        ratio=1000.0, stop_on_error=True, error_threshold=1.0,
    )
    assert results[0]['Rejected'] is True
    assert 'погрешность' in results[0]['RejectReason']
    assert aborted is not None


def test_rejected_point_does_not_abort_when_stop_on_error_disabled():
    dmm = FakeDMM(readings=([0.5] * 4) * 3 + [1.0] * 4)  # первая точка бракуется, но идём дальше
    src = FakeSource()
    relay = FakeRelay()
    results, aborted = _run(
        dmm, src, relay, X_start=1, X_stop=2, X_step=1, branch=Branch.POSITIVE,
        ratio=1000.0, stop_on_error=False, error_threshold=1.0,
    )
    assert aborted is None
    assert results[0]['Rejected'] is True
    assert len(results) == 2  # вторая точка всё равно была снята


def test_rejected_point_still_appears_in_raw_results():
    # "остаётся в сырых данных с флагом" — не выбрасывается из results.
    dmm = FakeDMM(readings=[0.5] * 12)
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(
        dmm, src, relay, X_start=1, X_stop=1, X_step=1, branch=Branch.POSITIVE,
        ratio=1000.0, stop_on_error=False, error_threshold=1.0,
    )
    assert len(results) == 1
    assert results[0]['X_set'] == 1.0


def test_no_retries_happen_without_ratio():
    # Без ratio нечего сверять — читаем один раз, никаких повторных заходов.
    dmm = FakeDMM(readings=[999.0, 999.0, 999.0, 999.0])  # хватит РОВНО на один заход
    src = FakeSource()
    relay = FakeRelay()
    results, aborted = _run(
        dmm, src, relay, X_start=1, X_stop=1, X_step=1, branch=Branch.POSITIVE,
        stop_on_error=True, error_threshold=1.0,  # включено, но ratio не задан
    )
    assert aborted is None
    assert results[0]['Rejected'] is False


def test_nan_reading_does_not_trigger_rejection_or_retries():
    # Все чтения провалились -> NaN. Это сбой связи, а не "мимо ожидания" —
    # ретраить/браковать по погрешности бессмысленно без единого показания.
    dmm = FakeDMM(readings=[Exception("x")] * 4)
    src = FakeSource()
    relay = FakeRelay()
    results, aborted = _run(
        dmm, src, relay, X_start=1, X_stop=1, X_step=1, branch=Branch.POSITIVE,
        ratio=1000.0, stop_on_error=True, error_threshold=1.0,
    )
    assert aborted is None
    assert math.isnan(results[0]['Y_meas'])
    assert results[0]['Rejected'] is False


# ----------------------------------------------------------------------
# run_measurement — перепутанная полярность (п.14)
# ----------------------------------------------------------------------

def test_polarity_mismatch_flagged_when_sign_disagrees_with_setpoint():
    # X_set положительный, а показание пришло отрицательным.
    dmm = FakeDMM(readings=[-1.0, -1.0, -1.0, -1.0])
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(
        dmm, src, relay, X_start=1, X_stop=1, X_step=1, branch=Branch.POSITIVE,
        ratio=1.0, error_threshold=1000.0,  # порог огромный — не мешает проверке полярности
    )
    assert results[0]['PolarityMismatch'] is True


def test_no_polarity_mismatch_when_signs_agree():
    dmm = FakeDMM(readings=[1.0, 1.0, 1.0, 1.0])
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(
        dmm, src, relay, X_start=1, X_stop=1, X_step=1, branch=Branch.POSITIVE,
        ratio=1.0, error_threshold=1000.0,
    )
    assert results[0]['PolarityMismatch'] is False


def test_polarity_check_does_not_apply_to_zero_point():
    dmm = FakeDMM(readings=[-0.001, -0.001, -0.001, -0.001])
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(dmm, src, relay, X_start=0, X_stop=0, X_step=1)
    assert results[0]['Branch'] == 'zero'
    assert 'PolarityMismatch' in results[0]
    assert results[0]['PolarityMismatch'] is False


def test_polarity_mismatch_requires_ratio_to_be_meaningful():
    # Без ratio expected не определён — проверка молча пропускается (как и
    # раньше без ratio для отсечки), не даёт ложных срабатываний.
    dmm = FakeDMM(readings=[-1.0, -1.0, -1.0, -1.0])
    src = FakeSource()
    relay = FakeRelay()
    results, _ = _run(dmm, src, relay, X_start=1, X_stop=1, X_step=1, branch=Branch.POSITIVE)
    assert results[0]['PolarityMismatch'] is False


# ----------------------------------------------------------------------
# results_sink — точки должны пережить аварийный останов
# ----------------------------------------------------------------------

class DyingSource(FakeSource):
    """Источник, у которого сессию закрыли снаружи после N уставок."""

    def __init__(self, alive_setpoints):
        super().__init__()
        self.alive_setpoints = alive_setpoints

    def set_current(self, current):
        if self.alive_setpoints <= 0:
            raise RuntimeError("сессия источника закрыта аварийным остановом")
        self.alive_setpoints -= 1
        super().set_current(current)


def test_results_sink_keeps_points_measured_before_the_loop_blows_up():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = DyingSource(alive_setpoints=2)
    relay = FakeRelay()
    sink = []

    with pytest.raises(RuntimeError):
        _run(dmm, src, relay, X_start=1, X_stop=10, X_step=1, results_sink=sink)

    assert [r['X_set'] for r in sink] == [1.0, 2.0]


def test_results_sink_gets_the_same_points_as_the_return_value():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    sink = []
    results, _ = _run(dmm, src, relay, X_start=0, X_stop=1, X_step=1, results_sink=sink)
    assert sink == results


def test_cleanup_failures_do_not_mask_the_real_error():
    class DeadEverything(FakeSource):
        def set_current(self, current):
            raise RuntimeError("НАСТОЯЩАЯ ПРИЧИНА: сессия источника закрыта")

        def shutdown(self):
            raise RuntimeError("вторичное падение уборки")

    class DeadRelay(FakeRelay):
        def off(self):
            raise RuntimeError("вторичное падение уборки")

    with pytest.raises(RuntimeError, match="НАСТОЯЩАЯ ПРИЧИНА"):
        _run(FakeDMM(readings=[1.0] * 100), DeadEverything(), DeadRelay(),
             X_start=1, X_stop=1, X_step=1)


# ----------------------------------------------------------------------
# log_callback
# ----------------------------------------------------------------------

def test_log_callback_receives_progress_instead_of_stdout(capsys):
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    lines = []

    _run(dmm, src, relay, X_start=0, X_stop=1, X_step=1, log_callback=lines.append)

    assert any('forward' in line for line in lines)
    assert any('zero' in line for line in lines)
    assert capsys.readouterr().out == ''


def test_rejection_and_polarity_mismatch_are_logged():
    dmm = FakeDMM(readings=[-0.5] * 12)
    src = FakeSource()
    relay = FakeRelay()
    lines = []
    _run(
        dmm, src, relay, X_start=1, X_stop=1, X_step=1, branch=Branch.POSITIVE,
        ratio=1000.0, stop_on_error=False, error_threshold=1.0, log_callback=lines.append,
    )
    joined = '\n'.join(lines)
    assert 'БРАК' in joined
    assert 'полярност' in joined


def test_excitation_units_mapping():
    assert EXCITATION_UNITS == {'current': 'A', 'voltage': 'V'}


# ----------------------------------------------------------------------
# output_type (ось А-1, PLAN_V2.md) — что измеряет мультиметр на выходе
# датчика, независимо от excitation_type (чем датчик возбуждается)
# ----------------------------------------------------------------------

def test_output_units_mapping():
    assert OUTPUT_UNITS == {'current': 'A', 'voltage': 'V'}


def test_default_output_type_is_current_and_produces_y_meas_column():
    dmm = FakeDMM(readings=[0.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5])
    src, relay = FakeSource(), FakeRelay()
    results, _ = _run(dmm, src, relay)
    assert all('Y_meas' in row for row in results)
    assert all(row['Y_unit'] == 'A' for row in results)
    assert all('I_meas_A' not in row for row in results)


def test_output_type_voltage_tags_rows_with_voltage_unit():
    dmm = FakeDMM(readings=[0.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5])
    src, relay = FakeSource(), FakeRelay()
    results, _ = _run(dmm, src, relay, output_type='voltage')
    assert all(row['Y_unit'] == 'V' for row in results)


def test_output_type_does_not_change_which_dmm_method_is_called():
    # Роль прибора (амперметр/вольтметр) определяется КОНФИГОМ, с которым он
    # открыт (см. orchestrate._resolve_instruments), а не тем, какой метод
    # у него дёрнули изнутри measurement.py — здесь всегда generic measure().
    dmm = FakeDMM(readings=[0.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5])
    src, relay = FakeSource(), FakeRelay()
    _run(dmm, src, relay, output_type='voltage')
    # FakeDMM.measure() успешно отдало все заготовленные значения — если бы
    # код звал несуществующий dmm.measure_voltage(), тест упал бы с AttributeError.


# ----------------------------------------------------------------------
# _adaptive_cooling_delay (п.27, BETA)
# ----------------------------------------------------------------------

def test_adaptive_cooling_at_zero_magnitude_is_the_base_delay():
    assert _adaptive_cooling_delay(1.0, magnitude=0.0, max_magnitude=100.0) == pytest.approx(1.0)


def test_adaptive_cooling_at_max_magnitude_reaches_the_cap():
    assert _adaptive_cooling_delay(1.0, magnitude=100.0, max_magnitude=100.0,
                                    max_multiplier=5.0) == pytest.approx(5.0)


def test_adaptive_cooling_scales_quadratically_not_linearly():
    # На половине максимума задержка должна быть заметно ближе к базовой,
    # чем при линейном масштабировании (джоулево тепло ∝ I²).
    half = _adaptive_cooling_delay(1.0, magnitude=50.0, max_magnitude=100.0, max_multiplier=5.0)
    linear_half = 1.0 + 0.5 * (5.0 - 1.0)  # было бы при линейном масштабе
    assert half < linear_half
    assert half == pytest.approx(1.0 + 0.25 * (5.0 - 1.0))


def test_adaptive_cooling_never_exceeds_the_cap_beyond_max_magnitude():
    # magnitude > max_magnitude в принципе не должно случаться (max_magnitude
    # берётся из самого плана), но функция не должна улетать выше потолка.
    assert _adaptive_cooling_delay(1.0, magnitude=150.0, max_magnitude=100.0,
                                    max_multiplier=5.0) == pytest.approx(5.0)


def test_adaptive_cooling_falls_back_to_base_when_sweep_has_no_magnitude():
    # Свип из одной только нулевой точки — max_magnitude=0, делить не на что.
    assert _adaptive_cooling_delay(1.0, magnitude=0.0, max_magnitude=0.0) == pytest.approx(1.0)


def test_run_measurement_uses_flat_cooling_delay_by_default(monkeypatch):
    sleeps = []
    monkeypatch.setattr('measurement.time.sleep', lambda s: sleeps.append(s))

    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    _run(dmm, src, relay, X_start=0, X_stop=2, X_step=1, cooling_delay=0.3)

    # Все cooling-паузы (не считая delay=0 на установку) — ровно 0.3, без масштабирования.
    assert 0.3 in sleeps
    assert not any(0.3 < s < 1.5 for s in sleeps)  # никаких промежуточных масштабированных значений


def test_run_measurement_scales_cooling_delay_when_adaptive_enabled(monkeypatch):
    sleeps = []
    monkeypatch.setattr('measurement.time.sleep', lambda s: sleeps.append(s))

    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    _run(
        dmm, src, relay, X_start=0, X_stop=2, X_step=1, cooling_delay=1.0,
        branch=Branch.POSITIVE, adaptive_cooling=True, adaptive_cooling_max_multiplier=5.0,
    )

    # Точка X=2 — самая большая (max_magnitude=2) -> должна получить потолок 5.0.
    assert any(s == pytest.approx(5.0) for s in sleeps)
    # Точка X=1 — половина максимума -> задержка меньше потолка, но больше базовой.
    assert any(1.0 < s < 5.0 for s in sleeps)
