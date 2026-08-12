"""자동 썸네일 — 완성 영상 프레임 + 대형 후크 텍스트 (순수 PIL, ImageMagick 불필요).

완성된 최종 mp4(자막·후크가 얹힌 영상)의 duration*0.25 지점 프레임을 1080x1920으로
뽑아 후크 텍스트를 크게 얹는다. 블러·과한 감광은 의도적으로 뺀다 — 채널 그리드에서는
선명한 프레임이 유리하다(밝기 x0.85 + 세로 중앙부 얕은 그라데이션 밴드만).
모든 예외는 잡아서 False 반환 — 썸네일 실패가 영상 발행을 절대 막지 않는다(fail-soft).
"""
import logging
import os

log = logging.getLogger("pipeline.thumbnail")

WIDTH, HEIGHT = 1080, 1920
FALLBACK_BG = (15, 18, 32)       # 프레임 추출 실패 시 단색 배경
MAX_BYTES = 2 * 1024 * 1024      # YouTube 썸네일 한도 2MB
MIN_FONT_SIZE = 80               # 폭 초과 시 축소 하한
FONT_STEP = 14                   # 축소 단위
STROKE_W = 10                    # 검정 외곽선 두께


def _find_font():
    """굵은 폰트 단계 폴백 — config.FONT_PATH → DejaVu-Bold → Liberation-Bold → 시스템.

    composer._find_font 로직 참고하되 독립 구현(파이프라인 모듈 간 의존 최소화).
    """
    try:
        import config as _cfg
        cfg_font = getattr(_cfg, "FONT_PATH", None)
    except Exception:
        cfg_font = None
    candidates = [
        cfg_font,
        # Linux (GitHub Actions ubuntu)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        # Windows (로컬 테스트)
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for f in candidates:
        if f and os.path.exists(f):
            return f
    return None


def _base_frame(video_path: str):
    """duration*0.25 지점 프레임 → 1080x1920 PIL 이미지 (LANCZOS). 실패 시 단색 폴백."""
    from PIL import Image
    try:
        from moviepy.editor import VideoFileClip
        with VideoFileClip(video_path) as clip:
            t = max(0.0, float(clip.duration or 0.0) * 0.25)
            frame = clip.get_frame(t)
        img = Image.fromarray(frame).convert("RGB")
        if img.size != (WIDTH, HEIGHT):
            img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
        return img
    except Exception as e:
        log.warning(f"프레임 추출 실패 → 단색 폴백: {e}")
        return Image.new("RGB", (WIDTH, HEIGHT), FALLBACK_BG)


def _dim_and_band(img):
    """밝기 x0.85 + 세로 중앙부 얕은 어둠 밴드(텍스트 가독용). 블러/강한 감광은 금지."""
    from PIL import Image, ImageDraw, ImageEnhance
    img = ImageEnhance.Brightness(img).enhance(0.85)
    band = Image.new("L", (WIDTH, HEIGHT), 0)
    d = ImageDraw.Draw(band)
    center, half = HEIGHT // 2, int(HEIGHT * 0.22)  # 밴드 = 화면 세로의 44%
    for y in range(center - half, center + half):
        # 중앙이 가장 진하고 가장자리로 갈수록 0 — 최대 알파 90/255 ('살짝'만)
        a = int(90 * (1.0 - abs(y - center) / half))
        if a > 0:
            d.line([(0, y), (WIDTH, y)], fill=a)
    img.paste(Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)), (0, 0), band)
    return img


def _line_w(draw, line, font):
    box = draw.textbbox((0, 0), line, font=font, stroke_width=STROKE_W)
    return box[2] - box[0]


def _wrap(draw, text, font, max_w):
    """최대 2줄 워드랩. 이 폰트 크기로 두 줄에도 안 들어가면 None(호출부가 축소)."""
    if _line_w(draw, text, font) <= max_w:
        return [text]
    words = text.split()
    if len(words) < 2:
        return None
    best = None
    for i in range(1, len(words)):
        l1, l2 = " ".join(words[:i]), " ".join(words[i:])
        w1, w2 = _line_w(draw, l1, font), _line_w(draw, l2, font)
        if w1 <= max_w and w2 <= max_w:
            spread = abs(w1 - w2)  # 두 줄 폭이 비슷할수록 좋다
            if best is None or spread < best[0]:
                best = (spread, [l1, l2])
    return best[1] if best else None


def generate_thumbnail(video_path, out_path, text, accent=(255, 240, 0)) -> bool:
    """완성 영상 프레임 위에 후크 텍스트를 얹은 썸네일 생성. 성공 시 True.

    text 는 호출자가 결정한 그대로(대문자 여부 포함) 그린다.
    모든 예외를 흡수하고 False 를 반환한다 — 발행 흐름을 절대 막지 않는다.
    """
    try:
        from PIL import ImageDraw, ImageFont

        text = (text or "").strip()
        if not text:
            log.warning("썸네일 텍스트 없음 → 생성 스킵")
            return False

        font_path = _find_font()
        if not font_path:
            log.warning("사용 가능한 폰트 없음 → 썸네일 스킵")
            return False

        try:
            import config as _cfg
            size = int(getattr(_cfg, "THUMBNAIL_FONT_SIZE", 190))
        except Exception:
            size = 190

        img = _dim_and_band(_base_frame(video_path))
        draw = ImageDraw.Draw(img)

        # 폭 초과 시 FONT_STEP 씩 축소하며 1~2줄 배치 탐색
        max_w = int(WIDTH * 0.92)
        lines = None
        while size >= MIN_FONT_SIZE:
            font = ImageFont.truetype(font_path, size)
            lines = _wrap(draw, text, font, max_w)
            if lines:
                break
            size -= FONT_STEP
        if not lines:
            # 최소 크기로도 안 들어가면(공백 없는 초장문 단어) 40px까지 더 줄이고,
            # 그래도 넘치면 말줄임 — x가 음수가 되어 좌우가 잘리는 것 방지 (codex 지적)
            size = MIN_FONT_SIZE
            font = ImageFont.truetype(font_path, size)
            while size > 40 and _line_w(draw, text, font) > max_w:
                size -= FONT_STEP
                font = ImageFont.truetype(font_path, size)
            if _line_w(draw, text, font) > max_w:
                t = text
                while len(t) > 1 and _line_w(draw, t + "…", font) > max_w:
                    t = t[:-1]
                text = t + "…"
            lines = [text]

        # 세로 중앙 정렬 + 가로 중앙 정렬
        ascent, descent = font.getmetrics()
        line_h = ascent + descent + STROKE_W
        gap = int(line_h * 0.12)
        total_h = line_h * len(lines) + gap * (len(lines) - 1)
        y = (HEIGHT - total_h) // 2
        for line in lines:
            x = (WIDTH - _line_w(draw, line, font)) // 2
            draw.text((x, y), line, font=font, fill=accent,
                      stroke_width=STROKE_W, stroke_fill=(0, 0, 0))
            y += line_h + gap

        # 2MB 한도 내 저장 — 초과 시 quality 를 낮춰 재시도
        quality = 92
        while True:
            img.save(out_path, "JPEG", quality=quality, optimize=True)
            if os.path.getsize(out_path) <= MAX_BYTES or quality <= 40:
                break
            quality -= 8
        size_kb = os.path.getsize(out_path) / 1024
        if os.path.getsize(out_path) > MAX_BYTES:
            log.warning(f"썸네일 2MB 초과({size_kb:.0f}KB, q={quality}) → 스킵")
            try:
                os.remove(out_path)
            except OSError:
                pass
            return False
        log.info(f"썸네일 생성: {os.path.basename(out_path)} "
                 f"({size_kb:.0f}KB, q={quality}, {len(lines)}줄, font={size}px)")
        return True
    except Exception as e:
        log.warning(f"썸네일 생성 실패 (영상 발행은 정상 진행): {e}")
        return False
