from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# API Routes
from app.routes.api.file_routes import router as file_router
from app.routes.api.file_mapping_routes import router as file_mapping_router
from app.routes.api.transaction_routes import router as transaction_router
from app.routes.api.organization_routes import router as organization_router
from app.routes.api.sheet_data_routes import router as sheet_data_router
from app.routes.api.reconciliation_routes import router as reconciliation_router

# Page Routes (server-rendered HTML)
from app.routes.pages import router as pages_router

app = FastAPI(
    title="Reconciliation System",
    description="FastAPI-based reconciliation system.",
    version="1.0.0",
)

# ── API Routers ───────────────────────────────────────────────────────────────
app.include_router(file_router)
app.include_router(file_mapping_router)
app.include_router(transaction_router)
app.include_router(organization_router)
app.include_router(sheet_data_router)
app.include_router(reconciliation_router)

# ── Page Routers (HTML) ───────────────────────────────────────────────────────
# Must be registered last so API routes take priority on prefix overlaps.
app.include_router(pages_router)
