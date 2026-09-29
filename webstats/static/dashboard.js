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
  let almanacState = almanacStateFromUrl();

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

  function validDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const year = Number(value.slice(0, 4));
    const parsed = new Date(`${value}T12:00:00Z`);
    return year > 0 && !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
  }

  function stateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const fallback = defaultRange(7);
    const from = params.get("from") || fallback.from;
    const to = params.get("to") || fallback.to;
    const valid = validDate(from) && validDate(to) && from <= to;
    return {
      range: valid ? { from, to } : fallback,
      bots: params.get("bots") === "1",
      assets: params.get("assets") === "1",
    };
  }

  function almanacStateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const fallbackYear = Number(serverToday.slice(0, 4));
    const parsedYear = Number(params.get("year"));
    return {
      year: Number.isInteger(parsedYear) && parsedYear >= 2000 && parsedYear <= fallbackYear ? parsedYear : fallbackYear,
      site: params.get("site") || "",
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

  function persistentParams() {
    return page === "page" ? { path: document.body.dataset.path } : {};
  }

  function query(extra = {}) {
    return new URLSearchParams({ ...range, ...filters(), ...persistentParams(), ...extra }).toString();
  }

  function siteQuery() {
    return new URLSearchParams({ ...range, ...filters() }).toString();
  }

  function almanacQuery() {
    return new URLSearchParams({
      year: String(almanacState.year),
      site: almanacState.site,
      ...filters(),
    }).toString();
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
      if (!validDate(from.value) || !validDate(to.value) || from.value > to.value) return showError(new Error("Choose a valid date range."));
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

  function renderEvents(events) {
    const journal = document.querySelector("#event-journal");
    if (!journal) return;
    if (!events.length) {
      journal.innerHTML = '<li class="empty">No first sightings in this range yet.</li>';
      return;
    }
    journal.innerHTML = events.map(event => {
      const isReferrer = event.kind === "new_referrer";
      const subject = isReferrer ? event.source : event.agent;
      const label = isReferrer ? "New referrer" : "First AI crawler sighting";
      const detail = isReferrer
        ? `${subject} led someone to ${event.path}`
        : `${subject} visited ${event.path}`;
      const country = event.country ? ` · ${event.country}` : "";
      return `<li class="event-item"><span class="event-marker ${isReferrer ? "referrer" : "crawler"}"></span><div><strong>${escapeHtml(label)}</strong><p>${escapeHtml(detail)}</p><small>${escapeHtml(event.site)} · ${escapeHtml(new Date(event.occurred_at * 1000).toLocaleString())}${escapeHtml(country)}</small></div></li>`;
    }).join("");
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
      const [data, journal] = await Promise.all([
        api(`/api/overview?${query()}`),
        api(`/api/events?${query({ limit: 20 })}`),
      ]);
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
      renderEvents(journal.events);
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
      const actualInterval = series.interval;
      updateChart("site", document.querySelector("#site-chart"), {
        type: "line",
        data: { labels: series.series.map(row => actualInterval === "hour" ? `${row.bucket.slice(11, 16)} ${row.bucket.slice(-5)}` : row.bucket), datasets: [
          { label: "Requests", data: series.series.map(row => row.requests), borderColor: colors[0], backgroundColor: `${colors[0]}22`, tension: .3, fill: true },
          { label: actualInterval === "hour" ? "Hourly visitors" : "Daily visitors", data: series.series.map(row => row.unique_visitors), borderColor: colors[1], backgroundColor: "transparent", tension: .3 },
        ] }, options: chartOptions,
      });
      const heading = document.querySelector("#traffic-heading");
      if (heading) heading.textContent = actualInterval === "hour" ? "Requests and hourly visitors" : "Requests and daily visitors";
      fillTable("#pages-table", pages.pages, [
        {
          key: "path",
          format: value => {
            const params = new URLSearchParams({ ...range, ...filters(), path: value });
            const href = `/site/${encodeURIComponent(site)}/page?${params}`;
            return `<a class="table-link" href="${escapeHtml(href)}">${escapeHtml(value)}</a>`;
          },
        },
        { key: "requests", format: fmt.format },
        { key: "unique_visitors", format: fmt.format },
      ]);
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

  async function loadPage() {
    clearError();
    const site = document.body.dataset.site;
    try {
      const siteLink = document.querySelector("#site-link");
      if (siteLink) siteLink.href = `/site/${encodeURIComponent(site)}?${siteQuery()}`;
      const data = await api(`/api/site/${encodeURIComponent(site)}/page?${query()}`);
      document.querySelector("#page-metrics").innerHTML = [
        metric("Requests", fmt.format(data.totals.requests)),
        metric("Visitor-days", fmt.format(data.totals.unique_visitors)),
        metric("First seen", data.totals.first_seen || "Never"),
        metric("Last seen", data.totals.last_seen || "Never"),
      ].join("");
      updateChart("page", document.querySelector("#page-chart"), {
        type: "line",
        data: {
          labels: data.series.map(row => row.bucket),
          datasets: [
            { label: "Requests", data: data.series.map(row => row.requests), borderColor: colors[0], backgroundColor: `${colors[0]}22`, tension: .3, fill: true },
            { label: "Visitor-days", data: data.series.map(row => row.unique_visitors), borderColor: colors[1], backgroundColor: "transparent", tension: .3 },
          ],
        },
        options: chartOptions,
      });
      fillTable("#page-referrers", data.referrers, [
        { key: "group" },
        { key: "requests", format: fmt.format },
      ], "No referrers to this page in this range.");
      fillTable("#page-ai-agents", data.ai_agents, [
        { key: "agent", format: (value, row) => `<strong>${escapeHtml(value)}</strong><span class="table-subtitle">${escapeHtml(row.provider)}</span>` },
        { key: "purpose" },
        { key: "requests", format: fmt.format },
      ], "No recognized AI readers visited this page in this range.");
      fillTable("#page-countries", data.countries, [
        { key: "country" },
        { key: "requests", format: fmt.format },
      ], "GeoIP is disabled or no country data is available.");
      fillTable("#page-statuses", data.statuses, [
        { key: "status" },
        { key: "requests", format: fmt.format },
      ], "No responses for this page in this range.");
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

  async function loadAiCrawlers() {
    clearError();
    try {
      const data = await api(`/api/ai-crawlers?${query({ limit: 250 })}`);
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      document.querySelector("#ai-metrics").innerHTML = [
        metric("Page requests", fmt.format(data.totals.requests)),
        metric("Agents observed", fmt.format(data.totals.agents)),
        metric("Pages explored", fmt.format(data.totals.pages)),
        metric("Sites visited", fmt.format(data.totals.sites)),
      ].join("");
      fillTable("#ai-sightings", data.sightings, [
        { key: "agent", format: (value, row) => `<strong>${escapeHtml(value)}</strong><span class="table-subtitle">${escapeHtml(row.provider)}</span>` },
        { key: "purpose" },
        { key: "site" },
        { key: "path" },
        { key: "first_seen" },
        { key: "last_seen" },
        { key: "requests", format: fmt.format },
      ], "No recognized AI crawlers visited pages in this range.");
    } catch (error) { showError(error); }
  }

  async function loadFeedReaders() {
    clearError();
    try {
      const data = await api(`/api/feed-readers?${query({ limit: 500 })}`);
      const hasReports = data.totals.reporting_feeds > 0;
      const change = data.totals.subscriber_change;
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      document.querySelector("#feed-metrics").innerHTML = [
        metric("Latest reported subscriptions", hasReports ? fmt.format(data.totals.reported_subscribers) : "Not reported"),
        metric("Change in range", hasReports ? `${change > 0 ? "+" : ""}${fmt.format(change)}` : "Not reported"),
        metric("Readers observed", fmt.format(data.totals.readers)),
        metric("Feeds observed", fmt.format(data.totals.feeds)),
      ].join("");
      updateChart("feeds", document.querySelector("#feed-chart"), {
        type: "line",
        data: {
          labels: data.series.map(row => row.bucket),
          datasets: [{
            label: "Reported subscriptions",
            data: data.series.map(row => row.reported_subscribers),
            borderColor: colors[0],
            backgroundColor: `${colors[0]}22`,
            tension: .3,
            spanGaps: false,
            fill: true,
          }],
        },
        options: chartOptions,
      });
      fillTable("#feed-sightings", data.sightings, [
        { key: "reader" },
        { key: "site" },
        { key: "path" },
        { key: "latest_subscribers", format: value => value === null ? "—" : fmt.format(value) },
        { key: "peak_subscribers", format: value => value === null ? "—" : fmt.format(value) },
        { key: "first_seen", format: unixTime },
        { key: "last_seen", format: unixTime },
        { key: "requests", format: fmt.format },
      ], "No recognized feed readers visited in this range.");
    } catch (error) { showError(error); }
  }

  function renderCalendar(data) {
    const grid = document.querySelector("#calendar-grid");
    const months = document.querySelector("#calendar-months");
    const first = new Date(`${data.year}-01-01T12:00:00Z`);
    const offset = (first.getUTCDay() + 6) % 7;
    const maximum = Math.max(0, ...data.days.filter(row => !row.future).map(row => row.visitor_days));
    const recordDays = new Set(data.record_breakers.map(row => row.day));
    const blanks = Array.from({ length: offset }, () => '<span class="calendar-blank" aria-hidden="true"></span>');
    const cells = data.days.map(row => {
      const level = row.visitor_days === 0 || maximum === 0 ? 0 : Math.max(1, Math.ceil(4 * row.visitor_days / maximum));
      const description = `${row.day}: ${fmt.format(row.visitor_days)} visitor-days, ${fmt.format(row.requests)} requests`;
      const classes = ["calendar-day", `level-${level}`, row.future ? "future" : "", recordDays.has(row.day) ? "record-day" : ""].filter(Boolean).join(" ");
      return `<button type="button" class="${classes}" role="gridcell" aria-label="${escapeHtml(description)}" title="${escapeHtml(description)}"></button>`;
    });
    grid.innerHTML = [...blanks, ...cells].join("");
    const monthLabels = [];
    for (let month = 0; month < 12; month += 1) {
      const date = new Date(Date.UTC(data.year, month, 1, 12));
      const dayIndex = Math.round((date - first) / 86400000);
      const column = Math.floor((offset + dayIndex) / 7) + 1;
      const label = date.toLocaleDateString(undefined, { month: "short", timeZone: "UTC" });
      monthLabels.push(`<span style="grid-column:${column}">${escapeHtml(label)}</span>`);
    }
    months.innerHTML = monthLabels.join("");
  }

  function recordCard(label, value, detail) {
    return `<article class="record-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail || "")}</small></article>`;
  }

  async function loadAlmanac() {
    clearError();
    try {
      const data = await api(`/api/almanac?${almanacQuery()}`);
      almanacState.year = data.year;
      almanacState.site = data.scope.site || "";
      document.querySelector("#almanac-scope").textContent = data.scope.label;
      document.querySelector("#calendar-heading").textContent = `${data.year} daily visitor-days`;

      const siteSelect = document.querySelector("#almanac-site");
      siteSelect.innerHTML = ["", ...data.sites].map(name => `<option value="${escapeHtml(name)}"${name === almanacState.site ? " selected" : ""}>${escapeHtml(name || "All sites")}</option>`).join("");
      const years = [...new Set([data.year, ...data.available_years])].sort((left, right) => right - left);
      const yearSelect = document.querySelector("#almanac-year");
      yearSelect.innerHTML = years.map(year => `<option value="${year}"${year === data.year ? " selected" : ""}>${year}</option>`).join("");

      document.querySelector("#almanac-metrics").innerHTML = [
        metric("All-time requests", fmt.format(data.totals.requests)),
        metric("All-time visitor-days", fmt.format(data.totals.visitor_days)),
        metric("Active days", fmt.format(data.totals.active_days)),
        metric("Current streak", `${fmt.format(data.records.current)} days`),
      ].join("");
      renderCalendar(data);

      const busiest = data.records.busiest_day;
      const week = data.records.best_week;
      const month = data.records.best_month;
      document.querySelector("#record-cards").innerHTML = [
        recordCard("Busiest day", busiest ? `${fmt.format(busiest.requests)} requests` : "No traffic yet", busiest?.day),
        recordCard("Best week", week ? `${fmt.format(week.requests)} requests` : "No traffic yet", week ? `Week of ${week.period}` : ""),
        recordCard("Best month", month ? `${fmt.format(month.requests)} requests` : "No traffic yet", month?.period),
        recordCard("Longest streak", `${fmt.format(data.records.longest)} days`, data.records.longest_start ? `${data.records.longest_start} to ${data.records.longest_end}` : ""),
        recordCard("First seen", data.totals.first_seen || "Never", "First active day"),
        recordCard("Last seen", data.totals.last_seen || "Never", "Most recent active day"),
      ].join("");

      const next = data.milestones.next;
      document.querySelector("#next-milestone").innerHTML = `<strong>${fmt.format(data.totals.visitor_days)} of ${fmt.format(next)} visitor-days</strong><div class="milestone-progress" aria-label="${escapeHtml(String(data.milestones.progress))}% toward next milestone"><span style="width:${Math.min(100, data.milestones.progress)}%"></span></div><small class="muted">Next milestone · ${data.milestones.progress}% complete</small>`;
      const reached = [...data.milestones.reached].reverse();
      document.querySelector("#milestone-list").innerHTML = reached.length
        ? reached.slice(0, 8).map(item => `<li><strong>${fmt.format(item.value)} visitor-days</strong><time datetime="${escapeHtml(item.day)}">${escapeHtml(item.day)}</time></li>`).join("")
        : '<li class="empty">The first milestone is still ahead.</li>';

      fillTable("#record-breakers", data.record_breakers.slice(0, 20), [
        { key: "day" },
        { key: "requests", format: fmt.format },
        { key: "visitor_days", format: fmt.format },
      ], "No record days yet.");
      fillTable("#on-this-day", data.on_this_day, [
        { key: "day", format: value => escapeHtml(value.slice(0, 4)) },
        { key: "requests", format: fmt.format },
        { key: "visitor_days", format: fmt.format },
      ], "No traffic recorded on this date in prior years.");
    } catch (error) { showError(error); }
  }

  function setupAlmanac() {
    const bots = document.querySelector("#include-bots");
    const assets = document.querySelector("#include-assets");
    bots.checked = almanacState.bots;
    assets.checked = almanacState.assets;
    const apply = (push = true) => {
      almanacState = {
        ...almanacState,
        year: Number(document.querySelector("#almanac-year").value || almanacState.year),
        site: document.querySelector("#almanac-site").value,
        bots: bots.checked,
        assets: assets.checked,
      };
      window.history[push ? "pushState" : "replaceState"]({}, "", `${window.location.pathname}?${almanacQuery()}`);
      loadAlmanac();
    };
    document.querySelector("#apply-almanac").addEventListener("click", () => apply());
    [bots, assets].forEach(input => input.addEventListener("change", () => apply()));
    window.addEventListener("popstate", () => {
      almanacState = almanacStateFromUrl();
      bots.checked = almanacState.bots;
      assets.checked = almanacState.assets;
      loadAlmanac();
    });
    window.history.replaceState({}, "", `${window.location.pathname}?${almanacQuery()}`);
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
  if (page === "page") { setupFilters(loadPage); loadPage(); }
  if (page === "live") {
    document.querySelectorAll("#include-bots, #include-assets").forEach(input => input.addEventListener("change", loadLive));
    loadLive();
    window.setInterval(loadLive, 60000);
  }
  if (page === "ai-crawlers") { setupFilters(loadAiCrawlers); loadAiCrawlers(); }
  if (page === "feed-readers") { setupFilters(loadFeedReaders); loadFeedReaders(); }
  if (page === "almanac") { setupAlmanac(); loadAlmanac(); }
  if (page === "health") loadHealth();
})();
