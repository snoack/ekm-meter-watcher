# ekm-meter-watcher

Script and Docker image for monitoring an EKM meter over GPIO and logging readings in SQLite.

## Setup

1. Build the image:

```sh
docker build -t ekm-meter-watcher .
```

2. Create a `docker-compose.yaml` like this:

```yaml
services:
  ekm-meter-watcher:
    image: ghcr.io/snoack/ekm-meter-watcher:latest
    restart: unless-stopped
    devices:
      - /dev/gpiochip4:/dev/gpiochip4
    # environment:
    #   EKM_GPIO: "27"
    #   EKM_TIMEOUT: "10"
    #   EKM_LOG_LEVEL: WARNING
    #   EKM_AGGREGATE_AFTER_WEEKS: "6"
    #   EKM_AGGREGATE_BY_SECONDS: "3600"
    volumes:
      - ./data:/data
```

These environment variables are optional; the commented values shown above
are the defaults.

3. Start it:

```sh
docker compose up -d
```

The host still needs to expose the GPIO character device you map in `devices`.
