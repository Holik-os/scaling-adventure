/* Peta rute MASPUT: Leaflet + OpenStreetMap. Jarak & tarif resmi selalu dihitung server. */
(function () {
  var el = document.getElementById("map");
  if (!el || typeof L === "undefined") { if (el) el.textContent = "Peta tidak dapat dimuat. Periksa koneksi internet."; return; }
  var map = L.map("map").setView([-1.2203, 116.8650], 12);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
    { maxZoom: 19, subdomains: "abcd", attribution: "&copy; OpenStreetMap, &copy; CARTO" }).addTo(map);
  var line = null, mo = null, md = null;
  var rp = function (n) { return "Rp" + String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "."); };

  function drawRoute(o, d, cb) {
    var u = "/api/rute?olat=" + o[0] + "&olng=" + o[1] + "&dlat=" + d[0] + "&dlng=" + d[1];
    fetch(u, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (j) {
      if (line) map.removeLayer(line);
      line = L.polyline(j.coords, { color: "#0b5c73", weight: 5 }).addTo(map);
      map.fitBounds(line.getBounds(), { padding: [30, 30] });
      if (cb) cb(j);
    }).catch(function () { if (cb) cb(null); });
  }

  var mode = el.dataset.mode;
  if (mode === "view") {
    var o = [+el.dataset.olat, +el.dataset.olng], d = [+el.dataset.dlat, +el.dataset.dlng];
    L.marker(o).addTo(map).bindPopup("Asal"); L.marker(d).addTo(map).bindPopup("Tujuan");
    map.fitBounds([o, d], { padding: [30, 30] });
    drawRoute(o, d);
    return;
  }

  var F = function (n) { return document.getElementById(n); };
  var info = F("route-info"), step = 0;
  function reset() {
    [mo, md, line].forEach(function (x) { if (x) map.removeLayer(x); });
    mo = md = line = null; step = 0;
    ["olat", "olng", "dlat", "dlng"].forEach(function (n) { F(n).value = ""; });
    info.textContent = "Klik peta untuk titik awal, lalu klik lagi untuk titik tujuan.";
  }
  map.on("click", function (e) {
    var p = [e.latlng.lat, e.latlng.lng];
    if (step === 0) {
      mo = L.marker(p).addTo(map).bindPopup("Asal").openPopup();
      F("olat").value = p[0].toFixed(6); F("olng").value = p[1].toFixed(6);
      info.textContent = "Sekarang klik titik tujuan."; step = 1;
    } else if (step === 1) {
      md = L.marker(p).addTo(map).bindPopup("Tujuan");
      F("dlat").value = p[0].toFixed(6); F("dlng").value = p[1].toFixed(6);
      info.textContent = "Menghitung rute..."; step = 2;
      drawRoute([+F("olat").value, +F("olng").value], p, function (j) {
        info.textContent = j ? "Jarak " + j.km + " km" + (j.source === "estimasi" ? " (perkiraan, server rute tidak terjangkau)" : "") +
          " - tarif per penumpang " + rp(j.fare) + ". Tarif final dihitung ulang oleh server." : "Rute gagal dihitung. Coba lagi.";
      });
    }
  });
  F("reset-map").addEventListener("click", reset);
  F("trip-form").addEventListener("submit", function (ev) {
    if (!F("olat").value || !F("dlat").value) { ev.preventDefault(); info.textContent = "Pilih titik asal dan tujuan di peta dulu."; }
  });
  reset();
})();
