FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root user; UID matches the usual first host user so ./data stays editable on the host.
ARG UID=1000
RUN useradd --create-home --uid ${UID} app
USER app
WORKDIR /app

# Claude Code CLI (native build for the image's architecture: amd64 or arm64).
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/app/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PORT=8765

COPY --chown=app:app server.py webauthn.py backup.py things.py index.html ./
RUN mkdir -p data

# Start as root only to fix ownership of the mounted ./data (Docker creates it as root), then run as app.
USER root
ENV HOME=/home/app
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/auth/state')" || exit 1
CMD ["sh", "-c", "chown -R app:app /app/data && exec setpriv --reuid=app --regid=app --init-groups python3 server.py"]
