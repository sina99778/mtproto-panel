// Dark / light theme toggle (persisted in localStorage; Tabler reads data-bs-theme).
function toggleTheme() {
  const html = document.documentElement;
  const current = html.getAttribute("data-bs-theme");
  const next = current === "dark" ? "light" : "dark";
  
  // Transition effect class
  html.classList.add("theme-transitioning");
  html.setAttribute("data-bs-theme", next);
  
  try {
    localStorage.setItem("theme", next);
  } catch (e) {}
  
  setTimeout(() => {
    html.classList.remove("theme-transitioning");
  }, 300);
}

// Modern Toast Notification API
function showToast(message, duration = 3000) {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  toast.className = "toast-alert";
  
  // Custom toast markup (with clean modern checkmark icon)
  toast.innerHTML = `
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="text-success" style="vertical-align:middle"><polyline points="20 6 9 17 4 12"/></svg>
    <span>${message}</span>
  `;

  container.appendChild(toast);

  // Auto remove toast
  setTimeout(() => {
    toast.classList.add("fadeOut");
    toast.addEventListener("transitionend", () => {
      toast.remove();
    });
  }, duration);
}

// Read the proxy link from the row's input. Using input.value (instead of an
// inline template string) avoids HTML-entity corruption of '&' -> '&amp;'.
function rowLink(btn) {
  // Finds closest card-body or input-group containing proxy-link
  const container = btn.closest(".card-body") || btn.closest(".input-group");
  const input = container ? container.querySelector(".proxy-link") : null;
  return input ? input.value : "";
}

function copyFromRow(btn) {
  copyText(rowLink(btn));
}

function qrFromRow(btn) {
  showQR(rowLink(btn));
}

// Copy-to-clipboard with modern toast feedback.
function copyText(text) {
  const done = () => {
    showToast("لینک اتصال با موفقیت کپی شد! 📋");
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
  try {
    document.execCommand("copy");
    done();
  } catch (e) {
    showToast("کپی لینک با خطا مواجه شد.");
  }
  document.body.removeChild(ta);
}

// QR modal.
function showQR(text) {
  const modal = document.getElementById("qr-modal");
  const holder = document.getElementById("qrcode");
  holder.innerHTML = "";
  
  if (window.QRCode) {
    // Elegant QR generation with proper sizing
    new QRCode(holder, {
      text: text,
      width: 200,
      height: 200,
      colorDark: "#0f172a",
      colorLight: "#ffffff"
    });
  } else {
    holder.textContent = "کتابخانهٔ QR بارگذاری نشد.";
  }
  
  modal.style.display = "flex";
}

function closeQR(e) {
  if (e.target.id === "qr-modal" || e.target.closest("#close-qr-btn")) {
    document.getElementById("qr-modal").style.display = "none";
  }
}
