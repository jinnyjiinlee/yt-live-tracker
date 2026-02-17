FROM python:3.13-slim

WORKDIR /app

# yt-dlp 설치
RUN pip install --no-cache-dir yt-dlp flask gunicorn

# 앱 복사
COPY youtube_live_web.py .
COPY templates/ templates/
RUN mkdir -p data

EXPOSE 5050

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "1", "--threads", "8", "--timeout", "0", "youtube_live_web:app"]
