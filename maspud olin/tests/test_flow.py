import io, os, re, sys, tempfile, unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(tmp, "t.db"), UPLOAD_DIR=os.path.join(tmp, "up"),
                  ADMIN_NIM="admin01", ADMIN_PASSWORD="admin-pass-123", APP_ENV="development", SCHEDULED_ENABLED="1",
                  OTP_PROVIDER="console", PAYMENT_MODE="simulation", OSRM_URL="http://127.0.0.1:9")
import app as appmod  # noqa: E402
import services as svc  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
OTPS = {}
svc.send_whatsapp = lambda phone, msg: OTPS.__setitem__(phone, re.search(r"\b(\d{6})\b", msg).group(1)) or True


class Client:
    def __init__(self, app):
        self.c = app.test_client()
        self.c.get("/")

    def post(self, url, data=None, **kw):
        with self.c.session_transaction() as s:
            tok = s.get("csrf")
        if not tok:  # seperti browser: halaman dimuat dulu sebelum formulir dikirim
            self.c.get("/")
            with self.c.session_transaction() as s:
                tok = s.get("csrf")
        d = dict(data or {})
        d["csrf"] = tok
        return self.c.post(url, data=d, **kw)

    def get(self, url, **kw):
        return self.c.get(url, **kw)


def reg(app, nim, name, phone, role, **extra):
    c = Client(app)
    data = {"nim": nim, "name": name, "phone": phone, "password": "rahasia123", "role": role,
            "ktm": (io.BytesIO(PNG), "ktm.png")}
    if role == "driver":
        data.update(plate="KT 1234 AB", ktp=(io.BytesIO(PNG), "ktp.png"), sim=(io.BytesIO(PNG), "sim.png"))
    data.update(extra)
    r = c.post("/daftar", data, content_type="multipart/form-data")
    return c, r


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = appmod.app
        cls.app.config["TESTING"] = True

    def test_full_flow(self):
        app = self.app
        # --- daftar pengemudi & penumpang
        drv, r = reg(app, "12241020", "Budi Pengemudi", "081234567890", "driver")
        self.assertEqual(r.status_code, 302, r.data)
        self.assertIn("/verifikasi", r.headers["Location"])
        # belum verifikasi -> tidak boleh buat perjalanan
        self.assertEqual(drv.get("/perjalanan/baru").status_code, 302)
        # OTP salah lalu benar
        drv.post("/verifikasi", {"code": "000000"})
        r = drv.post("/verifikasi", {"code": OTPS["6281234567890"]})
        self.assertIn("/status", r.headers["Location"])
        # belum disetujui admin
        r = drv.get("/perjalanan/baru")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/status", r.headers["Location"])

        pas, r = reg(app, "12241036", "Sari Penumpang", "+62 812-9999-0001", "passenger")
        self.assertEqual(r.status_code, 302)
        phone = [k for k in OTPS if k.endswith("99990001")][0]
        pas.post("/verifikasi", {"code": OTPS[phone]})

        # --- admin menyetujui
        adm = Client(app)
        r = adm.post("/masuk", {"nim": "admin01", "password": "admin-pass-123"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(adm.get("/admin").status_code, 200)
        from db import one
        with app.app_context():
            ids = {u["nim"]: u["id"] for u in appmod.q("SELECT id,nim FROM users")}
        self.assertEqual(adm.get(f"/admin/pengguna/{ids['12241020']}").status_code, 200)
        self.assertEqual(adm.get(f"/berkas/{ids['12241020']}/sim").status_code, 200)
        # penumpang tidak boleh membuka berkas orang lain, admin boleh
        self.assertEqual(pas.get(f"/berkas/{ids['12241020']}/ktm").status_code, 403)
        # penolakan wajib alasan
        adm.post(f"/admin/pengguna/{ids['12241020']}/status", {"status": "rejected"})
        with app.app_context():
            self.assertEqual(one("SELECT status FROM users WHERE nim='12241020'")["status"], "pending")
        for n in ("12241020", "12241036"):
            adm.post(f"/admin/pengguna/{ids[n]}/status", {"status": "approved"})

        # --- pengemudi buat perjalanan (jarak ~ lurus x1,3 karena OSRM tidak terjangkau)
        dep = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        trip = {"origin_name": "ITK", "dest_name": "Plaza", "olat": "-1.1480", "olng": "116.8490",
                "dlat": "-1.2700", "dlng": "116.8300", "depart_at": dep, "seats": "1", "note": ""}
        bad = dict(trip, depart_at="2000-01-01T10:00")
        self.assertEqual(drv.post("/perjalanan/baru", bad).status_code, 400)
        self.assertEqual(pas.post("/perjalanan/baru", trip).status_code, 403)  # penumpang tidak bisa
        r = drv.post("/perjalanan/baru", trip)
        self.assertEqual(r.status_code, 302, r.data)
        tid = int(r.headers["Location"].rsplit("/", 1)[1])
        with app.app_context():
            t = one("SELECT * FROM trips WHERE id=?", (tid,))
        self.assertEqual(t["fare"], svc.calc_fare(t["distance_km"]))
        self.assertGreaterEqual(t["fare"], 5000)

        # --- pencarian & detail
        self.assertIn(b"Plaza", pas.get("/?q=plaza").data)
        self.assertEqual(pas.get(f"/perjalanan/{tid}").status_code, 200)
        self.assertEqual(pas.get("/api/rute?olat=-1.148&olng=116.849&dlat=-1.27&dlng=116.83").status_code, 200)

        # --- pesan; tidak boleh dobel; pengemudi tak boleh pesan sendiri
        pas.post(f"/perjalanan/{tid}/pesan")
        pas.post(f"/perjalanan/{tid}/pesan")
        drv.post(f"/perjalanan/{tid}/pesan")
        with app.app_context():
            bs = appmod.q("SELECT * FROM bookings WHERE trip_id=?", (tid,))
        self.assertEqual(len(bs), 1)
        b = bs[0]
        self.assertEqual(b["fare"], b["commission"] + b["driver_share"])
        self.assertEqual(b["commission"], b["fare"] * 20 // 100)
        bid = b["id"]

        # penumpang tidak bisa menerima/mulai sendiri; belum bayar tidak bisa mulai
        pas.post(f"/pesanan/{bid}/aksi", {"aksi": "terima"})
        drv.post(f"/pesanan/{bid}/aksi", {"aksi": "mulai"})
        def st():
            with app.app_context():
                return one("SELECT status FROM bookings WHERE id=?", (bid,))["status"]
        self.assertEqual(st(), "requested")
        drv.post(f"/pesanan/{bid}/aksi", {"aksi": "terima"})
        self.assertEqual(st(), "confirmed")
        drv.post(f"/pesanan/{bid}/aksi", {"aksi": "mulai"})
        self.assertEqual(st(), "confirmed")  # wajib bayar dulu
        # kursi penuh untuk orang lain
        oth, _ = reg(app, "12241099", "Lain Lagi", "081200000099", "passenger")
        oth.post("/verifikasi", {"code": OTPS[[k for k in OTPS if k.endswith("200000099")][0]]})
        # bayar (simulasi)
        self.assertEqual(pas.get(f"/pesanan/{bid}/bayar").status_code, 200)
        drv.post(f"/pesanan/{bid}/simulasi-bayar")  # bukan pemilik -> 404
        self.assertEqual(st(), "confirmed")
        pas.post(f"/pesanan/{bid}/simulasi-bayar")
        self.assertEqual(st(), "paid")
        drv.post(f"/pesanan/{bid}/aksi", {"aksi": "mulai"})
        self.assertEqual(st(), "ongoing")
        drv.post(f"/pesanan/{bid}/aksi", {"aksi": "selesai"})
        self.assertEqual(st(), "done")

        # --- rating & laporan
        self.assertEqual(pas.get(f"/pesanan/{bid}/nilai").status_code, 200)
        pas.post(f"/pesanan/{bid}/nilai", {"score": "5", "comment": "ramah"})
        pas.post(f"/pesanan/{bid}/nilai", {"score": "1"})  # tidak boleh dua kali
        drv.post(f"/pesanan/{bid}/nilai", {"score": "4"})
        pas.post(f"/pesanan/{bid}/lapor", {"category": "tarif", "detail": "tarif terasa berbeda dari aplikasi"})
        with app.app_context():
            self.assertEqual(one("SELECT COUNT(*) c FROM ratings")["c"], 2)
            self.assertEqual(one("SELECT score FROM ratings WHERE from_id=?", (ids["12241036"],))["score"], 5)
            self.assertEqual(one("SELECT COUNT(*) c FROM reports")["c"], 1)

        # --- dasbor
        page = adm.get("/admin").data.decode()
        self.assertIn("Tandai sudah dibayarkan", page)
        self.assertEqual(drv.get("/jadwal-saya").status_code, 200)
        self.assertEqual(pas.get("/pesanan").status_code, 200)
        adm.post(f"/admin/pembayaran/{bid}/payout")
        with app.app_context():
            self.assertIsNotNone(one("SELECT payout_at FROM bookings WHERE id=?", (bid,))["payout_at"])
        # penumpang tidak bisa buka admin
        self.assertEqual(pas.get("/admin").status_code, 403)

    def test_security(self):
        app = self.app
        c = app.test_client()
        c.get("/")
        self.assertEqual(c.post("/masuk", data={"nim": "x", "password": "y"}).status_code, 400)  # tanpa CSRF
        # berkas bukan gambar ditolak
        cl, r = reg(app, "12241055", "Pak Jahat", "081255555555", "passenger", ktm=(io.BytesIO(b"<?php evil ?>"), "a.png"))
        self.assertEqual(r.status_code, 400)
        # NIM salah format
        _, r = reg(app, "abc", "Nama Valid", "081255555556", "passenger")
        self.assertEqual(r.status_code, 400)
        # brute force login dibatasi
        cl = Client(app)
        codes = [cl.post("/masuk", {"nim": "12345678", "password": "salah"}).status_code for _ in range(7)]
        self.assertEqual(codes[-1], 429)
        # belum login
        self.assertEqual(app.test_client().get("/perjalanan/1").status_code, 302)
        self.assertEqual(app.test_client().post("/webhook/midtrans", json={}).status_code, 403)

    def test_fare(self):
        self.assertEqual(svc.calc_fare(2), 5000)
        self.assertEqual(svc.calc_fare(5), 5000)
        self.assertEqual(svc.calc_fare(5.1), 6000)
        self.assertEqual(svc.calc_fare(10), 10000)
        self.assertEqual(svc.split_fare(5000), (1000, 4000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
