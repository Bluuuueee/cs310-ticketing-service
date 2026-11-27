"use strict";

const apiBase = new URL(".", document.currentScript.src);

const byId = (id) => document.getElementById(id);
const tabs = [...document.querySelectorAll("[data-panel]")];
const eventForm = byId("event-form");
const eventInput = byId("event");
const checkoutStatus = byId("checkout-status");
const scanStatus = byId("scan-status");
const buyer = sessionStorage.getItem("ticketing.buyer") || `demo-${crypto.randomUUID()}`;
sessionStorage.setItem("ticketing.buyer", buyer);

let eventId;
let state = {};
let searchRequest = 0;
let busy = false;
let serverOffset = 0;
let currentSeats = [];
let seatRequest = 0;
let statsRequest = 0;
let suggestions = null;
let suggestionIndex = 0;
let emailEnabled = false;

function readState(id) {
  try {
    return JSON.parse(sessionStorage.getItem(`ticketing.order.${id}`)) || {};
  } catch {
    return {};
  }
}

function persist() {
  sessionStorage.setItem(`ticketing.order.${eventId}`, JSON.stringify(state));
}

function setStatus(element, message, isError = false) {
  element.textContent = message;
  element.hidden = !message;
  element.classList.toggle("error", isError);
}

async function api(path, body, key) {
  const options = {signal: AbortSignal.timeout(15000)};
  if (body !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type": "application/json", "X-Buyer-Id": buyer};
    if (key) options.headers["Idempotency-Key"] = key;
    options.body = JSON.stringify(body);
  }
  const result = await fetch(new URL(path.slice(1), apiBase), options);
  const data = await result.json();
  if (!result.ok) {
    const error = new Error(data.message || `HTTP ${result.status}`);
    error.status = result.status;
    throw error;
  }
  return data;
}

function eventPath(path) {
  return `/events/${eventId}/${path}`;
}

function remainingSeconds() {
  if (!state.hold) return 0;
  return Math.max(0, Math.ceil((state.hold.expires_at_ms - Date.now() - serverOffset) / 1000));
}

function activeHold() {
  return !state.order && remainingSeconds() > 0;
}

function rankSeatGroups(seats, size, width) {
  const rows = new Map();
  for (const seat of seats) {
    if (!rows.has(seat.row)) rows.set(seat.row, []);
    rows.get(seat.row).push(seat);
  }
  const groups = [];
  for (const [letter, row] of rows) {
    row.sort((a, b) => a.seat_num - b.seat_num);
    const rowIndex = letter.charCodeAt(0) - "A".charCodeAt(0);
    for (let start = 0; start <= row.length - size; start++) {
      const block = row.slice(start, start + size);
      const left = block[0].seat_num;
      if (!block.every((seat, index) => seat.status === "available" && seat.seat_num === left + index)) {
        continue;
      }
      // Scale the API's score by four to preserve exact ties.
      const score = 4 * rowIndex * size + block.reduce(
        (sum, seat) => sum + Math.abs(2 * seat.seat_num - (width + 1)), 0,
      );
      groups.push({score, rowIndex, left, ids: block.map((seat) => seat.seat_id)});
    }
  }
  groups.sort((a, b) => a.score - b.score || a.rowIndex - b.rowIndex || a.left - b.left);
  return groups.map((group) => group.ids);
}

function currentSuggestion() {
  return suggestions?.[suggestionIndex];
}

function clearSuggestions() {
  suggestions = null;
  suggestionIndex = 0;
  renderSeats();
  updateControls();
}

function updateControls() {
  const seconds = remainingSeconds();
  const waiting = state.key && state.order?.status !== "confirmed" && state.order?.status !== "failed";
  byId("hold-button").disabled = busy || Boolean(state.key || activeHold());
  byId("party-size").disabled = byId("hold-button").disabled;
  const selected = currentSuggestion();
  byId("seat-options").hidden = suggestions === null || Boolean(state.key || activeHold());
  const optionLabel = byId("option-label");
  const optionText = selected
    ? `Option ${suggestionIndex + 1} of ${suggestions.length}: ${selected.join(", ")}`
    : "No adjacent seats available for this group size.";
  if (optionLabel.textContent !== optionText) optionLabel.textContent = optionText;
  byId("option-navigation").hidden = !selected;
  byId("hold-option").hidden = !selected;
  byId("previous-option").disabled = busy || !selected || suggestionIndex === 0;
  byId("next-option").disabled = busy || !selected || suggestionIndex >= suggestions.length - 1;
  byId("hold-option").disabled = busy || !selected || Boolean(state.key || activeHold());
  byId("refresh-seats").disabled = busy;
  byId("hold-details").hidden = !state.hold || Boolean(state.order?.status === "confirmed" || state.order?.status === "failed");
  byId("held-seats").textContent = state.hold?.seats.join(", ") || "";
  byId("countdown").textContent = seconds ? `${seconds}s remaining` : "Hold expired";
  byId("checkout-button").disabled = busy || !seconds || Boolean(state.order);
  byId("receipt-field").hidden = !emailEnabled;
  byId("receipt-email").disabled = busy || Boolean(state.order);
  byId("checkout-button").textContent = state.key ? "Retry checkout" : "Submit checkout";
  byId("resume-order").hidden = busy || !waiting;
  byId("next-order").hidden = busy || !(state.order?.status === "confirmed" ||
    state.order?.status === "failed" || (state.hold && !seconds && !state.order));
  byId("back-to-search").disabled = busy;
}

function renderSeats() {
  const map = byId("seat-map");
  const preview = new Set(currentSuggestion());
  const rows = new Map();
  for (const seat of currentSeats) {
    if (!rows.has(seat.row)) rows.set(seat.row, []);
    rows.get(seat.row).push(seat);
  }
  const elements = [];
  for (const [row, seats] of rows) {
    const line = document.createElement("div");
    line.className = "seat-row";
    const label = document.createElement("span");
    label.className = "row-label";
    label.textContent = row;
    line.append(label);
    for (const seat of seats) {
      const cell = document.createElement("span");
      const own = seat.status === "held" && seat.holder === buyer;
      const isPreview = preview.has(seat.seat_id) && seat.status === "available";
      const category = own ? "own" : isPreview ? "preview" : seat.status;
      const stateLabel = own ? "your hold" : category;
      cell.className = `seat ${category}`;
      cell.textContent = seat.seat_num;
      cell.title = `${seat.seat_id} · ${stateLabel}`;
      cell.setAttribute("aria-label", cell.title);
      line.append(cell);
    }
    elements.push(line);
  }
  map.replaceChildren(...elements);
}

function formatSchedule(event) {
  if (!event?.starts_at || !event.ends_at || !event.timezone) return "";
  try {
    const format = new Intl.DateTimeFormat("en-US", {
      dateStyle: "medium", timeStyle: "short", timeZone: event.timezone,
    });
    return `${format.formatRange(new Date(event.starts_at), new Date(event.ends_at))} · ${event.timezone}`;
  } catch {
    return "";
  }
}

async function refreshSeats() {
  const request = ++seatRequest;
  try {
    const data = await api(eventPath("seats"));
    if (request !== seatRequest) return;
    const previous = currentSuggestion();
    if (suggestions !== null) {
      suggestions = rankSeatGroups(data.seats, Number(byId("party-size").value), data.seats_per_row);
      const sameOption = previous
        ? suggestions.findIndex((seats) => seats.every((seat, index) => seat === previous[index]))
        : -1;
      suggestionIndex = Math.max(0, sameOption);
    }
    currentSeats = data.seats;
    emailEnabled = data.confirmation_email_enabled === true;
    const schedule = data.event;
    byId("event-title").textContent = schedule?.name || "Seat map";
    byId("event-schedule").textContent = formatSchedule(schedule);
    serverOffset = data.server_time_ms - Date.now();
    renderSeats();
    const available = data.seats.filter((seat) => seat.status === "available").length;
    byId("seat-summary").textContent = `${available} of ${data.seats.length} available · ${data.mode} mode`;
    updateControls();
    return true;
  } catch (error) {
    if (request !== seatRequest) return;
    byId("seat-summary").textContent = error.message;
    return false;
  }
}

async function refreshStats() {
  const request = ++statsRequest;
  const values = byId("stats-values");
  const note = byId("double-booked");
  byId("stats-status").textContent = "Loading stats…";
  values.replaceChildren();
  note.textContent = "";
  try {
    const data = await api(eventPath("stats"));
    if (request !== statsRequest) return;
    for (const [label, value] of [
      ["Capacity", data.capacity], ["Available", data.available], ["Held", data.held],
      ["Sold", data.sold], ["Tickets issued", data.tickets_issued], ["Seat conflicts", data.conflicts],
      ["Confirmed", data.orders.confirmed], ["Pending", data.orders.pending], ["Failed", data.orders.failed],
    ]) {
      const group = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = label;
      const definition = document.createElement("dd");
      definition.textContent = value;
      group.append(term, definition);
      values.append(group);
    }
    byId("stats-status").textContent = eventId;
    note.textContent = data.double_booked_seats.length
      ? `Double-booked seats: ${data.double_booked_seats.join(", ")}`
      : "Double-booked seats: 0";
  } catch (error) {
    if (request === statsRequest) byId("stats-status").textContent = error.message;
  }
}

function activate(panel) {
  byId("scan-back-to-search").hidden = true;
  for (const tab of tabs) {
    const selected = tab.dataset.panel === panel;
    tab.setAttribute("aria-pressed", String(selected));
    byId(tab.dataset.panel).hidden = !selected;
  }
  if (panel === "stats") refreshStats();
  if (panel === "buy" && !busy) refreshSeats();
}

function renderOrder() {
  const list = byId("tickets");
  list.replaceChildren();
  const status = state.order?.status;
  list.hidden = status !== "confirmed";
  if (status === "confirmed") {
    setStatus(checkoutStatus, state.receiptRequested
      ? "Order confirmed. Your tickets are ready. Check your inbox for the confirmation email."
      : "Order confirmed. Your tickets are ready.");
    for (const ticket of state.order.tickets) {
      const item = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = ticket.seat_id;
      const code = document.createElement("code");
      code.textContent = ticket.code;
      const use = document.createElement("button");
      use.type = "button";
      use.className = "button secondary";
      use.textContent = "Open in scanner";
      use.addEventListener("click", () => {
        byId("ticket-code").value = ticket.code;
        setStatus(scanStatus, "");
        activate("scan");
        byId("gate").focus();
      });
      item.append(name, code, use);
      list.append(item);
    }
  } else if (status === "failed") {
    const reason = state.order.fail_reason;
    setStatus(checkoutStatus, reason === "hold_expired"
      ? "The hold expired before checkout completed. Start another order."
      : `Checkout failed: ${reason || "unknown error"}.`, true);
  } else if (state.key) {
    setStatus(checkoutStatus, "Checking order status…");
  } else {
    setStatus(checkoutStatus, "");
  }
  updateControls();
}

async function pollOrder() {
  const deadline = Date.now() + 85000;
  while (Date.now() < deadline) {
    try {
      state.order = await api(`/orders/${state.key}`);
    } catch (error) {
      if (error.status === 404 && !state.order) {
        setStatus(checkoutStatus, "Order not found yet. You can retry checkout with the same key while the hold is active.", true);
        return;
      }
      throw error;
    }
    persist();
    if (state.order.status !== "pending") {
      renderOrder();
      await refreshSeats();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
  setStatus(checkoutStatus, "Your order is still pending. Use Check order again to resume.", true);
}

async function runCheckout(submit) {
  if (busy) return;
  const receiptInput = byId("receipt-email");
  const email = submit && emailEnabled ? receiptInput.value.trim() : "";
  if (email && !receiptInput.reportValidity()) return;
  busy = true;
  updateControls();
  try {
    if (submit) {
      state.key ||= crypto.randomUUID().replaceAll("-", "");
      state.receiptRequested = Boolean(email);
      persist();
      setStatus(checkoutStatus, "Submitting checkout…");
      await api(eventPath(`holds/${state.hold.hold_id}/checkout`), email ? {email} : {}, state.key);
    }
    setStatus(checkoutStatus, "Waiting for checkout…");
    await pollOrder();
  } catch (error) {
    setStatus(checkoutStatus, error.message || "Checkout could not be reached. Check the order again.", true);
  } finally {
    busy = false;
    updateControls();
  }
}

byId("hold-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || activeHold() || state.key) return;
  busy = true;
  suggestions = [];
  suggestionIndex = 0;
  updateControls();
  setStatus(checkoutStatus, "Finding seats…");
  try {
    if (await refreshSeats()) {
      setStatus(checkoutStatus, "");
    } else {
      clearSuggestions();
      setStatus(checkoutStatus, "Could not load seat options. Try again.", true);
    }
  } finally {
    busy = false;
    updateControls();
  }
});

for (const [id, step] of [["previous-option", -1], ["next-option", 1]]) {
  byId(id).addEventListener("click", () => {
    const next = suggestionIndex + step;
    if (busy || !suggestions || next < 0 || next >= suggestions.length) return;
    suggestionIndex = next;
    renderSeats();
    updateControls();
  });
}

byId("party-size").addEventListener("change", () => {
  clearSuggestions();
  setStatus(checkoutStatus, "");
});

byId("hold-option").addEventListener("click", async () => {
  const selected = currentSuggestion();
  if (busy || activeHold() || state.key || !selected) return;
  busy = true;
  updateControls();
  setStatus(checkoutStatus, "Holding selected seats…");
  try {
    state = {hold: await api(eventPath("holds"), {seats: selected})};
    persist();
    clearSuggestions();
    setStatus(checkoutStatus, "");
    await refreshSeats();
  } catch (error) {
    if (error.status === 409) {
      suggestions = [];
      suggestionIndex = 0;
      const updated = await refreshSeats();
      if (!updated) clearSuggestions();
      setStatus(checkoutStatus, updated
        ? "Those seats are no longer available. Review the updated options."
        : "Those seats could not be held. Show best seats again to refresh the options.", true);
    } else {
      setStatus(checkoutStatus, error.message, true);
    }
  } finally {
    busy = false;
    updateControls();
  }
});

byId("checkout-button").addEventListener("click", () => runCheckout(true));
byId("resume-order").addEventListener("click", () => runCheckout(false));
byId("next-order").addEventListener("click", () => {
  state = {};
  byId("receipt-email").value = "";
  clearSuggestions();
  persist();
  renderOrder();
  refreshSeats();
});
byId("refresh-seats").addEventListener("click", refreshSeats);
byId("refresh-stats").addEventListener("click", refreshStats);
for (const tab of tabs) tab.addEventListener("click", () => activate(tab.dataset.panel));

function selectEvent(concert) {
  if (busy) return;
  eventId = concert.event_id;
  state = readState(eventId);
  currentSeats = [];
  serverOffset = 0;
  statsRequest++;
  emailEnabled = false;
  byId("receipt-email").value = "";
  byId("event-title").textContent = concert.name;
  byId("event-schedule").textContent = formatSchedule(concert);
  byId("event-search").hidden = true;
  byId("event-workspace").hidden = false;
  clearSuggestions();
  renderOrder();
  activate("buy");
  byId("event-title").focus();
  if (state.key && (!state.order || state.order.status === "pending")) runCheckout(false);
}

eventForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = eventInput.value.trim();
  const status = byId("event-search-status");
  const items = byId("event-results");
  const version = ++searchRequest;
  items.replaceChildren();
  if (!name) {
    status.textContent = "Enter a concert name or event ID.";
    return;
  }
  status.textContent = "Searching…";
  try {
    const data = await api("/events?" + new URLSearchParams({name}));
    if (version !== searchRequest) return;
    for (const concert of data.events) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "event-result";
      const title = document.createElement("strong");
      title.textContent = concert.name;
      const detail = document.createElement("span");
      const schedule = formatSchedule(concert);
      detail.textContent = schedule ? `${schedule} · ${concert.event_id}` : concert.event_id;
      button.append(title, detail);
      button.addEventListener("click", () => selectEvent(concert));
      item.append(button);
      items.append(item);
    }
    status.textContent = data.events.length
      ? `${data.events.length} result${data.events.length === 1 ? "" : "s"}${data.truncated ? " · Refine your search for more matches" : ""}`
      : "No matching concerts.";
  } catch (error) {
    if (version === searchRequest) status.textContent = error.message;
  }
});

function showEventSearch() {
  if (busy) return;
  byId("event-workspace").hidden = true;
  byId("scan").hidden = true;
  byId("event-search").hidden = false;
  eventInput.focus();
}

byId("back-to-search").addEventListener("click", showEventSearch);
byId("scan-back-to-search").addEventListener("click", showEventSearch);
byId("open-scanner").addEventListener("click", () => {
  byId("event-search").hidden = true;
  byId("scan").hidden = false;
  byId("scan-back-to-search").hidden = false;
  setStatus(scanStatus, "");
  byId("ticket-code").focus();
});

byId("scan-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  setStatus(scanStatus, "Verifying ticket…");
  try {
    const result = await api("/tickets/scan", {
      code: byId("ticket-code").value.trim(), gate: byId("gate").value.trim(),
    });
    if (result.result === "ADMIT") {
      setStatus(scanStatus, `ADMIT · Seat ${result.seat_id}`);
    } else if (result.result === "ALREADY_USED") {
      const firstUse = result.first_used_at_ms == null ? ""
        : `\nFirst scan: ${result.first_gate || "unspecified gate"} · ${new Date(result.first_used_at_ms).toLocaleString()}`;
      setStatus(scanStatus, `ALREADY USED · Seat ${result.seat_id}${firstUse}`, true);
    } else {
      setStatus(scanStatus, "INVALID · The ticket code could not be verified.", true);
    }
  } catch (error) {
    setStatus(scanStatus, error.message, true);
  } finally {
    submit.disabled = false;
  }
});

let holdWasActive = false;
setInterval(() => {
  updateControls();
  const active = remainingSeconds() > 0;
  if (holdWasActive && !active && !busy) refreshSeats();
  holdWasActive = active;
}, 500);
