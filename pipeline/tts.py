"""edge-tts 영문 내레이션 + WordBoundary 단어 타임스탬프.

각 라인별 mp3 + duration + word_cues (시간차 자막에 사용).

2026-07-17 fix:
- edge-tts 7.x는 boundary 기본값이 SentenceBoundary라 WordBoundary 이벤트가 아예 오지 않아
  word_cues가 항상 빈 리스트였다(단어 하이라이트·자막 싱크 소실). boundary="WordBoundary" 명시.
  (edge-tts 6.x는 boundary 인자가 없어 TypeError → 인자 없이 폴백. 6.x는 기본이 word boundary.)
- 일시적 네트워크/NoAudioReceived 실패 1회로 런 전체가 죽던 문제 → 라인당 3회 backoff 재시도.
"""
import asyncio
import logging
import os
import time

from config import TTS_PITCH, TTS_RATE, TTS_VOICE

log = logging.getLogger("travelbot.tts")


async def _synth_with_words(text: str, output_path: str):
    """텍스트 → mp3 저장 + word_cues [{text, start, end}] 반환.

    edge-tts WordBoundary 이벤트의 offset/duration은 100ns 단위.
    """
    import edge_tts
    word_cues = []
    try:
        communicate = edge_tts.Communicate(
            text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH, boundary="WordBoundary")
    except TypeError:
        # edge-tts 6.x: boundary 인자 없음 (기본이 word boundary라 그대로 정상)
        communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    with open(output_path, "wb") as f:
        async for chunk in communicate.stream():
            ctype = chunk.get("type")
            if ctype == "audio":
                f.write(chunk["data"])
            elif ctype == "WordBoundary":
                offset_s = chunk["offset"] / 10_000_000.0
                dur_s = chunk["duration"] / 10_000_000.0
                word_cues.append({
                    "text": chunk["text"],
                    "start": offset_s,
                    "end": offset_s + dur_s,
                })
    return word_cues


def synth(text: str, output_path: str, retries: int = 3) -> list:
    """1라인 합성. 무료 MS 엔드포인트의 간헐 실패(NoAudioReceived/WebSocket 리셋)에
    backoff 재시도 — 실패 1회로 이미 과금된 대본·미디어를 통째로 버리지 않는다."""
    last = None
    for attempt in range(retries):
        try:
            cues = asyncio.run(_synth_with_words(text, output_path))
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return cues
            raise RuntimeError("empty audio file")
        except Exception as e:
            last = e
            if attempt < retries - 1:
                wait = [2, 6][min(attempt, 1)]
                log.warning(f"tts 실패 → {wait}s 후 재시도 ({attempt + 1}/{retries}): {e}")
                time.sleep(wait)
    raise RuntimeError(f"tts {retries}회 모두 실패: {last}")


def get_duration(audio_path: str) -> float:
    from moviepy.editor import AudioFileClip
    with AudioFileClip(audio_path) as a:
        return float(a.duration)


def synth_lines(lines: list, output_dir: str, prefix: str = "tts") -> list:
    """각 라인별 mp3 + duration + word_cues 반환.

    [{"text", "path", "duration", "words": [{text, start, end}, ...]}, ...]
    """
    os.makedirs(output_dir, exist_ok=True)
    out = []
    for i, text in enumerate(lines):
        path = os.path.join(output_dir, f"{prefix}_{i:02d}.mp3")
        word_cues = synth(text, path)
        dur = get_duration(path)
        out.append({"text": text, "path": path, "duration": dur, "words": word_cues})
        log.info(f"tts[{i}] {dur:.2f}s words={len(word_cues)} — {text[:60]}")
    return out
