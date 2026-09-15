FROM python:3.12-slim

WORKDIR /app

# System deps for httpx/dnspython are pure-Python; nothing extra needed.
COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir ".[api,enrich,anthropic]"

EXPOSE 8000

CMD ["uvicorn", "lead_qualifier.api:app", "--host", "0.0.0.0", "--port", "8000"]
