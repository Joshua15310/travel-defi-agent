from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
from langchain_core.runnables import RunnableLambda
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
from typing import List, Dict, Any

from agent import workflow_app

app = FastAPI(
    title="Travel DeFi Agent",
    version="1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

class InputSchema(BaseModel):
    messages: List[Dict[str, Any]]

def adapter(data):
    msgs = []
    for m in data["messages"]:
        if m.get("content"):
            msgs.append(HumanMessage(content=m["content"]))

    return {
        "messages": msgs
    }

chain = RunnableLambda(adapter) | workflow_app

add_routes(
    app,
    chain,
    path="/agent",
    playground_type="default"
)

@app.get("/")
def root():
    return {"status": "ok"}
