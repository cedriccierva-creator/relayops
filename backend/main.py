# FastAPI entrypoint (run with: uvicorn main:app --reload --port 8000).
from app.file_permissions import initialize_file_permissions

# Process startup: before imports that create database directories or files.
initialize_file_permissions()

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db import init_db
from app.routers import auth, health, notes, hooks, relay
from app.config import get_settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # Phase 1 is additive: create missing tables, never alter existing data.
    yield


app = FastAPI(title="RelayOps API", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    # Pydantic's default errors echo input, which can contain passwords or source secrets.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in exc.errors()
    ]})


@app.middleware("http")
async def dashboard_security(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not request.url.path.startswith("/api/hooks/"):
        origin = request.headers.get("origin")
        if (origin is not None and origin not in get_settings().DASHBOARD_ORIGINS
                or request.headers.get("sec-fetch-site") == "cross-site"):
            return JSONResponse(status_code=403, content={"detail": "Cross-origin dashboard request blocked"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(notes.router)
app.include_router(hooks.router)
app.include_router(relay.router)
