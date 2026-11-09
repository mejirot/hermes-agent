"""One private VAPID key per serving Webapp home; no provider credentials involved."""
from __future__ import annotations

import base64
import os
import stat
import tempfile
from pathlib import Path


def prepare_key(directory: Path) -> tuple[Path, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    path = Path(directory) / "vapid.pem"
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".vapid-", dir=directory)
        key = ec.generate_private_key(ec.SECP256R1())
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Publish only a complete key; another owner's key is never replaced.
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    info = path.lstat()
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        # A crash between link publication and temporary-name removal leaves a
        # complete key with two names. Recover only our own same-inode names.
        for temporary in Path(directory).glob('.vapid-*'):
            leftover = temporary.lstat()
            if leftover.st_ino == info.st_ino and leftover.st_dev == info.st_dev:
                temporary.unlink()
        info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("VAPID key must be a private regular file")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("VAPID key must use P-256")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return path, base64.urlsafe_b64encode(public).decode().rstrip("=")
