from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# Health check
@app.get("/status")
def status():
    return {"status": "ok"}

# Agent discovery
@app.get("/assistants/search")
def search_assistants():
    return {
        "agents": [
            {
                "id": "travel-defi-agent",
                "name": "Travel Defi Agent",
                "description": "Books hotels using USDC and DeFi protocols"
            }
        ]
    }

# Run agent workflow
@app.post("/run")
async def run_agent(request: Request):
    body = await request.json()
    # Expecting {"messages":[{"role":"user","content":"..."}]}
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing 'messages' in request body"}
        )
    user_message = messages[0].get("content", "")
    # TODO: integrate your LangGraph workflow here
    return {
        "result": {
            "reply": f"Agent received: {user_message}",
            "status": "success"
        }
    }

# Optional: info endpoint
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
