"""edge-tts 영문 내레이션 + WordBoundary 단어 타임스탬프.

각 라인별 mp3 + duration + word_cues (시간차 자막에 사용).
"""
import asyncio
import logging
import os

from config import TTS_PITCH, TTS_RATE, TTS_VOICE

log = logging.getLogger("spacebot.tts")


async def _synth_with_words(text: str, output_path: str):
    """텍스트 → mp3 저장 + word_cues [{text, start, end}] 반환.

    edge-tts WordBoundary 이벤트의 offset/duration은 100ns 단위.
    """
    import edge_tts
    word_cues = []
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


def synth(text: str, output_path: str) -> list:
    return asyncio.run(_synth_with_words(text, output_path))


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
