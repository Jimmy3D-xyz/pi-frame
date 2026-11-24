"""Tests' shortcut for the admin login."""

ADMIN_PW = "admin-pass-1"


def unlock(client, frame, headers=None, password=None):
    """Give the frame an admin password (if it has none) and unlock it for this client."""
    if not frame.cfg.get("admin_password"):
        frame.cfg.update({"admin_password": password or ADMIN_PW})
    r = client.post("/api/admin/unlock", json={"password": frame.cfg.get("admin_password")}, headers=headers or {})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r
