# Space Trivia 60s — 쿠마님 셋업 가이드 (운동 후 진행)

GitHub repo는 이미 생성·코드 push 완료. **남은 일은 YouTube 새 채널 + OAuth 토큰 + GitHub Secrets 6개 등록뿐**입니다.

소요 시간: **약 20~30분**.

---

## 단계 1. YouTube 새 채널 만들기 (5분)

기존 "Daily Reddit Stories" / 음악쇼츠 채널과는 분리된 새 채널이 필요합니다.

1. https://youtube.com 접속 → 우측 상단 본인 프로필 아이콘 클릭
2. **"계정 전환" → "+ 채널 만들기"** 클릭
3. 채널 이름: **`Space Trivia 60s`** (또는 원하는 이름)
4. 채널 생성 완료 후, **YouTube Studio 진입 → 좌측 사이드바 "맞춤설정"**
5. (선택) 채널 아트, 프로필 이미지 업로드. 이건 나중에 천천히 해도 OK

> ⚠️ 이 채널이 **"브랜드 계정"** 으로 만들어져야 다른 채널과 분리됩니다. 일반 개인 채널로 만들지 마세요.

---

## 단계 2. Google Cloud OAuth 클라이언트 발급 (5~10분)

새 채널용 OAuth 토큰을 받기 위해.

1. https://console.cloud.google.com 접속
2. 좌측 상단 프로젝트 드롭다운 → **"새 프로젝트"** → 이름 `space-trivia-bot` → 만들기
3. 좌측 메뉴 → **"API 및 서비스 → 라이브러리"** → "YouTube Data API v3" 검색 → **"사용 설정"**
4. 좌측 메뉴 → **"API 및 서비스 → OAuth 동의 화면"**:
   - User Type: **외부** 선택
   - 앱 이름: `space-trivia-bot`
   - 사용자 지원 이메일: 본인 이메일
   - 개발자 연락처: 본인 이메일
   - 저장 후 다음 → 범위 추가 → "/auth/youtube.upload" + "/auth/youtube.force-ssl" 두 개 추가 → 저장
   - **테스트 사용자**에 본인 Google 이메일 추가 (필수!)
5. 좌측 메뉴 → **"API 및 서비스 → 사용자 인증 정보"**:
   - 상단 **"+ 사용자 인증 정보 만들기" → "OAuth 클라이언트 ID"**
   - 애플리케이션 유형: **데스크톱 앱**
   - 이름: `animal-trivia-desktop`
   - 만들기 → **JSON 다운로드** (파일 이름이 보통 `client_secret_xxxxx.apps.googleusercontent.com.json`)
6. 다운받은 JSON을 다음 경로에 복사·이름 변경:
   ```
   C:\Users\goopy\Desktop\Claude\동물쇼츠봇\credentials\youtube_client_secret.json
   ```
   (`credentials` 폴더가 없으면 만드세요)

---

## 단계 3. 로컬에서 OAuth 토큰 받기 (5분)

브라우저로 한 번 로그인해서 토큰 발급받는 단계.

PowerShell 또는 cmd 새로 열고:
```bash
cd C:\Users\goopy\Desktop\Claude\동물쇼츠봇
pip install -r requirements.txt
python -c "from pipeline import youtube_upload; youtube_upload.get_service(); print('done')"
```

자동으로 브라우저 열림 → **Space Trivia 60s 채널을 만든 Google 계정으로 로그인** → 권한 허용 →
"인증 흐름이 완료되었습니다" 메시지 → 콘솔에 `done` 표시되면 성공.

> ⚠️ 로그인하는 Google 계정은 단계 1에서 새 채널을 만든 그 계정이어야 합니다.

이제 `credentials/youtube_token.json` 파일이 자동 생성됐을 겁니다.

---

## 단계 4. 두 JSON을 Base64로 인코딩 (1분)

GitHub Secrets에 넣을 형식으로 변환.

PowerShell에서:
```powershell
cd C:\Users\goopy\Desktop\Claude\동물쇼츠봇
[Convert]::ToBase64String([IO.File]::ReadAllBytes("credentials/youtube_client_secret.json")) | Out-File -Encoding ascii yt_client_secret_b64.txt
[Convert]::ToBase64String([IO.File]::ReadAllBytes("credentials/youtube_token.json")) | Out-File -Encoding ascii yt_token_b64.txt
```

`yt_client_secret_b64.txt`, `yt_token_b64.txt` 두 파일에 Base64 문자열이 들어 있습니다.

---

## 단계 5. GitHub Secrets 6개 등록 (10분)

https://github.com/Kkumakkuma/space-trivia-bot/settings/secrets/actions 접속 → **"New repository secret"** 6번 클릭.

| Secret 이름 | 값 어디서 가져오나 |
|---|---|
| `OPENAI_API_KEY` | https://github.com/Kkumakkuma/music-shorts-bot/settings/secrets/actions 에 있는 그 값. 복사 못 하니 본인 OpenAI 대시보드 (https://platform.openai.com/api-keys) 에서 새로 만들어 사용. **음악쇼츠봇과 같은 키 재사용 가능** — 그 키 아는 곳에서 복사 |
| `PEXELS_API_KEY` | 음악쇼츠봇과 같은 키 재사용. 본인 Pexels 계정 (https://www.pexels.com/api/) 대시보드에서 확인 |
| `YT_CLIENT_SECRET_B64` | 단계 4의 `yt_client_secret_b64.txt` 내용 전체 복사 |
| `YT_TOKEN_B64` | 단계 4의 `yt_token_b64.txt` 내용 전체 복사 |
| `TELEGRAM_BOT_TOKEN` | 음악쇼츠봇과 같은 값 |
| `TELEGRAM_CHAT_ID` | 음악쇼츠봇과 같은 값 (`1667761411`) |

> 💡 OpenAI, Pexels, Telegram 4개는 **음악쇼츠봇과 같은 값** 그대로 쓰시면 됩니다. 매번 새로 만들 필요 없음.

---

## 단계 6. 첫 발행 테스트 (5분)

https://github.com/Kkumakkuma/space-trivia-bot/actions/workflows/space-trivia-auto.yml
→ 우측 **"Run workflow" → 초록 버튼 "Run workflow"** 클릭

5~10분 후 텔레그램에 자동 알림 옵니다:
```
🦦 동물쇼츠 발행 완료
📅 2026-XX-XX HH:MM KST
🐾 동물: otter
🌐 다국어: ko,ja,es,pt,de,fr,hi
🎥 https://youtube.com/shorts/XXXXX
```

이 URL 클릭해서 영상 보시고, OK면 끝. 그 후로는 **KST 00/06/12/18 자동 발행**.

---

## 문제 시 (자가 진단 표)

| 텔레그램 알림 메시지 | 원인 / 해결 |
|---|---|
| ❌ 동물쇼츠 실패 + Run URL | Actions 페이지에서 로그 확인. 보통 secret 값 오타 또는 OAuth 토큰 만료 |
| (알림 자체가 안 옴) | TELEGRAM_BOT_TOKEN / CHAT_ID 잘못 입력. 음악쇼츠봇과 같은 값으로 다시 확인 |
| OAuth 만료 ("invalid_grant") | 60일 지나면 자동 갱신. 그래도 만료되면 단계 3 다시 |

---

## 완료 체크리스트

- [ ] YouTube `Space Trivia 60s` 채널 생성
- [ ] Google Cloud OAuth 클라이언트 + 테스트 사용자 등록
- [ ] `credentials/youtube_client_secret.json` 저장
- [ ] `python -c "from pipeline import youtube_upload; youtube_upload.get_service()"` 성공 → `youtube_token.json` 생성
- [ ] Base64 두 파일 변환
- [ ] GitHub Secrets 6개 등록
- [ ] Run workflow 첫 발행 → 텔레그램 알림 + YouTube URL 확인

다 되면 **하루 4편 자동 발행**이 24/7 돌아갑니다. 🦦
