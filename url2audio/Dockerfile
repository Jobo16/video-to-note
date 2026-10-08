FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg chromium \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY url2audio/requirements.txt /app/url2audio/requirements.txt
RUN pip install --no-cache-dir -r /app/url2audio/requirements.txt
COPY backend/__init__.py backend/video_processor.py backend/transcript.py backend/douyin_browser.py /app/backend/
COPY url2audio /app/url2audio

RUN useradd --uid 10001 --create-home app
USER app
ENV URL2AUDIO_DATA_DIR=/data URL2AUDIO_CONTAINER=1 PYTHONUNBUFFERED=1

CMD ["uvicorn", "url2audio.app:app", "--host", "127.0.0.1", "--port", "14175"]
