(() => {
  "use strict";

  const page = document.body.dataset.page;
  const fmt = new Intl.NumberFormat();
  const colors = ["#6ee7c7", "#61a9ff", "#a78bfa", "#f5c66b", "#ff7a8a", "#5eead4", "#fb923c"];
  const charts = {};
  const serverToday = document.body.dataset.serverToday || localDate(new Date());
  const initialState = stateFromUrl();
  let range = initialState.range;
  let pagesOffset = 0;

  function localDate(date) {
    const copy = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return copy.toISOString().slice(0, 10);
  }

  function defaultRange(days) {
    const end = new Date(`${serverToday}T12:00:00Z`);
    const start = new Date(end);
    start.setUTCDate(start.getUTCDate() - days + 1);
    return { from: start.toISOString().slice(0, 10), to: serverToday };
  }

  function stateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const fallback = defaultRange(7);
    const from = params.get("from") || fallback.from;
    const to = params.get("to") || fallback.to;
    const valid = /^\d{4}-\d{2}-\d{2}$/.test(from) && /^\d{4}-\d{2}-\d{2}$/.test(to) && from <= to;
    return {
      range: valid ? { from, to } : fallback,
      bots: params.get("bots") === "1",
      assets: params.get("assets") === "1",
    };
  }

  function filters() {
    return {
      bots: document.querySelector("#include-bots")?.checked ? "1" : "0",
      assets: document.querySelector("#include-assets")?.checked ? "1" : "0",
    };
  }

  function query(extra = {}) {
    return new URLSearchParams({ ...range, ...filters(), ...extra }).toString();
  }

  function syncUrl(push = true) {
    const url = `${window.location.pathname}?${query()}`;
    window.history[push ? "pushState" : "replaceState"]({}, "", url);
  }

  async function api(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("Authentication required");
    }
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `Request failed with ${response.status}`);
    return body;
  }

  function showError(error) {
    const box = document.querySelector("#page-error");
    if (!box) return;
    box.textContent = error.message || String(error);
    box.classList.remove("hidden");
  }

  function clearError() {
    document.querySelector("#page-error")?.classList.add("hidden");
  }

  function setupFilters(reload) {
    const from = document.querySelector("#date-from");
    const to = document.querySelector("#date-to");
    if (!from || !to) return;
    const bots = document.querySelector("#include-bots");
    const assets = document.querySelector("#include-assets");

    function renderState(state) {
      range = state.range;
      from.value = range.from;
      to.value = range.to;
      if (bots) bots.checked = state.bots;
      if (assets) assets.checked = state.assets;
      document.querySelectorAll("[data-days]").forEach(button => {
        const preset = defaultRange(Number(button.dataset.days));
        button.classList.toggle("active", preset.from === range.from && preset.to === range.to);
      });
    }

    renderState(initialState);
    syncUrl(false);
    document.querySelectorAll("[data-days]").forEach(button => {
      button.addEventListener("click", () => {
        range = defaultRange(Number(button.dataset.days));
        from.value = range.from;
        to.value = range.to;
        document.querySelectorAll("[data-days]").forEach(item => item.classList.toggle("active", item === button));
        pagesOffset = 0;
        syncUrl();
        reload();
      });
    });
    document.querySelector("#apply-dates")?.addEventListener("click", () => {
      if (!from.value || !to.value || from.value > to.value) return showError(new Error("Choose a valid date range."));
      range = { from: from.value, to: to.value };
      document.querySelectorAll("[data-days]").forEach(item => item.classList.remove("active"));
      pagesOffset = 0;
      syncUrl();
      reload();
    });
    document.querySelectorAll("#include-bots, #include-assets").forEach(input => input.addEventListener("change", () => {
      pagesOffset = 0;
      syncUrl();
      reload();
    }));
    window.addEventListener("popstate", () => {
      renderState(stateFromUrl());
      pagesOffset = 0;
      reload();
    });
  }

  function dateLabel(from, to) {
    const options = { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" };
    const left = new Date(`${from}T12:00:00Z`).toLocaleDateString(undefined, options);
    const right = new Date(`${to}T12:00:00Z`).toLocaleDateString(undefined, options);
    return from === to ? left : `${left} to ${right}`;
  }

  function compactBytes(value) {
    if (!value) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const level = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    return `${(value / (1024 ** level)).toFixed(level ? 1 : 0)} ${units[level]}`;
  }

  function metric(label, value) {
    return `<article class="metric-card"><span class="label">${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></article>`;
  }

  function escapeHtml(value) {
    const node = document.createElement("span");
    node.textContent = value ?? "";
    return node.innerHTML;
  }

  function updateChart(name, canvas, config) {
    charts[name]?.destroy();
    if (typeof Chart === "undefined") return;
    charts[name] = new Chart(canvas, config);
  }

  const chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { color: "#8fa2ba", usePointStyle: true, pointStyle: "circle" } } },
    scales: {
      x: { ticks: { color: "#71859d", maxRotation: 0 }, grid: { color: "rgba(143,162,186,.08)" } },
      y: { beginAtZero: true, ticks: { color: "#71859d", precision: 0 }, grid: { color: "rgba(143,162,186,.08)" } },
    },
  };

  async function loadOverview() {
    clearError();
    try {
      const data = await api(`/api/overview?${query()}`);
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      document.querySelector("#site-cards").innerHTML = data.sites.map(site => {
        const change = site.change_percent;
        const changeText = change === null ? "No prior data" : `${change > 0 ? "+" : ""}${change}%`;
        const changeClass = change > 0 ? "change-up" : change < 0 ? "change-down" : "";
        return `<a class="site-card" href="/site/${encodeURIComponent(site.name)}?${escapeHtml(query())}"><span class="site-name">${escapeHtml(site.name)}</span><strong class="site-value">${fmt.format(site.requests)}</strong><span class="site-meta"><span>${fmt.format(site.unique_visitors)} visitor-days</span><span class="${changeClass}">${changeText}</span></span></a>`;
      }).join("");
      const days = [...new Set(data.timeseries.map(row => row.day))];
      const sites = data.sites.map(site => site.name);
      const seriesValues = new Map(
        data.timeseries.map(row => [`${row.day}\0${row.site}`, row.requests])
      );
      updateChart("overview", document.querySelector("#overview-chart"), {
        type: "line",
        data: { labels: days, datasets: sites.map((site, index) => ({ label: site, data: days.map(day => seriesValues.get(`${day}\0${site}`) || 0), borderColor: colors[index % colors.length], backgroundColor: `${colors[index % colors.length]}22`, tension: .3, pointRadius: 2, fill: false })) },
        options: chartOptions,
      });
      document.querySelector("#server-metrics").innerHTML = [
        metric("Total requests", fmt.format(data.totals.requests)),
        metric("Visitor-days", fmt.format(data.totals.unique_visitors)),
        metric("Bandwidth", compactBytes(data.totals.bytes)),
        metric("Client error rate (4xx)", `${data.totals.client_error_rate}%`),
        metric("Server error rate (5xx)", `${data.totals.server_error_rate}%`),
        metric("Bot share", `${data.totals.bot_share}%`),
      ].join("");
    } catch (error) { showError(error); }
  }

  function fillTable(selector, rows, columns, empty = "No data for this range") {
    const body = document.querySelector(selector);
    if (!body) return;
    body.innerHTML = rows.length ? rows.map(row => `<tr>${columns.map(column => `<td>${column.format ? column.format(row[column.key], row) : escapeHtml(String(row[column.key] ?? ""))}</td>`).join("")}</tr>`).join("") : `<tr><td class="empty" colspan="${columns.length}">${escapeHtml(empty)}</td></tr>`;
  }

  async function loadSite() {
    clearError();
    const site = document.body.dataset.site;
    try {
      const suffix = query();
      const interval = range.from === range.to ? "hour" : "day";
      const overviewLink = document.querySelector("#overview-link");
      if (overviewLink) overviewLink.href = `/?${query()}`;
      const [series, pages, referrers, statuses, agents, countries] = await Promise.all([
        api(`/api/site/${encodeURIComponent(site)}/timeseries?${query({ interval })}`),
        api(`/api/site/${encodeURIComponent(site)}/pages?${suffix}&limit=25&offset=${pagesOffset}`),
        api(`/api/site/${encodeURIComponent(site)}/referrers?${suffix}`),
        api(`/api/site/${encodeURIComponent(site)}/status?${suffix}`),
        api(`/api/site/${encodeURIComponent(site)}/agents?${suffix}`),
        api(`/api/site/${encodeURIComponent(site)}/countries?${suffix}`),
      ]);
      updateChart("site", document.querySelector("#site-chart"), {
        type: "line",
        data: { labels: series.series.map(row => interval === "hour" ? row.bucket.slice(11, 16) : row.bucket), datasets: [
          { label: "Requests", data: series.series.map(row => row.requests), borderColor: colors[0], backgroundColor: `${colors[0]}22`, tension: .3, fill: true },
          { label: interval === "hour" ? "Hourly visitors" : "Daily visitors", data: series.series.map(row => row.unique_visitors), borderColor: colors[1], backgroundColor: "transparent", tension: .3 },
        ] }, options: chartOptions,
      });
      const heading = document.querySelector("#traffic-heading");
      if (heading) heading.textContent = interval === "hour" ? "Requests and hourly visitors" : "Requests and daily visitors";
      fillTable("#pages-table", pages.pages, [{ key: "path" }, { key: "requests", format: fmt.format }, { key: "unique_visitors", format: fmt.format }]);
      document.querySelector("#pages-prev").disabled = pagesOffset === 0;
      document.querySelector("#pages-next").disabled = pages.pages.length < 25;
      fillTable("#referrers-table", referrers.referrers, [{ key: "group" }, { key: "requests", format: fmt.format }]);
      fillTable("#missing-table", statuses.top_404, [{ key: "path" }, { key: "requests", format: fmt.format }]);
      fillTable("#browsers-table", agents.browsers.filter(row => !row.is_bot), [{ key: "ua_family" }, { key: "requests", format: fmt.format }]);
      fillTable("#os-table", agents.operating_systems, [{ key: "os_family" }, { key: "requests", format: fmt.format }]);
      fillTable("#countries-table", countries.countries, [{ key: "country" }, { key: "requests", format: fmt.format }], "GeoIP is disabled or no country data is available");
      fillTable("#bots-table", agents.bots, [{ key: "ua_family" }, { key: "requests", format: fmt.format }]);
      updateChart("status", document.querySelector("#status-chart"), {
        type: "doughnut",
        data: { labels: statuses.statuses.map(row => String(row.status)), datasets: [{ data: statuses.statuses.map(row => row.requests), backgroundColor: statuses.statuses.map((row, index) => colors[index % colors.length]), borderColor: "#142135", borderWidth: 3 }] },
        options: { responsive: true, maintainAspectRatio: false, cutout: "68%", plugins: chartOptions.plugins },
      });
    } catch (error) { showError(error); }
  }

  async function loadLive() {
    clearError();
    try {
      const data = await api(`/api/live?${new URLSearchParams(filters())}&minutes=60`);
      document.querySelector("#live-updated").textContent = `Updated ${new Date(data.generated_at * 1000).toLocaleTimeString()}`;
      document.querySelector("#live-cards").innerHTML = data.sites.map(site => `<a class="site-card" href="/site/${encodeURIComponent(site.site)}"><span class="site-name">${escapeHtml(site.site)}</span><strong class="site-value">${fmt.format(site.requests)}</strong><span class="site-meta"><span>${fmt.format(site.unique_visitors)} visitors</span><span>${compactBytes(site.bytes)}</span></span></a>`).join("");
    } catch (error) { showError(error); }
  }

  async function loadHealth() {
    clearError();
    try {
      const response = await fetch("/api/health", { headers: { Accept: "application/json" } });
      const data = await response.json();
      const badge = document.querySelector("#health-badge");
      badge.textContent = data.status === "ok" ? "Healthy" : "Needs attention";
      badge.className = `status-badge ${data.status}`;
      document.querySelector("#health-metrics").innerHTML = [metric("Configured sites", fmt.format(data.sites)), metric("Raw requests", fmt.format(data.raw_requests)), metric("Database size", compactBytes(data.database_bytes)), metric("Tracked logs", fmt.format(data.logs.length))].join("");
      const run = data.last_ingest;
      fillTable("#health-run", run ? [run] : [], [
        { key: "started_at", format: unixTime }, { key: "finished_at", format: unixTime },
        { key: "lines_seen", format: fmt.format }, { key: "parsed", format: fmt.format },
        { key: "parse_failures", format: fmt.format }, { key: "inserted", format: fmt.format },
      ], "No ingest run recorded");
      fillTable("#health-logs", data.logs, [{ key: "id" }, { key: "offset", format: fmt.format }, { key: "last_run", format: unixTime }]);
    } catch (error) { showError(error); }
  }

  function unixTime(value) {
    return value ? escapeHtml(new Date(value * 1000).toLocaleString()) : "Never";
  }

  if (page === "overview") { setupFilters(loadOverview); loadOverview(); }
  if (page === "site") {
    setupFilters(loadSite);
    document.querySelector("#pages-prev")?.addEventListener("click", () => { pagesOffset = Math.max(0, pagesOffset - 25); loadSite(); });
    document.querySelector("#pages-next")?.addEventListener("click", () => { pagesOffset += 25; loadSite(); });
    loadSite();
  }
  if (page === "live") {
    document.querySelectorAll("#include-bots, #include-assets").forEach(input => input.addEventListener("change", loadLive));
    loadLive();
    window.setInterval(loadLive, 60000);
  }
  if (page === "health") loadHealth();
})();
