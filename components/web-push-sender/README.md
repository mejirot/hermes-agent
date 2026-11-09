# Isolated Android Web Push sender

This component owns its Python dependencies. It is outside Hermes's dependency
graph and does not import Hermes, invoke PM, discover plugins or change a runtime
environment. The image prepares its sealed environment in WSL2 at
`/opt/hermes-web-push`; production runs only the prepared interpreter.

The fixed process contract is:

```text
/opt/hermes-web-push/bin/python -I -B /opt/hermes-web-push/send.py
```

Send exactly one JSON document on stdin, then close stdin:

```json
{
  "subscription": {
    "endpoint": "https://fcm.googleapis.com/wp/REDACTED",
    "keys": {"p256dh": "BASE64URL_UNCOMPRESSED_P256_POINT", "auth": "BASE64URL_16_BYTES"}
  },
  "payload": {"id": "event-id", "title": "Hermes", "body": "A new update is available"},
  "vapid_private_key_path": "/opt/data/web-push/vapid-private.pem",
  "vapid_subject": "https://example.com",
  "ttl": 300
}
```

The private key must be an absolute, regular file, with no group/other access;
symlinks are refused. No secret belongs in argv, image layers or source control.
The VAPID subject identifies the operator's contact URI. Configure the operator's
own contact in the serving Webapp home's private `config.yaml`; the worker passes
that value to the sender. For example, replace this placeholder before use:

```yaml
webapp:
  push:
    enabled: true
    vapid_subject: "https://operator.example"
```

Use either a single `mailto:` contact, such as `mailto:push@operator.example`, or
an HTTPS origin with no path, query or fragment. The pinned library's strict HTTPS
validation requires a host-only URI. This setting is a contact address; it does
not configure the PWA origin or the subscription endpoint. There is no default
contact: an absent or invalid value leaves Push unavailable while the Webapp
remains usable.

When upgrading an existing deployment, set the operator's contact in the private
configuration before deploying the new image, then restart the serving Webapp.
Keep the existing persistent data, VAPID key and browser subscriptions. Changing
only this contact URI does not require key rotation or browser re-enrollment.
The payload is limited to 3,072 UTF-8 bytes and the whole request to 16,384 bytes.
TTL is an integer from zero through 2,419,200 seconds.

The sole stdout line has this shape; callers use the result fields rather than
process exit status to decide whether to retry:

```json
{"ok":true,"status":201,"retry_after":null,"error_code":null}
```

`status` is an HTTP status or null. `retry_after` is a finite delay in seconds,
capped at one day, or null. Error codes are `invalid_input`,
`subscription_gone`, `rate_limited`, `service_unavailable`, `http_error`,
`encryption_error`, `timeout`, `transport_error`, or `sender_error`. Neither
exception details nor response bodies are returned. An expired subscription
has status 404 or 410; the parent owns removal and retry policy.

Only HTTPS on exactly `fcm.googleapis.com`, port 443, is allowed. DNS answers
must all be global addresses; the validated sockaddr is used directly while TLS
still verifies the original hostname. Redirects, environment proxies, adapter
retries and additional destinations are disabled. The HTTP timeout is ten
seconds and the response prefix is limited to 4,096 bytes. The parent must
enforce its 15-second process deadline, including DNS and encryption time.

`pyproject.toml` and `uv.lock` independently pin the runtime graph. Runtime
versions shared with Hermes reuse its existing pins; pywebpush is exactly 2.5.0.
The lock was seeded from PyPI release metadata and published artifact hashes;
**it still requires the pinned PM's `check-lock` on the WSL builder before it is
accepted for a release**. No resolver or install was run on the VPS. Do not
silently fall back to an unlocked build if that gate rejects the seed. Resolve
and review the component lock in WSL, commit it, then restart the clean build.
The main `pyproject.toml`, `uv.lock`, and `pm/lock.json` must remain unchanged.

The [feature build guide](../../scripts/bundles/android-web-push/README.md) describes
the image, test and artifact gates. `receipt.py` inventories the actual isolated
environment and refuses missing, additional or differently versioned packages.

References: [pywebpush 2.5.0 release metadata](https://pypi.org/pypi/pywebpush/2.5.0/json),
[pywebpush source](https://github.com/web-push-libs/pywebpush),
[uv build constraints](https://docs.astral.sh/uv/reference/settings/#build-constraint-dependencies).
