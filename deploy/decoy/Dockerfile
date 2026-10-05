# 诱捕层镜像：只跑一个标准库 HTTP 服务，无出网、无写盘、非 root。
FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DECOY_PORT=8080

WORKDIR /app
COPY decoy_service.py /app/decoy_service.py

# 固定非 root 用户；镜像内不留 shell 入口给外部使用（容器不暴露端口到宿主）。
RUN adduser -D -u 10001 -s /sbin/nologin decoy \
    && chown -R decoy:decoy /app

USER decoy
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/.env', timeout=3).status == 200 else 1)"

CMD ["python3", "/app/decoy_service.py"]
