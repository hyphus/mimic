from __future__ import annotations

import secrets


def csrf_token(client) -> str:
    """Read the client's CSRF token, seeding one if the session has none yet."""
    with client.session_transaction() as sess:
        token = sess.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            sess["csrf_token"] = token
    return token
