"""여행봇(Wander 60s) YouTube OAuth 토큰 재발급 + 채널 검증.

사용법:
  cd travel-shorts-bot
  python auth.py

작동:
1. credentials/youtube_client_secret.json 읽기
2. 브라우저 자동 열림 → 구글 계정 로그인 → ⚠️ 여행 채널(리브랜드된 음악 채널) 선택!
3. 토큰을 credentials/youtube_token.json에 저장
4. 토큰으로 실제 채널 이름 조회 → config.CHANNEL_KEYWORDS(travel/wander/trip/journey/city 등)
   중 하나를 포함하는지 검증. 불일치 시 sys.exit(2) (잘못된 채널 결속 방지).

⚠️ 중요: 채널을 여행 브랜드로 리브랜드한 뒤 실행할 것. OAuth 화면에서
        "Animal Trivia 60s" / "Space Trivia 60s" / (리브랜드 전) "Lofi & Lit" 등
        여행 키워드가 없는 채널을 고르면 검증에서 거부된다.

배경: 2026-05-07 동물봇 토큰이 lofi 채널에 잘못 묶여 엉뚱한 영상이 발행된 사고
      재발 방지 (feedback_oauth_channel_verify).
"""
import os
import pickle
import sys

# Windows cp949 콘솔에서 이모지 print 시 UnicodeEncodeError 크래시 방지
# (인코딩은 유지 → 한글 정상, 인코딩 불가한 이모지만 ? 로 대체)
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
CREDS = os.path.join(ROOT, "credentials")
CLIENT = os.path.join(CREDS, "youtube_client_secret.json")
TOKEN = os.path.join(CREDS, "youtube_token.json")

sys.path.insert(0, ROOT)
import config as _cfg

CHANNEL_KEYWORDS = [k.lower() for k in getattr(_cfg, "CHANNEL_KEYWORDS",
                    ["wander", "travel", "trip", "journey", "city"])]
CHANNEL_NAME = getattr(_cfg, "CHANNEL_NAME", "Wander 60s")
# 다른 봇 채널로 잘못 묶이는 것을 추가 차단 (여행 키워드가 우연히 포함돼도 거부)
_FOREIGN_CHANNEL_WORDS = ["lofi", "lit", "animal", "space trivia", "spacetrivia"]

if not os.path.exists(CLIENT):
    print(f"[ERROR] {CLIENT} 없음")
    sys.exit(1)

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

print("=" * 60)
print(f"⚠️  브라우저가 열리면 반드시 여행 채널('{CHANNEL_NAME}') 선택!")
print("⚠️  Animal Trivia 60s / Space Trivia 60s / (리브랜드 전) Lofi & Lit 등 고르면 안 됨.")
print("=" * 60)

flow = InstalledAppFlow.from_client_secrets_file(CLIENT, SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

with open(TOKEN, "wb") as f:
    pickle.dump(creds, f)

print(f"\n[OK] 토큰 저장됨: {TOKEN}")
print(f"[OK] refresh_token 포함: {bool(creds.refresh_token)}")

print("\n[검증] 발급된 토큰의 채널 확인 중...")
yt = build("youtube", "v3", credentials=creds)
resp = yt.channels().list(part="snippet", mine=True).execute()
items = resp.get("items", [])
if not items:
    print("[ERROR] 채널 정보 조회 실패")
    sys.exit(1)

ch = items[0]
title = ch["snippet"]["title"]
ch_id = ch["id"]
print(f"\n채널 이름: {title}")
print(f"채널 ID:  {ch_id}")

low = title.lower()
has_travel_kw = any(k in low for k in CHANNEL_KEYWORDS)
has_foreign = any(w in low for w in _FOREIGN_CHANNEL_WORDS)
if has_travel_kw and not has_foreign:
    print(f"\n✅ 여행 채널('{title}')로 정상 발급됨! (키워드 매칭)")
elif has_foreign:
    print(f"\n❌ 채널 이름 모호/불일치 ({title}) — 다른 봇 채널 단어 포함 (animal/lofi/space 등)")
    print(f"❌ 이 토큰 쓰지 말고 채널을 여행 브랜드로 리브랜드한 뒤 auth.py 다시 실행!")
    sys.exit(2)
else:
    print(f"\n❌ 잘못된 채널 ({title})! 여행 채널 아님 (키워드 {CHANNEL_KEYWORDS} 미포함)")
    print(f"❌ 이 토큰 쓰지 말고 채널을 '{CHANNEL_NAME}'로 리브랜드한 뒤 auth.py 다시 실행!")
    sys.exit(2)
