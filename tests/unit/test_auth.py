from orchivio.api.throttle import LoginThrottle
from orchivio.auth import (
    API_TOKEN_PREFIX,
    hash_password,
    hash_token,
    matches,
    needs_rehash,
    new_token,
    password_problem,
    verify_password,
)


def test_password_roundtrip_and_salting():
    h1, h2 = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert h1 != h2 and h1.startswith("scrypt$")
    assert verify_password("correct horse battery", h1)
    assert not verify_password("correct horse batterz", h1)
    assert not needs_rehash(h1)


def test_malformed_or_missing_hashes_never_verify():
    for bad in [None, "", "x", "scrypt$1$2", "bcrypt$1$1$1$AA$AA", "scrypt$a$b$c$d$e", "scrypt$16384$8$1$!!$!!"]:
        assert not verify_password("anything", bad)


def test_oversized_password_is_rejected_without_hashing():
    assert not verify_password("x" * 5000, hash_password("x" * 20))


def test_password_policy():
    assert password_problem("short") and password_problem("a" * 200) and password_problem("aaaaaaaaaaaaaaaa")
    assert password_problem("correct horse battery") is None


def test_old_parameters_trigger_rehash():
    assert needs_rehash("scrypt$1024$8$1$AA==$AA==") and needs_rehash("garbage")


def test_tokens_are_random_prefixed_and_hash_only():
    a, ah = new_token(API_TOKEN_PREFIX)
    b, _ = new_token(API_TOKEN_PREFIX)
    assert a != b and a.startswith("orv_") and ah == hash_token(a) and matches(a, ah)
    assert not matches(b, ah) and a not in ah


def test_throttle_blocks_then_expires_and_resets():
    now = [0.0]
    t = LoginThrottle(3, 100, clock=lambda: now[0])
    for _ in range(3):
        assert not t.blocked("k")
        t.fail("k")
    assert t.blocked("k") and not t.blocked("other")
    now[0] = 101
    assert not t.blocked("k")
    t.fail("k")
    t.succeed("k")
    assert not t.blocked("k")
