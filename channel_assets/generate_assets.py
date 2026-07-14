import math

"""Animal Trivia 60s 채널 자산 v3 — 보내주신 곰발바닥 SVG 마크 활용.

3종:
- banner_2048x1152.png  : 채널 상단 배너 (YouTube 권장 2048x1152, TV-safe 1235x338)
- profile_1080x1080.png : 채널 프로필 (YouTube 800x800 다운스케일 대응)
- watermark_150x150.png : 동영상 우측 하단 표시용 워터마크 (투명 배경)

마크 출처: bear_paw_mark.png (1400x1400, 사용자 제공 SVG 렌더)
톤 유지: 사파리 그린/골드 (NatGeo Wild · BBC Earth 풍).
"""
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
MARK_PATH = os.path.join(OUT_DIR, "bear_paw_mark.png")

CHANNEL_NAME = "Animal Trivia 60s"
TAGLINE_EN = "wild facts in 60 seconds"

PALETTE = {
    "bg_dark":   (22, 42, 30),
    "bg_mid":    (52, 90, 60),
    "bg_light":  (175, 145, 95),
    "accent":    (240, 200, 70),
    "text_white": (248, 245, 232),
    "text_dim":  (185, 175, 140),
    "dark":      (12, 22, 16),
}


def find_font(size, bold=True):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for f in candidates:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def safari_gradient(w, h):
    img = Image.new("RGB", (w, h), 0)
    pix = img.load()
    c1, c2, c3 = PALETTE["bg_dark"], PALETTE["bg_mid"], PALETTE["bg_light"]
    diag = w + h
    for y in range(h):
        for x in range(w):
            t = (x + y) / diag
            if t < 0.55:
                u = t / 0.55
                r = int(c1[0] + (c2[0] - c1[0]) * u)
                g = int(c1[1] + (c2[1] - c1[1]) * u)
                b = int(c1[2] + (c2[2] - c1[2]) * u)
            else:
                u = (t - 0.55) / 0.45
                r = int(c2[0] + (c3[0] - c2[0]) * u)
                g = int(c2[1] + (c3[1] - c2[1]) * u)
                b = int(c2[2] + (c3[2] - c2[2]) * u)
            pix[x, y] = (r, g, b)
    return img


def add_film_grain(img, opacity=18):
    random.seed(42)
    w, h = img.size
    grain_rgba = Image.new("RGBA", (w, h))
    gpix = grain_rgba.load()
    for y in range(h):
        for x in range(w):
            v = random.randint(80, 175)
            gpix[x, y] = (v, v, v, opacity)
    base = img.convert("RGBA")
    return Image.alpha_composite(base, grain_rgba).convert("RGB")


def load_mark(size):
    """곰발바닥 마크를 size×size로 리샘플 (LANCZOS, 고품질)."""
    m = Image.open(MARK_PATH).convert("RGBA")
    return m.resize((size, size), Image.LANCZOS)


def paste_mark_with_glow(canvas, mark, cx, cy, size, glow=True, glow_color=(240, 200, 70, 90), glow_blur=40):
    """마크를 canvas 위에 합성. glow=True면 골든 글로우 후광."""
    m = mark if mark.size == (size, size) else mark.resize((size, size), Image.LANCZOS)
    x = cx - size // 2
    y = cy - size // 2
    if glow:
        # 알파 채널을 마스크로 글로우 레이어 생성
        alpha = m.split()[-1]
        glow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        gd = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        gd_pix = Image.new("RGBA", (size, size), glow_color)
        gd.paste(gd_pix, (0, 0), alpha)
        glow_layer.paste(gd, (x, y), gd)
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_blur))
        canvas.alpha_composite(glow_layer)
    canvas.paste(m, (x, y), m)


def text_with_blur_shadow(canvas, xy, text, fnt, fill, shadow=(0, 0, 0, 220), blur=10, offset=(0, 6)):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(layer)
    sd.text((xy[0] + offset[0], xy[1] + offset[1]), text, font=fnt, fill=shadow)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(layer)
    d = ImageDraw.Draw(canvas)
    d.text(xy, text, font=fnt, fill=fill)


# ───────── 1. 배너 ─────────
def make_banner():
    """
    YouTube 권장 풀사이즈: 2560×1440.
    안전영역(모든 기기 표시): 가운데 1546×423 박스. 모든 콘텐츠는 이 박스 안에.
    데스크톱 표시영역: 가운데 가로 2560×423.
    """
    W, H = 2560, 1440
    SAFE_W, SAFE_H = 1546, 423
    SAFE_X = (W - SAFE_W) // 2          # 507
    SAFE_Y = (H - SAFE_H) // 2          # 508
    SAFE_CX = W // 2                    # 1280
    SAFE_CY = H // 2                    # 720

    img = safari_gradient(W, H)
    img = add_film_grain(img, opacity=20).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # 풀잎 — 데스크톱 영역(safe_y 양 끝) 안쪽 살짝 위 (TV-safe 박스 밖이지만 데스크톱에는 보임)
    grass_top_y = SAFE_Y + SAFE_H - 30   # 안전영역 하단 살짝 위
    grass_h_max = 70
    for gx in range(80, 480, 32):
        draw.polygon([(gx, grass_top_y), (gx + 9, grass_top_y - grass_h_max), (gx + 18, grass_top_y)],
                     fill=(*PALETTE["bg_dark"], 180))
    for gx in range(W - 480, W - 80, 32):
        draw.polygon([(gx, grass_top_y), (gx + 9, grass_top_y - grass_h_max), (gx + 18, grass_top_y)],
                     fill=(*PALETTE["bg_dark"], 180))

    # ── 가로 배치 (마크 좌, 타이틀+태그라인 우) ──
    # 안전영역 높이 423px 안에 마크 320 + 텍스트가 모두 들어가야 함.
    mark_size = 340
    gap_mark_text = 50

    # 타이틀 폰트 — 안전영역에 맞춰 자동 fit
    title_font_size = 180
    title_font = find_font(title_font_size, bold=True)
    title_w = int(draw.textlength(CHANNEL_NAME, font=title_font))
    # 텍스트 가용 가로폭: 안전영역 가로 - 마크 - gap - 우측 마진
    text_max_w = SAFE_W - mark_size - gap_mark_text - 20
    while title_w > text_max_w and title_font_size > 90:
        title_font_size -= 4
        title_font = find_font(title_font_size, bold=True)
        title_w = int(draw.textlength(CHANNEL_NAME, font=title_font))

    tag_font_size = 56
    tag_font = find_font(tag_font_size, bold=False)
    tag_w = int(draw.textlength(TAGLINE_EN, font=tag_font))
    gap_title_tag = 18

    text_block_h = title_font_size + gap_title_tag + tag_font_size

    # 그룹 전체 가로 폭
    text_w_max = max(title_w, tag_w)
    group_w = mark_size + gap_mark_text + text_w_max
    group_left = int(SAFE_CX - group_w // 2)

    # 마크 (좌)
    mark_cx = group_left + mark_size // 2
    mark_cy = SAFE_CY
    mark = load_mark(mark_size)
    paste_mark_with_glow(img, mark, cx=mark_cx, cy=mark_cy, size=mark_size,
                         glow=True, glow_color=(240, 200, 70, 90), glow_blur=55)

    # 텍스트 (우)
    text_left = group_left + mark_size + gap_mark_text
    text_top = SAFE_CY - text_block_h // 2

    title_x = text_left
    title_y = text_top
    # blur shadow + 외곽선 + 흰 텍스트
    text_with_blur_shadow(img, (title_x, title_y), CHANNEL_NAME, title_font,
                          fill=PALETTE["text_white"] + (255,), blur=10, offset=(0, 6))
    d2 = ImageDraw.Draw(img)
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            if dx == 0 and dy == 0:
                continue
            d2.text((title_x + dx, title_y + dy), CHANNEL_NAME,
                    font=title_font, fill=(0, 0, 0, 230))
    d2.text((title_x, title_y), CHANNEL_NAME, font=title_font, fill=PALETTE["text_white"] + (255,))

    # 태그라인 (타이틀 바로 아래, 좌측 정렬)
    tag_y = title_y + title_font_size + gap_title_tag
    d2.text((title_x, tag_y), TAGLINE_EN, font=tag_font, fill=PALETTE["accent"] + (255,))

    img.convert("RGB").save(os.path.join(OUT_DIR, "banner_2560x1440.png"), "PNG", optimize=True)
    # 호환을 위해 2048x1152 버전도 함께 (LANCZOS 다운스케일)
    img.convert("RGB").resize((2048, 1152), Image.LANCZOS).save(
        os.path.join(OUT_DIR, "banner_2048x1152.png"), "PNG", optimize=True)
    print("saved: banner_2560x1440.png  (YouTube 권장 풀사이즈, TV-safe 1546x423 안에 콘텐츠 배치)")
    print("saved: banner_2048x1152.png  (다운스케일 호환 버전)")


# ───────── 2. 프로필 ─────────
def make_profile():
    """
    프로필은 YouTube가 원형으로 크롭한다.
    → 모든 콘텐츠(마크 + 채널명)가 가운데 원 안에 완전히 들어가야 함.
    원형 크롭 반경 = S * 0.5 (가장자리). 안전 반경(권장) = S * 0.45 ≈ 486px (S=1080 기준).
    """
    S = 1080
    img = safari_gradient(S, S)
    img = add_film_grain(img, opacity=22).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # 골드 외곽 원 (안쪽으로 충분히 마진 두기 → YouTube 크롭에도 살아남음)
    ring_margin = 80
    ring_width = 8
    ring_box = [ring_margin, ring_margin, S - ring_margin, S - ring_margin]
    draw.ellipse(ring_box, outline=PALETTE["accent"] + (255,), width=ring_width)

    cx = S // 2
    cy = S // 2
    # 원의 안전 반경 = 외곽선 안쪽 + 추가 마진 (외곽선 두께 + stroke 외곽선 + 여유)
    safe_r = (S // 2) - ring_margin - 14
    side_margin = 14   # 텍스트 stroke(±3) + 여유

    # 그룹 레이아웃: 마크(위) + gap + 채널명(아래). 마크 키우고 글씨는 chord에 fit.
    mark_size = 480
    gap = 30

    # 채널명 폰트 자동 fit — 채널명 y 위치에서의 원 chord 안에 stroke 포함 들어가게
    def chord_at(r, dy):
        return 2.0 * math.sqrt(max(0, r * r - dy * dy))

    name_font_size = 100
    while name_font_size > 50:
        name_font = find_font(name_font_size, bold=True)
        name_w = int(draw.textlength(CHANNEL_NAME, font=name_font))
        # 그룹 위치 (현 폰트 사이즈 기준)
        group_h_try = mark_size + gap + name_font_size
        group_top_try = cy - group_h_try // 2
        name_y_top_try = group_top_try + mark_size + gap
        name_y_center_try = name_y_top_try + name_font_size // 2
        dy = abs(name_y_center_try - cy)
        chord = chord_at(safe_r, dy)
        text_w_with_stroke = name_w + 6   # ±3 stroke 양쪽
        if text_w_with_stroke + 2 * side_margin <= chord:
            break
        name_font_size -= 2
    name_font = find_font(name_font_size, bold=True)
    name_w = int(draw.textlength(CHANNEL_NAME, font=name_font))

    group_h = mark_size + gap + name_font_size
    group_top = cy - group_h // 2

    # 마크
    mark_cy = group_top + mark_size // 2
    mark = load_mark(mark_size)
    paste_mark_with_glow(img, mark, cx=cx, cy=mark_cy, size=mark_size,
                         glow=True, glow_color=(240, 200, 70, 80), glow_blur=45)

    # 채널명 (마크 바로 아래, 원 안에 완전히 포함)
    name_y = group_top + mark_size + gap
    name_x = cx - name_w // 2

    text_with_blur_shadow(img, (name_x, name_y), CHANNEL_NAME, name_font,
                          fill=PALETTE["text_white"] + (255,), blur=6, offset=(0, 4))
    d2 = ImageDraw.Draw(img)
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            if dx == 0 and dy == 0:
                continue
            d2.text((name_x + dx, name_y + dy), CHANNEL_NAME, font=name_font, fill=(0, 0, 0, 255))
    d2.text((name_x, name_y), CHANNEL_NAME, font=name_font, fill=(255, 255, 255, 255))

    img.convert("RGB").save(os.path.join(OUT_DIR, "profile_1080x1080.png"), "PNG", optimize=True)
    # YouTube가 다운스케일하니 800px 버전도 직접 제공
    img.convert("RGB").resize((800, 800), Image.LANCZOS).save(
        os.path.join(OUT_DIR, "profile_800x800.png"), "PNG", optimize=True)
    print("saved: profile_1080x1080.png  (원 안에 마크+채널명 완전 포함)")
    print("saved: profile_800x800.png    (YouTube 권장 사이즈)")


# ───────── 3. 워터마크 ─────────
def make_watermark():
    """
    YouTube 동영상 우측 하단 표시용. 사파리 그린 라운드 박스 + 골드 테두리 + 곰발바닥 마크.
    배경 박스가 있어야 검정/밝은 영상 위에서도 식별이 잘 된다.
    """
    S = 150
    # 사파리 그라데이션 + 라운드 박스 마스크
    grad = safari_gradient(S, S).convert("RGBA")
    mask = Image.new("L", (S, S), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([2, 2, S - 2, S - 2], radius=22, fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)

    # 골드 테두리
    odraw = ImageDraw.Draw(out)
    odraw.rounded_rectangle([2, 2, S - 2, S - 2], radius=22,
                            outline=(*PALETTE["accent"], 230), width=3)

    # 곰발바닥 마크 (가운데, 박스 내부 안전 마진 18)
    mark_size = S - 36
    mark = load_mark(mark_size)
    mark_x = (S - mark_size) // 2
    mark_y = (S - mark_size) // 2
    out.paste(mark, (mark_x, mark_y), mark)

    out.save(os.path.join(OUT_DIR, "watermark_150x150.png"), "PNG", optimize=True)

    # 고해상도 1024 버전 (필요 시 업로드용)
    HI = 1024
    grad_hi = safari_gradient(HI, HI).convert("RGBA")
    mask_hi = Image.new("L", (HI, HI), 0)
    mdraw_hi = ImageDraw.Draw(mask_hi)
    mdraw_hi.rounded_rectangle([16, 16, HI - 16, HI - 16], radius=150, fill=255)
    out_hi = Image.new("RGBA", (HI, HI), (0, 0, 0, 0))
    out_hi.paste(grad_hi, (0, 0), mask_hi)
    odraw_hi = ImageDraw.Draw(out_hi)
    odraw_hi.rounded_rectangle([16, 16, HI - 16, HI - 16], radius=150,
                               outline=(*PALETTE["accent"], 230), width=20)
    mark_hi_size = HI - 240
    mark_hi = load_mark(mark_hi_size)
    out_hi.paste(mark_hi, ((HI - mark_hi_size) // 2, (HI - mark_hi_size) // 2), mark_hi)
    out_hi.save(os.path.join(OUT_DIR, "watermark_1024x1024.png"), "PNG", optimize=True)

    print("saved: watermark_150x150.png   (사파리 그린 박스 + 골드 테두리 + 곰발바닥)")
    print("saved: watermark_1024x1024.png (고해상도 원본)")


if __name__ == "__main__":
    make_banner()
    make_profile()
    make_watermark()
    print("\nAnimal Trivia 60s v3 (bear paw mark) - done.")
