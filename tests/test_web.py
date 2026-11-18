"""Интерфейс демо: отдаётся с того же сервера, работает без внешних ресурсов, политика CSP на месте."""
import re

from fastapi.testclient import TestClient

from pulse.api.main import app

client = TestClient(app)


def test_index_served_with_csp():
    r = client.get("/")
    assert r.status_code == 200 and "Пульс маржи" in r.text
    assert "Content-Security-Policy" in r.text and "default-src 'self'" in r.text


def test_assets_exist_and_are_local():
    html = client.get("/").text
    refs = re.findall(r'(?:src|href)="([^"#][^"]*)"', html)
    local = [u for u in refs if not u.startswith(("data:", "/docs"))]
    assert local, "страница должна подключать свои стили и скрипты"
    for u in local:
        assert not u.startswith(("http:", "https:", "//")), f"внешний ресурс: {u}"
        assert client.get(f"/{u.lstrip('/')}").status_code == 200, u
    for js in ("js/charts.js", "js/fmt.js"):
        assert client.get(f"/{js}").status_code == 200


def test_check_page_served_and_local():
    r = client.get("/check.html")
    assert r.status_code == 200 and "Снизят ли цену" in r.text and "default-src 'self'" in r.text
    for u in ("js/check.js", "css/check.css"):
        assert client.get(f"/{u}").status_code == 200
