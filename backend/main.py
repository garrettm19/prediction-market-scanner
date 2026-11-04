import logging
from fastapi import FastAPI
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

app.include_router(odds.router, prefix="/api")
app.include_router(opportunities.router, prefix="/api")
app.include_router(monitor.router, prefix="/api")
app.include_router(scan.router, prefix="/api")
app.include_router(maker.router, prefix="/api")
