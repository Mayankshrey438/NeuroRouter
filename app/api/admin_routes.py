from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/v1/admin")


def _check_admin(request: Request, x_admin_token: str | None):
    settings = request.app.state.settings
    if x_admin_token != settings.neurorouter_admin_token:
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Token header")


# ---------- API Keys ----------

class CreateKeyRequest(BaseModel):
    name: str


@router.get("/keys")
async def list_keys(request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(request, x_admin_token)
    records = await request.app.state.api_key_store.list()
    # Full keys are shown here (not masked) because this whole endpoint is
    # already gated behind the admin token - the admin is a higher trust
    # boundary than the API keys it manages, and masking would make revoke
    # unusable after a page reload since it wouldn't exact-match anything.
    return {"keys": [r.public_dict(reveal_full=True) for r in records]}


@router.post("/keys")
async def create_key(body: CreateKeyRequest, request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(request, x_admin_token)
    record = await request.app.state.api_key_store.create(body.name)
    # reveal the full key only on creation - this is the one time the caller sees it
    return record.public_dict(reveal_full=True)


@router.delete("/keys/{key}")
async def revoke_key(key: str, request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(request, x_admin_token)
    ok = await request.app.state.api_key_store.revoke(key)
    if not ok:
        raise HTTPException(status_code=404, detail="key not found")
    return {"revoked": True, "key": key}


# ---------- Runtime config ----------

class UpdateConfigRequest(BaseModel):
    rate_limit_requests: int | None = None
    rate_limit_window_seconds: int | None = None
    cache_ttl_seconds: int | None = None
    semantic_cache_threshold: float | None = None
    semantic_cache_max_entries: int | None = None
    circuit_failure_threshold: int | None = None
    circuit_recovery_seconds: int | None = None


@router.get("/config")
async def get_config(request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(request, x_admin_token)
    return await request.app.state.runtime_config.get()


@router.post("/config")
async def update_config(body: UpdateConfigRequest, request: Request,
                         x_admin_token: str | None = Header(default=None)):
    _check_admin(request, x_admin_token)
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    return await request.app.state.runtime_config.update(values)


@router.post("/config/reset")
async def reset_config(request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(request, x_admin_token)
    return await request.app.state.runtime_config.reset()


# ---------- Provider control ----------

@router.post("/providers/{name}/disable")
async def disable_provider(name: str, request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(request, x_admin_token)
    if name not in request.app.state.providers:
        raise HTTPException(status_code=404, detail=f"unknown provider '{name}'")
    await request.app.state.provider_control.disable(name)
    return {"provider": name, "disabled": True}


@router.post("/providers/{name}/enable")
async def enable_provider(name: str, request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(request, x_admin_token)
    if name not in request.app.state.providers:
        raise HTTPException(status_code=404, detail=f"unknown provider '{name}'")
    await request.app.state.provider_control.enable(name)
    return {"provider": name, "disabled": False}


@router.post("/providers/{name}/reset-circuit")
async def reset_circuit(name: str, request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(request, x_admin_token)
    breaker = request.app.state.circuit_breakers.get(name)
    if breaker is None:
        raise HTTPException(status_code=404, detail=f"unknown provider '{name}'")
    await breaker.record_success()  # forces closed
    return {"provider": name, "state": "closed"}
