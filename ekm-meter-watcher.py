#!/usr/bin/env python3

import argparse
import fcntl
import sys
import time
import logging
import os
import signal
import sqlite3
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

def create_view(name, interval=None):
    time = "timestamp - (interval >> 1)"
    watts = "impulses * 3600 / 0.8 / interval"
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

class Watcher:
    def acquire_lock(self):
        fd = os.open(LOCKFILE, os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            sys.exit("It seems another instance is already running")

    def record_data(self, terminating=False):
        now = self.last_write_attempt = time.monotonic()
        interval = now - self.last_write_success
        try:
            with self.db as db:
                db.execute("INSERT INTO usage (timestamp, interval, impulses) " +
                           "VALUES (STRFTIME('%s', 'now'), ?, ?)", (interval, self.impulses))
            logging.info("Recorded %s impulses in %s seconds", self.impulses, interval)
            self.last_write_success = self.last_write_attempt
            self.impulses = 0
        except sqlite3.OperationalError as e:
            logging.warning("SQLite: %s%s", e, "" if terminating else ", postponing update")

    def signal_handler(self, sig, frame):
        logging.info("Terminating after receiving signal %s", signal.Signals(sig).name)
        self.record_data(terminating=True)
        sys.exit(0)

    def run(self):
        self.acquire_lock()

        logging.info("Listening for pulses on %s line %s", GPIO_CHIP, GPIO)
        line_request = gpiod.request_lines(
            GPIO_CHIP,
            consumer="ekm-meter-watcher",
            config={GPIO: gpiod.LineSettings(edge_detection=Edge.RISING, bias=Bias.DISABLED)}
        )

        self.db = connect_db()
        self.last_write_attempt = self.last_write_success = time.monotonic()
        self.impulses = 0

        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)

        while True:
            while line_request.wait_edge_events(max(0, self.last_write_attempt +
                                                       TIMEOUT - time.monotonic())):
                self.impulses += len(line_request.read_edge_events())
            self.record_data()

def aggregate():
    logging.info("Combining records older than %s weeks into %s second " +
                 "intervals", AGGREGATE_AFTER_WEEKS, AGGREGATE_BY_SECONDS)

    round_up_time = "(timestamp / ?1 + 1) * ?1"
    cutoff = (datetime.now() - timedelta(weeks=AGGREGATE_AFTER_WEEKS)).timestamp()
    params = (AGGREGATE_BY_SECONDS, cutoff)

    with connect_db() as db:
        cursor = db.cursor()
        cursor.execute("INSERT INTO usage (timestamp, interval, impulses) " +
                       "SELECT {} AS time_rounded_up, ?1, SUM(impulses) ".format(round_up_time) +
                       "FROM usage WHERE interval < ?1 AND time_rounded_up < ?2 " +
                       "GROUP BY time_rounded_up", params)
        logging.info("Inserted %s rows", cursor.rowcount)
        cursor.execute("DELETE FROM usage " +
                       "WHERE interval < ?1 AND {} < ?2".format(round_up_time), params)
        logging.info("Deleted %s rows", cursor.rowcount)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument("--aggregate", action="store_true")
    args = parser.parse_args()
    logging.getLogger().setLevel(args.log_level)

    if args.aggregate:
        aggregate()
    else:
        Watcher().run()
