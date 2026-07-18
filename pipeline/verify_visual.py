"""GPT-4o-mini vision으로 영상 첫 프레임이 해당 도시 여행 배경으로 적합한지 검증.

여행봇 정책: 정밀한 도시 판별은 slug 사전 필터(_CITY_CONFUSION)가 1차로 담당하고,
vision은 2차로 (1) 명백히 다른 식별가능 도시의 랜드마크, (2) 실내 제품샷/스튜디오,
(3) 인물 셀피 위주, (4) 텍스트/지도 오버레이, (5) 식별불가/블러만 reject한다.
니치 소도시는 Pexels가 랜드마크 없이 일반 풍경만 줄 때가 많아 — 도시임을 증명하지 못해도
목적지에 어울리는 일반 도시/거리/음식/풍경이면 통과(발행 0 방지).
strict=True(아이콘 랜드마크 슬롯): 추가 reject 규칙은 없고 "이 슬롯은 랜드마크 선호" 강조만
주입한다(2026-07-14 cbd828d에서 오거절 대량 발생으로 의도적으로 완화 — 판정 기준은 base와 동일).
confusion_pair: 혼동 도시(예: Florence↔Rome)의 상징 랜드마크가 명백하면 reject하는 규칙을 주입.

2026-05-06 fix (Saturn 케이크 사고 계승): rate limit 429에서 fail-OPEN(return True)로
garbage가 통과하던 문제 → fail-CLOSED로 전환 + 429 backoff 재시도.
2026-07-17 fix: 인프라 fault(API 키 없음 / frame 추출 실패)도 fail-CLOSED로 전환 —
키 없음은 main.py 시작 시 fail-fast로도 차단되고, 여기서도 reject한다.
garbage 발행보다 발행 안 하는 게 낫다는 정책.

비용: gpt-4o-mini vision은 1 이미지 약 $0.0005~0.001.
"""
import base64
import logging
import os
import re
import subprocess
import time

from config import OPENAI_API_KEY

log = logging.getLogger("travelbot.vision")

# ffmpeg 경로: 시스템 PATH 우선 (GitHub Actions ubuntu apt-get 설치본),
# fallback으로 imageio_ffmpeg 번들 (로컬 Windows).
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = "ffmpeg"


def extract_frame(video_path: str, timestamp: float = 1.5) -> str:
    """ffmpeg로 영상의 timestamp초 시점 프레임을 PNG로 추출.

    timestamp=1.5: 첫 프레임은 검은 fade-in일 수 있어 1.5초가 안전.
    """
    out = video_path + ".verify.png"
    cmd = [
        FFMPEG, "-y",
        "-ss", str(timestamp),
        "-i", video_path,
        "-vframes", "1",
        "-vf", "scale=512:-1",  # 비전 API 비용 절감 + 충분히 식별 가능
        "-q:v", "3",
        out,
    ]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0 or not os.path.exists(out):
        return ""
    return out


def _build_question(city: str, country: str = "", landmark_lock: str = "",
                    strict: bool = False, confusion_pair: list = None, subject: str = "") -> str:
    """도시 여행 배경 적합성 판정 vision 프롬프트 (accept-by-default).

    2026-07-14 튜닝: 초기 프롬프트가 reject 편향이라 로마 콜로세움 내부·바티칸 성베드로
    같은 명백한 도시 배경까지 'no'로 오거절(로마 22개 중 18개 reject 실측)했다.
    정밀한 '다른 도시' 차단은 slug 필터(_CITY_CONFUSION + 키워드 도시명)가 1차로 하므로,
    vision은 '명백한 위반'만 걸러내고 애매하면 통과시킨다(가비지 발행보다 발행0이 낫다는
    fail-closed 정책은 유지하되, false-reject로 발행0이 되는 역효과를 막는다)."""
    where = city + (f", {country}" if country and country.lower() not in city.lower() else "")
    lm = (f" Landmarks and neighborhoods in or near {city} — which COUNT as {city} — include: {landmark_lock}."
          if landmark_lock else "")
    strict_line = (
        f" (This slot prefers an iconic landmark, so lean toward imagery that fits {where}"
        f" — but still ACCEPT any plausible {city} scene.)"
        if strict else ""
    )
    # 주제(자막 하이라이트) 인식 — vision이 '카나자와 랜드마크인가'만 보지 않고 그 문장 주제에
    # 맞는 장면(음식/거리 등)을 적극 accept하게 한다. (2026-07-18 쿠마님: 꽃 차단이 아니라
    # '주제에 맞는 영상 매칭'이 핵심. 음식 자막에 카레 클립이 product-shot으로 오거절되던 문제)
    subj_low = (subject or "").lower()
    _toks = set(re.findall(r"[a-z]+", subj_low))  # 단어경계 토큰화 ('Swedish'의 'dish' 오탐 방지)
    _food_words = {
        "food", "cuisine", "curry", "ramen", "sushi", "dish", "sweets", "seafood", "gourmet",
        "cafe", "coffee", "dessert", "delicacy", "culinary", "noodle", "noodles", "cooking",
        "sake", "sashimi", "gastronomy", "snack", "snacks", "bakery", "pastry", "restaurant",
        "wine", "beer", "tea", "matcha", "gelato", "kaiseki", "izakaya", "dining", "eat",
        "market", "markets", "marketplace", "fish", "produce", "grocer",
    }
    _food_phrases = ("ice cream", "gold leaf", "street food", "fish market", "food market", "seafood market")
    _street_words = {
        "street", "streets", "district", "alley", "alleys", "shopping", "nightlife", "quarter",
        "downtown", "nagamachi", "chaya", "samurai", "geisha", "arcade", "lane", "lanes",
    }
    subject_line = ""
    if (_toks & _food_words) or any(p in subj_low for p in _food_phrases):
        # reject 규칙 (1)-(5)는 그대로 우선. 이 힌트는 오직 규칙 (2)의 '음식=product shot' 오분류만 무효화.
        subject_line = (
            " NOTE: this clip is for a FOOD highlight. Rules (1)-(5) above still apply first; but for"
            " THIS highlight a dish, plate, bowl, restaurant, market stall, street-food, or dessert"
            " scene (including a close-up of food) does NOT count as a 'product/catalog shot' under"
            " rule (2) — answer 'yes' for genuine food / cooking / dining / market imagery in {city}'s"
            " country.".replace("{city}", city))
    elif _toks & _street_words:
        subject_line = (
            " NOTE: this clip is for a STREET / DISTRICT highlight. Rules (1)-(5) still apply first;"
            " any ordinary city street, alley, shopping arcade, market street, teahouse row, or"
            " neighborhood scene should be accepted — answer 'yes'.")
    # 혼동쌍 방어: slug 필터를 통과한 혼동 도시의 '상징 랜드마크'가 명백하면 reject.
    # (같은 나라 혼동쌍(Florence↔Rome)은 country 필터가 못 거르므로 vision이 최후 방어선)
    confusion_line = ""
    pairs = [c for c in (confusion_pair or []) if c and c.lower() != city.lower()]
    if pairs:
        confusion_line = (
            f" (1b) A signature landmark that unmistakably belongs to {', or '.join(pairs)}"
            f" — commonly confused with {city} — and is NOT in {city}."
        )
    return (
        f"This is a background clip for a 60-second travel video about {where}.{lm}"
        f" Default answer is 'yes'. Answer ONLY one word: 'yes' or 'no'."
        f" Answer 'no' ONLY if the image CLEARLY shows one of these:"
        f" (1) A world-famous landmark that unmistakably belongs to a DIFFERENT, far-away city or country"
        f" and is NOT in or near {city} — e.g. the Eiffel Tower (Paris), Big Ben (London),"
        f" Statue of Liberty (New York), Sydney Opera House, Taj Mahal, Golden Gate Bridge."
        f"{confusion_line}"
        f" (2) An obvious indoor studio / product / packaging / e-commerce catalog shot,"
        f" or a wedding / jewelry / cosmetics / fashion-beauty shoot as the main subject."
        f" (3) A single person's face or a selfie/portrait filling most of the frame"
        f" (a wide street, square, or landmark scene that happens to include tourists or people is FINE — accept it)."
        f" (4) Text, slides, charts, maps, or graphic overlays covering most of the frame."
        f" (5) Almost entirely black/blank, or too blurry to make out anything."
        f"{strict_line}{subject_line}"
        f" For EVERYTHING else — cityscapes, skylines, streets, alleys, squares, plazas, architecture,"
        f" ruins, temples, churches, cathedrals, domes, mosques, palaces, castles, monuments, bridges,"
        f" markets, cafes, local food and dishes, coastlines, beaches, mountains, rivers, canals, harbors,"
        f" gardens, aerials, night scenes, sunsets, and general scenic travel footage — answer 'yes'."
        f" You do NOT need to confirm it is specifically {city}; plausible travel imagery that fits the"
        f" destination is enough. When in doubt, answer 'yes'."
    )


def verify_city(video_path: str, city: str, country: str = "",
                landmark_lock: str = "", strict: bool = False,
                confusion_pair: list = None, subject: str = "") -> bool:
    """영상 첫 프레임이 {city} 여행 배경으로 적합한지 GPT-4o-mini vision으로 검증.

    True: 여행 배경 적합
    False: 다른 도시 랜드마크 / 비-여행 garbage / vision API 자체 실패(rate limit 포함)
           / 인프라 fault(API 키 없음, frame 추출 실패) — 전부 fail-CLOSED

    strict=True: 아이콘 랜드마크 슬롯 — 랜드마크 선호 강조(판정 기준은 base와 동일).
    confusion_pair: 혼동 도시 목록 — 그 도시들의 상징 랜드마크가 명백하면 reject.
    fail-CLOSED: 검증할 수 없는 클립은 발행하지 않는다. rate limit 429만 backoff 재시도 (3회).
    """
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY 없음 — vision 검증 불가 → REJECT (fail-closed). "
                  "main.py 시작 시 fail-fast로도 차단되지만 방어층으로 여기서도 거절.")
        return False

    img_path = extract_frame(video_path, timestamp=1.5)
    if not img_path:
        log.warning(f"frame 추출 실패: {os.path.basename(video_path)} — REJECT (fail-closed, 다른 클립으로 대체)")
        return False

    try:
        with open(img_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")

        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        question = _build_question(city, country, landmark_lock, strict, confusion_pair, subject)

        # rate limit 429에서 fail-OPEN으로 garbage 통과되던 사고 방지 — 3회 backoff 재시도.
        # 비-rate-limit 에러는 재시도 없이 즉시 fail-CLOSED.
        last_err = None
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": question},
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ],
                    }],
                    max_tokens=5,
                    temperature=0,
                )
                ans = (resp.choices[0].message.content or "").strip().lower()
                ok = ans.startswith("yes")
                log.info(f"vision verify: {os.path.basename(video_path)} → {city}{'(strict)' if strict else ''}? {ans!r} ({'OK' if ok else 'REJECT'})")
                return ok
            except Exception as e:
                last_err = e
                es = str(e).lower()
                is_rate = "rate_limit" in es or "429" in es or "tpm" in es or "too many requests" in es
                if is_rate and attempt < 2:
                    wait = [4, 12][attempt]
                    log.warning(f"vision rate limit → {wait}s 후 재시도 ({attempt+1}/3): {os.path.basename(video_path)}")
                    time.sleep(wait)
                    continue
                # 비-rate-limit 또는 마지막 시도 실패 → fail-CLOSED
                log.error(f"vision API 실패 → REJECT (fail-closed, attempt {attempt+1}/3): {os.path.basename(video_path)}: {e}")
                return False
        # for-else 보호망 (정상 흐름은 위에서 return으로 빠짐)
        log.error(f"vision API 3회 모두 실패 → REJECT: {last_err}")
        return False

    except Exception as e:
        log.error(f"vision verify pre-call 실패 → REJECT (fail-closed): {e}")
        return False

    finally:
        try:
            if img_path and os.path.exists(img_path):
                os.remove(img_path)
        except Exception:
            pass


def verify_animal(video_path: str, animal_single: str,
                  animal_display: str = "", animal_category: str = "") -> bool:
    """하위 호환 alias — 파이프라인 fork 잔존 호출용. verify_city로 위임.

    animal_single = 도시명. 국가/랜드마크 정보가 없으니 lenient 모드로만 검증한다
    (main.py는 verify_city를 직접 호출해 country/landmark_lock/strict를 넘긴다)."""
    return verify_city(video_path, animal_single, country="", landmark_lock="", strict=False)
