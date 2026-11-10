"""Regenerate committed Cyber icons locally (requires Pillow, not needed on VPS)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


OUTPUT = Path(__file__).resolve().parents[1] / "static" / "themes" / "cyber"
MARK = [(154, 157), (366, 157), (366, 196), (219, 318), (366, 318),
        (366, 358), (146, 358), (146, 319), (293, 197), (154, 197)]
SCALE = 4
SIZE = 512 * SCALE


def gradient(start, end):
    image = Image.new("RGB", (SIZE, SIZE))
    draw = ImageDraw.Draw(image)
    for x in range(SIZE):
        weight = x / (SIZE - 1)
        color = tuple(round(a + (b - a) * weight) for a, b in zip(start, end))
        draw.line((x, 0, x, SIZE), fill=color)
    return image


def generate():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    points = " ".join(f"{x},{y}" for x, y in MARK)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="panel"><stop stop-color="#17102e"/><stop offset="1" stop-color="#071c32"/></linearGradient>
    <linearGradient id="neon"><stop stop-color="#b331ff"/><stop offset=".45" stop-color="#7140ff"/><stop offset="1" stop-color="#00cfff"/></linearGradient>
    <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="12"/></filter>
  </defs>
  <rect width="512" height="512" fill="#050a1c"/>
  <rect x="48" y="48" width="416" height="416" rx="106" fill="url(#panel)"/>
  <rect x="49.5" y="49.5" width="413" height="413" rx="104.5" stroke="url(#neon)" stroke-width="3" fill="none" opacity=".65"/>
  <polygon points="{points}" fill="url(#neon)" filter="url(#glow)" opacity=".5"/>
  <polygon points="{points}" fill="url(#neon)"/>
</svg>
'''
    (OUTPUT / "favicon.svg").write_text(svg, encoding="utf-8")
    image = Image.new("RGB", (SIZE, SIZE), "#050a1c")
    panel = Image.new("L", (SIZE, SIZE))
    ImageDraw.Draw(panel).rounded_rectangle(
        (48 * SCALE, 48 * SCALE, 464 * SCALE, 464 * SCALE),
        radius=106 * SCALE, fill=255,
    )
    image.paste(gradient((23, 16, 46), (7, 28, 50)), (0, 0), panel)
    neon = gradient((179, 49, 255), (0, 207, 255))
    border = Image.new("L", (SIZE, SIZE))
    ImageDraw.Draw(border).rounded_rectangle(
        (49 * SCALE, 49 * SCALE, 463 * SCALE, 463 * SCALE),
        radius=105 * SCALE, outline=166, width=3 * SCALE,
    )
    image.paste(neon, (0, 0), border)
    mark = Image.new("L", (SIZE, SIZE))
    ImageDraw.Draw(mark).polygon([(x * SCALE, y * SCALE) for x, y in MARK], fill=255)
    glow = mark.filter(ImageFilter.GaussianBlur(12 * SCALE)).point(lambda value: value // 2)
    image.paste(neon, (0, 0), glow)
    image.paste(neon, (0, 0), mark)
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        image.resize((size, size), Image.Resampling.LANCZOS).save(OUTPUT / name, optimize=True)
    image.resize((256, 256), Image.Resampling.LANCZOS).save(
        OUTPUT / "favicon.ico", sizes=[(size, size) for size in (16, 32, 48, 64, 128, 256)],
    )


if __name__ == "__main__":
    generate()
