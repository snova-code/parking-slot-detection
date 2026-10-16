# server/parking/migrations/0003_fix_polygon_shape_lax_mode.py
"""Replace parking_slot_polygon_shape; the original ran in lax jsonpath mode.

SQL/JSON path defaults to lax, which auto-unwraps arrays inside a filter. In
`$[*] ? (@.type() != "array" ...)` the `@` bound to an element such as
`[10, 10]` was unwrapped to its members, so `@.type()` returned "number"
rather than "array", the filter matched, and jsonb_path_exists returned true.
The NOT then rejected every well-formed polygon. The predicate read as "no
element is a non-array" and behaved as "no element contains a non-array",
which every polygon violates by construction.

Two changes:

  `strict` disables unwrapping, so `@` stays the element itself.

  The coordinate check nests `exists(@[*] ? ...)` instead of walking
  `$[*][*]`. Inside a filter, strict-mode structural errors are suppressed and
  treated as non-matching, so a malformed element cannot raise an exception out
  of the CHECK; it is rejected by the shape predicate above as an ordinary
  constraint violation instead.

The CASE is replaced by a plain conjunction. jsonb_array_length errors on a
non-array, and the jsonb_typeof conjunct guards it.

Verified against every case in tests/test_schema_constraints.py before this
migration was written: the three valid polygons pass, the ten malformed ones
fail, and none of them error or return NULL.
"""
from __future__ import annotations

from django.db import migrations

# Kept verbatim from 0002 so the reverse is a true inverse, bug included.
OLD_POLYGON_SHAPE = """
ALTER TABLE parking_slot ADD CONSTRAINT parking_slot_polygon_shape CHECK (
    CASE WHEN jsonb_typeof(polygon) = 'array' THEN
        jsonb_array_length(polygon) = 4
        AND NOT jsonb_path_exists(
            polygon, '$[*] ? (@.type() != "array" || @.size() != 2)')
        AND NOT jsonb_path_exists(
            polygon, '$[*][*] ? (@.type() != "number" || @ < 0)')
    ELSE false
    END
);
"""

NEW_POLYGON_SHAPE = """
ALTER TABLE parking_slot ADD CONSTRAINT parking_slot_polygon_shape CHECK (
    jsonb_typeof(polygon) = 'array'
    AND jsonb_array_length(polygon) = 4
    AND NOT jsonb_path_exists(
        polygon, 'strict $[*] ? (@.type() != "array" || @.size() != 2)')
    AND NOT jsonb_path_exists(
        polygon, 'strict $[*] ? (exists(@[*] ? (@.type() != "number" || @ < 0)))')
);
"""

DROP_POLYGON_SHAPE = (
    "ALTER TABLE parking_slot DROP CONSTRAINT parking_slot_polygon_shape;"
)


class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0002_tenant_consistency"),
    ]

    operations = [
        migrations.RunSQL(
            sql=DROP_POLYGON_SHAPE,
            reverse_sql=OLD_POLYGON_SHAPE,
        ),
        migrations.RunSQL(
            sql=NEW_POLYGON_SHAPE,
            reverse_sql=DROP_POLYGON_SHAPE,
        ),
    ]