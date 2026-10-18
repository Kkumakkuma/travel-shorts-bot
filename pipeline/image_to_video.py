"""정적 PNG → 9:16 1080x1920 Ken Burns 영상 변환.

image_fallback.py에서 받은 PNG를 composer.py가 처리할 수 있는 mp4로 변환.
줌인+팬 애니메이션으로 정적 이미지에 동적 느낌 부여.

만들어진 이유: AI 이미지 생성 결과(PNG)를 기존 Pexels 영상 파이프라인에 끼워넣기 위해.
composer.compose는 mp4 경로 리스트를 받으므로 형식 통일 필요.
"""
import logging
import os
import random

log = logging.getLogger("travelbot.image_to_video")


def png_to_video(png_path: str, output_mp4: str, duration: float = 10.0,
                 width: int = 1080, height: int = 1920,
                 fps: int = 30) -> str:
    """PNG 1장 → 9:16 mp4 (Ken Burns 줌인 + 미세 팬).

    duration: 출력 영상 길이 (초). composer가 concatenate하므로 ~10초가 적절.
    width/height: 9:16 portrait. 1080x1920이 표준.

    실패 시 RuntimeError raise.
    """
    if not os.path.exists(png_path):
        raise RuntimeError(f"PNG 입력 없음: {png_path}")

    from PIL import Image
    if not hasattr(Image, "ANTIALIAS"):
        Image.ANTIALIAS = Image.LANCZOS

    from moviepy.editor import ImageClip, concatenate_videoclips

    img = Image.open(png_path).convert("RGB")
    src_w, src_h = img.size
    target_ratio = width / height
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        x_off = (src_w - new_w) // 2
        img = img.crop((x_off, 0, x_off + new_w, src_h))
    elif src_ratio < target_ratio:
        new_h = int(src_w / target_ratio)
        y_off = (src_h - new_h) // 2
        img = img.crop((0, y_off, src_w, y_off + new_h))
    img = img.resize((width, height), Image.LANCZOS)

    # 2026-10-18: 임시 프레임을 원본 옆(.png 치환)이 아니라 출력 옆에 만든다 — .jpg 원본은 치환이 안 돼
    # 원본을 덮어쓰고 마지막에 지워 버리던 버그(codex 검토).
    os.makedirs(os.path.dirname(output_mp4) or ".", exist_ok=True)
    tmp_resized = os.path.splitext(output_mp4)[0] + "_frame.png"
    img.save(tmp_resized)

    clip = final = None
    try:
        clip = ImageClip(tmp_resized).set_duration(duration)

        start_zoom = random.uniform(1.04, 1.08)
        end_zoom = random.uniform(1.18, 1.25)

        def zoom_factor(t):
            return start_zoom + (end_zoom - start_zoom) * (t / duration)

        clip = clip.resize(zoom_factor)

        pan_dir = random.choice(["lr", "rl", "tb", "bt", "center"])

        def pos_func(t):
            progress = t / duration
            if pan_dir == "center":
                return ("center", "center")
            if pan_dir == "lr":
                return (-int(20 * progress), "center")
            if pan_dir == "rl":
                return (int(20 * progress), "center")
            if pan_dir == "tb":
                return ("center", -int(40 * progress))
            if pan_dir == "bt":
                return ("center", int(40 * progress))
            return ("center", "center")

        clip = clip.set_position(pos_func)

        from moviepy.editor import CompositeVideoClip, ColorClip
        bg = ColorClip(size=(width, height), color=(0, 0, 0)).set_duration(duration)
        final = CompositeVideoClip([bg, clip], size=(width, height)).set_duration(duration)

        os.makedirs(os.path.dirname(output_mp4) or ".", exist_ok=True)
        final.write_videofile(
            output_mp4,
            fps=fps,
            codec="libx264",
            audio=False,
            preset="ultrafast",
            threads=2,
            verbose=False,
            logger=None,
        )
    finally:
        # 2026-10-18: 실패해도 클립을 닫고 임시 프레임을 지운다(codex 검토)
        for c in (final, clip):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        try:
            os.remove(tmp_resized)
        except Exception:
            pass

    log.info(f"image→video: {os.path.basename(png_path)} → {os.path.basename(output_mp4)}")
    return output_mp4


def pngs_to_videos(png_paths: list, output_dir: str, duration: float = 10.0) -> list:
    """PNG N장 → mp4 N개. 실패한 것은 skip하고 성공만 리턴."""
    out_paths = []
    for i, png in enumerate(png_paths):
        out_mp4 = os.path.join(output_dir, f"ai_clip_{i+1}.mp4")
        try:
            png_to_video(png, out_mp4, duration=duration)
            out_paths.append(out_mp4)
        except Exception as e:
            log.warning(f"image_to_video 실패 ({png}): {e}")
    return out_paths
