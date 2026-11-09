"""The isolated sender has a bounded, secret-free process and HTTP contract."""
from __future__ import annotations

import base64
from datetime import UTC, datetime
import importlib.util
import io
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest


@pytest.fixture
def sender():
    path = Path(__file__).resolve().parents[2] / "components/web-push-sender/send.py"
    spec = importlib.util.spec_from_file_location("isolated_web_push_sender", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def document(tmp_path):
    key = tmp_path / "vapid.pem"
    key.write_text("private key fixture")
    key.chmod(0o600)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return {"subscription": {"endpoint": "https://fcm.googleapis.com/wp/private-endpoint",
                             "keys": {"p256dh": encode(b"\x04" + b"x" * 64),
                                      "auth": encode(b"a" * 16)}},
            "payload": {"title": "Hermes", "event_id": "event-1"},
            "vapid_private_key_path": str(key),
            "vapid_subject": "https://example.com", "ttl": 300}


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/wp/a", "https://127.0.0.1/wp/a",
    "https://fcm.googleapis.com.evil.test/wp/a", "https://fcm.googleapis.com:444/wp/a",
    "https://user@fcm.googleapis.com/wp/a", "https://fcm.googleapis.com/wp/a#private",
    "https://fcm.googleapis.com\\@127.0.0.1/wp/a", "https://fcm.googleapis.com\n/wp/a",
])
def test_endpoint_cannot_change_transport_destination(sender, document, endpoint):
    document["subscription"]["endpoint"] = endpoint
    with pytest.raises(ValueError):
        sender.validate(document)


def test_input_and_output_are_bounded_and_do_not_disclose_failures(sender, document, monkeypatch, capsys):
    def fail(_document):
        print("private endpoint and secret")
        raise RuntimeError("private endpoint and secret")

    monkeypatch.setattr(sender, "send", fail)
    output = io.StringIO()
    assert sender.main(io.BytesIO(json.dumps(document).encode()), output) == 0
    assert json.loads(output.getvalue()) == sender.result(error_code="sender_error")
    assert len(output.getvalue()) < 256
    assert capsys.readouterr() == ("", "")
    output = io.StringIO()
    sender.main(io.BytesIO(b"x" * (sender.MAX_INPUT + 1)), output)
    assert json.loads(output.getvalue())["error_code"] == "invalid_input"


@pytest.mark.parametrize("field,value", [("ttl", True), ("ttl", -1), ("ttl", 2419201),
                                         ("payload", {"body": "x" * 3073}),
                                         ("vapid_subject", None), ("vapid_subject", ""),
                                         ("vapid_subject", "http://example.com"),
                                         ("vapid_subject", "https://example.com/"),
                                         ("vapid_subject", "https://example.com/contact"),
                                         ("vapid_subject", "https://example.com?contact=ops"),
                                         ("vapid_subject", "https://user@example.com"),
                                         ("vapid_subject", "https://example.com:"),
                                         ("vapid_subject", "https://example.com:65536"),
                                         ("vapid_subject", "mailto:not-an-address"),
                                         ("vapid_private_key_path", "relative.pem")])
def test_malformed_or_oversized_input_is_refused(sender, document, field, value):
    document[field] = value
    with pytest.raises(ValueError):
        sender.validate(document)


def test_private_key_must_be_a_private_regular_file(sender, document, tmp_path):
    key = Path(document["vapid_private_key_path"])
    key.chmod(0o644)
    with pytest.raises(ValueError):
        sender.validate(document)
    key.chmod(0o600)
    linked = tmp_path / "linked.pem"
    linked.symlink_to(key)
    document["vapid_private_key_path"] = str(linked)
    with pytest.raises(ValueError):
        sender.validate(document)


def test_redirect_and_environment_proxy_are_not_followed(sender, monkeypatch):
    import requests

    requests_made = []
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    with sender.guarded_session() as session:
        def redirect(request, **kwargs):
            requests_made.append((request.url, kwargs))
            response = requests.Response()
            response.status_code = 302
            response.headers["Location"] = "http://127.0.0.1/private"
            response._content = b""
            response.request = request
            return response

        monkeypatch.setattr(session.adapters["https://"], "send", redirect)
        response = session.post("https://fcm.googleapis.com/wp/test", data=b"encrypted")
        assert response.status_code == 302
        assert not response.history
        assert not session.trust_env
    assert len(requests_made) == 1
    assert requests_made[0][1]["proxies"] == {}
    assert requests_made[0][1]["timeout"] == 10


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_connection_rejects_private_dns_answers_before_opening_socket(sender, monkeypatch, address):
    from urllib3.exceptions import NewConnectionError

    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(family, socket.SOCK_STREAM, 6, "", (address, 443))])
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("private IP must not be connected"))
    with sender.guarded_session() as session:
        pool = session.adapters["https://"].poolmanager.connection_from_url("https://fcm.googleapis.com")
        with pytest.raises(NewConnectionError):
            pool.ConnectionCls("fcm.googleapis.com", 443)._new_conn()


def test_connection_uses_the_validated_address_without_another_lookup(sender, monkeypatch):
    connected = []
    calls = []
    address = ("142.250.191.106", 443)

    def resolve(*args, **kwargs):
        calls.append(args)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", address)]

    class Connection:
        def settimeout(self, value):
            assert value == 10

        def setsockopt(self, *args):
            pass

        def connect(self, target):
            connected.append(target)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", lambda *a: Connection())
    with sender.guarded_session() as session:
        pool = session.adapters["https://"].poolmanager.connection_from_url("https://fcm.googleapis.com")
        pool.ConnectionCls("fcm.googleapis.com", 443)._new_conn()
    assert connected == [address]
    assert len(calls) == 1


def test_response_body_is_bounded_before_pywebpush_reads_it(sender, monkeypatch):
    import requests

    limits = []

    class Raw(io.BytesIO):
        def read(self, amount, **kwargs):
            limits.append(amount)
            return super().read(amount)

    def response(*args, **kwargs):
        assert kwargs["stream"] is True
        assert kwargs["verify"] is True
        reply = requests.Response()
        reply.status_code = 503
        reply.raw = Raw(b"x" * 10000)
        return reply

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", response)
    with sender.guarded_session() as session:
        reply = session.post("https://fcm.googleapis.com/wp/test", data=b"encrypted")
        assert len(reply.content) == sender.MAX_RESPONSE
        assert sender.response_result(reply)["error_code"] == "service_unavailable"
    assert limits == [sender.MAX_RESPONSE]


@pytest.mark.parametrize("subject", ["https://example.com", "mailto:push@example.com"])
def test_real_encryption_uses_dedicated_transport_and_returns_only_metadata(sender, document, monkeypatch, subject):
    import requests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    document["vapid_subject"] = subject
    private_key = ec.generate_private_key(ec.SECP256R1())
    Path(document["vapid_private_key_path"]).write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    document["subscription"]["keys"]["p256dh"] = base64.urlsafe_b64encode(public).decode().rstrip("=")
    attempts = []

    def respond(_adapter, request, **kwargs):
        attempts.append(request)
        assert request.headers["Authorization"].startswith("vapid ")
        parameters = dict(part.strip().split("=", 1) for part in request.headers["Authorization"][6:].split(","))
        encoded_claims = parameters["t"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded_claims + "=" * (-len(encoded_claims) % 4)))
        assert claims["sub"] == subject
        assert b"event-1" not in request.body
        reply = requests.Response()
        reply.status_code = 201
        reply.raw = io.BytesIO()
        return reply

    # BytesIO has no decode_content parameter; the transport test above covers
    # bounded reading, while this fixture supplies requests' raw interface.
    class Raw(io.BytesIO):
        def read(self, amount, **kwargs):
            return super().read(amount)

    original = respond

    def raw_response(*args, **kwargs):
        reply = original(*args, **kwargs)
        reply.raw = Raw()
        return reply

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", raw_response)
    output = io.StringIO()
    sender.main(io.BytesIO(json.dumps(document).encode()), output)
    assert json.loads(output.getvalue()) == sender.result(ok=True, status=201)
    assert len(attempts) == 1


def test_retry_after_and_expired_subscription_metadata(sender):
    assert sender.response_result(SimpleNamespace(status_code=410, headers={})) == sender.result(
        status=410, error_code="subscription_gone")
    now = datetime(2026, 11, 9, tzinfo=UTC)
    assert sender.retry_delay("Mon, 09 Nov 2026 00:01:00 GMT", now=now) == 60
    assert sender.retry_delay("120") == 120
    assert sender.retry_delay("NaN") is None
