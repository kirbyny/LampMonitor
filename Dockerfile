FROM python:3.12-slim

# ffmpeg: frames from RTSP(S) camera streams. fonts-dejavu-core: labels on alert snapshots. tzdata: local times in emails.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY lampmonitor ./lampmonitor
RUN pip install --no-cache-dir .

RUN useradd --system --uid 1000 lampmonitor && mkdir -p /data && chown lampmonitor /data
USER lampmonitor

ENV LAMPMONITOR_DATA_DIR=/data \
    LAMPMONITOR_HOST=0.0.0.0 \
    LAMPMONITOR_PORT=8080 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"

CMD ["lampmonitor"]
