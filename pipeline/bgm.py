"""음악쇼츠봇 Google Drive의 수노(Suno) 음원 풀 전체에서 BGM 1개 read-only 다운로드.

전략 (2026-07-17 쿠마님 지시로 재설계):
- /MusicShorts/queue/ 하위 **모든 테마 폴더**의 mp3 전체 풀(~190곡)에서 랜덤 픽.
  (기존 카테고리→무드 매핑은 7~10곡짜리 폴더 4개만 써서 money/country/space 등
   38~40곡 폴더를 통째로 놓쳤음 — "아무거나, 대신 겹치지 않게"로 단순화)
- 중복 방지: used_bgm.json에 사용한 파일 id를 쌓고 제외. 풀을 한 바퀴 다 돌아
  소진됐을 때만 이력을 풀어 재사용 허용(직전 10곡은 그때도 계속 제외).
- read-only: 파일을 used로 옮기지 않음 → 음악쇼츠봇 발행에 영향 없음.
- 사용 "기록"은 호출 측(main.py)이 발행 성공 시에만 mark_bgm_used()로 남긴다
  (--no-upload/dry-run 상태 오염 방지 원칙과 동일).
"""
import io
import json
import logging
import os
import pickle
import random
from typing import Optional, Tuple

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from config import CREDENTIALS_DIR

log = logging.getLogger("travelbot.bgm")

# 음악쇼츠봇 토큰 재활용 (같은 Google 계정의 Drive)
_OAUTH_TOKEN = os.path.join(CREDENTIALS_DIR, "gdrive_oauth_token.json")

GDRIVE_ROOT = "MusicShorts"
GDRIVE_QUEUE = "queue"

# 사용 이력 — repo 루트(used_cities.json과 나란히), 워크플로 persist 대상.
USED_BGM_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "used_bgm.json")
USED_BGM_KEEP = 500      # 사실상 풀 1회전 전체를 기억 (풀 ~190곡, 여유 포함)
RESET_RECENT_KEEP = 10   # 풀 소진 리셋 직후에도 직전 N곡은 계속 제외 (연속 반복 방지)


def _load_used_bgm() -> list:
    if not os.path.exists(USED_BGM_PATH):
        return []
    try:
        return [str(i) for i in json.load(open(USED_BGM_PATH, encoding="utf-8"))]
    except Exception:
        return []


def mark_bgm_used(file_id: str):
    """발행 성공 시 호출 — 사용한 BGM 파일 id를 이력에 기록."""
    if not file_id:
        return
    used = _load_used_bgm()
    used.append(str(file_id))
    json.dump(used[-USED_BGM_KEEP:], open(USED_BGM_PATH, "w", encoding="utf-8"))


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


def fetch_bgm_random(output_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """queue 하위 모든 테마 폴더의 수노 음원 전체 풀에서 미사용곡 1개 랜덤 다운로드.

    반환: (local_path, file_id). 실패 시 (None, None) — composer가 BGM 없이 진행.
    사용 기록은 여기서 하지 않는다 — 발행 성공 시 main.py가 mark_bgm_used(file_id).
    """
    try:
        # _get_service()는 만료/폐지/포맷 불일치 토큰에서 예외(RefreshError/UnpicklingError)를
        # 던질 수 있다 — BGM은 부가요소이므로 반드시 try 안에서 호출해 런 전체 크래시를 막는다.
        svc = _get_service()
        if svc is None:
            return None, None

        root_id = _find_folder(svc, GDRIVE_ROOT)
        if not root_id:
            log.warning(f"Drive '{GDRIVE_ROOT}' 폴더 없음")
            return None, None
        queue_id = _find_folder(svc, GDRIVE_QUEUE, root_id)
        if not queue_id:
            log.warning("queue 서브폴더 없음")
            return None, None

        # queue 하위 모든 테마 폴더 → 전체 mp3 풀 수집
        r = svc.files().list(
            q=f"'{queue_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id,name)", pageSize=100).execute()
        theme_folders = r.get("files", [])
        pool = []
        for tf in theme_folders:
            for f in _list_mp3(svc, tf["id"]):
                pool.append({"id": f["id"], "name": f["name"], "theme": tf["name"]})
        if not pool:
            log.warning("queue 전체에 mp3 없음 → BGM 없이")
            return None, None

        # 중복 방지: 사용 이력 제외 → 풀 소진 시에만 이력 해제(직전 N곡은 유지)
        used = _load_used_bgm()
        used_set = set(used)
        fresh = [f for f in pool if f["id"] not in used_set]
        if not fresh:
            recent = set(used[-RESET_RECENT_KEEP:])
            fresh = [f for f in pool if f["id"] not in recent] or pool
            log.info(f"BGM 풀 1회전 소진({len(pool)}곡) → 이력 해제 재사용 (직전 {RESET_RECENT_KEEP}곡 제외)")

        pick = random.choice(fresh)
        os.makedirs(output_dir, exist_ok=True)
        local_path = os.path.join(output_dir, f"bgm_{pick['theme']}_{pick['name']}")
        req = svc.files().get_media(fileId=pick["id"])
        with io.FileIO(local_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, req)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        log.info(f"bgm downloaded: theme={pick['theme']} name={pick['name']} "
                 f"(풀 {len(pool)}곡, 미사용 {len(fresh)}곡 중 픽)")
        return local_path, pick["id"]
    except Exception as e:
        log.warning(f"BGM 다운로드 실패 → BGM 없이: {e}")
        return None, None


def fetch_bgm_by_category(category: str, output_dir: str) -> Optional[str]:
    """하위 호환 래퍼 — 카테고리 매핑은 폐지(2026-07-17), 전체 풀 랜덤으로 위임."""
    path, _ = fetch_bgm_random(output_dir)
    return path
