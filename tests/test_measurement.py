import math

import pytest

from measurement import (
    run_measurement, _measure_branch, _read_attempts, _read_averaged_current,
    EXCITATION_UNITS,
)


class FakeDMM:
    """Заглушка Multimeter: отдаёт заготовленные значения тока по порядку вызовов."""

    def __init__(self, readings):
        self.readings = list(readings)  # каждый элемент — либо float, либо Exception-класс/инстанс
        self.ranges = [0.002, 0.02, 0.2, 2.0]
        self.current_range_idx = len(self.ranges) - 1
        self.set_range_calls = []
        self.auto_range_calls = []

    def measure_current(self) -> float:
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


# ----------------------------------------------------------------------
# _measure_branch
# ----------------------------------------------------------------------

def test_measure_branch_averages_three_readings_and_signs_x_set():
    dmm = FakeDMM(readings=[1.0, 1.1, 1.2] * 3)  # 3 точки по 3 чтения
    src = FakeSource()

    results, _ = _measure_branch(
        dmm, src, 'current',
        X_start=0, X_stop=2, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    assert len(results) == 3
    assert [r['X_set'] for r in results] == [0, 1, 2]
    assert results[0]['I_meas_A'] == pytest.approx((1.0 + 1.1 + 1.2) / 3)
    assert all(r['Branch'] == 'forward' for r in results)


def test_measure_branch_negative_sign_produces_negative_x_set():
    dmm = FakeDMM(readings=[0.5] * 9)
    src = FakeSource()

    results, _ = _measure_branch(
        dmm, src, 'current',
        X_start=0, X_stop=2, X_step=1,
        delay=0, cooling_delay=0,
        sign=-1, branch_name='reverse',
    )

    assert [r['X_set'] for r in results] == [0, -1, -2]


def test_measure_branch_voltage_excitation_calls_set_voltage():
    dmm = FakeDMM(readings=[0.1] * 6)
    src = FakeSource()

    _measure_branch(
        dmm, src, 'voltage',
        X_start=0, X_stop=1, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    assert src.voltage_setpoints == [0, 1]
    assert src.current_setpoints == []


def test_measure_branch_all_reads_failing_yields_nan_not_zero():
    """
    Регрессия: раньше при полном отказе чтения (3/3 исключения) точка
    молча записывалась как I_meas_A=0.0, что маскировало сбой связи под
    видом реального измерения. Должен быть NaN.
    """
    dmm = FakeDMM(readings=[Exception("comm error")] * 3)
    src = FakeSource()

    results, _ = _measure_branch(
        dmm, src, 'current',
        X_start=0, X_stop=0, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    assert len(results) == 1
    assert math.isnan(results[0]['I_meas_A'])


def test_measure_branch_partial_failure_averages_successful_reads():
    dmm = FakeDMM(readings=[Exception("timeout"), 2.0, 2.2])
    src = FakeSource()

    results, _ = _measure_branch(
        dmm, src, 'current',
        X_start=0, X_stop=0, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    assert results[0]['I_meas_A'] == pytest.approx((2.0 + 2.2) / 2)


def test_measure_branch_range_reset_starts_from_max_range():
    dmm = FakeDMM(readings=[0.1] * 3)
    dmm.current_range_idx = 0
    src = FakeSource()

    _measure_branch(
        dmm, src, 'current',
        X_start=0, X_stop=0, X_step=1,
        delay=0, cooling_delay=0,
        sign=-1, branch_name='reverse', range_reset=True,
    )

    assert dmm.current_range_idx == len(dmm.ranges) - 1
    assert dmm.set_range_calls[0] == dmm.ranges[-1]


# ----------------------------------------------------------------------
# _read_attempts / _read_averaged_current — сентинел переполнения (issue #3)
# ----------------------------------------------------------------------

def test_read_attempts_excludes_overflow_and_jumps_straight_to_max_range():
    dmm = FakeDMM(readings=[9.9e37, 0.02, 0.021])
    dmm.current_range_idx = 0

    valid, overflowed = _read_attempts(dmm, attempts=3)

    assert valid == [0.02, 0.021]
    assert overflowed is True
    # Прыжок сразу на максимум, а не на одну ступень: по факту сентинела
    # невозможно понять, насколько именно ушли за предел.
    assert dmm.current_range_idx == len(dmm.ranges) - 1
    assert dmm.set_range_calls[0] == dmm.ranges[-1]


def test_read_attempts_excludes_negative_overflow_sentinel_too():
    dmm = FakeDMM(readings=[-9.9e37, 1.0, 1.0])

    valid, overflowed = _read_attempts(dmm, attempts=3)

    assert valid == [1.0, 1.0]
    assert overflowed is True


def test_read_attempts_no_overflow_flag_on_clean_readings():
    dmm = FakeDMM(readings=[1.0, 1.0, 1.0])
    valid, overflowed = _read_attempts(dmm, attempts=3)
    assert overflowed is False
    assert valid == [1.0, 1.0, 1.0]


def test_read_averaged_current_retries_once_after_full_overflow():
    # Первый заход (3 попытки) — сплошное переполнение, диапазон
    # исправляется на максимум внутри самого захода; второй заход на уже
    # исправленном диапазоне даёт настоящие показания.
    dmm = FakeDMM(readings=[9.9e37, 9.9e37, 9.9e37, 0.5, 0.51, 0.49])
    currents = _read_averaged_current(dmm, attempts=3)
    assert currents == [0.5, 0.51, 0.49]


def test_read_averaged_current_gives_up_after_second_round_still_overflowing():
    # Ни один из двух заходов не дал валидного чтения — сдаёмся (пустой
    # список превратится в NaN точку выше по стеку, а не в мусорное число).
    dmm = FakeDMM(readings=[9.9e37] * 6)
    assert _read_averaged_current(dmm, attempts=3) == []


def test_read_averaged_current_does_not_retry_when_no_overflow_happened():
    # Если сбои — это просто обычные исключения (сбой связи), а не
    # переполнение, повторного захода быть не должно: это уже поведение
    # "все чтения провалились", а не "диапазон был занижен".
    dmm = FakeDMM(readings=[Exception("timeout")] * 3)
    assert _read_averaged_current(dmm, attempts=3) == []


# ----------------------------------------------------------------------
# _measure_branch — переполнение не должно попасть в результат (issue #3)
# ----------------------------------------------------------------------

def test_measure_branch_excludes_overflow_reading_from_the_average():
    dmm = FakeDMM(readings=[9.9e37, 0.02, 0.021])
    src = FakeSource()

    results, _ = _measure_branch(
        dmm, src, 'current',
        X_start=1, X_stop=1, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    assert results[0]['I_meas_A'] == pytest.approx((0.02 + 0.021) / 2)


def test_measure_branch_records_nan_not_the_sentinel_when_permanently_saturated():
    dmm = FakeDMM(readings=[9.9e37] * 6)
    src = FakeSource()

    results, _ = _measure_branch(
        dmm, src, 'current',
        X_start=1, X_stop=1, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    # Ключевая регрессия issue #3: раньше сюда попадало ~9.9e37 как будто
    # это настоящее измерение. Теперь — честный NaN, а не гигантское число.
    assert math.isnan(results[0]['I_meas_A'])


# ----------------------------------------------------------------------
# Предсказание диапазона по ожидаемому значению (Ф1 п.6, defect #1)
# ----------------------------------------------------------------------

def test_measure_branch_preselects_range_from_expected_value_when_ratio_known():
    dmm = FakeDMM(readings=[0.025] * 3)  # X_set=50, ratio=2000 -> ожидаемый выход 0.025
    src = FakeSource()

    _measure_branch(
        dmm, src, 'current',
        X_start=50, X_stop=50, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
        ratio=2000.0,
    )

    assert (0.025, True) in dmm.auto_range_calls


def test_measure_branch_preselection_happens_before_excitation_and_measurement():
    """
    Это и есть сама починка defect #1: диапазон должен выбираться ДО того,
    как выставлено возбуждение и снято показание — по ожидаемому значению
    следующей точки, а не по факту предыдущей.
    """
    order = []

    class OrderedDMM(FakeDMM):
        def auto_range(self, measured_current, is_first=False):
            order.append(('auto_range', measured_current, is_first))
            super().auto_range(measured_current, is_first)

        def measure_current(self):
            order.append(('measure',))
            return super().measure_current()

    class OrderedSource(FakeSource):
        def set_current(self, current):
            order.append(('set_current', current))
            super().set_current(current)

    dmm = OrderedDMM(readings=[0.025] * 3)
    src = OrderedSource()

    _measure_branch(
        dmm, src, 'current',
        X_start=50, X_stop=50, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
        ratio=2000.0,
    )

    predict_idx = order.index(('auto_range', 0.025, True))
    excite_idx = order.index(('set_current', 50))
    measure_idx = order.index(('measure',))
    assert predict_idx < excite_idx < measure_idx


def test_measure_branch_skips_preselection_without_ratio():
    # Без ratio предсказывать нечего — остаётся только подстройка по факту
    # после измерения (is_first только на первой точке прохода), как раньше.
    dmm = FakeDMM(readings=[1.0] * 3)
    src = FakeSource()

    _measure_branch(
        dmm, src, 'current',
        X_start=1, X_stop=1, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    # Единственный auto_range-вызов — послеизмерительный (is_first=True для
    # первой точки прохода), не предсказательный.
    assert len(dmm.auto_range_calls) == 1
    assert dmm.auto_range_calls[0] == (1.0, True)


# ----------------------------------------------------------------------
# run_measurement
# ----------------------------------------------------------------------

def test_run_measurement_runs_forward_then_reverse_and_shuts_down():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    results, _ = run_measurement(
        dmm, src, relay, 'current',
        X_start=0, X_stop=1, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
    )

    assert relay.calls == ['forward', 'reverse', 'off']
    assert ('shutdown',) in src.calls
    branches = [r['Branch'] for r in results]
    # X=0 снимается один раз отдельно (без реле/источника), а не по разу
    # на каждую ветвь — см. _measure_zero_point.
    assert branches.count('zero') == 1
    assert branches.count('forward') == 1
    assert branches.count('reverse') == 1
    signs = {r['Branch']: [] for r in results}
    for r in results:
        signs[r['Branch']].append(r['X_set'])
    assert signs['zero'] == [0.0]
    assert signs['forward'] == [1]
    assert signs['reverse'] == [-1]


def test_run_measurement_all_zero_sweep_never_touches_relay_or_source_output():
    """Весь свип — это только X=0: реле вообще не коммутируется, выход источника не включается."""
    dmm = FakeDMM(readings=[0.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    results, _ = run_measurement(
        dmm, src, relay, 'current',
        X_start=0, X_stop=0, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
    )

    assert relay.calls == ['off']  # только финальный cleanup, ни forward, ни reverse
    assert ('output_on',) not in src.calls
    assert [r['Branch'] for r in results] == ['zero']


def test_run_measurement_current_excitation_passes_v_limit_to_setup():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    run_measurement(
        dmm, src, relay, 'current',
        X_start=0, X_stop=0, X_step=1,
        V_limit=7.5, delay=0, cooling_delay=0,
    )

    assert src.calls[0] == ('setup', 7.5)


def test_run_measurement_voltage_excitation_uses_x_stop_as_setup_limit():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    run_measurement(
        dmm, src, relay, 'voltage',
        X_start=0, X_stop=64, X_step=64,
        V_limit=0.0, delay=0, cooling_delay=0,
    )

    assert src.calls[0] == ('setup', 64)


def test_run_measurement_shuts_down_source_and_relay_even_on_failure():
    class ExplodingRelay(FakeRelay):
        def reverse(self):
            raise RuntimeError("relay comm failure")

    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = ExplodingRelay()

    with pytest.raises(RuntimeError):
        # X_stop=0 сам по себе (без ненулевых точек) вообще не коммутирует
        # реле — нужен хотя бы один шаг за пределами нуля, чтобы дойти до
        # relay.reverse() и проверить, что failure тут не мешает cleanup.
        run_measurement(
            dmm, src, relay, 'current',
            X_start=0, X_stop=1, X_step=1,
            V_limit=5.0, delay=0, cooling_delay=0,
        )

    assert ('shutdown',) in src.calls
    assert 'off' in relay.calls


def test_run_measurement_unknown_excitation_type_raises_value_error():
    dmm = FakeDMM(readings=[])
    src = FakeSource()
    relay = FakeRelay()

    with pytest.raises(ValueError):
        run_measurement(
            dmm, src, relay, 'bogus',
            X_start=0, X_stop=0, X_step=1,
            V_limit=5.0, delay=0, cooling_delay=0,
        )


def test_excitation_units_mapping():
    assert EXCITATION_UNITS == {'current': 'A', 'voltage': 'V'}


# ----------------------------------------------------------------------
# Отсечка по погрешности (stop_on_error) — пришло из ветки
# test_deepseek_hermes без единого теста, покрывается здесь.
# ----------------------------------------------------------------------

def test_stop_on_error_aborts_branch_and_drops_the_bad_point():
    # ratio=1000 -> ожидаемый выход X/1000. Точки 1 и 2 А в допуске,
    # на 3 А датчик отдаёт вдвое меньше положенного (0.0015 вместо 0.003).
    dmm = FakeDMM(readings=[0.001] * 3 + [0.002] * 3 + [0.0015] * 3)
    src = FakeSource()

    results, aborted = _measure_branch(
        dmm, src, 'current',
        X_start=1, X_stop=3, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
        ratio=1000.0, stop_on_error=True, error_threshold=1.0,
    )

    # Точка, на которой сработала отсечка, в результат НЕ попадает: она не
    # характеристика датчика, а свидетельство того, что мерить дальше нечего.
    assert [r['X_set'] for r in results] == [1.0, 2.0]
    assert aborted is not None
    assert '3' in aborted  # причина называет точку, на которой встали


def test_stop_on_error_does_not_fire_when_sensor_is_within_threshold():
    dmm = FakeDMM(readings=[0.001] * 3 + [0.002] * 3)
    src = FakeSource()

    results, aborted = _measure_branch(
        dmm, src, 'current',
        X_start=1, X_stop=2, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
        ratio=1000.0, stop_on_error=True, error_threshold=1.0,
    )

    assert len(results) == 2
    assert aborted is None


def test_stop_on_error_without_ratio_is_silently_skipped():
    # Без коэффициента преобразования ожидаемый выход посчитать не из чего,
    # поэтому проверка не должна ни падать, ни рубить измерение.
    dmm = FakeDMM(readings=[999.0] * 3)
    src = FakeSource()

    results, aborted = _measure_branch(
        dmm, src, 'current',
        X_start=1, X_stop=1, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
        ratio=None, stop_on_error=True, error_threshold=1.0,
    )

    assert len(results) == 1
    assert aborted is None


def test_stop_on_error_ignores_nan_readings():
    # NaN — это сбой связи, а не выход датчика за допуск. Рубить измерение
    # по нему нельзя: иначе одна потерянная посылка обрывает весь прогон.
    dmm = FakeDMM(readings=[Exception("timeout")] * 3)
    src = FakeSource()

    results, aborted = _measure_branch(
        dmm, src, 'current',
        X_start=1, X_stop=1, X_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
        ratio=1000.0, stop_on_error=True, error_threshold=1.0,
    )

    assert aborted is None
    assert math.isnan(results[0]['I_meas_A'])


def test_run_measurement_propagates_abort_and_skips_reverse_branch():
    dmm = FakeDMM(readings=[0.5] * 100)  # заведомо мимо ожидаемых 0.001
    src = FakeSource()
    relay = FakeRelay()

    results, aborted = run_measurement(
        dmm, src, relay, 'current',
        X_start=0, X_stop=1, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
        ratio=1000.0, stop_on_error=True, error_threshold=1.0,
    )

    assert aborted is not None
    # Обратную ветвь не начинали — датчик уже признан негодным.
    assert 'reverse' not in relay.calls
    # Но источник и реле всё равно погашены (finally).
    assert ('shutdown',) in src.calls
    assert 'off' in relay.calls


# ----------------------------------------------------------------------
# Режим без реле (use_relay=False)
# ----------------------------------------------------------------------

def test_use_relay_false_measures_only_forward_and_never_commutates():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    results, aborted = run_measurement(
        dmm, src, relay, 'current',
        X_start=0, X_stop=1, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
        use_relay=False,
    )

    assert aborted is None
    assert relay.calls == []  # ни forward, ни reverse, ни даже off
    # Ноль реле не нужен в любом случае, поэтому снимается как обычно.
    assert [r['Branch'] for r in results] == ['zero', 'forward']


def test_use_relay_false_still_shuts_down_source():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    run_measurement(
        dmm, src, relay, 'current',
        X_start=0, X_stop=1, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
        use_relay=False,
    )

    assert ('shutdown',) in src.calls


# ----------------------------------------------------------------------
# log_callback
# ----------------------------------------------------------------------

def test_log_callback_receives_progress_instead_of_stdout(capsys):
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    lines = []

    run_measurement(
        dmm, src, relay, 'current',
        X_start=0, X_stop=1, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
        log_callback=lines.append,
    )

    assert any('forward' in line for line in lines)
    assert any('zero' in line for line in lines)
    # При заданном колбэке ход измерения в stdout не дублируется.
    assert capsys.readouterr().out == ''


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
    # Аварийный останов закрывает сессии из другого потока, после чего цикл
    # падает, не успев ничего вернуть. Всё, что было снято до этого момента,
    # обязано уцелеть — как раз эти точки и объясняют, почему нажали «Стоп».
    #
    # Падает именно источник, а не мультиметр: чтения обёрнуты в
    # «except Exception: pass» (сбой связи = NaN, а не обрыв измерения), а
    # уставка на источник — нет. Так же и в жизни: safety.emergency_shutdown
    # гасит и закрывает источник первым.
    dmm = FakeDMM(readings=[1.0] * 100)
    src = DyingSource(alive_setpoints=2)
    relay = FakeRelay()
    sink = []

    with pytest.raises(RuntimeError):
        run_measurement(
            dmm, src, relay, 'current',
            X_start=1, X_stop=10, X_step=1,
            V_limit=5.0, delay=0, cooling_delay=0,
            results_sink=sink,
        )

    assert [r['X_set'] for r in sink] == [1.0, 2.0]


def test_dead_dmm_yields_nan_points_instead_of_crashing_the_sweep():
    # Обратная сторона того же решения, зафиксирована сознательно: закрытый
    # мультиметр не обрывает проход, а даёт NaN. Сбой связи не должен
    # выдаваться за провал характеристики, но и ронять измерение из-за одной
    # потерянной посылки нельзя.
    class DeadDMM(FakeDMM):
        def measure_current(self):
            raise RuntimeError("сессия мультиметра закрыта")

    dmm = DeadDMM(readings=[])
    src = FakeSource()
    relay = FakeRelay()

    results, aborted = run_measurement(
        dmm, src, relay, 'current',
        X_start=1, X_stop=2, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
    )

    assert aborted is None
    assert all(math.isnan(r['I_meas_A']) for r in results)


def test_results_sink_gets_the_same_points_as_the_return_value():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()
    sink = []

    results, _ = run_measurement(
        dmm, src, relay, 'current',
        X_start=0, X_stop=1, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
        results_sink=sink,
    )

    assert sink == results


def test_cleanup_failures_do_not_mask_the_real_error():
    # Если аварийный останов уже закрыл сессии, src.shutdown()/relay.off() в
    # finally тоже упадут. Наружу должна уйти настоящая причина выхода из
    # цикла, а не вторичное падение уборки — иначе в журнале оператора вместо
    # «источник не отвечает» окажется «порт реле закрыт», и разбираться он
    # будет не с тем.
    class DeadEverything(FakeSource):
        def set_current(self, current):
            raise RuntimeError("НАСТОЯЩАЯ ПРИЧИНА: сессия источника закрыта")

        def shutdown(self):
            raise RuntimeError("вторичное падение уборки")

    class DeadRelay(FakeRelay):
        def off(self):
            raise RuntimeError("вторичное падение уборки")

    with pytest.raises(RuntimeError, match="НАСТОЯЩАЯ ПРИЧИНА"):
        run_measurement(
            FakeDMM(readings=[1.0] * 100), DeadEverything(), DeadRelay(), 'current',
            X_start=1, X_stop=1, X_step=1,
            V_limit=5.0, delay=0, cooling_delay=0,
        )
