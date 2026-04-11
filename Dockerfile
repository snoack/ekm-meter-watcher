FROM debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        python3 \
        python3-libgpiod \
        sqlite3 \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /data

COPY --chmod=755 ekm-meter-watcher.py /usr/local/bin/ekm-meter-watcher
COPY --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/docker-entrypoint.sh"]
