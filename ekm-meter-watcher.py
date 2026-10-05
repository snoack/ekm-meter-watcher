#!/usr/bin/env python3

import argparse
import fcntl
import json
import sys
import time
import logging
import math
import os
import select
import signal
import sqlite3
import threading
from datetime import datetime, timedelta

import gpiod
from gpiod.line import Edge, Bias

DATABASE = "db"
LOCKFILE = "lock"
GPIO_CHIP = "/dev/gpiochip4"
GPIO = int(os.environ.get("EKM_GPIO", 27))
TIMEOUT = float(os.environ.get("EKM_TIMEOUT", 10))
AGGREGATE_AFTER_WEEKS = int(os.environ.get("EKM_AGGREGATE_AFTER_WEEKS", 6))
AGGREGATE_BY_SECONDS = int(os.environ.get("EKM_AGGREGATE_BY_SECONDS", 3600))
AGGREGATE_INTERVAL = int(os.environ.get("EKM_AGGREGATE_INTERVAL", 60 * 60 * 24))
MQTT_HOST = os.environ.get("EKM_MQTT_HOST")
MQTT_PORT = int(os.environ.get("EKM_MQTT_PORT", 1883))
MQTT_USERNAME = os.environ.get("EKM_MQTT_USERNAME")
MQTT_PASSWORD = os.environ.get("EKM_MQTT_PASSWORD")
MQTT_TOPIC = os.environ.get("EKM_MQTT_TOPIC", "ekm_meter")
MQTT_DISCOVERY_PREFIX = os.environ.get("EKM_MQTT_DISCOVERY_PREFIX", "homeassistant")
ENERGY_INTERVAL = float(os.environ.get("EKM_ENERGY_INTERVAL", 60))
POWER_TIMEOUT = float(os.environ.get("EKM_POWER_TIMEOUT", 300))
IMPULSES_PER_KWH = 800
JOULES_PER_IMPULSE = 3.6e6 / IMPULSES_PER_KWH

def create_view(name, interval=None):
    time = "timestamp - (interval >> 1)"
    watts = f"impulses * {JOULES_PER_IMPULSE} / interval"
    group_by = ""

    if interval is not None:
        time = f"({time}) / {interval} * {interval} + {interval // 2}"
        watts = f"AVG({watts})"
        group_by = " GROUP BY ts"

    return (f"CREATE VIEW IF NOT EXISTS {name} AS " +
            f"SELECT {time} AS ts, {watts} AS watts " +
            f"FROM usage{group_by};")

def connect_db():
    logging.info("Opening SQLite database at %s", os.path.abspath(DATABASE))
    db = sqlite3.connect(DATABASE)
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS usage (
            timestamp INTEGER NOT NULL,
            interval REAL NOT NULL,
            impulses INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS timestamp on usage (timestamp);
        CREATE INDEX IF NOT EXISTS interval on usage (interval);
        {create_view("view_realtime")}
        {create_view("view_5m", 300)}
        {create_view("view_1h", 3600)}""")
    return db

def next_deadline(deadline, interval, now):
    return deadline + ((now - deadline) // interval + 1) * interval

def aggregate(db=None):
    logging.info("Combining records older than %s weeks into %s second " +
                 "intervals", AGGREGATE_AFTER_WEEKS, AGGREGATE_BY_SECONDS)

    round_up_time = "(timestamp / ?1 + 1) * ?1"
    cutoff = (datetime.now() - timedelta(weeks=AGGREGATE_AFTER_WEEKS)).timestamp()
    params = (AGGREGATE_BY_SECONDS, cutoff)

    close_db = False
    if not db:
        db = connect_db()
        close_db = True

    try:
        with db:
            cursor = db.cursor()
            cursor.execute("INSERT INTO usage (timestamp, interval, impulses) " +
                        "SELECT {} AS time_rounded_up, ?1, SUM(impulses) ".format(round_up_time) +
                        "FROM usage WHERE interval < ?1 AND time_rounded_up < ?2 " +
                        "GROUP BY time_rounded_up", params)
            inserted = cursor.rowcount
            cursor.execute("DELETE FROM usage " +
                        "WHERE interval < ?1 AND {} < ?2".format(round_up_time), params)
            deleted = cursor.rowcount
        logging.info("Aggregation complete: inserted %s rows, deleted %s rows", inserted, deleted)
    finally:
        if close_db:
            db.close()

class HomeAssistant:
    availability_topic = f"{MQTT_TOPIC}/status"

    def __init__(self):
        import paho.mqtt.client as mqtt

        self.ref_pulse_ns = None
        self.watts = math.inf

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_TOPIC)
        if MQTT_USERNAME:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.client.will_set(self.availability_topic, "offline", qos=1, retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_connect_fail = self.on_connect_fail
        self.client.on_disconnect = self.on_disconnect
        # Network I/O and reconnects happen on paho's own thread,
        # so publish() only queues messages and never blocks.
        self.client.connect_async(MQTT_HOST, MQTT_PORT)
        self.client.loop_start()

    def close(self):
        if self.client.is_connected():
            info = self.client.publish(self.availability_topic, "offline", qos=1, retain=True)
            try:
                info.wait_for_publish(1)
            except (RuntimeError, ValueError):
                pass
        self.client.disconnect()
        self.client.loop_stop()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            logging.warning("MQTT: connection refused: %s", reason_code)
            return

        logging.info("MQTT: connected to %s:%s", MQTT_HOST, MQTT_PORT)
        for key, config in [
            ("energy", {"device_class": "energy",
                        "state_class": "total_increasing",
                        "unit_of_measurement": "kWh",
                        "suggested_display_precision": 3}),
            ("power", {"device_class": "power",
                       "state_class": "measurement",
                       "unit_of_measurement": "W",
                       "suggested_display_precision": 0}),
        ]:
            client.publish(f"{MQTT_DISCOVERY_PREFIX}/sensor/{MQTT_TOPIC}/{key}/config", json.dumps({
                "name": key.capitalize(),
                "unique_id": f"{MQTT_TOPIC}_{key}",
                "state_topic": self.state_topic(key),
                "availability_topic": self.availability_topic,
                "device": {"identifiers": [MQTT_TOPIC], "name": "EKM meter"},
                **config,
            }), qos=1, retain=True)
        client.publish(self.availability_topic, "online", qos=1, retain=True)

    def on_connect_fail(self, client, userdata):
        logging.warning("MQTT: failed to connect to %s:%s", MQTT_HOST, MQTT_PORT)

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            logging.warning("MQTT: disconnected: %s", reason_code)

    def state_topic(self, key):
        return f"{MQTT_TOPIC}/{key}"

    def power(self, impulses, last_pulse_ns, now_ns):
        if impulses:
            # The span from the newest pulse at the previous update to the
            # newest pulse now contains exactly `impulses` pulse periods.
            return round(impulses * JOULES_PER_IMPULSE * 1e9 / (last_pulse_ns - self.ref_pulse_ns))

        # No pulse since: the power can't be higher than if the next one arrived now.
        # Edge event timestamps come from CLOCK_MONOTONIC, same as monotonic_ns().
        elapsed_ns = now_ns - last_pulse_ns
        if elapsed_ns >= POWER_TIMEOUT * 1e9:
            return 0
        return min(round(JOULES_PER_IMPULSE * 1e9 / elapsed_ns), self.watts)

    def publish(self, impulses, last_pulse_ns, total):
        if total is not None:
            self.client.publish(self.state_topic("energy"),
                                f"{total / IMPULSES_PER_KWH:.5f}", retain=True)

        if self.ref_pulse_ns is not None:
            self.watts = self.power(impulses, last_pulse_ns, time.monotonic_ns())
            self.client.publish(self.state_topic("power"), str(self.watts), retain=True)
        self.ref_pulse_ns = last_pulse_ns

class Watcher:
    def __init__(self):
        self.shutdown_requested = False
        self.impulses = 0
        self.condition = threading.Condition()
        self.worker_completed = False
        self.last_pulse_ns = None

    def acquire_lock(self):
        fd = os.open(LOCKFILE, os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            sys.exit("It seems another instance is already running")

    def request_shutdown(self):
        with self.condition:
            self.shutdown_requested = True
            self.condition.notify()

    def signal_handler(self, sig, frame):
        self.request_shutdown()

    def worker(self, done_w, start):
        try:
            last_write_success = start
            next_write_at = start + TIMEOUT
            next_aggregate_at = start + AGGREGATE_INTERVAL
            next_energy_at = start

            db = connect_db()
            ha = HomeAssistant() if MQTT_HOST else None
            if not ha:
                logging.info("EKM_MQTT_HOST not set, MQTT disabled")
            try:
                while True:
                    with self.condition:
                        remaining = next_write_at - time.monotonic()
                        if not self.shutdown_requested and remaining > 0:
                            self.condition.wait(remaining)

                        impulses = self.impulses
                        last_pulse_ns = self.last_pulse_ns

                    now = time.monotonic()
                    next_write_at = next_deadline(next_write_at, TIMEOUT, now)
                    interval = now - last_write_success
                    fetch_total = ha and now >= next_energy_at
                    total = None

                    try:
                        with db:
                            db.execute(
                                "INSERT INTO usage (timestamp, interval, impulses) "
                                "VALUES (STRFTIME('%s', 'now'), ?, ?)", (interval, impulses)
                            )
                            if fetch_total:
                                total, = db.execute("SELECT SUM(impulses) FROM usage").fetchone()
                        logging.info("Recorded %s impulses in %s seconds", impulses, interval)
                        last_write_success = now
                        with self.condition:
                            self.impulses -= impulses
                        if fetch_total:
                            next_energy_at = next_deadline(next_energy_at, ENERGY_INTERVAL, now)
                        if ha:
                            ha.publish(impulses, last_pulse_ns, total)
                    except sqlite3.OperationalError as e:
                        logging.warning(
                            "SQLite: %s, %s", e,
                            "data lost" if self.shutdown_requested else "postponing update"
                        )

                    if self.shutdown_requested:
                        break

                    if now >= next_aggregate_at:
                        next_aggregate_at = next_deadline(next_aggregate_at, AGGREGATE_INTERVAL, now)
                        try:
                            aggregate(db)
                        except sqlite3.OperationalError as e:
                            logging.warning("SQLite: %s, aggregation skipped", e)
            finally:
                if ha:
                    ha.close()
                db.close()

            self.worker_completed = True
        finally:
            os.close(done_w)

    def run(self):
        self.acquire_lock()

        logging.info("Listening for pulses on %s line %s", GPIO_CHIP, GPIO)
        with gpiod.request_lines(
            GPIO_CHIP,
            consumer="ekm-meter-watcher",
            config={GPIO: gpiod.LineSettings(edge_detection=Edge.RISING, bias=Bias.DISABLED)}
        ) as line_request:
            start = time.monotonic()
            signal_r, signal_w = os.pipe2(os.O_NONBLOCK)
            worker_done_r, worker_done_w = os.pipe2(os.O_NONBLOCK)

            poller = select.poll()
            poller.register(line_request.fd, select.POLLIN)
            poller.register(signal_r, select.POLLIN)
            poller.register(worker_done_r, select.POLLIN)

            signal.signal(signal.SIGTERM, self.signal_handler)
            signal.signal(signal.SIGINT, self.signal_handler)
            signal.set_wakeup_fd(signal_w)

            threading.Thread(target=self.worker, args=(worker_done_w, start)).start()

            try:
                while True:
                    for fd, _ in poller.poll():
                        if fd == line_request.fd:
                            events = line_request.read_edge_events()
                            with self.condition:
                                self.impulses += len(events)
                                if events:
                                    self.last_pulse_ns = events[-1].timestamp_ns
                        elif fd == signal_r:
                            os.read(signal_r, 64)
                        elif fd == worker_done_r:
                            sys.exit(0 if self.worker_completed else 1)
            finally:
                signal.set_wakeup_fd(-1)
                os.close(signal_r)
                os.close(signal_w)
                os.close(worker_done_r)
                self.request_shutdown()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-level", default=os.environ.get("EKM_LOG_LEVEL", "WARNING"))
    parser.add_argument("--aggregate", action="store_true")
    args = parser.parse_args()
    logging.getLogger().setLevel(args.log_level)

    if args.aggregate:
        aggregate()
    else:
        Watcher().run()
