"""eBR-Lite API. Run:  uvicorn app.main:app --reload   (from app/backend)
Interactive API docs: http://127.0.0.1:8000/docs
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import clock, config, db
from .errors import EbrError
from .routers import auth, demo, execution, master_data, review


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.create_schema()          # idempotent; use `python -m app.seed` to reset + load demo data
    yield


app = FastAPI(title="eBR-Lite", version="0.1.0", lifespan=lifespan,
              description="Electronic Batch Record prototype (MES Level 3) – demo, not a validated system.")
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(EbrError)
async def ebr_error_handler(_: Request, exc: EbrError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, "error": type(exc).__name__})


@app.get("/api/health")
def health():
    return {"status": "ok", "server_utc": clock.iso(clock.utcnow()), "demo_mode": config.DEMO_MODE}


for r in (auth.router, master_data.router, execution.router, review.router, demo.router):
    app.include_router(r)


# Serve the built React app (app/frontend/dist) so one command runs everything.
if (config.FRONTEND_DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        f = config.FRONTEND_DIST / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(config.FRONTEND_DIST / "index.html")
