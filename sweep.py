"""
Планировщик развёртки — чистая функция без обращения к железу.

Раньше развёртка считалась прямо в измерительном цикле:
`num_steps = int(round((X_stop - X_start) / X_step)) + 1`, знак навешивался
параметром `sign`, а обе полярности были жёстко зашиты как "сначала
forward(0..X_stop), потом reverse(0..X_stop)". Этого не хватало ни для
произвольного знака/порядка X_start/X_stop (п.17), ни для выбора одной
полярности (п.8), ни для разных схем прохода (п.19).

Здесь вся эта комбинаторика решается один раз, в отрыве от приборов:
`plan_sweep()` принимает параметры развёртки и возвращает готовый список
`SweepPoint` — что и в каком порядке измерять, какое положение реле для
каждой точки. Измерительный цикл (`measurement.py`) становится тупым
исполнителем этого плана: не принимает решений о комбинаторике сам.

## Модель

X_start/X_stop — ЗНАКОВЫЕ значения, буквально то, что ввёл оператор (любой
знак, любой порядок: -250→250, 0→250, 250→-250, -25→25, 150→250, ...).
X_step — положительная величина шага.

Если интервал [X_start, X_stop] уже сам по себе захватывает оба знака
(например -250→250) — это уже полное двуполярное описание, и `branch`
работает как ФИЛЬТР (не порождает новых точек): 'positive' оставляет
X_set >= 0, 'negative' — X_set <= 0, 'both' (по умолчанию) — всё как есть.

Если интервал односторонний (X_start и X_stop одного знака, либо один из
них равен нулю) — это "развёртка по модулю", и `branch='both'` порождает
ВТОРУЮ, зеркальную по знаку развёртку через реле, применяя ОДИН из четырёх
именованных пресетов направления (см. DirectionPreset) — если односторонняя
развёртка заякорена в нуле (X_start==0 или X_stop==0). Если не заякорена
(например 150→250) — пресет неприменим (нечего "заходить в ноль"), и both
даёт буквальное зеркало исходного диапазона без хореографии пресета.

Ноль измеряется тогда и только тогда, когда развёртка через него реально
проходит — попадает он на шаг или нет, минимально достаточным числом раз
для выбранного пресета (см. DirectionPreset). Модуль конечной точки
измеряется всегда точно, без сноса плавающей точкой (см. _raw_pass).
"""
import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import List, Optional


class Branch(str, Enum):
    """Какая полярность(и) измеряется. Заменяет булев use_relay (п.8)."""
    POSITIVE = 'positive'
    NEGATIVE = 'negative'
    BOTH = 'both'
    NO_RELAY = 'no_relay'  # реле физически нет — источник однополярный, коммутации не бывает вовсе


class DirectionPreset(str, Enum):
    """
    Схема прохода для одностороннего (заякоренного в нуле) свипа при
    branch=BOTH — см. PLAN_V2.md, В-3. Для развёртки, уже охватывающей оба
    знака буквально (-250→250), пресет не участвует (branch работает как
    фильтр, см. модуль-докстринг).
    """
    DIVERGING = 'diverging'      # 0→+X, затем 0→−X (поведение по умолчанию, как в v1.4ae)
    CONVERGING = 'converging'    # +X→0, затем 0→−X — непрерывный переход через ноль
    DESCENDING = 'descending'    # +X→0, затем −X→0 — обе ветви к нулю с разных сторон
    FULL_CYCLE = 'full_cycle'    # 0→+X→0→−X→0 — петля гистерезиса


@dataclass(frozen=True)
class SweepPoint:
    """
    Одна точка плана.

    x_set       — знаковая уставка, что пишется в CSV как X_set.
    magnitude   — |x_set|, то, что реально уходит на источник (источник не
                  умеет отрицательные значения).
    relay       — 'forward' (x_set > 0), 'reverse' (x_set < 0) или None
                  (x_set == 0 — реле не коммутируется вовсе).
    is_zero     — x_set == 0. На такой точке источник и реле не трогаются
                  (см. измерительный цикл): нет возбуждения — нет
                  полярности, которую можно перепутать или которой можно
                  повредить.
    is_endpoint — это самая дальняя точка своей ветви (|x_set| == заданный
                  для этой ветви максимум) — гарантированно измеряется
                  точно, не приблизительно (см. _raw_pass).
    """
    x_set: float
    magnitude: float
    relay: Optional[str]
    is_zero: bool
    is_endpoint: bool


def _raw_pass(start: float, stop: float, step: float) -> List[float]:
    """
    Один буквальный проход от start до stop (оба знаковые, любого порядка)
    с шагом step (положительная величина).

    Три гарантии (п.17, п.18):
      1. stop измеряется ТОЧНО — не как start + n*step (что на нецелых шагах
         вроде 0.1 накапливает погрешность плавающей точки и может не
         попасть точно в конец), а как буквальное значение stop.
      2. Каждая промежуточная точка считается от start НЕЗАВИСИМО
         (start + i*step*direction), а не приращением к предыдущей — так
         ошибка отдельной точки не накапливается от точки к точке на
         длинных развёртках.
      3. Ноль включается, если интервал [start, stop] его пересекает, даже
         если он не попадает точно на шаг.
    """
    if step <= 0:
        raise ValueError("X_step должен быть положительным")
    if start == stop:
        return [start]

    direction = 1.0 if stop > start else -1.0
    n = max(1, round(abs(stop - start) / step))
    points = [start + direction * step * i for i in range(n)]
    points.append(stop)
    # Если регулярная сетка почти попала в stop сама, не дублируем точку.
    if len(points) >= 2 and abs(points[-2] - stop) < step * 1e-9:
        points.pop(-2)

    lo, hi = (start, stop) if start < stop else (stop, start)
    if lo < 0 < hi and not any(p == 0 for p in points):
        insert_at = next(i for i, p in enumerate(points) if (p > 0) == (direction > 0))
        points.insert(insert_at, 0.0)

    return points


def _magnitude_pass(magnitude: float, step: float) -> List[float]:
    """0 → +magnitude, по возрастанию. Строительный блок для пресетов."""
    if magnitude == 0:
        return [0.0]
    return _raw_pass(0.0, magnitude, step)


# Плавный проход нуля (feature, баг-репорт п.18): множитель и делители для
# автоматического «зазора» у нуля и мелкого подшага в нём. Идея — на подходе к
# нулю и на отходе от него ток должен меняться мелкими шагами (плавно, без
# скачка), а вдали от нуля — обычным шагом. Отдельного UI-поля под ширину зоны
# и подшаг нет (заказчик просил «просто красиво»); выводим их из X_step.
ZERO_SMOOTH_ZONE_STEPS = 1.0   # ширина зоны сглаживания = 1 обычный шаг
ZERO_SMOOTH_SUBSTEPS = 4       # столько мелких подшагов на эту зону


def _graduated_magnitude_pass(magnitude: float, step: float,
                              zone: float, substep: float) -> List[float]:
    """
    0 → +magnitude, но у нуля — мелким подшагом (substep) в пределах zone, а
    дальше — обычным step. Строительный блок для FULL_CYCLE с плавным проходом
    нуля (п.18): каждая ветвь стартует/финиширует у нуля густо и медленно.
    """
    if magnitude == 0:
        return [0.0]
    zone = min(zone, magnitude)
    if zone <= 0 or substep <= 0:
        return _raw_pass(0.0, magnitude, step)
    fine = _raw_pass(0.0, zone, substep)          # 0 … zone мелким подшагом
    if magnitude <= zone:
        return fine
    coarse = _raw_pass(zone, magnitude, step)     # zone … magnitude обычным шагом
    # zone встречается в конце fine и в начале coarse — не дублируем.
    return fine[:-1] + coarse


def _to_sweep_points(values: List[float], magnitude: float) -> List[SweepPoint]:
    return [
        SweepPoint(
            # "-0.0" — законный итог отрицания/вычитания (например -(-p) в
            # зеркалировании), но в CSV/логе он читался бы как "-0.0000 А",
            # что путает оператора: нормализуем -0.0 -> 0.0 (IEEE 754:
            # -0.0 + 0.0 == 0.0).
            x_set=(v + 0.0),
            magnitude=abs(v),
            relay=None if v == 0 else ('forward' if v > 0 else 'reverse'),
            is_zero=(v == 0),
            is_endpoint=(abs(v) == magnitude and magnitude > 0),
        )
        for v in values
    ]


def _preset_sequence(magnitude: float, step: float, preset: DirectionPreset,
                     zero_crossing_smooth: bool = False) -> List[float]:
    """
    Строит знаковую последовательность для одностороннего (заякоренного в
    нуле) свипа по выбранному пресету. Каждый вызов _magnitude_pass строит
    ОДИН проход 0→+magnitude; отражения/развороты — обычные срезы списка.

    Сколько раз в последовательности встречается 0 — решение пресета, не
    случайность: DIVERGING/CONVERGING экономят на повторном заходе в ноль
    (переход воспринимается как один и тот же физический ноль — как было
    задумано ещё в Ф0), DESCENDING/FULL_CYCLE специально возвращаются в
    ноль несколько раз — это и есть смысл снятия петли гистерезиса, ноль
    после разных экскурсий может показывать разное.

    zero_crossing_smooth (feature, баг-репорт п.18) — только для FULL_CYCLE:
    каждая ветвь проходит ноль густо и медленно (мелкий подшаг в зоне у нуля,
    см. _graduated_magnitude_pass), без резкого скачка. Ноль при этом всё
    равно измеряется отдельной точкой (это и есть «остановка в нуле» — на ней
    источник выключен, см. measurement._measure_zero_row). Для остальных
    пресетов флаг игнорируется (у них проход нуля не является смыслом схемы).
    """
    if preset == DirectionPreset.FULL_CYCLE and zero_crossing_smooth:
        zone = step * ZERO_SMOOTH_ZONE_STEPS
        substep = zone / ZERO_SMOOTH_SUBSTEPS
        pos_away = _graduated_magnitude_pass(magnitude, step, zone, substep)
    else:
        pos_away = _magnitude_pass(magnitude, step)      # 0 → +X
    pos_toward = list(reversed(pos_away))                # +X → 0
    neg_away = [-p for p in pos_away]                     # 0 → -X
    neg_toward = list(reversed(neg_away))                 # -X → 0

    if preset == DirectionPreset.DIVERGING:
        return pos_away + neg_away[1:]
    if preset == DirectionPreset.CONVERGING:
        return pos_toward + neg_away[1:]
    if preset == DirectionPreset.DESCENDING:
        return pos_toward + neg_toward
    if preset == DirectionPreset.FULL_CYCLE:
        return pos_away + pos_toward[1:] + neg_away[1:] + neg_toward[1:]
    raise ValueError(f"Неизвестный пресет направления: {preset!r}")


def preset_applies(X_start: float, X_stop: float, branch: Branch) -> bool:
    """
    True, если выбранная схема прохода (preset) реально повлияет на план
    plan_sweep() при этих X_start/X_stop/branch — то есть branch=BOTH И
    развёртка ОДНОСТОРОННЯЯ, заякоренная в нуле (см. докстринг модуля).

    Баг-репорт: если X_start/X_stop уже сами по себе охватывают обе
    полярности буквально (например 150 → −150), preset ни на что не
    влияет — plan_sweep идёт прямым проходом между ними, полностью
    игнорируя выбранную схему (петлю гистерезиса и т.п.), даже если
    оператор её явно выбрал в UI. Эта функция — чтобы UI мог честно
    предупредить об этом ДО старта измерения, а не молча измерить не то,
    что подразумевал выбор в выпадающем списке.

    Логика буквально повторяет ветвление plan_sweep — единственный
    источник истины, чтобы предупреждение не могло разойтись с тем, что
    измерение реально сделает.
    """
    if branch != Branch.BOTH:
        return False
    spans_both_signs = (X_start < 0 < X_stop) or (X_stop < 0 < X_start)
    if spans_both_signs:
        return False
    return X_start == 0 or X_stop == 0


def plan_sweep(X_start: float, X_stop: float, X_step: float,
               branch: Branch = Branch.BOTH,
               preset: DirectionPreset = DirectionPreset.DIVERGING,
               zero_crossing_smooth: bool = False) -> List[SweepPoint]:
    """
    Строит полный план измерения. Ничего не знает про приборы — только
    комбинаторика точек. См. докстринг модуля про модель целиком.

    zero_crossing_smooth (п.18) — плавный проход нуля; действует только на
    FULL_CYCLE с заякоренной в нуле развёрткой (см. _preset_sequence).
    """
    if X_step <= 0:
        raise ValueError("X_step должен быть положительным")
    if math.isnan(X_start) or math.isnan(X_stop):
        raise ValueError("X_start/X_stop не могут быть NaN")

    if branch == Branch.NO_RELAY:
        # Без платы реле источник физически не может сменить полярность
        # (см. README: "полярность на первичке меняет плата реле, источник
        # всегда однополярный") — знак X_start/X_stop здесь не имеет
        # смысла вовсе, берём модуль и идём одним проходом по величине.
        # relay=None форсируется на КАЖДОЙ точке, включая ненулевые и
        # крайние — в отличие от Branch.POSITIVE, где ветвь всё равно один
        # раз коммутируется в 'forward'.
        magnitude_start, magnitude_stop = abs(X_start), abs(X_stop)
        values = _raw_pass(magnitude_start, magnitude_stop, X_step)
        magnitude = max(magnitude_start, magnitude_stop)
        points = _to_sweep_points(values, magnitude)
        return [replace(p, relay=None) for p in points]

    spans_both_signs = (X_start < 0 < X_stop) or (X_stop < 0 < X_start)

    if spans_both_signs:
        # X_start/X_stop уже сами по себе описывают двуполярную развёртку
        # буквально — пресет здесь ни при чём, branch работает как фильтр.
        base = _raw_pass(X_start, X_stop, X_step)
        magnitude = max(abs(X_start), abs(X_stop))
        if branch == Branch.POSITIVE:
            values = [p for p in base if p >= 0]
        elif branch == Branch.NEGATIVE:
            values = [p for p in base if p <= 0]
        else:
            values = base
        return _to_sweep_points(values, magnitude)

    # Односторонняя развёртка: X_start и X_stop одного знака (или один из
    # них — 0).
    base = _raw_pass(X_start, X_stop, X_step)
    base_is_negative = X_start < 0 or X_stop < 0
    anchored_at_zero = (X_start == 0 or X_stop == 0)

    if branch == Branch.BOTH:
        if not anchored_at_zero:
            # Ничего не "заходит" в ноль (например 150→250) — пресетную
            # хореографию применить не к чему. both здесь буквально
            # означает "эта развёртка и её знаковое зеркало", без общего
            # нуля — его в этом диапазоне попросту нет (см. п.17: "если
            # измеряем 150-250, ноль не трогаем").
            mirror = [-p for p in base]
            magnitude = max(abs(X_start), abs(X_stop))
            return _to_sweep_points(base, magnitude) + _to_sweep_points(mirror, magnitude)

        magnitude = max(abs(X_start), abs(X_stop))
        sequence = _preset_sequence(magnitude, X_step, preset,
                                    zero_crossing_smooth=zero_crossing_smooth)
        return _to_sweep_points(sequence, magnitude)

    # branch — конкретная полярность, не обе.
    wants_negative = (branch == Branch.NEGATIVE)
    magnitude = max(abs(X_start), abs(X_stop))
    if wants_negative == base_is_negative:
        values = base
    else:
        # Запрошенная полярность противоположна тому, что буквально
        # описывают X_start/X_stop, — зеркалим единственную ветвь целиком
        # (удобно: можно ввести дружелюбный положительный диапазон и
        # получить отрицательную ветвь, не переписывая числа).
        values = [-p for p in base]
    return _to_sweep_points(values, magnitude)


# ----------------------------------------------------------------------
# Планировщик кастомных программ (feature) — текстовый DSL вместо единого
# X_start/X_stop/X_step + branch/preset. Полная свобода порядка/знака/
# повторов точек — оператор пишет буквально то, что хочет измерить, в том
# порядке, в котором хочет это измерить; никакой комбинаторики (branch,
# пресеты) здесь нет вовсе, она для этого режима не имеет смысла.
# ----------------------------------------------------------------------

def parse_custom_program(text: str) -> List[float]:
    """
    Разбирает строку кастомной программы в список ЗНАКОВЫХ уставок,
    буквально в том порядке, в котором они даны.

    Токены — через запятую, каждый один из двух видов:
      - одно число: "-25", "+40", "5", "0" — одна точка;
      - диапазон "начало:конец:шаг" (шаг — положительная величина) —
        разворачивается ЧЕРЕЗ _raw_pass, то есть с теми же гарантиями
        точности конца прохода и вставки нуля на пересечении, что и у
        обычной развёртки (см. модульный докстринг).

    Повторы допускаются (например точка 0 дважды в разных местах строки) —
    "полная свобода", как и попросил оператор; ни сортировки, ни
    дедупликации не производится.

    Десятичный разделитель — ТОЛЬКО точка ("2.5"), не запятая: запятая уже
    занята как разделитель токенов ("1,5" — это ДВЕ точки, 1 и 5, а не
    "1.5"), в отличие от остальных числовых полей CLI/GUI, где запятая
    равнозначна точке.
    """
    text = (text or '').strip()
    if not text:
        raise ValueError("Программа не может быть пустой.")

    values: List[float] = []
    for raw_token in text.split(','):
        token = raw_token.strip()
        if not token:
            continue
        if ':' in token:
            parts = token.split(':')
            if len(parts) != 3:
                raise ValueError(
                    f"Некорректный диапазон {token!r} — ожидается 'начало:конец:шаг'."
                )
            try:
                start, stop, step = (float(p.strip()) for p in parts)
            except ValueError:
                raise ValueError(
                    f"Некорректный диапазон {token!r} — начало/конец/шаг должны быть числами."
                )
            if step <= 0:
                raise ValueError(f"Шаг в диапазоне {token!r} должен быть положительным.")
            values.extend(_raw_pass(start, stop, step))
        else:
            try:
                values.append(float(token))
            except ValueError:
                raise ValueError(
                    f"Не удалось разобрать {token!r} — ожидается число или диапазон "
                    "'начало:конец:шаг'."
                )

    if not values:
        raise ValueError("Программа не содержит ни одной точки.")
    return values


def plan_custom_sweep(text: str) -> List[SweepPoint]:
    """
    Строит план измерения из текста кастомной программы (см.
    parse_custom_program). branch/preset здесь не участвуют вовсе —
    полярность каждой точки определяется буквально её знаком (та же
    relay-логика, что и везде, см. _to_sweep_points), в порядке, заданном
    оператором.
    """
    values = parse_custom_program(text)
    magnitude = max((abs(v) for v in values), default=0.0)
    return _to_sweep_points(values, magnitude)
