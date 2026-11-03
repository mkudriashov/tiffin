"""Sign a test client in the way a person does: open the link, and the page sends its form
(its own script presses the button; a test does what the script does)."""

from __future__ import annotations

import html
import re

from fastapi.testclient import TestClient
from httpx import Response

_TOKEN = re.compile(r'name="k" value="([^"]+)"')
_NEXT = re.compile(r'name="next" value="([^"]*)"')


def sign_in(client: TestClient, link: str) -> Response:
    """Open `link`, then post the page's form. Returns the form's response (303 on success,
    unfollowed), or the page itself when it is not a sign-in page."""
    page = client.get(link, follow_redirects=False)
    found = _TOKEN.search(page.text)
    if page.status_code != 200 or found is None:
        return page
    action = re.search(r'<form[^>]*method="post" action="([^"]*)"', page.text)
    assert action is not None
    data = {"k": found.group(1)}
    nxt = _NEXT.search(page.text)
    if nxt is not None and nxt.group(1):
        data["next"] = html.unescape(nxt.group(1))
    return client.post(action.group(1) or "/", data=data, follow_redirects=False)
