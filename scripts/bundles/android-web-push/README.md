# WSL2 Android Web Push image build

Run this only on the Windows management computer, inside **Ubuntu 26.04 WSL2
on x86_64**. The build deliberately refuses other hosts. The VPS receives
prepared artifacts; it must never resolve dependencies, run npm or build images.

Use a clean Linux-filesystem clone such as `~/src/hermes-agent`, CPython 3.14,
Node **26.12.1**, npm **12.0.2**, and Docker Engine targeting Linux amd64. Keep
the clone out of `/mnt/c` and do not put production dotenv files or credentials
in it. Install Docker Engine through its [official Ubuntu apt repository](https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository),
including the Buildx plugin. Ubuntu 26.04 is a supported distribution. Enable
WSL systemd through the [Microsoft WSL instructions](https://learn.microsoft.com/windows/wsl/systemd)
if the native daemon needs it; verify `docker info` before building. The feature
tool does not install or configure Docker or grant access to its socket.

Only an existing local Unix socket is accepted. The tool inspects the selected
Docker context and `DOCKER_HOST`/`DOCKER_CONTEXT`, rejects SSH/TCP endpoints and
TLS/certificate overrides, then pins the verified Unix socket explicitly for
every daemon command. A WSL shell using a remote VPS Docker context is refused.
Local Docker Engine and Docker Desktop's local WSL Unix socket are supported.
Custom BuildKit/Buildx host and builder overrides are refused too; the image
build explicitly uses the local Engine's `default` builder.

The immutable base is:

```text
nousresearch/hermes-agent@sha256:adb7890916de63245523492be8f68f9d2095fecbec4e2a6dab7960381f0fc002
```

It corresponds to the established v0.23.0 backend. The custom recipe inherits
its normal dispatcher, ENTRYPOINT, CMD, s6 tree, root bootstrap and UID remap.
It adds the separate sealed sender, reassembles the committed fork source into
the original prepared environment, refreshes source provenance and includes the
new renderer. It does not add an entrypoint overlay or change PM selection.

Before the first release, the component's metadata-seeded lock must pass the
official pinned PM's `check-lock` in WSL. If normalization/resolution is needed,
use `pm.lock_project` / `python -m pm.build_env --source <component> --lock-only`
from the prepared image in a disposable WSL build context. Review and commit
only the resulting component lock; never update the main lock to accommodate
the sender. The normal image build refuses an out-of-date lock.

Check out the exact reviewed commit, and provide a public renderer archive from
the prior deployment plus its independently verified SHA-256. Its root must be
`webapp/` or `base-webapp/`. Prior hashed lazy chunks are retained by the upstream
renderer recipe.

```bash
python3 scripts/bundles/android-web-push/build.py \
  --commit FULL_REVIEWED_40_CHARACTER_COMMIT \
  --release v0.23.0-android-web-push.1 \
  --base-renderer "$HOME/artifacts/hermes/base-webapp.tar.gz" \
  --base-sha256 VERIFIED_64_CHARACTER_SHA256 \
  --out "$HOME/artifacts/hermes/v0.23.0-android-web-push.1"
```

The clone must be clean and all new tests committed. The tool checks that the
main Python locks, Node lock, PM, bootstrap and official Docker runtime files
remain identical to upstream v0.23.0. It pulls the exact official digest into the
verified local Engine, inspects and records that baseline before any image build,
then runs:

1. The eight existing cron history / canonical chat UI regression files, which
   previously contained 105 tests, and all new Web Push UI regression files.
2. Desktop typecheck and lint; the upstream production renderer build; native
   source/output freshness receipt validation.
3. Pinned-PM component lock validation and a fresh sealed sender build in the
   official image's CPython 3.14 environment.
4. Sender and packaging behavior tests plus API/store/worker, session, cron and
   Group producer tests through `scripts/run_tests.sh` in a disposable image
   stage. Its separate test environment can import the sender dependencies;
   neither test packages nor tests enter the runtime image.
   The same gate also runs 21 existing regression files covering admission,
   session authority/Groups, clarify and managed workers, execution ledger and
   cron completion/shutdown, browser credentials/sign-out and dashboard auth.
5. Runtime image assembly, preservation of the official launch contract, sender
   distribution inventory and Linux amd64 checks, followed by image export.

No gate is inferred from a file's presence, and failed or unexecuted gates do
not produce `release.json`. Logs and actual return codes are recorded separately.

Transfer `image.tar`, `webapp.tar.gz`, `release.json`, `sender-build.json`,
`official-image.json` and `SHA256SUMS`. Retain build logs privately. The receipt binds the full fork commit,
official base digest, unchanged core inputs, component lock/script/distributions,
renderer files, image ID and archive hashes. These are candidate artifacts:
production acceptance and cutover remain separate operator actions.

For startup acceptance, use a disposable, **offline** clone with sanitized PM
metadata and its selected dependency generation at the original `/opt/data/...`
paths. Never transfer production credentials, session text or the whole home.
Metadata alone is insufficient because PM validates the referenced venv's
existence. Check the original entrypoint reports dependency environment `current`,
uses the expected environment, makes no install/build/network attempt, and
preserves recorded selection, native Groups, normal Discord and cron routes.
Pending repair/publication must be absent or resolved before cutover. Keep the
old PM data and old image/renderer for rollback. The public release receipt
explicitly records that this private production-clone startup acceptance is
not performed by the build tool.

An existing `HERMES_WEBAPP_DIST` mount or override must select the new renderer
at deployment. The custom image defaults to its baked renderer at
`/opt/hermes/apps/desktop/dist-webapp`. Deployment files and private PM snapshots
belong outside this repository, under the operator's runtime directory.
