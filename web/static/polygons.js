// web/static/polygons.js
// Scaling slot polygons for display, in one place.
//
// Polygons are stored in their camera's reference-frame pixels. Both the
// annotation canvas and the live overlay have to map them onto a rendered
// image of some other size, and v1's defining bug was two code paths doing
// that differently (x2 in one, x1.8/x1.9 in the other). There is one function
// here and both pages call it.

export function scalePolygon(polygon, from, to) {
  const scaleX = to.width / from.width;
  const scaleY = to.height / from.height;
  return polygon.map(([x, y]) => [x * scaleX, y * scaleY]);
}

export function toPoints(polygon) {
  return polygon.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
}

export function centroid(polygon) {
  const sum = polygon.reduce(([ax, ay], [x, y]) => [ax + x, ay + y], [0, 0]);
  return [sum[0] / polygon.length, sum[1] / polygon.length];
}

// The longest edge's angle, so a label reads along the bay rather than across
// it. Kept within a quarter turn of upright so nothing ends up upside down.
export function labelAngle(polygon) {
  let best = 0;
  let longest = -1;
  for (let index = 0; index < polygon.length; index += 1) {
    const [x1, y1] = polygon[index];
    const [x2, y2] = polygon[(index + 1) % polygon.length];
    const length = Math.hypot(x2 - x1, y2 - y1);
    if (length > longest) {
      longest = length;
      best = (Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI;
    }
  }
  if (best > 90) return best - 180;
  if (best < -90) return best + 180;
  return best;
}