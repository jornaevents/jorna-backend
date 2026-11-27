# Pinned to an exact image so staging and production — separate Railway
# builds — get the same Python and Debian packages. Dependabot (docker) bumps
# it. Keep the minor version in step with python-version in
# .github/workflows/ci.yml, which tests on it.
FROM python:3.13.18-slim-trixie@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50

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
