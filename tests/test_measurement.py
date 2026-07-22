import math

import pytest

from measurement import run_measurement, _measure_branch, EXCITATION_UNITS


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

    results = _measure_branch(
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

    results = _measure_branch(
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

    results = _measure_branch(
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

    results = _measure_branch(
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
# run_measurement
# ----------------------------------------------------------------------

def test_run_measurement_runs_forward_then_reverse_and_shuts_down():
    dmm = FakeDMM(readings=[1.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    results = run_measurement(
        dmm, src, relay, 'current',
        X_start=0, X_stop=1, X_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
    )

    assert relay.calls == ['forward', 'reverse', 'off']
    assert ('shutdown',) in src.calls
    branches = [r['Branch'] for r in results]
    assert branches.count('forward') == 2
    assert branches.count('reverse') == 2
    signs = {r['Branch']: [] for r in results}
    for r in results:
        signs[r['Branch']].append(r['X_set'])
    assert signs['forward'] == [0, 1]
    assert signs['reverse'] == [0, -1]


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
        run_measurement(
            dmm, src, relay, 'current',
            X_start=0, X_stop=0, X_step=1,
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
