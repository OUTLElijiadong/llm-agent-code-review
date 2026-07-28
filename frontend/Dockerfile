FROM node:20-alpine AS builder

WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=builder /app/dist /usr/share/nginx/html
# nginx.conf 是 http 块主配置(含 gzip);nginx.conf.template 是 server 块模板,
# 由官方镜像 entrypoint 经 envsubst 渲染为 conf.d/default.conf。
# 注意:生产实际生效的 server 配置当前由 docker-compose.override.yml 以
# bind-mount 覆盖(/opt/code-review/frontend/{main,default}.conf),
# 下次正式构建并切换镜像后,应以本镜像内 nginx.conf + nginx.conf.template 为准。
COPY nginx.conf /etc/nginx/nginx.conf
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 80 443
CMD ["nginx", "-g", "daemon off;"]
