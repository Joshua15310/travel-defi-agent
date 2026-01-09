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

# --- FIX: Using standard Pydantic library directly ---
from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

import warden_client

load_dotenv()

# --- CONFIGURATION ---
BOOKING_KEY = os.getenv("BOOKING_API_KEY")

# We use ChatOpenAI client but point it to xAI's base_url for Grok
# This allows us to use Grok's reasoning with LangChain's tools
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
    # Internal flags
    date_just_set: bool 
    requirements_complete: bool

# --- 2. Structured Output Schema (The "Brain" Structure) ---
class TravelIntent(BaseModel):
    """Structure for extracting travel details from natural conversation."""
    destination: Optional[str] = Field(description="City or country name. None if asking for suggestions.")
    check_in: Optional[str] = Field(description="YYYY-MM-DD date. Calculate from relative terms like 'next friday'.")
    guests: Optional[int] = Field(description="Number of people. Infer from context (e.g., 'me and wife' = 2).")
    budget_max: Optional[float] = Field(description="Maximum price per night in USD.")
    user_intent_type: str = Field(description="One of: 'booking_request', 'general_chat', 'asking_suggestion', 'reset'")

# --- 3. Helpers ---
def get_llm():
    """Initializes the LLM connection (Grok or OpenAI)."""
    if not LLM_API_KEY:
        # Fallback to a clear error if no key is found
        print("⚠️ Warning: No LLM API Key found. Chat features may fail.")
    
    return ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL if "grok" in LLM_MODEL else None,
        temperature=0.7 # Slight creativity for friendly conversation
    )

# --- 4. Node: Intelligent Intent Parser (Grok Powered) ---
def parse_intent(state: AgentState):
    """Uses Grok to understand natural language and extract booking fields."""
    messages = state.get("messages", [])
    if not messages: return {}
    
    # 1. Check for Reset first (Simple keyword check is faster/safer)
    last_msg = messages[-1].content.lower()
    if "start over" in last_msg or "reset" in last_msg:
        return {
            "destination": None, "check_in": None, "check_out": None,
            "guests": None, "budget_max": None, "hotels": [],
            "messages": [AIMessage(content="🔄 System reset. Where are we going next?")]
        }

    # 2. Prepare Grok Prompt
    today = date.today().strftime("%Y-%m-%d")
    llm = get_llm()
    
    # We ask Grok to extract data OR tell us if it's just chat
    # "with_structured_output" forces the LLM to return JSON matching our TravelIntent schema
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"""
    You are an intelligent travel assistant. Today is {today}.
    Analyze the conversation history. Extract travel details into the JSON format.
    
    Rules:
    - If user says "me and my wife", guests = 2.
    - If user says "next friday", calculate the date YYYY-MM-DD.
    - If user says "suggest a place", leave destination as null and set type to 'asking_suggestion'.
    - If user says "hello", set type to 'general_chat'.
    - Ignore currency symbols, extract numeric budget value.
    """
    
    try:
        # Pass the full conversation history so Grok understands context
        intent: TravelIntent = structured_llm.invoke([SystemMessage(content=system_prompt)] + messages)
        
        updates = {}
        if intent.destination: updates["destination"] = intent.destination.title()
        if intent.check_in: 
            updates["check_in"] = intent.check_in
            # Auto-set checkout to +2 days if not specified (simplification)
            try:
                dt = datetime.strptime(intent.check_in, "%Y-%m-%d")
                updates["check_out"] = (dt + timedelta(days=2)).strftime("%Y-%m-%d")
            except: pass
        if intent.guests: updates["guests"] = intent.guests
        if intent.budget_max: updates["budget_max"] = intent.budget_max
        
        # Handle "Number selection" for hotels (Manual fallback for stability)
        if state.get("hotels") and last_msg.strip().isdigit():
            idx = int(last_msg) - 1
            if 0 <= idx < len(state["hotels"]):
                updates["selected_hotel"] = state["hotels"][idx]

        return updates

    except Exception as e:
        print(f"LLM Error: {e}")
        return {} # Fallback to no updates if LLM fails

# --- 5. Node: Conversational Gatherer (Grok Powered) ---
def gather_requirements(state: AgentState):
    """Decides if we have enough info. If not, Grok generates a friendly question."""
    
    # Check what is missing
    missing = []
    if not state.get("destination"): missing.append("Destination (City/Country)")
    if not state.get("check_in"): missing.append("Check-in Date")
    if not state.get("guests"): missing.append("Number of Guests")
    if state.get("budget_max") is None: missing.append("Budget per night")

    # If nothing missing, we are ready to search!
    if not missing:
        return {"requirements_complete": True}

    # If info is missing, ask Grok to generate the specific question
    llm = get_llm()
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are Nomad, a witty and friendly travel agent. 
        The user wants to travel but some details are missing: {missing_fields}.
        Current known details: {state_summary}.
        
        Your Goal:
        1. If the user asked for suggestions (e.g., "Where should I go?"), suggest 2-3 exciting destinations based on their vibe, then ask which one they prefer.
        2. If they just said "Hello", greet them warmly and ask where they want to go.
        3. Otherwise, politely ask for the missing details in a conversational way (don't act like a robot form).
        4. Keep it short (under 2 sentences).
        """),
        MessagesPlaceholder(variable_name="messages"),
    ])
    
    chain = prompt | llm
    
    # We pass the conversation history so Grok knows if the user just asked a question
    response = chain.invoke({
        "missing_fields": ", ".join(missing),
        "state_summary": str(state),
        "messages": state.get("messages", [])
    })
    
    return {"requirements_complete": False, "messages": [response]}

# --- 6. Node: Search Hotels (API + Retry) ---
def search_hotels(state: AgentState):
    # Only run if requirements are complete
    if not state.get("requirements_complete"): return {}
    if state.get("hotels"): return {} # Don't search twice
    
    city = state.get("destination")
    print(f"🔎 Searching for hotels in {city}...")
    
    try:
        # 1. Get Location ID
        loc_url = "https://booking-com.p.rapidapi.com/v1/hotels/locations"
        headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
        r = requests.get(loc_url, headers=headers, params={"name": city, "locale": "en-us"}, timeout=10)
        data = r.json()
        if not data: return {"messages": [AIMessage(content=f"⚠️ I couldn't find any hotels in '{city}'.")]}
        
        dest_id, dest_type = data[0].get("dest_id"), data[0].get("dest_type")

        # 2. Search Hotels (With Retry)
        search_url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
        params = {
            "dest_id": dest_id, "dest_type": dest_type,
            "checkin_date": state["check_in"], "checkout_date": state["check_out"],
            "adults_number": str(state["guests"]), "units": "metric", 
            "filter_by_currency": "USD", "order_by": "price", "locale": "en-us"
        }
        
        # Retry Logic
        max_retries = 3
        raw_data = []
        for attempt in range(max_retries):
            try:
                r = requests.get(search_url, headers=headers, params=params, timeout=15)
                r.raise_for_status()
                raw_data = r.json().get("result", [])[:10]
                break
            except:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return {"messages": [AIMessage(content="😔 The hotel service is busy. Please try again.")]}
        
        # Filter and Format
        final_list = []
        budget = state.get("budget_max", 10000)
        
        for h in raw_data:
            try: price = float(h.get("min_total_price", 150))
            except: price = 150.0
            if price <= budget:
                final_list.append({"name": h.get("hotel_name"), "price": price, "rating": h.get("review_score", "N/A")})
                if len(final_list) >= 5: break
        
        if not final_list:
            return {"messages": [AIMessage(content=f"😔 I found hotels in {city}, but none under ${budget}. Shall we raise the budget?")]}
            
        options = "\n".join([f"{i+1}. {h['name']} - ${h['price']}" for i, h in enumerate(final_list)])
        msg = f"🎉 I found these great options in {city} for {state['check_in']}:\n\n{options}\n\nWhich number should I book for you?"
        return {"hotels": final_list, "messages": [AIMessage(content=msg)]}

    except Exception as e:
        return {"messages": [AIMessage(content=f"⚠️ Search failed: {str(e)}")]}

# --- 7. Node: Select Room & Book (Warden Integration) ---
def select_room(state: AgentState):
    if state.get("selected_hotel") and not state.get("room_options"):
        # Simple simulation of room types
        h = state["selected_hotel"]
        return {
            "room_options": [{"type": "Standard", "price": h["price"]}, {"type": "Suite", "price": h["price"]*1.5}],
            "messages": [AIMessage(content=f"Great choice! For {h['name']}, do you want the Standard Room (${h['price']}) or the Suite (${h['price']*1.5})?")]
        }
    
    # If user selected room type (via text), we map it here
    last_msg = state["messages"][-1].content.lower()
    if "suite" in last_msg:
        return {"final_room_type": "Suite", "final_price": state["selected_hotel"]["price"]*1.5}
    if "standard" in last_msg or "1" in last_msg:
        return {"final_room_type": "Standard", "final_price": state["selected_hotel"]["price"]}
    return {}

def validate_booking(state: AgentState):
    # Safety Check: Guardrail
    if state.get("final_price") > state.get("budget_max", 10000) * 1.2:
        return {"messages": [AIMessage(content="⚠️ Wait! This room is significantly over your budget. confirm?")]}
    return {}

def book_hotel(state: AgentState):
    if not state.get("final_room_type"): return {}
    
    # Call Warden SDK
    details = f"{state['selected_hotel']['name']} ({state['final_room_type']})"
    res = warden_client.submit_booking(details, state["final_price"], state["destination"], 0.0)
    
    tx = res.get("tx_hash", "0xMOCK")
    msg = f"✅ All done! I've booked the {details} for you.\nTransaction: {tx}\n\nCan I help you with anything else?"
    return {"final_status": "Booked", "messages": [AIMessage(content=msg)]}

# --- 8. Routing & Graph ---
def route_step(state):
    # If we are missing requirements, go back to gather (which talks to user)
    if not state.get("requirements_complete"):
        return "end" # We return to user to let them answer the question
    
    if not state.get("hotels"): return "search"
    if not state.get("selected_hotel"): return "end" # Wait for user selection
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

# We update the edges to loop efficiently
workflow.add_edge("parse", "gather")
# From gather, we check if we need to talk to user or proceed
workflow.add_conditional_edges("gather", route_step, {
    "end": END,
    "search": "search",
    "select_room": "select_room",
    "validate": "validate",
    "book": "book"
})
workflow.add_edge("search", END)       # Return results to user
workflow.add_edge("select_room", END)  # Ask user for room
workflow.add_edge("validate", "book")  # If valid, go to book
workflow.add_edge("book", END)

memory = MemorySaver()
workflow_app = workflow.compile(checkpointer=memory)