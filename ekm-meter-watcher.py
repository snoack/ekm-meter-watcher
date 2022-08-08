#!/usr/bin/env python3

import argparse
import fcntl
import sys
import time
import logging
import os
from datetime import datetime, timedelta, timezone
import sqlite3

import pigpio

DATABASE = "db"
LOCKFILE = "lock"
GPIO = 27
TIMEOUT = 10
AGGREGATE_AFTER_WEEKS = 6
AGGREGATE_BY_SECONDS = 3600

def connect_db():
    logging.info("Opening SQLite database at %s", os.path.abspath(DATABASE))
    db = sqlite3.connect(DATABASE)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS usage (
            time TIMESTAMP NOT NULL,
            interval REAL NOT NULL,
            impulses INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS time on usage (time);
        CREATE INDEX IF NOT EXISTS interval on usage (interval);""")
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

    while True:
        time.sleep(TIMEOUT)
        this_check = time.monotonic()
        this_tally = cb.tally()
        interval = this_check - last_check
        impulses = this_tally - last_tally

        try:
            with db: db.execute("INSERT INTO usage (time, interval, impulses) " +
                                "VALUES (DATETIME(), ?, ?)", (interval, impulses))
            logging.info("Recorded %s impulses in %s seconds", impulses, interval)
            last_check = this_check
            last_tally = this_tally
        except sqlite3.OperationalError as e:
            logging.warning("SQLite: %s, postponing update", e)

def aggregate():
    logging.info("Combining records older than %s weeks into %s second " +
                 "intervals", AGGREGATE_AFTER_WEEKS, AGGREGATE_BY_SECONDS)

    cutoff = datetime.now(tz=timezone.utc) - timedelta(weeks=AGGREGATE_AFTER_WEEKS)
    round_up_time = "DATETIME((STRFTIME('%s', time) / ?1 + 1) * ?1, 'unixepoch')"
    params = (AGGREGATE_BY_SECONDS, cutoff)

    with connect_db() as db:
        cursor = db.cursor()
        cursor.execute("INSERT INTO usage (time, interval, impulses) " +
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
