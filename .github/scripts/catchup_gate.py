"""놓친 정규 회차 자동 보충(catch-up) 판정 — 워크플로 gate 잡에서 실행.

2026-10-14 추가. 10/12(동물)·10/13(우주) 정규 회차가 edge-tts 403(MS 인증 변경, 라이브러리
수정판은 10/13 밤 배포)으로 실패했는데 다음 정규 회차까지 슬롯이 그냥 비었다. 외부 무료 API 는
또 바뀔 수 있으니, 실패한 회차를 매일 다시 시도해 고쳐지는 즉시 메우도록 한다.

판정(stdout 에 GitHub output 형식으로 run=true/false 한 줄):
- 수동 실행(workflow_dispatch)·정규 cron → 항상 run=true (기존 동작 그대로)
- 보충 cron → 오늘이 정규 발행 요일이면 false(정규 cron 이 처리),
  대기열(queue/pending)이 비면 false(QUEUE_EMPTY 알림 반복 방지),
  직전 정규 발행일(오늘 이전) 이후 발행 기록(queue/done/*.json published_at)이 있으면 false,
  없으면 true(놓친 회차 보충).

발행 기록 = queue/done/*.json 의 published_at 과 content_history.json 의 date 중 가장 늦은 날
(done 메타 기록이 실패해도 content_history 로 잡는다 — codex 지적).

공유 할당량(3봇 합산 10,000/일, 1편 약 4,850 → 하루 2편)을 넘지 않게 보충 요일을 봇마다 나눈다:
정규 동물 월목·우주 화금·여행 수토 → 보충 동물 화금일·우주 수토월·여행 목일.
어느 요일이든 정규 1 + 보충 1(일요일은 보충 2)을 넘지 않는다. 보충 요일은 보충 cron 자체에 박는다.

env: EVENT_NAME, EVENT_SCHEDULE(발화한 cron 문자열), REGULAR_CRON·CATCHUP_CRON(워크플로 on.schedule 과 글자까지 같아야 함),
     GATE_TODAY(YYYY-MM-DD, 테스트용 — 비우면 오늘 UTC)
"""
import datetime as dt
import glob
import json
import os
import sys


def _say(run: bool, why: str) -> None:
    print(f"run={'true' if run else 'false'}")
    print(f"[catchup-gate] run={run} — {why}", file=sys.stderr)


def _cron_dows(cron: str) -> set:
    """cron 요일 필드(0/7=일 … 6=토, 쉼표·범위)만 파싱."""
    field = cron.split()[4]
    if field == "*":
        return set(range(7))
    out = set()
    for part in field.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return {d % 7 for d in out}


def _norm(cron: str) -> str:
    return " ".join(cron.split())  # 공백 개수 차이로 문자열 비교가 어긋나지 않게(agy 지적)


def _cron_dow(day: dt.date) -> int:
    return (day.weekday() + 1) % 7  # python 월=0 → cron 일=0


def _latest_published(root: str = "."):
    latest = None
    for path in glob.glob(os.path.join(root, "queue", "done", "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                ts = json.load(f).get("published_at")
            day = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").date()
        except Exception:
            continue
        latest = day if latest is None or day > latest else latest
    try:
        with open(os.path.join(root, "content_history.json"), encoding="utf-8") as f:
            hist = json.load(f)
        for entries in (hist.values() if isinstance(hist, dict) else []):
            for e in (entries if isinstance(entries, list) else [entries]):
                try:
                    day = dt.date.fromisoformat(str(e.get("date"))[:10])
                except Exception:
                    continue
                latest = day if latest is None or day > latest else latest
    except Exception:
        pass
    return latest


def main() -> None:
    event = os.environ.get("EVENT_NAME", "")
    fired, regular, catchup = (_norm(os.environ.get(k, "")) for k in ("EVENT_SCHEDULE", "REGULAR_CRON", "CATCHUP_CRON"))
    if event != "schedule":
        return _say(True, f"{event or '수동'} 실행")
    if fired == regular or not catchup or fired != catchup:
        # 정규 cron, 또는 보충 cron 으로 확인되지 않는 발화 → 예전처럼 그대로 실행
        return _say(True, f"정규/기타 cron '{fired}'")

    try:
        today_env = os.environ.get("GATE_TODAY", "").strip()
        today = dt.date.fromisoformat(today_env) if today_env else dt.datetime.utcnow().date()
        slots = _cron_dows(regular)
    except Exception as e:
        return _say(False, f"설정 오류로 보충 판정 불가(REGULAR_CRON='{regular}'): {e}")
    if _cron_dow(today) in slots:
        return _say(False, f"{today} 은 정규 발행일 — 정규 cron 이 처리")
    if not glob.glob(os.path.join("queue", "pending", "*.json")):
        return _say(False, "대기열 비어 있음 — 보충할 대본 없음")

    last_slot = today - dt.timedelta(days=1)
    for _ in range(7):
        if _cron_dow(last_slot) in slots:
            break
        last_slot -= dt.timedelta(days=1)
    latest = _latest_published()
    if latest is not None and latest >= last_slot:
        return _say(False, f"직전 정규일 {last_slot} 이후 발행 있음(마지막 {latest})")
    return _say(True, f"직전 정규일 {last_slot} 회차 미발행(마지막 발행 {latest}) — 보충")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 판정기가 죽어 정규 회차까지 막히는 일은 없게: 보충 cron 이면 skip, 그 외엔 실행
        is_catchup = os.environ.get("EVENT_NAME") == "schedule" and _norm(os.environ.get("EVENT_SCHEDULE", "")) == _norm(os.environ.get("CATCHUP_CRON", "")) != ""
        _say(not is_catchup, f"판정 중 예외 {type(e).__name__}: {e}")
