// Read the proxy link from the row's input. Using input.value (instead of an
// inline template string) avoids HTML-entity corruption of '&' -> '&amp;'.
function rowLink(btn) {
  const input = btn.parentElement.querySelector(".proxy-link");
  return input ? input.value : "";
}
function copyFromRow(btn) { copyText(rowLink(btn), btn); }
function qrFromRow(btn) { showQR(rowLink(btn)); }

// Copy-to-clipboard with button feedback.
function copyText(text, btn) {
  const done = () => {
    const old = btn.textContent;
    btn.textContent = "کپی شد ✓";
    setTimeout(() => (btn.textContent = old), 1500);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch (e) {}
  document.body.removeChild(ta);
}

// QR modal.
let qrInstance = null;
function showQR(text) {
  const modal = document.getElementById("qr-modal");
  const holder = document.getElementById("qrcode");
  holder.innerHTML = "";
  qrInstance = new QRCode(holder, { text: text, width: 240, height: 240 });
  modal.classList.remove("hidden");
  modal.classList.add("flex");
}

function closeQR(e) {
  if (e.target.id === "qr-modal") {
    const modal = document.getElementById("qr-modal");
    modal.classList.add("hidden");
    modal.classList.remove("flex");
  }
}
