"""Writes static/logo.png: the GroupThink wordmark, 512x512, dark ground, green block letters.

Standard library only (zlib + struct). The letters are a hand-coded 5x7 bitmap. No Groupon or
LivingSocial mark is drawn. Run from anywhere: python3 scripts/make_logo.py
"""
import struct
import zlib
from pathlib import Path

SIZE = 512
BG = (11, 26, 15)          # #0b1a0f
GREEN = (83, 163, 24)      # #53A318
SHADOW = (44, 100, 16)     # #2C6410

FONT = {
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
}
WORDS = ["GROUP", "THINK"]
CELL = 14                  # pixels per bitmap dot
SHADOW_OFFSET = 6
BORDER = 6


def draw(pixels, x0, y0, rows, color):
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch == "1":
                for y in range(y0 + r * CELL, y0 + (r + 1) * CELL):
                    base = y * SIZE
                    for x in range(x0 + c * CELL, x0 + (c + 1) * CELL):
                        pixels[base + x] = color


def build():
    pixels = [BG] * (SIZE * SIZE)
    for i in range(SIZE):
        for t in range(BORDER):
            pixels[t * SIZE + i] = pixels[(SIZE - 1 - t) * SIZE + i] = SHADOW
            pixels[i * SIZE + t] = pixels[i * SIZE + SIZE - 1 - t] = SHADOW
    word_w = 5 * 5 * CELL + 4 * CELL          # five letters, one dot of gap between them
    word_h = 7 * CELL
    gap = 2 * CELL
    top = (SIZE - (2 * word_h + gap)) // 2
    left = (SIZE - word_w) // 2
    for n, word in enumerate(WORDS):
        y = top + n * (word_h + gap)
        for i, ch in enumerate(word):
            draw(pixels, left + i * 6 * CELL + SHADOW_OFFSET, y + SHADOW_OFFSET, FONT[ch], SHADOW)
        for i, ch in enumerate(word):
            draw(pixels, left + i * 6 * CELL, y, FONT[ch], GREEN)
    return pixels


def chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png_bytes(pixels, size=SIZE):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for r, g, b in pixels[y * size:(y + 1) * size]:
            raw += bytes((r, g, b))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


FAVICON_SIZE = 32
FAVICON_CELL = 4


def make_favicon(path):
    """Writes a 32x32 PNG: dark ground, a green G from the 5x7 font at 4px cells, centred."""
    size, cell = FAVICON_SIZE, FAVICON_CELL
    pixels = [BG] * (size * size)
    left, top = (size - 5 * cell) // 2, (size - 7 * cell) // 2
    for r, row in enumerate(FONT["G"]):
        for c, ch in enumerate(row):
            if ch == "1":
                for y in range(top + r * cell, top + (r + 1) * cell):
                    for x in range(left + c * cell, left + (c + 1) * cell):
                        pixels[y * size + x] = GREEN
    path = Path(path)
    path.write_bytes(png_bytes(pixels, size))
    return path


def main():
    static = Path(__file__).resolve().parent.parent / "static"
    out = static / "logo.png"
    out.write_bytes(png_bytes(build()))
    print("wrote", out, out.stat().st_size, "bytes")
    fav = make_favicon(static / "favicon.png")
    print("wrote", fav, fav.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
