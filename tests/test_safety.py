"""
Тесты аварийного останова.

Главное, что здесь проверяется, — не «счастливый путь», а поведение при
сломанном железе: аварийный останов ценен ровно тем, что доводит
последовательность до конца, когда часть приборов уже не отвечает.
"""
import pytest

from safety import EMERGENCY_VISA_TIMEOUT_MS, emergency_shutdown


class FakeVisaSession:
    def __init__(self):
        self.timeout = 5000


class FakeSource:
    """Источник; любой метод можно заставить падать через fail={'имя'}."""

    def __init__(self, fail=frozenset()):
        self.calls = []
        self.fail = set(fail)
        self.instr = FakeVisaSession()

    def _maybe_fail(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"прибор не отвечает на {name}")

    def output_off(self):
        self._maybe_fail('output_off')

    def set_current(self, value):
        self._maybe_fail('set_current')

    def close(self):
        self._maybe_fail('close')


class FakeVoltageSource(FakeSource):
    def set_voltage(self, value):
        self._maybe_fail('set_voltage')


class FakeDMM:
    def __init__(self, fail=frozenset()):
        self.calls = []
        self.fail = set(fail)
        self.instr = FakeVisaSession()

    def close(self):
        self.calls.append('close')
        if 'close' in self.fail:
            raise RuntimeError("мультиметр не отвечает")


class FakeRelay:
    def __init__(self, fail=frozenset()):
        self.calls = []
        self.fail = set(fail)

    def emergency_off(self):
        self.calls.append('emergency_off')
        if 'emergency_off' in self.fail:
            raise RuntimeError("порт реле мёртв")

    def drop(self):
        self.calls.append('drop')
        if 'drop' in self.fail:
            raise RuntimeError("порт уже закрыт")

    def close(self):
        self.calls.append('close')


# ----------------------------------------------------------------------
# Порядок — он продиктован физикой, а не удобством
# ----------------------------------------------------------------------

def test_source_output_is_killed_before_relay_opens():
    # Размыкать реле под током — дуга на контактах. Выход источника обязан
    # погаснуть раньше, чем реле начнёт размыкаться.
    order = []
    src, relay = FakeSource(), FakeRelay()
    src.output_off = lambda: order.append('output_off')
    relay.emergency_off = lambda: order.append('relay_off')

    emergency_shutdown(src=src, relay=relay)

    assert order == ['output_off', 'relay_off']


def test_setpoints_are_zeroed_only_after_output_is_off():
    src = FakeSource()
    emergency_shutdown(src=src)

    assert src.calls.index('output_off') < src.calls.index('set_current')


def test_voltage_source_gets_both_setpoints_zeroed():
    src = FakeVoltageSource()
    emergency_shutdown(src=src)

    assert 'set_current' in src.calls
    assert 'set_voltage' in src.calls


# ----------------------------------------------------------------------
# Ни один сбой не мешает следующему шагу
# ----------------------------------------------------------------------

def test_dead_source_still_lets_the_relay_open():
    # Самый важный тест модуля: зависший источник не должен стоить
    # разомкнутого реле.
    src = FakeSource(fail={'output_off'})
    relay = FakeRelay()

    emergency_shutdown(src=src, relay=relay)

    assert 'emergency_off' in relay.calls


def test_relay_that_refuses_the_command_gets_its_port_dropped():
    relay = FakeRelay(fail={'emergency_off'})

    steps = emergency_shutdown(relay=relay)

    assert relay.calls == ['emergency_off', 'drop', 'close']
    assert any('принудительно закрыт' in s for s in steps)


def test_relay_failure_is_reported_not_swallowed():
    # Молчаливое проглатывание здесь опаснее самой ошибки: оператор решит,
    # что реле разомкнуто, а оно под током.
    relay = FakeRelay(fail={'emergency_off', 'drop'})

    steps = emergency_shutdown(relay=relay)

    assert any('НЕ УДАЛОСЬ разомкнуть реле' in s for s in steps)
    assert any('НЕ УДАЛОСЬ закрыть порт реле' in s for s in steps)


def test_everything_broken_does_not_raise():
    src = FakeSource(fail={'output_off', 'set_current', 'close'})
    dmm = FakeDMM(fail={'close'})
    relay = FakeRelay(fail={'emergency_off', 'drop'})

    steps = emergency_shutdown(src=src, relay=relay, dmm=dmm)

    # Ни одного исключения наружу — иначе аварийный останов сам стал бы
    # причиной необработанного падения в обработчике кнопки «Стоп».
    assert steps
    assert all(instrument.calls for instrument in (src, dmm, relay))


def test_missing_instruments_are_skipped_not_crashed():
    # Аварийный останов вызывается и до того, как приборы успели открыться.
    assert emergency_shutdown(src=None, relay=None, dmm=None) == []


# ----------------------------------------------------------------------
# Никаких ожиданий
# ----------------------------------------------------------------------

def test_visa_timeout_is_shortened_before_the_sequence_runs():
    # Иначе один неотвечающий прибор задержит размыкание реле на свои
    # штатные 5 секунд.
    src, dmm = FakeSource(), FakeDMM()

    emergency_shutdown(src=src, dmm=dmm)

    assert src.instr.timeout == EMERGENCY_VISA_TIMEOUT_MS
    assert dmm.instr.timeout == EMERGENCY_VISA_TIMEOUT_MS


def test_emergency_never_calls_the_waiting_relay_off():
    # RelayController.off() ждёт 'OK' до 1.5 с — в аварийной
    # последовательности ему делать нечего.
    class StrictRelay(FakeRelay):
        def off(self):
            raise AssertionError("аварийный останов не должен ждать ответа реле")

    relay = StrictRelay()
    emergency_shutdown(relay=relay)

    assert relay.calls == ['emergency_off', 'close']


def test_instrument_without_visa_session_does_not_break_the_sequence():
    class NoSession:
        def __init__(self):
            self.calls = []

        def output_off(self):
            self.calls.append('output_off')

        def close(self):
            self.calls.append('close')

    src = NoSession()
    emergency_shutdown(src=src)

    assert src.calls == ['output_off', 'close']


def test_connections_are_closed_for_every_instrument():
    src, dmm, relay = FakeSource(), FakeDMM(), FakeRelay()

    emergency_shutdown(src=src, relay=relay, dmm=dmm)

    assert 'close' in src.calls
    assert 'close' in dmm.calls
    assert 'close' in relay.calls


def test_log_callback_receives_every_step():
    lines = []
    emergency_shutdown(src=FakeSource(), relay=FakeRelay(), dmm=FakeDMM(),
                       log=lines.append)

    assert lines
    assert all(line.startswith('[АВАРИЙНЫЙ ОСТАНОВ]') for line in lines)


# ----------------------------------------------------------------------
# SessionHandle — ручка, через которую стенд гасится СНАРУЖИ цикла
# ----------------------------------------------------------------------

def test_session_handle_with_no_instruments_yet_is_harmless():
    # «Стоп» может быть нажат, пока приборы ещё открываются.
    from orchestrate import SessionHandle

    handle = SessionHandle()
    assert handle.emergency_stop() == []
    assert handle.stopped is True


def test_session_handle_marks_stopped_and_kills_the_stand():
    from orchestrate import SessionHandle

    handle = SessionHandle()
    handle.src, handle.relay, handle.dmm = FakeSource(), FakeRelay(), FakeDMM()

    handle.emergency_stop()

    assert handle.stopped is True
    assert 'output_off' in handle.src.calls
    assert 'emergency_off' in handle.relay.calls


def test_session_handle_survives_being_stopped_twice():
    # Двойное нажатие «Стоп» не должно ронять обработчик кнопки.
    from orchestrate import SessionHandle

    handle = SessionHandle()
    handle.src, handle.relay = FakeSource(), FakeRelay()

    handle.emergency_stop()
    handle.emergency_stop()  # сессии уже закрыты — повтор безвреден

    assert handle.stopped is True
