ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN set -eu; \
    image_digest="${BASE_IMAGE##*@sha256:}"; \
    [ "$image_digest" != "$BASE_IMAGE" ]; \
    [ "${#image_digest}" -eq 64 ]; \
    case "$image_digest" in *[!0-9a-f]*) exit 1 ;; esac
# The fixed runner uses the pinned Go toolchain and a shell-only local probe.
COPY --chmod=0555 runner.sh /opt/prism/runner.sh
ENTRYPOINT ["/opt/prism/runner.sh"]
