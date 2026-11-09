import sqlite3
from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nim TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  phone TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('passenger','driver','admin')),
  plate TEXT,
  ktm_file TEXT, ktp_file TEXT, sim_file TEXT,
  phone_verified INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','suspended')),
  reject_reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS otps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  code_hash TEXT NOT NULL,
  expires_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  driver_id INTEGER NOT NULL REFERENCES users(id),
  origin_name TEXT NOT NULL, origin_lat REAL NOT NULL, origin_lng REAL NOT NULL,
  dest_name TEXT NOT NULL, dest_lat REAL NOT NULL, dest_lng REAL NOT NULL,
  depart_at TEXT NOT NULL,
  seats INTEGER NOT NULL CHECK (seats BETWEEN 1 AND 2),
  distance_km REAL NOT NULL,
  fare INTEGER NOT NULL,
  note TEXT,
  kind TEXT NOT NULL DEFAULT 'scheduled' CHECK (kind IN ('scheduled','instant')),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','cancelled','done')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id INTEGER NOT NULL REFERENCES trips(id),
  passenger_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'requested'
    CHECK (status IN ('requested','confirmed','paid','ongoing','done','cancelled','rejected')),
  fare INTEGER NOT NULL,
  commission INTEGER NOT NULL,
  driver_share INTEGER NOT NULL,
  order_id TEXT, pay_ref TEXT, qr_url TEXT, qr_string TEXT,
  paid_at TEXT, started_at TEXT, finished_at TEXT, payout_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (trip_id, passenger_id)
);
CREATE TABLE IF NOT EXISTS ratings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id INTEGER NOT NULL REFERENCES bookings(id),
  from_id INTEGER NOT NULL REFERENCES users(id),
  to_id INTEGER NOT NULL REFERENCES users(id),
  score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
  comment TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (booking_id, from_id)
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id INTEGER REFERENCES bookings(id),
  from_id INTEGER NOT NULL REFERENCES users(id),
  about_id INTEGER REFERENCES users(id),
  category TEXT NOT NULL,
  detail TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
  admin_note TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ride_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  passenger_id INTEGER NOT NULL REFERENCES users(id),
  pickup_name TEXT NOT NULL, pickup_lat REAL NOT NULL, pickup_lng REAL NOT NULL,
  dest_name TEXT NOT NULL, dest_lat REAL NOT NULL, dest_lng REAL NOT NULL,
  distance_km REAL NOT NULL,
  fare INTEGER NOT NULL,
  note TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','taken','cancelled','expired')),
  driver_id INTEGER REFERENCES users(id),
  trip_id INTEGER REFERENCES trips(id),
  booking_id INTEGER REFERENCES bookings(id),
  created_ts REAL NOT NULL,
  expires_ts REAL NOT NULL,
  taken_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_req_status ON ride_requests(status, expires_ts);
CREATE INDEX IF NOT EXISTS idx_trips_status_depart ON trips(status, depart_at);
CREATE INDEX IF NOT EXISTS idx_bookings_trip ON bookings(trip_id, status);
CREATE INDEX IF NOT EXISTS idx_bookings_pass ON bookings(passenger_id);
"""


def get_db():
    if "db" not in g:
        con = sqlite3.connect(current_app.config["DB_PATH"], timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        g.db = con
    return g.db


def close_db(_exc=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def init_db(app):
    con = sqlite3.connect(app.config["DB_PATH"])
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(trips)")]
    if "kind" not in cols:  # migrasi database lama (tanpa menghapus data)
        con.execute("ALTER TABLE trips ADD COLUMN kind TEXT NOT NULL DEFAULT 'scheduled'")
    con.commit()
    con.close()


def q(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def one(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def ex(sql, args=()):
    cur = get_db().execute(sql, args)
    return cur.lastrowid
