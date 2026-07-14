"""3-tier AI 이미지 폴백 — Pexels 영상 부족분을 정적 이미지로 채우기.

호출 우선순위:
1. codex-image (Codex CLI, ChatGPT OAuth, 최대 5장 병렬, 별도 API 키 X)
2. gpt-image2 (OpenAI gpt-image-2, OPENAI_API_KEY 필요)
3. gemini-3-pro-imagegen (Google Gemini Nano Banana, GEMINI_API_KEY 필요)

각 tier는 실패/미설정 시 자동으로 다음 tier로 넘어감. 모두 실패하면 빈 리스트 반환
(호출 측에서 RuntimeError 처리).

전제:
- codex CLI: 로컬에서 ChatGPT 로그인 됐다고 가정 (codex login). GitHub Actions 환경에서는
  보통 미설치 → 자동 skip → 다음 tier로.
- OPENAI_API_KEY / GEMINI_API_KEY: 환경변수에 있어야 활성화. 없으면 자동 skip.

생성된 PNG는 다음 단계 image_to_video.py에서 9:16 1080x1920 Ken Burns mp4로 변환.

만들어진 이유: 2026-05-08 우주봇 OAuth 만료 사고 + asteroid/exoplanet placeholder
사고 디버깅 중 발견 — Pexels 우주 영상 커버리지 부족(Uranus 0/6, Mars 2/6 등)으로
토픽 패스 빈발. AI 이미지 폴백으로 영상 발행 안정화.
"""
import logging
import os
import shutil
import subprocess
import sys
from typing import List

log = logging.getLogger("spacebot.image_fallback")


_SPACE_CATEGORIES = {
    "planet", "star", "galaxy", "moon", "phenomenon",
    "spacecraft", "object", "constellation", "mission",
}
_ANIMAL_CATEGORIES = {
    "mammal", "bird", "marine", "reptile", "amphibian", "insect",
    "fish", "arachnid", "crustacean", "cephalopod",
}


def _build_prompts(meta: dict, n: int) -> List[str]:
    """토픽 메타 → 9:16 portrait 영상화 적합한 프롬프트 N개. 우주/동물 자동 분기."""
    display = meta.get("display", "subject")
    single = meta.get("single", display)
    category = meta.get("category", "phenomenon").lower()

    if category in _SPACE_CATEGORIES:
        domain_hint = "outer space cosmos astronomy"
        styles = [
            f"hyperrealistic photograph of {single}, {domain_hint}, dramatic lighting, deep cosmic colors",
            f"cinematic close-up of {single}, {domain_hint}, ultra-detailed, NASA quality, 8k",
            f"wide angle view of {single}, {domain_hint}, awe-inspiring scale, photorealistic",
            f"artistic rendering of {single}, {domain_hint}, vibrant nebula colors, deep space photography",
            f"telescope photograph of {single}, {domain_hint}, sharp focus, high contrast",
        ]
        suffix_extra = "edge to edge cosmic scenery"
    elif category in _ANIMAL_CATEGORIES:
        domain_hint = "natural wildlife habitat"
        styles = [
            f"hyperrealistic wildlife photograph of {single}, {domain_hint}, golden hour lighting, sharp focus",
            f"cinematic close-up portrait of {single}, {domain_hint}, ultra-detailed fur or scales, 8k",
            f"wide angle environmental shot of {single}, {domain_hint}, in its natural environment, photorealistic",
            f"national geographic style photograph of {single}, {domain_hint}, vivid colors, depth of field",
            f"documentary-style image of {single}, {domain_hint}, sharp focus, high contrast, natural pose",
        ]
        suffix_extra = "edge to edge natural scenery"
    else:
        domain_hint = "high-quality stock photograph"
        styles = [
            f"hyperrealistic photograph of {single}, {domain_hint}, dramatic lighting, vivid colors",
            f"cinematic close-up of {single}, {domain_hint}, ultra-detailed, 8k",
            f"wide angle view of {single}, {domain_hint}, photorealistic",
            f"artistic rendering of {single}, {domain_hint}, vibrant colors",
            f"editorial photograph of {single}, {domain_hint}, sharp focus, high contrast",
        ]
        suffix_extra = "edge to edge full-bleed scenery"

    portrait_suffix = (
        f" 9:16 vertical portrait orientation, no text, no captions, no watermark, "
        f"no logos, full bleed, {suffix_extra}"
    )
    return [s + "," + portrait_suffix for s in styles[:n]]


def _try_codex(prompts: List[str], output_dir: str) -> List[str]:
    """tier 1: Codex CLI 병렬 이미지 생성. ChatGPT OAuth 필요."""
    if not shutil.which("codex"):
        log.info("codex CLI 없음 → tier 1 skip")
        return []

    try:
        rc = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if rc.returncode != 0 or "Logged in" not in (rc.stdout + rc.stderr):
            log.info(f"codex 미로그인 → tier 1 skip (status={rc.stdout.strip()})")
            return []
    except Exception as e:
        log.info(f"codex login status 실패 → tier 1 skip: {e}")
        return []

    os.makedirs(output_dir, exist_ok=True)
    procs = []
    out_paths = []
    log_dir = os.path.join(output_dir, ".codex-imagegen-logs")
    os.makedirs(log_dir, exist_ok=True)

    for i, prompt in enumerate(prompts):
        out_name = f"ai_codex_{i+1}.png"
        out_path = os.path.join(output_dir, out_name)
        log_path = os.path.join(log_dir, f"codex-{i+1}.md")
        cmd = [
            "codex", "exec",
            "--sandbox", "workspace-write",
            "--skip-git-repo-check",
            "--cd", output_dir,
            "-o", log_path,
            f"이미지 생성 도구로 '{prompt}' 이미지를 생성하고 ./{out_name} 로 저장. 파일 경로만 한 줄로 보고.",
        ]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append((p, out_path))
        except Exception as e:
            log.warning(f"codex tier 1 spawn 실패 #{i+1}: {e}")

    timeout = 300
    for p, out_path in procs:
        try:
            p.wait(timeout=timeout)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            out_paths.append(out_path)

    log.info(f"tier 1 codex-image: {len(out_paths)}/{len(prompts)} 생성")
    return out_paths


def _try_gpt_image2(prompts: List[str], output_dir: str) -> List[str]:
    """tier 2: OpenAI gpt-image-2. OPENAI_API_KEY 필요."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.info("OPENAI_API_KEY 없음 → tier 2 skip")
        return []

    try:
        from openai import OpenAI
    except ImportError:
        log.warning("openai SDK 미설치 → tier 2 skip")
        return []

    os.makedirs(output_dir, exist_ok=True)
    out_paths = []
    client = OpenAI(api_key=api_key)
    import base64

    for i, prompt in enumerate(prompts):
        out_name = f"ai_gpt_{i+1}.png"
        out_path = os.path.join(output_dir, out_name)
        try:
            result = client.images.generate(
                model="gpt-image-2",
                prompt=prompt,
                size="1024x1536",  # 2:3 portrait — 9:16에 가까움
                quality="high",
            )
            b64 = result.data[0].b64_json
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
            if os.path.getsize(out_path) > 1024:
                out_paths.append(out_path)
        except Exception as e:
            log.warning(f"tier 2 gpt-image-2 #{i+1} 실패: {e}")

    log.info(f"tier 2 gpt-image-2: {len(out_paths)}/{len(prompts)} 생성")
    return out_paths


def _try_gemini(prompts: List[str], output_dir: str) -> List[str]:
    """tier 3: Google Gemini Nano Banana. GEMINI_API_KEY 또는 GOOGLE_API_KEY 필요."""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        log.info("GEMINI_API_KEY 없음 → tier 3 skip")
        return []

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        log.warning("google-genai SDK 미설치 → tier 3 skip")
        return []

    os.makedirs(output_dir, exist_ok=True)
    out_paths = []
    client = genai.Client(api_key=api_key)

    for i, prompt in enumerate(prompts):
        out_name = f"ai_gemini_{i+1}.png"
        out_path = os.path.join(output_dir, out_name)
        try:
            resp = client.models.generate_content(
                model="gemini-3.1-flash-image-preview",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                    image_config=types.ImageConfig(
                        aspect_ratio="9:16",
                        image_size="2K",
                    ),
                ),
            )
            saved = False
            for part in resp.parts:
                img = getattr(part, "as_image", lambda: None)()
                if img is not None:
                    img.save(out_path)
                    saved = True
                    break
            if saved and os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                out_paths.append(out_path)
        except Exception as e:
            log.warning(f"tier 3 gemini #{i+1} 실패: {e}")

    log.info(f"tier 3 gemini: {len(out_paths)}/{len(prompts)} 생성")
    return out_paths


def generate_fallback_images(meta: dict, n_needed: int, output_dir: str) -> List[str]:
    """3-tier 폴백 — 부족분 N장 PNG 생성. 성공한 PNG 경로 리스트 반환.

    한 tier에서 N장 다 생기면 즉시 리턴. 부족하면 다음 tier 추가.
    모두 실패하면 빈 리스트 (호출 측 RuntimeError).
    """
    if n_needed <= 0:
        return []

    prompts = _build_prompts(meta, n_needed)
    log.info(f"image fallback: {n_needed}장 필요, prompts={len(prompts)}")

    collected: List[str] = []

    # 2026-05-08 쿠마님 결정 v2 — Pexels 다음 순위: gemini(1) → gpt-image2(2) → codex(3)
    for tier_name, tier_fn in [
        ("gemini", _try_gemini),
        ("gpt-image2", _try_gpt_image2),
        ("codex", _try_codex),
    ]:
        if len(collected) >= n_needed:
            break
        remaining = n_needed - len(collected)
        try:
            new_paths = tier_fn(prompts[len(collected):len(collected) + remaining], output_dir)
        except Exception as e:
            log.warning(f"tier '{tier_name}' 호출 자체 예외: {e}")
            new_paths = []
        collected.extend(new_paths)
        log.info(f"누적 {len(collected)}/{n_needed} (tier {tier_name} 후)")

    return collected[:n_needed]
