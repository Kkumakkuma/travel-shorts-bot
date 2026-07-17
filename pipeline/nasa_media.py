"""NASA Images and Video Library — 우주 토픽 배경 영상/이미지 소스 (우주봇 전용).

API 키 불필요 (퍼블릭 도메인). https://images-api.nasa.gov/search

용도: Uranus/Neptune처럼 Pexels 커버리지 0인 우주 토픽이 살아나도록.
우선순위(main.py에서): NASA 영상 → Pexels 영상 → NASA 이미지 Ken Burns → (기존 AI 폴백).
모든 소스에 기존 vision 검증(fail-closed) 그대로 적용된다.

동작:
- search(q, media_type): collection.items에서 nasa_id + asset manifest(href) 추출.
- 각 item의 collection.json(자산 매니페스트)에서 mp4(~mobile/~orig) 또는 이미지 URL 추출·다운로드.
- 다운로드 실패/timeout/과대용량은 안전 skip.
"""
import json
import logging
import os

import requests

log = logging.getLogger("travelbot.nasa")

_SEARCH_API = "https://images-api.nasa.gov/search"
_UA = "SpaceTriviaBot/1.0 (educational; contact via youtube)"
_HEADERS = {"User-Agent": _UA}

# 다운로드 안전 상한 (Actions timeout 회피). NASA ~orig mp4는 수백 MB일 수 있어 mobile 우선.
_MAX_VIDEO_BYTES = 90 * 1024 * 1024   # 90MB
_MAX_IMAGE_BYTES = 25 * 1024 * 1024   # 25MB


def search(query: str, media_type: str = "video", page_size: int = 12,
           timeout: float = 12.0) -> list:
    """NASA 라이브러리 검색 → [{nasa_id, title, manifest}] 리스트. 실패 시 []."""
    try:
        r = requests.get(
            _SEARCH_API,
            params={"q": query, "media_type": media_type, "page_size": page_size},
            headers=_HEADERS, timeout=timeout,
        )
        r.raise_for_status()
        items = r.json().get("collection", {}).get("items", []) or []
    except Exception as e:
        log.warning(f"NASA search 실패 ({query!r}, {media_type}): {e}")
        return []

    out = []
    for it in items:
        data = (it.get("data") or [{}])[0]
        nasa_id = data.get("nasa_id") or ""
        manifest = it.get("href") or ""  # collection.json 자산 매니페스트
        if nasa_id and manifest:
            out.append({
                "nasa_id": nasa_id,
                "title": data.get("title", "") or "",
                "manifest": manifest,
            })
    return out


def _asset_urls(manifest_url: str, timeout: float = 10.0) -> list:
    """collection.json(자산 매니페스트) → 자산 URL 리스트. 실패 시 []."""
    try:
        r = requests.get(manifest_url, headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return [u for u in data if isinstance(u, str)]
    except Exception as e:
        log.warning(f"NASA manifest 실패 ({manifest_url}): {e}")
    return []


def _pick_video_urls(urls: list) -> list:
    """다운로드용 mp4 URL 후보를 **순서대로** 반환 (작은 것 우선, orig 마지막).

    단일 반환이면 orig가 용량 초과일 때 통째 skip됨 → 순서 리스트로 성공할 때까지 시도.
    """
    mp4s = [u for u in urls if u.lower().endswith(".mp4")]
    order = ["~mobile.mp4", "mobile.mp4", "~small.mp4", "~medium.mp4", "~large.mp4"]

    def rank(u):
        low = u.lower()
        for i, tag in enumerate(order):
            if low.endswith(tag):
                return i
        if "~orig.mp4" in low:
            return 90  # orig(대용량)은 맨 마지막
        return 50

    return sorted(mp4s, key=rank)


def _pick_image_urls(urls: list) -> list:
    """이미지 URL 후보를 **순서대로** 반환 (large/medium 우선, orig 마지막)."""
    imgs = [u for u in urls if u.lower().endswith((".jpg", ".jpeg", ".png"))]
    order = ["~large.", "~medium.", "~small.", "~thumb."]

    def rank(u):
        low = u.lower()
        for i, tag in enumerate(order):
            if tag in low:
                return i
        if "~orig." in low:
            return 90
        return 50

    return sorted(imgs, key=rank)


def _download(url: str, out_path: str, max_bytes: int, timeout: float = 60.0) -> bool:
    """스트리밍 다운로드 + 용량 상한. 성공 True. 실패/초과 시 False + 파일 제거."""
    try:
        with requests.get(url, headers=_HEADERS, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            clen = resp.headers.get("Content-Length")
            if clen and int(clen) > max_bytes:
                log.warning(f"NASA asset 용량 초과 skip ({int(clen)//1024//1024}MB): {url}")
                return False
            total = 0
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        log.warning(f"NASA asset 용량 초과 중단 skip: {url}")
                        raise RuntimeError("max_bytes exceeded")
                    fh.write(chunk)
        return True
    except Exception as e:
        log.warning(f"NASA 다운로드 실패 ({url}): {e}")
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return False


def fetch_video(query: str, output_dir: str, exclude_ids: set = None,
                max_items: int = 6):
    """query로 NASA 영상 1개 다운로드 → (mp4_path, nasa_id). 실패 시 (None, None).

    각 item의 mp4 후보를 작은 것부터 시도(orig 용량 초과 시 다음 후보).
    """
    exclude_ids = {str(i) for i in (exclude_ids or set())}
    os.makedirs(output_dir, exist_ok=True)
    for item in search(query, media_type="video", page_size=max_items * 2):
        nid = item["nasa_id"]
        if nid in exclude_ids:
            continue
        urls = _asset_urls(item["manifest"])
        safe_id = "".join(c if c.isalnum() else "_" for c in nid)[:40]
        out = os.path.join(output_dir, f"nasa_{safe_id}.mp4")
        for vurl in _pick_video_urls(urls):
            if _download(vurl, out, _MAX_VIDEO_BYTES):
                log.info(f"NASA video: q={query!r} id={nid}")
                return out, nid
    return None, None


def fetch_image(query: str, output_dir: str, exclude_ids: set = None,
                max_items: int = 8):
    """query로 NASA 이미지 1개 → (png_path, nasa_id). 실패 시 (None, None).

    반환 경로는 .png (image_to_video.png_to_video 호환 위해 PIL로 변환 저장).
    각 item의 이미지 후보를 large부터 시도(용량 초과 시 다음 후보).
    """
    exclude_ids = {str(i) for i in (exclude_ids or set())}
    os.makedirs(output_dir, exist_ok=True)
    for item in search(query, media_type="image", page_size=max_items * 2):
        nid = item["nasa_id"]
        if nid in exclude_ids:
            continue
        urls = _asset_urls(item["manifest"])
        safe_id = "".join(c if c.isalnum() else "_" for c in nid)[:40]
        raw = os.path.join(output_dir, f"nasa_img_{safe_id}_raw")
        downloaded = False
        for iurl in _pick_image_urls(urls):
            if _download(iurl, raw, _MAX_IMAGE_BYTES):
                downloaded = True
                break
        if not downloaded:
            continue
        try:
            from PIL import Image
            png_out = os.path.join(output_dir, f"nasa_img_{safe_id}.png")
            Image.open(raw).convert("RGB").save(png_out)
            try:
                os.remove(raw)
            except Exception:
                pass
            log.info(f"NASA image: q={query!r} id={nid}")
            return png_out, nid
        except Exception as e:
            log.warning(f"NASA 이미지 변환 실패 ({nid}): {e}")
            try:
                os.remove(raw)
            except Exception:
                pass
    return None, None
