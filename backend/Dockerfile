FROM python:3.11-slim AS builder

WORKDIR /app

# 构建阶段保留编译工具链；最终运行镜像不会继承这些开发包。
# 国内构建加速：Debian apt 源切腾讯云镜像（deb.debian.org 国内拉取较慢或可能超时）。
RUN (sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
     || sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list 2>/dev/null || true) \
    && apt-get update && apt-get install -y --no-install-recommends \
    build-essential default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

# 将 Python 索引参数放在系统依赖层之后，切换索引时仍可复用 apt 缓存。
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN python -m venv "${VIRTUAL_ENV}"

COPY requirements.lock .
RUN python -m pip install \
    --no-cache-dir \
    --require-hashes \
    --index-url "${PIP_INDEX_URL}" \
    -r requirements.lock

FROM python:3.11-slim AS runtime

WORKDIR /app

# curl 仅用于容器健康检查；运行阶段不安装编译器或开发头文件。
RUN (sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
     || sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list 2>/dev/null || true) \
    && apt-get update && apt-get install -y --no-install-recommends curl \
    && groupadd --system --gid 991 prism-ops \
    && useradd --system --uid 10001 --gid prism-ops --home-dir /app --shell /usr/sbin/nologin prism \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv
COPY --chown=prism:prism . .

# WORKDIR 创建的 /app 仍属于 root；数字 UID/GID 与 Compose 的运行身份保持一致。
RUN install -d -o 10001 -g 991 -m 0750 /app/logs

USER prism

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-date-header"]
