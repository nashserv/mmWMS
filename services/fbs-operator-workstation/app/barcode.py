"""Code 128 — штрихкод бумажного листа подбора.

Лист подбора печатается на бумаге и уходит со сборщиком на склад. Через час
он лежит на столе среди пяти таких же, и вернуть его к своей сессии можно
только одним способом — сканером, тем же, что висит у сборщика на поясе.
Поэтому штрихкод на листе обязан быть настоящим, а не картинкой «похоже на
штрихкод».

Реализация своя и в двадцать строк таблицы, потому что набор зависимостей
платформы заморожен (раздел 2.2 мастера): тянуть библиотеку ради одного
символа Code 128 дороже, чем написать его.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

# Ширины штрихов Code 128, значения 0–106. Последний — стоп-символ, у него семь
# элементов вместо шести.
_PATTERNS = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232 2331112"
).split()

START_B = 104
STOP = 106

# Тихая зона: без неё сканер не «зацепится» за код, и печать окажется
# бесполезной ровно тогда, когда лист нужен.
QUIET_MODULES = 10


def _values(text: str) -> list[int]:
    """Символы в значения Code 128 B.

    Набор B покрывает печатные ASCII — этого хватает: штрихкод листа
    генерируем мы сами, и кириллицы в нём нет по построению.
    """
    values: list[int] = []
    for char in text:
        code = ord(char)
        if not 32 <= code <= 126:
            raise ValueError(f"символ {char!r} не кодируется Code 128 B")
        values.append(code - 32)
    return values


def modules(text: str) -> list[int]:
    """Последовательность ширин штрихов и пробелов, начиная со штриха."""
    values = _values(text)
    checksum = START_B
    for position, value in enumerate(values, start=1):
        checksum += position * value
    checksum %= 103

    widths: list[int] = []
    for value in [START_B, *values, checksum, STOP]:
        widths.extend(int(width) for width in _PATTERNS[value])
    return widths


def svg(text: str, *, module_width: float = 2.0, height: float = 56.0,
        show_text: bool = True) -> str:
    """SVG со штрихкодом — вставляется прямо в лист подбора.

    SVG, а не PNG: лист печатается браузером станции, а векторный код
    переживает любое масштабирование принтером. Растр на 203 dpi легко
    расплывается ровно настолько, чтобы сканер перестал его брать.
    """
    widths = modules(text)
    total_modules = sum(widths) + 2 * QUIET_MODULES
    width = total_modules * module_width
    caption_height = 16.0 if show_text else 0.0

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.1f}" '
        f'height="{height + caption_height:.1f}" '
        f'viewBox="0 0 {width:.1f} {height + caption_height:.1f}" role="img" '
        f'aria-label="{escape(text)}">',
        f'<rect width="{width:.1f}" height="{height + caption_height:.1f}" fill="#fff"/>',
    ]
    position = QUIET_MODULES * module_width
    is_bar = True
    for module_count in widths:
        span = module_count * module_width
        if is_bar:
            parts.append(f'<rect x="{position:.2f}" y="0" width="{span:.2f}" '
                         f'height="{height:.1f}" fill="#000"/>')
        position += span
        is_bar = not is_bar
    if show_text:
        parts.append(
            f'<text x="{width / 2:.1f}" y="{height + 12:.1f}" text-anchor="middle" '
            f'font-family="monospace" font-size="12" fill="#000">{escape(text)}</text>')
    parts.append("</svg>")
    return "".join(parts)
