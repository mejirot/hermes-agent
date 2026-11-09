"""Rebind the fork source to the prepared official environment at image build time."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

BASE_IMAGE = "nousresearch/hermes-agent@sha256:adb7890916de63245523492be8f68f9d2095fecbec4e2a6dab7960381f0fc002"
UPSTREAM_COMMIT = "2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19"
CORE_INPUTS = ("pyproject.toml", "uv.lock", "pm/lock.json", "package-lock.json")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_inputs(root: Path, metadata: dict, commit: str) -> None:
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("invalid source commit")
    if metadata.get("fork_commit") != commit or metadata.get("base_image") != BASE_IMAGE:
        raise ValueError("source or base image does not match build inputs")
    if metadata.get("upstream_commit") != UPSTREAM_COMMIT:
        raise ValueError("unexpected upstream source")
    recorded = metadata.get("core_inputs", {})
    if set(recorded) != set(CORE_INPUTS):
        raise ValueError("incomplete protected input inventory")
    for name in CORE_INPUTS:
        if file_hash(root / name) != recorded[name]:
            raise ValueError(f"protected input changed: {name}")
    component = root / "components/web-push-sender"
    if file_hash(component / "uv.lock") != metadata.get("sender_lock_sha256"):
        raise ValueError("sender lock does not match build inputs")


def assemble_image(root: Path, sender: Path, commit: str) -> None:
    metadata = json.loads((sender / "source-inputs.json").read_text(encoding="utf-8"))
    verify_inputs(root, metadata, commit)
    if file_hash(sender / "uv.lock") != metadata["sender_lock_sha256"]:
        raise ValueError("prepared sender uses a different lock")
    stamp_path = root / "install-stamp.json"
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    if (stamp.get("commit") != commit or stamp.get("distribution") != "docker"
            or stamp.get("updateMechanism") != "external" or stamp.get("dirty") is not False):
        raise ValueError("fork image stamp is incomplete")
    stamp["pmRuntime"] = str(root / "pm-runtime")
    stamp_path.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    sys.path.insert(0, str(root))
    from docker.build_agent import assemble_image as assemble

    assemble(root)
    marker = {"schema": 1, "deployment_kind": "image", "manager": "docker",
              "image": "mejirot/hermes-agent", "base_image": BASE_IMAGE,
              "version": "0.23.0", "revision": commit}
    Path("/etc/hermes/image-provenance.json").write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    assemble_image(Path("/opt/hermes"), Path("/opt/hermes-web-push"), args.commit)
