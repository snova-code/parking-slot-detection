# server/parking/migrations/0006_camera_outage_tenant_fk.py
"""Tenant consistency for camera_outage, in the same style as 0002: an outage
cannot reference another organization's camera."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("parking", "0005_cameraoutage")]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE camera_outage ADD CONSTRAINT outage_camera_same_org "
                "FOREIGN KEY (camera_id, org_id) REFERENCES camera (id, org_id);",
            reverse_sql="ALTER TABLE camera_outage DROP CONSTRAINT outage_camera_same_org;",
        ),
    ]