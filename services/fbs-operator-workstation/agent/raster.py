"""Растр этикетки: PNG → команды принтера.

Формат стикера — открытый вопрос 2 раздела 13 мастера. Целевой `zplv`, но
XP-420B заявляет эмуляцию ZPL, а подтверждения на живом принтере нет.
Поэтому оба пути заложены заранее, а не «когда выяснится»:

* стикер пришёл в ZPL, принтер понимает ZPL — печатаем как есть, без обработки;
* стикер пришёл в PNG — растр разбирается здесь и уходит либо командой TSPL
  `BITMAP`, либо командой ZPL `^GFA`, смотря что понимает принтер станции;
* стикер пришёл в ZPL, а принтер понимает только TSPL — это единственный
  случай, который здесь не решается: ZPL нужно не перерисовать, а
  интерпретировать. Тогда агент говорит об этом вслух, и поток A переключает
  `WB_STICKER_FORMAT` на `png`.

Декодер PNG — свой, потому что Pillow на пяти складских ПК это пятая установка,
которую кто-то должен повторять после каждой переустановки Windows. Здесь нужен
не редактор изображений, а «прочитать чёрно-белую этикетку 58×40 мм».
"""
from __future__ import annotations

import struct
import zlib

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Порог «чёрное/белое». Этикетка WB — штрихкод и текст, полутонов в ней нет,
# поэтому середина шкалы работает и не требует настройки.
DEFAULT_THRESHOLD = 128


class RasterError(RuntimeError):
    pass


def decode_png(data: bytes) -> tuple[int, int, list[bytearray]]:
    """PNG → (ширина, высота, строки яркости 0–255).

    Поддержаны все неинтерлейсные комбинации: серый, RGB, палитра, с альфой и
    без, глубина 1/2/4/8/16. Интерлейс Adam7 не поддержан сознательно — WB
    его не отдаёт, а поддержка ради гипотезы это код, который никто не
    проверит.
    """
    if not data.startswith(PNG_MAGIC):
        raise RasterError("это не PNG")

    position = len(PNG_MAGIC)
    header: tuple[int, ...] | None = None
    palette = b""
    transparency = b""
    compressed = bytearray()

    while position + 8 <= len(data):
        (length,) = struct.unpack_from("!I", data, position)
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        position += 12 + length  # длина + тип + данные + CRC
        if kind == b"IHDR":
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                "!IIBBBBB", body)
            if compression != 0 or filtering != 0:
                raise RasterError("нестандартное сжатие или фильтрация PNG")
            if interlace != 0:
                raise RasterError("интерлейсный PNG не поддержан")
            header = (width, height, depth, colour)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IDAT":
            compressed += body
        elif kind == b"IEND":
            break

    if header is None:
        raise RasterError("в PNG нет заголовка IHDR")
    width, height, depth, colour = header
    if width <= 0 or height <= 0 or width * height > 50_000_000:
        raise RasterError(f"неправдоподобный размер картинки {width}×{height}")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if channels is None:
        raise RasterError(f"тип цвета {colour} не поддержан")

    raw = zlib.decompress(bytes(compressed))
    bits_per_pixel = channels * depth
    stride = (width * bits_per_pixel + 7) // 8
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise RasterError("PNG обрывается посреди данных")

    rows = _unfilter(raw, height, stride, max(1, bits_per_pixel // 8))
    return width, height, _to_gray(rows, width, height, depth, colour, palette, transparency)


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> list[bytearray]:
    """Снятие построчных фильтров PNG."""
    rows: list[bytearray] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        if filter_type == 1:      # Sub
            for index in range(bpp, stride):
                line[index] = (line[index] + line[index - bpp]) & 0xFF
        elif filter_type == 2:    # Up
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:    # Average
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:    # Paeth
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                up = previous[index]
                upper_left = previous[index - bpp] if index >= bpp else 0
                estimate = left + up - upper_left
                da, db, dc = (abs(estimate - left), abs(estimate - up),
                              abs(estimate - upper_left))
                nearest = left if (da <= db and da <= dc) else (up if db <= dc else upper_left)
                line[index] = (line[index] + nearest) & 0xFF
        elif filter_type != 0:
            raise RasterError(f"неизвестный фильтр строки {filter_type}")
        rows.append(line)
        previous = line
    return rows


def _samples(line: bytearray, width: int, depth: int, channels: int) -> list[int]:
    """Строку байтов — в список отсчётов, чем бы ни была глубина."""
    if depth == 8:
        return list(line[:width * channels])
    if depth == 16:
        # Старший байт: этикетка чёрно-белая, младший ничего не решает.
        return [line[index] for index in range(0, width * channels * 2, 2)]
    values: list[int] = []
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    maximum = mask
    for index in range(width * channels):
        byte = line[index // per_byte]
        shift = 8 - depth * (index % per_byte + 1)
        raw = (byte >> shift) & mask
        # Растягиваем к шкале 0–255, чтобы порог был один для всех глубин.
        values.append(raw * 255 // maximum if maximum else 0)
    return values


def _to_gray(rows: list[bytearray], width: int, height: int, depth: int, colour: int,
             palette: bytes, transparency: bytes) -> list[bytearray]:
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
    gray: list[bytearray] = []
    for line in rows:
        values = _samples(line, width, depth, channels)
        out = bytearray(width)
        for x in range(width):
            base = x * channels
            if colour == 3:
                # Индекс палитры берётся до растяжения шкалы.
                index = _palette_index(line, x, depth)
                red, green, blue = _palette_rgb(palette, index)
                alpha = transparency[index] if index < len(transparency) else 255
                level = (red * 299 + green * 587 + blue * 114) // 1000
            elif colour in (0, 4):
                level = values[base]
                alpha = values[base + 1] if colour == 4 else 255
            else:
                red, green, blue = values[base], values[base + 1], values[base + 2]
                alpha = values[base + 3] if colour == 6 else 255
                level = (red * 299 + green * 587 + blue * 114) // 1000
            # Прозрачное считаем белым: на этикетке фон — бумага.
            out[x] = 255 if alpha < 128 else min(255, max(0, level))
        gray.append(out)
    del height
    return gray


def _palette_index(line: bytearray, x: int, depth: int) -> int:
    if depth == 8:
        return line[x]
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    shift = 8 - depth * (x % per_byte + 1)
    return (line[x // per_byte] >> shift) & mask


def _palette_rgb(palette: bytes, index: int) -> tuple[int, int, int]:
    offset = index * 3
    if offset + 2 >= len(palette):
        return 0, 0, 0
    return palette[offset], palette[offset + 1], palette[offset + 2]


def to_bilevel(gray: list[bytearray], width: int, *,
               threshold: int = DEFAULT_THRESHOLD) -> tuple[int, list[bytes]]:
    """Яркость → упакованные биты, где 1 означает «печатать чёрным».

    Возвращает (байт в строке, строки). Хранение в «1 = чёрное» выбрано
    потому, что так устроен ZPL; для TSPL биты инвертируются при выводе — там
    ноль означает чёрное.
    """
    row_bytes = (width + 7) // 8
    packed: list[bytes] = []
    for line in gray:
        out = bytearray(row_bytes)
        for x in range(width):
            if line[x] < threshold:
                out[x >> 3] |= 0x80 >> (x & 7)
        packed.append(bytes(out))
    return row_bytes, packed


def png_to_zpl(data: bytes, *, threshold: int = DEFAULT_THRESHOLD,
               x: int = 0, y: int = 0) -> bytes:
    """PNG → ZPL с командой ^GFA. Единица бита печатается чёрным."""
    width, height, gray = decode_png(data)
    row_bytes, rows = to_bilevel(gray, width, threshold=threshold)
    body = b"".join(rows)
    hex_data = body.hex().upper()
    total = len(body)
    command = (
        f"^XA^FO{x},{y}^GFA,{total},{total},{row_bytes},{hex_data}^FS^XZ"
    ).encode("ascii")
    del height
    return command


def png_to_tspl(data: bytes, *, threshold: int = DEFAULT_THRESHOLD,
                width_mm: int = 58, height_mm: int = 40,
                gap_mm: int = 2, x: int = 0, y: int = 0) -> bytes:
    """PNG → TSPL с командой BITMAP.

    В TSPL ноль бита печатается чёрным — обратная ZPL договорённость, поэтому
    биты инвертируются здесь, а не в вызывающем коде.
    """
    width, height, gray = decode_png(data)
    row_bytes, rows = to_bilevel(gray, width, threshold=threshold)
    inverted = b"".join(bytes(byte ^ 0xFF for byte in row) for row in rows)
    header = (
        f"SIZE {width_mm} mm,{height_mm} mm\r\n"
        f"GAP {gap_mm} mm,0 mm\r\n"
        "DIRECTION 1\r\n"
        "CLS\r\n"
        f"BITMAP {x},{y},{row_bytes},{height},0,"
    ).encode("ascii")
    return header + inverted + b"\r\nPRINT 1,1\r\n"


# Тестовые этикетки для проверки принтера на месте (раздел 13, вопрос 2 и
# блок «Печать» файла 03). Печатаются обе — какая вышла, скажет человек.
ZPL_PROBE = (b"^XA^PW464^LL240\r\n"
             b"^FO40,40^A0N,40,40^FDZPL OK^FS\r\n"
             b"^FO40,110^A0N,28,28^FDMM-Express probe^FS\r\n^XZ\r\n")

TSPL_PROBE = (b"SIZE 58 mm,40 mm\r\nGAP 2 mm,0 mm\r\nDIRECTION 1\r\nCLS\r\n"
              b'TEXT 40,40,"3",0,1,1,"TSPL OK"\r\n'
              b'TEXT 40,110,"2",0,1,1,"MM-Express probe"\r\n'
              b"PRINT 1,1\r\n")
