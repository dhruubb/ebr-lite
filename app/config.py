"""Application settings. Override any of these with environment variables."""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent          # app/backend
SEED_DIR = BACKEND_DIR / "seed"

DATABASE_URL = os.getenv("EBR_DATABASE_URL", f"sqlite:///{BACKEND_DIR / 'ebr_lite.db'}")

JWT_SECRET = os.getenv("EBR_JWT_SECRET", "dev-only-secret-change-me-in-production-0123456789")
JWT_ALGORITHM = "HS256"
SESSION_MINUTES = int(os.getenv("EBR_SESSION_MINUTES", "30"))   # URS: session timeout
BCRYPT_ROUNDS = int(os.getenv("EBR_BCRYPT_ROUNDS", "12"))

IPC_GRACE_MINUTES = 5          # an IPC sample is "missed" if not taken within interval + grace
CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

# Demo mode adds a "fast-forward clock" control so a 30-min IPC interval can be demonstrated
# without waiting. Every use is audited. Turn off with EBR_DEMO_MODE=0.
DEMO_MODE = os.getenv("EBR_DEMO_MODE", "1") == "1"

# If the frontend has been built (npm run build), FastAPI serves it at http://127.0.0.1:8000/
FRONTEND_DIST = BACKEND_DIR.parent / "frontend" / "dist"
