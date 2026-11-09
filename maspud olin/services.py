"""Layanan eksternal: rute (OSRM), OTP WhatsApp (Fonnte), pembayaran QRIS (Midtrans)."""
import base64
import hashlib
import hmac
import math
import secrets
import time

import requests
from flask import current_app

FIRST_KM = 5
BASE_FARE = 5000
PER_KM = 1000
COMMISSION_PCT = 20


# ---------- Tarif ----------
def calc_fare(km: float) -> int:
    """Rp5.000 untuk 5 km pertama, lalu Rp1.000 per km (dibulatkan ke atas)."""
    extra = max(0, math.ceil(km - FIRST_KM))
    return BASE_FARE + PER_KM * extra


def split_fare(fare: int):
    commission = fare * COMMISSION_PCT // 100
    return commission, fare - commission


# ---------- Rute ----------
def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def route_info(lat1, lng1, lat2, lng2, geometry=False):
    """Info rute dari OSRM. Jika gagal: garis lurus x 1,3 (sumber 'estimasi').
    Mengembalikan {km, source, coords} dengan coords = [[lat, lng], ...]."""
    url = current_app.config["OSRM_URL"].rstrip("/")
    try:
        r = requests.get(
            f"{url}/route/v1/driving/{lng1},{lat1};{lng2},{lat2}",
            params={"overview": "simplified" if geometry else "false", "geometries": "geojson"},
            timeout=6,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") == "Ok" and data.get("routes"):
            rt = data["routes"][0]
            coords = [[c[1], c[0]] for c in rt["geometry"]["coordinates"]] if geometry else []
            return {"km": round(rt["distance"] / 1000, 2), "source": "osrm", "coords": coords}
    except (requests.RequestException, ValueError, KeyError):
        pass
    km = round(haversine_km(lat1, lng1, lat2, lng2) * 1.3, 2)
    return {"km": km, "source": "estimasi", "coords": [[lat1, lng1], [lat2, lng2]] if geometry else []}


# ---------- OTP ----------
def new_otp_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(code: str) -> str:
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()


def normalize_phone(raw: str):
    d = "".join(c for c in raw if c.isdigit())
    if d.startswith("0"):
        d = "62" + d[1:]
    elif d.startswith("8"):
        d = "62" + d
    if not d.startswith("62") or not (10 <= len(d) <= 15):
        return None
    return d


def send_whatsapp(phone: str, message: str) -> bool:
    provider = current_app.config["OTP_PROVIDER"]
    if provider == "console":
        current_app.logger.warning("[OTP console] ke %s: %s", phone, message)
        print(f"\n=== PESAN WHATSAPP (mode console) ke {phone} ===\n{message}\n", flush=True)
        return True
    if provider == "fonnte":
        try:
            r = requests.post(
                "https://api.fonnte.com/send",
                headers={"Authorization": current_app.config["FONNTE_TOKEN"]},
                data={"target": phone, "message": message}, timeout=10,
            )
            return r.ok and r.json().get("status") is True
        except (requests.RequestException, ValueError):
            return False
    return False


# ---------- Pembayaran ----------
def _midtrans_base():
    return ("https://api.midtrans.com" if current_app.config["MIDTRANS_PRODUCTION"]
            else "https://api.sandbox.midtrans.com")


def _midtrans_auth():
    key = current_app.config["MIDTRANS_SERVER_KEY"] + ":"
    return {"Authorization": "Basic " + base64.b64encode(key.encode()).decode(),
            "Accept": "application/json", "Content-Type": "application/json"}


def create_qris(order_id: str, amount: int, expiry_min=None):
    """Buat tagihan QRIS. Mengembalikan dict {ok, qr_url, qr_string, error}."""
    mode = current_app.config["PAYMENT_MODE"]
    if mode == "simulation":
        return {"ok": True, "qr_url": None, "qr_string": f"SIMULASI-{order_id}"}
    payload = {"payment_type": "qris",
               "transaction_details": {"order_id": order_id, "gross_amount": amount},
               "qris": {"acquirer": "gopay"}}
    if expiry_min:
        payload["custom_expiry"] = {"expiry_duration": int(expiry_min), "unit": "minute"}
    try:
        r = requests.post(f"{_midtrans_base()}/v2/charge", json=payload,
                          headers=_midtrans_auth(), timeout=12)
        d = r.json()
    except (requests.RequestException, ValueError):
        return {"ok": False, "error": "Penyedia pembayaran tidak dapat dihubungi."}
    if str(d.get("status_code")) not in ("200", "201"):
        return {"ok": False, "error": d.get("status_message", "Gagal membuat tagihan.")}
    qr_url = next((a["url"] for a in d.get("actions", []) if "qr" in a.get("name", "")), None)
    return {"ok": True, "qr_url": qr_url, "qr_string": d.get("qr_string")}


def midtrans_status(order_id: str):
    try:
        r = requests.get(f"{_midtrans_base()}/v2/{order_id}/status",
                         headers=_midtrans_auth(), timeout=10)
        return r.json()
    except (requests.RequestException, ValueError):
        return None


def midtrans_signature_ok(d: dict) -> bool:
    raw = (str(d.get("order_id", "")) + str(d.get("status_code", "")) +
           str(d.get("gross_amount", "")) + current_app.config["MIDTRANS_SERVER_KEY"])
    expected = hashlib.sha512(raw.encode()).hexdigest()
    return hmac.compare_digest(expected, str(d.get("signature_key", "")))


def is_paid_status(d: dict) -> bool:
    ts = d.get("transaction_status")
    return ts == "settlement" or (ts == "capture" and d.get("fraud_status") in (None, "accept"))
