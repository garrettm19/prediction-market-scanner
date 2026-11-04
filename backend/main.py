import logging
import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from routers import odds, opportunities, monitor, scan, maker
from services.redaction import configure_log_redaction

load_dotenv()
logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO, and Odds API calls carry ?apiKey=...
# Keep the request logs but mask credential query params.
configure_log_redaction()

app = FastAPI(title="MasG EV Tool API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats (inf/-inf/nan) with their string form so
    a validation-error payload that echoes them back can be serialized."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    """Same 422 response as FastAPI's default handler, except that
    non-finite inputs (e.g. a JSON body containing ``Infinity``) are
    stringified first.  Without this, rejecting such a body crashed the
    default handler's JSON encoding and turned the 422 into a 500."""
    safe = RequestValidationError(_json_safe(list(exc.errors())), body=None)
    return await request_validation_exception_handler(request, safe)


app.include_router(odds.router, prefix="/api")
app.include_router(opportunities.router, prefix="/api")
app.include_router(monitor.router, prefix="/api")
app.include_router(scan.router, prefix="/api")
app.include_router(maker.router, prefix="/api")
