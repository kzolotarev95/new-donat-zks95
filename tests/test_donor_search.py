import re
from urllib.parse import parse_qs, urlsplit

import pytest

from app.db import create_donor, get_db


@pytest.fixture()
def donor_admin(app, client):
    with app.app_context():
        alice = create_donor("Alice", note="Спасибо за проект", social_url="https://t.me/alice")
        peter = create_donor("Пётр", note="За сервер", pinned=1)
        literal = create_donor("Percent%_donor")
        get_db().execute("UPDATE donors SET is_active = 0 WHERE id = ?", (peter,))
        get_db().commit()
    login = client.get("/admin/login").get_data(as_text=True)
    token = login.split('name="csrf_token" value="')[1].split('"', 1)[0]
    response = client.post("/admin/login", data={
        "csrf_token": token, "username": "admin", "password": "correct horse battery staple",
    })
    assert response.status_code == 302
    return client, {"alice": alice, "peter": peter, "literal": literal}


@pytest.mark.parametrize(("query", "expected"), [
    ("aLIc", ["alice"]),
    ("  пЁтР  ", ["peter"]),
    ("ЗА СЕРВЕР", ["peter"]),
    ("T.ME/ALICE", ["alice"]),
    ("%_", ["literal"]),
    ("unknown", []),
    ("' OR 1=1 --", []),
    ("   ", ["peter", "literal", "alice"]),
])
def test_search_finds_matching_donors_and_keeps_pin_order(donor_admin, query, expected):
    client, ids = donor_admin
    response = client.get("/admin/donors", query_string={"q": query})
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    rows = [int(value) for value in re.findall(r'action="/admin/donors/(\d+)"', body)]
    assert rows == [ids[name] for name in expected]
    assert "Поиск по донатерам" in body
    if query.strip():
        assert f"Найдено {len(expected)} из 3" in body
        assert 'href="/admin/donors">Сбросить' in body
    if not expected:
        assert "Донатеры не найдены" in body


def test_search_query_is_escaped(donor_admin):
    client, _ = donor_admin
    response = client.get("/admin/donors", query_string={"q": '<script>alert("test")</script>'})
    body = response.get_data(as_text=True)
    assert '<script>alert("test")</script>' not in body
    assert "&lt;script&gt;" in body


def test_editing_and_deleting_keep_search_and_do_not_affect_other_donors(donor_admin, app):
    client, ids = donor_admin
    body = client.get("/admin/donors?q=alice").get_data(as_text=True)
    token = body.split('name="csrf_token" value="')[1].split('"', 1)[0]
    edited = client.post(f"/admin/donors/{ids['alice']}", data={
        "csrf_token": token, "q": "alice", "nickname": "AliceUpdated", "color": "red",
        "total_amount": "250", "note": "Обновлено", "is_active": "1",
    })
    assert edited.status_code == 302
    assert parse_qs(urlsplit(edited.location).query) == {"q": ["alice"]}
    result = client.get(edited.location).get_data(as_text=True)
    assert 'value="AliceUpdated"' in result
    deleted = client.post(f"/admin/donors/{ids['alice']}/delete", data={"csrf_token": token, "q": "alice"})
    assert deleted.status_code == 302
    assert parse_qs(urlsplit(deleted.location).query) == {"q": ["alice"]}
    assert "Донатеры не найдены" in client.get(deleted.location).get_data(as_text=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM donors").fetchone()[0] == 2


def test_donor_search_requires_authentication(client):
    response = client.get("/admin/donors?q=alice")
    assert response.status_code == 302
    assert urlsplit(response.location).path == "/admin/login"
