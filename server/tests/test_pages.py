# server/tests/test_pages.py
"""The page shells.

They are deliberately anonymous: no tenant data is rendered server-side, so a
page route needs no session. The test below proves it by putting a distinctive
organization name in the database and checking it never appears in any page.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client

from accounts.models import Organization

PAGES = [
    ("/", "lots.html"),
    ("/login/", "login.html"),
    ("/lots/3f2a0000-0000-4000-8000-000000000001/", "lot.html"),
    ("/lots/3f2a0000-0000-4000-8000-000000000001/cameras/"
     "3f2a0000-0000-4000-8000-000000000002/annotate/", "annotate.html"),
]
STATIC_TAG = re.compile(r"{%\s*static\s+['\"]([^'\"]+)['\"]\s*%}")


@pytest.mark.parametrize("path,template", PAGES, ids=[p for p, _ in PAGES])
def test_pages_render_without_a_session(db, path, template):
    response = Client().get(path)
    assert response.status_code == 200
    assert template in [t.name for t in response.templates]


def test_pages_never_contain_tenant_data(db):
    Organization.objects.create(name="Zzyzx Holdings", slug="zzyzx")
    for path, _ in PAGES:
        body = Client().get(path).content.decode()
        assert "Zzyzx" not in body, path


def test_every_referenced_static_file_exists():
    """A typo in a {% static %} name is a blank page with a 404 in the
    console; this turns it into a failing test."""
    static_root = Path(settings.STATICFILES_DIRS[0])
    missing = []
    for template in Path(settings.TEMPLATES[0]["DIRS"][0]).glob("*.html"):
        for name in STATIC_TAG.findall(template.read_text(encoding="utf-8")):
            if not (static_root / name).exists():
                missing.append(f"{template.name} -> {name}")
    assert not missing, missing


def test_scripts_only_import_modules_that_exist():
    static_root = Path(settings.STATICFILES_DIRS[0])
    pattern = re.compile(r'from\s+"\./([^"]+)"')
    missing = []
    for script in static_root.glob("*.js"):
        for name in pattern.findall(script.read_text(encoding="utf-8")):
            if not (static_root / name).exists():
                missing.append(f"{script.name} -> {name}")
    assert not missing, missing