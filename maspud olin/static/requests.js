(function () {
  var b = document.getElementById("use-loc"), m = document.getElementById("loc-msg");
  if (!b) return;
  b.addEventListener("click", function () {
    if (!navigator.geolocation) { m.textContent = "Browser ini tidak mendukung lokasi."; return; }
    m.textContent = "Mengambil lokasi...";
    navigator.geolocation.getCurrentPosition(function (p) {
      location.href = "/permintaan?lat=" + p.coords.latitude.toFixed(5) + "&lng=" + p.coords.longitude.toFixed(5);
    }, function () { m.textContent = "Lokasi tidak bisa diambil. Izinkan akses lokasi di browser lalu coba lagi."; },
    { enableHighAccuracy: true, timeout: 10000 });
  });
})();
