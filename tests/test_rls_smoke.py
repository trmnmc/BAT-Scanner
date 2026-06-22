"""RLS anon smoke test — the CRITICAL go-live gate for the Supabase backend (Phase 5).

The anon key is public (it ships in the static frontend), so Row Level Security is the ENTIRE
security boundary. This test proves the boundary holds by hitting the live REST API with the
anon key and asserting it can read NOTHING and write NOTHING on every table.

It SKIPS unless you point it at a real project, so `python -m pytest` stays green locally:

    SUPABASE_URL=https://<ref>.supabase.co \
    SUPABASE_ANON_KEY=<anon key> \
    python -m pytest tests/test_rls_smoke.py -v

Run it (and require it to pass) AFTER applying supabase/schema.sql and BEFORE putting any real
data in. A green run here is what makes the public anon key safe.

Deeper checks to add once two test users exist (set SUPABASE_USER_A_JWT / _B_JWT): user A cannot
read user B's private stars/sends, owner-scoped UPDATE/DELETE, and anon cannot subscribe to
realtime. The anon-deny checks below are the core boundary proof.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

URL = os.environ.get("SUPABASE_URL")
ANON = os.environ.get("SUPABASE_ANON_KEY")
TABLES = ["profiles", "listing_cache", "stars", "watchlist", "sends", "reactions", "saved_filters"]

pytestmark = pytest.mark.skipif(
    not (URL and ANON),
    reason="set SUPABASE_URL + SUPABASE_ANON_KEY to run the live RLS smoke test (go-live gate)",
)


def _req(method, path, body=None):
    req = urllib.request.Request(
        URL.rstrip("/") + path, method=method,
        headers={"apikey": ANON, "Authorization": f"Bearer {ANON}",
                 "Content-Type": "application/json", "Prefer": "return=representation"})
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.mark.parametrize("table", TABLES)
def test_anon_cannot_read(table):
    """Deny-by-default RLS: anon sees ZERO rows (200 []) or is rejected (4xx). Never data."""
    status, body = _req("GET", f"/rest/v1/{table}?select=*&limit=5")
    if status == 200:
        assert json.loads(body) == [], f"{table}: anon SELECT returned rows — boundary leak!"
    else:
        assert status in (401, 403), f"{table}: unexpected SELECT status {status}: {body[:200]}"


@pytest.mark.parametrize("table", TABLES)
def test_anon_cannot_insert(table):
    """No anon INSERT policy anywhere -> every anon write is rejected."""
    status, body = _req("POST", f"/rest/v1/{table}", body={"bat_id": 1})
    assert status in (400, 401, 403), f"{table}: anon INSERT not denied (status {status}): {body[:200]}"


@pytest.mark.parametrize("table", TABLES)
def test_anon_cannot_delete(table):
    status, body = _req("DELETE", f"/rest/v1/{table}?bat_id=eq.1")
    assert status in (400, 401, 403, 404), f"{table}: anon DELETE not denied (status {status})"
