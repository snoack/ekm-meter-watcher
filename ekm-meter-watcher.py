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

import pigpio

DATABASE = "db"
LOCKFILE = "lock"
GPIO = 27
TIMEOUT = 10
AGGREGATE_AFTER_WEEKS = 6
AGGREGATE_BY_SECONDS = 3600

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
        PRAGMA journal_mode=WAL;
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

def acquire_lock():
    fd = os.open(LOCKFILE, os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        sys.exit("It seems another instance is already running")

def watch():
    acquire_lock()

    pi = pigpio.pi()
    if not pi.connected:
        sys.exit(1)

    db = connect_db()
    cb = pi.callback(GPIO)
    last_check = time.monotonic()
    last_tally = 0

    signal.signal(signal.SIGTERM, lambda sig, frame: None)

    while True:
        siginfo = signal.sigtimedwait([signal.SIGTERM, signal.SIGINT], TIMEOUT)
        this_check = time.monotonic()
        this_tally = cb.tally()
        interval = this_check - last_check
        impulses = this_tally - last_tally

        try:
            with db: db.execute("INSERT INTO usage (timestamp, interval, impulses) " +
                                "VALUES (STRFTIME('%s', 'now'), ?, ?)", (interval, impulses))
            logging.info("Recorded %s impulses in %s seconds", impulses, interval)
            last_check = this_check
            last_tally = this_tally
        except sqlite3.OperationalError as e:
            logging.warning("SQLite: %s%s", e, ", postponing update" if not siginfo else "")

        if siginfo:
            sig = signal.Signals(siginfo.si_signo).name
            logging.info("Terminating after receiving signal %s", sig)
            break

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
        watch()
