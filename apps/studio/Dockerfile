# Stage 1: Build Vue app
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Stage 2: Serve with Nginx
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html/studio
# Nginx config is mounted via docker-compose volume (D-11)
EXPOSE 80
