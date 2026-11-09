# Build only in WSL2. The runtime keeps the official dispatcher, /init and s6.
FROM nousresearch/hermes-agent@sha256:adb7890916de63245523492be8f68f9d2095fecbec4e2a6dab7960381f0fc002 AS official

FROM official AS sender
USER root
WORKDIR /opt/hermes
COPY snapshot/components/web-push-sender/ /opt/web-push-source/
RUN python3 -c 'import platform, sys; assert sys.version_info[:2] == (3, 14); assert platform.machine() == "x86_64"' && \
    python3 -m pm.build_env --source /opt/web-push-source --check-lock --python /usr/local/bin/python3 && \
    python3 -m pm.build_env --source /opt/web-push-source --out /opt/hermes-web-push \
      --python /usr/local/bin/python3 --no-install-project --sealed && \
    cp /opt/web-push-source/send.py /opt/hermes-web-push/send.py && \
    cp /opt/web-push-source/pyproject.toml /opt/web-push-source/uv.lock /opt/hermes-web-push/ && \
    /opt/hermes-web-push/bin/python -I -B -c 'import importlib.metadata as m, pywebpush, sys; assert m.version("pywebpush") == "2.5.0"; assert "hermes_bootstrap" not in sys.modules' && \
    /opt/hermes-web-push/bin/python -I -B /opt/web-push-source/receipt.py \
      > /opt/hermes-web-push/sender-build.json && \
    chmod -R a+rX,go-w /opt/hermes-web-push

# This stage is explicitly built and must pass before the final image is made.
# Its test interpreter, tests and credentials never enter the runtime stage.
FROM sender AS tests
COPY snapshot/ /opt/hermes/
COPY test-source/tests/ /opt/hermes/tests/
COPY tracked-python /opt/hermes-tracked-python
# A test interpreter owns its own dependencies. Without this test-stage-only
# change, bootstrap would activate the runtime payload over pytest's environment.
# The final stage starts again from official and reassembles its real manifest.
RUN rm /opt/hermes/manifest.json
RUN python3 -m pm.build_env --source /opt/hermes --out /opt/hermes-web-push-tests \
      --python /usr/local/bin/python3 --group dev --group test --no-install-project --sealed && \
    printf '/opt/hermes-web-push/lib/python3.14/site-packages\n' \
      > /opt/hermes-web-push-tests/lib/python3.14/site-packages/web-push-sender-test.pth && \
    git init --quiet /opt/hermes && \
    git -C /opt/hermes add --pathspec-from-file=/opt/hermes-tracked-python --pathspec-file-nul
RUN HERMES_PYTHON=/opt/hermes-web-push-tests/bin/python \
    HERMES_TEST_WORKERS=2 HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh \
      tests/components/test_web_push_sender.py \
      tests/scripts/bundles/test_android_web_push_bundle.py \
      tests/hermes_cli/test_web_push_store.py \
      tests/hermes_cli/test_web_push_api.py \
      tests/hermes_cli/test_web_push_worker.py \
      tests/hermes_cli/test_web_push_events.py \
      tests/gateway/test_session_web_push.py \
      tests/cron/test_web_push.py \
      tests/gateway/test_hosted_room_web_push.py \
      tests/hermes_state/test_runtime_admission.py \
      tests/gateway/test_session_authority.py \
      tests/gateway/test_session_group_execution_controls.py \
      tests/gateway/test_local_viewer_clarify_batch.py \
      tests/gateway/test_clarify_batch_settlement_parity.py \
      tests/gateway/test_managed_worker_clarify_timeout.py \
      tests/gateway/test_server_request_resume_replay.py \
      tests/cron/test_execution_ledger.py \
      tests/cron/test_cron_execution_identity.py \
      tests/cron/test_scheduler_cron_session_isolation.py \
      tests/cron/test_warning_execution_outcome.py \
      tests/cron/test_scheduler_completion_verification.py \
      tests/cron/test_scheduler_shutdown_guard.py \
      tests/hermes_cli/test_browser_credential.py \
      tests/hermes_cli/test_webapp_sign_out_sockets.py \
      tests/hermes_cli/test_dashboard_auth_middleware.py \
      tests/hermes_cli/test_dashboard_auth_password_login.py \
      tests/hermes_cli/test_dashboard_auth_page_icons.py \
      tests/hermes_cli/test_dashboard_auth_native_cookie_writes.py \
      tests/hermes_cli/test_dashboard_auth_prefix.py \
      tests/gateway/test_webapp_browser_credential.py

FROM official AS runtime
USER root
WORKDIR /opt/hermes
ARG FORK_COMMIT
COPY --link --chmod=a+rX,go-w snapshot/ /opt/hermes/
COPY --link --chmod=a+rX,go-w renderer/ /opt/hermes/apps/desktop/dist-webapp/
COPY --from=sender --link --chmod=a+rX,go-w /opt/hermes-web-push/ /opt/hermes-web-push/
COPY source-inputs.json /opt/hermes-web-push/source-inputs.json
RUN python3 scripts/write_install_stamp.py --output /opt/hermes/install-stamp.json \
      --commit "$FORK_COMMIT" --branch codex/android-web-push --base-version 0.23.0 \
      --distance 0 --source feature-image --distribution docker --update-mechanism external && \
    /opt/hermes/.venv/bin/python scripts/bundles/android-web-push/image.py --commit "$FORK_COMMIT" && \
    chmod 0644 /opt/hermes/install-stamp.json /opt/hermes/manifest.json \
      /opt/hermes-web-push/source-inputs.json /etc/hermes/image-provenance.json
ENV HERMES_WEBAPP_DIST=/opt/hermes/apps/desktop/dist-webapp
LABEL org.opencontainers.image.revision=$FORK_COMMIT
# ENTRYPOINT, CMD, s6 services and the root bootstrap contract are inherited.
