ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN set -eu; \
    image_digest="${BASE_IMAGE##*@sha256:}"; \
    [ "$image_digest" != "$BASE_IMAGE" ]; \
    [ "${#image_digest}" -eq 64 ]; \
    case "$image_digest" in *[!0-9a-f]*) exit 1 ;; esac
# The fixed runner uses the pinned JDK and a shell-only local health probe.
ARG PRISM_RUNNER_SHA256
ARG JADX_VERSION=1.5.6
ARG JADX_SHA256=545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974
ENV PRISM_JADX_VERSION=${JADX_VERSION}
RUN set -eu; \
    case "$JADX_VERSION" in *[!0-9.]*) exit 1 ;; esac; \
    case "$JADX_SHA256" in *[!0-9a-f]*) exit 1 ;; esac; \
    [ "${#JADX_SHA256}" -eq 64 ]; \
    mkdir -p /opt/jadx; \
    curl -fsSL --retry 3 "https://github.com/skylot/jadx/releases/download/v${JADX_VERSION}/jadx-${JADX_VERSION}.zip" -o /tmp/jadx.zip; \
    echo "$JADX_SHA256  /tmp/jadx.zip" | sha256sum -c -; \
    cd /opt/jadx; jar xf /tmp/jadx.zip; chmod 0555 /opt/jadx/bin/jadx; test -x /opt/jadx/bin/jadx; \
    rm -f /tmp/jadx.zip; chmod -R go-w /opt/jadx
COPY --chmod=0555 runner.sh /opt/prism/runner.sh
# 构建期自检:烤入的 runner 哈希必须等于注入值,并写入镜像 label 供执行器校验
RUN set -eu;     case "$PRISM_RUNNER_SHA256" in ''|*[!0-9a-f]*) exit 1 ;; esac;     [ "${#PRISM_RUNNER_SHA256}" -eq 64 ];     [ "$(sha256sum /opt/prism/runner.sh | cut -d' ' -f1)" = "$PRISM_RUNNER_SHA256" ]
LABEL org.prism.runner.sha256="${PRISM_RUNNER_SHA256}" \
      org.prism.agent-test-protocol="2" \
      org.prism.decompiler="jadx" \
      org.prism.decompiler.version="${JADX_VERSION}" \
      org.prism.decompiler.sha256="${JADX_SHA256}"
ENTRYPOINT ["/opt/prism/runner.sh"]
