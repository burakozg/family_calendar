"""Regenerate the PWA app icons (a white shopping bag on the app's accent blue).

    pip install pillow && python make_icons.py

Writes icon-192.png, icon-512.png, apple-touch-icon.png next to this file.
"""
from pathlib import Path
from PIL import Image, ImageDraw

ACCENT = (45, 107, 228)   # #2d6be4
WHITE  = (255, 255, 255)
HERE   = Path(__file__).parent


def make(size: int) -> Image.Image:
    SS = 4                      # supersample for smooth edges
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Full-bleed rounded background (iOS masks it; Android maskable wants bleed).
    d.rounded_rectangle([0, 0, S, S], radius=int(S * 0.22), fill=ACCENT)
    # Shopping-bag glyph, kept inside the maskable safe zone (~central 66%).
    d.rounded_rectangle([0.33 * S, 0.42 * S, 0.67 * S, 0.72 * S], radius=int(S * 0.03), fill=WHITE)
    d.arc([0.405 * S, 0.31 * S, 0.595 * S, 0.50 * S], start=180, end=360, fill=WHITE, width=int(S * 0.038))
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for name, size in [("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)]:
        make(size).save(HERE / name)
        print("wrote", name)
