# agent.py
import os
import requests
import time
import operator
from datetime import date, timedelta, datetime
from typing import TypedDict, List, Optional, Annotated

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI

# Standard Pydantic
from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

import warden_client

load_dotenv()

# --- CONFIGURATION ---
BOOKING_KEY = os.getenv("BOOKING_API_KEY")

LLM_BASE_URL = "https://api.x.ai/v1" 
LLM_API_KEY = os.getenv("GROK_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_MODEL = "grok-beta" if os.getenv("GROK_API_KEY") else "gpt-4o-mini"

# --- 1. State Definition ---
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    destination: str
    check_in: str
    check_out: str
    guests: int
    rooms: int
    budget_max: float
    budget_min: float
    hotels: List[dict]
    selected_hotel: dict
    room_options: List[dict]
    final_room_type: str
    final_price: float
    final_status: str
    date_just_set: bool 
    requirements_complete: bool

# --- 2. Structured Output Schema ---
class TravelIntent(BaseModel):
    destination: Optional[str] = Field(description="City or country name. None if asking for suggestions.")
    check_in: Optional[str] = Field(description="YYYY-MM-DD date. Calculate from relative terms like 'next friday'.")
    guests: Optional[int] = Field(description="Number of people. Infer from context.")
    budget_max: Optional[float] = Field(description="Maximum price per night in USD.")
    user_intent_type: str = Field(description="One of: 'booking_request', 'general_chat', 'asking_suggestion', 'reset'")

# --- 3. Helpers ---
def get_llm():
    if not LLM_API_KEY:
        print("⚠️ Warning: No LLM API Key found.")
    return ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL if "grok" in LLM_MODEL else None,
        temperature=0.7
    )

def get_message_text(msg):
    """Safely extracts text from either a Dict or an Object."""
    if hasattr(msg, 'content'):
        return msg.content
    if isinstance(msg, dict):
        return msg.get('content', '')
    return str(msg)

# --- 4. Node: Intelligent Intent Parser ---
def parse_intent(state: AgentState):
    messages = state.get("messages", [])
    if not messages: return {}
    
    # FIX: Use helper to safely get text
    last_msg = get_message_text(messages[-1]).lower()
    
    if "start over" in last_msg or "reset" in last_msg:
        return {
            "destination": None, "check_in": None, "check_out": None,
            "guests": None, "budget_max": None, "hotels": [],
            "messages": [AIMessage(content="🔄 System reset. Where are we going next?")]
        }

    # Prepare Prompt
    today = date.today().strftime("%Y-%m-%d")
    llm = get_llm()
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"""
    You are an intelligent travel assistant. Today is {today}.
    Analyze the conversation history. Extract travel details.
    """
    
    try:
        # ChatOpenAI handles dicts in messages automatically
        intent: TravelIntent = structured_llm.invoke([SystemMessage(content=system_prompt)] + messages)
        
        updates = {}
        if intent.destination: updates["destination"] = intent.destination.title()
        if intent.check_in: 
            updates["check_in"] = intent.check_in
            try:
                dt = datetime.strptime(intent.check_in, "%Y-%m-%d")
                updates["check_out"] = (dt + timedelta(days=2)).strftime("%Y-%m-%d")
            except: pass
        if intent.guests: updates["guests"] = intent.guests
        if intent.budget_max: updates["budget_max"] = intent.budget_max
        
        # Handle manual hotel selection by number
        if state.get("hotels") and last_msg.strip().isdigit():
            idx = int(last_msg) - 1
            if 0 <= idx < len(state["hotels"]):
                updates["selected_hotel"] = state["hotels"][idx]

        return updates

    except Exception as e:
        print(f"LLM Error: {e}")
        return {} 

# --- 5. Node: Conversational Gatherer ---
def gather_requirements(state: AgentState):
    missing = []
    if not state.get("destination"): missing.append("Destination")
    if not state.get("check_in"): missing.append("Check-in Date")
    if not state.get("guests"): missing.append("Guest Count")
    if state.get("budget_max") is None: missing.append("Budget")

    if not missing:
        return {"requirements_complete": True}

    llm = get_llm()
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are Nomad, a witty travel agent. Ask the user for missing details: {missing_fields}. Keep it short."),
        MessagesPlaceholder(variable_name="messages"),
    ])
    
    chain = prompt | llm
    response = chain.invoke({
        "missing_fields": ", ".join(missing),
        "messages": state.get("messages", [])
    })
    
    return {"requirements_complete": False, "messages": [response]}

# --- 6. Node: Search Hotels ---
def search_hotels(state: AgentState):
    if not state.get("requirements_complete"): return {}
    if state.get("hotels"): return {} 
    
    city = state.get("destination")
    print(f"🔎 Searching for hotels in {city}...")
    
    try:
        headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
        
        # 1. Get Location
        r = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/locations", 
                        headers=headers, params={"name": city, "locale": "en-us"}, timeout=10)
        data = r.json()
        if not data: return {"messages": [AIMessage(content=f"⚠️ I couldn't find '{city}'.")]}
        
        dest_id, dest_type = data[0].get("dest_id"), data[0].get("dest_type")

        # 2. Search (with Retry)
        params = {
            "dest_id": dest_id, "dest_type": dest_type,
            "checkin_date": state["check_in"], "checkout_date": state["check_out"],
            "adults_number": str(state["guests"]), "units": "metric", 
            "filter_by_currency": "USD", "order_by": "price", "locale": "en-us"
        }
        
        raw_data = []
        for attempt in range(3):
            try:
                r = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/search", 
                                headers=headers, params=params, timeout=15)
                r.raise_for_status()
                raw_data = r.json().get("result", [])[:10]
                break
            except:
                if attempt < 2: time.sleep(2)
        
        # Filter
        final_list = []
        budget = state.get("budget_max", 10000)
        for h in raw_data:
            try: price = float(h.get("min_total_price", 150))
            except: price = 150.0
            if price <= budget:
                final_list.append({"name": h.get("hotel_name"), "price": price, "rating": h.get("review_score", "N/A")})
                if len(final_list) >= 5: break
        
        if not final_list:
            return {"messages": [AIMessage(content=f"😔 No hotels found under ${budget} in {city}.")]}
            
        options = "\n".join([f"{i+1}. {h['name']} - ${h['price']}" for i, h in enumerate(final_list)])
        msg = f"🎉 Options in {city} for {state['check_in']}:\n\n{options}\n\nReply with the number to book."
        return {"hotels": final_list, "messages": [AIMessage(content=msg)]}

    except Exception as e:
        return {"messages": [AIMessage(content=f"⚠️ Search failed: {str(e)}")]}

# --- 7. Node: Select Room & Book ---
def select_room(state: AgentState):
    if state.get("selected_hotel") and not state.get("room_options"):
        h = state["selected_hotel"]
        return {
            "room_options": [{"type": "Standard", "price": h["price"]}, {"type": "Suite", "price": h["price"]*1.5}],
            "messages": [AIMessage(content=f"Great! For {h['name']}, Standard (${h['price']}) or Suite (${h['price']*1.5})?")]
        }
    
    # FIX: Use helper to safely get text
    last_msg = get_message_text(state["messages"][-1]).lower()
    
    if "suite" in last_msg:
        return {"final_room_type": "Suite", "final_price": state["selected_hotel"]["price"]*1.5}
    if "standard" in last_msg or "1" in last_msg:
        return {"final_room_type": "Standard", "final_price": state["selected_hotel"]["price"]}
    return {}

def validate_booking(state: AgentState):
    if state.get("final_price") > state.get("budget_max", 10000) * 1.2:
        return {"messages": [AIMessage(content="⚠️ Price is over budget. Confirm?")]}
    return {}

def book_hotel(state: AgentState):
    if not state.get("final_room_type"): return {}
    
    details = f"{state['selected_hotel']['name']} ({state['final_room_type']})"
    res = warden_client.submit_booking(details, state["final_price"], state["destination"], 0.0)
    
    tx = res.get("tx_hash", "0xMOCK")
    msg = f"✅ Booked {details}!\nTransaction: {tx}\n\nAnything else?"
    return {"final_status": "Booked", "messages": [AIMessage(content=msg)]}

# --- 8. Routing ---
def route_step(state):
    if not state.get("requirements_complete"): return "end"
    if not state.get("hotels"): return "search"
    if not state.get("selected_hotel"): return "end"
    if not state.get("final_room_type"): return "select_room"
    if state.get("final_status") != "Booked": return "validate"
    return "book"

workflow = StateGraph(AgentState)
workflow.add_node("parse", parse_intent)
workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels)
workflow.add_node("select_room", select_room)
workflow.add_node("validate", validate_booking)
workflow.add_node("book", book_hotel)

workflow.set_entry_point("parse")
workflow.add_edge("parse", "gather")
workflow.add_conditional_edges("gather", route_step, {
    "end": END, "search": "search", "select_room": "select_room", "validate": "validate", "book": "book"
})
workflow.add_edge("search", END)
workflow.add_edge("select_room", END)
workflow.add_edge("validate", "book")
workflow.add_edge("book", END)

memory = MemorySaver()
workflow_app = workflow.compile(checkpointer=memory)