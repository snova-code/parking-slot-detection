// web/static/analytics.js
// Occupancy, dwell and turnover from GET /lots/{id}/analytics.
//
// Coverage is the load-bearing idea. Every bucket carries how much of it was
// actually observed, and a rate computed over four minutes of an hour is not
// a rate. It is drawn as opacity, hatched out below a threshold, explained in
// the caption, and carried in the CSV — a screenshot of this chart should not
// be able to mislead someone who does not know the system.
import { api } from "./api.js";
import { barChart, histogram } from "./charts.js";
import { describe, mountNav, requireSession, toast } from "./ui.js";

const MIN_COVERAGE = 0.25;
const lotId = location.pathname.split("/").filter(Boolean)[1];
const nodes = {
  name: document.getElementById("lot-name"),
  scope: document.getElementById("scope"),
  form: document.getElementById("range"),
  from: document.getElementById("from"),
  to: document.getElementById("to"),
  bucket: document.getElementById("bucket"),
  export: document.getElementById("export"),
  status: document.getElementById("status"),
  occupancy: document.getElementById("occupancy"),
  occupancyCard: document.getElementById("occupancy-card"),
  dwell: document.getElementById("dwell"),
  dwellCard: document.getElementById("dwell-card"),
  dwellSummary: document.getElementById("dwell-summary"),
  turnover: document.getElementById("turnover"),
  turnoverCard: document.getElementById("turnover-card"),
  facts: document.getElementById("facts"),
};

let latest = null;

// ------------------------------------------------------------------ helpers
function toLocalInput(date) {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function fromLocalInput(value) {
  return new Date(value);       // parsed in the browser's zone, sent as an offset
}

function percent(value, digits = 1) {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function bucketLabel(iso, bucket, index, total) {
  const date = new Date(iso);
  if (bucket === "day") {
    return index % Math.ceil(total / 12) === 0
      ? date.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
  }
  return date.getHours() % (total > 48 ? 6 : 3) === 0
    ? date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "";
}

// ----------------------------------------------------------------- rendering
function renderOccupancy(data) {
  const buckets = data.occupancy_rate;
  nodes.occupancyCard.hidden = false;
  barChart({
    container: nodes.occupancy,
    yMax: 1,
    minWeight: MIN_COVERAGE,
    formatTick: (tick) => `${Math.round(tick * 100)}%`,
    formatValue: percent,
    bars: buckets.map((bucket, index) => ({
      value: bucket.rate,
      weight: bucket.coverage ?? 0,
      tick: bucketLabel(bucket.bucket, data.bucket, index, buckets.length),
      title: `${new Date(bucket.bucket).toLocaleString()}\n`
           + `occupancy ${percent(bucket.rate)}\ncoverage ${percent(bucket.coverage)}`
           + ((bucket.coverage ?? 0) < MIN_COVERAGE
               ? "\nToo little was observed to read this as a rate." : ""),
    })),
  });
}

function renderDwell(data) {
  const dwell = data.dwell_minutes;
  nodes.dwellCard.hidden = false;
  if (!dwell.samples) {
    nodes.dwellSummary.textContent =
      "No completed occupancies in this range.";
    nodes.dwell.replaceChildren();
    return;
  }
  nodes.dwellSummary.textContent =
    `median ${dwell.p50} min · p90 ${dwell.p90} min · mean ${dwell.mean} min `
    + `· ${dwell.samples} occupanc${dwell.samples === 1 ? "y" : "ies"}`;
  // The API returns the summary, not the samples, so the shape is drawn from
  // the three order statistics rather than invented.
  nodes.dwell.replaceChildren();
  barChart({
    container: nodes.dwell,
    yMax: dwell.p90,
    minWeight: 0,
    height: 160,
    formatTick: (tick) => `${Math.round(tick)}m`,
    formatValue: (value) => `${value} min`,
    bars: [
      { value: dwell.p50, weight: 1, tick: "median", title: `median ${dwell.p50} min` },
      { value: dwell.mean, weight: 1, tick: "mean", title: `mean ${dwell.mean} min` },
      { value: dwell.p90, weight: 1, tick: "p90", title: `p90 ${dwell.p90} min` },
    ],
  });
}

function renderTurnover(data) {
  nodes.turnoverCard.hidden = false;
  nodes.turnover.textContent = data.turnover_per_slot_per_day === null
    ? "—" : data.turnover_per_slot_per_day.toFixed(2);

  const observedShare = data.slots_counted && data.observed_slot_days
    ? data.observed_slot_days / (data.slots_counted * spanDays()) : 0;
  nodes.facts.replaceChildren();
  [
    ["Slots", data.slots_counted],
    ["Observed slot-days", data.observed_slot_days.toFixed(2)],
    ["Observed share of the range", percent(Math.min(observedShare, 1), 0)],
    ["Timezone", data.timezone],
  ].forEach(([term, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    nodes.facts.append(dt, dd);
  });
}

function spanDays() {
  const from = fromLocalInput(nodes.from.value);
  const to = fromLocalInput(nodes.to.value);
  return Math.max((to - from) / 86400000, 1 / 24);
}

// ------------------------------------------------------------------- export
function exportCsv() {
  if (!latest) return;
  const rows = [["bucket", "occupancy_rate", "coverage"]];
  latest.occupancy_rate.forEach((bucket) => rows.push([
    bucket.bucket,
    bucket.rate === null ? "" : bucket.rate,
    bucket.coverage === null ? "" : bucket.coverage,
  ]));
  rows.push([], ["metric", "value"]);
  [
    ["timezone", latest.timezone],
    ["bucket_size", latest.bucket],
    ["slots_counted", latest.slots_counted],
    ["observed_slot_days", latest.observed_slot_days],
    ["dwell_p50_minutes", latest.dwell_minutes.p50 ?? ""],
    ["dwell_p90_minutes", latest.dwell_minutes.p90 ?? ""],
    ["dwell_mean_minutes", latest.dwell_minutes.mean ?? ""],
    ["dwell_samples", latest.dwell_minutes.samples],
    ["turnover_per_slot_per_day", latest.turnover_per_slot_per_day ?? ""],
  ].forEach((row) => rows.push(row));

  const csv = rows.map((row) => row.join(",")).join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `apms-analytics-${lotId}-${nodes.bucket.value}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

// -------------------------------------------------------------------- loading
async function load() {
  nodes.status.hidden = false;
  nodes.status.textContent = "Loading…";
  nodes.export.disabled = true;

  const params = new URLSearchParams({
    from: fromLocalInput(nodes.from.value).toISOString(),
    to: fromLocalInput(nodes.to.value).toISOString(),
    bucket: nodes.bucket.value,
  });
  try {
    const data = await api(`/lots/${lotId}/analytics?${params}`);
    latest = data;
    renderOccupancy(data);
    renderDwell(data);
    renderTurnover(data);
    nodes.status.hidden = true;
    nodes.export.disabled = false;

    const thin = data.occupancy_rate.filter(
      (bucket) => (bucket.coverage ?? 0) < MIN_COVERAGE).length;
    if (thin) {
      toast(`${thin} of ${data.occupancy_rate.length} buckets had too little `
            + "coverage to read", "warn");
    }
  } catch (error) {
    nodes.status.textContent = describe(error);
    [nodes.occupancyCard, nodes.dwellCard, nodes.turnoverCard]
      .forEach((card) => { card.hidden = true; });
  }
}

nodes.form.addEventListener("submit", (event) => {
  event.preventDefault();
  load();
});
nodes.export.addEventListener("click", exportCsv);
document.querySelectorAll(".quick button").forEach((button) => {
  button.addEventListener("click", () => {
    const now = new Date();
    nodes.to.value = toLocalInput(now);
    nodes.from.value = toLocalInput(
      new Date(now.getTime() - Number(button.dataset.hours) * 3600000));
    if (button.dataset.bucket) nodes.bucket.value = button.dataset.bucket;
    load();
  });
});

(async () => {
  const me = await requireSession();
  if (!me) return;
  mountNav(me);

  const now = new Date();
  nodes.to.value = toLocalInput(now);
  nodes.from.value = toLocalInput(new Date(now.getTime() - 24 * 3600000));

  try {
    const lot = await api(`/lots/${lotId}/slots`);
    nodes.name.textContent = `${lot.lot_name} — analytics`;
    nodes.scope.textContent = `${lot.cameras.length} camera(s) · times shown in `
                            + `your browser's timezone, buckets aligned to ${lot.timezone}`;
  } catch (error) {
    nodes.name.textContent = "Analytics";
    toast(describe(error), "error");
    return;
  }
  load();
})();