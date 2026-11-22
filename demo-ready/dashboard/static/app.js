"use strict";

const byId = (id) => document.getElementById(id);
const moneyFormat = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" });
const signedFormat = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR", signDisplay: "exceptZero" });
const integerFormat = new Intl.NumberFormat("de-DE");
const percentageFormat = new Intl.NumberFormat("de-DE", { style: "percent", maximumFractionDigits: 1 });
const dateFormat = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Berlin" });
const hasNumber = (value) => typeof value === "number" && Number.isFinite(value);
const floorCent = (value) => Math.floor(value * 100 + 1e-8) / 100;
const money = (value, signed = false) => hasNumber(value) ? (signed ? signedFormat : moneyFormat).format(value) : "–";
const number = (value) => hasNumber(value) ? integerFormat.format(value) : "–";
const polarity = (value) => value > 0 ? "positive" : value < 0 ? "negative" : "";
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
const outcomeInfo = {
  accepted_fair: ["Angenommen · fair", "good"],
  accepted_over: ["Angenommen · überhöht", "bad"],
  accepted_unknown: ["Angenommen · Fairness offen", "neutral"],
  rejected_paid: ["Abgelehnt · mit Aufschlag", "caution"],
  rejected_zero: ["Abgelehnt · keine Zahlung", "neutral"],
};
const chargeInfo = {
  fair: ["Forderung belegt fair", "good"],
  over: ["Forderung belegt überhöht", "bad"],
  unknown: ["Fairness offen", "neutral"],
};
const pageSize = 50;

function storedTeam() {
  try { return localStorage.getItem("claim-to-fame.team") || "chineseModels"; }
  catch { return "chineseModels"; }
}

const state = {
  team: storedTeam(), overview: null, currentGame: null, detail: null,
  selectedLine: null, rows: [], page: 1, sort: "line", descending: false,
  followLatest: true, overviewPending: false, detailPending: null, timer: null,
  detailError: "", contextVersion: 0, scenario: null, scenarioPending: false, changes: {},
};

function timeLabel(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : dateFormat.format(date);
}

function interval(bounds, options = {}) {
  const { signed = false, exclusiveUpper = false, unknownZero = false } = options;
  const low = bounds?.lower;
  const high = bounds?.upper;
  if (!hasNumber(low) && !hasNumber(high)) return "Unbekannt";
  if (unknownZero && low === 0 && !hasNumber(high)) return "Unbekannt";
  if (hasNumber(low) && hasNumber(high) && low === high) return money(low, signed);
  if (!hasNumber(low)) return `${exclusiveUpper ? "Unter" : "Höchstens"} ${money(high, signed)}`;
  if (!hasNumber(high)) return `Mindestens ${money(low, signed)}`;
  const towards = exclusiveUpper ? " bis unter " : " bis ";
  return `${money(low, signed)}${towards}${money(high, signed)}`;
}

function badge(label, style) {
  return `<span class="badge badge-${style}">${escapeHtml(label)}</span>`;
}

function metric(label, value, { meta, accent, color } = {}) {
  return `<dl class="metric${accent ? " metric-accent" : ""}"><dt>${escapeHtml(label)}</dt><dd class="${color || ""}">${escapeHtml(value)}</dd>${meta ? `<dd class="metric-meta">${escapeHtml(meta)}</dd>` : ""}</dl>`;
}

function option(value, label) {
  return `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`;
}

function urlFor(path) {
  return `${path}?${new URLSearchParams({ team: state.team })}`;
}

async function request(path, options = {}) {
  const { timeout = 12000, ...settings } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(urlFor(path), { ...settings, cache: "no-store", signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Der lokale Server antwortet zu langsam.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function updateHealth(error) {
  const { updated_at: updatedAt, fetching, error: sourceError, offline } = state.overview || {};
  const problem = error || sourceError;
  const status = byId("connection");
  status.dataset.state = problem ? "stale" : updatedAt ? "ok" : "loading";
  status.textContent = problem ? "Abruf gestört" : offline ? "Lokaler Cache" : fetching ? "Synchronisiere …" : updatedAt ? "Daten verfügbar" : "Warte auf Daten";
  const notice = byId("notice");
  notice.hidden = !problem;
  notice.textContent = problem ? `Datenabruf gestört. Bereits geladene Ergebnisse bleiben sichtbar. ${problem}` : "";
}

function availableForTeam(game) {
  return !game.ready || hasNumber(game.net);
}

function renderOverview() {
  const overview = state.overview;
  renderDemo(overview.demo);
  const games = overview.games || [];
  const ready = games.filter((game) => game.ready);
  const withNet = ready.filter((game) => hasNumber(game.net));
  const latest = games.find(availableForTeam);
  const loadedTransactions = ready.reduce((sum, game) => sum + (game.transaction_count || 0), 0);
  const net = withNet.reduce((sum, game) => sum + game.net, 0);
  const liveAt = timeLabel(overview.updated_at);
  byId("overview-context").textContent = `${state.team} · ${liveAt ? `Abrufstand ${liveAt} Uhr` : "Abgerechnete Runden"}`;
  const summary = byId("summary");
  summary.hidden = false;
  summary.innerHTML = [
    metric("Netto gesamt", withNet.length ? money(net, true) : "–", { accent: true, color: polarity(net), meta: `${number(withNet.length)} Runden mit Saldo` }),
    metric("Abgerechnete Runden", number(games.length)),
    metric("Geladene Runden", number(ready.length), { meta: ready.length < games.length ? "Weitere Daten werden abgerufen" : "Rundendetails verfügbar" }),
    metric("Transaktionen", number(loadedTransactions), { meta: "Alle Teams in geladenen Runden" }),
  ].join("");
  const knownTeams = [...new Set([state.team, ...(overview.teams || [])])].sort((a, b) => a.localeCompare(b, "de"));
  const teamPicker = byId("team");
  const teamOptions = knownTeams.map((team) => option(team, team)).join("");
  if (teamPicker.dataset.options !== teamOptions) {
    teamPicker.innerHTML = teamOptions;
    teamPicker.dataset.options = teamOptions;
  }
  teamPicker.value = state.team;
  teamPicker.disabled = knownTeams.length < 2;
  if (state.followLatest) {
    const latestReady = withNet[0] || latest;
    if (latestReady && state.currentGame !== latestReady.id) selectGame(latestReady.id);
  }
  renderGamePicker(games);
  if (latest && state.currentGame === null) selectGame(latest.id);
  const activeGame = games.find((game) => game.id === state.currentGame);
  if (activeGame?.ready && state.detailPending === null && (!state.detail || state.detail.complete === false && activeGame.transaction_count !== state.detail.transaction_count)) loadGame();
  if (state.currentGame === null) showEmpty();
  updateHealth();
}

function renderGamePicker(games = state.overview?.games || []) {
  const options = games.map((game) => `<option value="${escapeHtml(game.id)}"${availableForTeam(game) ? "" : " disabled"}>Spiel ${escapeHtml(game.id)}${game.ready ? (availableForTeam(game) ? "" : " · ohne Teilnahme") : " · lädt"}</option>`).join("");
  const picker = byId("round-select");
  if (picker.dataset.options !== options) {
    picker.innerHTML = options || option("", "Keine abgerechnete Runde");
    picker.dataset.options = options;
  }
  picker.disabled = !games.some(availableForTeam);
  picker.value = String(state.currentGame ?? "");
  const gameList = byId("games");
  const gameRows = games.map((game) => {
    const active = game.id === state.currentGame;
    const meta = timeLabel(game.start_time);
    const available = availableForTeam(game);
    const roundStatus = !available ? "Team hat nicht teilgenommen" : game.ready ? (meta ? `${meta} Uhr` : "Daten verfügbar") : "Daten werden abgerufen";
    return `<button class="round-button" type="button" data-game="${escapeHtml(game.id)}" ${active ? 'aria-current="true"' : ""} ${available ? "" : "disabled"}><strong>Spiel ${escapeHtml(game.id)}</strong><span class="round-net ${polarity(game.net)}">${hasNumber(game.net) ? escapeHtml(money(game.net, true)) : "–"}</span><small>${escapeHtml(roundStatus)}</small></button>`;
  }).join("");
  if (gameList.dataset.rows !== gameRows) {
    const focusedId = gameList.contains(document.activeElement) ? document.activeElement.dataset.game : undefined;
    gameList.innerHTML = gameRows;
    gameList.dataset.rows = gameRows;
    if (focusedId) Array.from(gameList.children).find((button) => button.dataset.game === focusedId)?.focus({ preventScroll: true });
  }
}

async function refreshOverview() {
  if (state.overviewPending) return;
  clearTimeout(state.timer);
  state.overviewPending = true;
  const requestedTeam = state.team;
  byId("refresh").disabled = true;
  try {
    const overview = await request("/api/overview");
    if (state.team !== requestedTeam) return;
    state.overview = overview;
    renderOverview();
  } catch (error) {
    if (state.team !== requestedTeam) return;
    updateHealth(error.message);
    byId("overview-context").textContent = `Letzter Abruf für ${requestedTeam} fehlgeschlagen`;
    if (!state.detail) showEmpty();
  } finally {
    state.overviewPending = false;
    byId("refresh").disabled = false;
    const intervalSeconds = Math.max(5, state.overview?.refresh_seconds || 25);
    state.timer = setTimeout(refreshOverview, state.team === requestedTeam ? intervalSeconds * 1000 : 0);
  }
}

function showEmpty() {
  const selected = state.overview?.games?.find((game) => game.id === state.currentGame);
  const empty = byId("overview-empty");
  empty.hidden = false;
  const noParticipation = state.overview?.games?.length > 0 && !state.overview.games.some(availableForTeam);
  empty.textContent = state.detailError || (selected ? `Spiel ${selected.id}: ${selected.ready ? "Details werden geladen." : "Transaktionen werden abgerufen."}` : noParticipation ? "Für dieses Team wurden keine Teilnahmen gefunden." : "Warte auf abgerechnete Runden.");
  byId("round").hidden = true;
}

function selectGame(id, manual = false) {
  if (manual) {
    const latestReady = state.overview?.games?.find((game) => game.ready && availableForTeam(game));
    state.followLatest = id === (latestReady || state.overview?.games?.find(availableForTeam))?.id;
  }
  if (state.currentGame === id && (state.detail || state.detailPending !== null)) return;
  state.currentGame = id;
  state.detail = null;
  state.detailError = "";
  state.selectedLine = null;
  state.page = 1;
  state.sort = "line";
  state.descending = false;
  state.contextVersion += 1;
  state.detailPending = null;
  state.scenarioPending = false;
  state.scenario = null;
  state.changes = {};
  showEmpty();
  renderGamePicker();
  if (id !== null) loadGame();
}

async function loadGame() {
  const game = state.overview?.games?.find((item) => item.id === state.currentGame);
  if (!game?.ready || !availableForTeam(game) || state.detailPending !== null) return;
  const version = state.contextVersion;
  state.detailPending = version;
  try {
    const detail = await request(`/api/games/${state.currentGame}`);
    if (state.contextVersion !== version) return;
    state.detail = detail;
    state.detailError = "";
    renderGame();
  } catch (error) {
    if (state.contextVersion !== version) return;
    state.detailError = `Spiel ${game.id}: ${error.message}`;
    showEmpty();
  } finally {
    if (state.detailPending === version) state.detailPending = null;
  }
}

function renderGame() {
  const detail = state.detail;
  renderDemoRound(detail.demo_replay);
  const { summary = {}, lines = [], complete, transaction_count: count, team } = detail;
  const { income, paid, net, review_penalty: penalty, accepted_overpay: overpay } = summary;
  byId("overview-empty").hidden = true;
  byId("round").hidden = false;
  byId("round-title").textContent = `Spiel ${state.currentGame}`;
  byId("round-context").textContent = `${team} · ${number(lines.length)} Positionen · ${number(count)} Transaktionen${complete === false ? " · Teilstand" : ""}`;
  byId("round-summary").innerHTML = [
    metric("Rundensaldo", money(net, true), { accent: true, color: polarity(net) }),
    metric("Erhalten", money(income)),
    metric("Gezahlt", money(paid)),
    metric("Aufschlag bei Fehlablehnungen", money(penalty), { meta: `Überhöht akzeptiert: ${interval(overpay)}` }),
  ].join("");
  byId("position").innerHTML = lines.map((line) => option(line.index, `Pos. ${line.index}${line.recommendation ? " · Vorschlag" : ""}`)).join("");
  byId("position").disabled = !lines.length;
  byId("line-filter").innerHTML = option("all", "Alle Positionen") + lines.map((line) => option(line.index, `Pos. ${line.index}`)).join("");
  state.rows = lines.flatMap((line) => (line.transactions || []).map((row) => ({ ...row, line: line.index })));
  const attention = (line) => Math.abs(line.summary?.net || 0) + (line.summary?.review_penalty || 0) + (line.summary?.accepted_overpay?.lower || 0);
  if (!lines.some((line) => line.index === state.selectedLine)) {
    const opportunity = (line) => line.recommendation?.delta?.lower || 0;
    state.selectedLine = lines.reduce((best, line) => {
      if (!best || opportunity(line) > opportunity(best)) return line;
      return opportunity(line) === opportunity(best) && attention(line) > attention(best) ? line : best;
    }, null)?.index ?? null;
  }
  renderPosition();
  renderTransactions();
}

function rangeChart(bounds, a, b) {
  if (!bounds || bounds.lower === 0 && !hasNumber(bounds.upper) || !hasNumber(bounds.lower) && !hasNumber(bounds.upper)) return "";
  const maximum = Math.max(bounds.upper || 0, bounds.lower || 0, a || 0, b || 0, 1) * 1.12;
  const percent = (value) => Math.min(100, Math.max(0, value / maximum * 100)).toFixed(2);
  const start = percent(bounds.lower || 0);
  const end = hasNumber(bounds.upper) ? percent(bounds.upper) : "100";
  const mark = (value, kind) => hasNumber(value) ? `<i class="range-marker" data-kind="${kind}" style="left:${percent(value)}%"></i>` : "";
  return `<div class="range-chart" aria-hidden="true"><div class="range-track"><span class="range-fill" data-open="${!hasNumber(bounds.upper)}" style="left:${start}%;right:${(100 - Number(end)).toFixed(2)}%"></span>${mark(a, "a")}${mark(b, "b")}</div><div class="range-labels"><span>${money(0)}</span><span>${money(Math.ceil(maximum))}</span></div><div class="range-legend"><span><i></i> Belegtes Intervall t</span>${hasNumber(a) ? '<span><i class="legend-a"></i> Forderung a</span>' : ""}${hasNumber(b) ? '<span><i class="legend-b"></i> Limit b</span>' : ""}</div></div>`;
}

function selectedLine() {
  return state.detail?.lines?.find((line) => line.index === state.selectedLine);
}

function renderPosition() {
  const line = selectedLine();
  const insights = byId("position-insights");
  const inputA = byId("scenario-a");
  const inputB = byId("scenario-b");
  byId("position").value = String(state.selectedLine ?? "");
  inputA.disabled = !line;
  inputB.disabled = !line;
  byId("simulate").disabled = !line || state.scenarioPending;
  if (!line) {
    insights.textContent = "Für diese Runde sind keine Positionsdaten verfügbar.";
    renderScenario();
    return;
  }
  const own = line.own || {};
  const t = line.t;
  const status = chargeInfo[own.charge_status] || chargeInfo.unknown;
  const actualA = hasNumber(own.a) ? money(own.a) : interval(own.a_bounds, { unknownZero: true });
  const actualB = hasNumber(own.b) ? money(own.b) : interval(own.b_bounds, { exclusiveUpper: true, unknownZero: true });
  const distance = interval(own.gap, { signed: true });
  const estimate = own.estimate;
  const estimateDetail = hasNumber(estimate?.m) ? `<span>Modellwert ${money(estimate.m)}${hasNumber(estimate.p) ? ` · Deckung ${percentageFormat.format(estimate.p)}` : ""}</span>` : "";
  const source = { runner: "Werte aus dem Runner-Log", public: "Werte aus Zahlungen abgeleitet", legacy: "Historisches Runner-Log, ungeprüft" }[own.source];
  const legacy = own.legacy;
  const legacyDetail = legacy ? `<p class="position-explanation muted">Historischer Logkandidat: ${money(legacy.a)} Forderung · ${money(legacy.b)} Limit</p>` : "";
  insights.innerHTML = `
    <div class="position-value"><var>t</var> · ${escapeHtml(interval(t, { exclusiveUpper: true, unknownZero: true }))}</div>
    <p class="position-explanation muted">${escapeHtml(line.inference_error ? "Für diese Position lässt sich keine belastbare Grenze ableiten." : (line.description || "Grenzen aus den veröffentlichten Annahmen, Ablehnungen und Zahlungen."))}</p>
    ${rangeChart(t, own.a, own.b)}
    <dl class="position-grid">
      <div><dt>Unsere Forderung <var>a</var></dt><dd>${escapeHtml(actualA)}</dd></div>
      <div><dt>Unser Annahmelimit <var>b</var></dt><dd>${escapeHtml(actualB)}</dd></div>
      <div><dt>Abstand <var>a</var> − <var>t</var></dt><dd title="Negative Werte bedeuten eine Forderung unter der fairen Grenze.">${escapeHtml(distance)}</dd></div>
    </dl>
    <div class="position-footer muted">${badge(...status)}${estimateDetail}${source ? `<span>${escapeHtml(source)}</span>` : ""}</div>
    ${legacyDetail}
    ${own.log_warning ? `<p class="position-explanation caution">${escapeHtml(own.log_warning)}</p>` : ""}`;
  const draft = state.changes[line.index] || {};
  inputA.value = hasNumber(draft.a) ? draft.a : "";
  inputB.value = hasNumber(draft.b) ? draft.b : "";
  inputA.placeholder = hasNumber(own.a) ? `Ist ${money(own.a)}` : "Unverändert";
  inputB.placeholder = hasNumber(own.b) ? `Ist ${money(own.b)}` : "Unverändert";
  renderScenario();
}

function renderScenario() {
  const result = byId("scenario-result");
  const scenario = state.scenario;
  const line = selectedLine();
  const presets = byId("scenario-presets");
  const lower = hasNumber(line?.t?.lower) ? floorCent(line.t.lower) : null;
  const recommendation = line?.recommendation;
  const newPreset = (type, label) => `<button type="button" data-preset="${type}" ${state.scenarioPending ? "disabled" : ""}>${escapeHtml(label)}</button>`;
  presets.innerHTML = (recommendation && (hasNumber(recommendation.a) || hasNumber(recommendation.b)) ? newPreset("recommended", `Berechneten Vorschlag prüfen${recommendation.delta ? ` · ${interval(recommendation.delta, { signed: true })}` : ""}`) : "") + (hasNumber(lower) && lower > 0 && !line?.inference_error ? newPreset("a", `a = ${money(lower)} testen`) + newPreset("b", `b = ${money(lower)} testen`) : "");
  byId("reset-scenario").disabled = state.scenarioPending;
  if (state.scenarioPending) {
    result.textContent = "Szenario wird berechnet …";
    return;
  }
  if (!scenario) {
    result.textContent = "";
    return;
  }
  if (scenario.available === false) {
    result.innerHTML = `<p class="caution">Simulation nicht verfügbar. ${escapeHtml(scenario.reason || "Für diese Änderung fehlt belastbare Evidenz.")}</p>`;
    return;
  }
  const simulated = interval(scenario, { signed: true });
  const change = interval(scenario.delta, { signed: true });
  const scenarioLine = scenario.lines?.find((item) => item.index === state.selectedLine);
  const altered = Object.keys(state.changes).length;
  result.innerHTML = `<div class="result-box"><p><strong>Simulierter Rundensaldo</strong> · ${number(altered)} ${altered === 1 ? "Position geändert" : "Positionen geändert"}</p><p class="result-amount">${escapeHtml(simulated)}</p><p class="result-detail">Original ${escapeHtml(money(scenario.actual_net, true))} · Differenz ${escapeHtml(change)}</p>${scenarioLine ? `<p class="result-detail">Pos. ${escapeHtml(scenarioLine.index)} · Differenz ${escapeHtml(interval(scenarioLine.delta, { signed: true }))}</p>` : ""}${scenario.exact ? "" : '<p class="result-detail">Die Spanne berücksichtigt unbekannte Preise, Limits und Grenzen.</p>'}</div>`;
}

async function simulate(event) {
  event.preventDefault();
  if (state.scenarioPending || !selectedLine()) return;
  state.followLatest = false;
  const change = {};
  const a = byId("scenario-a").value;
  const b = byId("scenario-b").value;
  if (a !== "") change.a = Number(a);
  if (b !== "") change.b = Number(b);
  const next = { ...state.changes };
  if (Object.keys(change).length) next[state.selectedLine] = change;
  else delete next[state.selectedLine];
  const version = state.contextVersion;
  state.scenarioPending = true;
  byId("simulate").disabled = true;
  renderScenario();
  try {
    const scenario = await request(`/api/games/${state.currentGame}/simulate`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ changes: next }),
    });
    if (state.contextVersion !== version) return;
    state.changes = next;
    state.scenario = scenario;
  } catch (error) {
    if (state.contextVersion !== version) return;
    state.scenario = { available: false, reason: error.message };
  } finally {
    if (state.contextVersion === version) {
      state.scenarioPending = false;
      byId("simulate").disabled = !selectedLine();
      renderScenario();
    }
  }
}

function renderTransactions() {
  const role = byId("role-filter").value;
  const lineFilter = byId("line-filter").value;
  const outcome = byId("outcome-filter").value;
  const search = byId("search").value.trim().toLocaleLowerCase("de");
  const { sort, descending } = state;
  const directionForRole = { issuer: "outgoing", reviewer: "incoming" };
  const matches = state.rows.filter((row) => {
    const selectedTeam = role === "all" || (role === "mine" ? row.direction !== "other" : row.direction === directionForRole[role]);
    const selectedOutcome = outcome === "all" || (outcome === "accepted" ? row.accepted : outcome === "miss" ? ["rejected_paid", "accepted_over"].includes(row.outcome) : row.outcome === outcome);
    const selectedPosition = lineFilter === "all" || String(row.line) === lineFilter;
    const containsText = !search || `${row.issuer} ${row.reviewer}`.toLocaleLowerCase("de").includes(search);
    return selectedTeam && selectedOutcome && selectedPosition && containsText;
  }).sort((lhs, rhs) => {
    const order = ["issuer", "reviewer"].includes(sort) ? String(lhs[sort]).localeCompare(String(rhs[sort]), "de") : lhs[sort] - rhs[sort];
    return descending ? -order : order;
  });

  const pages = Math.max(1, Math.ceil(matches.length / pageSize));
  state.page = Math.min(state.page, pages);
  const visible = matches.slice((state.page - 1) * pageSize, state.page * pageSize);
  byId("transaction-count").textContent = `${number(matches.length)} von ${number(state.rows.length)} Transaktionen`;
  byId("transactions").innerHTML = visible.length ? visible.map((row) => {
    const status = outcomeInfo[row.outcome] || (row.accepted ? outcomeInfo.accepted_unknown : ["Abgelehnt", "neutral"]);
    const incoming = row.direction === "incoming";
    const outgoing = row.direction === "outgoing";
    const delta = outgoing ? row.amount : incoming ? -row.amount : null;
    const charge = interval(row.charge, { unknownZero: true });
    const limit = interval(row.acceptance_limit, { exclusiveUpper: true, unknownZero: true });
    return `<tr><td>${escapeHtml(row.line)}</td><td${outgoing ? ' class="own-name"' : ""}>${escapeHtml(row.issuer)}</td><td${incoming ? ' class="own-name"' : ""}>${escapeHtml(row.reviewer)}</td><td class="numeric">${escapeHtml(charge)}</td><td class="numeric">${escapeHtml(limit)}</td><td>${badge(...status)}</td><td class="numeric">${money(row.amount)}</td><td class="numeric ${polarity(delta)}">${money(delta, true)}</td></tr>`;
  }).join("") : '<tr><td colspan="8">Keine Transaktionen für diese Filter.</td></tr>';
  document.querySelectorAll("[data-sort]").forEach((button) => {
    const header = button.closest("th");
    if (button.dataset.sort === sort) header.setAttribute("aria-sort", descending ? "descending" : "ascending");
    else header.removeAttribute("aria-sort");
  });
  byId("pagination").hidden = pages <= 1;
  byId("page-status").textContent = `Seite ${number(state.page)} von ${number(pages)}`;
  byId("previous-page").disabled = state.page === 1;
  byId("next-page").disabled = state.page >= pages;
}

byId("refresh").addEventListener("click", refreshOverview);
byId("team").addEventListener("change", (event) => {
  state.team = event.target.value;
  try { localStorage.setItem("claim-to-fame.team", state.team); } catch { /* Private browsing may block storage. */ }
  state.overview = null;
  state.followLatest = true;
  byId("summary").hidden = true;
  selectGame(null);
  refreshOverview();
});
byId("games").addEventListener("click", (event) => {
  const button = event.target.closest("[data-game]");
  if (button) selectGame(Number(button.dataset.game), true);
});
byId("round-select").addEventListener("change", (event) => selectGame(Number(event.target.value), true));
byId("position").addEventListener("change", (event) => {
  state.selectedLine = Number(event.target.value);
  renderPosition();
});
byId("scenario-form").addEventListener("submit", simulate);
byId("scenario-presets").addEventListener("click", (event) => {
  const preset = event.target.closest("[data-preset]")?.dataset.preset;
  const line = selectedLine();
  if (!preset || !line) return;
  if (preset === "recommended") {
    const { a, b } = line.recommendation;
    byId("scenario-a").value = hasNumber(a) ? a : "";
    byId("scenario-b").value = hasNumber(b) ? b : "";
  } else if (hasNumber(line.t?.lower)) {
    byId(`scenario-${preset}`).value = floorCent(line.t.lower);
  }
  byId("scenario-form").requestSubmit();
});
for (const id of ["scenario-a", "scenario-b"]) {
  byId(id).addEventListener("input", () => { state.followLatest = false; });
}
byId("reset-scenario").addEventListener("click", () => {
  state.changes = {};
  state.scenario = null;
  renderPosition();
});
for (const name of ["role-filter", "line-filter", "outcome-filter", "search"]) {
  byId(name).addEventListener(name === "search" ? "input" : "change", () => {
    state.page = 1;
    renderTransactions();
  });
}
document.querySelectorAll("[data-sort]").forEach((button) => {
  button.addEventListener("click", () => {
    state.descending = state.sort === button.dataset.sort ? !state.descending : button.dataset.sort === "amount";
    state.sort = button.dataset.sort;
    state.page = 1;
    renderTransactions();
  });
});
byId("previous-page").addEventListener("click", () => { state.page -= 1; renderTransactions(); });
byId("next-page").addEventListener("click", () => { state.page += 1; renderTransactions(); });

refreshOverview();


function renderDemo(demo) {
  const panel = byId("demo-panel");
  panel.hidden = !demo;
  if (!demo) return;
  const completed = demo.results.filter(r => r.status === "model_completed").length;
  const rules = demo.results.filter(r => r.status === "rules_only").length;
  panel.innerHTML = `<h2>Demo replay · ${escapeHtml(demo.state)}</h2>
    <p>${escapeHtml(demo.mode)}. Retrospective assessment, never submitted.</p>
    <p>${completed} model results · ${rules} rules-only results · ${demo.results.length - completed - rules} model fallbacks.
    ${demo.current_game ? `Processing game ${escapeHtml(demo.current_game)}…` : ""}</p>
    <p>${demo.missing_cases.length ? `Missing decrypted cases: ${escapeHtml(demo.missing_cases.join(", "))}` : ""}
    ${Object.keys(demo.errors).length ? `Failed games: ${escapeHtml(Object.keys(demo.errors).join(", "))}` : ""}
    ${demo.error ? escapeHtml(demo.error) : ""}</p>
    <p>${demo.enabled ? "Newly completed rounds are picked up while online. Select a round to compare its replay." : "Start with --replay to assess cases with the configured models."}</p>`;
  const current = demo.results.find(r => r.game === state.currentGame);
  if (current && state.detail && !state.detail.demo_replay && state.detailPending === null) loadGame();
}

function renderDemoRound(replay) {
  const panel = byId("demo-round");
  panel.hidden = !replay;
  if (!replay) return;
  const sim = replay.simulation;
  panel.innerHTML = `<h3>Latest-model replay · ${escapeHtml(replay.status)}</h3>
    <p>History: ${escapeHtml(replay.history?.mode || "none")} · earlier games: ${escapeHtml((replay.history?.evidence_games || []).join(", ") || "none")}
    · ${escapeHtml(replay.history?.memory_entries || 0)} comparable records.</p>
    <p>${replay.history?.missing_prior_cases?.length ? `Earlier cases missing: ${escapeHtml(replay.history.missing_prior_cases.join(", "))}. Recall is partial. ` : ""}
    ${replay.history?.excluded_original_records?.length ? `${replay.history.excluded_original_records.length} original records excluded from learning. ` : ""}
    ${replay.history?.missing_original_logs?.length ? `Original logs missing: ${escapeHtml(replay.history.missing_original_logs.join(", "))}. Reconstruction is partial.` : ""}</p>
    <p>${escapeHtml(Object.entries(replay.models).map(([tier, model]) => `${tier}: ${model}`).join(" · "))}
    · ${escapeHtml(replay.seconds)} seconds</p>
    <p>${sim.available ? `Hypothetical net: ${escapeHtml(interval(sim))}. Difference vs actual: ${escapeHtml(interval(sim.delta, {signed:true}))}.` : escapeHtml(sim.reason)}</p>
    <p>Same opponents; bounds reflect unknown prices and limits. This is not a competition result or independent holdout.</p>
    <details><summary>Replay prices by position</summary><ul>${Object.entries(replay.prices).map(([i, pair]) =>
      `<li>Position ${escapeHtml(i)}: a ${escapeHtml(money(pair[0]))} · b ${escapeHtml(money(pair[1]))}</li>`).join("")}</ul></details>`;
}
