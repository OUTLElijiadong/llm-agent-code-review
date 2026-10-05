# 诱捕层镜像：nginx:alpine 直接以真实证书听 8443，内容由 decoy_service.py 生成。
#
# 为什么用 nginx 而不是 Python 自带 TLS：私钥是 root:600，只有 root 能读；
# 用 nginx 可以让 master 进程持有私钥，与生产前端容器同款做法（前端 master 同样是 root），
# 避免为了诱捕层把私钥权限放宽。
FROM nginx:alpine

# 用生成器把诱饵内容写成静态树（33 个文件，全部虚构），再删除生成器，
# 运行期镜像里只有 nginx + 静态文件。
COPY decoy_service.py /tmp/decoy_service.py
RUN apk add --no-cache python3 >/dev/null \
    && rm -rf /usr/share/nginx/html \
    && mkdir -p /usr/share/nginx/html \
    && python3 /tmp/decoy_service.py --generate /usr/share/nginx/html \
    && rm -f /tmp/decoy_service.py \
    && mkdir -p /tmp/client_body /tmp/proxy /tmp/fastcgi /tmp/uwsgi /tmp/scgi

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --no-check-certificate -q --spider https://127.0.0.1:8443/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
