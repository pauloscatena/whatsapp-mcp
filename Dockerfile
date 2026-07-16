# --- Stage 1: build the Go WhatsApp bridge (needs CGO for go-sqlite3) ---
FROM golang:1.25-bookworm AS bridge-build
WORKDIR /src
COPY whatsapp-bridge/go.mod whatsapp-bridge/go.sum ./
RUN go mod download
COPY whatsapp-bridge/ ./
RUN CGO_ENABLED=1 GOOS=linux go build -o /out/whatsapp-bridge .

# --- Stage 2: runtime image with the bridge binary + the Python MCP server ---
FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app/whatsapp-bridge
COPY --from=bridge-build /out/whatsapp-bridge ./whatsapp-bridge

WORKDIR /app/whatsapp-mcp-server
COPY whatsapp-mcp-server/pyproject.toml whatsapp-mcp-server/uv.lock ./
RUN uv sync --frozen
COPY whatsapp-mcp-server/ ./

WORKDIR /app
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

VOLUME ["/app/whatsapp-bridge/store"]
EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
