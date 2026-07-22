"""
Единая точка создания и проверки VISA-бэкенда.

Программа работает ИСКЛЮЧИТЕЛЬНО через NI-VISA (полноценная реализация VISA
от National Instruments) + pyvisa. Никакого запасного чисто-питоновского
бэкенда (pyvisa-py) не используется: приборы подключены по USB-TMC, и
надёжно с ними работает только настоящая NI-VISA.

Отсюда две задачи модуля:

  1. check_visa() — до любых операций с железом убедиться, что pyvisa видит
     установленную и рабочую NI-VISA. Возвращает подробный вердикт, который
     показывается и в CLI, и в GUI, и в предполётной проверке.
  2. make_resource_manager() — создать pyvisa.ResourceManager на NI-VISA;
     при отсутствии бэкенда бросить понятную ошибку с инструкцией.

Как отличить настоящую NI-VISA от случайно установленного pyvisa-py:
pyvisa грузит бэкенд классом *VisaLibrary. У NI-VISA (и любой IVI-VISA)
модуль реализации — pyvisa.ctwrapper.*, у pyvisa-py — pyvisa_py.*. Если
загрузился pyvisa_py, для наших целей это "NI-VISA не найдена".
"""
from dataclasses import dataclass, field
from typing import List, Optional


# Явный спецификатор IVI-бэкенда (NI-VISA и совместимые). Заставляет pyvisa
# грузить именно настоящую VISA, а не откатываться на pyvisa-py, если тот
# случайно оказался в окружении.
_IVI_BACKEND = "@ivi"

INSTALL_HINT = (
    "NI-VISA не обнаружена. Установите NI-VISA (National Instruments), "
    "перезагрузите ПК/приборы и запустите программу заново.\n"
    "Дистрибутив NI-VISA поставляется вместе с этой программой (см. README, "
    "раздел «Установка NI-VISA»). После установки в «NI MAX» приборы должны "
    "быть видны как USB-ресурсы."
)


@dataclass
class VisaStatus:
    """Результат проверки VISA-бэкенда."""
    ok: bool
    backend: Optional[str] = None          # человекочитаемое имя бэкенда
    library_path: Optional[str] = None     # путь к visaXX.dll / .so, если известен
    resource_count: Optional[int] = None   # сколько VISA-ресурсов сейчас видно
    resources: List[str] = field(default_factory=list)
    message: str = ""                      # готовый к показу текст вердикта
    is_fallback: bool = False              # True, если загрузился pyvisa-py (для нас == не ok)

    def summary_line(self) -> str:
        """Короткая строка статуса для заголовка/статус-бара."""
        if self.ok:
            n = self.resource_count if self.resource_count is not None else "?"
            return f"NI-VISA: OK ({self.backend}); ресурсов видно: {n}"
        return "NI-VISA: НЕ НАЙДЕНА"


def _describe_backend(rm) -> tuple:
    """
    Возвращает (backend_name, library_path, is_pyvisa_py) для созданного
    ResourceManager, аккуратно обрабатывая различия версий pyvisa.
    """
    visalib = getattr(rm, "visalib", None)
    module = type(visalib).__module__ if visalib is not None else ""
    is_pyvisa_py = "pyvisa_py" in module

    library_path = None
    for attr in ("library_path", "_library_path"):
        val = getattr(visalib, attr, None)
        if val:
            library_path = str(val)
            break

    if is_pyvisa_py:
        backend_name = "pyvisa-py (запасной, НЕ NI-VISA)"
    else:
        backend_name = "NI-VISA / IVI"
    return backend_name, library_path, is_pyvisa_py


def _open_resource_manager():
    """
    Пытается создать ResourceManager именно на IVI (NI-VISA). Если явный
    спецификатор '@ivi' не поддерживается данной версией pyvisa, откатывается
    на бэкенд по умолчанию (а распознавание pyvisa-py делается отдельно в
    _describe_backend, чтобы не выдать запасной бэкенд за NI-VISA).
    """
    import pyvisa

    try:
        return pyvisa.ResourceManager(_IVI_BACKEND)
    except Exception:
        # '@ivi' может быть не распознан старыми pyvisa — пробуем дефолт.
        return pyvisa.ResourceManager()


def check_visa() -> VisaStatus:
    """
    Проверяет доступность рабочего NI-VISA-бэкенда, не бросая исключений.
    Всегда возвращает VisaStatus с заполненным полем message.

    Порядок:
      1. импорт pyvisa;
      2. создание ResourceManager на NI-VISA;
      3. определение реального бэкенда (NI-VISA vs случайный pyvisa-py);
      4. пробный list_resources() — это и проверка, что библиотека реально
         грузится и отвечает.
    """
    try:
        import pyvisa  # noqa: F401
    except Exception as e:
        return VisaStatus(
            ok=False,
            message=f"Не удалось импортировать pyvisa: {e}\n{INSTALL_HINT}",
        )

    try:
        rm = _open_resource_manager()
    except Exception as e:
        return VisaStatus(
            ok=False,
            message=f"pyvisa не смогла загрузить VISA-библиотеку: {e}\n\n{INSTALL_HINT}",
        )

    backend_name, library_path, is_pyvisa_py = _describe_backend(rm)

    if is_pyvisa_py:
        try:
            rm.close()
        except Exception:
            pass
        return VisaStatus(
            ok=False,
            backend=backend_name,
            library_path=library_path,
            is_fallback=True,
            message=(
                "Загрузился запасной бэкенд pyvisa-py, а не NI-VISA. Для "
                "надёжной работы с USB-TMC приборами это неприемлемо.\n\n"
                + INSTALL_HINT
            ),
        )

    try:
        resources = list(rm.list_resources())
    except Exception as e:
        try:
            rm.close()
        except Exception:
            pass
        return VisaStatus(
            ok=False,
            backend=backend_name,
            library_path=library_path,
            message=(
                f"NI-VISA загрузилась, но опрос ресурсов не удался: {e}\n"
                "Проверьте подключение приборов и работу службы NI-VISA (NI MAX)."
            ),
        )

    try:
        rm.close()
    except Exception:
        pass

    lib_part = f"\nБиблиотека: {library_path}" if library_path else ""
    if resources:
        res_part = "Видимые VISA-ресурсы:\n  " + "\n  ".join(resources)
    else:
        res_part = (
            "VISA-ресурсы не обнаружены. Бэкенд рабочий, но ни один прибор "
            "сейчас не подключён/не включён (это нормально до подключения)."
        )

    return VisaStatus(
        ok=True,
        backend=backend_name,
        library_path=library_path,
        resource_count=len(resources),
        resources=resources,
        message=f"NI-VISA обнаружена и работает.{lib_part}\n\n{res_part}",
    )


def make_resource_manager():
    """
    Создаёт pyvisa.ResourceManager на NI-VISA для реальной работы с приборами.

    Бросает RuntimeError с понятным текстом, если рабочей NI-VISA нет — чтобы
    ни CLI, ни GUI не начинали измерение на неисправном бэкенде.
    """
    status = check_visa()
    if not status.ok:
        raise RuntimeError(status.message)

    import pyvisa
    try:
        return pyvisa.ResourceManager(_IVI_BACKEND)
    except Exception:
        return pyvisa.ResourceManager()
