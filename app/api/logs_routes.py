from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/v1")


@router.get("/logs")
async def get_logs(
    request: Request,
    limit: int = Query(default=50, le=500),
    level: str | None = None,
    type: str | None = None,
):
    entries = await request.app.state.event_logger.recent(limit=limit, level=level, type_=type)
    return {"logs": entries, "count": len(entries)}


@router.get("/logs/{trace_id}")
async def get_trace(trace_id: str, request: Request):
    entry = await request.app.state.event_logger.get(trace_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="trace not found (it may have aged out of the log window)")
    return entry
