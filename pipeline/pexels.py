"""Pexels에서 여행 도시 영상/사진 다운로드 (세로/portrait 우선).

2026-07-14 여행봇 전환: URL slug 기반 사전 필터로 vision 호출 전에 garbage 후보를 일찍 거른다.
여행 특유의 오매칭 = "동음이의 도시"(Paris France ↔ Paris Texas, Venice ↔ Venice Beach 등)를
_CITY_CONFUSION 하드 쌍으로 사전 차단하고, 비-여행 garbage(제품/스튜디오/웨딩/뷰티)를 거른다.
사진 tier(fetch_photos)는 v1/search 엔드포인트로 portrait 사진을 받아 Ken Burns 소스로 쓴다.
"""
import logging
import os
import random
import shutil
import subprocess

import requests

from config import PEXELS_API_KEY, VIDEO_DURATION_SEC, CONTENT_DOMAIN

log = logging.getLogger("travelbot.pexels")
PEXELS_API = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_API = "https://api.pexels.com/v1/search"


def _media_ok(path: str, min_bytes: int = 30_000) -> bool:
    """다운로드 클립 무결성 검증 — 잘린/손상 파일이 MoviePy/ffmpeg 인코딩을 깨뜨리는 것 방지.
    ffmpeg로 전체 디코드(-xerror)해 스트림 중간 truncation('0 bytes read at frame N')을 잡는다.
    도구(ffmpeg)가 없으면 크기만 검사해 통과 — 인프라 fault로 발행을 막지 않는다(fail-open on tooling)."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < min_bytes:
            return False
    except OSError:
        return False
    ff = shutil.which("ffmpeg")
    if not ff:
        return True
    try:
        r = subprocess.run(
            [ff, "-v", "error", "-xerror", "-i", path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=90,
        )
        return r.returncode == 0 and not (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return True

# Pexels slug에 이 단어가 있고 여행 토큰/도시명이 없으면 사전 reject.
# 여행 검색엔 제품/스튜디오/웨딩/뷰티/모델 류 garbage가 섞여 들어옴.
_BLOCK_SLUG_WORDS = {
    "wedding", "weddings", "bride", "groom", "engagement",
    "jewelry", "necklace", "diamond", "ring", "rings",
    "manicure", "fashion", "model", "models", "selfie", "makeup", "cosmetic", "cosmetics",
    "lingerie", "bikini", "lipstick", "perfume", "skincare",
    "product", "packaging", "mockup", "advertisement", "ecommerce",
    "baby", "newborn", "infant", "toddler",
    "office", "meeting", "workspace", "laptop", "coding",
}

# 여행/도시 토큰이 slug에 있으면 위 차단어가 있어도 진짜 여행 사진일 가능성 → 통과.
_TRAVEL_TOKENS = {
    "city", "cityscape", "town", "village", "downtown", "skyline", "street", "streets",
    "alley", "square", "plaza", "boulevard", "avenue", "old",
    "travel", "tourism", "tourist", "trip", "vacation", "holiday", "landmark",
    "architecture", "building", "buildings", "temple", "shrine", "church", "cathedral",
    "mosque", "palace", "castle", "fortress", "ruins", "monument", "tower", "bridge",
    "market", "bazaar", "cafe", "restaurant", "food", "cuisine", "dish", "street-food",
    "beach", "coast", "coastline", "sea", "ocean", "island", "harbor", "harbour", "port",
    "mountain", "mountains", "hill", "hills", "river", "lake", "waterfall", "canal",
    "garden", "gardens", "park", "forest", "desert", "valley", "countryside",
    "sunset", "sunrise", "aerial", "drone", "panorama", "night", "nightlife", "lantern",
    "canals", "gondola", "rooftop", "skyscraper", "promenade", "boardwalk",
}

# 꽃/식물/곤충 매크로·클로즈업 하드 차단 (2026-07-18 쿠마님 지시: 카나자와 첫 화면이
# 붓꽃 클로즈업으로 나온 사고). 도시 소개 배경에 부적합한 자연 매크로는 여행 토큰/도시명이
# 같이 있어도 무조건 reject한다 — 도시 전경·건축·거리·랜드마크를 우선한다.
# (cherry/blossom/maple은 벚꽃·단풍 광각 명소일 수 있어 제외하지 않음.)
_HARD_MACRO_WORDS = {
    "flower", "flowers", "floral", "iris", "irises", "tulip", "tulips", "orchid", "orchids",
    "lily", "lilies", "rose", "roses", "daisy", "sunflower", "sunflowers", "lavender",
    "petal", "petals", "bloom", "blooms", "blooming", "bouquet", "florist", "botanical",
    "macro", "closeup", "close-up", "pollen", "dew", "potted", "houseplant", "succulent",
    "insect", "insects", "butterfly", "butterflies", "bee", "bees", "dragonfly", "ladybug",
    "mushroom", "moss",
}


def _slug_tokens(url: str, sep: str = "/video/") -> set:
    """Pexels url slug → 단어 토큰 집합 (소문자). sep은 video/photo 구분."""
    if not url:
        return set()
    try:
        low = url.lower()
        seg = sep if sep in low else ("/photo/" if "/photo/" in low else "/video/")
        slug = low.split(seg, 1)[1].rstrip("/")
        # 끝의 숫자 ID 제거: "kyoto-fushimi-inari-9168193" → "kyoto-fushimi-inari"
        parts = slug.split("-")
        if parts and parts[-1].isdigit():
            parts = parts[:-1]
        return {p for p in parts if p}
    except Exception:
        return set()


# 동음이의 도시 하드 쌍: 도시명(소문자) → 이 토큰이 slug에 있으면(도시 자체 국가/맥락이 아니면)
# 다른 곳(주로 미국 동명 도시)이므로 reject. 대소문자 무시, 부분 토큰.
# 항목은 공백 구분 다중 토큰 허용("new york" = slug 토큰에 new와 york가 모두 있으면 매치).
# 2026-07-17 fix: 과거 연결형('newyork')/하이픈형('nice-day') 항목은 '-' 분해 토큰과
# whole-token 교집합으로는 절대 매치 불가였다(Rome NY·Hamburg NY·Berlin NH 무방비).
_CITY_CONFUSION = {
    "paris": {"texas", "tx", "vegas", "kentucky", "tennessee", "ontario", "hilton"},
    "venice": {"beach", "california", "los angeles", "florida"},
    "athens": {"georgia", "ohio", "ga", "alabama"},
    "naples": {"florida", "fl"},
    "cairo": {"illinois", "georgia", "il"},
    "sydney": {"nova", "scotia", "montana"},
    "rome": {"georgia", "new york", "ny"},
    "florence": {"alabama", "sc", "kentucky", "oregon"},
    "dublin": {"ohio", "california", "georgia"},
    "moscow": {"idaho"},
    "berlin": {"new hampshire", "connecticut", "maryland"},
    "vienna": {"virginia", "sausage"},
    "lima": {"ohio", "bean"},
    "manchester": {"new hampshire", "connecticut"},
    "birmingham": {"alabama"},
    "melbourne": {"florida"},
    "columbia": {"missouri", "carolina", "maryland"},
    "hamburg": {"new york", "burger"},
    "st petersburg": {"florida"},
    "nice": {"guy", "weather", "nice day"},
}


def _has_confused_city(tokens: set, city_lower: str, country_lower: str = "") -> bool:
    """slug 토큰에 이 도시의 동음이의 오매칭 토큰이 있으면 True (해당 도시 국가는 예외).

    항목이 다중 토큰("new york")이면 그 토큰들이 slug 토큰에 전부 있어야 매치 —
    'rome-new-york-street'는 {new, york} ⊆ {rome, new, york, street} 로 잡힌다."""
    conf = _CITY_CONFUSION.get(city_lower)
    if not conf:
        return False
    hit = False
    for c in conf:
        parts = {p for p in c.replace("-", " ").split() if p}
        if parts and parts <= tokens:
            hit = True
            break
    if not hit:
        return False
    # 도시 자체 국가/맥락 토큰이 slug에 있으면 오매칭 아님 (진짜 그 도시)
    country_words = {w for w in (country_lower or "").split() if len(w) >= 3}
    if tokens & country_words:
        return False
    return True


def _neighbor_tokens(confusion_pair) -> set:
    """cities.json confusion_pair(시각적으로 헷갈리는 다른 도시 이름 리스트) → 토큰 집합."""
    out = set()
    for name in (confusion_pair or []):
        if not isinstance(name, str):
            continue
        for w in name.lower().split():
            if len(w) >= 3:
                out.add(w)
    return out


def _has_confused_neighbor(tokens: set, single_lower: str, neighbor_tokens: set) -> bool:
    """slug 토큰이 (본 도시가 아닌) 헷갈리는 다른 도시명을 명시하면 True.

    본 도시명이 slug에 같이 있으면 진짜 그 도시 사진일 가능성 → 오매칭 아님."""
    if not neighbor_tokens:
        return False
    hit = tokens & neighbor_tokens
    if not hit:
        return False
    single_words = {w for w in (single_lower or "").split() if len(w) >= 3}
    if tokens & single_words:
        return False
    return True


def _passes_slug_filter(video: dict, single_lower: str, country_lower: str = "",
                        url_sep: str = "/video/", neighbor_tokens: set = None) -> bool:
    """Pexels url slug 토큰 기반 사전 필터.

    True: vision 검증으로 넘김
    False: 사전 reject (garbage/동음이의 도시/헷갈리는 이웃 도시 후보 일찍 거름)
    """
    tokens = _slug_tokens(video.get("url", ""), sep=url_sep)
    if not tokens:
        # url 정보 없으면 일단 vision으로 넘김 (안전 통과)
        return True
    # 꽃/식물/곤충 매크로 하드 차단 (여행 토큰/도시명 있어도 무조건 reject) — 첫 화면 꽃 사고 방지.
    # 단 '여행 도메인'에서만 적용: 동물봇(butterfly/bee/flower가 정당한 토픽)에 이 모듈을
    # 동기화해도 배경이 전량 차단되지 않도록 CONTENT_DOMAIN 게이트로 격리(fork 안전).
    if CONTENT_DOMAIN == "travel" and (tokens & _HARD_MACRO_WORDS):
        return False
    # 동음이의 도시 하드 차단 (Paris↔Texas 등) — 도시명이 slug에 있어도 무조건 reject
    if _has_confused_city(tokens, single_lower, country_lower):
        return False
    # 시각적으로 헷갈리는 다른 도시명이 slug에 명시되면 reject (본 도시명 없을 때만)
    if _has_confused_neighbor(tokens, single_lower, neighbor_tokens or set()):
        return False
    blocked = tokens & _BLOCK_SLUG_WORDS
    if not blocked:
        return True
    # 차단어 있어도 여행 토큰이 같이 들어있으면 진짜 여행 사진일 가능성 → 통과
    if tokens & _TRAVEL_TOKENS:
        return True
    if single_lower:
        single_words = {w for w in single_lower.split() if len(w) >= 3}
        if tokens & single_words:
            return True
    return False


def fetch_background(keywords: list, output_dir: str) -> str:
    """키워드 후보 → portrait 영상 → 로컬 mp4 경로."""
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY 없음")

    os.makedirs(output_dir, exist_ok=True)
    headers = {"Authorization": PEXELS_API_KEY}

    last_err = None
    candidates = list(keywords)
    random.shuffle(candidates)

    for kw in candidates:
        try:
            r = requests.get(
                PEXELS_API,
                headers=headers,
                params={"query": kw, "per_page": 20, "orientation": "portrait"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            videos = data.get("videos", [])
            if not videos:
                continue
            random.shuffle(videos)
            for v in videos:
                if v.get("duration", 0) < 8:
                    continue
                files = v.get("video_files", [])
                portrait_files = [f for f in files
                                  if f.get("width", 0) <= f.get("height", 0)
                                  and (f.get("file_type") == "video/mp4" or "mp4" in (f.get("link", "")))]
                if not portrait_files:
                    portrait_files = [f for f in files if f.get("file_type") == "video/mp4"]
                portrait_files.sort(key=lambda f: abs((f.get("width") or 0) - 1080))
                if not portrait_files:
                    continue
                pick = portrait_files[0]
                url = pick["link"]
                fname = f"pexels_{v['id']}.mp4"
                out = os.path.join(output_dir, fname)
                with requests.get(url, stream=True, timeout=60) as resp:
                    resp.raise_for_status()
                    with open(out, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 20):
                            fh.write(chunk)
                log.info(f"pexels bg: kw={kw} id={v['id']} dur={v.get('duration')}s")
                return out
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"Pexels 배경 못 찾음: {last_err}")


def fetch_backgrounds(keywords: list, output_dir: str, n: int = 3,
                       animal_single: str = "",
                       exclude_pexels_ids: list = None,
                       ordered: bool = False,
                       hard_exclude_ids: list = None,
                       country: str = "",
                       confusion_pair: list = None) -> list:
    """동영상 N개를 다운로드 (각 하이라이트 구간별로 다른 영상).

    animal_single: 도시명. 모든 키워드가 도시명을 포함하지 않으면
    fallback으로 도시명 여행 검색. generic 키워드로 엉뚱한 매칭되는 문제 방지.
    country: 동음이의 도시 slug 필터에서 "진짜 그 도시" 판별에 사용.
    confusion_pair: 시각적으로 헷갈리는 다른 도시 이름 리스트(cities.json) — slug에 명시되면 reject.

    exclude_pexels_ids: 최근 N개 영상에서 사용된 Pexels video_id 리스트.
    같은 풋티지 반복으로 채널 영상이 비슷해 보이는 문제 방지 (2026-05-07
    사용자 지적: Saturn/Uranus 두 영상 풋티지 가타). 풀이 부족할 때만 일부 허용.

    ordered: True면 키워드를 셔플하지 않고 주어진 순서대로 각 키워드당 1클립씩
    수집 → fact_keywords를 fact 순서대로 넘기면 배경도 fact 순서로 매칭됨.
    (2026-07-05 per-fact 배경 매칭)
    """
    paths = []
    used_ids = set()
    blocked_soft = set(int(i) for i in (exclude_pexels_ids or []) if str(i).isdigit() or isinstance(i, int))
    blocked_hard = set(int(i) for i in (hard_exclude_ids or []) if str(i).isdigit() or isinstance(i, int))
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY 없음")
    os.makedirs(output_dir, exist_ok=True)
    headers = {"Authorization": PEXELS_API_KEY}

    # 토픽명(도시명) 검증 안전망 — 키워드에 도시명이 없으면 generic으로 매칭 위험
    single_lower = ""
    country_lower = (country or "").lower()
    neighbor_tokens = _neighbor_tokens(confusion_pair)
    if animal_single:
        single_lower = animal_single.lower()
        verified = [kw for kw in keywords if single_lower in kw.lower()]
        if not verified:
            # 여행: 도시명 여행 fallback (도시명 포함 강제)
            verified = [animal_single, f"{animal_single} city", f"{animal_single} travel",
                        f"{animal_single} landmark", f"{animal_single} street"]
        keywords = verified
        log.info(f"pexels keywords (city-locked): {keywords}")

    if ordered:
        candidates = list(keywords)  # fact 순서 유지 (셔플/증식 안 함)
    else:
        candidates = list(keywords) * 3  # 키워드 풀 늘림
        random.shuffle(candidates)

    skipped_slug = 0
    skipped_recent = 0
    # 1차: soft+hard 모두 차단. 2차: 풀 부족 시 soft만 완화, **hard는 유지**.
    for pass_idx in (0, 1):
        block_set = (blocked_soft | blocked_hard) if pass_idx == 0 else blocked_hard
        if pass_idx == 1 and len(paths) >= n:
            break
        if pass_idx == 1:
            log.warning(f"pexels: 1차 후 {len(paths)}/{n} 부족 — 최근 사용 차단 풀고 재시도")
        for kw in candidates:
            if len(paths) >= n:
                break
            try:
                r = requests.get(
                    PEXELS_API,
                    headers=headers,
                    params={"query": kw, "per_page": 20, "orientation": "portrait"},
                    timeout=20,
                )
                r.raise_for_status()
                data = r.json()
                videos = data.get("videos", [])
                random.shuffle(videos)
                for v in videos:
                    if v.get("id") in used_ids:
                        continue
                    if v.get("id") in block_set:
                        skipped_recent += 1
                        continue
                    if v.get("duration", 0) < 6:
                        continue
                    # URL slug 사전 필터 (vision 비용/TPM 절약 + garbage/동음이의·이웃 도시 사전 차단)
                    if not _passes_slug_filter(v, single_lower, country_lower,
                                               neighbor_tokens=neighbor_tokens):
                        skipped_slug += 1
                        continue
                    files = v.get("video_files", [])
                    portrait_files = [f for f in files
                                      if f.get("width", 0) <= f.get("height", 0)
                                      and (f.get("file_type") == "video/mp4" or "mp4" in (f.get("link", "")))]
                    if not portrait_files:
                        portrait_files = [f for f in files if f.get("file_type") == "video/mp4"]
                    portrait_files.sort(key=lambda f: abs((f.get("width") or 0) - 1080))
                    if not portrait_files:
                        continue
                    pick = portrait_files[0]
                    url = pick["link"]
                    fname = f"pexels_{v['id']}.mp4"
                    out = os.path.join(output_dir, fname)
                    with requests.get(url, stream=True, timeout=60) as resp:
                        resp.raise_for_status()
                        with open(out, "wb") as fh:
                            for chunk in resp.iter_content(chunk_size=1 << 20):
                                fh.write(chunk)
                    if not _media_ok(out):
                        log.warning(f"pexels bg id={v['id']} 손상/잘린 다운로드 → 폐기·재선택")
                        try:
                            os.remove(out)
                        except OSError:
                            pass
                        continue
                    paths.append(out)
                    used_ids.add(v["id"])
                    log.info(f"pexels bg[{len(paths)}]: kw={kw} id={v['id']} dur={v.get('duration')}s")
                    if len(paths) >= n:
                        break
                    if ordered:
                        break  # 키워드당 1클립 → 다음 키워드로 (fact 순서 유지)
            except Exception as e:
                log.warning(f"pexels kw={kw} 실패: {e}")
                continue

    if skipped_slug:
        log.info(f"pexels slug filter: {skipped_slug}개 garbage 후보 사전 차단")
    if skipped_recent:
        log.info(f"pexels dedup: {skipped_recent}개 최근 사용 풋티지 차단 (used_pexels.json)")
    if not paths:
        raise RuntimeError("Pexels 영상 하나도 못 찾음")
    # 호출 측에서 사용한 ID 추적 가능하도록 paths의 파일명에 video_id가 포함됨 (pexels_<id>.mp4).
    return paths


def fetch_photos(keywords: list, output_dir: str, n: int = 1,
                 exclude_ids=None, animal_single: str = "", country: str = "",
                 confusion_pair: list = None) -> list:
    """Pexels v1/search에서 portrait 사진 N장 다운로드 → 로컬 png 경로 리스트.

    영상(fetch_backgrounds)이 부족한 슬롯을 Ken Burns 사진 tier로 채우기 위한 소스.
    같은 slug 필터(동음이의 도시/garbage 차단) + id dedup 적용. 실패해도 예외 없이
    수집한 만큼 반환(빈 리스트 가능) — 호출 측이 다음 tier로 폴백.

    파일명 pexels_photo_<id>.png (Pexels 바이트가 jpeg여도 PIL이 내용으로 포맷 판별).
    exclude_ids: 재사용 금지 photo id (str/int).
    """
    if not PEXELS_API_KEY:
        return []
    os.makedirs(output_dir, exist_ok=True)
    headers = {"Authorization": PEXELS_API_KEY}
    single_lower = (animal_single or "").lower()
    country_lower = (country or "").lower()
    neighbor_tokens = _neighbor_tokens(confusion_pair)
    blocked = {str(i) for i in (exclude_ids or [])}
    used_ids = set()
    paths = []

    candidates = [k for k in (keywords or []) if isinstance(k, str) and k.strip()]
    random.shuffle(candidates)
    skipped_slug = 0

    for kw in candidates:
        if len(paths) >= n:
            break
        try:
            r = requests.get(
                PEXELS_PHOTO_API,
                headers=headers,
                params={"query": kw, "per_page": 20, "orientation": "portrait"},
                timeout=20,
            )
            r.raise_for_status()
            photos = r.json().get("photos", [])
            random.shuffle(photos)
            for ph in photos:
                if len(paths) >= n:
                    break
                pid = str(ph.get("id", ""))
                if not pid or pid in used_ids or pid in blocked:
                    continue
                if not _passes_slug_filter(ph, single_lower, country_lower, url_sep="/photo/",
                                           neighbor_tokens=neighbor_tokens):
                    skipped_slug += 1
                    continue
                src = ph.get("src") or {}
                url = src.get("portrait") or src.get("large2x") or src.get("large") or src.get("original")
                if not url:
                    continue
                out = os.path.join(output_dir, f"pexels_photo_{pid}.png")
                try:
                    with requests.get(url, stream=True, timeout=60) as resp:
                        resp.raise_for_status()
                        with open(out, "wb") as fh:
                            for chunk in resp.iter_content(chunk_size=1 << 20):
                                fh.write(chunk)
                except Exception as e:
                    log.warning(f"pexels photo 다운로드 실패 id={pid}: {e}")
                    continue
                if not _media_ok(out):
                    log.warning(f"pexels photo id={pid} 손상/잘린 다운로드 → 폐기·재선택")
                    try:
                        os.remove(out)
                    except OSError:
                        pass
                    continue
                paths.append(out)
                used_ids.add(pid)
                log.info(f"pexels photo[{len(paths)}]: kw={kw} id={pid}")
        except Exception as e:
            log.warning(f"pexels photo kw={kw} 실패: {e}")
            continue

    if skipped_slug:
        log.info(f"pexels photo slug filter: {skipped_slug}개 후보 사전 차단")
    return paths
