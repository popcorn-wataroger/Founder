from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routers import stripe_router

app = FastAPI(title="Founder", version="0.1.0")

app.include_router(stripe_router.router)

# 静的ファイル配信
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")
