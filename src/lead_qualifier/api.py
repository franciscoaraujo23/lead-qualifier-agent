"""FastAPI surface for the domain service.

Thin by design: auth, one /qualify endpoint, typed-error -> HTTP mapping, and a
health check. All domain logic lives in the pipeline. n8n calls this in Phase 3.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .config import settings
from .errors import PipelineError
from .llm import get_provider
from .persistence import get_repository
from .schemas import QualifyRequest, QualifyResponse

app = FastAPI(title="Lead Qualifier", version="0.1.0")

_repo = get_repository()
_provider = get_provider()


async def verify_token(x_auth_token: str | None = Header(default=None)) -> None:
    """Shared-secret auth (§4.7). No-op if no token is configured, so the demo
    runs open locally; set LQ_WEBHOOK_AUTH_TOKEN to require it.
    """
    if settings.webhook_auth_token is None:
        return
    if x_auth_token != settings.webhook_auth_token:
        raise HTTPException(status_code=401, detail="invalid or missing auth token")


@app.exception_handler(PipelineError)
async def _pipeline_error_handler(_request, exc: PipelineError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": exc.__class__.__name__, "stage": exc.failure_stage, "detail": str(exc)},
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "provider": settings.llm_provider}


@app.post("/qualify", response_model=QualifyResponse, dependencies=[Depends(verify_token)])
async def qualify_endpoint(
    req: QualifyRequest,
    x_trace_id: str | None = Header(default=None),
) -> QualifyResponse:
    from .pipeline import qualify  # local import keeps app import light

    trace_id = x_trace_id or str(uuid.uuid4())
    return await qualify(req.domain, provider=_provider, repo=_repo, trace_id=trace_id)
