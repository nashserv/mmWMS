"""Штрихкод листа подбора и растр этикетки.

Обе вещи одинаково неприятны в отказе: нечитаемый штрихкод на листе делает
лист бесполезным, а неверно переложенный растр печатает мусор на настоящей
этикетке. Поэтому проверяется не «функция вернула байты», а конкретные
кодировки.
"""
from __future__ import annotations

import struct
import zlib

import pytest

from agent.raster import (RasterError, decode_png, png_to_tspl, png_to_zpl, to_bilevel)
from app.barcode import modules, svg


# --- Code 128 -------------------------------------------------------------

def test_code128_encodes_start_symbol_checksum_and_stop():
    """Проверка на известном символе: старт B, «A», контрольная сумма, стоп."""
    widths = "".join(str(width) for width in modules("A"))
    assert widths.startswith("211214"), "старт Code 128 B"
    assert widths[6:12] == "111323", "символ A"
    assert widths[12:18] == "131123", "контрольная сумма (104 + 1*33) % 103 = 34"
    assert widths.endswith("2331112"), "стоп-символ"


def test_code128_checksum_changes_with_the_text():
    assert modules("PL-1") != modules("PL-2")


def test_code128_refuses_what_it_cannot_encode():
    """Молча выдать нечитаемый код хуже, чем отказаться его печатать."""
    with pytest.raises(ValueError):
        modules("Лист")


def test_barcode_svg_has_quiet_zones_and_bars():
    picture = svg("PL-20260910-0001")
    assert picture.startswith("<svg") and picture.endswith("</svg>")
    assert picture.count("<rect") > 20
    assert "PL-20260910-0001" in picture


# --- PNG ------------------------------------------------------------------

def make_png(width, height, depth, colour, rows, palette=None, filter_type=0):
    def chunk(kind, body):
        data = kind + body
        return struct.pack("!I", len(body)) + data + struct.pack("!I", zlib.crc32(data))
    ihdr = struct.pack("!IIBBBBB", width, height, depth, colour, 0, 0, 0)
    raw = b"".join(bytes([filter_type]) + row for row in rows)
    out = b"\x89PNG\r\n\x1a\x0a" + chunk(b"IHDR", ihdr)
    if palette:
        out += chunk(b"PLTE", palette)
    return out + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_grayscale_png_becomes_black_and_white_bits():
    png = make_png(4, 2, 8, 0, [bytes([0, 255, 0, 255]), bytes([255, 0, 255, 0])])
    width, height, gray = decode_png(png)
    assert (width, height) == (4, 2)
    row_bytes, packed = to_bilevel(gray, width)
    assert row_bytes == 1
    assert packed[0][0] == 0b10100000, "единица бита — чёрное"


def test_palette_and_rgba_are_understood():
    palette = make_png(8, 1, 1, 3, [bytes([0b10101010])],
                       palette=bytes([0, 0, 0, 255, 255, 255]))
    _width, _height, gray = decode_png(palette)
    assert list(gray[0]) == [255, 0, 255, 0, 255, 0, 255, 0]

    rgba = make_png(2, 1, 8, 6, [bytes([0, 0, 0, 255, 0, 0, 0, 0])])
    _width, _height, gray = decode_png(rgba)
    assert list(gray[0]) == [0, 255], "прозрачное на этикетке — это бумага, то есть белое"


def test_row_filters_are_undone():
    expected = [[10, 20, 30, 40], [50, 60, 70, 80]]
    sub = make_png(4, 2, 8, 0, [bytes([10, 10, 10, 10]), bytes([50, 10, 10, 10])],
                   filter_type=1)
    _w, _h, gray = decode_png(sub)
    assert [list(row) for row in gray] == expected
    up = make_png(4, 2, 8, 0, [bytes([10, 20, 30, 40]), bytes([40, 40, 40, 40])],
                  filter_type=2)
    _w, _h, gray = decode_png(up)
    assert [list(row) for row in gray] == expected


def test_zpl_and_tspl_invert_bits_relative_to_each_other():
    """В ZPL единица печатается чёрным, в TSPL — ноль. Перепутать нельзя."""
    png = make_png(16, 8, 8, 0, [bytes([0] * 8 + [255] * 8) for _ in range(8)])
    zpl = png_to_zpl(png)
    assert zpl.startswith(b"^XA^FO0,0^GFA,16,16,2,") and zpl.endswith(b"^FS^XZ")
    assert b"FF00" in zpl, "левая половина чёрная"

    tspl = png_to_tspl(png)
    marker = b"BITMAP 0,0,2,8,0,"
    assert marker in tspl and tspl.endswith(b"PRINT 1,1\r\n")
    body = tspl[tspl.index(marker) + len(marker):]
    assert body[:2] == bytes([0x00, 0xFF]), "в TSPL чёрное — это нули"


def test_a_non_png_payload_is_refused():
    with pytest.raises(RasterError):
        decode_png("^XA^FDне картинка^FS^XZ".encode("utf-8"))


def test_interlaced_png_is_refused_loudly():
    """Поддержка ради гипотезы — это код, который никто не проверит."""
    body = struct.pack("!IIBBBBB", 2, 2, 8, 0, 0, 0, 1)
    def chunk(kind, payload):
        data = kind + payload
        return struct.pack("!I", len(payload)) + data + struct.pack("!I", zlib.crc32(data))
    png = b"\x89PNG\r\n\x1a\x0a" + chunk(b"IHDR", body) + chunk(b"IDAT", zlib.compress(b"\0\0\0")) + chunk(b"IEND", b"")
    with pytest.raises(RasterError):
        decode_png(png)
