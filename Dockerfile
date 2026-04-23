FROM debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-libgpiod \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /data
COPY --chmod=755 ekm-meter-watcher.py /usr/local/bin/ekm-meter-watcher
ENTRYPOINT ["/usr/local/bin/ekm-meter-watcher"]
