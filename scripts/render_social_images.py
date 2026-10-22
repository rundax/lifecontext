#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow==12.3.0"]  # the version pyproject.toml pins
# ///
"""Render the landing page's link-card images and the GitHub social preview.

    uv run scripts/render_social_images.py

writes site/og.png and site/og-uk.png (1200x630, the og:image of / and /uk/)
and .github/social-preview.png (1280x640, uploaded by hand under the
repository's Settings, General, Social preview). The text is in EN and UK
below; change it there and run the script again. The same Pillow version and
font files give byte-identical images. Fonts: DejaVu Sans, DejaVu Sans Mono
and Liberation Serif (Debian and Ubuntu packages fonts-dejavu-core,
fonts-liberation and, on Debian 13 and Ubuntu 24.04 or later,
fonts-dejavu-mono).
"""
from __future__ import annotations

import argparse
import functools
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

EN = {
    "tagline": "self-hosted · source-available",
    "hero": ("Your documents, compiled.", "Every claim cited."),
    "path": "wiki/Topics/Apartment lease.md · example",
    "quote": "“The monthly rent is EUR 950, payable in advance on the first (1st) day of each month.”",
    "chip": "lease-2024.pdf v1 · p.3",
    "verified": "quote found in source",
    "address": "rundax.github.io/lifecontext",
}
UK = {
    "tagline": "на вашій машині · source-available",
    "hero": ("Ваші документи, зведені у вікі.", "Кожне твердження має джерело."),
    "path": "wiki/Topics/Оренда квартири.md · приклад",
    # No-break spaces keep "22 000" and "с. 3" on one line.
    "quote": "«Орендна плата становить 22\u00a0000 гривень на місяць і сплачується авансом першого (1-го) числа кожного місяця».",
    "chip": "договір-оренди-2024.pdf v1 · с.\u00a03",
    "verified": "цитату знайдено в джерелі",
    "address": "rundax.github.io/lifecontext/uk",
}
IMAGES = (
    ("site/og.png", (1200, 630), EN),
    ("site/og-uk.png", (1200, 630), UK),
    (".github/social-preview.png", (1280, 640), EN),
)

# Dark-theme tokens from site/assets/site.css, and GRID for the ruled background.
BG = "#0c1110"
GRID = "#131c1a"
SURFACE = "#131a18"
SURFACE_2 = "#18211e"
INK = "#e4ebe8"
INK_2 = "#b0bdb8"
INK_3 = "#8d9b96"
LINE = "#25302c"
ACCENT = "#46c79a"
ACCENT_INK = "#06110d"
ACCENT_SOFT = "#10271e"
ACCENT_LINE = "#2b6b54"

CONTENT_WIDTH = 1056
SS = 2  # shapes are drawn at twice the size and scaled down, for smooth edges
FONT_DIRS = (
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".local/share/fonts",
    Path("/Library/Fonts"),
    Path.home() / "Library/Fonts",
)


@functools.cache
def font_path(name: str) -> Path:
    for root in FONT_DIRS:
        found = sorted(root.rglob(name)) if root.is_dir() else []
        if found:
            return found[0]
    raise SystemExit(f"font {name} not found under {', '.join(map(str, FONT_DIRS))}")


@functools.cache
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    # The basic layout needs no libraqm, so the result does not depend on it.
    return ImageFont.truetype(str(font_path(name)), size, layout_engine=ImageFont.Layout.BASIC)


def wrap(text: str, face: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines = [""]
    for word in text.split(" "):
        candidate = f"{lines[-1]} {word}" if lines[-1] else word
        if lines[-1] and face.getlength(candidate) > width:
            lines.append(word)
        else:
            lines[-1] = candidate
    return lines


def mix(a: str, b: str, t: float) -> tuple[int, ...]:
    return tuple(round(x + (y - x) * t) for x, y in zip(ImageColor.getrgb(a), ImageColor.getrgb(b), strict=True))


def render(width: int, height: int, text: dict) -> Image.Image:
    left = (width - CONTENT_WIDTH) // 2
    right = width - left
    mono = font("DejaVuSansMono.ttf", 17)
    serif = font("LiberationSerif-Regular.ttf", 30)

    # Layout. The header sits at the top, the card and the address at the
    # bottom, and the hero is centred in the space between them.
    tile_top, tile = 63, 46
    mid = tile_top + tile / 2
    address_baseline = height - 36
    card_bottom = height - 82
    bar = 44
    quote_x = left + 80
    quote_lines = wrap(text["quote"], serif, right - 32 - quote_x)
    leading = 40
    chip_h = 36
    body_top = card_bottom - (26 + len(quote_lines) * leading + 14 + chip_h + 24)
    card_top = body_top - bar
    chip_top = card_bottom - 24 - chip_h
    chip_right = quote_x + 24 + mono.getlength(text["chip"])

    size = 60
    while size > 40 and max(font("DejaVuSans-Bold.ttf", size).getlength(line) for line in text["hero"]) > CONTENT_WIDTH:
        size -= 1
    hero = font("DejaVuSans-Bold.ttf", size)
    cap = -hero.getbbox("H", anchor="ls")[1]
    hero_leading = round(size * 1.22)
    hero_baseline = round((tile_top + tile + card_top - cap - hero_leading) / 2) + cap
    descent = hero.getbbox("gy", anchor="ls")[3]
    too_wide = max(hero.getlength(line) for line in text["hero"]) > CONTENT_WIDTH
    if too_wide or hero_baseline + hero_leading + descent + 24 > card_top:
        raise SystemExit(f"{text['address']}: the hero or the quote is too long for the image")

    # Shapes, drawn at SS times the size.
    shapes = Image.new("RGB", (width * SS, height * SS), BG)
    d = ImageDraw.Draw(shapes)

    def box(*xy: float) -> list[int]:
        return [round(v * SS) for v in xy]

    # A ruled background that fades out before the card.
    for y in range(31, card_top, 32):
        d.line(box(0, y, width, y), fill=mix(GRID, BG, y / card_top), width=SS)

    # The logo from site/favicon.svg (a 32-unit grid), in dark-theme colours.
    u = tile / 32
    d.rounded_rectangle(box(left, tile_top, left + tile, tile_top + tile), radius=7 * u * SS, fill=ACCENT)
    for y, w in ((8.5, 12), (14.5, 18), (20.5, 9)):
        d.rounded_rectangle(
            box(left + 7 * u, tile_top + y * u, left + (7 + w) * u, tile_top + (y + 3) * u),
            radius=1.5 * u * SS,
            fill=ACCENT_INK,
        )
    cx, cy, r = left + 23.5 * u, tile_top + 10 * u, 2.5 * u
    d.ellipse(box(cx - r, cy - r, cx + r, cy + r), fill=mix(ACCENT, ACCENT_INK, 0.6))  # .mark-dot, opacity 0.6

    # The card: a title bar, the numbered claim and its citation.
    d.rounded_rectangle(box(left, card_top, right, card_bottom), radius=12 * SS, fill=SURFACE, outline=LINE, width=3)
    d.rounded_rectangle(
        box(left, card_top, right, body_top),
        radius=12 * SS,
        fill=SURFACE_2,
        outline=LINE,
        width=3,
        corners=(True, True, False, False),
    )
    badge = (left + 24, body_top + 26, left + 58, body_top + 60)
    d.rounded_rectangle(box(*badge), radius=6 * SS, fill=ACCENT_SOFT, outline=ACCENT_LINE, width=3)
    d.rounded_rectangle(
        box(quote_x, chip_top, chip_right, chip_top + chip_h), radius=6 * SS, fill=SURFACE_2, outline=LINE, width=3
    )
    # The check mark of the site's "quote found in source" (a 24-unit grid).
    check, check_x, chip_mid = 22, chip_right + 18, chip_top + chip_h / 2
    points = [(check_x + px * check / 24, chip_mid + (py - 12) * check / 24) for px, py in ((5, 12), (10, 17), (20, 7))]
    d.line([round(v * SS) for p in points for v in p], fill=ACCENT, width=round(2.5 * check / 24 * SS), joint="curve")

    image = shapes.resize((width, height), Image.Resampling.LANCZOS)

    # Text, drawn at the final size so that it stays sharp.
    d = ImageDraw.Draw(image)
    d.text((left + tile + 16, mid), "LifeContext", font=font("DejaVuSans-Bold.ttf", 31), fill=INK, anchor="lm")
    d.text((right, mid), text["tagline"], font=mono, fill=INK_3, anchor="rm")
    d.text((left, hero_baseline), text["hero"][0], font=hero, fill=INK, anchor="ls")
    d.text((left, hero_baseline + hero_leading), text["hero"][1], font=hero, fill=ACCENT, anchor="ls")
    d.text((left + 22, card_top + bar / 2), text["path"], font=mono, fill=INK_3, anchor="lm")
    d.text(
        ((badge[0] + badge[2]) / 2, (badge[1] + badge[3]) / 2),
        "2",
        font=font("DejaVuSansMono-Bold.ttf", 18),
        fill=ACCENT,
        anchor="mm",
    )
    for i, line in enumerate(quote_lines):
        d.text((quote_x, body_top + 50 + i * leading), line, font=serif, fill=INK, anchor="ls")
    d.text((quote_x + 12, chip_mid), text["chip"], font=mono, fill=INK_2, anchor="lm")
    d.text((check_x + 28, chip_mid), text["verified"], font=mono, fill=ACCENT, anchor="lm")
    d.text((left, address_baseline), text["address"], font=font("DejaVuSansMono.ttf", 19), fill=INK_3, anchor="ls")
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir", type=Path, default=ROOT, help="write the images under this directory (default: the repository)"
    )
    args = parser.parse_args()
    for path, (width, height), text in IMAGES:
        out = args.out_dir / path
        out.parent.mkdir(parents=True, exist_ok=True)
        render(width, height, text).save(out, optimize=True)
        print(out)


if __name__ == "__main__":
    main()
