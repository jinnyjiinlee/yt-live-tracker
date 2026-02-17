#!/usr/bin/env python3
"""
YouTube 라이브 최대 동접자 추적기 (웹 버전)
- SQLite 영구 저장
- 이메일 결과 발송
- 서버 재시작해도 진행 중인 추적 복구

실행: python3 youtube_live_web.py
접속: http://localhost:5050
"""

import subprocess
import json
import time
import threading
import uuid
import os
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

# ── 설정 ─────────────────────────────────────────────
SENDER_EMAIL = os.environ.get("YT_SENDER_EMAIL", "")
SENDER_PASSWORD = os.environ.get("YT_SENDER_PASSWORD", "")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "yt_tracker.db")
# ─────────────────────────────────────────────────────

# 메모리 세션 (활성 추적용)
active_sessions = {}


# ── DB ────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            email TEXT,
            title TEXT DEFAULT '',
            channel TEXT DEFAULT '',
            thumbnail TEXT DEFAULT '',
            max_viewers INTEGER DEFAULT 0,
            max_viewers_time TEXT,
            current_viewers INTEGER DEFAULT 0,
            status TEXT DEFAULT 'waiting',
            message TEXT DEFAULT '',
            email_sent INTEGER DEFAULT 0,
            start_time TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS viewer_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            time TEXT NOT NULL,
            viewers INTEGER NOT NULL,
            recorded_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_history_session ON viewer_history(session_id);
    """)
    conn.close()


def db_save_session(session):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO sessions
        (id, url, email, title, channel, thumbnail, max_viewers, max_viewers_time,
         current_viewers, status, message, email_sent, start_time, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
    """, (
        session["id"], session["url"], session.get("email"),
        session.get("title", ""), session.get("channel", ""),
        session.get("thumbnail", ""), session.get("max_viewers", 0),
        session.get("max_viewers_time"), session.get("current_viewers", 0),
        session.get("status", "waiting"), session.get("message", ""),
        1 if session.get("email_sent") else 0, session.get("start_time"),
    ))
    conn.commit()
    conn.close()


def db_add_history(session_id, time_str, viewers):
    conn = get_db()
    conn.execute(
        "INSERT INTO viewer_history (session_id, time, viewers) VALUES (?, ?, ?)",
        (session_id, time_str, viewers)
    )
    conn.commit()
    conn.close()


def db_get_session(session_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        conn.close()
        return None
    session = dict(row)
    history = conn.execute(
        "SELECT time, viewers FROM viewer_history WHERE session_id = ? ORDER BY id",
        (session_id,)
    ).fetchall()
    session["history"] = [{"time": h["time"], "viewers": h["viewers"]} for h in history]
    conn.close()
    return session


def db_get_recent_sessions(limit=20):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, channel, url, max_viewers, status, created_at FROM sessions ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 추적 워커 ─────────────────────────────────────────

class LiveWorker:
    def __init__(self, session_id, url, email=None, interval=30):
        self.session_id = session_id
        self.url = url
        self.email = email
        self.interval = max(10, min(interval, 600))  # 10초~10분
        self.running = True
        # 실시간 상태 (SSE용)
        self.current_viewers = 0
        self.max_viewers = 0
        self.max_viewers_time = None
        self.title = ""
        self.channel = ""
        self.thumbnail = ""
        self.status = "waiting"
        self.message = "영상 정보 확인 중..."
        self.start_time = None
        self.email_sent = False
        self.history = []

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "url": self.url,
            "title": self.title,
            "channel": self.channel,
            "thumbnail": self.thumbnail,
            "max_viewers": self.max_viewers,
            "max_viewers_time": self.max_viewers_time,
            "current_viewers": self.current_viewers,
            "history": self.history,
            "status": self.status,
            "message": self.message,
            "start_time": self.start_time,
            "email_sent": self.email_sent,
        }

    def _save(self):
        data = self.to_dict()
        data["id"] = self.session_id
        data["email"] = self.email
        db_save_session(data)

    def stop(self):
        self.running = False

    def run(self):
        poll_interval = self.interval
        wait_interval = 60
        consecutive_errors = 0

        while self.running:
            info = get_stream_info(self.url)

            if info is None:
                consecutive_errors += 1
                self.message = f"영상 정보를 가져올 수 없습니다 (재시도 {consecutive_errors}/10)"
                if consecutive_errors >= 10:
                    self.status = "error"
                    self.message = "영상 정보를 가져올 수 없습니다. URL을 확인해주세요."
                    self._save()
                    return
                time.sleep(wait_interval)
                continue

            consecutive_errors = 0
            self.title = info.get("title", "")
            self.channel = info.get("channel", info.get("uploader", ""))
            self.thumbnail = info.get("thumbnail", "")

            is_live = info.get("is_live", False)

            if not is_live and self.status == "waiting":
                self.message = "라이브 시작 대기 중..."
                self._save()
                time.sleep(wait_interval)
                continue

            if not is_live and self.status == "live":
                self.status = "ended"
                self.message = "라이브 종료!"
                self._save()
                send_result_email(self)
                return

            # 라이브 진행 중
            if self.status != "live":
                self.status = "live"
                self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            viewers = info.get("concurrent_view_count") or info.get("view_count") or 0
            self.current_viewers = viewers
            now_str = datetime.now().strftime("%H:%M:%S")
            self.history.append({"time": now_str, "viewers": viewers})

            # DB에 히스토리 저장
            db_add_history(self.session_id, now_str, viewers)

            if viewers > self.max_viewers:
                self.max_viewers = viewers
                self.max_viewers_time = now_str

            self.message = f"모니터링 중... (매 {poll_interval}초)"
            self._save()
            time.sleep(poll_interval)

        if self.status == "live":
            self.status = "ended"
            self.message = "추적 중단됨"
            self._save()


def get_stream_info(url):
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-download", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return None


def analyze_trends(history):
    """동접 추이 분석"""
    if len(history) < 3:
        return {}

    viewers = [h["viewers"] for h in history]
    times = [h["time"] for h in history]
    total = len(viewers)
    avg = sum(viewers) / total
    peak_idx = viewers.index(max(viewers))

    # 구간 분석 (전체를 4등분)
    q = max(total // 4, 1)
    phases = {
        "초반": viewers[:q],
        "중반 전반": viewers[q:q*2],
        "중반 후반": viewers[q*2:q*3],
        "후반": viewers[q*3:],
    }
    phase_avgs = {k: int(sum(v)/len(v)) if v else 0 for k, v in phases.items()}

    # 피크 이후 하락 시점 찾기
    decline_start = None
    if peak_idx < total - 2:
        # 피크 이후 3연속 감소하는 첫 지점
        for i in range(peak_idx, min(peak_idx + 20, total - 2)):
            if viewers[i] > viewers[i+1] > viewers[i+2]:
                decline_start = times[i]
                break

    # 급등/급락 감지 (이전 대비 20% 이상 변화)
    spikes = []
    for i in range(1, total):
        if viewers[i-1] == 0:
            continue
        change = (viewers[i] - viewers[i-1]) / viewers[i-1] * 100
        if abs(change) >= 20:
            direction = "급등" if change > 0 else "급락"
            spikes.append(f"{times[i]} {direction} ({change:+.0f}%)")

    return {
        "phase_avgs": phase_avgs,
        "peak_time": times[peak_idx],
        "decline_start": decline_start,
        "spikes": spikes[:5],  # 최대 5개
        "duration_minutes": total * 0.5,  # 30초 간격 기준
    }


def send_result_email(worker: LiveWorker):
    if not worker.email or not SENDER_EMAIL or not SENDER_PASSWORD:
        return

    try:
        viewers_list = [h["viewers"] for h in worker.history]
        avg_viewers = sum(viewers_list) / len(viewers_list) if viewers_list else 0
        min_viewers = min(viewers_list) if viewers_list else 0
        analysis = analyze_trends(worker.history)

        # 구간별 평균 HTML
        phase_html = ""
        if analysis.get("phase_avgs"):
            phase_rows = ""
            for phase, avg_val in analysis["phase_avgs"].items():
                bar_width = min(int(avg_val / max(worker.max_viewers, 1) * 200), 200)
                phase_rows += f"""
                <tr>
                    <td style="padding: 8px 10px; color: #aaa; font-size: 13px; border-bottom: 1px solid #222; width: 80px;">{phase}</td>
                    <td style="padding: 8px 10px; border-bottom: 1px solid #222;">
                        <div style="background: #ff4444; height: 16px; border-radius: 4px; width: {bar_width}px; display: inline-block; vertical-align: middle;"></div>
                        <span style="color: #fff; font-size: 13px; margin-left: 8px;">{avg_val:,}명</span>
                    </td>
                </tr>"""
            phase_html = f"""
            <div style="margin: 20px 0;">
                <div style="color: #888; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px;">구간별 평균 시청자</div>
                <table style="width: 100%; border-collapse: collapse;">{phase_rows}</table>
            </div>"""

        # 추이 분석 HTML
        trend_html = ""
        trend_items = []
        if analysis.get("decline_start"):
            trend_items.append(f"<li>{analysis['decline_start']}부터 시청자 감소 시작</li>")
        if analysis.get("spikes"):
            for spike in analysis["spikes"]:
                trend_items.append(f"<li>{spike}</li>")
        duration = analysis.get("duration_minutes", 0)
        if duration:
            hours = int(duration // 60)
            mins = int(duration % 60)
            dur_str = f"{hours}시간 {mins}분" if hours else f"{mins}분"
            trend_items.insert(0, f"<li>총 라이브 시간: {dur_str}</li>")

        if trend_items:
            trend_html = f"""
            <div style="margin: 20px 0;">
                <div style="color: #888; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px;">추이 분석</div>
                <ul style="color: #ccc; font-size: 14px; padding-left: 20px; line-height: 2;">
                    {''.join(trend_items)}
                </ul>
            </div>"""

        html = f"""
        <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; background: #1a1a1a; color: #e1e1e1; border-radius: 16px; overflow: hidden;">
            <div style="background: #ff4444; padding: 24px; text-align: center;">
                <h1 style="margin: 0; color: #fff; font-size: 22px;">YouTube Live Tracker 리포트</h1>
            </div>
            <div style="padding: 24px;">
                <h2 style="color: #fff; font-size: 18px; margin-bottom: 4px;">{worker.title}</h2>
                <p style="color: #aaa; margin-top: 0;">{worker.channel}</p>

                <!-- 핵심 지표 -->
                <div style="display: flex; gap: 12px; margin: 20px 0;">
                    <div style="flex: 1; background: #0f0f0f; border-radius: 12px; padding: 16px; text-align: center;">
                        <div style="color: #888; font-size: 11px; text-transform: uppercase;">최대 동접</div>
                        <div style="color: #ff4444; font-size: 32px; font-weight: 800;">{worker.max_viewers:,}</div>
                        <div style="color: #555; font-size: 11px;">{worker.max_viewers_time}</div>
                    </div>
                    <div style="flex: 1; background: #0f0f0f; border-radius: 12px; padding: 16px; text-align: center;">
                        <div style="color: #888; font-size: 11px; text-transform: uppercase;">평균 동접</div>
                        <div style="color: #fff; font-size: 32px; font-weight: 800;">{avg_viewers:,.0f}</div>
                        <div style="color: #555; font-size: 11px;">전체 평균</div>
                    </div>
                    <div style="flex: 1; background: #0f0f0f; border-radius: 12px; padding: 16px; text-align: center;">
                        <div style="color: #888; font-size: 11px; text-transform: uppercase;">최소 동접</div>
                        <div style="color: #fff; font-size: 32px; font-weight: 800;">{min_viewers:,}</div>
                        <div style="color: #555; font-size: 11px;">최저점</div>
                    </div>
                </div>

                {phase_html}
                {trend_html}

                <table style="width: 100%; border-collapse: collapse; margin: 16px 0; background: #0f0f0f; border-radius: 8px; overflow: hidden;">
                    <tr><td style="padding: 10px 14px; color: #888; border-bottom: 1px solid #222;">추적 시작</td><td style="padding: 10px 14px; color: #fff; text-align: right; border-bottom: 1px solid #222;">{worker.start_time or '-'}</td></tr>
                    <tr><td style="padding: 10px 14px; color: #888; border-bottom: 1px solid #222;">추적 종료</td><td style="padding: 10px 14px; color: #fff; text-align: right; border-bottom: 1px solid #222;">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
                    <tr><td style="padding: 10px 14px; color: #888;">데이터 포인트</td><td style="padding: 10px 14px; color: #fff; text-align: right;">{len(worker.history)}회</td></tr>
                </table>

                <p style="color: #444; font-size: 11px; text-align: center; margin-top: 24px;">YouTube Live Tracker</p>
            </div>
        </div>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Live Tracker] {worker.title} - 최대 {worker.max_viewers:,}명"
        msg["From"] = SENDER_EMAIL
        msg["To"] = worker.email
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, worker.email, msg.as_string())

        worker.email_sent = True
        worker._save()
        print(f"[Email] 결과 발송 완료 -> {worker.email}")
    except Exception as e:
        print(f"[Email] 발송 실패: {e}")


def restore_active_sessions():
    """서버 재시작 시 진행 중이던 세션 복구"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, url, email FROM sessions WHERE status IN ('waiting', 'live')"
    ).fetchall()
    conn.close()

    for row in rows:
        sid = row["id"]
        print(f"[복구] 세션 재시작: {sid} ({row['url']})")
        worker = LiveWorker(sid, row["url"], row["email"])
        active_sessions[sid] = worker
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()


# ── Routes ────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("youtube_tracker.html")


@app.route("/api/start", methods=["POST"])
def start_tracking():
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL을 입력해주세요"}), 400
    if "youtube.com" not in url and "youtu.be" not in url:
        return jsonify({"error": "유효한 YouTube URL을 입력해주세요"}), 400

    email = data.get("email", "").strip() or None
    interval = int(data.get("interval", 30))
    session_id = str(uuid.uuid4())[:8]

    # DB에 세션 생성
    db_save_session({
        "id": session_id, "url": url, "email": email,
        "status": "waiting", "message": "영상 정보 확인 중...",
    })

    # 워커 시작
    worker = LiveWorker(session_id, url, email, interval=interval)
    active_sessions[session_id] = worker
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()

    return jsonify({"session_id": session_id})


@app.route("/api/status/<session_id>")
def get_status(session_id):
    # 메모리에 있으면 실시간 데이터 반환
    worker = active_sessions.get(session_id)
    if worker:
        return jsonify(worker.to_dict())
    # 없으면 DB에서 조회 (종료된 세션)
    session = db_get_session(session_id)
    if not session:
        return jsonify({"error": "세션을 찾을 수 없습니다"}), 404
    session["session_id"] = session["id"]
    return jsonify(session)


@app.route("/api/stop/<session_id>", methods=["POST"])
def stop_tracking(session_id):
    worker = active_sessions.get(session_id)
    if not worker:
        return jsonify({"error": "진행 중인 세션이 아닙니다"}), 404
    worker.stop()
    return jsonify({"ok": True})


@app.route("/api/stream/<session_id>")
def stream(session_id):
    def generate():
        worker = active_sessions.get(session_id)
        if not worker:
            # DB에서 완료된 세션 데이터 전송
            session = db_get_session(session_id)
            if session:
                session["session_id"] = session["id"]
                yield f"data: {json.dumps(session)}\n\n"
            else:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
            return

        while worker.running or worker.status in ("live", "ended"):
            yield f"data: {json.dumps(worker.to_dict())}\n\n"
            if worker.status in ("ended", "error"):
                break
            time.sleep(3)

        yield f"data: {json.dumps(worker.to_dict())}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/history")
def get_history():
    """최근 추적 기록"""
    return jsonify(db_get_recent_sessions())


@app.route("/api/email-available")
def email_available():
    return jsonify({"available": bool(SENDER_EMAIL and SENDER_PASSWORD)})


@app.route("/api/session/<session_id>")
def get_session_detail(session_id):
    """완료된 세션 상세 (DB에서)"""
    session = db_get_session(session_id)
    if not session:
        return jsonify({"error": "세션을 찾을 수 없습니다"}), 404
    session["session_id"] = session["id"]
    return jsonify(session)


if __name__ == "__main__":
    init_db()
    restore_active_sessions()
    print("=" * 50)
    print("  YouTube 라이브 동접 추적기")
    print("  http://localhost:5050")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
