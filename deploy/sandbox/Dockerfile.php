ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN set -eu; \
    image_digest="${BASE_IMAGE##*@sha256:}"; \
    [ "$image_digest" != "$BASE_IMAGE" ]; \
    [ "${#image_digest}" -eq 64 ]; \
    case "$image_digest" in *[!0-9a-f]*) exit 1 ;; esac
# The fixed runner uses the pinned PHP runtime and a shell-only local probe.
ARG PRISM_RUNNER_SHA256
COPY --chmod=0555 runner.sh /opt/prism/runner.sh
# 构建期自检:烤入的 runner 哈希必须等于注入值,并写入镜像 label 供执行器校验
RUN set -eu;     case "$PRISM_RUNNER_SHA256" in ''|*[!0-9a-f]*) exit 1 ;; esac;     [ "${#PRISM_RUNNER_SHA256}" -eq 64 ];     [ "$(sha256sum /opt/prism/runner.sh | cut -d' ' -f1)" = "$PRISM_RUNNER_SHA256" ]
LABEL org.prism.runner.sha256="${PRISM_RUNNER_SHA256}"       org.prism.agent-test-protocol="2"
ENTRYPOINT ["/opt/prism/runner.sh"]
