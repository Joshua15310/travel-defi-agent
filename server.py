import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
from workflow.graph import graph  # Import your actual graph object

app = FastAPI(
    title="Nomad AI Travel Agent",
    version="1.0",
    description="Warden Protocol Travel Agent",
)

# CRITICAL: Broaden CORS for Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# This is what your index.html uses
add_routes(
    app,
    graph,
    path="/agent",
)

# ADD THIS: A manual /info endpoint to stop the Vercel 404
@app.get("/agent/info")
async def get_info():
    return {
        "graphs": {
            "agent": {
                "input_schema": graph.input_schema.schema(),
                "output_schema": graph.output_schema.schema(),
            }
        }
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)