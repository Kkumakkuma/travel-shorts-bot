"""자동 썸네일 — 배경 소재 클립 프레임 + 상단 대형 후크 텍스트 (순수 PIL, ImageMagick 불필요).

2026-09-03 개편: 소스를 완성 mp4가 아니라 **소재 클립(여행봇은 도입샷=도시 전경, 없으면
첫 배경 클립)의 1.0초 프레임**으로 바꿨다. 완성본에는 이미 자막·워터마크가 얹혀 있어
썸네일에 글자가 두 겹으로 들어갔다.
구성 = 배경 프레임(9:16 센터크롭) + 상단 반투명 밴드 위 영어 대문자 후크(흰 글자+검은
외곽선, 최대 2줄) + 하단 작은 채널 태그. 한글은 쓰지 않는다(글로벌 노출).
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


def _fit_portrait(img):
    """가로/정사각 소스를 9:16 센터크롭 후 1080x1920으로. (배경 클립은 landscape가 흔하다)"""
    from PIL import Image
    w, h = img.size
    target = WIDTH / HEIGHT
    if w <= 0 or h <= 0:
        return Image.new("RGB", (WIDTH, HEIGHT), FALLBACK_BG)
    src = w / h
    if src > target:            # 좌우가 넓다 → 좌우 크롭
        new_w = int(round(h * target))
        x = (w - new_w) // 2
        img = img.crop((x, 0, x + new_w, h))
    elif src < target:          # 위아래가 길다 → 상하 크롭
        new_h = int(round(w / target))
        y = (h - new_h) // 2
        img = img.crop((0, y, w, y + new_h))
    if img.size != (WIDTH, HEIGHT):
        img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    return img


def _base_frame(video_path: str, frame_time: float = 1.0):
    """지정 시점(기본 1.0초) 프레임 → 9:16 크롭 1080x1920 PIL 이미지. 실패 시 단색 폴백."""
    from PIL import Image
    try:
        from moviepy.editor import VideoFileClip
        with VideoFileClip(video_path) as clip:
            dur = float(clip.duration or 0.0)
            t = min(max(0.0, float(frame_time)), max(0.0, dur - 0.05))
            frame = clip.get_frame(t)
        return _fit_portrait(Image.fromarray(frame).convert("RGB"))
    except Exception as e:
        log.warning(f"프레임 추출 실패 → 단색 폴백: {e}")
        return Image.new("RGB", (WIDTH, HEIGHT), FALLBACK_BG)


def _dim_and_band(img, band_top: int, band_h: int):
    """밝기 x0.88 + 텍스트가 앉을 상단 구간에만 반투명 어둠 밴드. 블러/강한 감광은 금지."""
    from PIL import Image, ImageDraw, ImageEnhance
    img = ImageEnhance.Brightness(img).enhance(0.88)
    band = Image.new("L", (WIDTH, HEIGHT), 0)
    d = ImageDraw.Draw(band)
    top = max(0, int(band_top))
    bottom = min(HEIGHT, int(band_top + band_h))
    center = (top + bottom) / 2.0
    half = max(1.0, (bottom - top) / 2.0)
    for y in range(top, bottom):
        # 밴드 중앙이 가장 진하고 위아래 끝으로 갈수록 0 — 최대 알파 150/255
        a = int(150 * (1.0 - abs(y - center) / half))
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


def generate_thumbnail(source_path, out_path, text, channel_tag=None,
                       frame_time=1.0, accent=(255, 255, 255)) -> bool:
    """배경 클립 프레임 위에 상단 후크 텍스트 + 하단 채널 태그를 얹은 썸네일. 성공 시 True.

    source_path : 소재 클립 mp4 (여행봇은 도입샷 우선, 없으면 첫 배경 클립 — 완성본이
                  아니라 소재를 쓰는 이유는 글자 이중 노출 방지)
    text        : 영어 후크. 호출자가 정한 그대로(대문자 여부 포함) 그린다. 한글 금지.
    channel_tag : 하단 작은 글씨(기본 config.CHANNEL_HANDLE → "@Wander60s")
    모든 예외를 흡수하고 False 를 반환한다 — 발행 흐름을 절대 막지 않는다.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

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
            if channel_tag is None:
                channel_tag = getattr(_cfg, "CHANNEL_HANDLE", "") or "@Wander60s"
        except Exception:
            size = 190
            channel_tag = channel_tag or "@Wander60s"

        # 1) 텍스트 배치를 먼저 확정해야 밴드 높이를 정할 수 있다 → 측정용 임시 캔버스 사용
        probe = ImageDraw.Draw(Image.new("RGB", (WIDTH, HEIGHT)))
        max_w = int(WIDTH * 0.90)
        lines = None
        while size >= MIN_FONT_SIZE:
            font = ImageFont.truetype(font_path, size)
            lines = _wrap(probe, text, font, max_w)
            if lines:
                break
            size -= FONT_STEP
        if not lines:
            # 최소 크기로도 안 들어가면(공백 없는 초장문 단어) 40px까지 더 줄이고,
            # 그래도 넘치면 말줄임 — x가 음수가 되어 좌우가 잘리는 것 방지 (codex 지적)
            size = MIN_FONT_SIZE
            font = ImageFont.truetype(font_path, size)
            while size > 40 and _line_w(probe, text, font) > max_w:
                size -= FONT_STEP
                font = ImageFont.truetype(font_path, size)
            if _line_w(probe, text, font) > max_w:
                t = text
                while len(t) > 1 and _line_w(probe, t + "…", font) > max_w:
                    t = t[:-1]
                text = t + "…"
            lines = [text]

        ascent, descent = font.getmetrics()
        line_h = ascent + descent + STROKE_W
        gap = int(line_h * 0.12)
        total_h = line_h * len(lines) + gap * (len(lines) - 1)
        top_y = int(HEIGHT * 0.07)  # 상단 배치 (유튜브 그리드에서 위쪽이 먼저 읽힌다)

        # 2) 배경 프레임 + 텍스트 구간 반투명 밴드
        img = _dim_and_band(_base_frame(source_path, frame_time),
                            band_top=top_y - 60, band_h=total_h + 120)
        draw = ImageDraw.Draw(img)

        y = top_y
        for line in lines:
            x = (WIDTH - _line_w(draw, line, font)) // 2
            draw.text((x, y), line, font=font, fill=accent,
                      stroke_width=STROKE_W, stroke_fill=(0, 0, 0))
            y += line_h + gap

        # 3) 하단 채널 태그 (작게)
        tag = (channel_tag or "").strip()
        if tag:
            tag_font = ImageFont.truetype(font_path, 56)
            tw = _line_w(draw, tag, tag_font)
            draw.text(((WIDTH - tw) // 2, int(HEIGHT * 0.90)), tag, font=tag_font,
                      fill=(255, 255, 255), stroke_width=5, stroke_fill=(0, 0, 0))

        # 4) 2MB 한도 내 저장 — 초과 시 quality 를 낮춰 재시도
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
