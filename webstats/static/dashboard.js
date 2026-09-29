(() => {
  "use strict";

  const page = document.body.dataset.page;
  const fmt = new Intl.NumberFormat();
  const colors = ["#6ee7c7", "#61a9ff", "#a78bfa", "#f5c66b", "#ff7a8a", "#5eead4", "#fb923c"];
  const charts = {};
  const serverToday = document.body.dataset.serverToday || localDate(new Date());
  const defaultDays = Number(document.body.dataset.defaultDays) || 7;
  const initialState = stateFromUrl();
  let range = initialState.range;
  let pagesOffset = 0;
  let almanacState = almanacStateFromUrl();
  let briefingWeek = new URLSearchParams(window.location.search).get("week") || "";
  let scopeSite = initialState.site;
  let linkSource = initialState.source;
  let inboxCategory = initialState.category;
  let chronicleKind = initialState.kind;
  let chronicleQuery = initialState.q;
  let inboxSeenAt = readInboxSeenAt();
  let galaxyData = null;
  let errorsDays = [7, 30, 90].includes(Number(new URLSearchParams(window.location.search).get("days")))
    ? Number(new URLSearchParams(window.location.search).get("days")) : 30;

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
    const fallback = defaultRange(defaultDays);
    const from = params.get("from") || fallback.from;
    const to = params.get("to") || fallback.to;
    const valid = validDate(from) && validDate(to) && from <= to;
    return {
      range: valid ? { from, to } : fallback,
      bots: params.get("bots") === "1",
      assets: params.get("assets") === "1",
      site: params.get("site") || "",
      source: params.get("source") || "",
      category: params.get("category") || "",
      kind: params.get("kind") || "",
      q: params.get("q") || "",
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
    if (page === "page") return { path: document.body.dataset.path };
    if (page === "link-atlas" && linkSource) return { source: linkSource };
    if (page === "inbox" && inboxCategory) return { category: inboxCategory };
    if (page === "chronicle") return {
      ...(chronicleKind ? { kind: chronicleKind } : {}),
      ...(chronicleQuery ? { q: chronicleQuery } : {}),
    };
    return {};
  }

  function query(extra = {}) {
    const scope = document.querySelector("#site-filter") && scopeSite ? { site: scopeSite } : {};
    return new URLSearchParams({ ...range, ...filters(), ...persistentParams(), ...scope, ...extra }).toString();
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

  async function setupSiteFilter(reload) {
    const select = document.querySelector("#site-filter");
    if (!select) return;
    const data = await api("/api/sites");
    select.innerHTML = ["", ...data.sites.map(item => item.name)]
      .map(name => `<option value="${escapeHtml(name)}"${name === scopeSite ? " selected" : ""}>${escapeHtml(name || "All sites")}</option>`)
      .join("");
    select.addEventListener("change", () => {
      scopeSite = select.value;
      reload();
    });
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
      scopeSite = state.site;
      if (page === "link-atlas") linkSource = state.source;
      if (page === "inbox") inboxCategory = state.category;
      if (page === "chronicle") {
        chronicleKind = state.kind;
        chronicleQuery = state.q;
      }
      from.value = range.from;
      to.value = range.to;
      if (bots) bots.checked = state.bots;
      if (assets) assets.checked = state.assets;
      const siteSelect = document.querySelector("#site-filter");
      if (siteSelect) siteSelect.value = scopeSite;
      const chronicleKindSelect = document.querySelector("#chronicle-kind");
      if (chronicleKindSelect) chronicleKindSelect.value = chronicleKind;
      const chronicleQueryInput = document.querySelector("#chronicle-query");
      if (chronicleQueryInput) chronicleQueryInput.value = chronicleQuery;
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

  function signed(value) {
    return `${value > 0 ? "+" : ""}${fmt.format(value)}`;
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

  const momentMarkerPlugin = {
    id: "momentMarkers",
    afterDatasetsDraw(chart, _args, options) {
      const moments = options?.items || [];
      if (!moments.length) return;
      const labels = chart.data.labels || [];
      const days = [...new Set(moments.map(moment => moment.day))];
      const { ctx, chartArea, scales } = chart;
      ctx.save();
      ctx.strokeStyle = "rgba(245, 198, 107, .72)";
      ctx.fillStyle = "#f5c66b";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      days.forEach(day => {
        const index = labels.findIndex(label => String(label).startsWith(day));
        if (index < 0) return;
        const x = scales.x.getPixelForValue(index);
        ctx.beginPath();
        ctx.moveTo(x, chartArea.top + 7);
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.arc(x, chartArea.top + 5, 3.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.setLineDash([3, 4]);
      });
      ctx.restore();
    },
  };

  function optionsWithMoments(moments) {
    return {
      ...chartOptions,
      plugins: {
        ...chartOptions.plugins,
        momentMarkers: { items: moments },
      },
    };
  }

  function eventPresentation(event) {
    const value = fmt.format(event.value || 0);
    const presentations = {
      new_page: ["New content discovered", `${event.path} received its first successful human visit`, "page"],
      new_referrer: ["New referrer", `${event.source} led someone to ${event.path}`, "referrer"],
      first_ai_visit: ["First AI crawler sighting", `${event.agent} visited ${event.path}`, "crawler"],
      traffic_record: ["New traffic record", `${event.site} reached ${value} human page requests`, "record"],
      traffic_spike: ["Unusual attention", `${event.site} received ${value} requests—well above its recent baseline`, "spike"],
      visitor_milestone: ["Visitor-day milestone", `${event.site} passed ${value} visitor-days`, "milestone"],
      feed_subscriber_milestone: ["RSS readership milestone", `${event.source} reported ${value} subscribers to ${event.path}`, "feed"],
      content_resurfaced: ["Content resurfaced", `${event.path} returned after a long quiet spell`, "resurfaced"],
    };
    return presentations[event.kind] || ["Automatic moment", event.path || event.site, "other"];
  }

  function readInboxSeenAt() {
    try {
      const stored = Number(window.localStorage.getItem("webstatsInboxSeenAt"));
      return Number.isFinite(stored) && stored > 0 ? stored : 0;
    } catch (_error) {
      return 0;
    }
  }

  function writeInboxSeenAt(value) {
    inboxSeenAt = value;
    try { window.localStorage.setItem("webstatsInboxSeenAt", String(value)); }
    catch (_error) { /* Browser storage can be disabled without breaking Inbox. */ }
  }

  function updateInboxCount(count, limited = false) {
    const badge = document.querySelector("#inbox-count");
    if (!badge) return;
    badge.classList.toggle("hidden", count === 0);
    badge.textContent = count ? (limited ? `${count}+` : String(count)) : "";
    badge.setAttribute("aria-label", `${count} unread discoveries`);
  }

  async function loadInboxBadge() {
    try {
      const fallback = defaultRange(7).from;
      const from = inboxSeenAt ? localDate(new Date(inboxSeenAt * 1000)) : fallback;
      const data = await api(`/api/inbox?${new URLSearchParams({ from, to: serverToday, limit: "500" })}`);
      const unread = data.events.filter(item => item.occurred_at > inboxSeenAt).length;
      updateInboxCount(unread, data.limited);
    } catch (_error) {
      updateInboxCount(0);
    }
  }

  function renderEvents(events, selector = "#event-journal") {
    const journal = document.querySelector(selector);
    if (!journal) return;
    if (!events.length) {
      journal.innerHTML = '<li class="empty">No automatic moments in this range yet.</li>';
      return;
    }
    journal.innerHTML = events.slice(0, 20).map(event => {
      const [label, detail, category] = eventPresentation(event);
      const country = event.country ? ` · ${event.country}` : "";
      return `<li class="event-item"><span class="event-marker ${escapeHtml(category)}"></span><div><strong>${escapeHtml(label)}</strong><p>${escapeHtml(detail)}</p><small>${escapeHtml(event.site)} · ${escapeHtml(new Date(event.occurred_at * 1000).toLocaleString())}${escapeHtml(country)}</small></div></li>`;
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
      const [data, journal, briefing, pulse] = await Promise.all([
        api(`/api/overview?${query()}`),
        api(`/api/events?${query({ limit: 100 })}`),
        api("/api/briefing"),
        api("/api/pulse?limit=3"),
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
        options: optionsWithMoments(journal.events),
        plugins: [momentMarkerPlugin],
      });
      document.querySelector("#server-metrics").innerHTML = [
        metric("Total requests", fmt.format(data.totals.requests)),
        metric("Visitor-days", fmt.format(data.totals.unique_visitors)),
        metric("Bandwidth", compactBytes(data.totals.bytes)),
        metric("Client error rate (4xx)", `${data.totals.client_error_rate}%`),
        metric("Server error rate (5xx)", `${data.totals.server_error_rate}%`),
        metric("Bot share", `${data.totals.bot_share}%`),
      ].join("");
      document.querySelector("#briefing-preview-heading").textContent = briefing.week.complete
        ? `Week of ${dateLabel(briefing.week.from, briefing.week.to)}`
        : `This week so far · ${dateLabel(briefing.week.from, briefing.week.to)}`;
      document.querySelector("#briefing-preview").innerHTML = briefing.narrative.slice(0, 3)
        .map((line, index) => `<p${index === 0 ? ' class="lead"' : ""}>${escapeHtml(line)}</p>`)
        .join("");
      renderPulsePreview(pulse);
      renderEvents(journal.events);
    } catch (error) { showError(error); }
  }

  function pulseBadge(category) {
    return `<span class="pulse-badge ${escapeHtml(category)}">${escapeHtml(category)}</span>`;
  }

  function pageStoryHref(item, from, to) {
    const params = new URLSearchParams({ from, to, bots: "0", assets: "0", path: item.path });
    return `/site/${encodeURIComponent(item.site)}/page?${params}`;
  }

  function renderPulsePreview(data) {
    const target = document.querySelector("#pulse-preview");
    if (!target) return;
    const signals = data.signals.slice(0, 3);
    target.innerHTML = signals.length
      ? signals.map(item => `<a class="pulse-preview-card" href="${escapeHtml(pageStoryHref(item, data.window.from, data.window.to))}">${pulseBadge(item.category)}<strong>${escapeHtml(item.path)}</strong><span>${escapeHtml(item.site)} · ${escapeHtml(item.explanation)}</span></a>`).join("")
      : '<p class="empty">No unusual content movement in the last seven days.</p>';
  }

  async function loadPulse() {
    clearError();
    try {
      const parameters = new URLSearchParams({ limit: "100" });
      if (scopeSite) parameters.set("site", scopeSite);
      const data = await api(`/api/pulse?${parameters}`);
      document.querySelector("#pulse-range").textContent = dateLabel(data.window.from, data.window.to);
      const notable = data.signals.length;
      document.querySelector("#pulse-heading").textContent = notable
        ? `${fmt.format(notable)} signal${notable === 1 ? "" : "s"} worth noticing`
        : "Everything is moving normally";
      const resurfaced = data.counts.resurfaced;
      const rising = data.counts.rising;
      const debut = data.counts.debut;
      document.querySelector("#pulse-summary").textContent = notable
        ? `${fmt.format(rising)} rising, ${fmt.format(resurfaced)} resurfaced, and ${fmt.format(debut)} newly discovered across ${fmt.format(data.total_pages)} known pages.`
        : `No pages departed meaningfully from their recent pattern across ${fmt.format(data.total_pages)} known pages.`;
      document.querySelector("#pulse-metrics").innerHTML = [
        metric("Rising", fmt.format(data.counts.rising)),
        metric("Resurfaced", fmt.format(data.counts.resurfaced)),
        metric("Debuts", fmt.format(data.counts.debut)),
        metric("Evergreen", fmt.format(data.counts.evergreen)),
        metric("Cooling", fmt.format(data.counts.cooling)),
        metric("Dormant", fmt.format(data.counts.dormant)),
      ].join("");
      const signals = document.querySelector("#pulse-signals");
      signals.innerHTML = data.signals.length ? data.signals.map(item => {
        const context = [
          item.top_referrer ? `via ${item.top_referrer}` : null,
          item.top_country ? countryFlag(item.top_country) + " " + item.top_country : null,
          item.ai_requests ? `${item.ai_requests} AI request${item.ai_requests === 1 ? "" : "s"}` : null,
        ].filter(Boolean);
        const href = pageStoryHref(item, data.window.from, data.window.to);
        return `<article class="pulse-card"><div class="pulse-card-top">${pulseBadge(item.category)}<span class="pulse-change ${item.change > 0 ? "change-up" : item.change < 0 ? "change-down" : ""}">${escapeHtml(signed(item.change))}</span></div><a href="${escapeHtml(href)}">${escapeHtml(item.path)}</a><small>${escapeHtml(item.site)}</small><p>${escapeHtml(item.explanation)}</p><div class="pulse-numbers"><strong>${fmt.format(item.recent_requests)}</strong><span>visits · baseline ${fmt.format(item.weekly_baseline)}</span></div>${context.length ? `<div class="pulse-context">${context.map(value => `<span>${escapeHtml(value)}</span>`).join("")}</div>` : ""}</article>`;
      }).join("") : '<p class="empty">No unusual content movement in this window.</p>';
      fillTable("#pulse-steady", data.steady, [
        { key: "path", format: (value, item) => `<a class="table-link" href="${escapeHtml(pageStoryHref(item, data.window.from, data.window.to))}">${escapeHtml(value)}</a>` },
        { key: "site" },
        { key: "recent_requests", format: fmt.format },
        { key: "weekly_baseline", format: fmt.format },
      ], "No steady pages yet.");
    } catch (error) { showError(error); }
  }

  function errorBadge(category) {
    return `<span class="error-badge ${escapeHtml(category)}">${escapeHtml(category)}</span>`;
  }

  function errorUrl(push = false) {
    const parameters = new URLSearchParams({ days: String(errorsDays) });
    if (scopeSite) parameters.set("site", scopeSite);
    window.history[push ? "pushState" : "replaceState"]({}, "", `${window.location.pathname}?${parameters}`);
  }

  async function loadErrors() {
    clearError();
    try {
      const parameters = new URLSearchParams({ days: String(errorsDays) });
      if (scopeSite) parameters.set("site", scopeSite);
      const data = await api(`/api/errors?${parameters}`);
      document.querySelector("#errors-range").textContent = dateLabel(data.window.from, data.window.to);
      document.querySelector("#errors-days").value = String(data.window.days);
      const summary = data.summary;
      document.querySelector("#errors-heading").textContent = summary.actionable_paths
        ? `${fmt.format(summary.actionable_paths)} path${summary.actionable_paths === 1 ? "" : "s"} worth a look`
        : "No actionable misses detected";
      document.querySelector("#errors-summary").textContent = summary.miss_requests
        ? `${fmt.format(summary.actionable_requests)} of ${fmt.format(summary.miss_requests)} human 404 requests remain after filtering ${fmt.format(summary.probe_requests)} recognized probe requests.`
        : "No human, non-asset 404s appeared in this window.";
      document.querySelector("#errors-metrics").innerHTML = [
        metric("Misses to review", fmt.format(summary.actionable_requests)),
        metric("Regressions", fmt.format(summary.regressions)),
        metric("Possible typos", fmt.format(summary.typo_candidates)),
        metric("Probe noise removed", `${summary.noise_percent}%`),
      ].join("");
      const signals = document.querySelector("#error-signals");
      signals.innerHTML = data.signals.length ? data.signals.map(item => {
        const href = pageStoryHref(item, data.window.from, data.window.to);
        const context = [
          item.top_referrer ? `via ${item.top_referrer}` : null,
          item.top_country ? `${countryFlag(item.top_country)} ${item.top_country}` : null,
          item.suggestion ? `${item.suggestion.similarity}% match` : null,
        ].filter(Boolean);
        return `<article class="error-card"><div class="error-card-top">${errorBadge(item.category)}<strong>${fmt.format(item.requests)} miss${item.requests === 1 ? "" : "es"}</strong></div><a href="${escapeHtml(href)}">${escapeHtml(item.path)}</a><small>${escapeHtml(item.site)} · ${fmt.format(item.active_days)} active day${item.active_days === 1 ? "" : "s"}</small><p>${escapeHtml(item.explanation)}</p>${item.suggestion ? `<a class="error-suggestion" href="${escapeHtml(pageStoryHref({ site: item.site, path: item.suggestion.path }, data.window.from, data.window.to))}">Likely destination: ${escapeHtml(item.suggestion.path)}</a>` : ""}${context.length ? `<div class="pulse-context">${context.map(value => `<span>${escapeHtml(value)}</span>`).join("")}</div>` : ""}</article>`;
      }).join("") : '<p class="empty">Nothing actionable in this window. Recognized scanner paths remain listed below for transparency.</p>';
      fillTable("#error-probes", data.probes, [
        { key: "path" },
        { key: "site" },
        { key: "requests", format: fmt.format },
        { key: "active_days", format: fmt.format },
      ], "No recognized probe traffic in this window.");
    } catch (error) { showError(error); }
  }

  function setupErrors() {
    const days = document.querySelector("#errors-days");
    days.value = String(errorsDays);
    days.addEventListener("change", () => {
      errorsDays = Number(days.value);
      errorUrl(true);
      loadErrors();
    });
    setupSiteFilter(() => { errorUrl(true); loadErrors(); });
    window.addEventListener("popstate", () => {
      const parameters = new URLSearchParams(window.location.search);
      scopeSite = parameters.get("site") || "";
      errorsDays = [7, 30, 90].includes(Number(parameters.get("days")))
        ? Number(parameters.get("days")) : 30;
      const siteSelect = document.querySelector("#site-filter");
      if (siteSelect) siteSelect.value = scopeSite;
      days.value = String(errorsDays);
      loadErrors();
    });
    errorUrl(false);
  }

  function renderBriefingFacts(selector, facts) {
    const target = document.querySelector(selector);
    target.innerHTML = facts.map(fact => `<article><strong>${escapeHtml(String(fact.value))}</strong><span>${escapeHtml(fact.label)}</span>${fact.detail ? `<small>${escapeHtml(fact.detail)}</small>` : ""}</article>`).join("");
  }

  async function loadBriefing(push = false) {
    clearError();
    try {
      const parameters = new URLSearchParams();
      if (briefingWeek) parameters.set("week", briefingWeek);
      if (scopeSite) parameters.set("site", scopeSite);
      const data = await api(`/api/briefing?${parameters}`);
      briefingWeek = data.week.from;
      const urlParameters = new URLSearchParams({ week: briefingWeek });
      if (scopeSite) urlParameters.set("site", scopeSite);
      window.history[push ? "pushState" : "replaceState"]({}, "", `${window.location.pathname}?${urlParameters}`);
      document.querySelector("#briefing-range").textContent = dateLabel(data.week.from, data.week.to);
      const status = document.querySelector("#briefing-status");
      status.textContent = data.week.complete ? "Complete" : "In progress";
      status.className = `status-badge ${data.week.complete ? "ok" : ""}`;
      const weekSelect = document.querySelector("#briefing-week");
      weekSelect.innerHTML = data.available_weeks.map(week => {
        const label = `${dateLabel(week.week, week.to)}${week.complete ? "" : " · in progress"}`;
        return `<option value="${escapeHtml(week.week)}"${week.week === data.week.from ? " selected" : ""}>${escapeHtml(label)}</option>`;
      }).join("");
      const narrative = data.narrative.length
        ? data.narrative
        : ["The week was quiet. No human page traffic or notable automatic moments were recorded."];
      document.querySelector("#briefing-narrative").innerHTML = narrative
        .map((line, index) => `<p${index === 0 ? ' class="lead"' : ""}>${escapeHtml(line)}</p>`)
        .join("");
      const requestChange = data.summary.request_change_percent;
      const visitorChange = data.summary.visitor_change_percent;
      document.querySelector("#briefing-metrics").innerHTML = [
        metric("Human page requests", fmt.format(data.summary.requests)),
        metric("Traffic change", requestChange === null ? "New activity" : `${requestChange > 0 ? "+" : ""}${requestChange}%`),
        metric("Visitor-days", fmt.format(data.summary.visitor_days)),
        metric("Visitor change", visitorChange === null ? "New activity" : `${visitorChange > 0 ? "+" : ""}${visitorChange}%`),
        metric("Active sites", fmt.format(data.summary.active_sites)),
        metric("Countries", fmt.format(data.geography.countries)),
      ].join("");
      const siteBody = document.querySelector("#briefing-sites");
      siteBody.innerHTML = data.sites.some(site => site.requests || site.previous_requests)
        ? data.sites.filter(site => site.requests || site.previous_requests).map(site => {
            const style = site.change > 0 ? "change-up" : site.change < 0 ? "change-down" : "";
            return `<tr><td><a class="table-link" href="/site/${encodeURIComponent(site.site)}?from=${encodeURIComponent(data.week.from)}&to=${encodeURIComponent(data.week.to)}">${escapeHtml(site.site)}</a></td><td>${fmt.format(site.requests)}</td><td class="${style}">${escapeHtml(signed(site.change))}</td></tr>`;
          }).join("")
        : '<tr><td class="empty" colspan="3">No site traffic in this week.</td></tr>';
      const pageBody = document.querySelector("#briefing-pages");
      pageBody.innerHTML = data.page_changes.length
        ? data.page_changes.map(item => {
            const style = item.change > 0 ? "change-up" : item.change < 0 ? "change-down" : "";
            const params = new URLSearchParams({ from: data.week.from, to: data.week.to, bots: "0", assets: "0", path: item.path });
            const href = `/site/${encodeURIComponent(item.site)}/page?${params}`;
            return `<tr><td><a class="table-link" href="${escapeHtml(href)}">${escapeHtml(item.path)}</a><span class="table-subtitle">${escapeHtml(item.site)}</span></td><td>${fmt.format(item.requests)}</td><td class="${style}">${escapeHtml(signed(item.change))}</td></tr>`;
          }).join("")
        : '<tr><td class="empty" colspan="3">No page movement to report.</td></tr>';
      const newPages = data.moments.filter(item => item.kind === "new_page");
      const newReferrers = data.moments.filter(item => item.kind === "new_referrer");
      const resurfaced = data.moments.filter(item => item.kind === "content_resurfaced");
      renderBriefingFacts("#briefing-discoveries", [
        { value: data.discoveries.pages, label: "new pages discovered", detail: newPages[0] ? `${newPages[0].site}${newPages[0].path}` : "No new content this week" },
        { value: data.discoveries.referrers, label: "new referring domains", detail: newReferrers[0] ? `${newReferrers[0].source} → ${newReferrers[0].path}` : "No new referrers this week" },
        { value: data.discoveries.resurfaced, label: "pages resurfaced", detail: resurfaced[0] ? `${resurfaced[0].site}${resurfaced[0].path}` : "No revivals this week" },
        { value: data.moment_counts.traffic_record || 0, label: "traffic records", detail: `${data.moment_counts.traffic_spike || 0} unusual spikes` },
      ]);
      renderBriefingFacts("#briefing-signals", [
        { value: data.ai.requests, label: "AI crawler page requests", detail: `${data.ai.agents} agents across ${data.ai.pages} pages` },
        { value: data.feeds.reporting_feeds ? fmt.format(data.feeds.reported_subscribers) : "—", label: "reported feed subscriptions", detail: data.feeds.reporting_feeds ? `${signed(data.feeds.change)} from the prior week` : "No reader reports a subscriber count" },
        { value: data.geography.new_countries.length, label: "first-time countries", detail: data.geography.new_countries.slice(0, 5).join(", ") || "No first-time countries" },
      ]);
      renderEvents(data.moments, "#briefing-moments");
    } catch (error) { showError(error); }
  }

  function setupBriefings() {
    document.querySelector("#apply-briefing").addEventListener("click", () => {
      briefingWeek = document.querySelector("#briefing-week").value;
      loadBriefing(true);
    });
    window.addEventListener("popstate", () => {
      const parameters = new URLSearchParams(window.location.search);
      briefingWeek = parameters.get("week") || "";
      scopeSite = parameters.get("site") || "";
      const select = document.querySelector("#site-filter");
      if (select) select.value = scopeSite;
      loadBriefing(false);
    });
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
      const [series, pages, referrers, statuses, agents, countries, journal] = await Promise.all([
        api(`/api/site/${encodeURIComponent(site)}/timeseries?${query({ interval })}`),
        api(`/api/site/${encodeURIComponent(site)}/pages?${suffix}&limit=25&offset=${pagesOffset}`),
        api(`/api/site/${encodeURIComponent(site)}/referrers?${suffix}`),
        api(`/api/site/${encodeURIComponent(site)}/status?${suffix}`),
        api(`/api/site/${encodeURIComponent(site)}/agents?${suffix}`),
        api(`/api/site/${encodeURIComponent(site)}/countries?${suffix}`),
        api(`/api/events?${query({ site, limit: 100 })}`),
      ]);
      const actualInterval = series.interval;
      updateChart("site", document.querySelector("#site-chart"), {
        type: "line",
        data: { labels: series.series.map(row => actualInterval === "hour" ? `${row.bucket.slice(11, 16)} ${row.bucket.slice(-5)}` : row.bucket), datasets: [
          { label: "Requests", data: series.series.map(row => row.requests), borderColor: colors[0], backgroundColor: `${colors[0]}22`, tension: .3, fill: true },
          { label: actualInterval === "hour" ? "Hourly visitors" : "Daily visitors", data: series.series.map(row => row.unique_visitors), borderColor: colors[1], backgroundColor: "transparent", tension: .3 },
        ] },
        options: optionsWithMoments(actualInterval === "day" ? journal.events : []),
        plugins: [momentMarkerPlugin],
      });
      renderEvents(journal.events, "#site-event-journal");
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
        { key: "first_seen" },
        { key: "last_seen" },
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
      const journey = data.journey;
      document.querySelector("#page-journey-totals").textContent = journey.protected
        ? "Not collected for this privacy-protected site"
        : `${fmt.format(journey.entrances)} entrances · ${fmt.format(journey.exits)} exits`;
      const journeyPath = value => `<a class="table-link" href="/site/${encodeURIComponent(site)}/page?${new URLSearchParams({ ...range, bots: "0", assets: "0", path: value })}">${escapeHtml(value)}</a>`;
      fillTable("#page-journey-previous", journey.previous, [
        { key: "path", format: journeyPath },
        { key: "transitions", format: fmt.format },
      ], journey.protected ? "Journey data is not collected for this site." : "No preceding page observed in this range.");
      fillTable("#page-journey-next", journey.next, [
        { key: "path", format: journeyPath },
        { key: "transitions", format: fmt.format },
      ], journey.protected ? "Journey data is not collected for this site." : "No following page observed in this range.");
    } catch (error) { showError(error); }
  }

  function durationLabel(seconds) {
    if (!seconds) return "0s";
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  }

  async function loadJourneys() {
    clearError();
    try {
      const data = await api(`/api/journeys?${query()}`);
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      const privacy = document.querySelector("#journey-privacy");
      privacy.classList.toggle("hidden", !data.privacy.protected);
      privacy.textContent = data.privacy.protected
        ? `${data.scope.site} is privacy-protected. Reading Paths never processes or stores its visit sequences.`
        : "";
      document.querySelector("#journey-metrics").innerHTML = [
        metric("Inferred visits", fmt.format(data.totals.sessions)),
        metric("Average depth", `${fmt.format(data.totals.average_depth)} pages`),
        metric("Multi-page visits", `${data.totals.multi_page_rate}%`),
        metric("Average observed span", durationLabel(data.totals.average_duration_seconds)),
      ].join("");
      updateChart("journeys", document.querySelector("#journey-chart"), {
        type: "line",
        data: {
          labels: data.series.map(row => row.bucket),
          datasets: [
            { label: "Inferred visits", data: data.series.map(row => row.sessions), borderColor: colors[0], backgroundColor: `${colors[0]}22`, tension: .3, fill: true },
            { label: "Multi-page visits", data: data.series.map(row => row.multi_page_sessions), borderColor: colors[2], backgroundColor: "transparent", tension: .3 },
          ],
        },
        options: chartOptions,
      });
      const flow = document.querySelector("#journey-transitions");
      flow.innerHTML = data.transitions.length ? data.transitions.map(item => {
        const from = pageStoryHref({ site: item.site, path: item.from_path }, data.from, data.to);
        const to = pageStoryHref({ site: item.site, path: item.to_path }, data.from, data.to);
        return `<article class="journey-transition"><a href="${escapeHtml(from)}">${escapeHtml(item.from_path)}</a><span class="journey-arrow" aria-label="then">→</span><a href="${escapeHtml(to)}">${escapeHtml(item.to_path)}</a><strong>${fmt.format(item.transitions)}</strong><small>${escapeHtml(item.site)}</small></article>`;
      }).join("") : '<p class="empty">No multi-page reading paths were inferred in this range.</p>';
      const pageLink = (value, item) => `<a class="table-link" href="${escapeHtml(pageStoryHref({ site: item.site, path: value }, data.from, data.to))}">${escapeHtml(value)}</a>`;
      fillTable("#journey-entrances", data.entrances, [
        { key: "path", format: pageLink },
        { key: "site" },
        { key: "visits", format: fmt.format },
        { key: "single_page_visits", format: fmt.format },
      ], "No inferred entrances in this range.");
      fillTable("#journey-exits", data.exits, [
        { key: "path", format: pageLink },
        { key: "site" },
        { key: "visits", format: fmt.format },
      ], "No inferred exits in this range.");
    } catch (error) { showError(error); }
  }

  function linkBadge(category) {
    return `<span class="link-badge ${escapeHtml(category)}">${escapeHtml(category.replace("-", " "))}</span>`;
  }

  function linkAtlasHref(source) {
    const parameters = new URLSearchParams({
      ...range, bots: "0", assets: "0", source,
    });
    if (scopeSite) parameters.set("site", scopeSite);
    return `/links?${parameters}`;
  }

  async function loadLinkAtlas() {
    clearError();
    try {
      const data = await api(`/api/link-atlas?${query({ limit: 100 })}`);
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      const privacy = document.querySelector("#link-privacy");
      privacy.classList.toggle("hidden", !data.privacy.protected);
      privacy.textContent = data.privacy.protected
        ? `${data.scope.site} does not collect referrers, so Link Atlas has no source or destination relationships for this site.`
        : "";

      const sourceSelect = document.querySelector("#link-source");
      sourceSelect.innerHTML = ["", ...data.available_sources]
        .map(source => `<option value="${escapeHtml(source)}"${source === linkSource ? " selected" : ""}>${escapeHtml(source || "All sources")}</option>`)
        .join("");

      document.querySelector("#link-metrics").innerHTML = [
        metric("Referred visits", fmt.format(data.totals.requests)),
        metric("Active sources", fmt.format(data.totals.active_sources)),
        metric("New source/site pairs", fmt.format(data.totals.new_sources)),
        metric("Linked pages", fmt.format(data.totals.linked_pages)),
        metric("Sites reached", fmt.format(data.totals.sites)),
      ].join("");
      document.querySelector("#link-chart-heading").textContent = linkSource
        ? `Visits from ${linkSource}` : "Visits from elsewhere";
      updateChart("link-atlas", document.querySelector("#link-chart"), {
        type: "line",
        data: {
          labels: data.series.map(row => row.bucket),
          datasets: [{
            label: linkSource || "Referred visits",
            data: data.series.map(row => row.requests),
            borderColor: colors[0], backgroundColor: `${colors[0]}22`,
            tension: .3, fill: true,
          }],
        },
        options: chartOptions,
      });

      const sources = document.querySelector("#link-sources");
      sources.innerHTML = data.sources.length ? data.sources.map(item => {
        const href = linkAtlasHref(item.source);
        const changeClass = item.change > 0 ? "change-up" : item.change < 0 ? "change-down" : "";
        return `<article class="link-source-card"><div class="link-source-top">${linkBadge(item.category)}<span class="${changeClass}">${escapeHtml(signed(item.change))}</span></div><a href="${escapeHtml(href)}">${escapeHtml(item.source)}</a><small>${escapeHtml(item.site)}</small><p>${escapeHtml(item.explanation)}</p><div class="link-source-numbers"><strong>${fmt.format(item.requests)}</strong><span>visits · previous ${fmt.format(item.prior_requests)}</span></div><div class="link-source-dates"><span>First ${escapeHtml(item.first_seen)}</span><span>Last ${escapeHtml(item.last_seen)}</span><span>${fmt.format(item.active_days)} active days</span></div></article>`;
      }).join("") : '<p class="empty">No referring sources were active in this or the preceding period.</p>';

      const map = document.querySelector("#link-relationships");
      map.innerHTML = data.relationships.length ? data.relationships.map(item => {
        const sourceHref = linkAtlasHref(item.source);
        const pageHref = pageStoryHref(item, data.from, data.to);
        return `<article class="link-relationship"><a class="link-node source" href="${escapeHtml(sourceHref)}">${escapeHtml(item.source)}</a><span class="journey-arrow" aria-label="links to">→</span><a class="link-node destination" href="${escapeHtml(pageHref)}">${escapeHtml(item.path)}</a><strong>${fmt.format(item.requests)}</strong><small>${escapeHtml(item.site)} · ${escapeHtml(item.first_seen)} to ${escapeHtml(item.last_seen)}</small></article>`;
      }).join("") : '<p class="empty">No source-to-page relationships in this range.</p>';
    } catch (error) { showError(error); }
  }

  function inboxHref(event, from, to) {
    if (event.kind === "new_referrer" && event.source) {
      return `/links?${new URLSearchParams({
        from, to, bots: "0", assets: "0", site: event.site,
        source: event.source,
      })}`;
    }
    if (event.kind === "first_ai_visit") {
      return `/ai-crawlers?${new URLSearchParams({
        from, to, bots: "1", assets: "0", site: event.site,
      })}`;
    }
    if (event.kind === "feed_subscriber_milestone") {
      return `/feed-readers?${new URLSearchParams({
        from, to, bots: "1", assets: "0", site: event.site,
      })}`;
    }
    if (event.path) return pageStoryHref(event, from, to);
    return `/site/${encodeURIComponent(event.site)}?${new URLSearchParams({
      from, to, bots: "0", assets: "0",
    })}`;
  }

  async function loadInbox() {
    clearError();
    const previousSeenAt = inboxSeenAt;
    try {
      const data = await api(`/api/inbox?${query({ limit: 500 })}`);
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      const privacy = document.querySelector("#inbox-privacy");
      privacy.classList.toggle("hidden", !data.privacy.protected);
      privacy.textContent = data.privacy.protected
        ? `${data.scope.site} is privacy-protected. Discovery Inbox does not expose its page-level moments.`
        : "";
      const category = document.querySelector("#inbox-category");
      category.value = inboxCategory;
      document.querySelector("#inbox-metrics").innerHTML = [
        metric("All moments", fmt.format(data.counts.all)),
        metric("Discoveries", fmt.format(data.counts.discovery)),
        metric("Readers", fmt.format(data.counts.readers)),
        metric("Momentum", fmt.format(data.counts.momentum)),
      ].join("");
      const unread = data.events.filter(item => item.occurred_at > previousSeenAt).length;
      document.querySelector("#inbox-heading").textContent = inboxCategory
        ? `${category.options[category.selectedIndex].text} in this range`
        : "What changed";
      document.querySelector("#inbox-unread-label").textContent = previousSeenAt
        ? `${fmt.format(unread)} new since your last visit`
        : `${fmt.format(unread)} recent moments`;
      const target = document.querySelector("#inbox-events");
      target.innerHTML = data.events.length ? data.events.map(event => {
        const [label, detail, presentation] = eventPresentation(event);
        const unreadClass = event.occurred_at > previousSeenAt ? " unread" : "";
        const href = inboxHref(event, data.from, data.to);
        const country = event.country ? ` · ${countryFlag(event.country)} ${event.country}` : "";
        return `<li class="inbox-event${unreadClass}"><span class="event-marker ${escapeHtml(presentation)}"></span><div class="inbox-event-copy"><div class="inbox-event-top"><span class="inbox-category ${escapeHtml(event.category)}">${escapeHtml(event.category)}</span><time datetime="${escapeHtml(new Date(event.occurred_at * 1000).toISOString())}">${escapeHtml(new Date(event.occurred_at * 1000).toLocaleString())}</time></div><a href="${escapeHtml(href)}">${escapeHtml(label)}</a><p>${escapeHtml(detail)}</p><small>${escapeHtml(event.site)}${escapeHtml(country)}</small></div></li>`;
      }).join("") : '<li class="empty">Nothing landed in this category and date range.</li>';
      if (data.to === serverToday) writeInboxSeenAt(data.generated_at);
      updateInboxCount(0);
    } catch (error) { showError(error); }
  }

  function galaxyNodeHref(node, data) {
    if (node.kind === "page") return pageStoryHref(node, data.from, data.to);
    if (node.kind === "source") {
      const params = new URLSearchParams({
        from: data.from, to: data.to, bots: "0", assets: "0",
        source: node.source,
      });
      if (scopeSite) params.set("site", scopeSite);
      return `/links?${params}`;
    }
    const params = new URLSearchParams({
      from: data.from, to: data.to, bots: "1", assets: "0",
    });
    if (scopeSite) params.set("site", scopeSite);
    return `/ai-crawlers?${params}`;
  }

  function galaxyPositions(nodes) {
    const positions = new Map();
    const pages = nodes.filter(node => node.kind === "page");
    const sources = nodes.filter(node => node.kind === "source");
    const agents = nodes.filter(node => node.kind === "ai");
    const center = { x: 560, y: 340 };
    pages.forEach((node, index) => {
      if (index === 0) return positions.set(node.id, center);
      const inner = index <= 8;
      const offset = inner ? 1 : 9;
      const count = inner ? Math.min(8, Math.max(1, pages.length - 1)) : Math.max(1, pages.length - 9);
      const angle = -Math.PI / 2 + 2 * Math.PI * (index - offset) / count;
      const radiusX = inner ? 170 : 285;
      const radiusY = inner ? 130 : 245;
      positions.set(node.id, {
        x: center.x + Math.cos(angle) * radiusX,
        y: center.y + Math.sin(angle) * radiusY,
      });
    });
    sources.forEach((node, index) => positions.set(node.id, {
      x: index % 2 ? 185 : 75,
      y: 72 + (index + .5) * 536 / Math.max(1, sources.length),
    }));
    agents.forEach((node, index) => positions.set(node.id, {
      x: index % 2 ? 935 : 1045,
      y: 84 + (index + .5) * 512 / Math.max(1, agents.length),
    }));
    return positions;
  }

  function renderGalaxy() {
    const target = document.querySelector("#galaxy-map");
    if (!target || !galaxyData) return;
    if (!galaxyData.nodes.length) {
      target.innerHTML = '<p class="empty">No connected pages in this range.</p>';
      return;
    }
    const layers = {
      referral: document.querySelector("#galaxy-referrals")?.checked ?? true,
      journey: document.querySelector("#galaxy-journeys")?.checked ?? true,
      ai: document.querySelector("#galaxy-ai")?.checked ?? true,
    };
    const visibleNodes = galaxyData.nodes.filter(node =>
      node.kind === "page" || (node.kind === "source" && layers.referral)
      || (node.kind === "ai" && layers.ai)
    );
    const nodeIds = new Set(visibleNodes.map(node => node.id));
    const visibleEdges = galaxyData.edges.filter(edge =>
      layers[edge.kind] && nodeIds.has(edge.from) && nodeIds.has(edge.to)
    );
    const positions = galaxyPositions(galaxyData.nodes);
    const maxWeight = Math.max(1, ...galaxyData.nodes.map(node => node.weight));
    const shortLabel = value => value.length > 24 ? `${value.slice(0, 22)}…` : value;
    const edgeSvg = visibleEdges.map(edge => {
      const from = positions.get(edge.from);
      const to = positions.get(edge.to);
      const bend = edge.kind === "journey" ? -18 : edge.kind === "ai" ? 12 : 0;
      const middleX = (from.x + to.x) / 2;
      const middleY = (from.y + to.y) / 2 + bend;
      const width = Math.min(6, 1 + Math.log2(edge.weight + 1));
      return `<path class="galaxy-edge ${escapeHtml(edge.kind)}" aria-hidden="true" d="M ${from.x.toFixed(1)} ${from.y.toFixed(1)} Q ${middleX.toFixed(1)} ${middleY.toFixed(1)} ${to.x.toFixed(1)} ${to.y.toFixed(1)}" style="stroke-width:${width.toFixed(1)}" marker-end="url(#arrow-${escapeHtml(edge.kind)})"></path>`;
    }).join("");
    const nodeSvg = visibleNodes.map(node => {
      const position = positions.get(node.id);
      const radius = Math.min(20, 7 + 12 * Math.sqrt(node.weight / maxWeight));
      const href = galaxyNodeHref(node, galaxyData);
      const description = node.kind === "page"
        ? `${node.site} ${node.path}: ${node.weight} human visits`
        : node.kind === "source"
          ? `${node.source}: ${node.weight} referred visits`
          : `${node.agent} by ${node.provider}: ${node.weight} requests`;
      return `<a href="${escapeHtml(href)}" role="link" tabindex="0" aria-label="${escapeHtml(description)}"><g class="galaxy-node ${escapeHtml(node.kind)}" transform="translate(${position.x.toFixed(1)} ${position.y.toFixed(1)})"><circle r="${radius.toFixed(1)}"></circle><text y="${(radius + 14).toFixed(1)}" text-anchor="middle">${escapeHtml(shortLabel(node.label))}</text><title>${escapeHtml(description)}</title></g></a>`;
    }).join("");
    target.innerHTML = `<svg class="galaxy-svg" viewBox="0 0 1120 680" aria-label="Page Galaxy relationship map"><title>Page Galaxy relationship map</title><defs><marker id="arrow-referral" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker><marker id="arrow-journey" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker><marker id="arrow-ai" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker></defs>${edgeSvg}${nodeSvg}</svg>`;
  }

  async function loadGalaxy() {
    clearError();
    try {
      const data = await api(`/api/galaxy?${query({ limit: 24 })}`);
      galaxyData = data;
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      const privacy = document.querySelector("#galaxy-privacy");
      privacy.classList.toggle("hidden", !data.privacy.protected);
      privacy.textContent = data.privacy.protected
        ? `${data.scope.site} is privacy-protected. Page Galaxy does not construct relationships from its activity.`
        : "";
      document.querySelector("#galaxy-metrics").innerHTML = [
        metric("Human page visits", fmt.format(data.totals.page_requests)),
        metric("Pages", fmt.format(data.totals.pages)),
        metric("Mapped referring sites", fmt.format(data.totals.mapped_sources)),
        metric("AI readers", fmt.format(data.totals.ai_agents)),
        metric("Connections", fmt.format(data.totals.connections)),
      ].join("");
      renderGalaxy();
      fillTable("#galaxy-pages", data.pages, [
        { key: "path", format: (value, item) => `<a class="table-link" href="${escapeHtml(pageStoryHref(item, data.from, data.to))}">${escapeHtml(value)}</a>` },
        { key: "site" },
        { key: "requests", format: fmt.format },
        { key: "referrals", format: fmt.format },
        { key: "sources", format: fmt.format },
        { key: "ai_requests", format: fmt.format },
        { key: "entrances", format: fmt.format },
        { key: "exits", format: fmt.format },
      ], "No successful human pages in this range.");
    } catch (error) { showError(error); }
  }

  const countryPoints = {
    AD: [1.6, 42.5], AE: [54.4, 24.4], AF: [67.7, 33.9], AL: [20.2, 41.2],
    AR: [-64, -34], AT: [14.6, 47.5], AU: [134, -25], AZ: [47.6, 40.1],
    BD: [90.4, 23.7], BE: [4.7, 50.8], BG: [25.5, 42.7], BH: [50.6, 26],
    BO: [-64.7, -16.7], BR: [-51.9, -14.2], CA: [-106, 56], CH: [8.2, 46.8],
    CL: [-71.5, -35.7], CN: [104, 35], CO: [-74.3, 4.6], CR: [-84, 9.7],
    CY: [33.4, 35.1], CZ: [15.5, 49.8], DE: [10.5, 51.2], DK: [9.5, 56.3],
    DO: [-70.2, 18.7], DZ: [1.7, 28], EC: [-78.2, -1.8], EE: [25, 58.6],
    EG: [30.8, 26.8], ES: [-3.7, 40.5], FI: [26, 64], FR: [2.2, 46.2],
    GB: [-3, 55], GE: [43.4, 42.3], GH: [-1, 7.9], GP: [-61.6, 16.2],
    GR: [21.8, 39.1], GT: [-90.2, 15.8], GY: [-58.9, 4.9], HK: [114.2, 22.3],
    HR: [15.2, 45.1], HU: [19.5, 47.2], ID: [117.3, -2.5], IE: [-8, 53.4],
    IL: [34.9, 31.5], IN: [78.9, 20.6], IQ: [43.7, 33.2], IR: [53.7, 32.4],
    IS: [-19, 65], IT: [12.6, 42.8], JM: [-77.3, 18.1], JO: [36.2, 31.2],
    JP: [138.3, 36.2], KE: [37.9, .2], KH: [104.9, 12.6], KR: [128, 36.5],
    KW: [47.5, 29.3], KZ: [66.9, 48], LB: [35.9, 33.9], LK: [80.8, 7.9],
    LT: [23.9, 55.2], LU: [6.1, 49.8], LV: [24.6, 57], MA: [-7.1, 31.8],
    MX: [-102.6, 23.6], MY: [101.7, 4.2], NG: [8.7, 9.1], NL: [5.3, 52.1],
    NO: [8.5, 60.5], NZ: [174.9, -40.9], OM: [55.9, 21.5], PA: [-80.8, 8.5],
    PE: [-75, -9.2], PH: [121.8, 12.9], PK: [69.3, 30.4], PL: [19.1, 51.9],
    PR: [-66.6, 18.2], PT: [-8.2, 39.4], PY: [-58.4, -23.4], QA: [51.2, 25.4],
    RO: [24.9, 45.9], RS: [21, 44], RU: [100, 61], SA: [45.1, 23.9],
    SE: [18.6, 60.1], SG: [103.8, 1.4], SI: [14.8, 46.2], SK: [19.7, 48.7],
    TH: [100.9, 15.9], TN: [9.5, 33.9], TR: [35.2, 39], TW: [121, 23.7],
    UA: [31.2, 48.4], US: [-98, 39], UY: [-55.8, -32.5], UZ: [64.6, 41.4],
    VE: [-66.6, 6.4], VN: [108.3, 14.1], ZA: [24, -29], ZW: [29.2, -19],
  };
  const countryNames = typeof Intl.DisplayNames === "function"
    ? new Intl.DisplayNames(undefined, { type: "region" })
    : null;
  function countryName(code) {
    try { return countryNames?.of(code) || code; } catch (_error) { return code; }
  }

  function countryFlag(code) {
    return /^[A-Z]{2}$/.test(code)
      ? String.fromCodePoint(...[...code].map(letter => 127397 + letter.charCodeAt(0)))
      : "🌐";
  }

  function relativeAge(timestamp) {
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
    if (seconds < 10) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    return `${Math.floor(minutes / 60)}h ago`;
  }

  function updateLiveAges() {
    document.querySelectorAll("[data-live-ts]").forEach(node => {
      node.textContent = relativeAge(Number(node.dataset.liveTs));
    });
  }

  function renderWorldRadar(countries) {
    const pulses = document.querySelector("#live-map-pulses");
    if (!pulses) return;
    pulses.innerHTML = countries.flatMap(country => {
      const point = countryPoints[country.country];
      if (!point) return [];
      const [longitude, latitude] = point;
      const x = (longitude + 180) * 1000 / 360;
      const y = (90 - latitude) * 500 / 180;
      const radius = Math.min(8, 3.5 + Math.sqrt(country.requests));
      const title = `${countryName(country.country)}: ${fmt.format(country.requests)} visits`;
      return [`<g class="radar-pulse" transform="translate(${x.toFixed(1)} ${y.toFixed(1)})"><title>${escapeHtml(title)}</title><circle class="radar-wave" r="${radius}"></circle><circle class="radar-core" r="${Math.max(2.5, radius / 2)}"></circle></g>`];
    }).join("");
    const list = document.querySelector("#live-country-list");
    list.innerHTML = countries.length
      ? countries.slice(0, 8).map(country => `<span title="${escapeHtml(countryName(country.country))}">${countryFlag(country.country)} ${escapeHtml(country.country)} <strong>${fmt.format(country.requests)}</strong></span>`).join("")
      : '<span class="muted">No located visits in this window.</span>';
  }

  function renderLiveStream(activity) {
    const stream = document.querySelector("#live-stream");
    if (!activity.length) {
      stream.innerHTML = '<li class="empty">The radar is quiet right now.</li>';
      return;
    }
    const momentBadges = {
      new_page: ["page", "New page"],
      new_referrer: ["referrer", "New referrer"],
      traffic_record: ["record", "Record"],
      traffic_spike: ["spike", "Spike"],
      visitor_milestone: ["milestone", "Milestone"],
      content_resurfaced: ["resurfaced", "Resurfaced"],
    };
    stream.innerHTML = activity.map(hit => {
      const detail = [hit.country ? `${countryFlag(hit.country)} ${hit.country}` : null, hit.browser, hit.referrer_host ? `via ${hit.referrer_host}` : null].filter(Boolean).join(" · ");
      const badges = hit.moments.map(kind => momentBadges[kind])
        .filter(Boolean)
        .map(([style, label]) => `<span class="moment-chip ${style}">${label}</span>`)
        .join("");
      const params = new URLSearchParams({ from: hit.day, to: hit.day, bots: "0", assets: "0", path: hit.path });
      const href = `/site/${encodeURIComponent(hit.site)}/page?${params}`;
      return `<li class="live-hit"><span class="live-hit-dot"></span><div><div class="live-hit-heading"><a href="${escapeHtml(href)}">${escapeHtml(hit.path)}</a>${badges}</div><p>${escapeHtml(hit.site)}${detail ? ` · ${escapeHtml(detail)}` : ""}</p></div><time datetime="${escapeHtml(new Date(hit.ts * 1000).toISOString())}" data-live-ts="${hit.ts}">${escapeHtml(relativeAge(hit.ts))}</time></li>`;
    }).join("");
  }

  async function loadLive() {
    clearError();
    try {
      const parameters = new URLSearchParams({ minutes: "60", limit: "40" });
      if (scopeSite) parameters.set("site", scopeSite);
      const data = await api(`/api/live?${parameters}`);
      document.querySelector("#live-updated").textContent = `Updated ${new Date(data.generated_at * 1000).toLocaleTimeString()}`;
      document.querySelector("#live-cards").innerHTML = data.sites.map(site => `<a class="site-card" href="/site/${encodeURIComponent(site.site)}"><span class="site-name">${escapeHtml(site.site)}</span><strong class="site-value">${fmt.format(site.requests)}</strong><span class="site-meta"><span>${fmt.format(site.unique_visitors)} visitors</span><span>${compactBytes(site.bytes)}</span></span></a>`).join("");
      document.querySelector("#live-metrics").innerHTML = [
        metric("Human page visits", fmt.format(data.totals.requests)),
        metric("Recent visitors", fmt.format(data.totals.visitors)),
        metric("Countries", fmt.format(data.totals.countries)),
        metric("Active sites", fmt.format(data.totals.sites)),
      ].join("");
      renderWorldRadar(data.countries);
      renderLiveStream(data.activity);
      const excluded = data.privacy.excluded_activity_sites.join(", ");
      document.querySelector("#live-privacy").textContent = scopeSite && data.privacy.excluded_activity_sites.includes(scopeSite)
        ? `${scopeSite} is privacy-protected. Only anonymous aggregate totals are shown; its activity stream and map remain hidden.`
        : `${excluded} contributes only anonymous site totals and is excluded from this activity stream and map.`;
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

  function changeDriverRows(rows, label, href, emptyText) {
    if (!rows.length) return `<p class="empty">${escapeHtml(emptyText)}</p>`;
    const maximum = Math.max(...rows.map(row => Math.abs(row.change)), 1);
    return rows.slice(0, 8).map(row => {
      const text = label(row);
      const destination = href?.(row);
      const title = destination
        ? `<a href="${escapeHtml(destination)}">${escapeHtml(text)}</a>`
        : `<strong>${escapeHtml(text)}</strong>`;
      const direction = row.change > 0 ? "up" : "down";
      const width = Math.max(5, Math.round(100 * Math.abs(row.change) / maximum));
      return `<article class="change-driver ${direction}"><div class="change-driver-copy">${title}<small>${escapeHtml(row.site || "All sites")} · ${fmt.format(row.previous_requests)} → ${fmt.format(row.requests)}</small></div><span class="change-value">${signed(row.change)}</span><div class="change-track"><span style="width:${width}%"></span></div></article>`;
    }).join("");
  }

  async function loadChanges() {
    clearError();
    try {
      const data = await api(`/api/changes?${query({ limit: "16" })}`);
      document.querySelector("#range-label").textContent = dateLabel(data.window.from, data.window.to);
      document.querySelector("#changes-comparison").textContent = `vs ${dateLabel(data.window.previous_from, data.window.previous_to)}`;
      const change = data.summary.change;
      const percent = data.summary.change_percent;
      document.querySelector("#changes-heading").textContent = !data.summary.previous_requests
        ? "A new comparison baseline"
        : change === 0 ? "Traffic held steady"
          : `Traffic ${change > 0 ? "rose" : "fell"} ${Math.abs(percent)}%`;
      document.querySelector("#changes-narrative").innerHTML = data.narrative
        .map((line, index) => `<p${index === 0 ? ' class="lead"' : ""}>${escapeHtml(line)}</p>`)
        .join("");
      document.querySelector("#changes-metrics").innerHTML = [
        metric("Human page requests", fmt.format(data.summary.requests)),
        metric("Request change", signed(data.summary.change)),
        metric("Visitor-day change", signed(data.summary.visitor_change)),
        metric("Bot change", signed(data.audience.bots.change)),
        metric("AI crawler change", signed(data.audience.ai.change)),
        metric("Error change", signed(data.errors.change)),
      ].join("");
      const privacy = document.querySelector("#changes-privacy");
      privacy.classList.toggle("hidden", !data.privacy.protected);
      privacy.textContent = data.privacy.protected
        ? "This site contributes anonymous totals, but its page-level change drivers are intentionally private."
        : "";
      document.querySelector("#change-pages").innerHTML = changeDriverRows(
        data.pages, row => row.path,
        row => pageStoryHref(row, data.window.from, data.window.to),
        "No page-level movement in these windows."
      );
      document.querySelector("#change-referrers").innerHTML = changeDriverRows(
        data.referrers, row => row.source,
        row => `/links?${new URLSearchParams({ from: data.window.from, to: data.window.to, site: row.site, source: row.source })}`,
        "No referral movement in these windows."
      );
      const audienceRows = [
        { site: "All selected traffic", name: "All bots", ...data.audience.bots },
        { site: "Recognized agents", name: "AI crawlers", ...data.audience.ai },
        { site: "Recognized readers", name: "Feed readers", ...data.audience.feeds },
      ];
      document.querySelector("#change-audience").innerHTML = changeDriverRows(
        audienceRows, row => row.name, row => row.name === "AI crawlers" ? `/ai-crawlers?${query()}` : null,
        "No audience movement in these windows."
      );
      document.querySelector("#change-countries").innerHTML = changeDriverRows(
        data.countries, row => row.country, null,
        "No country movement in these windows."
      );
      document.querySelector("#change-errors").innerHTML = changeDriverRows(
        data.errors.paths, row => row.path,
        row => pageStoryHref(row, data.window.from, data.window.to),
        "No error movement in these windows."
      );
    } catch (error) { showError(error); }
  }

  function episodeLabel(value) {
    return String(value || "").replaceAll("-", " ").replace(/\b\w/g, letter => letter.toUpperCase());
  }

  function episodeTimeline(episode) {
    const maximum = Math.max(...episode.timeline.map(item => item.requests), 1);
    return `<div class="episode-timeline" role="img" aria-label="Daily traffic around this attention episode">${episode.timeline.map(item => {
      const height = Math.max(4, Math.round(100 * item.requests / maximum));
      const title = `${item.day}: ${fmt.format(item.requests)} request${item.requests === 1 ? "" : "s"}${item.peak ? " (peak)" : ""}`;
      return `<span class="episode-day ${escapeHtml(item.phase)}${item.peak ? " peak" : ""}" style="height:${height}%" title="${escapeHtml(title)}"><i></i></span>`;
    }).join("")}</div><div class="episode-timeline-axis"><span>${escapeHtml(episode.timeline[0]?.day || episode.start)}</span><span>Peak ${escapeHtml(episode.peak_day)}</span><span>${escapeHtml(episode.timeline.at(-1)?.day || episode.end)}</span></div>`;
  }

  function episodeDriverRows(rows, key, episode, kind) {
    if (!rows.length) return '<p class="episode-driver-empty">No concentrated driver.</p>';
    return rows.slice(0, 4).map(item => {
      const label = item[key];
      let href = "";
      if (kind === "page" || kind === "error") {
        href = pageStoryHref({ ...item, site: episode.site }, episode.start, episode.end);
      } else if (kind === "referrer") {
        href = `/links?${new URLSearchParams({ from: episode.start, to: episode.end, site: episode.site, source: label })}`;
      } else if (kind === "crawler") {
        href = `/ai-crawlers?${new URLSearchParams({ from: episode.start, to: episode.end, site: episode.site })}`;
      }
      const name = href
        ? `<a href="${escapeHtml(href)}">${escapeHtml(label)}</a>`
        : `<strong>${escapeHtml(label)}</strong>`;
      const movement = item.new ? "new" : `${signed(item.change)} vs before`;
      return `<div class="episode-driver-row">${name}<span>${fmt.format(item.requests)}</span><small>${escapeHtml(movement)}</small></div>`;
    }).join("");
  }

  function episodeMoments(moments) {
    if (!moments.length) return '<p class="episode-driver-empty">No automatic moment landed inside the episode.</p>';
    return moments.slice(0, 4).map(moment => {
      const [label, detail, category] = eventPresentation(moment);
      return `<div class="episode-moment"><span class="event-marker ${escapeHtml(category)}"></span><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail)}</small></div></div>`;
    }).join("");
  }

  function episodeCard(episode) {
    const multiple = episode.peak_multiple === null ? "new baseline" : `${episode.peak_multiple}× baseline`;
    const aftermath = episode.aftermath_average === null
      ? "Awaiting aftermath"
      : `${episode.aftermath_average} avg after`;
    const countryRows = episodeDriverRows(episode.drivers.countries, "country", episode, "country");
    return `<article class="episode-card ${escapeHtml(episode.kind)}">
      <header class="episode-card-header">
        <div><div class="episode-badges"><span class="episode-badge kind">${escapeHtml(episodeLabel(episode.kind))}</span><span class="episode-badge trajectory">${escapeHtml(episodeLabel(episode.trajectory))}</span><span class="episode-badge effect ${escapeHtml(episode.lasting_effect)}">${escapeHtml(episodeLabel(episode.lasting_effect))}</span></div><h3>${escapeHtml(episode.site)}</h3><p>${escapeHtml(dateLabel(episode.start, episode.end))} · ${fmt.format(episode.duration_days)} day${episode.duration_days === 1 ? "" : "s"}</p></div>
        <div class="episode-peak"><strong>${fmt.format(episode.peak_requests)}</strong><span>peak requests</span><small>${escapeHtml(multiple)}</small></div>
      </header>
      <div class="episode-stat-strip"><span><strong>${fmt.format(episode.requests)}</strong> during episode</span><span><strong>+${fmt.format(episode.excess_requests)}</strong> above baseline</span><span><strong>${fmt.format(episode.baseline_average)}</strong> prior daily avg</span><span><strong>${escapeHtml(aftermath)}</strong></span></div>
      <div class="episode-trajectory">${episodeTimeline(episode)}</div>
      <div class="episode-copy">${episode.narrative.map((line, index) => `<p${index === 0 ? ' class="lead"' : ""}>${escapeHtml(line)}</p>`).join("")}</div>
      <div class="episode-drivers">
        <section><h4>Pages</h4>${episodeDriverRows(episode.drivers.pages, "path", episode, "page")}</section>
        <section><h4>Referrers</h4>${episodeDriverRows(episode.drivers.referrers, "source", episode, "referrer")}<p class="episode-direct">${fmt.format(episode.drivers.direct_or_unknown)} direct or unknown</p></section>
        <section><h4>Countries</h4>${countryRows}</section>
        <section><h4>Search &amp; AI crawlers</h4>${episodeDriverRows(episode.drivers.crawlers, "agent", episode, "crawler")}</section>
        <section><h4>Errors</h4><p class="episode-driver-summary"><strong>${fmt.format(episode.drivers.errors.requests)}</strong> human errors · ${signed(episode.drivers.errors.change)} vs before</p>${episodeDriverRows(episode.drivers.errors.paths, "path", episode, "error")}</section>
        <section><h4>Automatic moments</h4>${episodeMoments(episode.drivers.moments)}</section>
      </div>
    </article>`;
  }

  async function loadEpisodes() {
    clearError();
    try {
      const data = await api(`/api/episodes?${query({ limit: "20" })}`);
      document.querySelector("#range-label").textContent = dateLabel(data.window.from, data.window.to);
      document.querySelector("#episodes-heading").textContent = data.summary.episodes
        ? `${fmt.format(data.summary.episodes)} unusual attention episode${data.summary.episodes === 1 ? "" : "s"}`
        : "No unusual attention episodes";
      document.querySelector("#episodes-narrative").textContent = data.narrative;
      document.querySelector("#episodes-metrics").innerHTML = [
        metric("Episodes", fmt.format(data.summary.episodes)),
        metric("Sites affected", fmt.format(data.summary.sites)),
        metric("Excess requests", `+${fmt.format(data.summary.excess_requests)}`),
        metric("Still unfolding", fmt.format(data.summary.active)),
        metric("Longest episode", `${fmt.format(data.summary.longest_days)} day${data.summary.longest_days === 1 ? "" : "s"}`),
      ].join("");
      const privacy = document.querySelector("#episodes-privacy");
      privacy.classList.toggle("hidden", !data.privacy.protected);
      privacy.textContent = data.privacy.protected ? data.narrative : "";
      document.querySelector("#episodes-list").innerHTML = data.episodes.length
        ? data.episodes.map(episodeCard).join("")
        : '<div class="episode-empty"><strong>The traffic stayed inside its ordinary range.</strong><p>Try a longer window or select another site. Smaller changes still appear in the Change Engine.</p></div>';
    } catch (error) { showError(error); }
  }

  function latencyLabel(value) {
    if (value === null || value === undefined) return "—";
    if (value < 1000) return `${fmt.format(value)} ms`;
    return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`;
  }

  function signedBytes(value) {
    if (!value) return "0 B";
    return `${value > 0 ? "+" : "−"}${compactBytes(Math.abs(value))}`;
  }

  function reliabilityIncidentCard(item, data) {
    const pageHref = item.path
      ? pageStoryHref(item, data.from, data.to)
      : `/errors?${new URLSearchParams({ site: item.site, days: "30" })}`;
    let value;
    let comparison;
    if (item.kind === "size-anomaly") {
      value = compactBytes(item.value);
      comparison = `${signedBytes(item.change)} vs before`;
    } else if (item.kind === "error-burst") {
      value = `${fmt.format(item.errors)} errors`;
      comparison = item.recovered ? `Recovered ${item.recovery_day}` : "Recovery unresolved";
    } else {
      value = latencyLabel(item.value);
      comparison = item.previous === null ? "No prior timing" : `${item.change > 0 ? "+" : ""}${latencyLabel(item.change)} vs before`;
    }
    const title = item.path || `${item.site} error burst`;
    const dates = item.start ? `${item.start}${item.end !== item.start ? ` to ${item.end}` : ""}` : data.from;
    return `<article class="reliability-incident ${escapeHtml(item.kind)}"><div class="reliability-incident-top"><span class="reliability-badge">${escapeHtml(episodeLabel(item.kind))}</span><strong>${escapeHtml(value)}</strong></div><a href="${escapeHtml(pageHref)}">${escapeHtml(title)}</a><small>${escapeHtml(item.site)} · ${escapeHtml(dates)}</small><p>${escapeHtml(item.explanation)}</p><span class="reliability-comparison">${escapeHtml(comparison)}</span></article>`;
  }

  async function loadReliability() {
    clearError();
    try {
      const data = await api(`/api/reliability?${query({ limit: "100" })}`);
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      document.querySelector("#reliability-comparison").textContent = `vs ${dateLabel(data.previous_from, data.previous_to)}`;
      const summary = data.summary;
      document.querySelector("#reliability-heading").textContent = summary.incidents
        ? `${fmt.format(summary.incidents)} reliability signal${summary.incidents === 1 ? "" : "s"} to inspect`
        : "No material regression detected";
      document.querySelector("#reliability-narrative").textContent = data.narrative;
      const privacy = document.querySelector("#reliability-privacy");
      privacy.classList.toggle("hidden", !data.privacy.protected);
      privacy.textContent = data.privacy.protected ? data.narrative : "";
      document.querySelector("#timing-coverage").innerHTML = `<div><span style="width:${Math.min(100, summary.timing_coverage_percent)}%"></span></div><p><strong>${escapeHtml(`${summary.timing_coverage_percent}%`)}</strong> timing coverage · ${fmt.format(summary.timed_requests)} of ${fmt.format(summary.requests)} successful page responses</p>`;
      document.querySelector("#reliability-metrics").innerHTML = [
        metric("Average response", latencyLabel(summary.average_ms)),
        metric("p95 response", latencyLabel(summary.p95_ms)),
        metric("Slowest response", latencyLabel(summary.max_ms)),
        metric("Average payload", compactBytes(summary.average_bytes)),
        metric("Application 4xx", fmt.format(summary.app_4xx)),
        metric("Application 5xx", fmt.format(summary.app_5xx)),
        metric("Scanner requests", fmt.format(summary.scanner_requests)),
        metric("Signals", fmt.format(summary.incidents)),
      ].join("");
      updateChart("reliability-latency", document.querySelector("#latency-chart"), {
        type: "line",
        data: {
          labels: data.series.map(item => item.day),
          datasets: [
            { label: "p95 ms", data: data.series.map(item => item.p95_ms), borderColor: colors[3], backgroundColor: `${colors[3]}20`, tension: .25, fill: true },
            { label: "Average ms", data: data.series.map(item => item.average_ms), borderColor: colors[0], backgroundColor: "transparent", tension: .25 },
          ],
        },
        options: chartOptions,
      });
      updateChart("reliability-errors", document.querySelector("#reliability-error-chart"), {
        type: "bar",
        data: {
          labels: data.series.map(item => item.day),
          datasets: [
            { label: "Application 4xx", data: data.series.map(item => item.app_4xx), backgroundColor: "rgba(245,198,107,.72)", stack: "application" },
            { label: "Application 5xx", data: data.series.map(item => item.app_5xx), backgroundColor: "rgba(255,122,138,.8)", stack: "application" },
            { label: "Scanner noise", data: data.series.map(item => item.scanner_requests), backgroundColor: "rgba(143,162,186,.32)", stack: "scanner" },
          ],
        },
        options: chartOptions,
      });
      document.querySelector("#reliability-incidents").innerHTML = data.incidents.length
        ? data.incidents.map(item => reliabilityIncidentCard(item, data)).join("")
        : '<p class="empty">No latency regression, slow page, payload anomaly, or error burst crossed its evidence threshold.</p>';
      document.querySelector("#reliability-pages").innerHTML = data.pages.length
        ? data.pages.map(item => {
          const href = pageStoryHref(item, data.from, data.to);
          const change = item.latency_change_ms === null ? "—" : `${item.latency_change_ms > 0 ? "+" : ""}${latencyLabel(item.latency_change_ms)}`;
          return `<tr><td><a class="table-link" href="${escapeHtml(href)}">${escapeHtml(item.path)}</a></td><td>${escapeHtml(item.site)}</td><td>${fmt.format(item.timed_requests)} / ${fmt.format(item.requests)}</td><td>${escapeHtml(latencyLabel(item.average_ms))}</td><td>${escapeHtml(latencyLabel(item.p95_ms))}</td><td>${escapeHtml(latencyLabel(item.previous_p95_ms))}</td><td>${escapeHtml(compactBytes(item.average_bytes))}</td><td class="${item.latency_change_ms > 0 ? "danger-text" : "change-up"}">${escapeHtml(change)}</td></tr>`;
        }).join("")
        : '<tr><td colspan="8" class="empty">No successful human page responses in this window.</td></tr>';
      document.querySelector("#reliability-error-paths").innerHTML = data.error_paths.length
        ? data.error_paths.map(item => `<tr><td><a class="table-link" href="${escapeHtml(pageStoryHref(item, data.from, data.to))}">${escapeHtml(item.path)}</a></td><td>${escapeHtml(item.site)}</td><td>${fmt.format(item.status)}</td><td>${fmt.format(item.requests)}</td></tr>`).join("")
        : '<tr><td colspan="4" class="empty">No application error paths in this window.</td></tr>';
      document.querySelector("#scanner-summary").textContent = `${fmt.format(summary.scanner_requests)} scanner requests kept separate · ${fmt.format(data.probe_error_requests)} human-classified probe errors`;
    } catch (error) { showError(error); }
  }

  async function loadAiPolicy() {
    clearError();
    try {
      const data = await api(`/api/ai-policy?${query()}`);
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      document.querySelector("#policy-heading").textContent = data.totals.conflict_requests
        ? `${fmt.format(data.totals.conflict_requests)} policy conflict${data.totals.conflict_requests === 1 ? "" : "s"} to inspect`
        : "No observed policy conflicts";
      document.querySelector("#policy-narrative").textContent = data.narrative;
      document.querySelector("#policy-metrics").innerHTML = [
        metric("Sites checked", fmt.format(data.totals.sites)),
        metric("robots.txt available", fmt.format(data.totals.robots_available)),
        metric("AI requests observed", fmt.format(data.totals.observed_requests)),
        metric("Policy conflicts", fmt.format(data.totals.conflict_requests)),
        metric("AI agents observed", fmt.format(data.totals.agents_observed)),
        metric("Policies unavailable", fmt.format(data.totals.unavailable)),
      ].join("");
      const privacy = document.querySelector("#policy-privacy");
      privacy.classList.toggle("hidden", !data.privacy.protected);
      privacy.textContent = data.privacy.protected
        ? "Current public policy is shown, but this site's observed crawler paths remain private."
        : "";
      document.querySelector("#policy-sites").innerHTML = data.sites.map(site => {
        const status = site.robots_status === "available" ? "Published"
          : site.robots_status === "not_found" ? "No robots.txt" : "Unavailable";
        const posture = site.restricted_agents
          ? `${site.restricted_agents} AI agent${site.restricted_agents === 1 ? "" : "s"} restricted`
          : "No AI-specific restrictions detected";
        return `<article class="policy-site-card ${site.conflict_requests ? "conflict" : ""}"><div><span class="policy-status ${escapeHtml(site.robots_status)}">${escapeHtml(status)}</span><strong>${escapeHtml(site.site)}</strong></div><p>${escapeHtml(posture)}</p><small>${fmt.format(site.observed_requests)} observed requests · ${fmt.format(site.conflict_requests)} conflicts</small><a href="${escapeHtml(site.robots_url)}" target="_blank" rel="noopener noreferrer">Open robots.txt</a></article>`;
      }).join("");
      const visibleAgents = data.agents.filter(row =>
        row.observed_requests || row.policy_source === "explicit" || row.conflict_requests
      );
      document.querySelector("#policy-agents").innerHTML = visibleAgents.length
        ? visibleAgents.map(row => {
          const conflictPaths = row.conflicts.map(item => `${item.path} (${item.requests})`).join(", ");
          const rulePaths = row.disallow.join(", ");
          const pathText = conflictPaths || rulePaths || "—";
          return `<tr><td><strong>${escapeHtml(row.agent)}</strong><small>${escapeHtml(row.provider)} · ${escapeHtml(row.purpose)}</small></td><td>${escapeHtml(row.site)}</td><td><span class="policy-pill ${escapeHtml(row.policy)}">${escapeHtml(row.policy)}</span><small>${escapeHtml(row.policy_source)}</small></td><td>${fmt.format(row.observed_requests)}</td><td class="${row.conflict_requests ? "danger-text" : ""}">${fmt.format(row.conflict_requests)}</td><td class="path-cell">${escapeHtml(pathText)}</td></tr>`;
        }).join("")
        : '<tr><td colspan="6" class="empty">No explicit AI rules or observed AI requests in this scope.</td></tr>';
    } catch (error) { showError(error); }
  }

  function coverage(value) {
    return value === null || value === undefined ? "—" : `${value}%`;
  }

  async function loadContentObservatory() {
    clearError();
    try {
      const data = await api(`/api/content-observatory?${query({ limit: "150" })}`);
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      document.querySelector("#content-heading").textContent = data.totals.published_pages
        ? `${fmt.format(data.totals.published_pages)} published pages under observation`
        : "No sitemap inventory discovered";
      document.querySelector("#content-narrative").textContent = data.narrative;
      document.querySelector("#content-metrics").innerHTML = [
        metric("Published pages", fmt.format(data.totals.published_pages)),
        metric("Analyzed pages", fmt.format(data.totals.analyzed_pages)),
        metric("Dark matter", fmt.format(data.totals.dark_pages)),
        metric("Outside sitemaps", fmt.format(data.totals.off_sitemap_pages)),
        metric("Human coverage", coverage(data.totals.human_coverage_percent)),
        metric("Search coverage", coverage(data.totals.search_coverage_percent)),
        metric("AI coverage", coverage(data.totals.ai_coverage_percent)),
        metric("Policy collisions", fmt.format(data.totals.policy_conflicts)),
      ].join("");
      const privacy = document.querySelector("#content-privacy");
      privacy.classList.toggle("hidden", !data.privacy.protected);
      privacy.textContent = data.privacy.protected
        ? "The public sitemap inventory can be counted, but page-level attention and crawler coverage for this privacy-protected site are intentionally hidden."
        : "";
      document.querySelector("#content-sites").innerHTML = data.sites.map(site => {
        const status = site.sitemap_status === "available" ? "Sitemap found"
          : site.sitemap_status === "not_found" ? "No sitemap" : "Sitemap unavailable";
        const detail = site.protected
          ? "Activity protected"
          : `${fmt.format(site.dark_pages)} dark · ${fmt.format(site.off_sitemap_pages)} outside sitemap`;
        return `<article class="content-site-card ${site.sitemap_status}"><div><span class="content-status">${escapeHtml(status)}</span><strong>${escapeHtml(site.site)}</strong></div><div class="content-site-number">${fmt.format(site.published_pages)}<small>published pages</small></div><div class="coverage-trio"><span>Human <strong>${escapeHtml(coverage(site.human_coverage_percent))}</strong></span><span>Search <strong>${escapeHtml(coverage(site.search_coverage_percent))}</strong></span><span>AI <strong>${escapeHtml(coverage(site.ai_coverage_percent))}</strong></span></div><p>${escapeHtml(detail)} · ${fmt.format(site.sitemap_documents)} sitemap document${site.sitemap_documents === 1 ? "" : "s"}</p></article>`;
      }).join("");
      document.querySelector("#dark-matter").innerHTML = data.dark_matter.length
        ? data.dark_matter.map(item => {
          const params = new URLSearchParams({ from: data.from, to: data.to, bots: "0", assets: "0", path: item.path });
          const lastmod = item.lastmod ? ` · sitemap ${item.lastmod.slice(0, 10)}` : "";
          return `<article class="dark-card"><div class="dark-card-top"><span class="dark-badge ${escapeHtml(item.state)}">${escapeHtml(item.state)}</span><span>${fmt.format(item.human_requests)} human</span></div><a href="/site/${encodeURIComponent(item.site)}/page?${params}">${escapeHtml(item.path)}</a><small>${escapeHtml(item.site)}${escapeHtml(lastmod)}</small><p>${escapeHtml(item.explanation)}</p><div class="dark-card-signals"><span>${fmt.format(item.search_requests)} search</span><span>${fmt.format(item.ai_requests)} AI</span><span>${fmt.format(item.error_requests)} errors</span></div></article>`;
        }).join("")
        : '<p class="empty">No content dark matter in this scope and window.</p>';
      document.querySelector("#content-crawlers").innerHTML = data.crawlers.length
        ? data.crawlers.map(item => `<tr><td><strong>${escapeHtml(item.agent)}</strong><small>${escapeHtml(item.provider)} · ${escapeHtml(item.purpose)}</small></td><td>${escapeHtml(item.site)}</td><td><span class="crawler-kind ${escapeHtml(item.kind)}">${escapeHtml(item.kind)}</span></td><td>${fmt.format(item.requests)}</td><td>${fmt.format(item.sitemap_pages_seen)}</td><td>${escapeHtml(coverage(item.coverage_percent))}</td><td>${fmt.format(item.off_sitemap_pages)}</td><td class="${item.blocked_sitemap_pages ? "danger-text" : ""}">${fmt.format(item.blocked_sitemap_pages)}</td></tr>`).join("")
        : '<tr><td colspan="8" class="empty">No recognized search or AI crawler coverage yet.</td></tr>';
      document.querySelector("#off-sitemap").innerHTML = data.off_sitemap.length
        ? data.off_sitemap.map(item => {
          const href = pageStoryHref(item, data.from, data.to);
          return `<tr><td><a class="table-link" href="${escapeHtml(href)}">${escapeHtml(item.path)}</a></td><td>${escapeHtml(item.site)}</td><td>${fmt.format(item.requests)}</td><td>${escapeHtml(item.last_seen || "—")}</td></tr>`;
        }).join("")
        : '<tr><td colspan="4" class="empty">No active human pages sit outside the discovered sitemap inventory.</td></tr>';
      document.querySelector("#content-conflicts").innerHTML = data.policy_conflicts.length
        ? data.policy_conflicts.map(item => {
          const href = pageStoryHref(item, data.from, data.to);
          return `<tr><td><a class="table-link" href="${escapeHtml(href)}">${escapeHtml(item.path)}</a></td><td>${escapeHtml(item.site)}</td><td>${escapeHtml(item.agents.join(", "))}</td><td class="${item.observed_requests ? "danger-text" : ""}">${fmt.format(item.observed_requests)}</td></tr>`;
        }).join("")
        : '<tr><td colspan="4" class="empty">No sitemap pages conflict with current AI or search crawler rules.</td></tr>';
    } catch (error) { showError(error); }
  }

  function chronicleEventLabel(kind) {
    return ({
      baseline: "Baseline", published: "Published", updated: "Updated",
      republished: "Republished", redirected: "Redirected",
      disappeared: "Disappeared", sitemap_removed: "Left sitemap",
      robots_changed: "Robots changed",
      sitemap_status_changed: "Sitemap status",
    })[kind] || kind.replaceAll("_", " ");
  }

  async function loadChronicle() {
    clearError();
    try {
      const data = await api(`/api/chronicle?${query({ limit: "500" })}`);
      document.querySelector("#range-label").textContent = dateLabel(data.from, data.to);
      const stateTotal = Object.values(data.states).reduce((sum, value) => sum + value, 0);
      const meaningful = data.events.filter(item => item.kind !== "baseline").length;
      document.querySelector("#chronicle-heading").textContent = data.latest.length
        ? `${fmt.format(meaningful)} change${meaningful === 1 ? "" : "s"} in this window`
        : "Run the Chronicle observer to establish a baseline";
      document.querySelector("#chronicle-narrative").textContent = data.latest.length
        ? `Webstats remembers ${fmt.format(stateTotal)} discovered page${stateTotal === 1 ? "" : "s"} across ${fmt.format(data.latest.length)} observed site${data.latest.length === 1 ? "" : "s"}, and connects content changes with the crawlers that followed.`
        : "No persistent public-inventory observation has been recorded yet.";
      document.querySelector("#chronicle-metrics").innerHTML = [
        metric("Published now", fmt.format(data.states.published || 0)),
        metric("Redirected", fmt.format(data.states.redirected || 0)),
        metric("Disappeared", fmt.format(data.states.disappeared || 0)),
        metric("Unlisted", fmt.format(data.states.unlisted || 0)),
        metric("Events shown", fmt.format(data.events.length)),
        metric("Policy versions", fmt.format(data.policy_versions.length)),
      ].join("");
      document.querySelector("#chronicle-sites").innerHTML = data.latest.length
        ? data.latest.map(item => {
          const observed = new Date(item.captured_at * 1000).toLocaleString();
          const warning = item.error_count || item.limited;
          return `<article class="content-site-card ${warning ? "unavailable" : "available"}"><div><span class="content-status">${escapeHtml(item.sitemap_status)}</span><strong>${escapeHtml(item.site)}</strong></div><div class="content-site-number">${fmt.format(item.page_count)}<small>published pages</small></div><p>robots.txt ${escapeHtml(item.robots_status)} · observed ${escapeHtml(observed)}${item.limited ? " · bounded" : ""}</p></article>`;
        }).join("")
        : '<p class="empty">No site snapshots yet.</p>';
      document.querySelector("#chronicle-count").textContent = `${fmt.format(data.events.length)} result${data.events.length === 1 ? "" : "s"}${data.limited ? "+" : ""}`;
      document.querySelector("#chronicle-events").innerHTML = data.events.length
        ? data.events.map(item => {
          const reaction = item.crawler_reaction;
          const reactionText = reaction === null
            ? "Crawler reaction hidden by site privacy policy"
            : reaction && (reaction.ai_requests || reaction.search_requests)
              ? `${fmt.format(reaction.search_requests)} search · ${fmt.format(reaction.ai_requests)} AI crawler requests${reaction.first_day ? ` since ${reaction.first_day}` : ""}`
              : item.path ? "No recognized crawler reaction yet" : "Site-level change";
          const title = item.path
            ? `<a href="${escapeHtml(pageStoryHref(item, data.from, data.to))}">${escapeHtml(item.path)}</a>`
            : `<strong>${escapeHtml(item.site)}</strong>`;
          return `<li class="chronicle-event ${escapeHtml(item.kind)}"><span class="chronicle-marker"></span><div class="chronicle-event-body"><div class="chronicle-event-top"><span class="reliability-badge">${escapeHtml(chronicleEventLabel(item.kind))}</span><time>${escapeHtml(new Date(item.occurred_at * 1000).toLocaleString())}</time></div>${title}<small>${escapeHtml(item.site)}</small><p>${escapeHtml(item.summary)}</p><span class="chronicle-reaction">${escapeHtml(reactionText)}</span></div></li>`;
        }).join("")
        : '<li class="empty">No matching site changes in this range.</li>';
      document.querySelector("#chronicle-policies").innerHTML = data.policy_versions.length
        ? data.policy_versions.map(item => {
          const lines = item.robots_text ? item.robots_text.split(/\r?\n/).filter(Boolean).length : 0;
          return `<tr><td>${escapeHtml(new Date(item.captured_at * 1000).toLocaleString())}</td><td>${escapeHtml(item.site)}</td><td>${escapeHtml(item.robots_status)}</td><td><code>${escapeHtml(item.robots_digest.slice(0, 12))}</code></td><td>${fmt.format(lines)} non-empty line${lines === 1 ? "" : "s"}</td></tr>`;
        }).join("")
        : '<tr><td colspan="5" class="empty">No robots.txt versions archived yet.</td></tr>';
      const privacy = document.querySelector("#chronicle-privacy");
      const protectedScope = data.scope.site && data.privacy.protected_sites.includes(data.scope.site);
      privacy.classList.toggle("hidden", !protectedScope);
      if (protectedScope) privacy.textContent = data.privacy.note;
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
    setupSiteFilter(() => {
      const parameters = new URLSearchParams();
      if (scopeSite) parameters.set("site", scopeSite);
      window.history.pushState({}, "", `${window.location.pathname}${parameters.size ? `?${parameters}` : ""}`);
      loadLive();
    });
    window.addEventListener("popstate", () => {
      scopeSite = new URLSearchParams(window.location.search).get("site") || "";
      const select = document.querySelector("#site-filter");
      if (select) select.value = scopeSite;
      loadLive();
    });
    loadLive();
    window.setInterval(updateLiveAges, 1000);
    window.setInterval(() => {
      if (document.visibilityState === "visible") loadLive();
    }, 10000);
  }
  if (page === "ai-crawlers") {
    setupFilters(loadAiCrawlers);
    setupSiteFilter(() => { syncUrl(); loadAiCrawlers(); });
    loadAiCrawlers();
  }
  if (page === "ai-policy") {
    setupFilters(loadAiPolicy);
    setupSiteFilter(() => { syncUrl(); loadAiPolicy(); });
    loadAiPolicy();
  }
  if (page === "feed-readers") {
    setupFilters(loadFeedReaders);
    setupSiteFilter(() => { syncUrl(); loadFeedReaders(); });
    loadFeedReaders();
  }
  if (page === "almanac") { setupAlmanac(); loadAlmanac(); }
  if (page === "briefings") {
    setupBriefings();
    setupSiteFilter(() => loadBriefing(true));
    loadBriefing();
  }
  if (page === "changes") {
    setupFilters(loadChanges);
    setupSiteFilter(() => { syncUrl(); loadChanges(); });
    loadChanges();
  }
  if (page === "episodes") {
    setupFilters(loadEpisodes);
    setupSiteFilter(() => { syncUrl(); loadEpisodes(); });
    loadEpisodes();
  }
  if (page === "reliability") {
    setupFilters(loadReliability);
    setupSiteFilter(() => { syncUrl(); loadReliability(); });
    loadReliability();
  }
  if (page === "content-observatory") {
    setupFilters(loadContentObservatory);
    setupSiteFilter(() => { syncUrl(); loadContentObservatory(); });
    loadContentObservatory();
  }
  if (page === "chronicle") {
    setupFilters(loadChronicle);
    setupSiteFilter(() => { syncUrl(); loadChronicle(); });
    document.querySelector("#chronicle-kind")?.addEventListener("change", event => {
      chronicleKind = event.target.value;
      syncUrl();
      loadChronicle();
    });
    const runChronicleSearch = () => {
      chronicleQuery = document.querySelector("#chronicle-query")?.value.trim() || "";
      syncUrl();
      loadChronicle();
    };
    document.querySelector("#chronicle-search")?.addEventListener("click", runChronicleSearch);
    document.querySelector("#chronicle-query")?.addEventListener("keydown", event => {
      if (event.key === "Enter") runChronicleSearch();
    });
    loadChronicle();
  }
  if (page === "pulse") {
    setupSiteFilter(() => {
      const parameters = new URLSearchParams();
      if (scopeSite) parameters.set("site", scopeSite);
      window.history.pushState({}, "", `${window.location.pathname}${parameters.size ? `?${parameters}` : ""}`);
      loadPulse();
    });
    window.addEventListener("popstate", () => {
      scopeSite = new URLSearchParams(window.location.search).get("site") || "";
      const select = document.querySelector("#site-filter");
      if (select) select.value = scopeSite;
      loadPulse();
    });
    loadPulse();
  }
  if (page === "errors") { setupErrors(); loadErrors(); }
  if (page === "journeys") {
    setupFilters(loadJourneys);
    setupSiteFilter(() => { syncUrl(); loadJourneys(); });
    loadJourneys();
  }
  if (page === "link-atlas") {
    setupFilters(loadLinkAtlas);
    setupSiteFilter(() => {
      linkSource = "";
      syncUrl();
      loadLinkAtlas();
    });
    document.querySelector("#link-source")?.addEventListener("change", event => {
      linkSource = event.target.value;
      syncUrl();
      loadLinkAtlas();
    });
    document.querySelector("#clear-link-source")?.addEventListener("click", () => {
      linkSource = "";
      const select = document.querySelector("#link-source");
      if (select) select.value = "";
      syncUrl();
      loadLinkAtlas();
    });
    loadLinkAtlas();
  }
  if (page === "inbox") {
    setupFilters(loadInbox);
    setupSiteFilter(() => { syncUrl(); loadInbox(); });
    document.querySelector("#inbox-category")?.addEventListener("change", event => {
      inboxCategory = event.target.value;
      syncUrl();
      loadInbox();
    });
    loadInbox();
  } else {
    loadInboxBadge();
  }
  if (page === "galaxy") {
    setupFilters(loadGalaxy);
    setupSiteFilter(() => { syncUrl(); loadGalaxy(); });
    document.querySelectorAll("#galaxy-referrals, #galaxy-journeys, #galaxy-ai")
      .forEach(input => input.addEventListener("change", renderGalaxy));
    loadGalaxy();
  }
  if (page === "health") loadHealth();
})();
