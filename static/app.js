const T = window.TYPES;
const LS = { get: k => { try { return localStorage.getItem(k) } catch { return null } }, set: (k, v) => { try { localStorage.setItem(k, v) } catch {} } };
let me = { lat: 24.9994, lng: 121.4997 };   // 預設：中和
let hidden = new Set(JSON.parse(LS.get("rr_hidden") || "[]"));
let markers = {}, data = [], pickType = null;

const map = L.map("map", { zoomControl: false }).setView([me.lat, me.lng], 15);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  { attribution: "© OpenStreetMap contributors", maxZoom: 19, className: "dark-tiles" }).addTo(map);
const meMarker = L.marker([me.lat, me.lng], { icon: L.divIcon({ className: "", html: '<div class="me"></div>', iconSize: [16, 16] }) }).addTo(map);

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = t => T[t].name.slice(0, 2);
const ago = s => { const d = Math.max(0, Date.now() / 1000 - s); return d < 60 ? "剛剛" : d < 3600 ? `${Math.floor(d / 60)} 分鐘前` : d < 86400 ? `${Math.floor(d / 3600)} 小時前` : `${Math.floor(d / 86400)} 天前` };
const left = s => s < 0 ? "" : s < 3600 ? `${Math.ceil(s / 60)} 分鐘後消失` : s < 86400 ? `${Math.round(s / 3600)} 小時後消失` : `${Math.round(s / 86400)} 天後消失`;

// 可見地圖中心（扣掉下方清單）
function viewCenter() {
  const s = map.getSize(), sheet = $("sheet").offsetHeight;
  return map.containerPointToLatLng([s.x / 2, (s.y - sheet) / 2]);
}

// ── 類型篩選 ──
$("filters").innerHTML = Object.entries(T).map(([k, t]) =>
  `<button class="chip ${hidden.has(k) ? "off" : ""}" data-k="${k}"><i style="background:${t.color}"></i>${t.name}</button>`).join("");
$("filters").onclick = e => {
  const b = e.target.closest(".chip"); if (!b) return;
  const k = b.dataset.k; hidden.has(k) ? hidden.delete(k) : hidden.add(k);
  b.classList.toggle("off"); LS.set("rr_hidden", JSON.stringify([...hidden])); render();
};

async function load() {
  const c = viewCenter();
  const r = await fetch(`/api/nearby?lat=${c.lat}&lng=${c.lng}&km=${$("radius").value}`);
  data = await r.json(); render();
}

function pinIcon(d, fresh) {
  return L.divIcon({ className: "", iconSize: [30, 30], iconAnchor: [15, 30], popupAnchor: [0, -28],
    html: `<div class="pin ${fresh ? "new" : ""}" style="background:${T[d.type].color}"><span>${short(d.type)[0]}</span></div>` });
}

function render(freshId) {
  const shown = data.filter(d => !hidden.has(d.type));
  const ids = new Set(shown.map(d => d.id));
  for (const id in markers) if (!ids.has(id)) { map.removeLayer(markers[id]); delete markers[id]; }
  for (const d of shown) {
    if (!markers[d.id]) {
      markers[d.id] = L.marker([d.lat, d.lng], { icon: pinIcon(d, d.id === freshId) }).addTo(map);
      markers[d.id].on("click", () => document.querySelector(`[data-id="${d.id}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" }));
    }
    markers[d.id].bindPopup(`<b>${T[d.type].name}</b><br>${esc(d.note) || ""}<br><small>${esc(d.area)}・${ago(d.created)}</small>`);
  }
  $("list").innerHTML = shown.length ? shown.map(d => {
    const pct = Math.max(2, Math.min(100, d.ttl / d.ttl_full * 100));
    return `<div class="item" data-id="${d.id}">
      <div class="ic" style="background:${T[d.type].color}">${short(d.type)}</div>
      <div><b>${T[d.type].name}</b> <span class="meta">${d.dist} km・${esc(d.area)}</span>
        ${d.note ? `<div class="note">${esc(d.note)}</div>` : ""}
        <div class="meta">${esc(d.rider)}・${ago(d.created)}・${left(d.ttl)}${d.still ? `・👍 ${d.still}` : ""}${d.gone ? `・已排除 ${d.gone}/3` : ""}</div>
        <div class="bar"><span style="width:${pct}%;background:${T[d.type].color}"></span></div></div>
      <div class="votes"><button class="still" data-v="still">還在</button><button class="gone" data-v="gone">已排除</button></div>
    </div>`;
  }).join("") : `<div class="empty">附近沒有路況，一路順風 🏍️</div>`;
}

$("list").onclick = async e => {
  const b = e.target.closest("button[data-v]"), it = e.target.closest(".item");
  if (!it) return;
  const d = data.find(x => x.id === it.dataset.id);
  if (!b) { map.flyTo([d.lat, d.lng], 17); markers[d.id]?.openPopup(); return; }
  b.disabled = true;
  const r = await (await fetch(`/api/vote/${it.dataset.id}`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: b.dataset.v, rider: LS.get("rr_rider") || "" }) })).json();
  toast(r.ok ? (r.event === "removed" ? "已排除，路況已從地圖移除" : b.dataset.v === "still" ? "謝謝確認，路況延長 15 分鐘" : `已記錄（${r.n}/3 人說已排除）`) : r.msg || r.error);
  load();
};

// ── 回報 ──
$("types").innerHTML = Object.entries(T).map(([k, t]) => {
  const h = t.ttl / 3600;
  return `<button data-k="${k}"><i style="background:${t.color}"></i>${t.name}<small>${h < 1 ? t.ttl / 60 + " 分鐘" : h < 24 ? h + " 小時" : h / 24 + " 天"}後消失</small></button>`;
}).join("");
$("types").onclick = e => {
  const b = e.target.closest("button"); if (!b) return;
  pickType = b.dataset.k; [...$("types").children].forEach(x => x.classList.toggle("on", x === b)); $("send").disabled = false;
};
$("rider").value = LS.get("rr_rider") || "";
function placeCross() { const s = map.getSize(), r = $("map").getBoundingClientRect(); $("cross").style.top = r.top + (s.y - $("sheet").offsetHeight) / 2 + "px"; }
$("fab").onclick = () => { $("modal").hidden = false; $("cross").hidden = false; placeCross(); };
$("cancel").onclick = () => { $("modal").hidden = true; $("cross").hidden = true; };
$("send").onclick = async () => {
  const c = viewCenter(); LS.set("rr_rider", $("rider").value.trim());
  $("send").disabled = true;
  const r = await fetch("/api/report", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: pickType, lat: c.lat, lng: c.lng, note: $("note").value.trim(), rider: $("rider").value.trim() }) });
  const d = await r.json();
  if (!r.ok) { toast(d.error); $("send").disabled = false; return; }
  $("modal").hidden = true; $("cross").hidden = true; $("note").value = "";
  toast(`已回報「${T[d.type].name}」，附近騎士都會收到通知`);
};

// ── Server-Sent Events：後端從 Redis Pub/Sub 收到事件就推過來 ──
function listen() {
  const es = new EventSource("/api/events");
  es.onmessage = async ev => {
    const m = JSON.parse(ev.data);
    if (m.event === "new") {
      const d = JSON.parse(m.data);
      const km = map.distance([d.lat, d.lng], viewCenter()) / 1000;
      if (km <= +$("radius").value) toast(`<b>⚠ 前方 ${km.toFixed(1)} km：${T[d.type].name}</b>${esc(d.note)}（${esc(d.area)}）`, T[d.type].color);
      await load(); render(d.id);
    } else if (m.event === "removed" || m.event === "vote") load();
  };
}

function toast(html, color) {
  const t = document.createElement("div"); t.className = "toast"; t.innerHTML = html;
  if (color) t.style.borderLeftColor = color;
  $("toasts").prepend(t); setTimeout(() => t.remove(), 5000);
}

$("radius").onchange = load;
let mv; map.on("moveend", () => { clearTimeout(mv); mv = setTimeout(load, 250); });
$("locate").onclick = () => map.flyTo([me.lat, me.lng], 15);
if (navigator.geolocation) navigator.geolocation.getCurrentPosition(p => {
  const ll = [p.coords.latitude, p.coords.longitude];
  if (ll[0] > 21 && ll[0] < 26.5) { me = { lat: ll[0], lng: ll[1] }; meMarker.setLatLng(ll); map.setView(ll, 15); }
}, () => {}, { timeout: 4000 });
setInterval(load, 30000);
load(); listen();
