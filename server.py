from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
from agent import workflow_app

app = FastAPI(
    title="Travel DeFi Agent",
    version="1.0",
    description="LangGraph agent for Warden Hub"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "ok", "agent": "travel-defi"}

add_routes(
    app,
    workflow_app,
    path="/agent",
)
