# MASPUT - bikepooling mahasiswa ITK

Aplikasi web (Python Flask + SQLite) dengan model jadwal seperti di proposal: pengemudi memposting perjalanan,
penumpang mencari dan memesan. Tarif Rp5.000 untuk 5 km pertama, lalu Rp1.000 per km. Bagi hasil 80% pengemudi, 20% MASPUT.

## Alur (model seperti Maxim)
Penumpang klik titik jemput dan tujuan di peta -> tarif dihitung server (Rp5.000 untuk 5 km pertama, lalu Rp1.000 per km,
dikunci di muka) -> pengemudi yang aktif melihat daftar Permintaan masuk dan mengambil satu -> penumpang membayar QRIS
dalam 10 menit -> pengemudi menjemput, mulai, dan selesaikan -> saling menilai. Bagi hasil 80% pengemudi, 20% MASPUT.

Aturan: permintaan kedaluwarsa 15 menit bila tidak diambil; bila penumpang terlambat bayar, pesanan batal dan permintaan
dibuka lagi untuk pengemudi lain; satu penumpang hanya punya satu permintaan aktif; satu pengemudi hanya satu perjalanan
berjalan. Halaman pengemudi dan penumpang memuat ulang otomatis (15 dan 10 detik). Model jadwal pengemudi lama disembunyikan
(aktifkan dengan `SCHEDULED_ENABLED=1` di `.env`). Pendaftaran, OTP, verifikasi admin, rating, laporan, dan dasbor admin tetap sama.

## Menjalankan di VS Code
1. Pasang Python 3.10+ dan ekstensi Python di VS Code, lalu File > Open Folder > folder ini.
2. Terminal (Ctrl+`):
   - Windows: `python -m venv .venv` lalu `.venv\Scripts\activate`
   - Mac/Linux: `python3 -m venv .venv` lalu `source .venv/bin/activate`
   - `pip install -r requirements.txt`
3. Salin `.env.example` menjadi `.env`, isi `ADMIN_NIM` dan `ADMIN_PASSWORD` (minimal 10 karakter).
4. `python app.py`, buka http://127.0.0.1:5000 dan masuk dengan NIM dan kata sandi admin tadi.
5. Mode awal: kode OTP muncul di terminal VS Code, dan pembayaran disimulasikan.
6. Uji otomatis: `python tests/test_flow.py`, `python tests/test_payment_seats.py`, dan `python tests/test_instant.py`.

## Menjadi layanan sungguhan
1. **OTP WhatsApp**: daftar di Fonnte, hubungkan nomor WhatsApp, isi `OTP_PROVIDER=fonnte` dan `FONNTE_TOKEN`.
2. **Pembayaran QRIS**: daftar merchant Midtrans (butuh data usaha/lembaga). Uji dulu di sandbox:
   `PAYMENT_MODE=midtrans`, `MIDTRANS_SERVER_KEY=<server key sandbox>`. Di dashboard Midtrans, isi
   Payment Notification URL: `https://DOMAIN-ANDA/webhook/midtrans`. Pindah ke uang asli dengan key produksi dan `MIDTRANS_PRODUCTION=1`.
3. **Server**: gunakan VPS Linux dengan HTTPS (Nginx + Let's Encrypt). Isi `.env`: `APP_ENV=production`, `SECRET_KEY`,
   `DB_PATH` dan `UPLOAD_DIR` di disk permanen. Jalankan: `gunicorn -w 2 -b 127.0.0.1:8000 app:app`.
   Di mode production, aplikasi menolak berjalan bila masih memakai OTP console atau pembayaran simulasi.
4. **Cadangan**: salin berkas database dan folder upload secara rutin.

## Yang perlu Anda ketahui
- Uang pembayaran masuk ke akun Midtrans MASPUT. Bagi hasil 80% ke pengemudi dicatat otomatis, tetapi transfernya ke
  pengemudi dilakukan manual oleh admin (lalu klik "Tandai sudah dibayarkan"). Transfer otomatis butuh layanan payout tambahan.
- Jarak dihitung dari rute jalan OSRM; bila server rute tidak terjangkau, dipakai garis lurus x 1,3 (tarif tetap dihitung server).
- Tidak ada pelacakan lokasi langsung dan tidak ada notifikasi otomatis: pengemudi harus membuka halaman Permintaan masuk (memuat ulang sendiri tiap 15 detik). Notifikasi WhatsApp ke pengemudi bisa ditambahkan nanti lewat Fonnte.
- Tidak ada pelacakan lokasi langsung. Pembatalan setelah pembayaran belum otomatis: gunakan laporan dan penanganan admin (refund lewat Midtrans).
- Foto KTM/KTP/SIM adalah data pribadi. Simpan di server yang aman, batasi akses, dan siapkan kebijakan privasi sebelum dipakai umum.
- Aspek legal usaha, asuransi, dan keselamatan perlu dikaji sebelum peluncuran, sesuai proposal.
- SQLite cukup untuk ratusan pengguna. Untuk pertumbuhan besar, pindah ke PostgreSQL.
