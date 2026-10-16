FROM python:3.12.10-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
WORKDIR /srv/hookdesk
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home hookdesk
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --compile-bytecode
COPY --chown=hookdesk:hookdesk . .
USER hookdesk
EXPOSE 8000
CMD ["uv", "run", "--frozen", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
