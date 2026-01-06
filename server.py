from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
from agent import workflow_app

app = FastAPI(title="Crypto Travel Agent")

# 1. Enable CORS (Allows your local HTML file to talk to the server)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Add the Agent Routes
add_routes(app, workflow_app, path="/agent")

# 3. FIX THE 404: Add a Root Route
@app.get("/")
async def redirect_root_to_playground():
    # When you visit the base URL, redirect to the playground
    return RedirectResponse(url="/agent/playground")

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)