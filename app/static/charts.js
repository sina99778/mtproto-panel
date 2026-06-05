/* Live dashboard & stats charts (ApexCharts). Degrades gracefully:
   if ApexCharts or the network fails, server-rendered numbers/tables remain. */

function humanBytes(n) {
  n = Number(n) || 0;
  var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i];
}
function humanRate(n) { return humanBytes(n) + "/s"; }
function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = v; }
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
async function getJSON(u) { var r = await fetch(u, { credentials: "same-origin" }); return r.json(); }

var REFRESH_MS = 15000;

/* ---------- Dashboard ---------- */
function _spark(color) {
  return {
    chart: { type: "area", height: 40, sparkline: { enabled: true } },
    stroke: { curve: "smooth", width: 2 }, fill: { opacity: 0.25 },
    colors: [color], series: [{ data: [] }], tooltip: { enabled: false },
  };
}

window.initDashboard = function () {
  if (!window.ApexCharts || !document.getElementById("chart-traffic")) { _dashTilesOnly(); return; }
  var traffic = new ApexCharts(document.getElementById("chart-traffic"), {
    chart: { type: "area", height: 240, fontFamily: "inherit", toolbar: { show: false }, animations: { enabled: true } },
    series: [{ name: "سرعت", data: [] }],
    xaxis: { type: "datetime", labels: { datetimeUTC: false } },
    yaxis: { labels: { formatter: function (v) { return humanRate(v); } } },
    dataLabels: { enabled: false }, stroke: { curve: "smooth", width: 2 },
    fill: { type: "gradient", gradient: { shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0.05 } },
    colors: ["#0d6efd"], tooltip: { x: { format: "HH:mm:ss" }, y: { formatter: function (v) { return humanRate(v); } } },
  });
  traffic.render();
  var sActive = new ApexCharts(document.getElementById("spark-active"), _spark("#206bc4")); sActive.render();
  var sTraffic = new ApexCharts(document.getElementById("spark-traffic"), _spark("#2fb344")); sTraffic.render();

  async function refresh() {
    try {
      var data = await getJSON("/api/series");
      var pts = data.points || [];
      document.getElementById("chart-empty").classList.toggle("d-none", pts.length > 0);
      traffic.updateSeries([{ name: "سرعت", data: pts.map(function (p) { return [p.t, p.rate]; }) }]);
      sActive.updateSeries([{ data: pts.map(function (p) { return [p.t, p.active]; }) }]);
      sTraffic.updateSeries([{ data: pts.map(function (p) { return [p.t, p.rate]; }) }]);
    } catch (e) {}
    _dashTilesOnly();
  }
  refresh();
  setInterval(refresh, REFRESH_MS);
};

async function _dashTilesOnly() {
  try {
    var s = await getJSON("/api/stats"), t = s.totals;
    setText("tile-active", t.active);
    setText("tile-traffic", humanBytes(t.cum_bytes));
    setText("tile-sessions", t.sessions);
    setText("tile-clients", t.clients);
  } catch (e) {}
}

/* ---------- Stats ---------- */
window.initStats = function () {
  var donut = null;
  var donutEl = document.getElementById("chart-ops");

  async function refresh() {
    var s;
    try { s = await getJSON("/api/stats"); } catch (e) { return; }
    var t = s.totals;
    setText("tile-active", t.active);
    setText("tile-traffic", humanBytes(t.cum_bytes));
    setText("tile-sessions", t.sessions);
    setText("tile-clients", t.clients);

    var ops = t.operators || [];
    var emptyEl = document.getElementById("ops-empty");
    if (emptyEl) emptyEl.classList.toggle("d-none", ops.length > 0);
    if (window.ApexCharts && donutEl && ops.length) {
      var series = ops.map(function (o) { return o.c; });
      var labels = ops.map(function (o) { return o.operator; });
      if (!donut) {
        donut = new ApexCharts(donutEl, {
          chart: { type: "donut", height: 300, fontFamily: "inherit" },
          series: series, labels: labels, legend: { position: "bottom" },
          tooltip: { y: { formatter: function (v) { return v + " کاربر"; } } },
        });
        donut.render();
      } else {
        donut.updateOptions({ labels: labels }, false, false);
        donut.updateSeries(series);
      }
    }

    var tbody = document.getElementById("proxy-rows");
    if (tbody && s.proxies) {
      if (!s.proxies.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-secondary py-4">پروکسی‌ای وجود ندارد.</td></tr>';
      } else {
        tbody.innerHTML = s.proxies.map(function (p) {
          var op = (p.operators && p.operators[0]) ? p.operators[0].operator : "—";
          return "<tr>" +
            '<td class="fw-bold"><a href="/proxies/' + p.id + '">' + escapeHtml(p.name) + "</a></td>" +
            '<td><span class="badge bg-blue-lt">' + p.active + "</span></td>" +
            '<td class="text-secondary">' + humanRate(p.rate_bps) + "</td>" +
            '<td class="text-green">' + humanBytes(p.cum_bytes) + "</td>" +
            "<td>" + p.sessions + "</td><td>" + p.clients + "</td>" +
            '<td class="text-secondary">' + escapeHtml(op) + "</td></tr>";
        }).join("");
      }
    }
  }
  refresh();
  setInterval(refresh, REFRESH_MS);
};

/* ---------- Per-proxy detail ---------- */
function _fmtTime(ts) {
  try { return new Date(Number(ts) * 1000).toLocaleString("fa-IR"); } catch (e) { return "—"; }
}

window.initProxyDetail = function (pid) {
  // Fill server-rendered "last seen" cells immediately.
  document.querySelectorAll("#client-rows [data-ts]").forEach(function (td) {
    var ts = parseInt(td.getAttribute("data-ts"), 10);
    if (ts) td.textContent = _fmtTime(ts);
  });

  var traffic = null, donut = null;
  if (window.ApexCharts && document.getElementById("chart-traffic")) {
    traffic = new ApexCharts(document.getElementById("chart-traffic"), {
      chart: { type: "area", height: 240, fontFamily: "inherit", toolbar: { show: false } },
      series: [{ name: "سرعت", data: [] }],
      xaxis: { type: "datetime", labels: { datetimeUTC: false } },
      yaxis: { labels: { formatter: function (v) { return humanRate(v); } } },
      dataLabels: { enabled: false }, stroke: { curve: "smooth", width: 2 },
      fill: { type: "gradient", gradient: { shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0.05 } },
      colors: ["#0d6efd"], tooltip: { x: { format: "HH:mm:ss" }, y: { formatter: function (v) { return humanRate(v); } } },
    });
    traffic.render();
  }

  async function refresh() {
    if (traffic) {
      try {
        var d = await getJSON("/api/proxies/" + pid + "/series");
        var pts = d.points || [];
        var ce = document.getElementById("chart-empty");
        if (ce) ce.classList.toggle("d-none", pts.length > 0);
        traffic.updateSeries([{ name: "سرعت", data: pts.map(function (p) { return [p.t, p.rate]; }) }]);
      } catch (e) {}
    }
    var s;
    try { s = await getJSON("/api/proxies/" + pid + "/stats"); } catch (e) { return; }
    setText("tile-active", s.active);
    setText("tile-traffic", humanBytes(s.cum_bytes));
    setText("tile-sessions", s.sessions);
    setText("tile-clients", s.clients);

    var ops = s.operators || [];
    var oe = document.getElementById("ops-empty");
    if (oe) oe.classList.toggle("d-none", ops.length > 0);
    if (window.ApexCharts && document.getElementById("chart-ops") && ops.length) {
      var series = ops.map(function (o) { return o.c; });
      var labels = ops.map(function (o) { return o.operator; });
      if (!donut) {
        donut = new ApexCharts(document.getElementById("chart-ops"), {
          chart: { type: "donut", height: 240, fontFamily: "inherit" },
          series: series, labels: labels, legend: { position: "bottom" },
        });
        donut.render();
      } else {
        donut.updateOptions({ labels: labels }, false, false);
        donut.updateSeries(series);
      }
    }

    var tb = document.getElementById("client-rows");
    if (tb && s.client_list) {
      if (!s.client_list.length) {
        tb.innerHTML = '<tr><td colspan="4" class="text-center text-secondary py-4">هنوز کاربری ثبت نشده.</td></tr>';
      } else {
        tb.innerHTML = s.client_list.map(function (cl) {
          return "<tr><td dir='ltr' class='font-monospace'>" + escapeHtml(cl.ip) + "</td><td>" +
            escapeHtml(cl.operator || "—") + "</td><td>" + cl.sessions + "</td><td class='text-secondary'>" +
            _fmtTime(cl.last_seen) + "</td></tr>";
        }).join("");
      }
    }
  }
  refresh();
  setInterval(refresh, REFRESH_MS);
};
