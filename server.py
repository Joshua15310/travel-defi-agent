from fastapi import FastAPI
from langserve import add_routes
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser

from agent import agent

app = FastAPI(
    title="Crypto Travel DeFi Agent",
    version="1.0"
)

prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "You are a crypto travel booking agent. You find hotels under budget and return prices."),
        MessagesPlaceholder(variable_name="messages")
    ]
)

chain = prompt | agent | StrOutputParser()

add_routes(
    app,
    chain,
    path="/agent"
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
