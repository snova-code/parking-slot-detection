# server/parking/geometry.py
"""Polygon canonicalisation for slot annotation.

The database guarantees shape: four [x, y] pairs of non-negative numbers.
This guarantees meaning: the corners sit inside the camera's reference frame,
they enclose a usable area, and they run clockwise from the corner nearest the
frame's top-left.

Corner order is not cosmetic. The worker crops with
cv2.getPerspectiveTransform(quad -> 96x96), mapping point i of the quad to
corner i of the square, so a different rotation or winding yields a rotated or
mirrored patch and the classifier was trained on upright ones. The goal here is
one consistent order for every polygon from every client, enforced server-side
rather than asked for in documentation.
"""
from __future__ import annotations

from math import atan2

from config.errors import ApiError

MIN_AREA_PIXELS = 16.0


def _fail(label: str, reason: str, detail: dict | None = None):
    raise ApiError(422, "invalid_polygon", f"Slot {label}: {reason}.",
                   {"label": label, **(detail or {})})


def _signed_area(points: list[tuple[float, float]]) -> float:
    """Shoelace. In image coordinates (y downwards) a clockwise quadrilateral
    has positive area."""
    total = 0.0
    for index in range(len(points)):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2


def canonical_polygon(points, label: str, width: int, height: int) -> list[list[float]]:
    corners = [(float(point[0]), float(point[1])) for point in points]

    for x, y in corners:
        if not (0 <= x <= width and 0 <= y <= height):
            _fail(label, "a corner lies outside the reference frame",
                  {"point": [x, y], "reference_frame": [width, height]})
    if len({(round(x, 6), round(y, 6)) for x, y in corners}) != 4:
        _fail(label, "corners must be four distinct points")

    centre_x = sum(x for x, _ in corners) / 4
    centre_y = sum(y for _, y in corners) / 4
    ordered = sorted(corners, key=lambda p: atan2(p[1] - centre_y, p[0] - centre_x))
    if _signed_area(ordered) < 0:
        ordered.reverse()

    first = min(range(4), key=lambda i: ordered[i][0] ** 2 + ordered[i][1] ** 2)
    ordered = ordered[first:] + ordered[:first]

    area = abs(_signed_area(ordered))
    if area < MIN_AREA_PIXELS:
        _fail(label, "corners do not enclose a usable area", {"area_px": round(area, 2)})
    return [[x, y] for x, y in ordered]