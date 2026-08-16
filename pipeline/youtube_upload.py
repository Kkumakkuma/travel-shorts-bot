"""YouTube 업로드 — defaultLanguage=en + localizations + AI 라벨 자동."""
import logging
import os
import pickle
import random
import re

from config import (
    CONTAINS_SYNTHETIC_MEDIA,
    YOUTUBE_CATEGORY_ID,
    YOUTUBE_CLIENT_SECRET,
    YOUTUBE_TOKEN,
)
import config as _cfg

log = logging.getLogger("travelbot.youtube")

# 업로드 직전 채널 결속 재검증 기준 (auth.py와 동일 — feedback_oauth_channel_verify)
_CHANNEL_KEYWORDS = [k.lower() for k in getattr(_cfg, "CHANNEL_KEYWORDS", [])]
_FOREIGN_CHANNEL_WORDS = ["lofi", "lit", "animal", "space trivia", "spacetrivia"]


def _verify_channel(svc):
    """업로드 직전 토큰이 여행 채널에 결속됐는지 재검증 (fail-closed).

    auth.py는 발급 시점만 검증한다 — 잘못된 토큰이 CI secret으로 유입되는 경로를
    업로드 시점에서 최종 차단한다. 불일치면 RuntimeError로 업로드 자체를 중단."""
    resp = svc.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("channel verify 실패: 채널 조회 결과 없음 — 업로드 중단 (fail-closed)")
    title = items[0]["snippet"]["title"]
    low = title.lower()
    if any(w in low for w in _FOREIGN_CHANNEL_WORDS):
        raise RuntimeError(f"channel verify 실패: 토큰이 다른 봇 채널('{title}')에 결속 — 업로드 중단")
    if _CHANNEL_KEYWORDS and not any(k in low for k in _CHANNEL_KEYWORDS):
        raise RuntimeError(f"channel verify 실패: '{title}'는 여행 채널 키워드 미포함 — 업로드 중단")
    log.info(f"channel verify OK: {title}")
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

# 업로드 후 자동 댓글 풀 (reply-magnet — 답글 유도형. "Tag a friend"/"Like if" 같은
# 답글 0% 죽은 패턴 금지, 의문/맞히기/도전형으로 댓글 engagement 유도. 우주 토픽)
# custom_comment(도시별 GPT 생성)이 없을 때만 쓰는 여행용 폴백 풀 (도시 무관 일반형).
COMMENT_HOOKS = [
    "Which city should we cover next? 👇",
    "Save this for your next trip ✈️",
    "Would you add this one to your bucket list?",
    "First time here — where should we go first? 👇",
    "Be honest: is this city on your list yet?",
    "What's the best city you've ever visited?",
    "Drop a 📍 if you'd book a flight right now",
    "Solo, couple, or squad trip for this one?",
]


USED_COMMENTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "used_comments.json")


def _load_used_comments() -> list:
    import json as _json
    if not os.path.exists(USED_COMMENTS_PATH):
        return []
    try:
        return _json.load(open(USED_COMMENTS_PATH, encoding="utf-8"))
    except Exception:
        return []


def _save_used_comments(used: list, keep: int = 5):
    import json as _json
    _json.dump(used[-keep:], open(USED_COMMENTS_PATH, "w", encoding="utf-8"))


def post_auto_comment(svc, video_id: str, text: str = None) -> str:
    if not text:
        used = _load_used_comments()
        pool = [c for c in COMMENT_HOOKS if c not in used[-5:]]
        if not pool:
            pool = COMMENT_HOOKS
        text = random.choice(pool)
        used.append(text)
        _save_used_comments(used)
    try:
        body = {
            "snippet": {
                "videoId": video_id,
                "topLevelComment": {
                    "snippet": {"textOriginal": text}
                }
            }
        }
        resp = svc.commentThreads().insert(part="snippet", body=body).execute()
        log.info(f"auto-comment posted: {video_id} — {text[:40]}")
        return resp.get("id")
    except Exception as e:
        log.warning(f"auto-comment 실패 (영상은 정상): {e}")
        return None


def get_service():
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(YOUTUBE_TOKEN):
        with open(YOUTUBE_TOKEN, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # CI(headless)에서 run_local_server는 브라우저 인증 대기로 timeout(40분)까지
            # 블록된다 — refresh 불가 토큰이면 즉시 실패시켜 낭비·지연을 막는다 (2026-07-17)
            if os.environ.get("GITHUB_ACTIONS"):
                raise RuntimeError("CI에서 대화형 OAuth 불가 — YT_TOKEN_B64 시크릿(refresh_token 포함) 확인 필요")
            flow = InstalledAppFlow.from_client_secrets_file(YOUTUBE_CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(YOUTUBE_TOKEN), exist_ok=True)
        with open(YOUTUBE_TOKEN, "wb") as f:
            pickle.dump(creds, f)
    return build("youtube", "v3", credentials=creds)


def has_caption_scope() -> bool:
    """captions.insert에 필요한 youtube.force-ssl scope가 현재 토큰에 있는지 검사.

    확정적으로 '없음'일 때만 False(→ captions 단계만 skip). 토큰 없음 / scope 정보 불명(None·빈값)은
    판단 보류 → True (영상 업로드 경로는 막지 않고, captions는 insert 자체가 fail-soft라 안전).
    구 토큰(youtube.upload만 보유)에서 force-ssl 미포함이면 여기서 잡아 무의미한 8회 실패를 방지."""
    force_ssl = "https://www.googleapis.com/auth/youtube.force-ssl"
    try:
        if not os.path.exists(YOUTUBE_TOKEN):
            return True
        with open(YOUTUBE_TOKEN, "rb") as f:
            creds = pickle.load(f)
        scopes = getattr(creds, "scopes", None)
        if not scopes:
            return True  # scope 정보 없음 → 판단 보류(시도 허용)
        return force_ssl in scopes
    except Exception:
        return True  # 판단 불가 → 보류(시도 허용)


def _sanitize_tags(tags):
    """YouTube 태그 한도 가드 — invalidTags(400) 시 발행 전체가 죽는 것 방지(2026-07-20).
    규칙: str 만, 태그당 100자 컷, 최대 30개, 합산 480자(공백 포함 태그는 API 가 따옴표
    2자를 가산하므로 +2로 계산 — 한도 500자에 여유).

    2026-08-16 위생 강화 — 발행분 태그 실측(YouTube Data API)에서 두 가지를 확인했다.
      · snake_case 슬러그 8건: long_tail / bird_of_prey / night_vision / color_change …
        아무도 밑줄 붙은 말로 검색하지 않으므로 검색 유입 0인 채 태그 슬롯만 차지한다.
      · 대소문자만 다른 중복 13건: ["Supernova", …, "supernova"] 처럼 한 영상에 동시 등장.
        YouTube 는 태그를 대소문자 구분 없이 취급하므로 슬롯 하나를 그냥 버리는 셈이다.
    둘 다 GPT 출력단에서 프롬프트로 막았지만, 여기서 결정적으로 한 번 더 정규화한다.
    태그를 없애는 게 아니라 검색 가능한 형태로 바꾸는 것이라 발행 위험은 없다."""
    out, seen, budget = [], set(), 480
    for t in (tags or []):
        if not isinstance(t, str):
            continue
        t = re.sub(r"_+", " ", t)          # long_tail → long tail (검색되는 형태로)
        t = re.sub(r"\s+", " ", t).strip()[:100]
        if not t:
            continue
        key = t.lower()
        if key in seen:                    # 대소문자만 다른 중복 → 슬롯 낭비이므로 제거
            continue
        cost = len(t) + (2 if " " in t else 0)
        if out:
            cost += 1  # 태그 구분 쉼표도 500자 한도에 포함(codex — YouTube 명세)
        if cost > budget:
            continue
        out.append(t)
        seen.add(key)
        budget -= cost
        if len(out) >= 30:
            break
    return out


def upload(video_path: str, title: str, description: str, tags: list,
           thumbnail_path: str = None, localizations: dict = None,
           default_language: str = "en", custom_comment: str = None) -> str:
    from googleapiclient.http import MediaFileUpload

    svc = get_service()
    _verify_channel(svc)
    snippet = {
        "title": title[:95],
        "description": description[:4900],
        "tags": _sanitize_tags(tags),
        "categoryId": YOUTUBE_CATEGORY_ID,
    }
    if localizations:
        snippet["defaultLanguage"] = default_language
        snippet["defaultAudioLanguage"] = default_language

    body = {
        "snippet": snippet,
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": CONTAINS_SYNTHETIC_MEDIA,
        },
    }
    parts = "snippet,status"
    if localizations:
        body["localizations"] = localizations
        parts = "snippet,status,localizations"

    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    log.info(f"upload start: {os.path.basename(video_path)} locales={len(localizations or {})}")
    try:
        response = svc.videos().insert(part=parts, body=body, media_body=media).execute()
    except Exception as e:
        if localizations and ("insufficientPermissions" in str(e) or "forbidden" in str(e).lower()):
            log.warning(f"locales 권한 부족 → 영어 단일로 재업로드: {e}")
            body.pop("localizations", None)
            snippet.pop("defaultLanguage", None)
            snippet.pop("defaultAudioLanguage", None)
            media2 = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
            response = svc.videos().insert(part="snippet,status", body=body, media_body=media2).execute()
        else:
            raise

    video_id = response["id"]
    url = f"https://youtube.com/shorts/{video_id}"
    log.info(f"upload done: {url}")

    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            tn = MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
            svc.thumbnails().set(videoId=video_id, media_body=tn).execute()
            log.info("썸네일 업로드 완료")
        except Exception as e:
            log.warning(f"thumbnail 실패: {e}")

    if localizations:
        applied = response.get("localizations") or {}
        if len(applied) < len(localizations):
            try:
                svc.videos().update(
                    part="localizations",
                    body={"id": video_id, "localizations": localizations},
                ).execute()
                log.info(f"localizations update: {len(localizations)}")
            except Exception as e:
                log.warning(f"localizations update 실패 (영상은 정상): {e}")

    # 업로드 후 자동 댓글 (동물별 맞춤 우선, 없으면 generic fallback)
    post_auto_comment(svc, video_id, text=custom_comment)

    return url
