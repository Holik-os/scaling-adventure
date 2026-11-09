import hashlib, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(tmp, "t.db"), UPLOAD_DIR=os.path.join(tmp, "up"), APP_ENV="development", SCHEDULED_ENABLED="1",
                  OTP_PROVIDER="console", PAYMENT_MODE="simulation", OSRM_URL="http://127.0.0.1:9")
os.environ.pop("ADMIN_NIM", None)
import app as appmod  # noqa
from db import ex, one, q  # noqa
from werkzeug.security import generate_password_hash  # noqa

REQ = {"pickup_name": "Gerbang 2 ITK", "dest_name": "Plaza", "olat": "-1.1480", "olng": "116.8490",
       "dlat": "-1.2700", "dlng": "116.8300", "note": "baju biru"}


class T(unittest.TestCase):
    def setUp(self):
        self.app = appmod.app
        self.ctx = self.app.app_context(); self.ctx.push()
        for t in ("ratings", "reports", "ride_requests", "bookings", "trips"):
            ex(f"DELETE FROM {t}")
        for n, role in (("p1", "passenger"), ("p2", "passenger"), ("d1", "driver"), ("d2", "driver")):
            if not one("SELECT 1 FROM users WHERE nim=?", (n,)):
                ex("INSERT INTO users (nim,name,phone,password_hash,role,plate,phone_verified,status) VALUES (?,?,?,?,?,?,1,'approved')",
                   (n, n.upper(), "62811" + n, generate_password_hash("rahasia123"), role, "KT 1 A" if role == "driver" else None))
        self.id = {r["nim"]: r["id"] for r in q("SELECT id,nim FROM users")}

    def tearDown(self):
        self.ctx.pop()

    def login(self, nim):
        c = self.app.test_client(); c.get("/")
        self.post(c, "/masuk", {"nim": nim, "password": "rahasia123"})
        c.get("/")
        return c

    def post(self, c, url, data=None):
        with c.session_transaction() as s:
            tok = s.get("csrf")
        return c.post(url, data=dict(data or {}, csrf=tok))

    def make_req(self, p):
        r = self.post(p, "/minta", REQ)
        self.assertEqual(r.status_code, 302, r.data)
        return int(r.headers["Location"].rsplit("/", 1)[1])

    def test_full_instant_flow(self):
        p, d1, d2, p2 = self.login("p1"), self.login("d1"), self.login("d2"), self.login("p2")
        rid = self.make_req(p)
        self.assertIn("Mencari pengemudi".encode(), p.get(f"/minta/{rid}").data)
        # permintaan kedua saat masih ada yang aktif dialihkan ke yang lama
        self.assertTrue(self.post(p, "/minta", REQ).headers["Location"].endswith(f"/minta/{rid}"))
        # pengemudi melihat, penumpang lain tidak bisa mengambil
        self.assertIn(b"Gerbang 2 ITK", d1.get("/permintaan").data)
        self.assertEqual(p2.get("/permintaan").status_code, 403)
        self.assertEqual(self.post(p2, f"/permintaan/{rid}/ambil").status_code, 403)
        self.assertIn(b"Gerbang 2 ITK", d1.get("/permintaan?lat=-1.15&lng=116.85").data)
        # d1 mengambil, d2 terlambat
        self.assertEqual(self.post(d1, f"/permintaan/{rid}/ambil").status_code, 302)
        self.post(d2, f"/permintaan/{rid}/ambil")
        self.assertEqual(one("SELECT COUNT(*) c FROM trips")["c"], 1)
        r = one("SELECT * FROM ride_requests WHERE id=?", (rid,))
        self.assertEqual((r["status"], r["driver_id"]), ("taken", self.id["d1"]))
        b = one("SELECT * FROM bookings WHERE id=?", (r["booking_id"],))
        self.assertEqual((b["status"], b["fare"]), ("confirmed", r["fare"]))
        self.assertEqual(b["commission"] + b["driver_share"], b["fare"])
        # perjalanan instan tidak muncul di jadwal & tidak bisa dibuka orang lain
        self.assertNotIn(b"Gerbang 2 ITK", p2.get("/").data)
        self.assertEqual(p2.get(f"/perjalanan/{r['trip_id']}").status_code, 404)
        self.assertEqual(self.post(p2, f"/perjalanan/{r['trip_id']}/pesan").status_code, 302)
        self.assertEqual(one("SELECT COUNT(*) c FROM bookings")["c"], 1)
        # bayar -> mulai -> selesai -> nilai
        self.assertIn(b"Bayar sebelum", p.get(f"/pesanan/{b['id']}/bayar").data)
        self.post(d1, f"/pesanan/{b['id']}/aksi", {"aksi": "mulai"})
        self.assertEqual(one("SELECT status FROM bookings WHERE id=?", (b["id"],))["status"], "confirmed")
        self.post(p, f"/pesanan/{b['id']}/simulasi-bayar")
        self.post(d1, f"/pesanan/{b['id']}/aksi", {"aksi": "mulai"})
        self.post(d1, f"/pesanan/{b['id']}/aksi", {"aksi": "selesai"})
        self.assertEqual(one("SELECT status FROM bookings WHERE id=?", (b["id"],))["status"], "done")
        self.post(p, f"/pesanan/{b['id']}/nilai", {"score": "5"})
        self.assertEqual(one("SELECT COUNT(*) c FROM ratings")["c"], 1)
        # setelah selesai, penumpang boleh meminta lagi
        self.make_req(p)

    def test_pay_timeout_reopens(self):
        p, d1, d2 = self.login("p1"), self.login("d1"), self.login("d2")
        rid = self.make_req(p)
        self.post(d1, f"/permintaan/{rid}/ambil")
        ex("UPDATE ride_requests SET taken_ts=? WHERE id=?", (time.time() - 11 * 60, rid))
        self.assertIn("Mencari pengemudi".encode(), p.get(f"/minta/{rid}").data)
        r = one("SELECT * FROM ride_requests WHERE id=?", (rid,))
        self.assertEqual((r["status"], r["driver_id"]), ("open", None))
        self.assertEqual(one("SELECT status FROM bookings").__getitem__("status"), "cancelled")
        self.assertEqual(one("SELECT status FROM trips")["status"], "cancelled")
        self.post(d2, f"/permintaan/{rid}/ambil")  # pengemudi lain bisa mengambil
        self.assertEqual(one("SELECT driver_id FROM ride_requests WHERE id=?", (rid,))["driver_id"], self.id["d2"])

    def test_cancel_reject_expire_busy(self):
        p, p2, d1 = self.login("p1"), self.login("p2"), self.login("d1")
        # batal saat terbuka
        rid = self.make_req(p)
        self.post(p, f"/minta/{rid}/batal")
        self.assertEqual(one("SELECT status FROM ride_requests WHERE id=?", (rid,))["status"], "cancelled")
        # pengemudi mundur -> permintaan dibuka lagi
        rid = self.make_req(p)
        self.post(d1, f"/permintaan/{rid}/ambil")
        bid = one("SELECT id FROM bookings")["id"]
        self.post(d1, f"/pesanan/{bid}/aksi", {"aksi": "tolak"})
        self.assertEqual(one("SELECT status FROM ride_requests WHERE id=?", (rid,))["status"], "open")
        # pengemudi yang sibuk tidak bisa mengambil dua
        rid2 = self.make_req(p2)
        self.post(d1, f"/permintaan/{rid}/ambil")
        self.post(d1, f"/permintaan/{rid2}/ambil")
        self.assertEqual(one("SELECT status FROM ride_requests WHERE id=?", (rid2,))["status"], "open")
        # penumpang batal setelah diambil -> permintaan batal
        bid = one("SELECT id FROM bookings WHERE status='confirmed'")["id"]
        self.post(p, f"/pesanan/{bid}/aksi", {"aksi": "batal"})
        self.assertEqual(one("SELECT status FROM ride_requests WHERE id=?", (rid,))["status"], "cancelled")
        # kedaluwarsa
        ex("UPDATE ride_requests SET expires_ts=? WHERE id=?", (time.time() - 1, rid2))
        d1.get("/permintaan")
        self.assertEqual(one("SELECT status FROM ride_requests WHERE id=?", (rid2,))["status"], "expired")

    def test_scheduled_hidden_by_default(self):
        self.app.config["SCHEDULED_ENABLED"] = False
        try:
            d, p = self.login("d1"), self.login("p1")
            self.assertEqual(d.get("/perjalanan/baru").status_code, 404)
            html = d.get("/").data.decode()
            self.assertNotIn("Buat perjalanan", html); self.assertNotIn("Jadwal pengemudi", html)
            self.assertIn("Permintaan masuk", html)
            self.assertNotIn("Buat perjalanan", d.get("/jadwal-saya").data.decode())
            self.assertIn("Minta tumpangan", p.get("/").data.decode())
        finally:
            self.app.config["SCHEDULED_ENABLED"] = True

    def test_validation(self):
        p = self.login("p1")
        near = dict(REQ, dlat="-1.1481", dlng="116.8491")
        self.assertEqual(self.post(p, "/minta", near).status_code, 400)
        self.assertEqual(self.post(p, "/minta", dict(REQ, olat="")).status_code, 400)
        self.assertEqual(one("SELECT COUNT(*) c FROM ride_requests")["c"], 0)

    def test_late_payment_creates_refund_report(self):
        self.app.config.update(PAYMENT_MODE="midtrans", MIDTRANS_SERVER_KEY="SB-key")
        try:
            ex("INSERT INTO trips (driver_id,origin_name,origin_lat,origin_lng,dest_name,dest_lat,dest_lng,depart_at,seats,distance_km,fare,kind,status) VALUES (?,?,?,?,?,?,?,?,1,8,8000,'instant','cancelled')",
               (self.id["d1"], "A", -1, 116, "B", -1, 116, "2026-01-01 10:00"))
            tid = one("SELECT MAX(id) m FROM trips")["m"]
            ex("INSERT INTO bookings (trip_id,passenger_id,status,fare,commission,driver_share,order_id) VALUES (?,?,'cancelled',8000,1600,6400,'MASPUT-L-1')",
               (tid, self.id["p1"]))
            body = {"order_id": "MASPUT-L-1", "status_code": "200", "gross_amount": "8000.00", "transaction_status": "settlement"}
            body["signature_key"] = hashlib.sha512((body["order_id"] + "200" + "8000.00" + "SB-key").encode()).hexdigest()
            c = self.app.test_client()
            c.post("/webhook/midtrans", json=body); c.post("/webhook/midtrans", json=body)
            self.assertEqual(one("SELECT COUNT(*) c FROM reports")["c"], 1)
            self.assertEqual(one("SELECT status FROM bookings WHERE order_id='MASPUT-L-1'")["status"], "cancelled")
        finally:
            self.app.config.update(PAYMENT_MODE="simulation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
