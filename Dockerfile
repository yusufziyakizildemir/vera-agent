# VERA — 7/24 Kişisel Otonom Ajan Docker İmajı
# Hem Hugging Face Spaces (UID 1000, Port 7860, 16 GB RAM, 0 TL) hem standart VPS ile %100 uyumlu!
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Istanbul \
    WEB_PORT=7860

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    curl \
    ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces güvenlik politikası gereği UID 1000 non-root kullanıcı
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH="/home/user/.local/bin:${PATH}"

WORKDIR /home/user/app

COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user:user main.py .
COPY --chown=user:user vera ./vera
COPY --chown=user:user skills ./skills
COPY --chown=user:user data/memory ./data/memory_defaults

RUN mkdir -p /home/user/app/data/memory /home/user/app/data/db /home/user/app/data/uploads /home/user/app/workspace \
    && chown -R user:user /home/user/app

RUN printf '#!/bin/sh\nmkdir -p /home/user/app/data/memory /home/user/app/data/db /home/user/app/data/uploads /home/user/app/workspace\nfor f in SOUL.md USER.md MEMORY.md; do\n  if [ ! -f "/home/user/app/data/memory/$f" ] && [ -f "/home/user/app/data/memory_defaults/$f" ]; then\n    cp "/home/user/app/data/memory_defaults/$f" "/home/user/app/data/memory/$f"\n  fi\ndone\nPORT_NUM="${PORT:-${WEB_PORT:-7860}}"\nexec uvicorn main:app --host 0.0.0.0 --port "$PORT_NUM" --workers 1\n' > /home/user/app/entrypoint.sh \
    && chmod +x /home/user/app/entrypoint.sh \
    && chown user:user /home/user/app/entrypoint.sh

USER user

EXPOSE 7860

HEALTHCHECK --interval=60s --timeout=10s --start-period=25s --retries=3 \
    CMD sh -c 'curl -fsS "http://127.0.0.1:${PORT:-7860}/api/ping"' || exit 1

ENTRYPOINT ["/home/user/app/entrypoint.sh"]
