import pytest

from orchivio.integrations import (
    DestinationError,
    Policy,
    check_url,
    is_public,
    resolve_public,
    sign,
    verify_signature,
)


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses(addr):
    assert is_public(addr)


@pytest.mark.parametrize("addr", [
    "127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "224.0.0.1", "255.255.255.255", "192.0.2.1", "198.51.100.7", "::1", "::", "fe80::1", "fc00::1", "fd12::1",
    "::ffff:127.0.0.1", "::ffff:10.1.2.3", "::ffff:169.254.169.254", "64:ff9b::7f00:1", "2002:7f00:1::1", "not an ip",
])
def test_non_public_addresses(addr):
    assert not is_public(addr)


@pytest.mark.parametrize("url", [
    "https://example.com/hook", "https://example.com:8443/a/b?x=1", "https://sub.example.co.jp/", "https://8.8.8.8/h",
    "HTTPS://EXAMPLE.COM/", "https://example.com./hook", "https://münchen.example/",
])
def test_acceptable_urls(url):
    assert check_url(url)


@pytest.mark.parametrize("url", [
    "http://example.com/", "ftp://example.com/", "file:///etc/passwd", "gopher://example.com/", "//example.com/",
    "https://user:pw@example.com/", "https://user@example.com/", "https://example.com@evil.test/",
    "https://localhost/", "https://app.localhost/", "https://printer.local/", "https://db.internal/", "https://intranet/",
    "https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/latest/meta-data/", "https://10.0.0.5/",
    "https://[::ffff:127.0.0.1]/", "https://2130706433/", "https://0x7f.0.0.1/", "https:///nohost", "https://exa mple.com/",
    "https://example.com/\r\nX-Evil: 1", "https://example.com:99999/", "https://example.com:abc/", "",
])
def test_refused_urls(url):
    with pytest.raises(DestinationError):
        check_url(url)


def test_dev_switches_and_allowlist():
    assert check_url("http://localhost:8080/x", Policy(allow_http=True, allow_private=True)).port == 8080
    with pytest.raises(DestinationError):
        check_url("http://localhost/", Policy(allow_http=True))  # plain http alone does not open private targets
    allow = Policy(allowed_hosts=("*.partner.example", "hooks.example.com"))
    assert check_url("https://api.partner.example/x", allow)
    assert check_url("https://hooks.example.com/x", allow)
    with pytest.raises(DestinationError):
        check_url("https://evil.test/x", allow)
    with pytest.raises(DestinationError):
        check_url("https://partner.example.evil.test/x", allow)


def test_default_ports_and_target():
    d = check_url("https://example.com/a/b?x=1")
    assert (d.scheme, d.host, d.port, d.target) == ("https", "example.com", 443, "/a/b?x=1")
    assert check_url("https://example.com").target == "/"
    assert check_url("https://münchen.example/").host == "xn--mnchen-3ya.example"


def test_resolution_must_be_all_public():
    dest = check_url("https://hooks.example.com/")
    assert resolve_public(dest, resolver=lambda h, p: ["93.184.216.34", "2606:4700::1111"]) == ["93.184.216.34", "2606:4700::1111"]
    for answers in (["10.0.0.1"], ["93.184.216.34", "127.0.0.1"], ["::1"], ["169.254.169.254"]):
        with pytest.raises(DestinationError):
            resolve_public(dest, resolver=lambda h, p, a=answers: a)  # one bad answer rejects the name
    with pytest.raises(DestinationError):
        resolve_public(dest, resolver=lambda h, p: [])
    assert resolve_public(dest, Policy(allow_private=True), resolver=lambda h, p: ["127.0.0.1"]) == ["127.0.0.1"]


def test_signature_roundtrip_and_tampering():
    body = b'{"a":1}'
    header = sign("s3cret", body, timestamp=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert verify_signature("s3cret", header, body, now=1_700_000_100)
    assert not verify_signature("other", header, body, now=1_700_000_100)
    assert not verify_signature("s3cret", header, b'{"a":2}', now=1_700_000_100)
    assert not verify_signature("s3cret", header, body, now=1_700_000_000 + 301)  # stale: replay window
    assert not verify_signature("s3cret", header.replace("t=1700000000", "t=1700000001"), body, now=1_700_000_100)
    for junk in ("", "v1=abc", "t=x,v1=y", "garbage"):
        assert not verify_signature("s3cret", junk, body)
