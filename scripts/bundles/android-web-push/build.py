"""Build and export the Android Web Push image and renderer from a clean WSL2 clone.

This is an operator-invoked build, never part of container startup. Failed or
unexecuted gates cannot produce release.json. No production data is consumed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
BASE_IMAGE = "nousresearch/hermes-agent@sha256:adb7890916de63245523492be8f68f9d2095fecbec4e2a6dab7960381f0fc002"
UPSTREAM_COMMIT = "2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19"
PROTECTED = ("pyproject.toml", "uv.lock", "pm/lock.json", "package-lock.json")
LEGACY_UI_TESTS = (
    "src/plugins/hermes-bots/cron-history.test.tsx",
    "src/plugins/hermes-bots/cron-pane.test.tsx",
    "src/sdk/profile-routing.test.ts",
    "src/plugins/hermes-bots/canonical-chat-registry.test.ts",
    "src/plugins/hermes-bots/canonical-chat-creation.test.ts",
    "src/plugins/hermes-bots/canonical-chat-adopt-on-conflict.test.ts",
    "src/plugins/hermes-bots/bot-row-opens-canonical-chat.test.ts",
    "src/plugins/hermes-bots/hide-bot-chats.test.ts",
)
PUSH_UI_TESTS = (
    "src/lib/web-push.test.ts", "src/lib/web-push-observation.test.ts",
    "src/store/web-push.test.ts", "src/app/settings/web-push-settings.test.tsx",
    "src/app/push-notification-open.test.tsx", "src/lib/browser-notifications.test.ts",
    "src/lib/web-push-worker.test.ts",
)
DOCKER_ROUTING_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY",
                      "DOCKER_CERT_PATH", "DOCKER_TLS_CERTDIR", "DOCKER_SSH_COMMAND",
                      "BUILDX_BUILDER", "BUILDKIT_HOST", "BUILDX_CONFIG")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def require_host() -> None:
    release = Path("/proc/sys/kernel/osrelease")
    if (sys.platform != "linux" or not release.is_file()
            or not re.search(r"microsoft.*(?:standard|WSL2)|WSL2", release.read_text(), re.I)):
        raise ValueError("Run this build in WSL2 on the Windows management PC")
    if sys.version_info[:2] != (3, 14) or platform.machine() != "x86_64":
        raise ValueError("The build host needs CPython 3.14 on Linux x86_64")
    if str(ROOT).startswith("/mnt/"):
        raise ValueError("Keep the clean clone in the WSL Linux filesystem")


def local_socket_endpoint(value: object) -> str:
    if not isinstance(value, str) or any(ord(char) <= 32 for char in value) or "%" in value:
        raise ValueError("Docker endpoint must be a local Unix socket")
    endpoint = urlsplit(value)
    if (endpoint.scheme != "unix" or endpoint.netloc or endpoint.query or endpoint.fragment
            or not Path(endpoint.path).is_absolute() or "\\" in endpoint.path):
        raise ValueError("Remote Docker endpoints are refused; use local Docker Engine in WSL")
    if not Path(endpoint.path).is_socket():
        raise ValueError("The selected local Docker socket does not exist")
    return "unix://" + endpoint.path


def local_docker_command(env: dict) -> list[str]:
    """Inspect local CLI configuration before contacting any daemon, then pin its socket.

    A WSL shell alone proves nothing about Docker's target. Both the selected
    context and any endpoint override must be local; conflicting TLS/SSH settings
    are refused. Later commands explicitly name the verified socket, so a context
    switch during the build cannot redirect them to the production VPS.
    """
    if any(env.get(key) for key in DOCKER_ROUTING_ENV[2:]):
        raise ValueError("Clear Docker TLS/SSH and custom BuildKit builder overrides before a local build")
    override = env.get("DOCKER_HOST")
    if override:
        local_socket_endpoint(override)
    context = subprocess.check_output(["docker", "context", "show"], env=env, text=True).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", context):
        raise ValueError("Invalid selected Docker context")
    selected = json.loads(subprocess.check_output(
        ["docker", "context", "inspect", context], env=env, text=True))
    if not isinstance(selected, list) or len(selected) != 1:
        raise ValueError("Cannot determine the selected Docker endpoint")
    record = selected[0]
    endpoint = record.get("Endpoints", {}).get("docker", {})
    if endpoint.get("SkipTLSVerify") or record.get("TLSMaterial"):
        raise ValueError("Docker context contains TLS configuration; use a local Unix socket context")
    host = local_socket_endpoint(endpoint.get("Host"))
    if override and not env.get("DOCKER_CONTEXT"):
        host = local_socket_endpoint(override)
    return ["docker", "--host", host]


def require_source_environment() -> None:
    for directory in (ROOT, ROOT / "apps/desktop"):
        if any(path.name not in (".env.example", ".envrc") for path in directory.glob(".env*")):
            raise ValueError("Use a dedicated build clone without local dotenv files")


def require_source(commit: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or git("rev-parse", "HEAD") != commit:
        raise ValueError("Checkout HEAD must equal the requested full commit")
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("The build requires a clean, committed source tree")
    subprocess.run(["git", "merge-base", "--is-ancestor", UPSTREAM_COMMIT, commit], cwd=ROOT, check=True)
    changed = git("diff", "--name-only", UPSTREAM_COMMIT, commit).splitlines()
    if any(name in ("hermes_bootstrap.py", "Dockerfile") or name.startswith(("pm/", "docker/"))
           for name in changed):
        raise ValueError("The feature must retain the original PM, bootstrap and Docker runtime")
    require_source_environment()
    hashes = {}
    for name in PROTECTED:
        baseline = subprocess.check_output(["git", "show", f"{UPSTREAM_COMMIT}:{name}"], cwd=ROOT)
        if (ROOT / name).read_bytes() != baseline:
            raise ValueError(f"The isolated sender must not change {name}")
        hashes[name] = hashlib.sha256(baseline).hexdigest()
    for name in (*LEGACY_UI_TESTS, *PUSH_UI_TESTS):
        if not (ROOT / "apps/desktop" / name).is_file():
            raise ValueError(f"Required regression test is missing: {name}")
    return {"fork_commit": commit, "upstream_commit": UPSTREAM_COMMIT,
            "base_image": BASE_IMAGE, "core_inputs": hashes,
            "sender_lock_sha256": digest(ROOT / "components/web-push-sender/uv.lock")}


def unpack_renderer(archive: Path, destination: Path) -> None:
    """Accept a previous public renderer; refuse links, duplicates and escaped paths."""
    if destination.exists():
        raise ValueError("Renderer output already exists; use a clean build clone")
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if len(members) > 100000 or sum(member.size for member in members) > 1024 * 1024 * 1024:
            raise ValueError("Renderer archive is too large")
        names = set()
        root_name = None
        for member in members:
            name = PurePosixPath(member.name)
            if (not (member.isfile() or member.isdir()) or name.is_absolute()
                    or ".." in name.parts or not name.parts
                    or name.parts[0] not in ("webapp", "base-webapp")
                    or "\\" in member.name or name.as_posix() in names):
                raise ValueError("Unsafe or duplicate renderer member")
            names.add(name.as_posix())
            if root_name is None:
                root_name = name.parts[0]
            if name.parts[0] != root_name:
                raise ValueError("Renderer archive has multiple roots")
        destination.mkdir(parents=True)
        for member in members:
            parts = PurePosixPath(member.name).parts[1:]
            if not parts:
                if not member.isdir():
                    raise ValueError("Renderer root must be a directory")
                continue
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as data, target.open("xb") as output:
                    shutil.copyfileobj(data, output)
                target.chmod(0o644)
    if not all((destination / name).is_file() for name in ("index.html", "hermes-build.json")):
        raise ValueError("Base renderer is missing its entry point or receipt")


def write_archive(directory: Path, output: Path) -> None:
    with output.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in [directory, *sorted(directory.rglob("*"))]:
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError("Renderer output contains a non-regular path")
                info = archive.gettarinfo(str(path), arcname=path.relative_to(directory.parent).as_posix())
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if path.is_dir() else 0o644
                if path.is_file():
                    with path.open("rb") as data:
                        archive.addfile(info, data)
                else:
                    archive.addfile(info)


def prepare_context(commit: str, output: Path, renderer: Path, inputs: dict) -> Path:
    context = output / "context"
    context.mkdir()
    snapshot = context / "snapshot"
    snapshot.mkdir()
    archive = output / "source.tar"
    with archive.open("xb") as stream:
        subprocess.run(["git", "archive", "--format=tar", commit], cwd=ROOT, stdout=stream, check=True)
    with tarfile.open(archive) as source:
        # git archive supplies tracked source only: no credentials, node_modules,
        # venv, local outputs or mutable PM state from the management computer.
        for member in source.getmembers():
            name = PurePosixPath(member.name)
            if name.parts[0] in ("tests", "tests-js", "website", ".github", ".hermes"):
                continue
            source.extract(member, snapshot, filter="data")
    test_source = context / "test-source"
    test_source.mkdir()
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            if PurePosixPath(member.name).parts[0] == "tests":
                source.extract(member, test_source, filter="data")
    archive.unlink()
    names = subprocess.check_output(["git", "ls-files", "-z", "*.py"], cwd=ROOT).split(b"\0")
    present = [name for name in names if name and ((snapshot / os.fsdecode(name)).is_file()
                                                  or (test_source / os.fsdecode(name)).is_file())]
    (context / "tracked-python").write_bytes(b"\0".join(present) + b"\0")
    shutil.copytree(renderer, context / "renderer")
    shutil.copy2(ROOT / "scripts/bundles/android-web-push/Dockerfile", context / "Dockerfile")
    (context / "source-inputs.json").write_text(json.dumps(inputs, indent=2) + "\n", encoding="utf-8")
    return context


class Gates:
    def __init__(self, output: Path, env: dict):
        self.output, self.env, self.results = output, env, []

    def run(self, name: str, args: list[str], *, stdin: str | None = None) -> None:
        log = self.output / "logs" / f"{name}.log"
        started = datetime.now(timezone.utc).isoformat()
        with log.open("w", encoding="utf-8") as stream:
            completed = subprocess.run(args, cwd=ROOT, env=self.env, input=stdin,
                                       text=True, stdout=stream, stderr=subprocess.STDOUT)
        self.results.append({"name": name, "returncode": completed.returncode,
                             "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
                             "log_sha256": digest(log)})
        (self.output / "build-gates.json").write_text(json.dumps(self.results, indent=2) + "\n")
        if completed.returncode:
            raise RuntimeError(f"Gate failed: {name}; inspect {log}")
        print(f"PASS {name}", flush=True)


def prepare_official_image(gates: Gates, docker: list[str]) -> dict:
    # BuildKit's content cache is separate from the Engine image store. Pull the
    # exact digest first so inspect and the following build share a known baseline.
    gates.run("official-image-pull", [*docker, "image", "pull", "--platform=linux/amd64", BASE_IMAGE])
    gates.run("official-image-inspect", [*docker, "image", "inspect", BASE_IMAGE])
    raw = (gates.output / "logs/official-image-inspect.log").read_text(encoding="utf-8")
    records = json.loads(raw)
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("Official image inspection was incomplete")
    official = records[0]
    if official.get("Architecture") != "amd64" or official.get("Os") != "linux":
        raise ValueError("Official image must target Linux amd64")
    (gates.output / "official-image.json").write_text(json.dumps(official, indent=2) + "\n", encoding="utf-8")
    return official


def link_workspace() -> None:
    for link, target in ((ROOT / "node_modules", ROOT / ".build/webapp-workspace/node_modules"),
                         (ROOT / "apps/desktop/node_modules", ROOT / ".build/webapp-workspace/apps/desktop/node_modules")):
        if not target.is_dir():
            if link.name == "node_modules" and link.parent == ROOT:
                raise ValueError("Upstream's prepared dependency workspace is missing")
            continue
        if link.is_symlink():
            if link.resolve() != target.resolve():
                raise ValueError("Existing dependency symlink points outside the prepared workspace")
        elif link.exists():
            raise ValueError("Use the upstream prepared workspace, not a source dependency install")
        else:
            link.symlink_to(os.path.relpath(target, link.parent), target_is_directory=True)


def build(args) -> None:
    require_host()
    inputs = require_source(args.commit)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,79}", args.release):
        raise ValueError("Invalid release id")
    if not re.fullmatch(r"[0-9a-f]{64}", args.base_sha256) or digest(args.base_renderer) != args.base_sha256:
        raise ValueError("Base renderer hash does not match")
    output = args.out.resolve()
    if output.exists() or output.is_relative_to(ROOT):
        raise ValueError("Choose a new artifact directory outside the source clone")
    output.mkdir(parents=True, mode=0o700)
    (output / "logs").mkdir()
    env = {key: value for key, value in os.environ.items()
           if key not in ("NODE_OPTIONS", "NODE_PATH", "NODE_ENV", "npm_execpath", "HERMES_BUILD_COMMIT",
                          "HERMES_PAYLOAD_TAG", "HERMES_DESKTOP_VARIANT")
           and not key.startswith(("VITE_", "npm_config_", "NPM_CONFIG_"))}
    env.update(CI="1", ELECTRON_SKIP_BINARY_DOWNLOAD="1", HERMES_HOME=str(output / "test-home"),
               HERMES_RUNTIME_DIR=str(output / "test-runtime"))
    docker = local_docker_command(env)
    for key in DOCKER_ROUTING_ENV:
        env.pop(key, None)
    env["DOCKER_BUILDKIT"] = "1"
    gates = Gates(output, env)
    gates.run("node-toolchain", ["node", "--input-type=module", "-e",
        "if(process.version!=='v26.12.1'||process.platform!=='linux'||process.arch!=='x64')throw Error('Need Linux Node 26.12.1 x64')"])
    npm = subprocess.check_output(["npm", "--version"], env=env, text=True).strip()
    if npm != "12.0.2":
        raise ValueError("Need npm 12.0.2")
    gates.run("local-docker-daemon", [*docker, "info", "--format", "{{json .}}"])
    docker_info = json.loads((output / "logs/local-docker-daemon.log").read_text(encoding="utf-8"))
    if docker_info.get("OSType") != "linux" or docker_info.get("Architecture") not in ("x86_64", "amd64"):
        raise ValueError("Docker Engine must target Linux amd64")
    official = prepare_official_image(gates, docker)
    gates.run("frontend-dependencies", ["node", "apps/desktop/scripts/build-webapp.mjs", "--deps-only"])
    link_workspace()
    gates.run("legacy-ui", ["npm", "run", "test:ui", "--workspace", "apps/desktop", "--", "--maxWorkers=1", *LEGACY_UI_TESTS])
    gates.run("web-push-ui", ["npm", "run", "test:ui", "--workspace", "apps/desktop", "--", "--maxWorkers=1", *PUSH_UI_TESTS])
    gates.run("typecheck", ["npm", "run", "typecheck", "--workspace", "apps/desktop"])
    gates.run("lint", ["npm", "run", "lint", "--workspace", "apps/desktop"])
    dist = ROOT / "apps/desktop/dist-webapp"
    unpack_renderer(args.base_renderer, dist)
    gates.run("renderer-build", ["node", "apps/desktop/scripts/build-webapp.mjs", "--no-install"])
    gates.run("renderer-receipt", ["node", "--input-type=module"], stdin="""
import { readFileSync } from 'node:fs';
import { buildInputs, outputIntact } from './scripts/build/freshness.mjs';
const dist='apps/desktop/dist-webapp', receipt=JSON.parse(readFileSync(`${dist}/hermes-build.json`));
if(!outputIntact(dist,'webapp') || receipt.node!=='26.12.1' ||
   JSON.stringify(receipt.inputs)!==JSON.stringify(buildInputs(process.cwd(),'webapp'))) {
  throw Error('Renderer source/output receipt does not match this build');
}
""")
    require_source(args.commit)
    shutil.copytree(dist, output / "webapp")
    write_archive(output / "webapp", output / "webapp.tar.gz")
    context = prepare_context(args.commit, output, dist, inputs)
    image_tag = "mejirot/hermes-agent:" + args.release.lower()
    command = [*docker, "build", "--builder", "default", "--platform=linux/amd64",
               "--build-arg", f"FORK_COMMIT={args.commit}"]
    gates.run("python-sender-and-backend-tests", [*command, "--target", "tests", str(context)])
    gates.run("image-build", [*command, "--target", "runtime", "--tag", image_tag, str(context)])
    inspect = json.loads(subprocess.check_output([*docker, "image", "inspect", image_tag], env=env, text=True))[0]
    for field in ("Entrypoint", "Cmd", "User", "WorkingDir"):
        if inspect["Config"].get(field) != official["Config"].get(field):
            raise ValueError(f"Official runtime contract changed: {field}")
    if inspect.get("Architecture") != "amd64" or inspect.get("Os") != "linux":
        raise ValueError("Built image has the wrong platform")
    container = subprocess.check_output([*docker, "container", "create", "--network=none", image_tag], env=env, text=True).strip()
    try:
        gates.run("sender-receipt", [*docker, "container", "cp",
                  f"{container}:/opt/hermes-web-push/sender-build.json", str(output / "sender-build.json")])
    finally:
        subprocess.run([*docker, "container", "rm", "--volumes", container], env=env,
                       check=True, stdout=subprocess.DEVNULL)
    sender_receipt = json.loads((output / "sender-build.json").read_text(encoding="utf-8"))
    if (sender_receipt.get("lock_sha256") != inputs["sender_lock_sha256"]
            or sender_receipt.get("script_sha256") != digest(ROOT / "components/web-push-sender/send.py")):
        raise ValueError("Prepared sender receipt does not match the canonical source")
    gates.run("image-export", [*docker, "image", "save", "--output", str(output / "image.tar"), image_tag])
    require_source(args.commit)
    release = {"schema": 1, "release_id": args.release, **inputs, "upstream_version": "v0.23.0",
               "node": "v26.12.1", "npm": npm, "platform": "linux/amd64", "image_tag": image_tag,
               "image_id": inspect["Id"], "image_archive_sha256": digest(output / "image.tar"),
               "base_image_id": official["Id"], "official_inspection_sha256": digest(output / "official-image.json"),
               "archive_sha256": digest(output / "webapp.tar.gz"), "base_renderer_archive_sha256": args.base_sha256,
               "sender_version": "pywebpush==2.5.0", "gates": gates.results,
               "sender_build": sender_receipt,
               "production_pm_clone_startup": "NOT_RUN_REQUIRES_PRIVATE_OPERATOR_ACCEPTANCE",
               "webapp_files": {p.relative_to(dist).as_posix(): digest(p) for p in sorted(dist.rglob("*")) if p.is_file()}}
    (output / "release.json").write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS").write_text("".join(f"{digest(output/name)}  {name}\n"
                                                for name in ("image.tar", "webapp.tar.gz", "release.json",
                                                             "sender-build.json", "official-image.json")))
    print(f"Candidate artifacts: {output}. Production startup acceptance is still required.")


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--base-renderer", type=Path, required=True)
    parser.add_argument("--base-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    build(parser.parse_args())
