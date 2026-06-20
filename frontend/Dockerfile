FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
# 国内构建加速: npm 源切淘宝/npmmirror 镜像(registry.npmjs.org 国内拉取极慢)
RUN npm ci --registry=https://registry.npmmirror.com
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
