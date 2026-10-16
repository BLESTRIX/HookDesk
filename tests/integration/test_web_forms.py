import re

import pytest
from sqlalchemy import select

from app.models import WebhookSource

pytestmark = pytest.mark.integration


def test_browser_registration_source_forms_and_logout(client, db):
    response = client.get("/register")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]
    response = client.post(
        "/register",
        data={
            "csrf_token": csrf,
            "email": "web@example.com",
            "password": "WebFormPassword123!",
            "organization_name": "Web Shop",
        },
    )
    assert response.status_code == 200 and "Every event" in response.text
    csrf = client.cookies.get("hookdesk_csrf")
    assert client.post("/sources", data={"name": "Web courier"}).status_code == 403
    response = client.post(
        "/sources", data={"name": "Web courier", "csrf_token": csrf}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 200 and "Save this secret now" in response.text
    source = db.scalar(select(WebhookSource).where(WebhookSource.name == "Web courier"))
    response = client.post(
        f"/sources/{source.id}/rotate-secret",
        data={"grace_seconds": "0", "csrf_token": csrf},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200 and "Save this secret now" in response.text
    response = client.post(
        f"/sources/{source.id}", data={"name": "Renamed courier", "csrf_token": csrf}
    )
    assert response.status_code == 200 and "Renamed courier" in response.text
    response = client.post("/logout", data={"csrf_token": csrf})
    assert response.status_code == 200 and "Welcome back" in response.text
    assert client.get("/api/v1/auth/me").status_code == 401
