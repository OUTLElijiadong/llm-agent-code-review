FROM node:20-alpine AS builder

WORKDIR /app
COPY package*.json ./
# npm 默认 registry 在国内/跨境拉包慢甚至卡死,统一走 npmmirror(可被 --build-arg 覆盖)
ARG NPM_CONFIG_REGISTRY=https://registry.npmmirror.com
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}
ARG VITE_APP_VERSION=3.8.1
ARG VITE_APP_RELEASE_SHA=local
ARG VITE_APP_BUILD_TIME=unknown
ENV VITE_APP_VERSION=${VITE_APP_VERSION}
ENV VITE_APP_RELEASE_SHA=${VITE_APP_RELEASE_SHA}
ENV VITE_APP_BUILD_TIME=${VITE_APP_BUILD_TIME}
RUN npm ci --registry=${NPM_CONFIG_REGISTRY}
COPY . .
# npm run build 先完成类型检查，再顺序运行 Vite；任一步失败均阻止镜像发布。
# vue-tsc 与 Vite 顺序运行；2 GiB 为默认堆上限，镜像构建时可按宿主资源覆盖。
ARG NODE_MAX_OLD_SPACE_SIZE=2048
ENV NODE_OPTIONS=--max-old-space-size=${NODE_MAX_OLD_SPACE_SIZE}
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY --from=builder /app/dist /opt/prism-dist
COPY --chmod=0755 docker-entrypoint.d/30-retain-versioned-assets.sh /docker-entrypoint.d/30-retain-versioned-assets.sh
# nginx.conf 是 http 块主配置(含 gzip);nginx.conf.template 是 server 块模板,
# 由官方镜像 entrypoint 经 envsubst 渲染为 conf.d/default.conf。
# 生产当前运行的 release Compose 未挂载 Nginx server 配置；
# nginx.conf + nginx.conf.template 随镜像发布并由官方 entrypoint 渲染。
COPY nginx.conf /etc/nginx/nginx.conf
COPY nginx.decoy.conf /etc/nginx/prism-decoy.conf
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 80 443
CMD ["nginx", "-g", "daemon off;"]
