FROM python:3.13-slim AS builder

WORKDIR /build

# TgCrypto 需从源码编译；slim 镜像仅装 gcc 会缺 stdint.h
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.13-slim

ARG APP_VERSION=0.0.0
LABEL org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IN_DOCKER=1 \
    DOWNLOAD_FOLDER=/data \
    CONFIG_FOLDER=/config \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080

WORKDIR /app

COPY --from=builder /install /usr/local
COPY start.py ./
COPY bot ./bot
COPY web ./web

RUN mkdir -p /data /config

EXPOSE 8080

VOLUME ["/data", "/config"]

CMD ["python", "start.py"]
