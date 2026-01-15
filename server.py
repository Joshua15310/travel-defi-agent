from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://agentchat.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/status")
def status():
    return {"status": "ok"}

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

@app.post("/run")
async def run_agent(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400, content={"error": "Missing 'messages'"})
    user_message = messages[0].get("content", "")
    return {
        "result": {
            "reply": f"Agent received: {user_message}",
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
