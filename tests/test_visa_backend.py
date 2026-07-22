import visa_backend
from visa_backend import VisaStatus, check_visa


# ----------------------------------------------------------------------
# VisaStatus — форматирование вердикта (без зависимости от наличия NI-VISA)
# ----------------------------------------------------------------------

def test_status_summary_ok():
    s = VisaStatus(ok=True, backend="NI-VISA / IVI", resource_count=2)
    line = s.summary_line()
    assert "OK" in line
    assert "2" in line


def test_status_summary_not_found():
    s = VisaStatus(ok=False)
    assert s.summary_line() == "NI-VISA: НЕ НАЙДЕНА"


# ----------------------------------------------------------------------
# check_visa — не бросает исключений и всегда возвращает заполненный вердикт
# ----------------------------------------------------------------------

def test_check_visa_returns_status_without_raising():
    s = check_visa()
    assert isinstance(s, VisaStatus)
    assert isinstance(s.ok, bool)
    assert s.message  # всегда есть текст, пригодный для показа


def test_check_visa_pyvisa_py_treated_as_not_found(monkeypatch):
    """
    Если pyvisa случайно загрузила чисто-питоновский бэкенд pyvisa-py, для
    наших целей это 'NI-VISA не найдена' (нельзя надёжно работать с USB-TMC).
    Эмулируем такой ResourceManager через подмену внутренних хелперов.
    """
    class _FakeVisalib:
        pass
    # модуль подделки должен содержать 'pyvisa_py', чтобы _describe_backend
    # распознал запасной бэкенд
    _FakeVisalib.__module__ = "pyvisa_py.highlevel"

    class _FakeRM:
        visalib = _FakeVisalib()
        def list_resources(self):
            return ()
        def close(self):
            pass

    monkeypatch.setattr(visa_backend, "_open_resource_manager", lambda: _FakeRM())
    s = check_visa()
    assert s.ok is False
    assert s.is_fallback is True


def test_check_visa_reports_ok_for_ivi_backend(monkeypatch):
    class _FakeVisalib:
        library_path = "C:/Windows/System32/visa64.dll"
    _FakeVisalib.__module__ = "pyvisa.ctwrapper.highlevel"

    class _FakeRM:
        visalib = _FakeVisalib()
        def list_resources(self):
            return ("USB0::0x1234::0x5678::SN::INSTR",)
        def close(self):
            pass

    monkeypatch.setattr(visa_backend, "_open_resource_manager", lambda: _FakeRM())
    s = check_visa()
    assert s.ok is True
    assert s.is_fallback is False
    assert s.resource_count == 1
