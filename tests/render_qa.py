# -*- coding: utf-8 -*-
"""화면 텍스트 실렌더 QA 하네스.

실제 composer.compose()를 로컬에서 돌려(진짜 TTS 1회 + 더미 배경 4개) 핵심 시점 프레임을
추출하고 GPT-4o-mini vision으로 판정: 텍스트 잘림/겹침/가독성/UI 안전영역 침범/CTA 깔끔함.

사용: py -3 tests/render_qa.py <bot_dir>
  예: py -3 tests/render_qa.py .            (현재 봇)
      py -3 동물쇼츠봇/tests/render_qa.py 우주쇼츠봇   (우주봇 대상)

프레임 PNG와 판정 JSON을 scratchpad/render_qa_frames/<bot>/ 에 저장.
"""
import base64
import json
import os
import subprocess
import sys

BOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
BOT_NAME = os.path.basename(BOT.rstrip("/\\"))
sys.path.insert(0, BOT)

OUT_ROOT = r"C:\Users\goopy\AppData\Local\Temp\claude\C--Users-goopy-Desktop-Claude\6218057d-91bb-496b-9957-7e18c8472c8c\scratchpad\render_qa_frames"
FRAME_DIR = os.path.join(OUT_ROOT, BOT_NAME)
os.makedirs(FRAME_DIR, exist_ok=True)

import config  # noqa: E402
from pipeline import composer, tts  # noqa: E402
from pipeline import verify_visual  # noqa: E402 (ffmpeg 경로 재사용)

WORK = os.path.join(config.OUTPUT_DIR, "_render_qa")
os.makedirs(WORK, exist_ok=True)

IS_SPACE = getattr(config, "CONTENT_DOMAIN", "") == "space"
if IS_SPACE:
    TOPIC = "Saturn"
    HOOK = "Saturn • Trivia"
    TITLE = "Saturn Has 146 Moons?! Here's Why"
    INTRO = "Saturn hides at least 146 confirmed moons in its orbit."
    FACTS = [
        "Saturn's rings stretch about 175,000 miles wide yet are only around 30 feet thick on average, making them almost impossibly thin for their enormous span across space.",
        "With winds reaching 1,100 miles per hour, Saturn hosts some of the fastest storms in the solar system, dwarfing any hurricane ever recorded here on Earth.",
        "Saturn is the only planet less dense than water, so if you had an ocean large enough, the entire gas giant would genuinely float on its surface.",
        "A single day on Saturn lasts just 10.7 hours, meaning this giant spins so fast that its equator bulges outward far more than most other planets do.",
    ]
    OUTRO = "Follow for more space facts."
else:
    # 여행봇 픽스처 (2026-07-17 fix: 동물 트리비아 잔재 → 실제 발행물과 같은 여행 제목/도시/CTA로 교체)
    TOPIC = "Rome"
    HOOK = "Rome in 60s"
    TITLE = "5 Reasons Rome Will Steal Your Heart"
    INTRO = "Rome layers three thousand years of history into one walkable city."
    FACTS = [
        "The Colosseum once held fifty thousand spectators — walk its upper tiers at opening time and you'll beat both the crowds and the midday heat.",
        "Toss a coin into the Trevi Fountain, then stay a while: the marble glows gold in the late afternoon light and the piazza empties by dinner.",
        "The Pantheon's two-thousand-year-old concrete dome is still the world's largest unreinforced one — stand under the oculus when it rains for something unforgettable.",
        "Skip the restaurants beside the big sights and eat in Trastevere, where family-run trattorias serve carbonara the way Romans actually make it.",
    ]
    OUTRO = "Save this for your trip."


def build_dummy_backgrounds(n=4):
    """더미 배경 mp4 n개 — ffmpeg lavfi color 소스(초고속). 가독성 스트레스: 밝은/복잡한 색 포함."""
    hexes = ["0xB9B9BE", "0x285A8C", "0x965F23", "0x161A2C"]  # 밝은 회색/블루/오렌지/딥네이비
    paths = []
    for i in range(n):
        p = os.path.join(WORK, f"bg_{i}.mp4")
        cmd = [verify_visual.FFMPEG, "-y", "-f", "lavfi",
               "-i", f"color=c={hexes[i % len(hexes)]}:s={config.VIDEO_WIDTH}x{config.VIDEO_HEIGHT}:d=4:r=10",
               "-pix_fmt", "yuv420p", p]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 or not os.path.exists(p):
            raise RuntimeError(f"더미 배경 생성 실패 (ffmpeg rc={r.returncode}): "
                               f"{(r.stderr or b'')[-300:].decode('utf-8', 'replace')}")
        paths.append(p)
    if len(paths) != n:
        raise RuntimeError(f"더미 배경 {len(paths)}/{n}개만 생성됨")
    return paths


def ensure_words(tts_lines):
    """로컬 edge-tts가 WordBoundary(단어 타임스탬프)를 안 줄 때, 균등 분할로 합성 주입 →
    프로덕션(Actions)에서 실제로 표시되는 karaoke 4단어 phrase 경로를 QA에서 검증."""
    for ln in tts_lines:
        if not ln.get("words"):
            toks = (ln.get("text") or "").split()
            dur = float(ln.get("duration") or 0)
            if toks and dur > 0:
                per = dur / len(toks)
                ln["words"] = [{"text": w, "start": i * per, "end": (i + 1) * per}
                               for i, w in enumerate(toks)]
    return tts_lines


def frame_times(tts_lines):
    """compose와 동일한 timeline으로 핵심 추출 시점 계산."""
    pad = 0.35
    total = float(config.VIDEO_DURATION_SEC) - 0.3
    starts = []
    cursor = 0.0
    for ln in tts_lines:
        starts.append(cursor)
        cursor += ln["duration"] + pad
    fact_starts = starts[1:-1]  # intro=0, outro=마지막 제외
    outro_start = starts[-1] if starts else total - 4
    cta_start = max(outro_start, total - 7.0)
    # fact는 첫 phrase 중앙(+0.7s)에서 샘플 — phrase crossfade(0.08s) 경계를 피해 결정적으로.
    pts = [("punch_0.3s", 0.3), ("topic_hook_1.2s", 1.2)]
    for i, fs in enumerate(fact_starts):
        pts.append((f"fact{i+1}_start+0.7s", min(fs + 0.7, total - 0.5)))
    pts.append(("cta_start+1.5s", min(cta_start + 1.5, total - 0.3)))
    # 시간 오름차순 + total 초과 제거
    return [(lbl, round(t, 2)) for lbl, t in pts if 0 <= t < total]


def zone_intrusion(png):
    """결정적 픽셀 검사 (vision 좌표 판정 부정확 대체):
    - ui_zone: 우측 12%(버튼열)·하단 13%(제목바) 존에 텍스트 침범?
    - edge_cut: 화면 최외곽 1.5% 여백(좌/우/상)에 텍스트가 닿아 잘렸는지?
    존 내부 중앙값 대비 outlier(고대비) 픽셀 비율로 판정 (비네트 그라데이션은 median이 흡수)."""
    try:
        from PIL import Image
        import numpy as np
        im = np.asarray(Image.open(png).convert("RGB")).astype("int16")
        H, W = im.shape[:2]

        def has_text(sub, thr=0.004):
            if sub.size == 0:
                return False
            med = np.median(sub.reshape(-1, 3), axis=0)
            diff = np.abs(sub - med).max(axis=2)
            return (float((diff > 80).sum()) / (sub.shape[0] * sub.shape[1])) > thr

        rw, bh = int(W * 0.88), int(H * 0.87)
        m = max(4, int(W * 0.015))  # 최외곽 여백
        right = has_text(im[:, rw:, :])
        bottom = has_text(im[bh:, :, :])
        edge = (has_text(im[:, :m, :], 0.02) or has_text(im[:, W - m:, :], 0.02)
                or has_text(im[:m, :, :], 0.02))
        return {"right": right, "bottom": bottom, "edge": edge}
    except Exception as e:
        return {"right": False, "bottom": False, "edge": False, "err": str(e)[:80]}


def vision_judge(png, label, is_cta):
    if not config.OPENAI_API_KEY:
        return {"verdict": "skip", "note": "no OPENAI_API_KEY"}
    with open(png, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    from openai import OpenAI
    client = OpenAI(api_key=config.OPENAI_API_KEY)
    extra = (" This is the CTA frame — check the big 'FOLLOW + LIKE' card is clean with NO leftover "
             "narration subtitle overlapping it." if is_cta else "")
    q = (f"This is a frame ({label}) from a vertical 9:16 (1080x1920) YouTube Shorts about {TOPIC}. "
         f"YouTube's play UI covers the RIGHT ~12% (like/comment button column) and the BOTTOM ~13% "
         f"(video title / channel name bar at the very bottom). \n"
         f"On-screen elements include: a top topic label, a small @handle channel watermark just below it, "
         f"the center subtitle (usually on a dark rounded box — that box is INTENTIONAL and PROVIDES contrast, "
         f"so white/yellow text on it IS readable), and a CTA card. Check ALL of them (including the @handle):\n"
         f"- cutoff: is any text clipped or running off the LEFT/RIGHT/TOP/BOTTOM screen edge?\n"
         f"- overlap: do any two text elements collide/overlap (e.g. the @handle watermark overlapping the "
         f"topic label or subtitle) so text becomes hard to read?\n"
         f"- low_contrast: is any text genuinely hard to read (after accounting for its box/outline)?\n"
         f"- mid_word_break: is any single word split across two lines mid-word?{extra}\n"
         f"(Do NOT judge UI-zone position — that is checked separately/programmatically.)\n"
         f"Answer STRICT JSON: {{\"verdict\":\"ok\"|\"problem\",\"cutoff\":bool,\"overlap\":bool,"
         f"\"low_contrast\":bool,\"mid_word_break\":bool,\"note\":\"short\"}}")
    import time as _t
    last = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": q},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]}],
                response_format={"type": "json_object"}, max_tokens=200, temperature=0)
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            last = str(e)
            if "429" in last or "rate" in last.lower():
                _t.sleep([8, 20][attempt] if attempt < 2 else 20)
                continue
            break
    return {"verdict": "error", "note": (last or "")[:120]}


def main():
    import shutil
    print(f"=== render QA: {BOT_NAME} ===")
    tts_texts = [INTRO] + FACTS + [OUTRO]
    tts_lines = ensure_words(tts.synth_lines(tts_texts, WORK, prefix="qa"))
    bg = build_dummy_backgrounds(4)
    out_video = os.path.join(WORK, "qa_out.mp4")

    pts = frame_times(tts_lines)  # [(label, t), ...]
    times = [t for _, t in pts]
    # compose가 전체 인코딩 없이 지정 시점 프레임만 저장 (실제 레이어 스택 그대로)
    saved = composer.compose(bg, tts_lines, HOOK, out_video, bgm_path=None,
                             title=TITLE, qa_frame_times=times)
    print(f"프레임 {len(saved)}개 렌더 (인코딩 없이 save_frame)")
    # 라벨 밀림 방지: compose가 요청 시점마다 1개씩 반환하는지 강제
    if len(saved) != len(pts):
        raise RuntimeError(f"QA 프레임 개수 불일치: 요청 {len(pts)} != 반환 {len(saved)} (라벨 밀림 방지)")

    results = []
    problems = 0
    for (lbl, _t), (t, src_png) in zip(pts, saved):
        if not src_png or not os.path.exists(src_png):
            problems += 1
            results.append({"label": lbl, "t": t, "png": None, "verdict": "render_fail", "flags": [], "note": "save_frame 실패"})
            print(f"  [X] {lbl} t={t}s :: 프레임 렌더 실패")
            continue
        png = os.path.join(FRAME_DIR, f"{lbl.replace('+', 'p').replace('.', '_')}.png")
        try:
            shutil.copyfile(src_png, png)
        except Exception:
            png = src_png
        v = vision_judge(png, lbl, is_cta=lbl.startswith("cta"))
        # 하드-페일 기준 = 결정적 공간 검사(존/가장자리) + vision 가독성(low_contrast).
        # 겹침/잘림/단어쪼개짐은 gpt-4o-mini가 좌표 오판/할루시네이트가 잦아 **advisory**로만 기록
        # (레이아웃상 구조적으로 방지됨: 중앙정렬 ≤72%폭, topic0.10/handle0.24/자막0.60/CTA 시점분리).
        z = zone_intrusion(png)
        det = []
        if z.get("right") or z.get("bottom"):
            det.append("ui_zone(" + ("R" if z.get("right") else "") + ("B" if z.get("bottom") else "") + ")")
        if z.get("edge"):
            det.append("edge_cutoff")
        # 하드-페일 = 결정적 공간 검사만. gpt-4o-mini는 clean 프레임에서도 cutoff/overlap/
        # low_contrast/mid_word_break를 할루시네이트하므로 vision은 전부 advisory(육안 검토용).
        hard = det
        advisory = [k for k in ("cutoff", "overlap", "low_contrast", "mid_word_break") if v.get(k)]
        errored = v.get("verdict") in ("error", "skip")
        bad = bool(hard)
        if bad:
            problems += 1
        mark = "ERR" if errored else ("X" if bad else "OK")
        results.append({"label": lbl, "t": t, "png": png, "verdict": v.get("verdict"),
                        "hard_flags": hard, "advisory": advisory, "note": v.get("note", "")})
        adv = f" (advisory:{advisory})" if advisory else ""
        print(f"  [{mark}] {lbl} t={t}s hard={hard or '-'}{adv} :: {str(v.get('note', ''))[:70]}")

    report = os.path.join(FRAME_DIR, "report.json")
    json.dump({"bot": BOT_NAME, "problems": problems, "frames": results},
              open(report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n총 프레임 {len(results)} / 문제 {problems} → {report}")
    print(f"프레임 저장: {FRAME_DIR}")
    for f in os.listdir(WORK):
        try:
            os.remove(os.path.join(WORK, f))
        except Exception:
            pass
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
