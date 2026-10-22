// web/static/charts.js
// Minimal SVG charting: a bar series and a histogram.
//
// No chart library. These are two shapes and an axis; a library would be
// ~200KB, a build step or a CDN pin, and it still could not encode coverage
// into the bar — which is the only interesting requirement here.

const SVG_NS = "http://www.w3.org/2000/svg";

export function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) =>
    element.setAttribute(key, String(value)));
  return element;
}

export function niceTicks(max, count = 4) {
  if (max <= 0) return [0];
  const raw = max / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude)
    .find((candidate) => candidate >= raw) || magnitude * 10;
  const ticks = [];
  for (let value = 0; value <= max + step / 2; value += step) ticks.push(value);
  return ticks;
}

/**
 * Bars whose height is `value` and whose confidence is `weight` (0–1).
 * Bars below `minWeight` are drawn hatched with no readable height: a rate
 * measured over four minutes of a full hour is not a rate.
 */
export function barChart({ container, bars, formatValue, formatTick,
                           yMax = 1, minWeight = 0.25, height = 240 }) {
  const width = 1000;
  const pad = { top: 12, right: 12, bottom: 34, left: 48 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;

  const svg = svgElement("svg", {
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    class: "chart",
    role: "img",
  });

  const defs = svgElement("defs");
  const hatch = svgElement("pattern", {
    id: "low-coverage", width: 6, height: 6,
    patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)",
  });
  hatch.append(svgElement("rect", { width: 6, height: 6, fill: "rgba(148,163,184,.10)" }));
  hatch.append(svgElement("line", { x1: 0, y1: 0, x2: 0, y2: 6,
                                    stroke: "rgba(148,163,184,.45)", "stroke-width": 2 }));
  defs.append(hatch);
  svg.append(defs);

  niceTicks(yMax).forEach((tick) => {
    const y = pad.top + plotHeight - (tick / yMax) * plotHeight;
    svg.append(svgElement("line", {
      x1: pad.left, x2: width - pad.right, y1: y, y2: y, class: "grid",
    }));
    const label = svgElement("text", { x: pad.left - 8, y: y + 4, class: "tick y" });
    label.textContent = formatTick(tick);
    svg.append(label);
  });

  const slot = plotWidth / Math.max(bars.length, 1);
  const barWidth = Math.max(slot * 0.72, 1);

  bars.forEach((bar, index) => {
    const x = pad.left + index * slot + (slot - barWidth) / 2;
    const group = svgElement("g", { class: "bar" });
    const confident = bar.weight >= minWeight && bar.value !== null;

    if (!confident) {
      group.classList.add("low");
      group.append(svgElement("rect", {
        x, y: pad.top, width: barWidth, height: plotHeight,
        fill: "url(#low-coverage)",
      }));
    } else {
      const barHeight = Math.max((bar.value / yMax) * plotHeight, 1);
      group.append(svgElement("rect", {
        x, y: pad.top + plotHeight - barHeight,
        width: barWidth, height: barHeight,
        // Opacity carries coverage: a thinner-evidence hour looks fainter.
        "fill-opacity": (0.35 + 0.65 * bar.weight).toFixed(2),
      }));
    }

    const title = svgElement("title");
    title.textContent = bar.title;
    group.append(title);
    svg.append(group);

    if (bar.tick) {
      const label = svgElement("text", {
        x: x + barWidth / 2, y: height - 12, class: "tick x",
      });
      label.textContent = bar.tick;
      svg.append(label);
    }
  });

  svg.append(svgElement("line", {
    x1: pad.left, x2: width - pad.right,
    y1: pad.top + plotHeight, y2: pad.top + plotHeight, class: "axis",
  }));

  container.replaceChildren(svg);
  return svg;
}

/** Equal-width buckets over `values`, labelled with `unit`. */
export function histogram({ container, values, unit, bins = 12 }) {
  if (!values.length) {
    container.replaceChildren();
    return;
  }
  const max = Math.max(...values);
  const width = Math.max(max / bins, 1);
  const counts = new Array(bins).fill(0);
  values.forEach((value) => {
    counts[Math.min(Math.floor(value / width), bins - 1)] += 1;
  });
  const tallest = Math.max(...counts);

  barChart({
    container,
    yMax: tallest,
    minWeight: 0,
    height: 200,
    formatTick: (tick) => String(Math.round(tick)),
    formatValue: (value) => String(value),
    bars: counts.map((count, index) => ({
      value: count,
      weight: 1,
      tick: index % 3 === 0 ? Math.round(index * width) : "",
      title: `${Math.round(index * width)}–${Math.round((index + 1) * width)} ${unit}: `
           + `${count} occupanc${count === 1 ? "y" : "ies"}`,
    })),
  });
}