"""음악쇼츠봇 Google Drive에서 여행 카테고리에 어울리는 BGM 1개 read-only 다운로드.

전략:
- 음악쇼츠봇이 운영하는 /MusicShorts/queue/<music_theme>/ 폴더에서 mp3 1개 랜덤 픽
- read-only: used로 옮기지 않음 → 음악쇼츠봇 발행에 영향 없음
- 여행 카테고리(대륙/테마)별로 어울리는 음악 무드 (comfort/study/dawn/sleep 등) 매핑
"""
import io
import logging
import os
import pickle
import random
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from config import CREDENTIALS_DIR

log = logging.getLogger("travelbot.bgm")

# 음악쇼츠봇 토큰 재활용 (같은 Google 계정의 Drive)
_OAUTH_TOKEN = os.path.join(CREDENTIALS_DIR, "gdrive_oauth_token.json")

GDRIVE_ROOT = "MusicShorts"
GDRIVE_QUEUE = "queue"

# 카테고리(여행 대륙/테마) → 음악 테마(무드) 매핑.
# 음악쇼츠 GDrive queue/<theme>/ 에 있는 무드 폴더로 라우팅. 여행 전용 폴더가 없을 수 있어
# 범용 lo-fi 무드(comfort/study/dawn/sleep)로 매핑하고, 없으면 DEFAULT_MUSIC_THEMES로 폴백.
CATEGORY_TO_MUSIC = {
    "europe_city": ["study", "comfort"],
    "asia_culture": ["comfort", "study"],
    "beach_tropical": ["dawn", "comfort"],
    "desert_middleeast": ["comfort", "study"],
    "africa_safari": ["comfort", "study"],
    "latin_america": ["dawn", "comfort"],
    "north_america": ["study", "comfort"],
    "oceania": ["dawn", "comfort"],
    "nordic_nature": ["sleep", "comfort"],
    "historic_ancient": ["study", "comfort"],
}
DEFAULT_MUSIC_THEMES = ["comfort", "study", "dawn", "sleep"]  # 매핑 테마 비어있으면 범용 lo-fi로 폴백


def _get_service():
    """음악쇼츠봇과 동일한 OAuth 토큰으로 Drive API 인증."""
    if not os.path.exists(_OAUTH_TOKEN):
        log.warning(f"GDrive OAuth 토큰 없음: {_OAUTH_TOKEN} → BGM 스킵")
        return None
    with open(_OAUTH_TOKEN, "rb") as f:
        creds = pickle.load(f)
    if not creds.valid and creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(_OAUTH_TOKEN, "wb") as f:
            pickle.dump(creds, f)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_folder(svc, name: str, parent_id: Optional[str] = None) -> Optional[str]:
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    r = svc.files().list(q=q, fields="files(id,name)", pageSize=10).execute()
    files = r.get("files", [])
    return files[0]["id"] if files else None


def _list_mp3(svc, folder_id: str) -> list:
    q = (f"'{folder_id}' in parents and trashed=false "
         f"and (mimeType='audio/mpeg' or name contains '.mp3')")
    r = svc.files().list(q=q, fields="files(id,name)", pageSize=200).execute()
    return r.get("files", [])


def fetch_bgm_by_category(category: str, output_dir: str) -> Optional[str]:
    """여행 카테고리에 어울리는 음악 무드에서 mp3 1개 read-only 다운로드.

    실패 시 None (composer가 BGM 없이 진행).
    """
    try:
        # _get_service()는 만료/폐지/포맷 불일치 토큰에서 예외(RefreshError/UnpicklingError)를
        # 던질 수 있다 — BGM은 부가요소이므로 반드시 try 안에서 호출해 런 전체 크래시를 막는다.
        svc = _get_service()
        if svc is None:
            return None

        music_themes = CATEGORY_TO_MUSIC.get((category or "").lower(), DEFAULT_MUSIC_THEMES)
        chosen_theme = random.choice(music_themes)
        log.info(f"bgm theme: category={category} → {chosen_theme}")

        root_id = _find_folder(svc, GDRIVE_ROOT)
        if not root_id:
            log.warning(f"Drive '{GDRIVE_ROOT}' 폴더 없음")
            return None
        queue_id = _find_folder(svc, GDRIVE_QUEUE, root_id)
        if not queue_id:
            log.warning("queue 서브폴더 없음")
            return None

        # 첫 매핑 테마에서 못 찾으면 다른 후보로 fallback
        for theme_try in [chosen_theme] + [t for t in music_themes if t != chosen_theme] + DEFAULT_MUSIC_THEMES:
            tid = _find_folder(svc, theme_try, queue_id)
            if not tid:
                continue
            files = _list_mp3(svc, tid)
            if not files:
                continue
            pick = random.choice(files)
            os.makedirs(output_dir, exist_ok=True)
            local_path = os.path.join(output_dir, f"bgm_{theme_try}_{pick['name']}")
            req = svc.files().get_media(fileId=pick["id"])
            with io.FileIO(local_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, req)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            log.info(f"bgm downloaded: theme={theme_try} name={pick['name']}")
            return local_path
        log.warning("모든 음악 테마 큐 비어있음 → BGM 없이")
        return None
    except Exception as e:
        log.warning(f"BGM 다운로드 실패 → BGM 없이: {e}")
        return None
