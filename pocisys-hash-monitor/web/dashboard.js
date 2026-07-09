const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "—").replace(/[&<>"']/g, char => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]
));

let state = null;
let managedMiners = [];
let managedPools = [];
let settings = null;
let activeGroup = "All";
let pointerStart = null;
let suppressNextClick = false;

const DEFAULT_SETTINGS = {
  poll_interval_seconds: 10,
  request_timeout_seconds: 4,
  alert_cooldown_seconds: 600,
  dashboard_port: 8765,
  dashboard_density: "comfortable",
  dashboard_base_url: "",
  lan_access_enabled: false,
  discord_enabled: false,
  webhook_configured: false,
  send_offline_alerts: true,
  send_recovery_alerts: true,
  send_hashrate_alerts: true,
  send_temperature_alerts: true,
  send_best_diff_alerts: true,
  send_block_found_alerts: true,
  send_pool_alerts: true,
  send_pool_switch_alerts: true,
  send_share_alerts: true,
  verbose_pool_events: false,
  btc_enabled: true,
  bch_enabled: true,
  auto_network_data: true,
  manual_btc_network_hashrate_eh: null,
  manual_bch_network_hashrate_eh: null,
};

function number(value, decimals = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString(undefined, {maximumFractionDigits: decimals});
}

function nullableNumber(value) {
  return value === "" || value === null || value === undefined ? null : Number(value);
}

function compactHashrate(ths) {
  if (ths === null || ths === undefined) return ["—", "TH/s"];
  if (ths < 0.001) return [number(ths * 1e6, 1), "MH/s"];
  if (ths < 1) return [number(ths * 1000, 2), "GH/s"];
  return [number(ths, 2), "TH/s"];
}

function difficulty(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (/[KMGTP]$/i.test(String(value).trim())) return String(value);
  const parsed = Number(String(value).replaceAll(",", ""));
  if (!Number.isFinite(parsed)) return String(value);
  const units = [["P", 1e15], ["T", 1e12], ["G", 1e9], ["M", 1e6], ["K", 1e3]];
  const unit = units.find(([, threshold]) => parsed >= threshold);
  return unit ? `${number(parsed / unit[1], 2)}${unit[0]}` : number(parsed, 0);
}

function percent(chance) {
  if (chance === null || chance === undefined) return "—";
  const value = chance * 100;
  if (!value) return "0%";
  if (value < 0.000001) return value.toExponential(2) + "%";
  if (value < 0.01) return value.toFixed(6) + "%";
  return value.toFixed(3) + "%";
}

function duration(days) {
  if (!days) return "Never at 0 H/s";
  if (days < 1) return `${number(days * 24, 1)} hours`;
  if (days < 365) return `${number(days, 1)} days`;
  if (days < 365000) return `${number(days / 365, 1)} years`;
  return `${Number(days / 365).toExponential(2)} years`;
}

function uptime(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return [days ? `${days}d` : "", hours ? `${hours}h` : "", `${minutes}m`].filter(Boolean).join(" ");
}

function highestTemp(miner) {
  const values = Object.values(miner?.temps || {}).filter(value => value !== null);
  return values.length ? Math.max(...values) : null;
}

function fanSummary(miner) {
  const rpms = (miner?.fans || []).map(fan => Number(fan.rpm)).filter(Number.isFinite);
  if (!rpms.length) return "Not reported";
  if (rpms.length === 1) return `${number(rpms[0], 0)} RPM`;
  return `${rpms.length} fans · ${number(Math.min(...rpms), 0)}–${number(Math.max(...rpms), 0)} RPM`;
}

function money(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString(undefined, {style: "currency", currency: "USD", maximumFractionDigits: value < 1000 ? 2 : 0});
}

function statusFor(id) {
  return state?.miners?.find(miner => miner.id === id) || null;
}

function configuredMiner(id) {
  return managedMiners.find(item => item.config.id === id)?.config || null;
}

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

async function request(path, {method = "GET", body, retries = 8} = {}) {
  let lastError = null;
  const payload = body === undefined ? undefined : JSON.stringify(body);
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(path, {
        method,
        cache: "no-store",
        headers: body === undefined ? {} : {"Content-Type": "application/json"},
        body: payload,
      });
      const data = await response.json().catch(() => ({}));
      if (response.ok) return data;
      let message = data.detail || `Request failed (${response.status})`;
      if (Array.isArray(message)) message = message.map(item => item.msg).join(" · ");
      lastError = new Error(message);
      if (![502, 503, 504].includes(response.status)) throw lastError;
    } catch (error) {
      lastError = error;
    }
    if (attempt < retries) {
      await sleep(Math.min(7500, 400 + attempt * 650));
    }
  }
  throw lastError || new Error("Request failed");
}

function toast(message, kind = "info") {
  const element = $("#toast");
  element.textContent = message;
  element.className = `show ${kind}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.className = "", 3800);
}

function navigate(path) {
  history.pushState({}, "", path);
  route();
}

function route() {
  const path = location.pathname.replace(/\/+$/, "") || "/";
  const detailMatch = path.match(/^\/miners\/([^/]+)$/);
  const known = {"/":"dashboard", "/miners":"miners", "/pools":"pools", "/odds":"odds", "/alerts":"alerts", "/settings":"settings"};
  const page = detailMatch ? "miner-detail" : (known[path] || "dashboard");
  $$(".page").forEach(element => element.classList.toggle("active", element.id === `page-${page}`));
  $$("nav a").forEach(element => {
    const target = element.getAttribute("href");
    element.classList.toggle("active", target === path || (detailMatch && target === "/miners"));
  });
  if (detailMatch) renderMinerDetail(detailMatch[1]);
  if (page === "pools") loadPoolEvents();
  if (page === "settings" && settings) fillSettings();
  if (location.hash) setTimeout(() => document.querySelector(location.hash)?.scrollIntoView({behavior: "smooth"}), 40);
  window.scrollTo({top: 0, behavior: "instant"});
}

function summaryMetric(label, value, sub = "", className = "") {
  return `<div class="metric"><span class="metric-label">${label}</span><strong class="metric-value ${className}">${value}</strong><span class="metric-sub">${sub}</span></div>`;
}

function renderSummary() {
  const summary = state.summary;
  const onlineClass = !summary.total_miners ? "" : summary.online_miners === summary.total_miners ? "good" : summary.online_miners ? "warn" : "bad";
  $("#summary-grid").innerHTML = [
    summaryMetric("Total hashrate", `${number(summary.total_hashrate_ths, 3)} <small>TH/s</small>`, "Live enabled devices", "good"),
    summaryMetric("Miners online", `${summary.online_miners} / ${summary.total_miners}`, `${summary.configured_miners} configured`, onlineClass),
    summaryMetric("Average response", summary.average_ping_ms === null ? "—" : `${number(summary.average_ping_ms)} ms`, "Miner API timing"),
    summaryMetric("Peak temperature", summary.highest_temperature_c === null ? "—" : `${number(summary.highest_temperature_c)}°C`, "Across current sensors"),
    summaryMetric("Valid shares", number(summary.total_valid_shares, 0), "Current counters"),
    summaryMetric("Bad shares", number(summary.total_bad_shares, 0), "Invalid + stale + rejected", summary.total_bad_shares ? "warn" : ""),
    summaryMetric("BTC daily odds", state.odds.btc.available ? percent(state.odds.btc.daily_chance) : "—", state.odds.btc.network_source || "Needs setup"),
    summaryMetric("BCH daily odds", state.odds.bch.available ? percent(state.odds.bch.daily_chance) : "—", state.odds.bch.network_source || "Needs setup"),
  ].join("");
  $("#last-poll").textContent = summary.last_poll ? `Updated ${new Date(summary.last_poll).toLocaleTimeString()}` : "Waiting for first poll…";
}

function minerCard(miner) {
  const [hash, unit] = compactHashrate(miner.hashrate_ths);
  const temp = highestTemp(miner);
  const fans = fanSummary(miner);
  const badShares = ["invalid", "stale", "rejected"].reduce((sum, key) => sum + (miner.shares?.[key] || 0), 0);
  const healthy = miner.online && miner.api_ok;
  return `<article class="miner-card ${healthy ? "online" : "offline"}" data-action="open-miner" data-id="${escapeHtml(miner.id)}" tabindex="0">
    <div class="card-glow"></div>
    <div class="miner-head"><i class="status-dot"></i><div class="miner-title"><strong>${escapeHtml(miner.name)}</strong><small>${escapeHtml(miner.ip)}</small></div><span class="type-badge">${escapeHtml(miner.type)}</span></div>
    <div class="hashrate"><strong>${hash}</strong><span>${unit}</span><small>${escapeHtml(miner.group)}</small></div>
    <div class="miner-details">
      <div class="detail"><label>Temperature</label><span>${temp === null ? "—" : number(temp) + "°C"}</span></div>
      <div class="detail"><label>API response</label><span>${miner.ping_ms === null ? "Failed" : number(miner.ping_ms) + " ms"}</span></div>
      <div class="detail"><label>Fan</label><span title="${escapeHtml(fans)}">${escapeHtml(fans)}</span></div>
      <div class="detail"><label>Pool</label><span title="${escapeHtml(miner.pool?.url)}">${escapeHtml(miner.pool?.status || "unknown")}</span></div>
      <div class="detail"><label>Valid shares</label><span>${number(miner.shares?.valid || 0, 0)}</span></div>
      <div class="detail"><label>Bad shares</label><span>${number(badShares, 0)}</span></div>
      <div class="detail"><label>Best session</label><span title="${escapeHtml(miner.difficulty?.best_session)}">${escapeHtml(difficulty(miner.difficulty?.best_session))}</span></div>
      <div class="detail"><label>Best all-time</label><span title="${escapeHtml(miner.difficulty?.best_all_time)}">${escapeHtml(difficulty(miner.difficulty?.best_all_time))}</span></div>
    </div>
    <div class="card-foot"><span>${healthy ? "Healthy" : escapeHtml(miner.status)}</span><strong>View details →</strong></div>
    ${(miner.warnings || []).length ? `<div class="warning-strip">${escapeHtml(miner.warnings[0])}</div>` : ""}
  </article>`;
}

function onboarding() {
  return `<section class="onboarding">
    <div class="onboarding-visual" aria-hidden="true">
      <div class="orbit orbit-one"><i></i></div>
      <div class="orbit orbit-two"><i></i></div>
      <div class="core-mark">P</div>
      <span class="signal signal-a"></span><span class="signal signal-b"></span><span class="signal signal-c"></span>
    </div>
    <div class="onboarding-copy"><p class="eyebrow">Your command center is ready</p><h2>Bring your first miner online</h2><p>Add an AxeOS, NerdQaxe, or LuxOS device. PoCiSys will test it, start live monitoring, and fold it into your fleet totals—without storing historical telemetry.</p>
      <div class="onboarding-steps"><span><b>1</b>Add its LAN address</span><span><b>2</b>Test the API</span><span><b>3</b>Watch it live</span></div>
      <button class="primary" data-action="add-miner">Add your first miner</button>
    </div>
  </section>`;
}

function renderMiners() {
  const miners = state.miners || [];
  const groups = ["All", ...new Set(miners.map(miner => miner.group || "Ungrouped"))];
  if (!groups.includes(activeGroup)) activeGroup = "All";
  $("#group-filters").innerHTML = miners.length > 1 ? groups.map(group => `<button class="filter-chip ${group === activeGroup ? "active" : ""}" data-action="filter-group" data-group="${escapeHtml(group)}">${escapeHtml(group)}</button>`).join("") : "";
  if (!miners.length) {
    $("#dashboard-miners").innerHTML = onboarding();
    return;
  }
  const visible = activeGroup === "All" ? miners : miners.filter(miner => (miner.group || "Ungrouped") === activeGroup);
  const grouped = Object.groupBy ? Object.groupBy(visible, miner => miner.group || "Ungrouped") : visible.reduce((result, miner) => {
    (result[miner.group || "Ungrouped"] ||= []).push(miner);
    return result;
  }, {});
  const density = state.ui?.dashboard_density === "compact" ? "compact" : "comfortable";
  $("#dashboard-miners").innerHTML = Object.entries(grouped).map(([group, items]) => `<section class="miner-group"><div class="group-label"><span>${escapeHtml(group)}</span><small>${items.length} miner${items.length === 1 ? "" : "s"}</small></div><div class="miner-grid ${density}">${items.map(minerCard).join("")}</div></section>`).join("");
}

function renderManagedMiners() {
  if (!managedMiners.length) {
    $("#miners-table").innerHTML = `<div class="empty-action"><span class="empty-icon">⛏</span><h2>No miners configured</h2><p>Add a device to start building your fleet.</p><button class="primary" data-action="add-miner">Add miner</button></div>`;
    return;
  }
  $("#miners-table").innerHTML = `<table><thead><tr><th>Order</th><th>Status</th><th>Miner</th><th>OS / API</th><th>Group</th><th>Hashrate</th><th>Temperature</th><th>Alerts</th><th>Actions</th></tr></thead><tbody>${managedMiners.map((entry, index) => {
    const config = entry.config;
    const status = statusFor(config.id);
    const [hash, unit] = compactHashrate(status?.hashrate_ths);
    const liveClass = !config.enabled ? "muted-status" : status?.api_ok ? "good" : "bad";
    const liveText = !config.enabled ? "Disabled" : status?.api_ok ? "Online" : "Offline";
    return `<tr>
      <td><div class="order-buttons"><button data-action="move-miner" data-id="${config.id}" data-direction="-1" ${index === 0 ? "disabled" : ""}>↑</button><button data-action="move-miner" data-id="${config.id}" data-direction="1" ${index === managedMiners.length - 1 ? "disabled" : ""}>↓</button></div></td>
      <td class="${liveClass}">● ${liveText}</td>
      <td><button class="table-link" data-action="open-miner" data-id="${config.id}"><strong>${escapeHtml(config.name)}</strong><small>${escapeHtml(config.ip)}</small></button></td>
      <td><span class="type-badge">${escapeHtml(config.type)}</span></td><td>${escapeHtml(config.group)}</td>
      <td>${status ? `${hash} ${unit}` : "—"}</td><td>${status && highestTemp(status) !== null ? number(highestTemp(status)) + "°C" : "—"}</td>
      <td><small>${config.min_hashrate_ths != null ? `Min ${number(config.min_hashrate_ths, 3)} TH/s` : status?.expected_hashrate_ths ? `Auto · 75% of ${number(status.expected_hashrate_ths, 3)} TH/s` : "No hash minimum"} · ${number(config.temp_warning_c, 0)}° / ${number(config.temp_critical_c, 0)}°</small></td>
      <td><div class="row-actions"><button data-action="edit-miner" data-id="${config.id}">Edit</button><button class="danger-text" data-action="delete-miner" data-id="${config.id}">Delete</button></div></td>
    </tr>`;
  }).join("")}</tbody></table>`;
}

function detailStat(label, value, sub = "") {
  return `<div class="detail-stat"><label>${label}</label><strong>${value}</strong>${sub ? `<small>${sub}</small>` : ""}</div>`;
}

function infoRow(label, value, suffix = "") {
  if (value === null || value === undefined || value === "") return "";
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}${suffix}</strong></div>`;
}

function renderChipHealth(status) {
  const health = status?.chip_health || {};
  const items = health.items || [];
  if (!health.reported || !items.length) {
    return `<div class="empty compact-empty">Chip-level health is not reported by this firmware.</div>`;
  }
  return `<div class="chip-grid">${items.map(item => {
    const count = item.chips_total ? `${number(item.chips_healthy, 0)} / ${number(item.chips_total, 0)} ASICs` : item.cores ? `${number(item.cores, 0)} cores reported` : "API health signal";
    const hash = item.hashrate_ths == null ? "" : `${number(item.hashrate_ths, 2)} TH/s`;
    const temp = item.temperature_c == null ? "" : `${number(item.temperature_c)}°C`;
    const meta = [count, hash, temp].filter(Boolean).join(" · ");
    return `<article class="chip-card ${item.status === "healthy" ? "healthy" : "warning"}"><div><i></i><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.status || "reported")}</span></div><p>${escapeHtml(meta)}</p>${item.hardware_errors != null ? `<small>${number(item.hardware_errors, 0)} hardware errors</small>` : ""}</article>`;
  }).join("")}</div>`;
}

function renderMinerDetail(id) {
  const config = configuredMiner(id);
  if (!config) {
    $("#miner-detail").innerHTML = `<div class="empty-action"><h2>Miner not found</h2><button data-link href="/miners">Back to miners</button></div>`;
    return;
  }
  const status = statusFor(id);
  const healthy = status?.api_ok;
  const [hash, unit] = compactHashrate(status?.hashrate_ths);
  const temps = status?.temps || {};
  const shares = status?.shares || {};
  const temperatureItems = [
    ["ASIC", temps.asic_c], ["VRM", temps.vrm_c], ["Board", temps.board_c], ["Chip", temps.chip_c],
  ].filter(([, value]) => value !== null && value !== undefined);
  const expected = Number(status?.expected_hashrate_ths || 0);
  const performance = expected > 0 ? Math.max(0, Math.min(125, Number(status?.hashrate_ths || 0) / expected * 100)) : null;
  $("#miner-detail").innerHTML = `<div class="detail-top">
    <button class="back-link" data-link href="/miners">← All miners</button>
    <div class="detail-actions"><a class="button-link" href="http://${escapeHtml(config.ip)}" target="_blank" rel="noopener">Open native dashboard ↗</a><button data-action="edit-miner" data-id="${config.id}">Edit setup</button></div>
  </div>
  <section class="miner-hero ${healthy ? "online" : "offline"}">
    <div class="hero-grid"></div><div class="hero-ident"><span class="hero-status"><i class="status-dot"></i>${!config.enabled ? "Monitoring disabled" : healthy ? "Live and healthy" : status?.status || "Waiting for miner"}</span><p class="eyebrow">${escapeHtml(config.group)}</p><h1>${escapeHtml(config.name)}</h1><p>${escapeHtml(config.ip)} · ${escapeHtml(config.type)} · ${escapeHtml(status?.firmware || "Firmware unavailable")}</p></div>
    <div class="hero-hash"><strong>${hash}</strong><span>${unit}</span><small>Current hashrate</small></div>
  </section>
  <div class="detail-stat-grid">
    ${detailStat("Peak temperature", highestTemp(status) === null ? "—" : number(highestTemp(status)) + "°C", "Current sensors")}
    ${detailStat("API response", status?.ping_ms === null || status?.ping_ms === undefined ? "—" : number(status.ping_ms) + " ms", status?.api_ok ? "API healthy" : "API unavailable")}
    ${detailStat("Uptime", uptime(status?.uptime_seconds), "Miner reported")}
    ${detailStat("Best difficulty", escapeHtml(difficulty(status?.difficulty?.best_all_time || status?.difficulty?.best_session)), "Best available")}
  </div>
  <div class="detail-panels">
    <section class="panel"><div class="panel-title"><h2>Hardware</h2><span class="type-badge">${escapeHtml(config.type)}</span></div><div class="info-list">
      ${infoRow("Firmware", status?.firmware)}
      ${infoRow("Frequency", status?.frequency_mhz == null ? null : number(status.frequency_mhz), " MHz")}
      ${infoRow("Voltage", status?.voltage_mv == null ? null : number(status.voltage_mv), " mV")}
      ${infoRow("Wi-Fi signal", status?.wifi_rssi == null ? null : number(status.wifi_rssi), " dBm")}
      ${infoRow("Expected hashrate", status?.expected_hashrate_ths == null ? "Not reported" : `${number(status.expected_hashrate_ths, 3)} TH/s`)}
      ${infoRow("Blocks found", number(status?.blocks_found || 0, 0))}
      ${infoRow("Hardware errors", status?.hardware_errors == null ? "Not reported" : number(status.hardware_errors, 0))}
      ${infoRow("Cooling", fanSummary(status))}
    </div>${performance == null ? "" : `<div class="performance"><span><b>Live performance</b><strong>${number(performance, 1)}%</strong></span><div><i style="width:${Math.min(performance, 100)}%"></i></div></div>`}</section>
    <section class="panel"><div class="panel-title"><h2>Temperatures & cooling</h2></div>
      ${temperatureItems.length ? `<div class="sensor-grid">${temperatureItems.map(([label, value]) => detailStat(label, `${number(value)}°C`)).join("")}</div>` : `<div class="empty compact-empty">No temperature sensors reported.</div>`}
      ${status?.fans?.length ? `<div class="fan-grid">${status.fans.map(fan => `<div><span>${escapeHtml(fan.name)}</span><strong>${number(fan.rpm, 0)} RPM</strong></div>`).join("")}</div>` : ""}
      <div class="threshold-note">Alerts at ${number(config.temp_warning_c)}°C · Critical at ${number(config.temp_critical_c)}°C</div></section>
    <section class="panel chip-health-panel"><div class="panel-title"><h2>Chip health</h2><span class="pill">${status?.chip_health?.reported ? `${number(status.chip_health.healthy, 0)} / ${number(status.chip_health.total, 0)} healthy` : "Not reported"}</span></div>${renderChipHealth(status)}</section>
    <section class="panel"><div class="panel-title"><h2>Pool</h2><span class="${status?.pool?.connected ? "good" : "warn"}">${escapeHtml(status?.pool?.status)}</span></div><div class="pool-url">${escapeHtml(status?.pool?.url || "Pool URL not reported")}</div><div class="info-list"><div><span>Connection</span><strong>${status?.pool?.connected == null ? "Unknown" : status.pool.connected ? "Connected" : "Disconnected"}</strong></div>${infoRow("Source", status?.pool?.source)}<div><span>Valid shares</span><strong>${number(shares.valid || 0, 0)}</strong></div></div></section>
    <section class="panel"><div class="panel-title"><h2>Share quality</h2></div><div class="sensor-grid">${detailStat("Valid", number(shares.valid || 0, 0))}${detailStat("Invalid", number(shares.invalid || 0, 0))}${detailStat("Stale", number(shares.stale || 0, 0))}${detailStat("Rejected", number(shares.rejected || 0, 0))}</div></section>
  </div>`;
}

function networkLine(item) {
  if (!item.available) return `<div class="network-line"><span class="coin ${item.symbol === "BCH" ? "bch" : ""}">${item.symbol === "BTC" ? "₿" : "B"}</span><div><strong>${item.symbol}</strong><small>${escapeHtml(item.message)}</small></div><strong>—</strong></div>`;
  return `<div class="network-line"><span class="coin ${item.symbol === "BCH" ? "bch" : ""}">${item.symbol === "BTC" ? "₿" : "B"}</span><div><strong>${item.symbol}</strong><small>${money(item.price_usd)} · ${number(item.network_hashrate_eh, 2)} EH/s · ${item.network_source}</small></div><strong>${percent(item.daily_chance)} / day</strong></div>`;
}

function oddsCard(item) {
  if (!item.available) return `<article class="odds-card"><header><span class="coin">${item.symbol}</span><div><h2>${item.symbol}</h2><p>${escapeHtml(item.message)}</p></div></header></article>`;
  return `<article class="odds-card"><header><span class="coin ${item.symbol === "BCH" ? "bch" : ""}">${item.symbol === "BTC" ? "₿" : "B"}</span><div><h2>${item.symbol}</h2><p>${number(item.network_hashrate_eh, 3)} EH/s · ${item.network_source}</p></div><strong class="coin-price">${money(item.price_usd)}</strong></header><div class="odds-stats">
    <div class="odds-stat"><label>Daily chance</label><strong>${percent(item.daily_chance)}</strong></div><div class="odds-stat"><label>Weekly chance</label><strong>${percent(item.weekly_chance)}</strong></div>
    <div class="odds-stat"><label>Network difficulty</label><strong>${difficulty(item.difficulty)}</strong></div><div class="odds-stat"><label>Estimated time</label><strong>${duration(item.estimated_days_to_block)}</strong></div>
  </div></article>`;
}

function renderOdds() {
  $("#dashboard-odds").innerHTML = networkLine(state.odds.btc) + networkLine(state.odds.bch);
  $("#odds-content").innerHTML = oddsCard(state.odds.btc) + oddsCard(state.odds.bch);
}

function renderDashboardPools() {
  const pools = state.pools || [];
  $("#dashboard-pools").innerHTML = pools.length ? pools.map(pool => `<div class="network-line"><span class="status-dot" style="background:${pool.available ? "var(--green)" : "var(--red)"}"></span><div><strong>${escapeHtml(pool.name)}</strong><small>${escapeHtml(pool.message || (pool.mode === "local_log" ? "Local log monitor" : "Waiting for API"))}</small></div><strong>${pool.available && pool.total_hashrate_ths != null ? `${number(pool.total_hashrate_ths, 2)} TH/s` : pool.enabled ? "Enabled" : "Disabled"}</strong></div>`).join("") : `<div class="empty-inline"><span>No pool monitors yet.</span><button data-action="add-pool">Add one</button></div>`;
}

function renderManagedPools() {
  if (!managedPools.length) {
    $("#pool-management").innerHTML = `<div class="empty-action pool-empty"><span class="empty-icon">≋</span><h2>No pool monitors</h2><p>Auto-detect Public Pool from your miner connections, or add a local log viewer.</p><button data-action="add-pool">Connect Public Pool</button></div>`;
    return;
  }
  $("#pool-management").innerHTML = managedPools.map(entry => {
    const pool = entry.config;
    const live = state?.pools?.find(item => item.name === pool.name);
    const source = pool.mode === "public_pool_api" ? pool.api_url : pool.log_path;
    const stats = pool.mode === "public_pool_api" && live?.available ? `<div class="pool-live-stats"><span><small>Pool hashrate</small><strong>${number(live.total_hashrate_ths, 2)} TH/s</strong></span><span><small>Connected miners</small><strong>${number(live.total_miners, 0)}</strong></span><span><small>Block height</small><strong>${number(live.block_height, 0)}</strong></span><span><small>Your workers</small><strong>${live.workers_count == null ? "Address not detected" : number(live.workers_count, 0)}</strong></span></div>${live.workers?.length ? `<div class="worker-list">${live.workers.map(worker => `<div><strong>${escapeHtml(worker.name)}</strong><span>${number(worker.hashrate_ths, 2)} TH/s</span><span>Best ${difficulty(worker.best_difficulty)}</span></div>`).join("")}</div>` : ""}` : "";
    return `<article class="manage-card ${pool.mode === "public_pool_api" ? "public-pool-card" : ""}"><div class="manage-card-top"><span class="settings-icon">${pool.mode === "public_pool_api" ? "P" : "≋"}</span><div><h2>${escapeHtml(pool.name)}</h2><p>${pool.mode === "public_pool_api" ? "Public Pool · live API" : `${escapeHtml(pool.type)} · local log`}</p></div><span class="pill ${live?.available ? "good-border" : "bad-border"}">${!pool.enabled ? "Disabled" : live?.available ? "Connected" : "Unavailable"}</span></div><div class="path-box">${escapeHtml(source)}</div>${stats}<div class="manage-card-actions"><button data-action="edit-pool" data-id="${pool.id}">Edit</button><button class="danger-text" data-action="delete-pool" data-id="${pool.id}">Delete</button></div></article>`;
  }).join("");
}

function alertFeed(events) {
  if (!events?.length) return `<div class="empty">No alert activity yet.</div>`;
  return events.map(event => `<div class="feed-item"><span class="feed-time">${new Date(event.time).toLocaleTimeString()}</span><span class="feed-source">${escapeHtml(event.source)}</span><div class="feed-message"><strong class="${event.severity === "critical" ? "bad" : event.severity === "warning" ? "warn" : ""}">${escapeHtml(event.title)}</strong><span>${escapeHtml(event.message)}</span></div></div>`).join("");
}

function renderAlerts() {
  const discord = state.discord;
  const label = discord.discord_enabled && discord.discord_configured ? "Ready" : discord.discord_configured ? "Configured but disabled" : "Not configured";
  $("#discord-status").innerHTML = `<div class="discord-card"><span class="discord-icon">D</span><div><strong>Dashboard-wide Discord webhook · <span class="${discord.discord_enabled && discord.discord_configured ? "good" : "warn"}">${label}</span></strong><p>One webhook covers every enabled miner. Block events send immediately; other repeated conditions observe the configured cooldown.</p></div><a class="button-link" href="/settings" data-link>Configure</a></div>`;
  $("#alert-events").innerHTML = alertFeed(discord.recent);
}

async function loadPoolEvents() {
  try {
    const events = (await request("/api/pool-events")).events || [];
    $("#pool-event-total").textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
    $("#pool-events").innerHTML = events.length ? events.map(event => `<div class="feed-item"><span class="feed-time">${new Date(event.time).toLocaleTimeString()}</span><span class="feed-source">${escapeHtml(event.pool)}</span><div class="feed-message"><strong class="${event.severity === "critical" ? "bad" : event.severity === "warning" ? "warn" : ""}">${escapeHtml(event.title)}</strong><span>${escapeHtml(event.line)}</span></div></div>`).join("") : `<div class="empty">Waiting for new important pool events.</div>`;
  } catch (_) {}
}

function renderAll() {
  if (!state) return;
  renderSummary();
  renderMiners();
  renderManagedMiners();
  renderOdds();
  renderDashboardPools();
  renderManagedPools();
  renderAlerts();
  const detailMatch = location.pathname.match(/^\/miners\/([^/]+)$/);
  if (detailMatch) renderMinerDetail(detailMatch[1]);
}

async function refresh() {
  try {
    state = await request("/api/status");
    renderAll();
    $(".connection").className = "connection live";
    $("#connection-text").textContent = "Live · Read-only";
    if (location.pathname === "/pools") loadPoolEvents();
  } catch (error) {
    $(".connection").className = "connection down";
    $("#connection-text").textContent = "Disconnected";
  }
}

async function loadManagement() {
  const [miners, pools] = await Promise.all([request("/api/miners"), request("/api/pools")]);
  managedMiners = miners.miners || [];
  managedPools = pools.pools || [];
  renderAll();
  route();
}

async function loadSettings() {
  try {
    settings = await request("/api/settings");
  } catch (error) {
    settings = {...DEFAULT_SETTINGS};
    toast(`Settings using defaults until API reconnects: ${error.message}`, "warning");
  }
  fillSettings();
}

function fillSettings() {
  if (!settings) return;
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(settings)) {
    const field = form.elements.namedItem(key);
    if (!field) continue;
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
  }
  $("#webhook-state").textContent = settings.webhook_configured ? "A webhook is configured and hidden." : "No webhook configured.";
}

function setFormValue(form, name, value) {
  const field = form.elements.namedItem(name);
  if (!field) return;
  if (field.type === "checkbox") field.checked = Boolean(value);
  else field.value = value ?? "";
}

function openMinerDialog(id = null) {
  const dialog = $("#miner-dialog");
  const form = $("#miner-form");
  form.reset();
  setFormValue(form, "id", "");
  setFormValue(form, "enabled", true);
  setFormValue(form, "group", "BTC Solo");
  setFormValue(form, "temp_warning_c", 70);
  setFormValue(form, "temp_critical_c", 80);
  setFormValue(form, "display_order", managedMiners.length + 1);
  $("#miner-test-result").innerHTML = "";
  if (id) {
    const config = configuredMiner(id);
    if (!config) return;
    Object.entries(config).forEach(([key, value]) => setFormValue(form, key, value));
    $("#miner-dialog-title").textContent = `Edit ${config.name}`;
  } else {
    $("#miner-dialog-title").textContent = "Add miner";
  }
  dialog.showModal();
  setTimeout(() => form.elements.name.focus(), 20);
}

function openPoolDialog(id = null) {
  const dialog = $("#pool-dialog");
  const form = $("#pool-form");
  form.reset();
  setFormValue(form, "id", "");
  setFormValue(form, "enabled", true);
  setFormValue(form, "name", "Public Pool");
  setFormValue(form, "type", "public_pool");
  setFormValue(form, "mode", "public_pool_api");
  $("#pool-detect-result").innerHTML = "";
  if (id) {
    const config = managedPools.find(item => item.config.id === id)?.config;
    if (!config) return;
    Object.entries(config).forEach(([key, value]) => setFormValue(form, key, value));
    $("#pool-dialog-title").textContent = `Edit ${config.name}`;
  } else {
    $("#pool-dialog-title").textContent = "Connect pool";
  }
  syncPoolFields();
  dialog.showModal();
}

function syncPoolFields() {
  const form = $("#pool-form");
  const publicPool = form.elements.mode.value === "public_pool_api";
  $$(".public-pool-field", form).forEach(field => field.hidden = !publicPool);
  $$(".local-log-field", form).forEach(field => field.hidden = publicPool);
  form.elements.type.value = publicPool ? "public_pool" : "ckpool";
}

async function moveMiner(id, direction) {
  const ids = managedMiners.map(item => item.config.id);
  const index = ids.indexOf(id);
  const target = index + Number(direction);
  if (index < 0 || target < 0 || target >= ids.length) return;
  [ids[index], ids[target]] = [ids[target], ids[index]];
  await request("/api/miners/reorder", {method: "POST", body: {ids}});
  await loadManagement();
  toast("Miner order updated.", "success");
}

async function deleteMiner(id) {
  const config = configuredMiner(id);
  if (!config || !confirm(`Remove “${config.name}” from PoCiSys? This does not change the miner itself.`)) return;
  await request(`/api/miners/${encodeURIComponent(id)}`, {method: "DELETE"});
  if (location.pathname.includes(id)) navigate("/miners");
  await Promise.all([loadManagement(), refresh()]);
  toast("Miner removed.", "success");
}

async function deletePool(id) {
  const config = managedPools.find(item => item.config.id === id)?.config;
  if (!config || !confirm(`Remove the “${config.name}” pool monitor? The original log file is untouched.`)) return;
  await request(`/api/pools/${encodeURIComponent(id)}`, {method: "DELETE"});
  await Promise.all([loadManagement(), refresh()]);
  toast("Pool monitor removed.", "success");
}

async function testDiscord(button) {
  button.disabled = true;
  try {
    const result = await request("/api/test-discord", {method: "POST"});
    toast(result.sent ? "Discord test sent." : `Not sent: ${result.reason || "unknown error"}`, result.sent ? "success" : "warning");
    await refresh();
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("click", async event => {
  if (suppressNextClick || window.getSelection()?.toString()) {
    suppressNextClick = false;
    return;
  }
  const link = event.target.closest("[data-link]");
  if (link) {
    event.preventDefault();
    navigate(link.getAttribute("href"));
    return;
  }
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  try {
    if (action === "add-miner") openMinerDialog();
    if (action === "edit-miner") openMinerDialog(target.dataset.id);
    if (action === "delete-miner") await deleteMiner(target.dataset.id);
    if (action === "move-miner") await moveMiner(target.dataset.id, target.dataset.direction);
    if (action === "open-miner") navigate(`/miners/${encodeURIComponent(target.dataset.id)}`);
    if (action === "add-pool") openPoolDialog();
    if (action === "edit-pool") openPoolDialog(target.dataset.id);
    if (action === "delete-pool") await deletePool(target.dataset.id);
    if (action === "close-dialog") target.closest("dialog").close();
    if (action === "filter-group") { activeGroup = target.dataset.group; renderMiners(); }
    if (action === "poll-now") {
      target.disabled = true;
      await request("/api/poll-now", {method: "POST"});
      await refresh();
      toast("Fleet poll complete.", "success");
      target.disabled = false;
    }
    if (action === "test-discord") await testDiscord(target);
    if (action === "detect-public-pool") {
      const form = $("#pool-form");
      target.disabled = true;
      $("#pool-detect-result").innerHTML = `<span class="testing-pulse"></span> Looking at miner pool connections…`;
      const result = await request("/api/pools/discover", {method: "POST", body: {host: null}});
      if (result.ok) {
        form.elements.api_url.value = result.api_url;
        $("#pool-detect-result").innerHTML = `<strong class="good">Public Pool found</strong><span>${escapeHtml(result.api_url)} · ${number(result.total_miners, 0)} connected miners</span>`;
      } else {
        $("#pool-detect-result").innerHTML = `<strong class="bad">Public Pool not found</strong><span>Enter its API URL manually. The stratum URL itself is not the web API.</span>`;
      }
      target.disabled = false;
    }
    if (action === "test-miner") {
      const form = $("#miner-form");
      const ip = form.elements.ip.value.trim();
      if (!ip) { form.elements.ip.reportValidity(); return; }
      target.disabled = true;
      $("#miner-test-result").innerHTML = `<span class="testing-pulse"></span> Contacting miner…`;
      const result = await request("/api/miners/test", {method: "POST", body: {ip, type: form.elements.type.value}});
      $("#miner-test-result").innerHTML = result.ok
        ? `<strong class="good">Connection successful</strong><span>${escapeHtml(result.status.firmware || result.status.type)} · ${number(result.status.hashrate_ths, 3)} TH/s · ${number(result.status.ping_ms)} ms</span>`
        : `<strong class="bad">Could not reach this API</strong><span>${escapeHtml(result.error || result.status?.warnings?.[0] || "Check the address and miner type.")}</span>`;
      target.disabled = false;
    }
  } catch (error) {
    target.disabled = false;
    toast(error.message, "error");
  }
});

document.addEventListener("keydown", event => {
  const card = event.target.closest?.('[data-action="open-miner"]');
  if (card && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    navigate(`/miners/${encodeURIComponent(card.dataset.id)}`);
  }
});

$("#miner-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value,
    ip: form.elements.ip.value,
    type: form.elements.type.value,
    group: form.elements.group.value || "Ungrouped",
    enabled: form.elements.enabled.checked,
    display_order: Number(form.elements.display_order.value || 0),
    min_hashrate_ths: nullableNumber(form.elements.min_hashrate_ths.value),
    temp_warning_c: nullableNumber(form.elements.temp_warning_c.value),
    temp_critical_c: nullableNumber(form.elements.temp_critical_c.value),
  };
  const submit = $('button[type="submit"]', form);
  submit.disabled = true;
  try {
    const result = await request(id ? `/api/miners/${encodeURIComponent(id)}` : "/api/miners", {method: id ? "PUT" : "POST", body: payload});
    $("#miner-dialog").close();
    await Promise.all([loadManagement(), refresh()]);
    toast(id ? "Miner updated." : "Miner added to your fleet.", "success");
    if (!id && result.miner?.id) navigate(`/miners/${encodeURIComponent(result.miner.id)}`);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

$("#pool-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value,
    type: form.elements.type.value,
    mode: form.elements.mode.value,
    enabled: form.elements.enabled.checked,
    log_path: form.elements.log_path.value,
    api_url: form.elements.api_url.value,
    bitcoin_address: form.elements.bitcoin_address.value,
  };
  const submit = $('button[type="submit"]', form);
  submit.disabled = true;
  try {
    await request(id ? `/api/pools/${encodeURIComponent(id)}` : "/api/pools", {method: id ? "PUT" : "POST", body: payload});
    $("#pool-dialog").close();
    toast(id ? "Pool monitor updated." : "Pool monitor added.", "success");
    loadManagement().catch(error => toast(`Saved, but refresh failed: ${error.message}`, "warning"));
    refresh().catch(() => {});
  } catch (error) {
    toast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    poll_interval_seconds: Number(form.elements.poll_interval_seconds.value),
    dashboard_port: Number(form.elements.dashboard_port.value),
    alert_cooldown_seconds: Number(form.elements.alert_cooldown_seconds.value),
    request_timeout_seconds: Number(form.elements.request_timeout_seconds.value),
    dashboard_density: form.elements.dashboard_density.value,
    dashboard_base_url: form.elements.dashboard_base_url.value || null,
    lan_access_enabled: form.elements.lan_access_enabled.checked,
    discord_enabled: form.elements.discord_enabled.checked,
    webhook_url: form.elements.webhook_url.value || null,
    clear_webhook: form.elements.clear_webhook.checked,
    send_offline_alerts: form.elements.send_offline_alerts.checked,
    send_recovery_alerts: form.elements.send_recovery_alerts.checked,
    send_hashrate_alerts: form.elements.send_hashrate_alerts.checked,
    send_temperature_alerts: form.elements.send_temperature_alerts.checked,
    send_best_diff_alerts: form.elements.send_best_diff_alerts.checked,
    send_block_found_alerts: form.elements.send_block_found_alerts.checked,
    send_pool_alerts: form.elements.send_pool_alerts.checked,
    send_pool_switch_alerts: form.elements.send_pool_switch_alerts.checked,
    send_share_alerts: form.elements.send_share_alerts.checked,
    verbose_pool_events: form.elements.verbose_pool_events.checked,
    btc_enabled: form.elements.btc_enabled.checked,
    bch_enabled: form.elements.bch_enabled.checked,
    auto_network_data: form.elements.auto_network_data.checked,
    manual_btc_network_hashrate_eh: nullableNumber(form.elements.manual_btc_network_hashrate_eh.value),
    manual_bch_network_hashrate_eh: nullableNumber(form.elements.manual_bch_network_hashrate_eh.value),
  };
  const submit = $('button[type="submit"]', form);
  submit.disabled = true;
  try {
    const result = await request("/api/settings", {method: "PUT", body: payload});
    await Promise.all([loadSettings(), refresh()]);
    form.elements.webhook_url.value = "";
    form.elements.clear_webhook.checked = false;
    toast(result.restart_required ? "Settings saved. Restart PoCiSys to apply the network change." : "Settings saved.", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

$("#pool-form").elements.mode.addEventListener("change", syncPoolFields);

document.addEventListener("pointerdown", event => {
  pointerStart = {x: event.clientX, y: event.clientY};
  suppressNextClick = false;
}, true);

document.addEventListener("pointermove", event => {
  if (!pointerStart) return;
  if (Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 6) {
    suppressNextClick = true;
  }
}, true);

document.addEventListener("pointerup", () => {
  pointerStart = null;
  setTimeout(() => {
    suppressNextClick = false;
  }, 0);
}, true);

window.addEventListener("popstate", route);
route();
Promise.all([refresh(), loadManagement(), loadSettings()]).catch(error => toast(error.message, "error"));
setInterval(refresh, 5000);
