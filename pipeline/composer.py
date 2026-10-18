"""1분 여행 도시 소개 영상 합성 (Wander 60s).

- 배경: Pexels portrait 영상/사진 Ken Burns concatenate (각 하이라이트별)
- 오디오: TTS 라인 (intro + 하이라이트 4~5개 + outro) concatenate
- 그래픽:
  - 첫 2.5초: BIG HOOK(영어 대문자 펀치) + 도시·국가 라벨
  - 상단: 도시명 라벨 상시 표시 (펀치 문구를 50초 내내 반복 노출하지 않는다)
  - 중하단: 현재 라인 자막 시간차 표시
  - 하단: 채널 브랜딩(@Wander60s)
- 길이: 내레이션 길이에 맞춰 적응형(2026-07-17) — min(59.5s, max(내레이션+3.5s, MIN_VIDEO_DURATION_SEC)).
  이전엔 항상 59.7s로 강제해 짧은 내레이션(~28s) 뒤 ~25s 무자막 데드타임이 생겼다(리텐션 붕괴).
  내레이션이 방어선을 넘어 음성이 잘리는 대본은 NarrationTooLongError 로 발행을 막는다(2026-09-03).
"""
import logging
import os
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

from moviepy.editor import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    VideoFileClip,
    afx,
    concatenate_audioclips,
    concatenate_videoclips,
    vfx,
)

from config import (
    BASE_DIR,
    BGM_VOLUME_DB,
    CAPTION_BASE_SIZE,
    CAPTION_HIGHLIGHT_NUMBERS,
    CAPTION_PHRASE_SIZE,
    CAPTION_Y_RATIO,
    FONT_PATH,
    VIDEO_BITRATE,
    VIDEO_DURATION_SEC,
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_PRESET,
    VIDEO_THREADS,
    VIDEO_WIDTH,
)

# 화면에 박히는 브랜딩(워터마크/CTA)은 config에서 — 우주봇 잔재 방지. 구버전 config 폴백.
import config as _cfg
CHANNEL_HANDLE = getattr(_cfg, "CHANNEL_HANDLE", "@Wander60s")
BRAND_CTA_SUBTITLE = getattr(_cfg, "BRAND_CTA_SUBTITLE", "for a new city daily")

log = logging.getLogger("travelbot.composer")

# 쇼츠 재생 UI 안전영역: 우측 ~15%(좋아요/댓글 버튼열)·하단 ~20%(제목/채널/음악) 회피.
# 화면 텍스트(펀치/토픽/자막/CTA)는 가로 중앙 SAFE_WIDTH_RATIO 이내로 배치.
SAFE_WIDTH_RATIO = 0.72
CAPTION_MAX_W = int(VIDEO_WIDTH * SAFE_WIDTH_RATIO)

# 첫 화면 BIG HOOK 노출 구간(초). 여기저기 중복 선언돼 있던 값을 상수 하나로 통합(2026-09-03).
# 이 구간에는 하단 phrase 자막을 띄우지 않는다(글자 충돌 방지).
BIG_HOOK_END = 2.5

# 자막 외곽선 두께 — 2026-09-03 가독성 강화(3 → 5). 그림자는 _make_text_image가 항상 얹는다.
CAPTION_STROKE_W = 5

# 쇼츠 최대 길이 방어선(초). 합성/자막이 공유하는 단일 출처.
MAX_TOTAL_DURATION = 59.5

# 인코딩 결과가 방어선을 살짝 넘는 건 인코더 오차(±0.1s 수준)라 허용하고, 이 폭을 넘으면
# 타임라인 계산 자체가 깨진 것으로 보고 발행을 막는다.
RENDER_DURATION_TOLERANCE = 0.5

# 내레이션이 방어선을 넘어 '음성이 잘린 채' 발행되는 것을 허용하지 않기 위한 판정 여유(초).
# end_pad(끝 무음)만 잘리는 건 문제가 아니라 실제 발화 종료 시각으로만 판정한다.
NARRATION_CUT_TOLERANCE = 0.05


class NarrationTooLongError(RuntimeError):
    """내레이션이 길어 음성이 잘리는 대본 — 발행 금지(fail-closed).

    호출부(main)가 이 예외를 잡아 해당 대기열 대본을 queue/failed 로 빼고 다음 대본을
    시도한다. 잘린 음성으로 영상이 나가는 것보다 그 회차를 다른 대본으로 채우는 게 낫다.
    """

    def __init__(self, cut_sec: float, voice_end: float, limit: float):
        self.cut_sec = float(cut_sec)
        self.voice_end = float(voice_end)
        self.limit = float(limit)
        super().__init__(
            f"내레이션 {voice_end:.2f}s > 영상 {limit:.2f}s — {cut_sec:.2f}s 잘림(발행 금지)")

# 진단 전용 훅: {"path": <wav>} 설정 시 compose가 비디오 인코딩 없이 최종 오디오만 저장하고 반환.
# 프로덕션 경로에선 항상 None (동작 불변). 음성 끊김 분석용.
_AUDIO_PROBE_OUT = {"path": None}
# _make_text_image가 내부에서 max_width-80으로 줄바꿈하므로, 폰트 축소 판정도 같은 폭을 써야
# 단어 중간 쪼개짐/1줄 초과를 막을 수 있다.
CAPTION_WRAP_W = CAPTION_MAX_W - 80

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F6FF"
    "\U0001F700-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F02F"
    "\U0001F0A0-\U0001F0FF"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def _find_font(bold: bool = True):
    candidates = [
        FONT_PATH,
        # Linux (GitHub Actions ubuntu)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        # Windows
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ]
    for f in candidates:
        if f and os.path.exists(f):
            return f
    return None


def _resize_loop_to_duration(clip, duration):
    if clip.duration >= duration:
        return clip.subclip(0, duration)
    loops = int(duration // clip.duration) + 1
    return concatenate_videoclips([clip] * loops).subclip(0, duration)


def _fit_portrait(clip):
    w, h = clip.size
    target_ratio = VIDEO_WIDTH / VIDEO_HEIGHT
    src_ratio = w / h
    if src_ratio > target_ratio:
        new_w = int(h * target_ratio)
        x_off = (w - new_w) // 2
        clip = clip.crop(x1=x_off, x2=x_off + new_w)
    elif src_ratio < target_ratio:
        new_h = int(w / target_ratio)
        y_off = (h - new_h) // 2
        clip = clip.crop(y1=y_off, y2=y_off + new_h)
    return clip.resize((VIDEO_WIDTH, VIDEO_HEIGHT))


def _ken_burns(clip, duration, start_zoom=1.05, end_zoom=1.15):
    return clip.resize(lambda t: start_zoom + (end_zoom - start_zoom) * (t / duration))


def _vignette_image():
    w, h = VIDEO_WIDTH, VIDEO_HEIGHT
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([-w // 4, -h // 6, w + w // 4, h + h // 6], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=int(min(w, h) * 0.18)))
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste((0, 0, 0, 70), (0, 0, w, h))
    edge = Image.new("RGBA", (w, h), (0, 0, 0, 170))
    inv_mask = Image.eval(mask, lambda v: 255 - v)
    out.paste(edge, (0, 0), inv_mask)
    return out


def _wrap_text(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return [text]
    parts = text.split()
    if len(parts) <= 1:
        out, cur = [], ""
        for ch in text:
            test = cur + ch
            if draw.textlength(test, font=font) <= max_width:
                cur = test
            else:
                if cur:
                    out.append(cur)
                cur = ch
        if cur:
            out.append(cur)
        return out
    lines, cur = [], ""
    for w in parts:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _make_text_image(text: str, font_size: int, max_width: int,
                     fg=(255, 255, 255, 250), shadow=True,
                     stroke_color=(0, 0, 0, 220), stroke_width=2) -> np.ndarray:
    text = _strip_emoji(text)
    if not text:
        return np.zeros((1, 1, 4), dtype=np.uint8)

    font_path = _find_font(bold=True)
    if font_path:
        font = ImageFont.truetype(font_path, font_size)
    else:
        font = ImageFont.load_default()

    tmp_img = Image.new("RGBA", (max_width, 100), (0, 0, 0, 0))
    tmp_draw = ImageDraw.Draw(tmp_img)
    lines = _wrap_text(tmp_draw, text, font, max_width - 80)
    line_h = font_size + 10
    # 외곽선이 두꺼울수록 위아래 여백을 더 준다 — 두꺼운 stroke가 캔버스 밖으로 잘리는 것 방지.
    margin = 30 + stroke_width * 2
    total_h = line_h * len(lines) + margin * 2

    img = Image.new("RGBA", (max_width, total_h), (0, 0, 0, 0))

    if shadow:
        # 드롭 섀도 — 밝은 배경에서도 글자가 배경에서 떠 보이게 (외곽선과 별개 레이어)
        shadow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow_layer)
        y = margin
        for line in lines:
            w = tmp_draw.textlength(line, font=font)
            x = (max_width - w) // 2
            sd.text((x + 5, y + 6), line, font=font, fill=(0, 0, 0, 200),
                    stroke_width=stroke_width, stroke_fill=(0, 0, 0, 200))
            y += line_h
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=6))
        img = Image.alpha_composite(img, shadow_layer)

    text_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    td = ImageDraw.Draw(text_layer)
    y = margin
    for line in lines:
        w = tmp_draw.textlength(line, font=font)
        x = (max_width - w) // 2
        # Pillow 네이티브 stroke — 예전 dx/dy 중첩 루프는 stroke_width가 커질수록
        # 드로우 횟수가 제곱으로 늘어(8px면 288회/줄) 렌더가 느려졌다.
        td.text((x, y), line, font=font, fill=fg,
                stroke_width=max(0, stroke_width), stroke_fill=stroke_color)
        y += line_h

    img = Image.alpha_composite(img, text_layer)
    return np.array(img)


def _fit_caption_image(text: str, base_size: int, max_width: int, max_lines: int = 3) -> np.ndarray:
    """자막 폰트 자동 축소: max_lines 안에 들어갈 때까지. 최소 사이즈 64로 가독성 보장."""
    size = base_size
    while size >= 64:
        font_path = _find_font()
        if font_path:
            font = ImageFont.truetype(font_path, size)
        else:
            font = ImageFont.load_default()
        tmp = Image.new("RGBA", (max_width, 100), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        lines = _wrap_text(td, text, font, max_width - 60)
        if len(lines) <= max_lines:
            break
        size -= 4
    return _make_text_image(text, size, max_width, fg=(255, 255, 255, 255),
                            stroke_width=CAPTION_STROKE_W)


def _make_static_clip(arr: np.ndarray, duration: float, position, fade_in=0.3, fade_out=0.3):
    if arr.shape[0] <= 1 or arr.shape[1] <= 1:
        return None
    clip = ImageClip(arr, transparent=True).set_duration(duration).set_position(position)
    if fade_in > 0:
        clip = clip.crossfadein(fade_in)
    if fade_out > 0:
        clip = clip.crossfadeout(fade_out)
    return clip


_NUMBER_RE = re.compile(r"\d")


def _phrase_color(text: str):
    """숫자 포함 phrase는 옐로우, 아니면 흰색."""
    if CAPTION_HIGHLIGHT_NUMBERS and _NUMBER_RE.search(text):
        return (255, 220, 0, 255)  # 옐로우
    return (255, 255, 255, 255)


def _add_caption_box(arr: np.ndarray, pad: int = 26, radius: int = 26, box_alpha: int = 175) -> np.ndarray:
    """텍스트 RGBA 뒤에 반투명 다크 라운드 박스 합성 → 밝은/복잡한 배경에서도 가독성 확보."""
    if arr is None or arr.shape[0] <= 1 or arr.shape[1] <= 1:
        return arr
    img = Image.fromarray(arr, "RGBA")
    bbox = img.split()[3].getbbox()
    if not bbox:
        return arr
    x0, y0, x1, y1 = bbox
    x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
    x1 = min(img.width, x1 + pad); y1 = min(img.height, y1 + pad)
    box = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(box)
    try:
        bd.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=(0, 0, 0, box_alpha))
    except Exception:
        bd.rectangle([x0, y0, x1, y1], fill=(0, 0, 0, box_alpha))
    return np.array(Image.alpha_composite(box, img))


def _make_phrase_clip(phrase_text: str, start: float, end: float, base_size=None):
    """짧은 phrase (3-4단어) 자막. 짧은 시간이라 fade도 빠르게."""
    base_size = base_size or CAPTION_BASE_SIZE
    if end <= start:
        return None
    fg = _phrase_color(phrase_text)
    # 짧은 phrase라 max_lines=2면 충분. 실제 wrap 폭(CAPTION_WRAP_W) 기준으로 자동 축소 →
    # 단어 중간 쪼개짐/1줄 초과 방지.
    size = base_size
    while size >= 44:
        font_path = _find_font()
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        tmp = Image.new("RGBA", (CAPTION_MAX_W, 100), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        lines = _wrap_text(td, phrase_text, font, CAPTION_WRAP_W)
        if len(lines) <= 2 and all(td.textlength(ln, font=font) <= CAPTION_WRAP_W for ln in lines):
            break
        size -= 4
    arr = _make_text_image(phrase_text, size, CAPTION_MAX_W, fg=fg,
                           stroke_width=CAPTION_STROKE_W)
    arr = _add_caption_box(arr)  # 가독성: 반투명 다크 박스 배경
    if arr.shape[0] <= 1:
        return None
    clip = ImageClip(arr, transparent=True).set_duration(end - start).set_start(start)
    y = int(VIDEO_HEIGHT * CAPTION_Y_RATIO)
    clip = clip.set_position(("center", y))
    clip = clip.crossfadein(0.08).crossfadeout(0.08)
    return clip


def _split_into_phrases(words: list, line_start: float, line_end: float,
                       phrase_size: int = None) -> list:
    """word_cues를 3-4단어 phrase로 묶기. 절대 시간 기준 (line_start 더해서)."""
    phrase_size = phrase_size or CAPTION_PHRASE_SIZE
    if not words:
        return []
    phrases = []
    cursor = 0
    while cursor < len(words):
        chunk = words[cursor:cursor + phrase_size]
        if not chunk:
            break
        text = " ".join(w["text"] for w in chunk)
        start = line_start + chunk[0]["start"]
        end_w = line_start + chunk[-1]["end"]
        phrases.append({"text": text, "start": start, "end": end_w})
        cursor += phrase_size

    # 끊김 없도록 끝 시간을 다음 phrase 시작으로 (마지막은 line_end로)
    for i in range(len(phrases) - 1):
        phrases[i]["end"] = phrases[i + 1]["start"]
    if phrases:
        phrases[-1]["end"] = line_end
    return phrases


def _make_caption_window(text: str, start: float, end: float, base_size=80):
    """words 없을 때 fallback: 라인 통째로 자막."""
    return [_make_phrase_clip(text, start, end, base_size)]


def _make_hook_clip(text: str, duration: float):
    """상단 도시 라벨 — BIG HOOK 이후 영상 내내 표시 (상단 y=0.10, 안전영역 폭).

    2026-09-03부터 여기에 들어가는 건 펀치 훅이 아니라 도시명이다.
    펀치 문구를 50초 내내 띄우면 같은 문장이 계속 반복돼 화면이 지저분해진다."""
    arr = _make_text_image(text, font_size=72, max_width=CAPTION_MAX_W)
    if arr.shape[0] <= 1:
        return None
    clip = ImageClip(arr, transparent=True).set_duration(duration)
    y = int(VIDEO_HEIGHT * 0.10)
    clip = clip.set_position(("center", y))
    clip = clip.crossfadein(0.5)
    return clip


def _extract_punch(title: str = None, facts=None):
    """펀치 텍스트("9 BRAINS!") 추출: 제목 숫자+명사 → 실패 시 facts → 둘 다 없으면 None(스킵).

    빈/이상한 펀치를 화면에 내보내지 않기 위해 숫자 근거가 없으면 None을 반환한다.
    """
    import re as _re
    pat = r"(\d[\d,\.]*)\s+([A-Za-z][A-Za-z\-]*)"
    for src in ([title] if title else []) + list(facts or []):
        if not src:
            continue
        m = _re.search(pat, _strip_emoji(src))
        if m:
            num = m.group(1).rstrip(".")
            word = m.group(2).upper()
            if word.lower() in {"the", "and", "of", "a", "an", "to", "in", "for"}:
                continue
            return f"{num} {word}!"
    return None


_LEGACY_HOOK_RE = re.compile(r"\bin\s*60\s*s(ec(onds)?)?[\s.!]*$", re.IGNORECASE)
# 새 대기열 스키마의 hook은 2~6단어 영어 대문자 펀치. 그보다 길면 문장형(구 GPT 갈래) 훅이다.
BIG_HOOK_MAX_WORDS = 6


def is_legacy_hook_label(hook: str) -> bool:
    """구 스키마 훅(라벨 "Lisbon in 60s" / 문장형 GPT 훅)인지 판정.

    새 대기열 스키마의 hook은 영어 대문자 펀치 문구(2~6단어)다. 구 갈래가 만드는 라벨이나
    12단어짜리 문장이 들어오면 BIG HOOK에 통째로 띄웠을 때 화면이 글자로 뒤덮이므로,
    도시명만 크게 띄우는 기존(2026-07-18) 레이아웃으로 강등한다(경고 로그 1줄).
    """
    h = _strip_emoji(hook or "").strip()
    if not h:
        return True
    if _LEGACY_HOOK_RE.search(h):
        return True
    return len(h.split()) > BIG_HOOK_MAX_WORDS


def _fit_font_size(text: str, start: int, floor: int, step: int, max_lines: int = 1):
    """max_lines 안에 안전폭(CAPTION_WRAP_W)으로 들어가는 최대 폰트 크기 탐색."""
    font_path = _find_font(bold=True)
    size = start
    while size >= floor:
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        tmp = Image.new("RGBA", (CAPTION_MAX_W, 100), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        lines = _wrap_text(td, text, font, CAPTION_WRAP_W)
        if len(lines) <= max_lines and all(
                td.textlength(ln, font=font) <= CAPTION_WRAP_W for ln in lines):
            return size
        size -= step
    return floor


def _make_big_hook_intro(hook_text: str, city_label: str = "", country: str = None,
                         duration: float = BIG_HOOK_END):
    """첫 0~duration초 화면 가득 BIG HOOK — 스크롤 멈춤 유도 (2026-09-03 개편).

    구성(대기열 펀치 훅):
    - 펀치 문구(hook, 대문자·최대 2줄·흰 글자+검은 외곽선) 0~duration, y=0.30, 줌인 pop.
    - 그 아래 "CITY, COUNTRY" 골드 라벨 y=0.56, 0.35~duration
      (어느 도시·나라인지 첫 화면에 보여야 한다 — 쿠마님 2026-07-18).
    구 스키마 훅(라벨/문장형)이면 도시명을 크게 + 국가를 골드로 띄우는 기존 레이아웃 유지.
    하단 phrase 자막은 호출 측에서 BIG_HOOK_END 이후에 시작 → 글자 겹침 없음.
    """
    layers = []
    hook_clean = _strip_emoji(hook_text or "").strip()
    city_clean = _strip_emoji(city_label or "").strip()
    country_clean = _strip_emoji((country or "").strip())

    legacy = is_legacy_hook_label(hook_clean)
    if legacy:
        if hook_clean:
            log.warning(f"[hook] 펀치 문구가 아님(라벨/문장형) — 도시명 표기로 강등: {hook_clean[:60]!r}")
        # 구 레이아웃: 중앙 대형 = 도시명, 그 아래 국가(골드)
        big_text = (city_clean or _LEGACY_HOOK_RE.sub("", hook_clean).strip()).upper()
        sub_text = country_clean.upper()
    else:
        big_text = hook_clean.upper()
        sub_text = ", ".join([t for t in (city_clean, country_clean) if t]).upper()

    if not big_text:
        return layers

    # 펀치 — 최대 2줄. 2줄이면 글자를 조금 줄여 화면을 덜 잡아먹게 한다.
    size = _fit_font_size(big_text, start=170, floor=64, step=8, max_lines=2)
    big_arr = _make_text_image(
        big_text, font_size=size, max_width=CAPTION_MAX_W,
        fg=(255, 255, 255, 255), stroke_color=(0, 0, 0, 255), stroke_width=8,
    )
    if big_arr.shape[0] > 1:
        big_clip = ImageClip(big_arr, transparent=True).set_duration(duration).set_start(0)
        big_clip = big_clip.set_position(("center", int(VIDEO_HEIGHT * 0.30)))
        big_clip = big_clip.resize(lambda t: 1.25 - 0.25 * min(1.0, t / 0.25))
        big_clip = big_clip.crossfadein(0.08).crossfadeout(0.35)
        layers.append(big_clip)

    # 도시·국가 라벨 — 펀치 아래 골드로 작게
    if sub_text:
        sub_size = _fit_font_size(sub_text, start=80, floor=40, step=4, max_lines=1)
        sub_arr = _make_text_image(
            sub_text, font_size=sub_size, max_width=CAPTION_MAX_W,
            fg=(255, 210, 90, 255), stroke_color=(0, 0, 0, 255), stroke_width=6,
        )
        if sub_arr.shape[0] > 1:
            sub_clip = ImageClip(sub_arr, transparent=True)
            sub_clip = sub_clip.set_duration(max(0.1, duration - 0.35)).set_start(0.35)
            sub_clip = sub_clip.set_position(("center", int(VIDEO_HEIGHT * 0.56)))
            sub_clip = sub_clip.crossfadein(0.15).crossfadeout(0.3)
            layers.append(sub_clip)

    return layers


def _make_brand_clip(duration: float, text=CHANNEL_HANDLE):
    # 워터마크는 상단 topic 라벨(y0.10)과 punch 영역(y0.30) 사이 빈 공간(y0.24)에 둔다 —
    # 중앙 자막(y0.60)/CTA(y0.42~0.68)와 절대 안 겹치고, topic 라벨과도 충분히 벌린다.
    # 레이어 순서상 자막보다 먼저 그려 자막이 항상 위로 오게(compose에서 처리).
    arr = _make_text_image(text, font_size=38, max_width=CAPTION_MAX_W,
                           fg=(255, 255, 255, 185))
    if arr.shape[0] <= 1:
        return None
    clip = ImageClip(arr, transparent=True).set_duration(duration)
    y = int(VIDEO_HEIGHT * 0.24)
    clip = clip.set_position(("center", y))
    return clip


def _make_jolt_flash(duration: float = 0.5):
    """영상 시작 시각 jolt — 짧은 화이트 플래시로 스크롤 멈춤 유도."""
    arr = np.full((VIDEO_HEIGHT, VIDEO_WIDTH, 4), [255, 255, 255, 255], dtype=np.uint8)
    clip = ImageClip(arr, transparent=True).set_duration(duration).set_start(0)
    clip = clip.crossfadeout(duration)  # alpha 100→0 fade
    return clip


def _make_cta_card_clip(start: float, duration: float):
    """마지막 4-5초 CTA — 큰 'FOLLOW + LIKE' 카드 (옐로우, 두꺼운 외곽선)."""
    if duration <= 0:
        return []
    layers = []
    # 큰 메인 텍스트 (안전영역 폭 이내, 1줄 유지 위해 120px)
    big_arr = _make_text_image(
        "FOLLOW + LIKE",
        font_size=120,
        max_width=CAPTION_MAX_W,
        fg=(255, 220, 0, 255),
        stroke_color=(0, 0, 0, 255),
        stroke_width=6,
    )
    big_clip = ImageClip(big_arr, transparent=True).set_duration(duration).set_start(start)
    big_clip = big_clip.set_position(("center", int(VIDEO_HEIGHT * 0.42)))
    big_clip = big_clip.crossfadein(0.3)
    layers.append(big_clip)

    # 보조 텍스트 (브랜드 CTA — config 기반, 우주봇 잔재 방지)
    small_arr = _make_text_image(
        BRAND_CTA_SUBTITLE,
        font_size=60,
        max_width=CAPTION_MAX_W,
        fg=(255, 255, 255, 240),
        stroke_color=(0, 0, 0, 220),
        stroke_width=3,
    )
    small_clip = ImageClip(small_arr, transparent=True).set_duration(duration).set_start(start)
    small_clip = small_clip.set_position(("center", int(VIDEO_HEIGHT * 0.62)))
    small_clip = small_clip.crossfadein(0.4)
    layers.append(small_clip)
    return layers


def _make_vignette_clip(duration: float):
    vig = _vignette_image()
    arr = np.array(vig)
    return ImageClip(arr, transparent=True).set_duration(duration).set_position((0, 0))


def _pick_bgm():
    """assets/bgm/*.mp3 풀에서 랜덤 1개. 없으면 None."""
    import random
    bgm_dir = os.path.join(BASE_DIR, "assets", "bgm")
    if not os.path.isdir(bgm_dir):
        return None
    files = [
        os.path.join(bgm_dir, f) for f in os.listdir(bgm_dir)
        if f.lower().endswith((".mp3", ".m4a", ".wav", ".ogg"))
    ]
    if not files:
        return None
    return random.choice(files)


def normalize_bg_groups(bg) -> list:
    """배경 입력을 [[path, ...], ...] (하이라이트 슬롯별 클립 묶음)로 정규화.

    받는 형태 2가지 — 둘 다 지원해야 기존 호출부(tests/render_qa.py 등)가 안 깨진다.
      1) 신규 계약: backgrounds_by_fact = [[(path, clip_id), ...], ...]  (슬롯당 1~N개)
      2) 구 계약  : bg_paths = [path, ...]                               (슬롯당 1개)
    """
    groups = []
    for item in (bg or []):
        if isinstance(item, str):
            groups.append([item])
            continue
        if isinstance(item, (list, tuple)):
            grp = []
            for c in item:
                if isinstance(c, str):
                    grp.append(c)
                elif isinstance(c, (list, tuple)) and c and isinstance(c[0], str):
                    grp.append(c[0])  # (path, clip_id)
            if grp:
                groups.append(grp)
            continue
        raise ValueError(f"compose: 알 수 없는 배경 항목 타입 {type(item)!r}")
    return groups


def _build_background(bg_paths: list, segment_durations: list, total_dur: float):
    """배경 영상들을 segment_durations 길이로 잘라 concatenate (세그먼트 사이는 하드컷).

    bg_paths가 부족하면 처음 영상 재사용. 각 세그먼트는 fit_portrait + Ken Burns.
    세그먼트 간 크로스페이드는 쓰지 않는다 — Actions 러너 메모리 보호(2026-09-03 확정).
    """
    n_seg = len(segment_durations)
    paths = list(bg_paths)
    while len(paths) < n_seg:
        paths.append(paths[0])

    clips = []
    for i, dur in enumerate(segment_durations):
        bg = VideoFileClip(paths[i]).without_audio()
        bg = _fit_portrait(bg)
        bg = _resize_loop_to_duration(bg, dur)
        bg = _ken_burns(bg, dur, start_zoom=1.04, end_zoom=1.12)
        bg = bg.resize(width=VIDEO_WIDTH).set_position(("center", "center"))
        clips.append(bg)
    final_bg = concatenate_videoclips(clips, method="compose")
    final_bg = final_bg.subclip(0, total_dur)
    final_bg = final_bg.crossfadein(0.4).crossfadeout(0.6)
    return final_bg


def final_video_duration() -> float:
    """compose가 강제하는 최종 영상 길이(초). 자막(captions) 등 외부 모듈이 참조하는 단일 출처.
    compose 내부 target_dur과 동일해야 자막이 잘린 영상 뒤로 삐져나오지 않는다.

    2026-09-03: 59.7 → 59.5 (MAX_TOTAL_DURATION). 쇼츠 60초 경계에 여유를 더 둔다."""
    return min(float(VIDEO_DURATION_SEC) - 0.5, MAX_TOTAL_DURATION)


def _mix_bgm(bgm_file: str, voice_audio, total_dur: float):
    """BGM 파일 로드 → 길이 맞춤(loop/subclip) → 볼륨/페이드 → voice와 믹스. 실패 시 예외 전파."""
    bgm = AudioFileClip(bgm_file)
    if bgm.duration < total_dur:
        from moviepy.audio.fx.all import audio_loop
        bgm = audio_loop(bgm, duration=total_dur)
    else:
        bgm = bgm.subclip(0, total_dur)
    bgm = bgm.volumex(10 ** (BGM_VOLUME_DB / 20.0))
    bgm = bgm.fx(afx.audio_fadein, 0.5).fx(afx.audio_fadeout, 1.0)
    return CompositeAudioClip([bgm, voice_audio])


def compose(
    bg_paths: list,
    tts_lines: list,
    hook: str,
    output_path: str,
    pad_between: float = 0.18,
    end_pad: float = 0.6,
    bgm_path: str = None,
    title: str = None,
    qa_frame_times: list = None,
    country: str = None,
    intro_clip: str = None,
    city: str = None,
):
    """tts_lines: [{"text", "path", "duration"}] (intro + 하이라이트 x4~5 + outro 순서)

    bg_paths: 배경 입력. 신규 계약은 하이라이트 슬롯별 묶음 [[(path, clip_id), ...], ...]
      (config.CLIPS_PER_FACT개까지, 수집 부족 시 1개). 구 계약인 평면 [path, ...]도 그대로 받는다.
    hook: 첫 화면 BIG HOOK에 크게 띄울 영어 펀치 문구(대기열 대본). 라벨/문장형이면 도시명으로 강등.
    city/country: BIG HOOK 아래 골드 라벨 + 상단 상시 라벨에 쓰는 도시·국가명.
    intro_clip: 도입샷(도시 전경) — 있으면 intro 구간 배경으로 앞에 붙인다.

    리턴: output_path (일반). qa_frame_times 주면 전체 인코딩 없이 그 시점 프레임 PNG만
    저장하고 [(t, png), ...]를 반환(렌더 QA 전용, 프로덕션 동작 불변).
    """
    import time as _time
    _t_compose0 = _time.monotonic()

    bg_groups = normalize_bg_groups(bg_paths)
    if not bg_groups:
        raise ValueError("compose: bg_paths가 비었음 — 검증 통과 배경 0개(발행 불가)")

    # 1) 오디오 timeline 계산
    timeline = []
    cursor = 0.0
    audio_clips = []
    for i, line in enumerate(tts_lines):
        ac = AudioFileClip(line["path"])
        start = cursor
        ac = ac.set_start(start)
        audio_clips.append(ac)
        end = start + line["duration"]
        timeline.append({
            "text": line["text"],
            "start": start,
            "end": end,
            "words": line.get("words", []),
        })
        cursor = end + pad_between

    raw_total = cursor - pad_between + end_pad
    # 2026-07-17 v4: 적응형 길이. v3의 '항상 59.7s 강제'는 여행봇의 짧은 내레이션(~28s)에서
    # 마지막 클립 ~39s 루프 + ~25s 무자막 데드타임을 만들었다(리텐션 붕괴, 검증 확정 finding).
    # tail 0.9s(2026-10-18, 이전 3.5s) — 발화 뒤 여운 = end_pad 0.6 + 0.9 = 1.5s. 끝의 빈 시간을 줄여 평균 시청 %·다시 보기를 올린다.
    # CTA 는 아래에서 outro 발화 시작과 동시에 뜬다(cta_start = last_tts_start, v5 동기 유지).
    cap_dur = final_video_duration()
    min_dur = float(getattr(_cfg, "MIN_VIDEO_DURATION_SEC", 40))
    tail = 0.9  # 음성이 끝난 뒤 BGM/CTA가 남는 아웃트로 길이(end_pad 0.6 별도)
    target = raw_total + tail
    if target > cap_dur:
        # 60초 방어: 넘치면 아웃트로(BGM 꼬리)를 줄여서 cap 안으로 넣는다.
        log.warning(
            f"길이 방어: 음성 {raw_total:.2f}s + 아웃트로 {tail:.1f}s = {target:.2f}s > {cap_dur:.1f}s "
            f"→ 아웃트로 {max(0.0, cap_dur - raw_total):.2f}s로 축소")
        target = cap_dur
    total_dur = min(cap_dur, max(target, min_dur))
    # 내레이션 자체가 잘리는지(= 마지막 라인 종료가 방어선을 넘는지)로만 판정한다.
    # raw_total 에는 end_pad(무음)가 포함돼 있어 raw_total 초과 ≠ 음성 잘림이다.
    voice_end = timeline[-1]["end"] if timeline else 0.0
    if voice_end > total_dur + NARRATION_CUT_TOLERANCE:
        # fail-closed: 잘린 음성으로는 발행하지 않는다. 호출부가 이 대본을 failed 로 빼고
        # 다음 대기열 대본을 시도한다. 인코딩 전에 끊어 러너 시간도 아낀다.
        cut = voice_end - total_dur
        log.error(f"음성 잘림 판정: 내레이션 종료 {voice_end:.2f}s > 영상 {total_dur:.2f}s "
                  f"({cut:.2f}s 잘림) — 발행 금지, 대본 교체 필요")
        for ac in audio_clips:
            try:
                ac.close()
            except Exception:
                pass
        raise NarrationTooLongError(cut, voice_end, total_dur)
    if raw_total > cap_dur:
        log.warning(f"음성+패딩 {raw_total:.2f}s > 영상 {cap_dur:.2f}s — 끝 무음만 잘림")
    log.info(f"timeline: raw={raw_total:.2f}s → total={total_dur:.2f}s (적응형, cap={cap_dur:.1f}s min={min_dur:.0f}s) lines={len(timeline)}")

    # 라인이 잘리는 경우, 마지막 라인 끝을 total_dur로 클램프
    for w in timeline:
        if w["end"] > total_dur:
            w["end"] = total_dur

    # 2) 오디오 합성 — TTS 트랙 + (선택) BGM
    voice_audio = CompositeAudioClip(audio_clips).set_duration(total_dur)
    voice_audio = voice_audio.fx(afx.audio_fadein, 0.3).fx(afx.audio_fadeout, 0.5)

    # 명시 인자 우선, 없으면 로컬 풀에서 픽
    chosen_bgm = bgm_path or _pick_bgm()
    full_audio = voice_audio
    if chosen_bgm:
        try:
            full_audio = _mix_bgm(chosen_bgm, voice_audio, total_dur)
            log.info(f"bgm mix: {os.path.basename(chosen_bgm)} ({BGM_VOLUME_DB}dB)")
        except Exception as e:
            # 1차(GDrive/로컬 픽) 로드 실패 → voice-only 직행 금지, 2차 폴백(리포 내장 ambient) 시도
            log.warning(f"bgm 적용 실패 → 2차 폴백(ambient_fallback) 시도: {e}")
            fb = os.path.join(BASE_DIR, "assets", "bgm", "ambient_fallback.mp3")
            if os.path.exists(fb):
                try:
                    full_audio = _mix_bgm(fb, voice_audio, total_dur)
                    log.info(f"bgm fallback mix: ambient_fallback.mp3 ({BGM_VOLUME_DB}dB)")
                except Exception as e2:
                    log.warning(f"2차 BGM 폴백도 실패 → voice only: {e2}")
                    full_audio = voice_audio
            else:
                log.warning("2차 BGM 폴백 파일 없음(assets/bgm/ambient_fallback.mp3) → voice only")
                full_audio = voice_audio

    # 오디오 프로브(진단 전용, guarded): 비디오 인코딩 없이 최종 오디오만 wav로 저장하고 반환.
    # 실제 발행 audio(full_audio)와 동일 — 침묵/끊김 분석용. 프로덕션 동작 불변.
    if qa_frame_times is None and _AUDIO_PROBE_OUT.get("path"):
        ap = _AUDIO_PROBE_OUT["path"]
        try:
            full_audio.set_duration(total_dur).write_audiofile(ap, fps=44100, verbose=False, logger=None)
        finally:
            for ac in audio_clips:
                ac.close()
        return ap

    # 3) 배경 — fact TTS 시작 시점 기준으로 클립 전환 (클립 수 = fact 수 지향).
    # timeline = [intro, fact1..factk, outro]. fact 라인 = timeline[1:-1].
    # 배경 클립 수가 fact 수와 같으면: seg0=[0, fact2.start)(intro 포함) ... 마지막 seg는 outro 포함.
    # 아니면(부족 등) 기존 균등 분할로 폴백.
    # 하이라이트 구간 안에서 클립이 2개 이상이면 그 구간을 클립 수만큼 균등 분할해 하드컷으로 잇는다.
    fact_lines = timeline[1:-1] if len(timeline) >= 3 else timeline
    k = len(fact_lines)

    def _cut_span(grp, span, bucket_path, bucket_dur):
        """한 하이라이트 구간(span)을 그 슬롯의 클립 수만큼 하드컷으로 나눠 담는다.
        클립 하나가 0.9초 미만이 될 만큼 잘게 쪼개지 않는다(깜빡임 방지)."""
        grp = list(grp or [])
        m = max(1, min(len(grp), int(span // 0.9) or 1)) if grp else 1
        if grp and m < len(grp):
            # 2026-10-18: 컷을 줄여야 하면 대본 그림(img:)을 먼저 남기고 남은 자리를 원래 순서의 영상으로 채운다
            def _is_img(g):
                return isinstance(g, (list, tuple)) and len(g) > 1 and str(g[1]).startswith("img:")
            keep = [i for i, g in enumerate(grp) if _is_img(g)][:m]
            rest = [i for i in range(len(grp)) if i not in keep][:m - len(keep)]
            grp = [grp[i] for i in sorted(keep + rest)]
        each = span / m
        for c in range(m):
            bucket_path.append(grp[c])
            bucket_dur.append(each)

    use_intro = bool(intro_clip) and os.path.exists(intro_clip or "") and k >= 1 and len(bg_groups) >= 1
    if use_intro:
        # 도입샷: intro 구간(0 ~ 첫 하이라이트 시작)을 '도시 전경'으로 깐다(첫 화면 임팩트).
        # 이후 세그먼트는 하이라이트별 클립. BIG HOOK+intro 내레이션이 이 도입샷 위에 얹힌다.
        intro_dur = max(0.5, float(fact_lines[0]["start"]))
        starts = [float(fact_lines[0]["start"])]
        for j in range(1, k):
            s = min(max(float(fact_lines[j]["start"]), starts[-1] + 0.5), total_dur)
            starts.append(s)
        # 배경 그룹이 하이라이트 수(k)보다 적으면(min_pass=3 부분통과) 마지막 그룹을 반복해 k개 채운다 —
        # 도입샷(첫 화면 도시 전경)은 항상 유지(쿠마님 반려사유 2~5 회귀 방지)
        _grps = list(bg_groups[:k])
        while _grps and len(_grps) < k:
            _grps.append(_grps[-1])
        bg_for_seg, seg_durs = [intro_clip], [intro_dur]
        for j in range(k):
            s = starts[j]
            e = starts[j + 1] if j + 1 < k else total_dur
            _cut_span(_grps[j], max(0.5, e - s), bg_for_seg, seg_durs)
        log.info(f"배경 세그먼트(도입샷+하이라이트 시작 기준, 컷 {len(seg_durs)}개) "
                 f"durs={[round(x, 1) for x in seg_durs]}")
    elif k >= 1 and len(bg_groups) >= k:
        starts = [0.0]
        for j in range(1, k):
            s = min(max(float(fact_lines[j]["start"]), starts[-1] + 0.5), total_dur)
            starts.append(s)
        bg_for_seg, seg_durs = [], []
        for j in range(k):
            s = starts[j]
            e = starts[j + 1] if j + 1 < k else total_dur
            _cut_span(bg_groups[j], max(0.5, e - s), bg_for_seg, seg_durs)
        log.info(f"배경 세그먼트(하이라이트 시작 기준, 컷 {len(seg_durs)}개) "
                 f"durs={[round(x, 1) for x in seg_durs]}")
    else:
        flat = [p for grp in bg_groups for p in grp]
        n_bg = max(1, min(len(flat), 4))
        seg_durs = [total_dur / n_bg] * n_bg
        bg_for_seg = flat[:n_bg]
        log.info(f"배경 세그먼트(균등 폴백) n={n_bg}")
    # 부동소수 누적 오차로 배경이 오디오보다 짧아지지 않게 마지막 세그먼트가 잔여를 흡수
    drift = total_dur - sum(seg_durs)
    if abs(drift) > 1e-6:
        seg_durs[-1] = max(0.5, seg_durs[-1] + drift)
    bg_clip = _build_background(bg_for_seg, seg_durs, total_dur)

    # 4) 비네트 + hook + brand + 자막
    layers = [bg_clip, _make_vignette_clip(total_dur)]

    # 시각 jolt (첫 0.5초 화이트 플래시 — 스크롤 멈춤 유도)
    layers.append(_make_jolt_flash(0.5))

    # 첫 BIG_HOOK_END초 BIG HOOK — 중앙에 영어 대문자 펀치 + 그 아래 "도시, 국가"(골드).
    # 상단 상시 라벨과 워터마크는 인트로 종료 후 노출 → '도시명 이중 표기'(쿠마님 2026-07-18
    # BLOCKER: 상단 hook과 중앙 big hook이 같은 "City in 60s"로 겹침) + 텍스트 클러터 제거.
    # 훅이 라벨/문장형이면 _make_big_hook_intro 가 도시명 표기로 자동 강등한다.
    city_label = _strip_emoji(city or "").strip()
    layers.extend(_make_big_hook_intro(hook, city_label=city_label, country=country,
                                       duration=BIG_HOOK_END))

    # 상단 상시 라벨 — 인트로(BIG HOOK) 종료 후부터 도시명 표시(펀치 문구 반복 노출 방지).
    # city 가 없으면(fork 호환) 구 훅에서 라벨을 유추한다.
    top_label = city_label or (hook.split("•")[0].strip() if "•" in hook else hook)
    hook_clip = _make_hook_clip(top_label, max(0.1, total_dur - BIG_HOOK_END))
    if hook_clip is not None:
        hook_clip = hook_clip.set_start(BIG_HOOK_END)
        layers.append(hook_clip)

    # 브랜드 워터마크 — 인트로 종료 후 표시(인트로 중앙 텍스트와 겹침 방지). 자막/CTA보다
    # **먼저** 그려서 자막이 항상 위로 오게 (겹침 시 자막 우선)
    brand = _make_brand_clip(max(0.1, total_dur - BIG_HOOK_END))
    if brand is not None:
        brand = brand.set_start(BIG_HOOK_END)
        layers.append(brand)

    # CTA 카드 시각 — v5: 음성 outro 시작과 동시에 CTA 글자 표시 (사용자 신고: 자막 음성 싱크 맞춰).
    # outro 음성은 timeline 마지막 라인. cta_start = outro_voice_start (자막=음성 동시 시작).
    last_tts_start = timeline[-1]["start"] if timeline else (total_dur - 4.0)
    last_tts_end = timeline[-1]["end"] if timeline else total_dur
    cta_start = last_tts_start  # outro 발화 시작 = CTA 시작(2026-10-18: 고정 7초 창 제거)
    cta_dur = max(0.0, total_dur - cta_start)
    if cta_dur < 1.0:
        cta_dur = 0.0
        cap_cutoff = total_dur
    else:
        cap_cutoff = max(0.0, cta_start - 0.2)
    log.info(f"cta v3: start={cta_start:.2f}s dur={cta_dur:.2f}s (outro_end={last_tts_end:.2f}s, total={total_dur:.2f}s)")

    # phrase 단위 자막 (TikTok karaoke 스타일)
    # 마지막 라인(outro)은 CTA 카드와 겹치니 자막에서 제외.
    # 첫 BIG_HOOK_END초는 BIG HOOK이 화면 가운데 점령 — phrase 자막 같이 띄우면 글자 겹침
    # (2026-05-07 사용자 신고). BIG HOOK 끝난 뒤부터 자막 시작. 상수는 모듈 상단 단일 선언.
    for i, w in enumerate(timeline):
        if i == len(timeline) - 1:
            continue
        # 자막 시작이 이미 cap_cutoff를 넘었으면 자막 자체 건너뜀
        if w["start"] >= cap_cutoff:
            continue
        line_start = w["start"]
        line_end = min(w["end"], cap_cutoff)
        words = w.get("words") or []
        if words:
            phrases = _split_into_phrases(words, line_start, line_end)
            for ph in phrases:
                ph_end = min(ph["end"], cap_cutoff)
                if ph_end <= ph["start"]:
                    continue
                # BIG HOOK 구간과 겹치는 phrase는 표시 안 함 — 글자 충돌 방지
                if ph["start"] < BIG_HOOK_END:
                    if ph_end <= BIG_HOOK_END:
                        continue
                    ph_start_clipped = BIG_HOOK_END
                    if ph_end <= ph_start_clipped:
                        continue
                    clip = _make_phrase_clip(ph["text"], ph_start_clipped, ph_end)
                else:
                    clip = _make_phrase_clip(ph["text"], ph["start"], ph_end)
                if clip is not None:
                    layers.append(clip)
        else:
            line_start_clipped = max(line_start, BIG_HOOK_END if line_start < BIG_HOOK_END else line_start)
            if line_end <= line_start_clipped:
                continue
            cap = _make_caption_window(w["text"], line_start_clipped, line_end, base_size=CAPTION_BASE_SIZE)
            if cap:
                layers.extend([c for c in cap if c is not None])

    layers.extend(_make_cta_card_clip(cta_start, cta_dur))

    final = CompositeVideoClip(layers, size=(VIDEO_WIDTH, VIDEO_HEIGHT)).set_duration(total_dur)
    final = final.set_audio(full_audio)

    # 렌더 QA: 전체 인코딩 없이 지정 시점 프레임만 저장 (실제 레이어 스택 그대로).
    # 라벨 밀림 방지: 요청 시점마다 항상 1개 반환 (실패 시 png=None).
    if qa_frame_times is not None:
        saved = []
        for t in qa_frame_times:
            tt = max(0.0, min(float(t), total_dur - 0.05))
            fp = f"{output_path}.qa_{tt:.2f}.png"
            try:
                final.save_frame(fp, t=tt)
                saved.append((tt, fp if os.path.exists(fp) else None))
            except Exception as e:
                log.warning(f"QA save_frame 실패 t={tt}: {e}")
                saved.append((tt, None))
        final.close()
        bg_clip.close()
        for ac in audio_clips:
            ac.close()
        return saved

    # temp_audiofile 명시 — 미지정 시 MoviePy가 CWD(리포 루트)에 temp를 만들고
    # 렌더 크래시 때 잔존한다(2026-07-17 fix: work_dir 안에 생성).
    _tmp_snd = os.path.join(
        os.path.dirname(os.path.abspath(output_path)),
        os.path.splitext(os.path.basename(output_path))[0] + "_TEMP_snd.mp4")
    log.info(f"[timing] compose={_time.monotonic() - _t_compose0:.1f}")
    _t_enc0 = _time.monotonic()
    final.write_videofile(
        output_path,
        fps=VIDEO_FPS,
        codec="libx264",
        audio_codec="aac",
        bitrate=VIDEO_BITRATE,
        audio_bitrate="192k",
        preset=VIDEO_PRESET,
        threads=VIDEO_THREADS,
        temp_audiofile=_tmp_snd,
        verbose=False,
        logger=None,
    )
    log.info(f"[timing] encode={_time.monotonic() - _t_enc0:.1f}")
    final.close()
    bg_clip.close()
    for ac in audio_clips:
        ac.close()

    # 60초 방어 실측 — 인코딩 결과 검사. 열리지 않거나 길이가 이상하면 **발행 차단**
    # (깨진 mp4 를 올리는 게 최악. codex 지적 반영, 2026-09-03).
    try:
        with VideoFileClip(output_path) as _chk:
            _dur = float(_chk.duration or 0.0)
    except Exception as e:
        raise RuntimeError(f"완성 영상을 열 수 없음(깨진 파일) — 발행 차단: {output_path}: {e}")
    if _dur < 1.0:
        raise RuntimeError(f"완성 영상 길이 이상({_dur:.2f}s) — 발행 차단: {output_path}")
    if _dur > MAX_TOTAL_DURATION + RENDER_DURATION_TOLERANCE:
        raise RuntimeError(
            f"완성 영상 {_dur:.2f}s > 방어선 {MAX_TOTAL_DURATION}s + 허용오차 "
            f"{RENDER_DURATION_TOLERANCE}s — 타임라인 버그 의심, 발행 차단")
    if _dur > MAX_TOTAL_DURATION:
        log.warning(f"길이 방어 경계: 최종 {_dur:.2f}s > {MAX_TOTAL_DURATION}s (허용오차 내) — 대본 길이 점검 권장")
    else:
        log.info(f"최종 길이 {_dur:.2f}s (방어선 {MAX_TOTAL_DURATION}s 이내)")
    return output_path
