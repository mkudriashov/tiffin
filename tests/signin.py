"""Sign a test client in the way a person does: open the link, press the button."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from httpx import Response

_TOKEN = re.compile(r'name="k" value="([^"]+)"')


def sign_in(client: TestClient, link: str) -> Response:
    """Open `link`, then post the page's button. Returns the button's response (303 on success,
    unfollowed), or the page itself when it is not a sign-in page."""
    page = client.get(link, follow_redirects=False)
    found = _TOKEN.search(page.text)
    if page.status_code != 200 or found is None:
        return page
    action = re.search(r'<form method="post" action="([^"]*)"', page.text)
    assert action is not None
    return client.post(action.group(1) or "/", data={"k": found.group(1)}, follow_redirects=False)
