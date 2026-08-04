from fastapi import FastAPI,Request
from fastapi.templating import Jinja2Templates

from app.routes.api.file_routes import router as file_router
from app.routes.api.file_mapping_routes import router as file_mapping_router
from app.routes.api.transaction_routes import router as transaction_router
from fastapi.responses import HTMLResponse
from app.routes.web.views import router as web_router

app = FastAPI(
    title="Reconciliation System",
    description="FastAPI-based reconciliation system.",
    version="1.0.0",
)

templates = Jinja2Templates(directory="app/templates")

app.include_router(file_router)
app.include_router(file_mapping_router)
app.include_router(transaction_router)
app.include_router(web_router)

@app.get("/")
def upload_page(request: Request):
    
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context={}
    )
