# Live Tracker

**YouTube 라이브 최대 동접자 추적기** by [DayFocusLab](https://github.com/jinnyjiinlee)

YouTube 라이브 링크를 넣으면 실시간으로 동접자를 추적하고, 최대 동접자 수를 기록합니다.

## Features

- **실시간 추적** - 10초~5분 간격으로 동접자 수 모니터링
- **라이브 대기** - 시작 전 링크를 넣어도 자동 대기 후 추적 시작
- **분석 리포트** - 구간별 평균, 급등/급락 감지, 하락 시점 분석
- **이메일 알림** - 라이브 종료 시 결과를 이메일로 자동 발송
- **추적 기록** - SQLite로 모든 추적 기록 영구 보관
- **서버 복구** - 서버 재시작 시 진행 중이던 추적 자동 재개

## Quick Start

```bash
# 클론
git clone https://github.com/jinnyjiinlee/yt-live-tracker.git
cd yt-live-tracker

# 의존성 설치
pip install flask
brew install yt-dlp  # 또는 pip install yt-dlp

# 실행
python3 youtube_live_web.py

# 접속
open http://localhost:5050
```

## Email Setup (선택)

라이브 종료 시 이메일 알림을 받으려면:

```bash
YT_SENDER_EMAIL="your@gmail.com" \
YT_SENDER_PASSWORD="xxxx xxxx xxxx xxxx" \
python3 youtube_live_web.py
```

> Gmail 앱 비밀번호 발급: Google 계정 > 보안 > 2단계 인증 > 앱 비밀번호

## Docker

```bash
docker build -t live-tracker .
docker run -p 5050:5050 \
  -e YT_SENDER_EMAIL="your@gmail.com" \
  -e YT_SENDER_PASSWORD="xxxx" \
  live-tracker
```

## How It Works

```
사용자 → URL + 이메일 입력 → 브라우저 닫아도 OK
                ↓
서버 (Flask) → yt-dlp로 30초마다 YouTube 메타데이터 수집
                ↓
            concurrent_view_count 기록 → SQLite 저장
                ↓
            라이브 종료 감지 → 분석 리포트 이메일 발송
```

## Tech Stack

- **Backend**: Python, Flask, SQLite
- **Frontend**: Vanilla JS, Chart.js
- **Data**: yt-dlp (YouTube metadata extraction)
- **Deploy**: Docker, Oracle Cloud Free Tier

---

Built with care by **DayFocusLab**
