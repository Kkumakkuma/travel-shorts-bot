"""Generate Wander 60s v2 profile images and video watermarks.

Outputs:
- profile_v2_[a|b|c]_800.png
- profile_v2_[a|b|c]_1080.png
- preview_profile_v2_[a|b|c].png
- watermark_v2_[a|b|c]_150.png
- watermark_v2_[a|b|c]_1024.png

The profile artwork is composed inside a 90% diameter safety circle so it
survives YouTube's circular crop. Shapes are drawn at 4x resolution and then
downsampled with LANCZOS for clean antialiasing.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


OUT_DIR = Path(__file__).resolve().parent
BANNER_PATH = OUT_DIR / "banner_wander_2560x1440.png"
SAFE_RATIO = 0.45
SS = 4

NAVY = (8, 18, 34)
NAVY_2 = (16, 33, 58)
NAVY_3 = (31, 47, 72)
GOLD = (247, 191, 72)
GOLD_2 = (197, 139, 35)
CREAM = (250, 247, 239)
WHITE = (255, 255, 255)
MIST = (218, 224, 231)
ORANGE = (239, 115, 72)
ROSE = (190, 87, 91)


@dataclass(frozen=True)
class FontSet:
    sans: str
    sans_bold: str
    serif: str


def existing_font(*names: str) -> str:
    for name in names:
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            return str(path)
    return ""


FONTS = FontSet(
    sans=existing_font("segoeui.ttf", "arial.ttf"),
    sans_bold=existing_font("segoeuib.ttf", "arialbd.ttf"),
    serif=existing_font("georgia.ttf", "times.ttf"),
)


def font(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path and os.path.exists(path):
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def rgba(color: tuple[int, int, int], alpha: int = 255) -> tuple[int, int, int, int]:
    return (*color, alpha)


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def mix(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(lerp(c1[i], c2[i], t) for i in range(3))


def linear_gradient(size: int, stops: list[tuple[float, tuple[int, int, int]]], diagonal: bool = False) -> Image.Image:
    img = Image.new("RGB", (size, size))
    pix = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1)) if diagonal else y / (size - 1)
            for idx in range(len(stops) - 1):
                p0, c0 = stops[idx]
                p1, c1 = stops[idx + 1]
                if p0 <= t <= p1:
                    local = (t - p0) / max(0.0001, p1 - p0)
                    pix[x, y] = mix(c0, c1, local)
                    break
            else:
                pix[x, y] = stops[-1][1]
    return img.convert("RGBA")


def add_vignette(img: Image.Image, strength: int = 110) -> Image.Image:
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((int(w * 0.03), int(h * 0.02), int(w * 0.97), int(h * 0.98)), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(int(w * 0.16)))
    inv = ImageOps.invert(mask)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, strength))
    out = img.convert("RGBA")
    out.alpha_composite(Image.composite(layer, Image.new("RGBA", (w, h), (0, 0, 0, 0)), inv))
    return out


def fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: str, max_width: int, start: int, min_size: int) -> ImageFont.ImageFont:
    size = start
    while size >= min_size:
        f = font(font_path, size)
        if draw.textbbox((0, 0), text, font=f)[2] <= max_width:
            return f
        size -= 2
    return font(font_path, min_size)


def center_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, fnt: ImageFont.ImageFont, fill, anchor="mm") -> None:
    draw.text(xy, text, font=fnt, fill=fill, anchor=anchor)


def shadowed_text(img: Image.Image, xy: tuple[int, int], text: str, fnt: ImageFont.ImageFont, fill, anchor="mm", blur=10) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((xy[0], xy[1] + 4), text, font=fnt, fill=(0, 0, 0, 150), anchor=anchor)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(layer)
    ImageDraw.Draw(img).text(xy, text, font=fnt, fill=fill, anchor=anchor)


def downsample(img: Image.Image, size: int) -> Image.Image:
    return img.resize((size, size), Image.Resampling.LANCZOS)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def circle_mask(size: int, inset: int = 0) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((inset, inset, size - 1 - inset, size - 1 - inset), fill=255)
    return mask


def draw_orbit_globe(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, line: int, scale: int = 1) -> None:
    gold = rgba(GOLD)
    soft = rgba(CREAM, 215)
    navy = rgba(NAVY, 230)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=gold, width=line)
    draw.arc((cx - r, cy - int(r * 0.62), cx + r, cy + int(r * 0.62)), 0, 360, fill=soft, width=max(2, line // 2))
    draw.arc((cx - int(r * 0.45), cy - r, cx + int(r * 0.45), cy + r), 0, 360, fill=soft, width=max(2, line // 2))
    draw.line((cx - r, cy, cx + r, cy), fill=soft, width=max(2, line // 2))
    draw.line((cx, cy - r, cx, cy + r), fill=soft, width=max(2, line // 2))
    orbit = (cx - int(r * 1.36), cy - int(r * 0.58), cx + int(r * 1.36), cy + int(r * 0.58))
    draw.arc(orbit, 202, 383, fill=rgba(GOLD, 235), width=line)
    theta = math.radians(27)
    px = cx + int(math.cos(theta) * r * 1.34)
    py = cy + int(math.sin(theta) * r * 0.55)
    plane = [
        (px + 0 * scale, py - 20 * scale),
        (px + 58 * scale, py + 2 * scale),
        (px + 0 * scale, py + 22 * scale),
        (px + 13 * scale, py + 4 * scale),
        (px - 45 * scale, py + 4 * scale),
        (px - 55 * scale, py - 4 * scale),
        (px + 13 * scale, py - 4 * scale),
    ]
    draw.polygon(plane, fill=gold)
    draw.line((px - 40 * scale, py + 19 * scale, px - 6 * scale, py + 4 * scale), fill=navy, width=max(2, line // 2))


def draw_compass(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, line: int, scale: int = 1) -> None:
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=rgba(GOLD), width=line)
    draw.ellipse((cx - int(r * 0.74), cy - int(r * 0.74), cx + int(r * 0.74), cy + int(r * 0.74)), outline=rgba(CREAM, 145), width=max(2, line // 3))
    n = [(cx, cy - int(r * 0.88)), (cx + int(r * 0.22), cy + int(r * 0.12)), (cx, cy + int(r * 0.04)), (cx - int(r * 0.22), cy + int(r * 0.12))]
    s = [(cx, cy + int(r * 0.88)), (cx + int(r * 0.16), cy + int(r * 0.06)), (cx, cy + int(r * 0.18)), (cx - int(r * 0.16), cy + int(r * 0.06))]
    draw.polygon(n, fill=rgba(GOLD))
    draw.polygon(s, fill=rgba(CREAM, 235))
    draw.line((cx - r, cy, cx - int(r * 0.84), cy), fill=rgba(CREAM, 180), width=line // 2)
    draw.line((cx + int(r * 0.84), cy, cx + r, cy), fill=rgba(CREAM, 180), width=line // 2)
    draw.line((cx, cy - r, cx, cy - int(r * 0.84)), fill=rgba(CREAM, 180), width=line // 2)
    draw.line((cx, cy + int(r * 0.84), cx, cy + r), fill=rgba(CREAM, 180), width=line // 2)
    f = font(FONTS.serif, max(16 * scale, int(r * 0.28)))
    draw.text((cx, cy - int(r * 0.47)), "N", font=f, fill=rgba(CREAM), anchor="mm")


def draw_pin_play(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, line: int, scale: int = 1) -> None:
    tip = (cx, cy + int(r * 1.18))
    left = (cx - int(r * 0.67), cy - int(r * 0.03))
    right = (cx + int(r * 0.67), cy - int(r * 0.03))
    draw.polygon((left, right, tip), fill=rgba(GOLD))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgba(GOLD))
    inner = int(r * 0.55)
    draw.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=rgba(NAVY, 245))
    tri = [
        (cx - int(inner * 0.24), cy - int(inner * 0.42)),
        (cx - int(inner * 0.24), cy + int(inner * 0.42)),
        (cx + int(inner * 0.48), cy),
    ]
    draw.polygon(tri, fill=rgba(CREAM))
    draw.arc((cx - r, cy - r, cx + r, cy + r), -220, 55, fill=rgba(GOLD_2), width=line)


def draw_wordmark(img: Image.Image, y: int, main_fill, sub_fill, max_width: int, small: bool = False) -> None:
    d = ImageDraw.Draw(img)
    main = fit_text(d, "WANDER", FONTS.serif if not small else FONTS.sans_bold, max_width, 72 if small else 88, 42)
    sub = fit_text(d, "60s", FONTS.sans_bold, max_width, 66 if small else 72, 38)
    shadowed_text(img, (img.width // 2, y), "WANDER", main, main_fill, blur=8)
    d.text((img.width // 2, y + (72 if small else 82)), "60s", font=sub, fill=sub_fill, anchor="mm")


def safe_border(draw: ImageDraw.ImageDraw, size: int, color: tuple[int, int, int], alpha: int = 235) -> None:
    pad = int(size * 0.072)
    draw.ellipse((pad, pad, size - pad, size - pad), outline=rgba(color, alpha), width=max(4, int(size * 0.009)))


def profile_a(size: int) -> Image.Image:
    big = size * SS
    img = linear_gradient(big, [(0.0, NAVY_3), (0.48, NAVY_2), (1.0, NAVY)], diagonal=False)
    img = add_vignette(img, 130)
    d = ImageDraw.Draw(img)
    safe_border(d, big, GOLD)
    cx = cy = big // 2
    d.ellipse((cx - int(big * 0.255), cy - int(big * 0.335), cx + int(big * 0.255), cy + int(big * 0.175)), fill=(255, 255, 255, 13), outline=rgba(CREAM, 34), width=max(2, big // 260))
    draw_orbit_globe(d, cx, int(big * 0.405), int(big * 0.182), max(8, big // 92), SS)
    draw_wordmark(img, int(big * 0.675), rgba(CREAM), rgba(GOLD), int(big * 0.64))
    return downsample(img, size).convert("RGB")


def center_square_banner(size: int) -> Image.Image:
    if not BANNER_PATH.exists():
        return linear_gradient(size, [(0, NAVY_3), (1, NAVY)], diagonal=True).convert("RGB")
    banner = Image.open(BANNER_PATH).convert("RGB")
    w, h = banner.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    crop = banner.crop((left, top, left + side, top + side))
    return crop.resize((size, size), Image.Resampling.LANCZOS)


def profile_b(size: int) -> Image.Image:
    big = size * SS
    texture_size = max(20, big // 220)
    bg = center_square_banner(texture_size).convert("RGBA")
    bg = bg.resize((big, big), Image.Resampling.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(max(16, big // 110)))
    overlay = Image.new("RGBA", (big, big), rgba(NAVY, 166))
    bg.alpha_composite(overlay)
    bg = add_vignette(bg, 120)
    d = ImageDraw.Draw(bg)
    safe_border(d, big, GOLD, 225)
    cx = big // 2
    cy = int(big * 0.395)
    d.ellipse((cx - int(big * 0.23), cy - int(big * 0.23), cx + int(big * 0.23), cy + int(big * 0.23)), fill=(4, 12, 24, 158), outline=rgba(CREAM, 72), width=max(3, big // 150))
    draw_compass(d, cx, cy, int(big * 0.158), max(8, big // 105), SS)
    draw_wordmark(bg, int(big * 0.655), rgba(WHITE), rgba(GOLD), int(big * 0.66), small=True)
    tag = font(FONTS.sans, max(22 * SS, int(big * 0.028)))
    d.text((cx, int(big * 0.802)), "ONE CITY  /  SIXTY SECONDS", font=tag, fill=rgba(MIST, 220), anchor="mm")
    return downsample(bg, size).convert("RGB")


def profile_c(size: int) -> Image.Image:
    big = size * SS
    img = linear_gradient(big, [(0.0, GOLD), (0.34, ORANGE), (0.68, ROSE), (1.0, NAVY)], diagonal=True)
    img = add_vignette(img, 95)
    d = ImageDraw.Draw(img)
    safe_border(d, big, CREAM, 210)
    cx = big // 2
    icon_cy = int(big * 0.39)
    d.ellipse((cx - int(big * 0.235), icon_cy - int(big * 0.235), cx + int(big * 0.235), icon_cy + int(big * 0.235)), fill=(8, 18, 34, 138), outline=rgba(WHITE, 64), width=max(3, big // 150))
    draw_pin_play(d, cx, icon_cy - int(big * 0.018), int(big * 0.13), max(8, big // 100), SS)
    draw_wordmark(img, int(big * 0.655), rgba(WHITE), rgba(GOLD), int(big * 0.66), small=True)
    return downsample(img, size).convert("RGB")


def preview_circle(name: str, source: Image.Image) -> None:
    size = source.width
    bg = Image.new("RGBA", (size, size), (213, 216, 221, 255))
    mask = circle_mask(size)
    bg.paste(source.convert("RGBA"), (0, 0), mask)
    bg.save(OUT_DIR / f"preview_profile_v2_{name}.png", "PNG", optimize=True)


def watermark_base(size: int, variant: str) -> Image.Image:
    big = size * SS
    if variant == "a":
        grad = linear_gradient(big, [(0, NAVY_3), (1, NAVY)], diagonal=False)
        outline = GOLD
        icon = "globe"
    elif variant == "b":
        # Keep the high-resolution watermark under 1 MB by using the same
        # navy/gold language as profile B instead of a photo-texture PNG.
        grad = linear_gradient(big, [(0, NAVY_3), (1, NAVY)], diagonal=True)
        outline = GOLD
        icon = "compass"
    else:
        grad = linear_gradient(big, [(0, GOLD), (0.45, ORANGE), (1, NAVY)], diagonal=True)
        grad.alpha_composite(Image.new("RGBA", (big, big), rgba(NAVY, 38)))
        outline = CREAM
        icon = "pin"

    mask = rounded_mask(big, int(big * 0.22))
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(out)
    d.rounded_rectangle((int(big * 0.035), int(big * 0.035), int(big * 0.965), int(big * 0.965)), radius=int(big * 0.2), outline=rgba(outline, 235), width=max(4, int(big * 0.035)))
    cx = cy = big // 2
    if icon == "globe":
        draw_orbit_globe(d, cx, cy, int(big * 0.235), max(8, big // 52), SS)
    elif icon == "compass":
        d.ellipse((cx - int(big * 0.32), cy - int(big * 0.32), cx + int(big * 0.32), cy + int(big * 0.32)), fill=(4, 12, 24, 118))
        draw_compass(d, cx, cy, int(big * 0.235), max(8, big // 55), SS)
    else:
        d.ellipse((cx - int(big * 0.32), cy - int(big * 0.32), cx + int(big * 0.32), cy + int(big * 0.32)), fill=(4, 12, 24, 102))
        draw_pin_play(d, cx, cy - int(big * 0.025), int(big * 0.215), max(8, big // 55), SS)
    return downsample(out, size)


def save_png(img: Image.Image, path: Path) -> None:
    img.save(path, "PNG", optimize=True)


def file_info(path: Path) -> tuple[int, int, float]:
    with Image.open(path) as im:
        w, h = im.size
    return w, h, path.stat().st_size / 1024


def main() -> None:
    profiles = {
        "a": profile_a,
        "b": profile_b,
        "c": profile_c,
    }

    outputs: list[Path] = []
    for name, maker in profiles.items():
        img1080 = maker(1080)
        p1080 = OUT_DIR / f"profile_v2_{name}_1080.png"
        p800 = OUT_DIR / f"profile_v2_{name}_800.png"
        save_png(img1080, p1080)
        save_png(img1080.resize((800, 800), Image.Resampling.LANCZOS), p800)
        preview_circle(name, img1080)
        outputs.extend([p800, p1080, OUT_DIR / f"preview_profile_v2_{name}.png"])

    for name in ("a", "b", "c"):
        for size in (150, 1024):
            out = OUT_DIR / f"watermark_v2_{name}_{size}.png"
            save_png(watermark_base(size, name), out)
            outputs.append(out)

    print("Wander 60s v2 assets generated:")
    for path in outputs:
        w, h, kb = file_info(path)
        print(f"- {path.name}: {w}x{h}, {kb:.1f} KB")


if __name__ == "__main__":
    main()
