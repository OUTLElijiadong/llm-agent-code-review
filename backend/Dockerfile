FROM python:3.11-slim

WORKDIR /app

# 国内构建加速: Debian apt 源切腾讯云镜像(deb.debian.org 国内拉取极慢/超时)
RUN (sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
     || sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list 2>/dev/null || true) \
    && apt-get update && apt-get install -y --no-install-recommends \
    build-essential default-libmysqlclient-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
