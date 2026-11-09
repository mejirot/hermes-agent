"""Artifact packaging refuses unsafe inputs and binds receipts to protected source."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


def load(name):
    path = Path(__file__).resolve().parents[3] / "scripts/bundles/android-web-push" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"web_push_bundle_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_environment_accepts_upstream_examples_and_direnv(tmp_path, monkeypatch):
    build = load("build")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    for directory in (tmp_path, tmp_path / "apps/desktop"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".env.example").write_text("EXAMPLE=value\n")
        (directory / ".envrc").write_text("source ./activate\n")
    build.require_source_environment()


@pytest.mark.parametrize("directory", [".", "apps/desktop"])
@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production", ".envrc.local"])
def test_source_environment_refuses_local_settings(tmp_path, monkeypatch, directory, name):
    build = load("build")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    target = tmp_path / directory
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text("PRIVATE=value\n")
    with pytest.raises(ValueError, match="without local dotenv files"):
        build.require_source_environment()


@pytest.mark.parametrize("name,kind", [("webapp/../escape", "file"), ("/webapp/absolute", "file"),
                                     ("webapp/assets/link", "symlink"),
                                     ("other/index.html", "file"), ("webapp/a", "duplicate")])
def test_previous_renderer_cannot_escape_or_replace_files(tmp_path, name, kind):
    build = load("build")
    archive = tmp_path / "base.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo(name)
        if kind == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, "/tmp/private"
            output.addfile(info)
        else:
            info.size = 1
            output.addfile(info, io.BytesIO(b"x"))
            if kind == "duplicate":
                output.addfile(info, io.BytesIO(b"y"))
    with pytest.raises(ValueError):
        build.unpack_renderer(archive, tmp_path / "dist")
    assert not (tmp_path / "escape").exists()


def test_renderer_archive_round_trip_is_reproducible(tmp_path):
    build = load("build")
    renderer = tmp_path / "webapp"
    (renderer / "assets").mkdir(parents=True)
    (renderer / "index.html").write_text("<script src='assets/app.js'></script>")
    (renderer / "hermes-build.json").write_text("{}")
    (renderer / "assets/app.js").write_text("console.log('public renderer')")
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    build.write_archive(renderer, first)
    build.write_archive(renderer, second)
    assert first.read_bytes() == second.read_bytes()
    restored = tmp_path / "restored"
    build.unpack_renderer(first, restored)
    assert (restored / "assets/app.js").read_bytes() == (renderer / "assets/app.js").read_bytes()
    assert (restored / "index.html").stat().st_mode & 0o777 == 0o644


def test_image_receipt_requires_the_exact_protected_inputs_and_sender_lock(tmp_path):
    image = load("image")
    commit = "a" * 40
    hashes = {}
    for name in image.CORE_INPUTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"unchanged {name}")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    component = tmp_path / "components/web-push-sender"
    component.mkdir(parents=True)
    (component / "uv.lock").write_text("independent sender lock")
    metadata = {"fork_commit": commit, "upstream_commit": image.UPSTREAM_COMMIT,
                "base_image": image.BASE_IMAGE, "core_inputs": hashes,
                "sender_lock_sha256": image.file_hash(component / "uv.lock")}
    image.verify_inputs(tmp_path, metadata, commit)
    (tmp_path / "uv.lock").write_text("accidentally changed main environment")
    with pytest.raises(ValueError, match="protected input changed"):
        image.verify_inputs(tmp_path, metadata, commit)


def test_failed_gate_never_records_a_success(tmp_path, monkeypatch):
    from types import SimpleNamespace

    build = load("build")
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=7))
    gate = build.Gates(tmp_path, {})
    with pytest.raises(RuntimeError, match="Gate failed"):
        gate.run("test", ["irrelevant"])
    assert gate.results[0]["returncode"] == 7
    assert not (tmp_path / "release.json").exists()


@pytest.mark.parametrize("host", ["ssh://operator@vps", "tcp://vps:2376", "https://vps:2376",
                                  "npipe:////./pipe/docker_engine"])
def test_remote_docker_override_is_rejected_before_any_cli_call(monkeypatch, host):
    build = load("build")
    monkeypatch.setattr(build.subprocess, "check_output", lambda *a, **kw: pytest.fail("must refuse before CLI"))
    with pytest.raises(ValueError):
        build.local_docker_command({"DOCKER_HOST": host})


@pytest.mark.parametrize("key", ["DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_TLS_CERTDIR",
                                 "DOCKER_SSH_COMMAND", "BUILDX_BUILDER", "BUILDKIT_HOST", "BUILDX_CONFIG"])
def test_docker_tls_and_ssh_overrides_cannot_reach_build(monkeypatch, key):
    build = load("build")
    monkeypatch.setattr(build.subprocess, "check_output", lambda *a, **kw: pytest.fail("must refuse before CLI"))
    with pytest.raises(ValueError):
        build.local_docker_command({key: "configured"})


def docker_context(monkeypatch, build, endpoint, *, tls=None):
    calls = []

    def inspect(args, **kwargs):
        calls.append(args)
        if args == ["docker", "context", "show"]:
            return "selected\n"
        assert args == ["docker", "context", "inspect", "selected"]
        return json.dumps([{"Name": "selected", "Endpoints": {"docker": {"Host": endpoint}},
                            "TLSMaterial": tls or {}}])

    monkeypatch.setattr(build.subprocess, "check_output", inspect)
    return calls


@pytest.mark.parametrize("endpoint", ["ssh://operator@vps", "tcp://vps:2375"])
def test_selected_remote_context_is_rejected_without_contacting_its_daemon(monkeypatch, endpoint):
    build = load("build")
    calls = docker_context(monkeypatch, build, endpoint)
    with pytest.raises(ValueError):
        build.local_docker_command({"DOCKER_CONTEXT": "selected"})
    assert len(calls) == 2  # Only local context metadata was read, never docker info/build.


def test_local_socket_is_pinned_even_if_context_changes_later(monkeypatch):
    build = load("build")
    endpoint = "unix:///var/run/docker.sock"
    docker_context(monkeypatch, build, endpoint)
    monkeypatch.setattr(Path, "is_socket", lambda path: str(path) == "/var/run/docker.sock")
    command = build.local_docker_command({"DOCKER_CONTEXT": "selected"})
    assert command == ["docker", "--host", endpoint]


def test_local_context_with_remote_host_override_is_rejected(monkeypatch):
    build = load("build")
    docker_context(monkeypatch, build, "unix:///var/run/docker.sock")
    with pytest.raises(ValueError):
        build.local_docker_command({"DOCKER_CONTEXT": "selected", "DOCKER_HOST": "ssh://vps"})


def test_missing_unix_socket_is_rejected(monkeypatch):
    build = load("build")
    docker_context(monkeypatch, build, "unix:///absent/docker.sock")
    monkeypatch.setattr(Path, "is_socket", lambda _path: False)
    with pytest.raises(ValueError, match="does not exist"):
        build.local_docker_command({})


def test_context_tls_material_is_rejected_for_a_unix_endpoint(monkeypatch):
    build = load("build")
    docker_context(monkeypatch, build, "unix:///var/run/docker.sock", tls={"docker": ["ca.pem"]})
    with pytest.raises(ValueError, match="TLS configuration"):
        build.local_docker_command({})


@pytest.mark.parametrize("pull_fails", [False, True])
def test_official_digest_is_pulled_and_inspected_before_any_build(tmp_path, monkeypatch, pull_fails):
    from types import SimpleNamespace

    build = load("build")
    (tmp_path / "logs").mkdir()
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if "pull" in args and pull_fails:
            return SimpleNamespace(returncode=1)
        if "inspect" in args:
            kwargs["stdout"].write(json.dumps([{"Id": "sha256:official", "Os": "linux",
                                               "Architecture": "amd64", "Config": {}}]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build.subprocess, "run", execute)
    gates = build.Gates(tmp_path, {})
    command = ["docker", "--host", "unix:///var/run/docker.sock"]
    if pull_fails:
        with pytest.raises(RuntimeError):
            build.prepare_official_image(gates, command)
        assert len(calls) == 1
        assert not (tmp_path / "official-image.json").exists()
        assert not (tmp_path / "release.json").exists()
    else:
        baseline = build.prepare_official_image(gates, command)
        assert baseline["Id"] == "sha256:official"
        assert calls == [[*command, "image", "pull", "--platform=linux/amd64", build.BASE_IMAGE],
                         [*command, "image", "inspect", build.BASE_IMAGE]]
        assert json.loads((tmp_path / "official-image.json").read_text())["Id"] == baseline["Id"]
