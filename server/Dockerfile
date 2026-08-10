FROM python:3.13-slim

WORKDIR /app

# ffmpeg (+ ffprobe) generate the poster frame for an uploaded service video
# and verify its duration server-side — needed by app/services/storage_service.py.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ .

RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
