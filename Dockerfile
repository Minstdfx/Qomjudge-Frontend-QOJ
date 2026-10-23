FROM node:24.22.1-alpine AS frontend
WORKDIR /build
COPY package.json package-lock.json .npmrc ./
RUN npm ci --no-audit --no-fund
COPY frontend ./frontend
COPY scripts/check-node.mjs ./scripts/check-node.mjs
RUN npm run build:check

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ccpc.py cli_auth.py qoj.py participation.py results.py submission_data.py storage.py demo.py downloads.py teams.py printing.py statements.py joint_entry.py joint_vp.py ./
COPY --from=frontend /build/frontend-dist ./static
RUN useradd --uid 10001 --create-home vp && mkdir -p /app/.data && chown vp:vp /app/.data
USER vp
EXPOSE 8080
VOLUME ["/app/.data"]
ENTRYPOINT ["python", "app.py"]
