# Stage 1: build static site with Python
FROM python:3.12-alpine AS build
WORKDIR /app
COPY src/ ./src/
COPY build.py ./
RUN python3 build.py

# Stage 2: serve with nginx
FROM nginx:alpine
COPY --from=build /app/dist/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf
