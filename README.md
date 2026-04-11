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

## Wiring

By default the watcher reads pulses from GPIO `27`, which is physical pin `13`
on the Raspberry Pi. If using a different GPIO pin set `EKM_GPIO` accordingly.

The EKM meter pulse output behaves like a switch that closes and opens `800`
times per kWh used. Wire it like this:

<img src="doc/wiring-schematic.svg" alt="Wiring schematic" width="100%">

1. Supply `3.3 V` from the Pi to the meter.
2. Put a `1 kOhm` resistor in series with the input GPIO pin to limit current.
3. Add a `10 kOhm` pull-down resistor on the GPIO input, on the meter side of
   the `1 kOhm` resistor.
4. Connect the meter pulse output to the GPIO input.

The script requests the GPIO line with bias disabled, so the external pull-down
is required.
