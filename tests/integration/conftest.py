import pytest


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch):
    """Integration tests create many users; use minimal scrypt cost (production cost is
    covered by tests/unit/test_auth.py). Stored hashes carry their own parameters."""
    from orchivio.auth import passwords

    monkeypatch.setattr(passwords, "N", 2**4)
