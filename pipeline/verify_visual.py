"""GPT-4o-mini vision으로 영상 첫 프레임이 해당 도시 여행 배경으로 적합한지 검증.

여행봇 정책: 정밀한 도시 판별은 slug 사전 필터(_CITY_CONFUSION)가 1차로 담당하고,
vision은 2차로 (1) 명백히 다른 식별가능 도시의 랜드마크, (2) 실내 제품샷/스튜디오,
(3) 인물 셀피 위주, (4) 텍스트/지도 오버레이, (5) 식별불가/블러만 reject한다.
니치 소도시는 Pexels가 랜드마크 없이 일반 풍경만 줄 때가 많아 — 도시임을 증명하지 못해도
목적지에 어울리는 일반 도시/거리/음식/풍경이면 통과(발행 0 방지).
strict=True(아이콘 랜드마크 슬롯): 다른 나라/지역이 명백한 이미지도 추가로 reject.

2026-05-06 fix (Saturn 케이크 사고 계승): rate limit 429에서 fail-OPEN(return True)로
garbage가 통과하던 문제 → fail-CLOSED로 전환 + 429 backoff 재시도.
인프라 fault(API 키 없음 / frame 추출 실패)만 통과 처리하고 vision 응답 자체가
실패한 경우는 reject — garbage 발행보다 발행 안 하는 게 낫다는 정책.

비용: gpt-4o-mini vision은 1 이미지 약 $0.0005~0.001.
"""
import base64
import logging
import os
import subprocess
import time

from config import OPENAI_API_KEY

log = logging.getLogger("spacebot.vision")

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
                    strict: bool = False) -> str:
    """도시 여행 배경 적합성 판정 vision 프롬프트."""
    where = city + (f", {country}" if country and country.lower() not in city.lower() else "")
    lm = f" Well-known landmarks of {city}: {landmark_lock}." if landmark_lock else ""
    strict_line = (
        " STRICT MODE (this slot should show an iconic landmark): ALSO reject 'no' if the image clearly"
        f" depicts a different country or region than {where}, or a generic scene that plainly belongs"
        " elsewhere. Prefer imagery consistent with the destination."
        if strict else ""
    )
    return (
        f"This image will be a background for a 60-second travel video about {where}.{lm}"
        f" Is it visually appropriate as a travel background for {city}?"
        f" Answer ONLY one word: 'yes' or 'no'."
        f" REJECT (answer 'no') if the image clearly shows ANY of these:"
        f" — A DIFFERENT, identifiable famous landmark that belongs to ANOTHER city"
        f" (e.g. the Eiffel Tower when the city is not Paris; the Statue of Liberty when the city is not New York)."
        f" — An indoor product shot, studio setup, packaging, or e-commerce catalog photo."
        f" — People as the main subject (selfies, portraits, models, couples posing, close-up faces)."
        f" — Text, maps, charts, slides, or graphic overlays dominating the frame."
        f" — Weddings, jewelry, cosmetics, fashion/beauty shoots as the main subject."
        f" — Image too blurry, dark, or unidentifiable."
        f"{strict_line}"
        f" ACCEPT (answer 'yes') for ANY travel-appropriate imagery consistent with the destination:"
        f" cityscapes, skylines, streets, alleys, squares, architecture, temples, churches, castles,"
        f" markets, local food and dishes, cafes, coastlines, beaches, mountains, rivers, bridges, harbors,"
        f" aerial/drone city views, sunsets over the city, gardens, and general scenic travel footage."
        f" It does NOT need to prove it is {city} — generic scenic/city/food/street imagery that fits the"
        f" destination is fine."
    )


def verify_city(video_path: str, city: str, country: str = "",
                landmark_lock: str = "", strict: bool = False) -> bool:
    """영상 첫 프레임이 {city} 여행 배경으로 적합한지 GPT-4o-mini vision으로 검증.

    True: 여행 배경 적합 (또는 인프라 fault — API 키 없음/frame 추출 실패만 통과)
    False: 다른 도시 랜드마크 / 비-여행 garbage / vision API 자체 실패(rate limit 포함)

    strict=True: 아이콘 랜드마크 슬롯 — 다른 나라/지역이 명백한 이미지도 추가 reject.
    fail-CLOSED: API 호출 실패는 reject. rate limit 429만 backoff 재시도 (3회).
    """
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY 없음 — vision 검증 스킵 (안전 통과)")
        return True

    img_path = extract_frame(video_path, timestamp=1.5)
    if not img_path:
        log.warning(f"frame 추출 실패: {os.path.basename(video_path)} — 안전 통과")
        return True

    try:
        with open(img_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")

        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        question = _build_question(city, country, landmark_lock, strict)

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
