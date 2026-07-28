import os
import shutil
import struct
import subprocess
import zlib
from pathlib import Path


_PS_RENDER_SCRIPT = r"""
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
$Output = [Environment]::GetEnvironmentVariable("KB_OCR_FIXTURE_OUTPUT")
$Text = [Environment]::GetEnvironmentVariable("KB_OCR_FIXTURE_TEXT")

$bitmap = New-Object System.Drawing.Bitmap -ArgumentList 900, 220
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$font = $null
try {
    $graphics.Clear([System.Drawing.Color]::White)
    $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    foreach ($name in @("Microsoft YaHei", "Arial")) {
        try {
            $font = New-Object System.Drawing.Font -ArgumentList $name, 36.0, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
            if ($null -ne $font) { break }
        } catch {
            $font = $null
        }
    }
    if ($null -eq $font) {
        $font = New-Object System.Drawing.Font -ArgumentList ([System.Drawing.FontFamily]::GenericSansSerif), 36.0, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
    }
    $graphics.DrawString($Text, $font, [System.Drawing.Brushes]::Black, 20, 70)
    $bitmap.Save($Output, [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
    if ($null -ne $font) { $font.Dispose() }
    $graphics.Dispose()
    $bitmap.Dispose()
}
"""

_DIGIT_GLYPHS = {
    "0": ("111", "101", "101", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "010", "010", "111"),
    "2": ("111", "001", "001", "111", "100", "100", "111"),
    "3": ("111", "001", "001", "111", "001", "001", "111"),
    "4": ("101", "101", "101", "111", "001", "001", "001"),
    "5": ("111", "100", "100", "111", "001", "001", "111"),
    "6": ("111", "100", "100", "111", "101", "101", "111"),
    "7": ("111", "001", "001", "010", "010", "100", "100"),
    "8": ("111", "101", "101", "111", "101", "101", "111"),
    "9": ("111", "101", "101", "111", "001", "001", "111"),
}
_LETTER_GLYPHS = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("111", "010", "010", "010", "010", "010", "111"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}
_GLYPHS = {**_DIGIT_GLYPHS, **_LETTER_GLYPHS}


def create_ocr_fixture(output: str | Path, *, text: str) -> dict[str, str]:
    """Render a minimal local image for OCR smoke tests."""
    output_path = Path(output).expanduser()
    if output_path.exists() and output_path.is_dir():
        raise RuntimeError("output path is a directory")
    if output_path.is_symlink():
        raise RuntimeError("output path is a symlink")
    if not text.strip():
        raise RuntimeError("text must not be empty")

    parent = output_path.parent
    if parent.exists() and not parent.is_dir():
        raise RuntimeError("output parent is not a directory")
    parent.mkdir(parents=True, exist_ok=True)

    resolved_output = output_path.resolve()
    if os.name == "nt":
        _render_with_powershell(resolved_output, text)
    else:
        _render_fallback_png(resolved_output, text)

    if not resolved_output.is_file():
        raise RuntimeError("OCR fixture was not created")
    if resolved_output.stat().st_size <= 100:
        raise RuntimeError("OCR fixture is unexpectedly small")
    return {"path": str(resolved_output)}


def _render_with_powershell(output: Path, text: str) -> None:
    powershell = _find_powershell()
    environment = os.environ.copy()
    environment["KB_OCR_FIXTURE_OUTPUT"] = str(output)
    environment["KB_OCR_FIXTURE_TEXT"] = text
    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _PS_RENDER_SCRIPT,
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("OCR fixture rendering failed") from exc
    if completed.returncode != 0:
        detail = _single_line(completed.stderr or completed.stdout)
        if detail:
            raise RuntimeError(f"OCR fixture rendering failed: {detail}")
        raise RuntimeError("OCR fixture rendering failed")


def _find_powershell() -> str:
    for name in ("powershell.exe", "powershell"):
        command = shutil.which(name)
        if command:
            return command
    return "powershell"


def _render_fallback_png(output: Path, text: str) -> None:
    width = 900
    height = 220
    pixels = bytearray([255] * width * height * 3)
    x = 20
    y = 70
    scale = 12
    for character in text.upper():
        glyph = _GLYPHS.get(character)
        if character.isspace() or glyph is None:
            x += scale * 3
            continue
        for row_index, row in enumerate(glyph):
            for column_index, value in enumerate(row):
                if value == "1":
                    _fill_rect(pixels, width, x + column_index * scale, y + row_index * scale, scale, scale)
        x += (len(glyph[0]) + 1) * scale
    _write_png(output, width, height, bytes(pixels))


def _fill_rect(
    pixels: bytearray, image_width: int, x: int, y: int, width: int, height: int
) -> None:
    for row in range(y, min(y + height, 220)):
        for column in range(x, min(x + width, image_width)):
            offset = (row * image_width + column) * 3
            pixels[offset : offset + 3] = b"\x00\x00\x00"


def _write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    rows = []
    stride = width * 3
    for row in range(height):
        rows.append(b"\x00" + rgb[row * stride : (row + 1) * stride])
    compressed = zlib.compress(b"".join(rows), level=9)
    with path.open("wb") as file:
        file.write(b"\x89PNG\r\n\x1a\n")
        _write_chunk(file, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        _write_chunk(file, b"IDAT", compressed)
        _write_chunk(file, b"IEND", b"")


def _write_chunk(file: object, chunk_type: bytes, data: bytes) -> None:
    file.write(struct.pack(">I", len(data)))
    file.write(chunk_type)
    file.write(data)
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(data, checksum) & 0xFFFFFFFF
    file.write(struct.pack(">I", checksum))


def _single_line(value: str) -> str:
    return " ".join(value.split())
