FROM node:20-alpine AS builder

WORKDIR /app
COPY package*.json ./
# npm 默认 registry 在国内/跨境拉包慢甚至卡死,统一走 npmmirror(可被 --build-arg 覆盖)
ARG NPM_CONFIG_REGISTRY=https://registry.npmmirror.com
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}
ARG VITE_APP_VERSION=3.7.1
ARG VITE_APP_RELEASE_SHA=local
ENV VITE_APP_VERSION=${VITE_APP_VERSION}
ENV VITE_APP_RELEASE_SHA=${VITE_APP_RELEASE_SHA}
RUN npm ci --registry=${NPM_CONFIG_REGISTRY}
COPY . .
# 生产构建跳过 vue-tsc:服务器内存有限(1.9G),vue-tsc+vite 并联易 OOM。
# 类型检查已在本地 CI/开发期通过(vue-tsc --noEmit),镜像内只做 vite 打包。
# NODE_OPTIONS 限堆内存,防止 vite 构建期 OOM。
ENV NODE_OPTIONS=--max-old-space-size=1536
RUN npx vite build

FROM nginx:1.27-alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY --from=builder /app/dist /opt/prism-dist
COPY --chmod=0755 docker-entrypoint.d/30-retain-versioned-assets.sh /docker-entrypoint.d/30-retain-versioned-assets.sh
# nginx.conf 是 http 块主配置(含 gzip);nginx.conf.template 是 server 块模板,
# 由官方镜像 entrypoint 经 envsubst 渲染为 conf.d/default.conf。
# 注意:生产实际生效的 server 配置当前由 docker-compose.override.yml 以
# bind-mount 覆盖(/opt/code-review/frontend/{main,default}.conf),
# 下次正式构建并切换镜像后,应以本镜像内 nginx.conf + nginx.conf.template 为准。
COPY nginx.conf /etc/nginx/nginx.conf
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 80 443
CMD ["nginx", "-g", "daemon off;"]
