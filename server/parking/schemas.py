# server/parking/schemas.py
"""Shapes used by more than one router."""
from ninja import Schema


class ReferenceFrameOut(Schema):
    """The resolution the polygons were drawn against. Always travels beside
    the polygons that depend on it, so no caller can pair them wrongly."""

    width: int
    height: int