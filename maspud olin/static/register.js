(function(){var r=document.getElementById("role"),d=document.getElementById("driver-fields");
function t(){var on=r.value==="driver";d.hidden=!on;["plate","ktp","sim"].forEach(function(i){document.getElementById(i).required=on;});}
r.addEventListener("change",t);t();})();
