"""Wander 60s 프로필 + 워터마크 — B안(실사진 야경 + 별 나침반), 밝게 리메이크.

쿠마님 피드백(2026-07-18): 2번(실사진+나침반) 채택하되
 ① 나침반을 더 예쁜 compass rose(별 나침반)로 새로 그림
 ② 워터마크도 같은 나침반으로 통일
 ③ 배경/색 전체를 더 밝게

배경: banner_wander_2560x1440.png 크롭+블러 후 밝기·채도 부스트(어두움 해소).
심볼: 골드 compass rose(입체 8방위 별, 북쪽=크림 강조) + 이중 골드 링 + 눈금.
규정: 프로필 원형 크롭 안전원 안에 콘텐츠 전부 / 프로필<4MB, 워터마크<1MB.
"""
import math
import os

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

OUT = os.path.dirname(os.path.abspath(__file__))
BANNER = os.path.join(OUT, "banner_wander_2560x1440.png")

GOLD      = (255, 208, 92)    # 밝은 골드(메인)
GOLD_DEEP = (222, 165, 44)    # 그늘 골드(입체 음영)
GOLD_SOFT = (255, 226, 156)   # 연한 골드(하이라이트/눈금)
CREAM     = (253, 249, 240)   # 북쪽 별 강조
WHITE     = (250, 250, 248)
NAVY_TOP  = (34, 52, 82)      # 밝은 네이비(워터마크 상단)
NAVY_BOT  = (16, 27, 46)      # 딥네이비(워터마크 하단/중심)


def font(sz, kind="sans-bold"):
    table = {
        "sans-bold": ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf"],
        "serif-bold": ["C:/Windows/Fonts/georgiab.ttf", "C:/Windows/Fonts/timesbd.ttf"],
    }
    for f in table[kind] + ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        if os.path.exists(f):
            return ImageFont.truetype(f, sz)
    return ImageFont.load_default()


def vgrad(w, h, top, bottom):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        u = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * u) for i in range(3)))
    return img


def text_shadow(canvas, xy, txt, f, fill, blur=8, off=5, salpha=205):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((xy[0], xy[1] + off), txt, font=f, fill=(0, 0, 0, salpha))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(layer)
    ImageDraw.Draw(canvas).text(xy, txt, font=f, fill=fill + (255,))


def photo_bg(S, blur_div=34, bright=1.5, ov_alpha=64):
    """배너 '하단 도시 빌딩' 영역만 크롭(중앙 텍스트를 피함) → 밝게. 도시 건물이 보이는 배경.

    배너 상단=하늘/석양, 중앙=WANDER 60s 텍스트, 하단=도시 빌딩·도로.
    → 하단 정사각만 크롭해서 글씨 흔적 없이 도시 건물만 배경으로 쓴다.
    """
    src = Image.open(BANNER).convert("RGB")
    w, h = src.size
    cs = int(h * 0.34)          # 하단 34% 높이(=정사각 한 변). 텍스트(중앙)보다 아래.
    top = h - cs                # 맨 아래에 붙임
    left = (w - cs) // 2        # 가로 중앙
    bg = src.crop((left, top, left + cs, top + cs)).resize((S, S), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(max(2, S // blur_div)))
    bg = ImageEnhance.Brightness(bg).enhance(bright)   # 밝게
    bg = ImageEnhance.Color(bg).enhance(1.20)          # 채도 살짝
    bg = bg.convert("RGBA")
    # 가독성용 약한 네이비 오버레이
    bg = Image.alpha_composite(bg, Image.new("RGBA", (S, S), (16, 26, 46, ov_alpha)))
    # 중앙 소프트 글로우(더 밝게)
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([S * 0.16, S * 0.10, S * 0.84, S * 0.70], fill=(255, 242, 214, 46))
    glow = glow.filter(ImageFilter.GaussianBlur(S // 7))
    bg = Image.alpha_composite(bg, glow)
    return bg


def make_compass(size, with_ticks=True):
    """골드 별 나침반(compass rose). 입체 8방위 별 + 이중 링 + 눈금. 북쪽=크림 강조."""
    ss = size * 4
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = ss // 2
    R = ss * 0.46
    lw = max(2, ss // 130)

    # 이중 링
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=GOLD + (255,), width=lw * 2)
    R2 = R * 0.86
    d.ellipse([cx - R2, cy - R2, cx + R2, cy + R2], outline=GOLD_SOFT + (210,), width=lw)

    # 눈금
    if with_ticks:
        for a in range(0, 360, 15):
            ar = math.radians(a)
            r_in = R2 * (0.88 if a % 45 == 0 else 0.93)
            col = GOLD + (235,) if a % 45 == 0 else GOLD_SOFT + (150,)
            d.line([(cx + R2 * math.cos(ar), cy + R2 * math.sin(ar)),
                    (cx + r_in * math.cos(ar), cy + r_in * math.sin(ar))], fill=col, width=lw)

    def star(reach, halfw, bright, shade, dirs):
        star_r = R2 * reach
        base = R2 * halfw
        for ang in dirs:
            a = math.radians(ang)
            perp = a + math.pi / 2
            tip = (cx + star_r * math.cos(a), cy + star_r * math.sin(a))
            bl = (cx + base * math.cos(perp), cy + base * math.sin(perp))
            br = (cx - base * math.cos(perp), cy - base * math.sin(perp))
            ctr = (cx, cy)
            d.polygon([tip, bl, ctr], fill=bright + (255,))   # 밝은 면
            d.polygon([tip, br, ctr], fill=shade + (255,))    # 그늘 면

    # 대각(작은 별) → 주요 4방위(큰 별) → 북쪽 강조 순으로 겹쳐 그림
    star(0.56, 0.05, GOLD_SOFT, GOLD_DEEP, [-45, 45, 135, 225])
    star(0.82, 0.075, GOLD, GOLD_DEEP, [0, 90, 180])   # E, S, W
    star(0.82, 0.075, CREAM, GOLD_DEEP, [-90])         # N(위) 크림 강조

    # 중심 허브
    cr = R2 * 0.11
    d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=NAVY_BOT + (255,))
    d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], outline=GOLD + (255,), width=lw)
    cr2 = cr * 0.42
    d.ellipse([cx - cr2, cy - cr2, cx + cr2, cy + cr2], fill=GOLD + (255,))

    return img.resize((size, size), Image.LANCZOS)


def make_profile_b():
    S = 1080
    img = photo_bg(S)
    cx = cy = S // 2
    m = 76
    ImageDraw.Draw(img).ellipse([m, m, S - m, S - m], outline=GOLD + (255,), width=9)
    m2 = m + 15
    ImageDraw.Draw(img).ellipse([m2, m2, S - m2, S - m2], outline=GOLD_SOFT + (110,), width=3)

    comp_size = 468
    gap = 24
    main_f, main_s = font(96, "sans-bold"), 96
    sub_f, sub_s = font(74, "sans-bold"), 74
    group_h = comp_size + gap + main_s + 2 + sub_s
    top = cy - group_h // 2 - 8

    img.alpha_composite(make_compass(comp_size), (cx - comp_size // 2, top))

    d = ImageDraw.Draw(img)
    mw = int(d.textlength("WANDER", font=main_f))
    my = top + comp_size + gap
    text_shadow(img, (cx - mw // 2, my), "WANDER", main_f, WHITE)
    sw = int(d.textlength("60s", font=sub_f))
    sy = my + main_s + 2
    text_shadow(img, (cx - sw // 2, sy), "60s", sub_f, GOLD)

    img.convert("RGB").save(os.path.join(OUT, "profile_wander_b_1080x1080.png"), "PNG", optimize=True)
    img.convert("RGB").resize((800, 800), Image.LANCZOS).save(
        os.path.join(OUT, "profile_wander_b_800x800.png"), "PNG", optimize=True)

    # 원형 크롭 미리보기
    cmask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(cmask).ellipse([0, 0, S - 1, S - 1], fill=255)
    prev = Image.new("RGBA", (S, S), (210, 214, 220, 255))
    prev.paste(img.convert("RGBA"), (0, 0), cmask)
    prev.convert("RGB").save(os.path.join(OUT, "preview_profile_wander_b.png"), "PNG", optimize=True)
    print("saved: profile_wander_b_1080x1080 / _800x800 / preview_profile_wander_b")


def make_watermark_b():
    for S, rad, bw in [(150, 24, 3), (1024, 150, 18)]:
        # 프로필과 같은 도시 빌딩 배경(작으니 블러 강하게) + 진한 네이비로 눌러 나침반 대비 확보
        base = photo_bg(S, blur_div=15, bright=1.18, ov_alpha=155).convert("RGBA")
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle([2, 2, S - 3, S - 3], radius=rad, fill=255)
        out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        out.paste(base, (0, 0), mask)
        ImageDraw.Draw(out).rounded_rectangle([2, 2, S - 3, S - 3], radius=rad,
                                              outline=GOLD + (240,), width=bw)
        comp = make_compass(int(S * 0.76), with_ticks=(S >= 300))
        out.alpha_composite(comp, ((S - comp.width) // 2, (S - comp.height) // 2))
        out.save(os.path.join(OUT, f"watermark_wander_b_{S}.png"), "PNG", optimize=True)
    print("saved: watermark_wander_b_150 / watermark_wander_b_1024 (city bg)")


if __name__ == "__main__":
    make_profile_b()
    make_watermark_b()
    print("Wander 60s B assets done.")
