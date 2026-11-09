"""MASPUT - platform bikepooling untuk mahasiswa ITK (Flask + SQLite)."""
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import services as svc
from db import close_db, ex, get_db, init_db, one, q

load_dotenv()
WITA = timezone(timedelta(hours=8))
REQ_TTL = 15 * 60      # permintaan terbuka kedaluwarsa setelah 15 menit
PAY_WINDOW = 10 * 60   # batas bayar setelah pengemudi mengambil
ACTIVE = ("requested", "confirmed", "paid", "ongoing", "done")  # status yang memakai kursi
BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def now_wita():
    return datetime.now(WITA).replace(tzinfo=None)


def create_app():
    app = Flask(__name__)
    env = os.getenv("APP_ENV", "development")
    inst = os.path.join(BASE_DIR, "instance")
    os.makedirs(inst, exist_ok=True)

    secret = os.getenv("SECRET_KEY")
    if not secret:
        if env == "production":
            raise RuntimeError("SECRET_KEY wajib diisi di mode production.")
        kf = os.path.join(inst, "secret_key")
        if not os.path.exists(kf):
            with open(kf, "w") as f:
                f.write(secrets.token_hex(32))
        secret = open(kf).read().strip()

    app.config.update(
        APP_ENV=env,
        SECRET_KEY=secret,
        DB_PATH=os.getenv("DB_PATH", os.path.join(inst, "masput.db")),
        UPLOAD_DIR=os.getenv("UPLOAD_DIR", os.path.join(inst, "uploads")),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        OSRM_URL=os.getenv("OSRM_URL", "https://router.project-osrm.org"),
        OTP_PROVIDER=os.getenv("OTP_PROVIDER", "console"),
        FONNTE_TOKEN=os.getenv("FONNTE_TOKEN", ""),
        PAYMENT_MODE=os.getenv("PAYMENT_MODE", "simulation"),
        MIDTRANS_SERVER_KEY=os.getenv("MIDTRANS_SERVER_KEY", ""),
        MIDTRANS_PRODUCTION=os.getenv("MIDTRANS_PRODUCTION", "0") == "1",
        SCHEDULED_ENABLED=os.getenv("SCHEDULED_ENABLED", "0") == "1",  # model jadwal pengemudi (lama), mati secara bawaan
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(env == "production"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    if env == "production":
        if app.config["PAYMENT_MODE"] == "simulation":
            raise RuntimeError("PAYMENT_MODE=simulation tidak boleh dipakai di production.")
        if app.config["OTP_PROVIDER"] == "console":
            raise RuntimeError("OTP_PROVIDER=console tidak boleh dipakai di production.")
    if app.config["PAYMENT_MODE"] == "midtrans" and not app.config["MIDTRANS_SERVER_KEY"]:
        raise RuntimeError("MIDTRANS_SERVER_KEY belum diisi.")
    if app.config["OTP_PROVIDER"] == "fonnte" and not app.config["FONNTE_TOKEN"]:
        raise RuntimeError("FONNTE_TOKEN belum diisi.")

    init_db(app)
    app.teardown_appcontext(close_db)
    bootstrap_admin(app)
    register_hooks(app)
    register_routes(app)
    return app


def bootstrap_admin(app):
    nim, pw = os.getenv("ADMIN_NIM"), os.getenv("ADMIN_PASSWORD")
    if not (nim and pw):
        return
    with app.app_context():
        if len(pw) < 10:
            raise RuntimeError("ADMIN_PASSWORD minimal 10 karakter.")
        if not one("SELECT 1 FROM users WHERE nim=?", (nim,)):
            ex("""INSERT INTO users (nim,name,phone,password_hash,role,phone_verified,status)
                  VALUES (?,?,?,?,'admin',1,'approved')""",
               (nim, "Admin MASPUT", "-", generate_password_hash(pw)))


# ---------- Hooks, filter, keamanan ----------
def register_hooks(app):
    @app.template_filter("rp")
    def rp(v):
        return "Rp" + f"{int(v or 0):,}".replace(",", ".")

    @app.template_filter("waktu")
    def waktu(v):
        if not v:
            return "-"
        try:
            d = datetime.fromisoformat(str(v).replace("T", " "))
        except ValueError:
            return str(v)
        hari = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][d.weekday()]
        return f"{hari}, {d.day}/{d.month}/{d.year} {d:%H:%M}"

    STATUS_LABEL = {
        "requested": "Menunggu pengemudi", "confirmed": "Menunggu pembayaran",
        "paid": "Sudah dibayar", "ongoing": "Dalam perjalanan", "done": "Selesai",
        "cancelled": "Dibatalkan", "rejected": "Ditolak",
    }
    app.jinja_env.globals["status_label"] = lambda s: STATUS_LABEL.get(s, s)

    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = one("SELECT * FROM users WHERE id=?", (uid,))
            if g.user is None or g.user["status"] == "suspended":
                session.clear()
                g.user = None

    @app.before_request
    def csrf_protect():
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if request.endpoint == "midtrans_webhook":
                return
            token = session.get("csrf")
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
            if not token or not sent or not secrets.compare_digest(token, sent):
                abort(400, "Sesi formulir tidak valid. Muat ulang halaman lalu coba lagi.")

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return {"csrf": session["csrf"], "me": g.get("user"), "payment_mode": app.config["PAYMENT_MODE"],
                "scheduled": app.config["SCHEDULED_ENABLED"]}

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https:; "
            "script-src 'self' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; connect-src 'self'; frame-ancestors 'none'")
        return resp

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def err(e):
        code = getattr(e, "code", 500)
        msg = {413: "Berkas terlalu besar (maksimal 12 MB)."}.get(code, getattr(e, "description", ""))
        return render_template("error.html", code=code, msg=msg), code


# ---------- Dekorator ----------
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def ready_required(*roles):
    """Wajib login, nomor terverifikasi, dan disetujui admin."""
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = g.user
            if not u:
                return redirect(url_for("login", next=request.path))
            if u["role"] != "admin":
                if not u["phone_verified"]:
                    return redirect(url_for("verify"))
                if u["status"] != "approved":
                    return redirect(url_for("account_status"))
            if roles and u["role"] not in roles:
                abort(403, "Halaman ini tidak tersedia untuk akun Anda.")
            return f(*a, **k)
        return w
    return deco


# ---------- Utilitas ----------
_attempts = {}


def too_many_logins(key, limit=5, window=600):
    now = time.time()
    lst = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = lst
    return len(lst) >= limit


def record_failed(key):
    _attempts.setdefault(key, []).append(time.time())


def sniff_image(head: bytes):
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def save_upload(field, kind, app, required=False):
    f = request.files.get(field)
    if not f or not f.filename:
        return (None, f"Berkas {kind.upper()} wajib diunggah.") if required else (None, None)
    head = f.stream.read(16)
    f.stream.seek(0)
    ext = sniff_image(head)
    if not ext:
        return None, f"{kind.upper()}: gunakan foto JPG, PNG, atau WEBP."
    d = os.path.join(app.config["UPLOAD_DIR"], kind)
    os.makedirs(d, exist_ok=True)
    name = secrets.token_hex(16) + ext
    f.save(os.path.join(d, name))
    return f"{kind}/{name}", None


def seats_left(trip_id, seats):
    used = one(f"SELECT COUNT(*) c FROM bookings WHERE trip_id=? AND status IN ({','.join('?'*len(ACTIVE))})",
               (trip_id, *ACTIVE))["c"]
    return seats - used


def rating_of(uid):
    r = one("SELECT AVG(score) a, COUNT(*) n FROM ratings WHERE to_id=?", (uid,))
    return (round(r["a"], 1) if r["a"] else None, r["n"])


def parse_coord(name, lo, hi):
    try:
        v = float(request.form.get(name, ""))
    except ValueError:
        return None
    return v if lo <= v <= hi else None


# ---------- Rute ----------
def register_routes(app):

    # === Beranda & pencarian ===
    @app.route("/")
    def index():
        qtext = (request.args.get("q") or "").strip()
        date = (request.args.get("tanggal") or "").strip()
        sql = """SELECT t.*, u.name driver_name, u.plate,
                 (SELECT COUNT(*) FROM bookings b WHERE b.trip_id=t.id AND b.status IN ('requested','confirmed','paid','ongoing','done')) used
                 FROM trips t JOIN users u ON u.id=t.driver_id
                 WHERE t.status='open' AND t.kind='scheduled' AND t.depart_at >= ?"""
        args = [now_wita().strftime("%Y-%m-%d %H:%M")]
        if qtext:
            sql += " AND (t.origin_name LIKE ? OR t.dest_name LIKE ?)"
            args += [f"%{qtext}%"] * 2
        if date:
            sql += " AND substr(t.depart_at,1,10)=?"
            args.append(date)
        sql += " ORDER BY t.depart_at LIMIT 60"
        trips = [dict(r, left=r["seats"] - r["used"], rating=rating_of(r["driver_id"]))
                 for r in q(sql, args)]
        trips = [t for t in trips if t["left"] > 0]
        return render_template("index.html", trips=trips, qtext=qtext, date=date)

    # === Auth ===
    @app.route("/daftar", methods=["GET", "POST"])
    def register():
        if g.user:
            return redirect(url_for("index"))
        if request.method == "POST":
            f = request.form
            nim, name = f.get("nim", "").strip(), f.get("name", "").strip()
            phone = svc.normalize_phone(f.get("phone", ""))
            pw, role = f.get("password", ""), f.get("role", "passenger")
            plate = re.sub(r"\s+", " ", f.get("plate", "").strip().upper())
            errs = []
            if not re.fullmatch(r"\d{8,12}", nim):
                errs.append("NIM harus 8-12 digit angka.")
            if len(name) < 3:
                errs.append("Nama lengkap wajib diisi.")
            if not phone:
                errs.append("Nomor WhatsApp tidak valid (contoh: 08123456789).")
            if len(pw) < 8:
                errs.append("Kata sandi minimal 8 karakter.")
            if role not in ("passenger", "driver"):
                errs.append("Pilih peran akun.")
            if role == "driver" and not re.fullmatch(r"[A-Z]{1,2} ?\d{1,4} ?[A-Z]{0,3}", plate):
                errs.append("Plat nomor tidak valid (contoh: KT 1234 AB).")
            if one("SELECT 1 FROM users WHERE nim=?", (nim,)):
                errs.append("NIM ini sudah terdaftar. Silakan masuk.")
            ktm, e1 = save_upload("ktm", "ktm", app, required=True)
            ktp, e2 = save_upload("ktp", "ktp", app, required=(role == "driver"))
            sim, e3 = save_upload("sim", "sim", app, required=(role == "driver"))
            errs += [e for e in (e1, e2, e3) if e]
            if errs:
                for p in (ktm, ktp, sim):
                    if p:
                        try:
                            os.remove(os.path.join(app.config["UPLOAD_DIR"], p))
                        except OSError:
                            pass
                for e in errs:
                    flash(e, "error")
                return render_template("register.html", f=f), 400
            uid = ex("""INSERT INTO users (nim,name,phone,password_hash,role,plate,ktm_file,ktp_file,sim_file)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (nim, name, phone, generate_password_hash(pw), role,
                      plate if role == "driver" else None, ktm, ktp, sim))
            session.clear()
            session["uid"] = uid
            session.permanent = True
            issue_otp(uid, phone)
            flash("Akun dibuat. Masukkan kode yang dikirim ke WhatsApp Anda.", "ok")
            return redirect(url_for("verify"))
        return render_template("register.html", f={})

    def issue_otp(uid, phone):
        last = one("SELECT created_at FROM otps WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
        if last and time.time() - last["created_at"] < 60:
            return False
        code = svc.new_otp_code()
        ex("DELETE FROM otps WHERE user_id=?", (uid,))
        ex("INSERT INTO otps (user_id,code_hash,expires_at,created_at) VALUES (?,?,?,?)",
           (uid, svc.hash_otp(code), time.time() + 300, time.time()))
        ok = svc.send_whatsapp(phone, f"MASPUT: kode verifikasi Anda {code}. Berlaku 5 menit. Jangan bagikan kode ini.")
        if not ok:
            flash("Kode gagal dikirim. Coba kirim ulang sebentar lagi.", "error")
        return ok

    @app.route("/verifikasi", methods=["GET", "POST"])
    @login_required
    def verify():
        u = g.user
        if u["phone_verified"]:
            return redirect(url_for("account_status"))
        if request.method == "POST":
            if request.form.get("aksi") == "kirim_ulang":
                if issue_otp(u["id"], u["phone"]):
                    flash("Kode baru dikirim.", "ok")
                else:
                    flash("Tunggu 1 menit sebelum meminta kode baru.", "error")
                return redirect(url_for("verify"))
            code = re.sub(r"\D", "", request.form.get("code", ""))
            rec = one("SELECT * FROM otps WHERE user_id=? ORDER BY id DESC LIMIT 1", (u["id"],))
            if not rec or rec["expires_at"] < time.time():
                flash("Kode kedaluwarsa. Kirim ulang kode.", "error")
            elif rec["attempts"] >= 5:
                flash("Terlalu banyak percobaan. Kirim ulang kode.", "error")
            elif secrets.compare_digest(rec["code_hash"], svc.hash_otp(code)):
                ex("UPDATE users SET phone_verified=1 WHERE id=?", (u["id"],))
                ex("DELETE FROM otps WHERE user_id=?", (u["id"],))
                flash("Nomor terverifikasi. Admin akan memeriksa berkas Anda.", "ok")
                return redirect(url_for("account_status"))
            else:
                ex("UPDATE otps SET attempts=attempts+1 WHERE id=?", (rec["id"],))
                flash("Kode salah.", "error")
            return redirect(url_for("verify"))
        return render_template("verify.html", phone_tail=u["phone"][-4:])

    @app.route("/status")
    @login_required
    def account_status():
        if not g.user["phone_verified"]:
            return redirect(url_for("verify"))
        if g.user["status"] == "approved":
            return redirect(url_for("index"))
        return render_template("status.html")

    @app.route("/masuk", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("index"))
        if request.method == "POST":
            nim, pw = request.form.get("nim", "").strip(), request.form.get("password", "")
            key = (request.remote_addr, nim)
            if too_many_logins(key):
                flash("Terlalu banyak percobaan. Coba lagi dalam 10 menit.", "error")
                return render_template("login.html"), 429
            u = one("SELECT * FROM users WHERE nim=?", (nim,))
            if u and check_password_hash(u["password_hash"], pw):
                if u["status"] == "suspended":
                    flash("Akun ini ditangguhkan. Hubungi admin.", "error")
                    return render_template("login.html"), 403
                session.clear()
                session["uid"] = u["id"]
                session.permanent = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
            record_failed(key)
            flash("NIM atau kata sandi salah.", "error")
            return render_template("login.html"), 401
        return render_template("login.html")

    @app.route("/keluar", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("index"))

    # === Rute peta ===
    @app.route("/api/rute")
    @ready_required()
    def api_route():
        try:
            a = [float(request.args[k]) for k in ("olat", "olng", "dlat", "dlng")]
        except (KeyError, ValueError):
            abort(400, "Koordinat tidak lengkap.")
        info = svc.route_info(*a, geometry=True)
        return jsonify(km=info["km"], fare=svc.calc_fare(info["km"]),
                       source=info["source"], coords=info["coords"])

    # === Perjalanan ===
    @app.route("/perjalanan/baru", methods=["GET", "POST"])
    @ready_required("driver")
    def trip_new():
        if not app.config["SCHEDULED_ENABLED"]:
            abort(404, "Pengemudi tidak membuat perjalanan sendiri. Ambil permintaan penumpang di menu Permintaan masuk.")
        if request.method == "POST":
            f = request.form
            oname, dname = f.get("origin_name", "").strip()[:80], f.get("dest_name", "").strip()[:80]
            olat, olng = parse_coord("olat", -90, 90), parse_coord("olng", -180, 180)
            dlat, dlng = parse_coord("dlat", -90, 90), parse_coord("dlng", -180, 180)
            seats = f.get("seats", "1")
            note = f.get("note", "").strip()[:200]
            errs = []
            try:
                depart = datetime.strptime(f.get("depart_at", ""), "%Y-%m-%dT%H:%M")
                if depart < now_wita() + timedelta(minutes=10):
                    errs.append("Jam berangkat minimal 10 menit dari sekarang.")
                if depart > now_wita() + timedelta(days=30):
                    errs.append("Jadwal maksimal 30 hari ke depan.")
            except ValueError:
                depart = None
                errs.append("Jam berangkat tidak valid.")
            if not oname or not dname:
                errs.append("Nama titik asal dan tujuan wajib diisi.")
            if None in (olat, olng, dlat, dlng):
                errs.append("Pilih titik asal dan tujuan di peta.")
            if seats not in ("1", "2"):
                errs.append("Jumlah boncengan 1 atau 2.")
            if not errs and svc.haversine_km(olat, olng, dlat, dlng) < 0.2:
                errs.append("Titik asal dan tujuan terlalu dekat.")
            if errs:
                for e in errs:
                    flash(e, "error")
                return render_template("trip_new.html", f=f), 400
            info = svc.route_info(olat, olng, dlat, dlng)  # jarak & tarif dihitung server, bukan dari browser
            fare = svc.calc_fare(info["km"])
            tid = ex("""INSERT INTO trips (driver_id,origin_name,origin_lat,origin_lng,dest_name,dest_lat,dest_lng,
                        depart_at,seats,distance_km,fare,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (g.user["id"], oname, olat, olng, dname, dlat, dlng,
                      depart.strftime("%Y-%m-%d %H:%M"), int(seats), info["km"], fare, note))
            flash(f"Perjalanan dibuat. Jarak {info['km']} km, tarif {fare:,}".replace(",", "."), "ok")
            return redirect(url_for("trip_detail", tid=tid))
        return render_template("trip_new.html", f={})

    @app.route("/perjalanan/<int:tid>")
    @ready_required()
    def trip_detail(tid):
        t = one("""SELECT t.*, u.name driver_name, u.plate FROM trips t JOIN users u ON u.id=t.driver_id
                   WHERE t.id=?""", (tid,))
        if not t:
            abort(404, "Perjalanan tidak ditemukan.")
        if (t["kind"] == "instant" and g.user["role"] != "admin" and g.user["id"] != t["driver_id"]
                and not one("SELECT 1 FROM bookings WHERE trip_id=? AND passenger_id=?", (tid, g.user["id"]))):
            abort(404, "Perjalanan tidak ditemukan.")
        left = seats_left(tid, t["seats"])
        mine = one("SELECT * FROM bookings WHERE trip_id=? AND passenger_id=?", (tid, g.user["id"]))
        is_owner = g.user["id"] == t["driver_id"]
        passengers = q("""SELECT b.*, u.name pname, u.phone pphone FROM bookings b JOIN users u ON u.id=b.passenger_id
                          WHERE b.trip_id=? AND b.status NOT IN ('cancelled','rejected') ORDER BY b.id""",
                       (tid,)) if is_owner else []
        return render_template("trip_detail.html", t=t, left=left, mine=mine, is_owner=is_owner,
                               passengers=passengers, rating=rating_of(t["driver_id"]),
                               expired=t["depart_at"] < now_wita().strftime("%Y-%m-%d %H:%M"))

    @app.route("/perjalanan/<int:tid>/pesan", methods=["POST"])
    @ready_required("passenger", "driver")
    def book(tid):
        if not app.config["SCHEDULED_ENABLED"]:
            abort(404)
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            t = db.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
            if not t or t["status"] != "open" or t["kind"] != "scheduled":
                raise ValueError("Perjalanan tidak tersedia.")
            if t["driver_id"] == g.user["id"]:
                raise ValueError("Anda tidak dapat memesan perjalanan sendiri.")
            if t["depart_at"] < now_wita().strftime("%Y-%m-%d %H:%M"):
                raise ValueError("Perjalanan ini sudah lewat.")
            used = db.execute(f"SELECT COUNT(*) c FROM bookings WHERE trip_id=? AND status IN ({','.join('?'*len(ACTIVE))})",
                              (tid, *ACTIVE)).fetchone()["c"]
            if used >= t["seats"]:
                raise ValueError("Kursi sudah penuh.")
            commission, share = svc.split_fare(t["fare"])
            old = db.execute("SELECT * FROM bookings WHERE trip_id=? AND passenger_id=?", (tid, g.user["id"])).fetchone()
            if old and old["status"] not in ("cancelled", "rejected"):
                raise ValueError("Anda sudah memesan perjalanan ini.")
            if old:
                db.execute("""UPDATE bookings SET status='requested',fare=?,commission=?,driver_share=?,order_id=NULL,
                              qr_url=NULL,qr_string=NULL,created_at=CURRENT_TIMESTAMP WHERE id=?""",
                           (t["fare"], commission, share, old["id"]))
            else:
                db.execute("INSERT INTO bookings (trip_id,passenger_id,fare,commission,driver_share) VALUES (?,?,?,?,?)",
                           (tid, g.user["id"], t["fare"], commission, share))
            db.execute("COMMIT")
            flash("Pesanan dikirim. Tunggu pengemudi mengonfirmasi.", "ok")
        except ValueError as e:
            db.execute("ROLLBACK")
            flash(str(e), "error")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return redirect(url_for("my_bookings"))

    @app.route("/perjalanan/<int:tid>/batal", methods=["POST"])
    @ready_required("driver")
    def trip_cancel(tid):
        t = one("SELECT * FROM trips WHERE id=? AND driver_id=?", (tid, g.user["id"]))
        if not t:
            abort(404)
        if one("SELECT 1 FROM bookings WHERE trip_id=? AND status IN ('paid','ongoing')", (tid,)):
            flash("Ada penumpang yang sudah membayar. Selesaikan perjalanan atau hubungi admin.", "error")
            return redirect(url_for("trip_detail", tid=tid))
        ex("UPDATE trips SET status='cancelled' WHERE id=?", (tid,))
        ex("UPDATE bookings SET status='cancelled' WHERE trip_id=? AND status IN ('requested','confirmed')", (tid,))
        req = one("SELECT id FROM ride_requests WHERE trip_id=? AND status='taken'", (tid,))
        if req:
            reopen_request(req["id"])
        flash("Perjalanan dibatalkan.", "ok")
        return redirect(url_for("my_trips"))

    # === Daftar pesanan ===
    @app.route("/pesanan")
    @ready_required("passenger", "driver")
    def my_bookings():
        sweep_instant()
        rows = q("""SELECT b.*, t.origin_name, t.dest_name, t.depart_at, t.distance_km, u.name driver_name, u.phone driver_phone,
                    u.plate, t.driver_id,
                    (SELECT 1 FROM ratings r WHERE r.booking_id=b.id AND r.from_id=b.passenger_id) rated
                    FROM bookings b JOIN trips t ON t.id=b.trip_id JOIN users u ON u.id=t.driver_id
                    WHERE b.passenger_id=? ORDER BY b.id DESC""", (g.user["id"],))
        return render_template("my_bookings.html", rows=rows)

    @app.route("/jadwal-saya")
    @ready_required("driver")
    def my_trips():
        sweep_instant()
        trips = q("SELECT * FROM trips WHERE driver_id=? ORDER BY depart_at DESC", (g.user["id"],))
        out = []
        for t in trips:
            bs = q("""SELECT b.*, u.name pname, u.phone pphone,
                      (SELECT 1 FROM ratings r WHERE r.booking_id=b.id AND r.from_id=?) rated
                      FROM bookings b JOIN users u ON u.id=b.passenger_id
                      WHERE b.trip_id=? AND b.status NOT IN ('cancelled','rejected') ORDER BY b.id""",
                   (g.user["id"], t["id"]))
            out.append({"t": t, "bs": bs, "left": seats_left(t["id"], t["seats"])})
        earn = one("""SELECT COALESCE(SUM(b.driver_share),0) s FROM bookings b JOIN trips t ON t.id=b.trip_id
                      WHERE t.driver_id=? AND b.status='done'""", (g.user["id"],))["s"]
        pending = one("""SELECT COALESCE(SUM(b.driver_share),0) s FROM bookings b JOIN trips t ON t.id=b.trip_id
                         WHERE t.driver_id=? AND b.status='done' AND b.payout_at IS NULL""", (g.user["id"],))["s"]
        return render_template("my_trips.html", items=out, earn=earn, pending=pending)

    @app.route("/pesanan/<int:bid>/aksi", methods=["POST"])
    @ready_required("passenger", "driver")
    def booking_action(bid):
        b = one("""SELECT b.*, t.driver_id, t.depart_at FROM bookings b JOIN trips t ON t.id=b.trip_id WHERE b.id=?""", (bid,))
        if not b:
            abort(404)
        act, uid = request.form.get("aksi"), g.user["id"]
        is_driver, is_pass = uid == b["driver_id"], uid == b["passenger_id"]
        if not (is_driver or is_pass):
            abort(403)
        s = b["status"]
        # (aksi, pelaku, status sebelumnya) -> status baru
        rules = {
            ("terima", "driver", "requested"): "confirmed",
            ("tolak", "driver", "requested"): "rejected",
            ("tolak", "driver", "confirmed"): "rejected",
            ("batal", "passenger", "requested"): "cancelled",
            ("batal", "passenger", "confirmed"): "cancelled",
            ("mulai", "driver", "paid"): "ongoing",
            ("selesai", "driver", "ongoing"): "done",
        }
        who = "driver" if is_driver else "passenger"
        new = rules.get((act, who, s))
        if not new:
            flash("Aksi tidak dapat dilakukan pada status ini.", "error")
        else:
            extra = ""
            if new == "ongoing":
                extra = ", started_at=datetime('now','+8 hours')"
            if new == "done":
                extra = ", finished_at=datetime('now','+8 hours')"
            ex(f"UPDATE bookings SET status=?{extra} WHERE id=? AND status=?", (new, bid, s))
            if new in ("cancelled", "rejected"):
                req = one("SELECT id FROM ride_requests WHERE booking_id=? AND status='taken'", (bid,))
                if req:
                    ex("UPDATE trips SET status='cancelled' WHERE id=?", (b["trip_id"],))
                    if new == "cancelled":   # penumpang membatalkan
                        ex("UPDATE ride_requests SET status='cancelled' WHERE id=?", (req["id"],))
                    else:                    # pengemudi mundur: permintaan dibuka lagi untuk pengemudi lain
                        reopen_request(req["id"])
            flash("Status pesanan diperbarui.", "ok")
        return redirect(request.referrer or url_for("my_bookings"))

    # === Pembayaran ===
    def mark_paid(bid, ref=None):
        ex("""UPDATE bookings SET status='paid', paid_at=datetime('now','+8 hours'), pay_ref=COALESCE(?,pay_ref)
              WHERE id=? AND status='confirmed'""", (ref, bid))

    @app.route("/pesanan/<int:bid>/bayar", methods=["GET", "POST"])
    @ready_required("passenger", "driver")
    def pay(bid):
        sweep_instant()
        b = one("""SELECT b.*, t.origin_name, t.dest_name, t.depart_at, t.kind FROM bookings b JOIN trips t ON t.id=b.trip_id
                   WHERE b.id=? AND b.passenger_id=?""", (bid, g.user["id"]))
        if not b:
            abort(404)
        if b["status"] == "paid":
            flash("Pembayaran sudah diterima.", "ok")
            return redirect(url_for("my_bookings"))
        if b["status"] != "confirmed":
            flash("Pesanan ini belum bisa dibayar.", "error")
            return redirect(url_for("my_bookings"))
        if not b["order_id"] or request.method == "POST" and request.form.get("aksi") == "buat_ulang":
            order_id = f"MASPUT-{bid}-{int(time.time())}"
            res = svc.create_qris(order_id, b["fare"], expiry_min=9 if b["kind"] == "instant" else None)
            if not res["ok"]:
                flash(res["error"], "error")
                return redirect(url_for("my_bookings"))
            ex("UPDATE bookings SET order_id=?, qr_url=?, qr_string=? WHERE id=?",
               (order_id, res["qr_url"], res["qr_string"], bid))
            b = one("""SELECT b.*, t.origin_name, t.dest_name, t.depart_at, t.kind FROM bookings b JOIN trips t ON t.id=b.trip_id
                       WHERE b.id=?""", (bid,))
        deadline = None
        if b["kind"] == "instant":
            rq = one("SELECT taken_ts FROM ride_requests WHERE booking_id=?", (bid,))
            if rq and rq["taken_ts"]:
                deadline = datetime.fromtimestamp(rq["taken_ts"] + PAY_WINDOW, WITA).strftime("%H:%M")
        return render_template("pay.html", b=b, deadline=deadline)

    @app.route("/pesanan/<int:bid>/cek-bayar", methods=["POST"])
    @ready_required("passenger", "driver")
    def pay_check(bid):
        b = one("SELECT * FROM bookings WHERE id=? AND passenger_id=?", (bid, g.user["id"]))
        if not b:
            abort(404)
        if app.config["PAYMENT_MODE"] == "midtrans" and b["order_id"] and b["status"] == "confirmed":
            d = svc.midtrans_status(b["order_id"])
            if d and is_valid_payment(d, b):
                mark_paid(bid, d.get("transaction_id"))
                flash("Pembayaran diterima.", "ok")
                return redirect(url_for("my_bookings"))
            flash("Pembayaran belum terdeteksi. Coba lagi beberapa detik setelah membayar.", "error")
        return redirect(url_for("pay", bid=bid))

    def is_valid_payment(d, b):
        try:
            amount_ok = int(float(d.get("gross_amount", 0))) == b["fare"]
        except ValueError:
            amount_ok = False
        return svc.is_paid_status(d) and d.get("order_id") == b["order_id"] and amount_ok

    @app.route("/pesanan/<int:bid>/simulasi-bayar", methods=["POST"])
    @ready_required("passenger", "driver")
    def pay_simulate(bid):
        if app.config["PAYMENT_MODE"] != "simulation":
            abort(404)
        b = one("SELECT * FROM bookings WHERE id=? AND passenger_id=?", (bid, g.user["id"]))
        if not b:
            abort(404)
        mark_paid(bid, "SIMULASI")
        flash("Pembayaran simulasi berhasil.", "ok")
        return redirect(url_for("my_bookings"))

    @app.route("/webhook/midtrans", methods=["POST"])
    def midtrans_webhook():
        d = request.get_json(silent=True) or {}
        if app.config["PAYMENT_MODE"] != "midtrans" or not svc.midtrans_signature_ok(d):
            return "invalid", 403
        b = one("SELECT * FROM bookings WHERE order_id=?", (d.get("order_id"),))
        if b and is_valid_payment(d, b):
            if b["status"] == "confirmed":
                mark_paid(b["id"], d.get("transaction_id"))
            elif b["status"] in ("cancelled", "rejected"):
                # uang masuk untuk pesanan yang sudah batal: catat agar admin melakukan refund
                detail = f"Pembayaran {d.get('order_id')} sebesar {b['fare']} masuk setelah pesanan batal. Perlu refund."
                if not one("SELECT 1 FROM reports WHERE detail=?", (detail,)):
                    ex("INSERT INTO reports (booking_id,from_id,category,detail) VALUES (?,?,?,?)",
                       (b["id"], b["passenger_id"], "tarif", detail))
        return "ok"

    # === Rating & laporan ===
    @app.route("/pesanan/<int:bid>/nilai", methods=["GET", "POST"])
    @ready_required("passenger", "driver")
    def rate(bid):
        b = one("""SELECT b.*, t.driver_id, t.origin_name, t.dest_name FROM bookings b JOIN trips t ON t.id=b.trip_id
                   WHERE b.id=?""", (bid,))
        if not b or g.user["id"] not in (b["passenger_id"], b["driver_id"]):
            abort(404)
        if b["status"] != "done":
            flash("Penilaian dibuka setelah perjalanan selesai.", "error")
            return redirect(url_for("index"))
        target = b["driver_id"] if g.user["id"] == b["passenger_id"] else b["passenger_id"]
        tu = one("SELECT name FROM users WHERE id=?", (target,))
        if one("SELECT 1 FROM ratings WHERE booking_id=? AND from_id=?", (bid, g.user["id"])):
            flash("Anda sudah menilai perjalanan ini.", "ok")
            return redirect(url_for("index"))
        if request.method == "POST":
            try:
                score = int(request.form.get("score", ""))
            except ValueError:
                score = 0
            if not 1 <= score <= 5:
                flash("Pilih nilai 1 sampai 5.", "error")
            else:
                ex("INSERT INTO ratings (booking_id,from_id,to_id,score,comment) VALUES (?,?,?,?,?)",
                   (bid, g.user["id"], target, score, request.form.get("comment", "").strip()[:300]))
                flash("Terima kasih atas penilaian Anda.", "ok")
                return redirect(url_for("my_bookings" if g.user["id"] == b["passenger_id"] else "my_trips"))
        return render_template("rate.html", b=b, target_name=tu["name"])

    @app.route("/pesanan/<int:bid>/lapor", methods=["GET", "POST"])
    @ready_required("passenger", "driver")
    def report(bid):
        b = one("SELECT b.*, t.driver_id FROM bookings b JOIN trips t ON t.id=b.trip_id WHERE b.id=?", (bid,))
        if not b or g.user["id"] not in (b["passenger_id"], b["driver_id"]):
            abort(404)
        about = b["driver_id"] if g.user["id"] == b["passenger_id"] else b["passenger_id"]
        if request.method == "POST":
            cat, detail = request.form.get("category", ""), request.form.get("detail", "").strip()
            if cat not in ("keselamatan", "pelecehan", "tarif", "tidak-hadir", "lainnya") or len(detail) < 10:
                flash("Pilih kategori dan jelaskan masalahnya (minimal 10 karakter).", "error")
            else:
                ex("INSERT INTO reports (booking_id,from_id,about_id,category,detail) VALUES (?,?,?,?,?)",
                   (bid, g.user["id"], about, cat, detail[:1000]))
                flash("Laporan terkirim. Admin akan menindaklanjuti.", "ok")
                return redirect(url_for("my_bookings" if g.user["id"] == b["passenger_id"] else "my_trips"))
        return render_template("report.html", b=b)

    # === Permintaan tumpangan (model Maxim) ===
    def reopen_request(rid):
        ex("""UPDATE ride_requests SET status='open', driver_id=NULL, trip_id=NULL, booking_id=NULL,
              taken_ts=NULL, expires_ts=? WHERE id=?""", (time.time() + REQ_TTL, rid))

    def sweep_instant():
        """Tidak ada penjadwal latar belakang, jadi kedaluwarsa dicek setiap halaman terkait dibuka."""
        now = time.time()
        ex("UPDATE ride_requests SET status='expired' WHERE status='open' AND expires_ts<?", (now,))
        rows = q("""SELECT r.id rid, r.booking_id, r.trip_id FROM ride_requests r JOIN bookings b ON b.id=r.booking_id
                    WHERE r.status='taken' AND b.status='confirmed' AND r.taken_ts<?""", (now - PAY_WINDOW,))
        for r in rows:
            ex("UPDATE bookings SET status='cancelled' WHERE id=? AND status='confirmed'", (r["booking_id"],))
            ex("UPDATE trips SET status='cancelled' WHERE id=?", (r["trip_id"],))
            reopen_request(r["rid"])

    def active_request(uid):
        return one("""SELECT r.* FROM ride_requests r LEFT JOIN bookings b ON b.id=r.booking_id
                      WHERE r.passenger_id=? AND (r.status='open' OR (r.status='taken' AND b.status IN ('confirmed','paid','ongoing')))
                      ORDER BY r.id DESC LIMIT 1""", (uid,))

    def hhmm(ts):
        return datetime.fromtimestamp(ts, WITA).strftime("%H:%M")

    @app.route("/minta", methods=["GET", "POST"])
    @ready_required("passenger", "driver")
    def request_new():
        sweep_instant()
        act = active_request(g.user["id"])
        if act:
            flash("Anda masih punya permintaan yang berjalan.", "error")
            return redirect(url_for("request_status", rid=act["id"]))
        if request.method == "POST":
            f = request.form
            pname, dname = f.get("pickup_name", "").strip()[:80], f.get("dest_name", "").strip()[:80]
            olat, olng = parse_coord("olat", -90, 90), parse_coord("olng", -180, 180)
            dlat, dlng = parse_coord("dlat", -90, 90), parse_coord("dlng", -180, 180)
            note = f.get("note", "").strip()[:200]
            errs = []
            if not pname or not dname:
                errs.append("Nama titik jemput dan tujuan wajib diisi.")
            if None in (olat, olng, dlat, dlng):
                errs.append("Pilih titik jemput dan tujuan di peta.")
            else:
                straight = svc.haversine_km(olat, olng, dlat, dlng)
                if straight < 0.2:
                    errs.append("Titik jemput dan tujuan terlalu dekat.")
                if straight > 40:
                    errs.append("Jarak terlalu jauh untuk layanan ini (maksimal sekitar 40 km).")
            if errs:
                for e in errs:
                    flash(e, "error")
                return render_template("request_new.html", f=f), 400
            info = svc.route_info(olat, olng, dlat, dlng)  # tarif dihitung server
            fare = svc.calc_fare(info["km"])
            now = time.time()
            rid = ex("""INSERT INTO ride_requests (passenger_id,pickup_name,pickup_lat,pickup_lng,dest_name,dest_lat,dest_lng,
                        distance_km,fare,note,created_ts,expires_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (g.user["id"], pname, olat, olng, dname, dlat, dlng, info["km"], fare, note, now, now + REQ_TTL))
            flash("Permintaan dikirim. Tunggu pengemudi mengambilnya.", "ok")
            return redirect(url_for("request_status", rid=rid))
        return render_template("request_new.html", f={})

    @app.route("/minta/<int:rid>")
    @ready_required("passenger", "driver")
    def request_status(rid):
        sweep_instant()
        r = one("SELECT * FROM ride_requests WHERE id=? AND passenger_id=?", (rid, g.user["id"]))
        if not r:
            abort(404, "Permintaan tidak ditemukan.")
        b = drv = None
        if r["booking_id"]:
            b = one("SELECT * FROM bookings WHERE id=?", (r["booking_id"],))
            drv = one("SELECT name, plate, phone FROM users WHERE id=?", (r["driver_id"],))
        rating = rating_of(r["driver_id"]) if r["driver_id"] else (None, 0)
        return render_template("request_status.html", r=r, b=b, drv=drv, rating=rating,
                               expires=hhmm(r["expires_ts"]),
                               pay_by=hhmm(r["taken_ts"] + PAY_WINDOW) if r["taken_ts"] else None)

    @app.route("/minta/<int:rid>/batal", methods=["POST"])
    @ready_required("passenger", "driver")
    def request_cancel(rid):
        r = one("SELECT * FROM ride_requests WHERE id=? AND passenger_id=?", (rid, g.user["id"]))
        if not r:
            abort(404)
        if r["status"] == "open":
            ex("UPDATE ride_requests SET status='cancelled' WHERE id=? AND status='open'", (rid,))
            flash("Permintaan dibatalkan.", "ok")
        else:
            flash("Permintaan ini sudah diambil pengemudi. Batalkan lewat Pesanan saya.", "error")
        return redirect(url_for("request_status", rid=rid))

    @app.route("/permintaan")
    @ready_required("driver")
    def requests_open():
        sweep_instant()
        try:
            lat, lng = float(request.args["lat"]), float(request.args["lng"])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError
        except (KeyError, ValueError):
            lat = lng = None
        rows = []
        for r in q("""SELECT r.*, u.name pname FROM ride_requests r JOIN users u ON u.id=r.passenger_id
                     WHERE r.status='open' AND r.passenger_id!=? ORDER BY r.id""", (g.user["id"],)):
            d = dict(r)
            d["away"] = round(svc.haversine_km(lat, lng, r["pickup_lat"], r["pickup_lng"]), 1) if lat is not None else None
            d["share"] = svc.split_fare(r["fare"])[1]
            d["prating"] = rating_of(r["passenger_id"])
            rows.append(d)
        if lat is not None:
            rows.sort(key=lambda x: x["away"])
        busy = one("""SELECT b.id FROM bookings b JOIN trips t ON t.id=b.trip_id
                    WHERE t.driver_id=? AND t.kind='instant' AND b.status IN ('confirmed','paid','ongoing') LIMIT 1""",
                   (g.user["id"],))
        return render_template("requests_open.html", rows=rows, located=lat is not None, busy=busy)

    @app.route("/permintaan/<int:rid>/ambil", methods=["POST"])
    @ready_required("driver")
    def request_take(rid):
        db = get_db()
        db.execute("BEGIN IMMEDIATE")   # dua pengemudi menekan bersamaan: hanya satu yang berhasil
        try:
            r = db.execute("SELECT * FROM ride_requests WHERE id=?", (rid,)).fetchone()
            now = time.time()
            if not r or r["status"] != "open" or r["expires_ts"] < now:
                raise ValueError("Permintaan sudah diambil pengemudi lain atau kedaluwarsa.")
            if r["passenger_id"] == g.user["id"]:
                raise ValueError("Anda tidak dapat mengambil permintaan sendiri.")
            if db.execute("""SELECT 1 FROM bookings b JOIN trips t ON t.id=b.trip_id
                           WHERE t.driver_id=? AND t.kind='instant' AND b.status IN ('confirmed','paid','ongoing')""",
                          (g.user["id"],)).fetchone():
                raise ValueError("Selesaikan perjalanan Anda yang sedang berjalan sebelum mengambil yang baru.")
            commission, share = svc.split_fare(r["fare"])
            cur = db.execute("""INSERT INTO trips (driver_id,origin_name,origin_lat,origin_lng,dest_name,dest_lat,dest_lng,
                              depart_at,seats,distance_km,fare,note,kind) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,'instant')""",
                             (g.user["id"], r["pickup_name"], r["pickup_lat"], r["pickup_lng"], r["dest_name"],
                              r["dest_lat"], r["dest_lng"], now_wita().strftime("%Y-%m-%d %H:%M"),
                              r["distance_km"], r["fare"], r["note"]))
            tid = cur.lastrowid
            cur = db.execute("""INSERT INTO bookings (trip_id,passenger_id,status,fare,commission,driver_share)
                              VALUES (?,?,'confirmed',?,?,?)""", (tid, r["passenger_id"], r["fare"], commission, share))
            bid = cur.lastrowid
            db.execute("UPDATE ride_requests SET status='taken', driver_id=?, trip_id=?, booking_id=?, taken_ts=? WHERE id=?",
                       (g.user["id"], tid, bid, now, rid))
            db.execute("COMMIT")
            flash("Permintaan diambil. Tunggu penumpang membayar, lalu jemput di titik yang ditandai.", "ok")
            return redirect(url_for("my_trips"))
        except ValueError as e:
            db.execute("ROLLBACK")
            flash(str(e), "error")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return redirect(url_for("requests_open"))

    # === Berkas identitas (hanya pemilik & admin) ===
    @app.route("/berkas/<int:uid>/<kind>")
    @login_required
    def user_file(uid, kind):
        if kind not in ("ktm", "ktp", "sim"):
            abort(404)
        if g.user["role"] != "admin" and g.user["id"] != uid:
            abort(403)
        u = one(f"SELECT {kind}_file f FROM users WHERE id=?", (uid,))
        if not u or not u["f"]:
            abort(404)
        folder, name = u["f"].split("/", 1)
        resp = send_from_directory(os.path.join(app.config["UPLOAD_DIR"], folder), name)
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    # === Admin ===
    def admin_only(f):
        @wraps(f)
        @login_required
        def w(*a, **k):
            if g.user["role"] != "admin":
                abort(403)
            return f(*a, **k)
        return w

    @app.route("/admin")
    @admin_only
    def admin():
        stats = {
            "users": one("SELECT COUNT(*) c FROM users WHERE role!='admin'")["c"],
            "pending": one("SELECT COUNT(*) c FROM users WHERE status='pending' AND phone_verified=1 AND role!='admin'")["c"],
            "trips": one("SELECT COUNT(*) c FROM trips")["c"],
            "gmv": one("SELECT COALESCE(SUM(fare),0) s FROM bookings WHERE status IN ('paid','ongoing','done')")["s"],
            "commission": one("SELECT COALESCE(SUM(commission),0) s FROM bookings WHERE status IN ('paid','ongoing','done')")["s"],
            "payout_due": one("SELECT COALESCE(SUM(driver_share),0) s FROM bookings WHERE status='done' AND payout_at IS NULL")["s"],
            "reports": one("SELECT COUNT(*) c FROM reports WHERE status='open'")["c"],
        }
        pending = q("SELECT * FROM users WHERE status='pending' AND phone_verified=1 AND role!='admin' ORDER BY id")
        users = q("SELECT * FROM users WHERE role!='admin' ORDER BY id DESC LIMIT 100")
        reports = q("""SELECT r.*, f.name fname, a.name aname FROM reports r JOIN users f ON f.id=r.from_id
                       LEFT JOIN users a ON a.id=r.about_id ORDER BY (r.status='open') DESC, r.id DESC LIMIT 50""")
        tx = q("""SELECT b.*, p.name pname, d.name dname, t.origin_name, t.dest_name FROM bookings b
                  JOIN users p ON p.id=b.passenger_id JOIN trips t ON t.id=b.trip_id JOIN users d ON d.id=t.driver_id
                  WHERE b.status IN ('paid','ongoing','done') ORDER BY b.id DESC LIMIT 50""")
        return render_template("admin.html", s=stats, pending=pending, users=users, reports=reports, tx=tx)

    @app.route("/admin/pengguna/<int:uid>")
    @admin_only
    def admin_user(uid):
        u = one("SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            abort(404)
        return render_template("admin_user.html", u=u, rating=rating_of(uid))

    @app.route("/admin/pengguna/<int:uid>/status", methods=["POST"])
    @admin_only
    def admin_user_status(uid):
        new = request.form.get("status")
        if new not in ("approved", "rejected", "suspended"):
            abort(400)
        reason = request.form.get("reason", "").strip()[:200] or None
        if new == "rejected" and not reason:
            flash("Isi alasan penolakan agar pengguna tahu apa yang perlu diperbaiki.", "error")
            return redirect(url_for("admin_user", uid=uid))
        ex("UPDATE users SET status=?, reject_reason=? WHERE id=? AND role!='admin'", (new, reason, uid))
        flash("Status pengguna diperbarui.", "ok")
        return redirect(url_for("admin"))

    @app.route("/admin/laporan/<int:rid>", methods=["POST"])
    @admin_only
    def admin_report(rid):
        ex("UPDATE reports SET status='resolved', admin_note=? WHERE id=?",
           (request.form.get("note", "").strip()[:300], rid))
        flash("Laporan ditandai selesai.", "ok")
        return redirect(url_for("admin") + "#laporan")

    @app.route("/admin/pembayaran/<int:bid>/payout", methods=["POST"])
    @admin_only
    def admin_payout(bid):
        ex("UPDATE bookings SET payout_at=datetime('now','+8 hours') WHERE id=? AND status='done'", (bid,))
        flash("Bagi hasil pengemudi ditandai sudah dibayarkan.", "ok")
        return redirect(url_for("admin") + "#transaksi")


app = create_app()

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    debug = os.getenv("DEBUG", "0") == "1" and host == "127.0.0.1" and app.config["APP_ENV"] != "production"
    app.run(host=host, debug=debug, port=int(os.getenv("PORT", "5000")))
