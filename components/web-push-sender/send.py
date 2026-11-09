"""One bounded Web Push attempt. No Hermes imports or dependency installation.

The parent supplies one JSON document on stdin. Only the small result document
is written to stdout; credentials, endpoints, response bodies and exceptions
never cross that output boundary.
"""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import socket
import stat
import sys
from urllib.parse import urlsplit

FCM_HOST = "fcm.googleapis.com"
VAPID_SUBJECT = "https://mejilab.com"
TIMEOUT = 10
MAX_INPUT = 16384
MAX_PAYLOAD = 3072
MAX_RESPONSE = 4096


def result(ok: bool = False, status: int | None = None,
           retry_after: float | None = None, error_code: str | None = None) -> dict:
    return {"ok": ok, "status": status, "retry_after": retry_after, "error_code": error_code}


def allowed_endpoint(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        return False
    if any(ord(char) <= 32 or ord(char) >= 127 for char in value) or "\\" in value:
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.hostname == FCM_HOST
                and url.port in (None, 443) and url.username is None
                and url.password is None and not url.fragment
                and url.netloc.lower() in (FCM_HOST, FCM_HOST + ":443")
                and url.path.startswith("/") and len(url.path) > 1)
    except ValueError:
        return False


def _decoded_key(value: object, length: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("invalid subscription key")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if any(char not in alphabet for char in value):
        raise ValueError("invalid subscription key")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != length:
        raise ValueError("invalid subscription key")
    return decoded


def validate(document: object) -> dict:
    if not isinstance(document, dict):
        raise ValueError("invalid input")
    subscription = document.get("subscription")
    if not isinstance(subscription, dict) or not allowed_endpoint(subscription.get("endpoint")):
        raise ValueError("invalid subscription")
    keys = subscription.get("keys")
    if not isinstance(keys, dict) or _decoded_key(keys.get("p256dh"), 65)[0] != 4:
        raise ValueError("invalid subscription key")
    _decoded_key(keys.get("auth"), 16)
    if not isinstance(document.get("payload"), dict):
        raise ValueError("invalid payload")
    data = json.dumps(document["payload"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(data.encode("utf-8")) > MAX_PAYLOAD:
        raise ValueError("payload too large")
    ttl = document.get("ttl")
    if type(ttl) is not int or not 0 <= ttl <= 2419200:
        raise ValueError("invalid ttl")
    if document.get("vapid_subject") != VAPID_SUBJECT:
        raise ValueError("invalid vapid subject")
    key_path = document.get("vapid_private_key_path")
    if not isinstance(key_path, str) or not Path(key_path).is_absolute():
        raise ValueError("invalid key path")
    info = Path(key_path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not 1 <= info.st_size <= 8192:
        raise ValueError("invalid private key file")
    return {"subscription": {"endpoint": subscription["endpoint"],
                             "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}},
            "data": data, "key_path": key_path, "ttl": ttl}


def retry_delay(value: object, *, now: datetime | None = None) -> float | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            delay = (date - (now or datetime.now(timezone.utc))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(86400.0, max(0.0, delay)) if math.isfinite(delay) else None


def response_result(response: object) -> dict:
    status = getattr(response, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        return result(error_code="transport_error")
    if 200 <= status <= 299:
        return result(ok=True, status=status)
    code = ("subscription_gone" if status in (404, 410) else
            "rate_limited" if status == 429 else
            "service_unavailable" if status >= 500 else "http_error")
    retry = retry_delay(getattr(response, "headers", {}).get("Retry-After"))
    return result(status=status, retry_after=retry, error_code=code)


def guarded_session():
    """Keep TLS host verification while connecting only to validated public IPs.

    Validation happens on the exact sockaddr passed to connect, so a second DNS
    lookup cannot redirect the encrypted payload to a private address. Proxies,
    redirects and adapter retries are disabled for this single attempt.
    """
    import requests
    from urllib3.connection import HTTPSConnection
    from urllib3.connectionpool import HTTPSConnectionPool
    from urllib3.exceptions import NewConnectionError

    class PublicConnection(HTTPSConnection):
        def _new_conn(self):
            if self.host != FCM_HOST or self.port != 443:
                raise NewConnectionError(self, "endpoint refused")
            try:
                addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                    raise OSError("non-public address refused")
            except (OSError, ValueError) as exc:
                raise NewConnectionError(self, "endpoint resolution refused") from exc
            for family, kind, protocol, _name, address in addresses:
                connection = socket.socket(family, kind, protocol)
                try:
                    connection.settimeout(TIMEOUT)
                    for option in self.socket_options or ():
                        connection.setsockopt(*option)
                    connection.connect(address)
                    return connection
                except OSError:
                    connection.close()
            raise NewConnectionError(self, "endpoint connection failed")

    class PublicPool(HTTPSConnectionPool):
        ConnectionCls = PublicConnection

    class FCMAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {"https": PublicPool}

        def send(self, request, **kwargs):
            if request.method != "POST" or not allowed_endpoint(request.url):
                raise requests.exceptions.InvalidURL("endpoint refused")
            response = super().send(request, stream=True, timeout=TIMEOUT,
                                    verify=True, cert=None, proxies={})
            # pywebpush inspects response.text even when verbose=False. Cache only
            # this bounded prefix, then release the socket without reading the rest.
            try:
                response._content = response.raw.read(MAX_RESPONSE, decode_content=True)
                response._content_consumed = True
            finally:
                response.close()
            return response

    class FCMSession(requests.Session):
        def request(self, method, url, **kwargs):
            if method.upper() != "POST" or not allowed_endpoint(url):
                raise requests.exceptions.InvalidURL("endpoint refused")
            kwargs.update(allow_redirects=False, timeout=TIMEOUT, proxies={})
            return super().request(method, url, **kwargs)

    session = FCMSession()
    session.trust_env = False
    session.adapters.clear()
    session.mount("https://", FCMAdapter(max_retries=0))
    return session


def send(document: dict) -> dict:
    from pywebpush import WebPushException, webpush
    import requests

    with guarded_session() as session:
        try:
            response = webpush(subscription_info=document["subscription"], data=document["data"],
                               vapid_private_key=document["key_path"],
                               vapid_claims={"sub": VAPID_SUBJECT}, ttl=document["ttl"],
                               timeout=TIMEOUT, requests_session=session, verbose=False)
            return response_result(response)
        except WebPushException as exc:
            return (response_result(exc.response) if exc.response is not None
                    else result(error_code="encryption_error"))
        except requests.exceptions.Timeout:
            return result(error_code="timeout")
        except requests.exceptions.RequestException:
            return result(error_code="transport_error")


def main(input_stream=None, output_stream=None) -> int:
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout
    answer = result(error_code="invalid_input")
    try:
        raw = input_stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("input too large")
        document = validate(json.loads(raw))
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            answer = send(document)
    except (ValueError, TypeError, KeyError, OSError):
        pass
    except Exception:
        answer = result(error_code="sender_error")
    output_stream.write(json.dumps(answer, separators=(",", ":"), allow_nan=False) + "\n")
    output_stream.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
