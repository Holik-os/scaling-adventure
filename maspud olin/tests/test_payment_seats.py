import hashlib, io, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(tmp, "t.db"), UPLOAD_DIR=os.path.join(tmp, "up"),
                  APP_ENV="development", SCHEDULED_ENABLED="1", OTP_PROVIDER="console", PAYMENT_MODE="simulation")
os.environ.pop("ADMIN_NIM", None)
import app as appmod  # noqa
from db import ex, one, q  # noqa
from werkzeug.security import generate_password_hash  # noqa


class T(unittest.TestCase):
    def setUp(self):
        self.app = appmod.app
        self.ctx = self.app.app_context(); self.ctx.push()
        for n, role in (("d1", "driver"), ("p1", "passenger"), ("p2", "passenger")):
            if not one("SELECT 1 FROM users WHERE nim=?", (n,)):
                ex("INSERT INTO users (nim,name,phone,password_hash,role,phone_verified,status) VALUES (?,?,?,?,?,1,'approved')",
                   (n, n, "62811", generate_password_hash("rahasia123"), role))
        self.ids = {r["nim"]: r["id"] for r in q("SELECT id,nim FROM users")}
        ex("""INSERT INTO trips (driver_id,origin_name,origin_lat,origin_lng,dest_name,dest_lat,dest_lng,depart_at,seats,distance_km,fare)
              VALUES (?,?,?,?,?,?,?,?,1,8,8000)""", (self.ids["d1"], "A", -1.1, 116.8, "B", -1.2, 116.9, "2099-01-01 10:00"))
        self.tid = one("SELECT MAX(id) m FROM trips")["m"]

    def tearDown(self):
        self.ctx.pop()

    def login(self, nim):
        c = self.app.test_client(); c.get("/")
        with c.session_transaction() as s:
            tok = s["csrf"]
        c.post("/masuk", data={"nim": nim, "password": "rahasia123", "csrf": tok})
        c.get("/")
        return c

    def post(self, c, url, data=None):
        with c.session_transaction() as s:
            tok = s.get("csrf")
        return c.post(url, data=dict(data or {}, csrf=tok))

    def test_seat_limit(self):
        a, b = self.login("p1"), self.login("p2")
        self.post(a, f"/perjalanan/{self.tid}/pesan")
        self.post(b, f"/perjalanan/{self.tid}/pesan")  # kursi hanya 1
        n = one("SELECT COUNT(*) c FROM bookings WHERE trip_id=?", (self.tid,))["c"]
        self.assertEqual(n, 1)
        # setelah penumpang pertama batal, kursi terbuka lagi
        bid = one("SELECT id FROM bookings WHERE trip_id=?", (self.tid,))["id"]
        self.post(a, f"/pesanan/{bid}/aksi", {"aksi": "batal"})
        self.post(b, f"/perjalanan/{self.tid}/pesan")
        self.assertEqual(one("SELECT COUNT(*) c FROM bookings WHERE trip_id=? AND status='requested'", (self.tid,))["c"], 1)

    def test_midtrans_webhook(self):
        self.app.config.update(PAYMENT_MODE="midtrans", MIDTRANS_SERVER_KEY="SB-Mid-server-test")
        try:
            ex("INSERT INTO bookings (trip_id,passenger_id,status,fare,commission,driver_share,order_id) VALUES (?,?,?,?,?,?,?)",
               (self.tid, self.ids["p1"], "confirmed", 8000, 1600, 6400, "MASPUT-X-1"))
            bid = one("SELECT MAX(id) m FROM bookings")["m"]
            body = {"order_id": "MASPUT-X-1", "status_code": "200", "gross_amount": "8000.00",
                    "transaction_status": "settlement", "transaction_id": "tx1"}
            sig = lambda b: hashlib.sha512((b["order_id"] + b["status_code"] + b["gross_amount"] + "SB-Mid-server-test").encode()).hexdigest()
            c = self.app.test_client()
            # tanda tangan palsu ditolak
            self.assertEqual(c.post("/webhook/midtrans", json=dict(body, signature_key="x" * 128)).status_code, 403)
            self.assertEqual(one("SELECT status FROM bookings WHERE id=?", (bid,))["status"], "confirmed")
            # nominal tidak cocok tidak membayar lunas
            bad = dict(body, gross_amount="1000.00"); bad["signature_key"] = sig(bad)
            c.post("/webhook/midtrans", json=bad)
            self.assertEqual(one("SELECT status FROM bookings WHERE id=?", (bid,))["status"], "confirmed")
            # valid
            good = dict(body, signature_key=sig(body))
            self.assertEqual(c.post("/webhook/midtrans", json=good).status_code, 200)
            self.assertEqual(one("SELECT status FROM bookings WHERE id=?", (bid,))["status"], "paid")
            # simulasi dimatikan di mode midtrans
            p = self.login("p1")
            self.assertEqual(self.post(p, f"/pesanan/{bid}/simulasi-bayar").status_code, 404)
        finally:
            self.app.config.update(PAYMENT_MODE="simulation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
