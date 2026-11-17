"""Contract tests: GitHubClient against recorded-shape responses (httpx MockTransport)."""

from __future__ import annotations

import base64
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from devopsagents.integrations.github.client import GitHubClient, GitHubError, ReviewComment
from devopsagents.integrations.github.webhooks import verify_signature


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    pub = key.public_key()
    return priv, pub


def make(keypair, handler, clock=lambda: 1_800_000_000.0):
    return GitHubClient("123", keypair[0], transport=httpx.MockTransport(handler), clock=clock)


def test_mints_installation_token_with_valid_app_jwt_and_caches_it(keypair):
    seen = {"token_calls": 0, "auth": []}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            seen["token_calls"] += 1
            claims = jwt.decode(
                req.headers["authorization"].split()[1],
                keypair[1],
                algorithms=["RS256"],
                options={"verify_exp": False, "verify_iat": False},
            )
            assert claims["iss"] == "123"
            return httpx.Response(201, json={"token": "inst-tok"})
        seen["auth"].append(req.headers["authorization"])
        return httpx.Response(
            200, json={"title": "t", "body": None, "head": {"sha": "h"}, "base": {"sha": "b"}}
        )

    gh = make(keypair, handler)
    pr = gh.get_pr(9, "o/r", 1)
    gh.get_pr(9, "o/r", 1)
    assert pr.head_sha == "h" and pr.body == ""
    assert seen["token_calls"] == 1 and set(seen["auth"]) == {"Bearer inst-tok"}


def test_token_refreshes_after_expiry(keypair):
    t = {"now": 1_800_000_000.0}
    calls = []

    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            calls.append(1)
            return httpx.Response(201, json={"token": f"t{len(calls)}"})
        return httpx.Response(200, json={"title": "", "head": {"sha": "h"}, "base": {"sha": "b"}})

    gh = make(keypair, handler, clock=lambda: t["now"])
    gh.get_pr(1, "o/r", 1)
    t["now"] += 4000
    gh.get_pr(1, "o/r", 1)
    assert len(calls) == 2


def test_post_review_payload_and_error_mapping(keypair):
    captured = {}

    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "x"})
        if req.url.path.endswith("/reviews"):
            captured.update(json.loads(req.content))
            return httpx.Response(200, json={"id": 77})
        return httpx.Response(404, json={"message": "Not Found"})

    gh = make(keypair, handler)
    rid = gh.post_review(1, "o/r", 5, "sha", "summary", [ReviewComment("a.py", 3, "hi")])
    assert rid == 77 and captured["commit_id"] == "sha" and captured["event"] == "COMMENT"
    assert captured["comments"] == [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "hi"}]
    assert gh.get_file(1, "o/r", "missing.txt", "sha") is None  # 404 -> None
    with pytest.raises(GitHubError):
        gh.get_pr(1, "o/r", 5)


def test_get_file_decodes_base64(keypair):
    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "x"})
        return httpx.Response(
            200, json={"encoding": "base64", "content": base64.b64encode(b"hello").decode()}
        )

    assert make(keypair, handler).get_file(1, "o/r", "f", "sha") == "hello"


def test_signature_verification_fails_closed():
    import hashlib
    import hmac

    body = b"{}"
    good = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert verify_signature("s", body, good)
    assert not verify_signature("s", body, "sha256=00")
    assert not verify_signature("", body, good)
    assert not verify_signature("s", body, None)
    assert not verify_signature("s", body, good.replace("sha256=", "sha1="))
