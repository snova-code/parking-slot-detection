# server/parking/migrations/0002_tenant_consistency.py
"""Constraints the Django ORM cannot express.

Composite foreign keys make cross-tenant parent/child mismatch impossible:
a camera cannot point at another org's lot, a slot at another org's camera, a
slot state at another org's slot, and an event cannot claim a camera that does
not own its slot. Denormalised org_id is only safe if the database forbids it
from disagreeing. Event -> slot -> org consistency follows from the two event
constraints plus the slot's own, so no third event constraint is needed.

All are NOT DEFERRABLE. Django defers its own FKs to commit, so a violation
inside a rolled-back test transaction would never fire and the tests proving
these constraints would prove nothing. Django deletes children before parents,
so immediate checking does not interfere with ORM cascades — tested.

The polygon check guarantees shape only: exactly four [x, y] pairs of
non-negative numbers. Geometry — clockwise order, non-self-intersecting, inside
the camera frame — is validated by the API, since frame size lives on another
table. CASE forces evaluation order: jsonb_array_length errors on a non-array.

The trigger makes occupancy_event append-only. It raises restrict_violation so
the failure surfaces as IntegrityError like every other constraint here.
"""
from django.db import migrations

COMPOSITE_FKS = [
    ("camera", "camera_lot_same_org",
     "(lot_id, org_id)", "parking_lot (id, org_id)"),
    ("parking_slot", "slot_camera_same_org",
     "(camera_id, org_id)", "camera (id, org_id)"),
    ("slot_state", "slot_state_slot_same_org",
     "(slot_id, org_id)", "parking_slot (id, org_id)"),
    ("occupancy_event", "event_camera_same_org",
     "(camera_id, org_id)", "camera (id, org_id)"),
    ("occupancy_event", "event_slot_owned_by_camera",
     "(slot_id, camera_id)", "parking_slot (id, camera_id)"),
]

POLYGON_CHECK = """
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

APPEND_ONLY_FUNCTION = """
CREATE FUNCTION occupancy_event_reject_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'occupancy_event is append-only (id %)', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

APPEND_ONLY_TRIGGER = """
CREATE TRIGGER occupancy_event_append_only
    BEFORE UPDATE ON occupancy_event
    FOR EACH ROW EXECUTE FUNCTION occupancy_event_reject_update();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("parking", "0001_initial"),
    ]

    operations = [
        *[
            migrations.RunSQL(
                sql=f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                    f"FOREIGN KEY {columns} REFERENCES {target};",
                reverse_sql=f"ALTER TABLE {table} DROP CONSTRAINT {name};",
            )
            for table, name, columns, target in COMPOSITE_FKS
        ],
        migrations.RunSQL(
            sql=POLYGON_CHECK,
            reverse_sql="ALTER TABLE parking_slot DROP CONSTRAINT parking_slot_polygon_shape;",
        ),
        migrations.RunSQL(
            sql=APPEND_ONLY_FUNCTION,
            reverse_sql="DROP FUNCTION occupancy_event_reject_update();",
        ),
        migrations.RunSQL(
            sql=APPEND_ONLY_TRIGGER,
            reverse_sql="DROP TRIGGER occupancy_event_append_only ON occupancy_event;",
        ),
    ]