"""Wander 60s 채널 자산 — 프로필 + 워터마크.

배너는 쿠마님이 고른 channel_art/1_wander60s.png(2560x1440, 실사진)를 사용한다.
여기서는 배너와 톤을 맞춘 심볼 로고(위치핀 + 재생버튼)를 PIL로 그려
원형 크롭/소형 표시에 강한 프로필·워터마크를 만든다.

사이즈 규정(YouTube):
- 프로필: 800x800 권장(1080도 함께 제공), 원형 크롭 → 안전 반경 내에 콘텐츠 전부
- 워터마크: 150x150 권장(1024 원본도 제공), 배경 박스가 있어야 밝은/어두운 영상 위에서 식별

톤: 골드(#F7BF1D) + 딥네이비 — 배너 "WANDER 60s"의 색과 통일.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

NAME_MAIN = "WANDER"
NAME_SUB = "60s"

PALETTE = {
    "bg_top":     (46, 54, 82),    # 상단 네이비(석양 기운 살짝)
    "bg_bottom":  (10, 18, 32),    # 하단 딥네이비(도시 야경)
    "accent":     (247, 191, 29),  # 골드 옐로우(배너 60s)
    "accent_dk":  (186, 140, 10),  # 골드 섀도/외곽선
    "text_white": (248, 248, 244),
    "navy_deep":  (11, 20, 35),
    "cream":      (250, 246, 236),
}


def find_font(size, bold=True):
    for f in [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def vgrad(w, h, top, bottom):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        u = y / max(1, h - 1)
        d.line([(0, y), (w, y)],
               fill=tuple(int(top[i] + (bottom[i] - top[i]) * u) for i in range(3)))
    return img


def add_vignette(img, strength=115):
    w, h = img.size
    v = Image.new("L", (w, h), 0)
    ImageDraw.Draw(v).ellipse([int(w * 0.06), int(h * 0.06), int(w * 0.94), int(h * 0.94)], fill=255)
    v = v.filter(ImageFilter.GaussianBlur(int(w / 7)))
    inv = Image.eval(v, lambda a: 255 - a)
    img = img.convert("RGBA")
    img.paste(Image.new("RGBA", (w, h), (0, 0, 0, strength)), (0, 0), inv)
    return img


def make_pin(size):
    """골드 위치핀(물방울) + 안쪽 네이비 원 + 크림 재생버튼. 투명 배경 RGBA. 4x 슈퍼샘플."""
    ss = size * 4
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = ss // 2
    r = int(ss * 0.30)
    cyc = int(ss * 0.35)
    tip_y = int(ss * 0.95)
    ang = math.radians(46)
    lx = cx - int(r * math.sin(ang)); ly = cyc + int(r * math.cos(ang))
    rx = cx + int(r * math.sin(ang)); ry = cyc + int(r * math.cos(ang))
    gold = PALETTE["accent"]; gdk = PALETTE["accent_dk"]
    # 핀 몸통(삼각형) + 머리(원)
    d.polygon([(lx, ly), (rx, ry), (cx, tip_y)], fill=gold + (255,))
    d.ellipse([cx - r, cyc - r, cx + r, cyc + r], fill=gold + (255,))
    # 외곽선(어두운 골드)
    lw = max(3, ss // 130)
    d.line([(lx, ly), (cx, tip_y)], fill=gdk + (255,), width=lw)
    d.line([(rx, ry), (cx, tip_y)], fill=gdk + (255,), width=lw)
    d.arc([cx - r, cyc - r, cx + r, cyc + r], start=-235, end=55, fill=gdk + (255,), width=lw)
    # 안쪽 네이비 원(구멍)
    ir = int(r * 0.60)
    d.ellipse([cx - ir, cyc - ir, cx + ir, cyc + ir], fill=PALETTE["navy_deep"] + (255,))
    # 재생버튼(크림 삼각형, 오른쪽)
    ph = int(ir * 0.95); pw = int(ir * 0.82)
    pcx = cx + int(ir * 0.12)
    d.polygon([(pcx - pw // 2, cyc - ph // 2), (pcx - pw // 2, cyc + ph // 2), (pcx + pw // 2, cyc)],
              fill=PALETTE["cream"] + (255,))
    return img.resize((size, size), Image.LANCZOS)


def draw_text_shadow(canvas, xy, text, font, fill):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((xy[0], xy[1] + 4), text, font=font, fill=(0, 0, 0, 180))
    layer = layer.filter(ImageFilter.GaussianBlur(6))
    canvas.alpha_composite(layer)
    ImageDraw.Draw(canvas).text(xy, text, font=font, fill=fill + (255,))


# ───────── 프로필 ─────────
def make_profile():
    S = 1080
    img = add_vignette(vgrad(S, S, PALETTE["bg_top"], PALETTE["bg_bottom"]))
    cx = cy = S // 2
    m = 80
    ImageDraw.Draw(img).ellipse([m, m, S - m, S - m], outline=PALETTE["accent"] + (255,), width=9)
    safe_r = S // 2 - m - 16

    def fit(txt, base, mn):
        s = base
        while s > mn:
            f = find_font(s, True)
            if ImageDraw.Draw(img).textlength(txt, font=f) <= 2 * safe_r * 0.86:
                return f, s
            s -= 2
        return find_font(mn, True), mn

    main_f, main_s = fit(NAME_MAIN, 100, 52)
    sub_f, sub_s = fit(NAME_SUB, 80, 44)

    mark_size = 400
    gap1, gap2 = 22, 4
    group_h = mark_size + gap1 + main_s + gap2 + sub_s
    top = cy - group_h // 2

    mark = make_pin(mark_size)
    img.alpha_composite(mark, (cx - mark_size // 2, top))

    main_w = int(ImageDraw.Draw(img).textlength(NAME_MAIN, font=main_f))
    main_y = top + mark_size + gap1
    draw_text_shadow(img, (cx - main_w // 2, main_y), NAME_MAIN, main_f, PALETTE["text_white"])

    sub_w = int(ImageDraw.Draw(img).textlength(NAME_SUB, font=sub_f))
    sub_y = main_y + main_s + gap2
    draw_text_shadow(img, (cx - sub_w // 2, sub_y), NAME_SUB, sub_f, PALETTE["accent"])

    img.convert("RGB").save(os.path.join(OUT_DIR, "profile_wander_1080x1080.png"), "PNG", optimize=True)
    img.convert("RGB").resize((800, 800), Image.LANCZOS).save(
        os.path.join(OUT_DIR, "profile_wander_800x800.png"), "PNG", optimize=True)

    # 원형 크롭 시뮬레이션(YouTube가 원형으로 자름 → 잘림 육안검증용)
    prof = img.convert("RGBA")
    cmask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(cmask).ellipse([0, 0, S - 1, S - 1], fill=255)
    prev = Image.new("RGBA", (S, S), (42, 42, 46, 255))
    prev.paste(prof, (0, 0), cmask)
    prev.convert("RGB").save(os.path.join(OUT_DIR, "_preview_profile_circle.png"), "PNG", optimize=True)
    print("saved: profile_wander_1080x1080.png / profile_wander_800x800.png / _preview_profile_circle.png")


# ───────── 워터마크 ─────────
def make_watermark():
    for S, rad, bw, ratio, fn in [
        (150, 24, 3, 0.72, "watermark_wander_150x150.png"),
        (1024, 150, 18, 0.72, "watermark_wander_1024x1024.png"),
    ]:
        grad = vgrad(S, S, PALETTE["bg_top"], PALETTE["bg_bottom"]).convert("RGBA")
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle([2, 2, S - 3, S - 3], radius=rad, fill=255)
        out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        out.paste(grad, (0, 0), mask)
        ImageDraw.Draw(out).rounded_rectangle([2, 2, S - 3, S - 3], radius=rad,
                                              outline=PALETTE["accent"] + (230,), width=bw)
        pin = make_pin(int(S * ratio))
        out.alpha_composite(pin, ((S - pin.width) // 2, (S - pin.height) // 2 - int(S * 0.02)))
        out.save(os.path.join(OUT_DIR, fn), "PNG", optimize=True)
    print("saved: watermark_wander_150x150.png / watermark_wander_1024x1024.png")


if __name__ == "__main__":
    make_profile()
    make_watermark()
    print("Wander 60s assets done.")
