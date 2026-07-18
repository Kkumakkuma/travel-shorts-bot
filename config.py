"""Wander 60s — 통합 설정 (Space Trivia 60s fork → 여행 도시 소개형).

매 cron마다 CITIES(cities.json)에서 랜덤 도시 1개 선택 →
GPT가 앵글(랜드마크/음식/역사/명소/숨은명소)을 정해 하이라이트 4~5개 생성 →
edge-tts 영문 내레이션 + Pexels 도시 영상/사진 + 시간차 자막으로 1분 영상 합성 →
YouTube Shorts (영문 메인 + 7개국 localizations) 자동 발행.

Note: 파이프라인 코드는 변수명 (ANIMALS, animal_key 등)을 그대로 쓰기도 한다 — 호환을 위해
ANIMALS = CITIES 로 별칭을 둔다. 실제 내용은 도시 소개 메타데이터.
동물봇/우주봇의 fork이므로 공통 모듈 수정 시 3 repo 동기화 원칙(feedback_shorts_dual_apply).
"""

# 채널 메타 (city_showcase.py GPT prompt + youtube_upload 등에서 참조)
CHANNEL_NAME = "Wander 60s"
CHANNEL_TAGLINE = "a new city in 60 seconds"
CONTENT_DOMAIN = "travel"  # grounding 도메인 맥락(동음이의 위키 오매칭 방지) + 코드 분기
CHANNEL_NICHE = "travel, world cities, famous landmarks, local food, culture, history, hidden gems"
# auth.py 채널 검증에 쓰는 허용 키워드(리브랜드된 여행 채널명이 이 중 하나를 포함해야 통과)
CHANNEL_KEYWORDS = ["wander", "travel", "trip", "journey", "city", "cities", "60s"]
# 화면에 박히는 브랜딩(composer 워터마크/CTA). 우주봇 잔재(@SpaceTrivia60s/"for more space facts") 대체.
# 채널 핸들은 리브랜드 후 실제 핸들로 맞출 것(쿠마님 YouTube Studio 설정값과 동기화).
CHANNEL_HANDLE = "@Wander60s"
# "daily"는 실제 발행 주기(3일 간격)와 불일치하는 허위 약속이라 주기 무관 문구로 교체 (2026-07-17)
BRAND_CTA_SUBTITLE = "for more city guides"
import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
CREDENTIALS_DIR = os.path.join(BASE_DIR, "credentials")

# .env
_env = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env):
    for line in open(_env, encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

# YouTube — 기존 음악 채널을 여행으로 리브랜드해 재사용. 음악 채널 토큰을 여기 넣는다.
YOUTUBE_CLIENT_SECRET = os.path.join(CREDENTIALS_DIR, "youtube_client_secret.json")
YOUTUBE_TOKEN = os.path.join(CREDENTIALS_DIR, "youtube_token.json")
YOUTUBE_CATEGORY_ID = "19"  # Travel & Events
CONTAINS_SYNTHETIC_MEDIA = True  # TTS는 AI 합성 음성

# Video — Shorts (1080x1920)
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30
VIDEO_DURATION_SEC = 60
# 적응형 길이 하한. 쿠마님 지시(2026-07-18): 숏츠는 최소 55초·최대한 60초를 채운다.
# 40→55 상향. 데드타임 방지는 '하한을 낮춤'이 아니라 '내레이션을 55s+로 길게'로 해결한다
# (하이라이트 26~34단어 x5 + TTS_RATE 완화). 짧게 나오면 이 하한이 55s를 보장한다.
MIN_VIDEO_DURATION_SEC = 55
VIDEO_BITRATE = "4500k"
VIDEO_PRESET = "veryfast"
VIDEO_THREADS = 8

# 영문 채널이라 영문 sans-serif 굵은 폰트 우선
FONT_PATH = os.path.join(ASSETS_DIR, "fonts", "Roboto-Bold.ttf")

# OpenAI — 생성/검증 모델 분리로 자기합치 편향 완화(우주봇 교훈 계승)
META_MODEL = "gpt-4o-mini"      # 경량 호출(번역·키워드 등)
SCRIPT_MODEL = "gpt-4o"         # 도시 픽 + 하이라이트 생성 (사실 정확도 우선)
FACTCHECK_MODEL = "gpt-4.1"     # 적대적 검증 — 생성 모델과 다른 계열

# edge-tts voice (영문)
TTS_VOICE = "en-US-AriaNeural"  # 자연스러운 여성 보이스
TTS_RATE = "+2%"  # 55~60s 길이 확보 위해 완화(2026-07-18, 기존 +12%는 44s로 짧게 끝남). 또렷함↑
TTS_PITCH = "+0Hz"

# 자막 스타일
CAPTION_BASE_SIZE = 80
CAPTION_Y_RATIO = 0.60  # 하단 1/3 (피사체 가림 해소 + 쇼츠 UI 회피)
CAPTION_PHRASE_SIZE = 4  # 한 자막에 3-4 단어 묶음
CAPTION_HIGHLIGHT_NUMBERS = True  # 숫자는 옐로우 강조

# BGM — assets/bgm/*.mp3 풀 or GDrive(뮤직봇 큐) read-only. 없으면 BGM 없이 진행
BGM_VOLUME_DB = -22  # TTS 가리지 않게 작게

# 콘텐츠 이력 (시간 기반 중복 방지 + 클립 dedup + angle 로테이션)
CONTENT_HISTORY = os.path.join(BASE_DIR, "content_history.json")
CONTENT_WINDOW_DAYS = 120       # 여행: 100개 도시 1회전(발행빈도 감안)보다 넓게 재등장 차단
HISTORY_MIN_CANDIDATES = 8      # 제외 후 후보 8개 미만이면 창 0.75배씩 완화
HISTORY_MIN_WINDOW_DAYS = 14    # 완화 하한 (한 번에 전체 리셋 금지)
CLIP_DEDUP_DAYS = 30            # 30일 내 사용한 Pexels 클립 id 재사용 금지
RECENT_FORMAT_KEEP = 2          # 전역 직전 N편 format+angle 연속 회피 (2026-07-17: angle 회전 실배선)

# 미디어 소스: 우주봇 NASA 자리를 Pexels-사진으로 치환
USE_NASA_MEDIA = False          # 여행봇: NASA 미사용
USE_PEXELS_PHOTO = True         # Pexels 사진 → Ken Burns 폴백 tier 사용

# 도시 로스터 — cities.json(100개)에서 로드. 코드는 ANIMALS 변수명 재사용(호환).
CITIES_JSON = os.path.join(BASE_DIR, "cities.json")


def _load_cities():
    """cities.json → {city_key: {display, single, country, category, pexels_keywords, landmark_lock, confusion_pair?}}."""
    if not os.path.exists(CITIES_JSON):
        return {}
    with open(CITIES_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    # cities.json은 list[dict] 또는 dict 모두 허용
    items = raw.values() if isinstance(raw, dict) else raw
    for c in items:
        key = c.get("city_key") or c["city"].lower().replace(" ", "_")
        out[key] = {
            "display": c.get("display") or c["city"],
            "single": c.get("single") or c["city"],
            "country": c.get("country", ""),
            "category": c.get("category", "city"),
            "pexels_keywords": c.get("pexels_keywords", []),
            "landmark_lock": c.get("landmark_lock", ""),
            "confusion_pair": c.get("confusion_pair", []),
        }
    return out


CITIES = _load_cities()
ANIMALS = CITIES  # 파이프라인 코드 호환 별칭 (실제 내용은 도시)
