import pytest

from limits import (
    RELAY_MAX_CURRENT_A,
    RELAY_WARN_CURRENT_A,
    VOLTAGE_SOURCE_SAFE_CEILING_V,
    relay_current_block_reason,
    relay_current_warning,
    voltage_ceiling_block_reason,
)


# ----------------------------------------------------------------------
# Жёсткий запрет — 800 А
# ----------------------------------------------------------------------

def test_below_warning_threshold_is_neither_blocked_nor_warned():
    assert relay_current_block_reason(399.9) is None
    assert relay_current_warning(399.9) is None


def test_at_warning_threshold_exactly_is_not_yet_a_warning():
    # Порог "свыше", а не "от" — ровно 400 А ещё в пределах паспорта.
    assert relay_current_warning(RELAY_WARN_CURRENT_A) is None
    assert relay_current_block_reason(RELAY_WARN_CURRENT_A) is None


def test_just_above_warning_threshold_warns_but_does_not_block():
    reason = relay_current_block_reason(400.1)
    warning = relay_current_warning(400.1)

    assert reason is None
    assert warning is not None
    assert "400" in warning


def test_at_hard_limit_exactly_is_not_blocked():
    # 800 А включительно — ещё разрешено, запрет начинается СВЫШЕ.
    assert relay_current_block_reason(RELAY_MAX_CURRENT_A) is None
    assert relay_current_warning(RELAY_MAX_CURRENT_A) is not None


def test_above_hard_limit_is_blocked_unconditionally():
    reason = relay_current_block_reason(800.1)

    assert reason is not None
    assert "800" in reason


def test_above_hard_limit_gives_only_block_not_a_duplicate_warning():
    # Выше жёсткого предела оператору не нужны два сообщения об одном и том
    # же — только блокирующая причина отказа.
    assert relay_current_block_reason(1020.0) is not None
    assert relay_current_warning(1020.0) is None


def test_none_input_is_not_a_limit_violation():
    # None означает "неизвестно" (например, режим без явной уставки тока),
    # а не "ноль" — не должно ни блокировать, ни предупреждать.
    assert relay_current_block_reason(None) is None
    assert relay_current_warning(None) is None


def test_source_can_physically_exceed_the_relay_limit():
    # АКИП-1162-10-1020 способен на 1020 А — больше, чем держит реле.
    # Ограничивающий фактор для тока — реле, а не источник.
    assert relay_current_block_reason(1020.0) is not None


# ----------------------------------------------------------------------
# voltage_ceiling_block_reason (п.35) — рабочий потолок 60 В, независимый
# от паспортного предела конкретного источника напряжения
# ----------------------------------------------------------------------

def test_voltage_ceiling_constant_is_60():
    assert VOLTAGE_SOURCE_SAFE_CEILING_V == 60.0


def test_voltage_at_exactly_ceiling_is_allowed():
    assert voltage_ceiling_block_reason(60.0) is None


def test_voltage_just_above_ceiling_is_blocked():
    reason = voltage_ceiling_block_reason(60.1)
    assert reason is not None
    assert '60' in reason


def test_voltage_well_below_ceiling_is_allowed():
    assert voltage_ceiling_block_reason(30.0) is None


def test_voltage_ceiling_none_input_is_not_a_violation():
    assert voltage_ceiling_block_reason(None) is None
