from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter

app = FastAPI()

# Enable CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Use specific origin if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Root endpoints (for CTO)
# -------------------------

@app.get("/status")
def status():
    return {"status": "ok"}

@app.get("/assistants/search")
def assistants_search():
    return {
        "agents": [
            {
                "id": "travel-defi-agent",
                "name": "Travel DeFi Agent",
                "description": "Books hotels using USDC and DeFi protocols"
            }
        ]
    }

@app.post("/run")
async def run_agent(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400, content={"error": "Missing messages"})
    return {
        "result": {
            "reply": f"Agent received: {messages[0]['content']}",
            "status": "success"
        }
    }

@app.get("/info")
def info():
    return {
        "graphs": {
            "agent": {
                "input_schema": {"messages": "list of {role, content}"},
                "output_schema": {"result": "structured JSON"}
            }
        }
    }

# -------------------------
# /agent endpoints (for LangGraph UI)
# -------------------------

agent = APIRouter(prefix="/agent")

@agent.get("/info")
def agent_info():
    return info()

@agent.get("/assistants/search")
def agent_assistants():
    return assistants_search()

@agent.post("/run")
async def agent_run(request: Request):
    return await run_agent(request)

@agent.post("/threads/search")
def threads_search():
    return {"threads": []}

@agent.post("/threads")
def create_thread():
    return {"thread_id": "demo-thread"}

@agent.post("/threads/{thread_id}/history")
def thread_history(thread_id: str):
    return {"messages": []}

@agent.post("/threads/{thread_id}/runs/stream")
def stream_run(thread_id: str):
    return {
        "event": "values",
        "data": {
            "messages": [
                {
                    "role": "ai",
                    "content": "👋 Welcome to Warden Travel! What would you like to book?"
                }
            ]
        }
    }

app.include_router(agent)
