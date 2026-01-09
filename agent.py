import os
import requests
import time
import operator
import hashlib
import re
from datetime import date, timedelta, datetime
from typing import TypedDict, List, Optional, Annotated

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

import warden_client

load_dotenv()

# --- CONFIGURATION ---
BOOKING_KEY = os.getenv("BOOKING_API_KEY")
LLM_BASE_URL = "https://api.x.ai/v1" 
LLM_API_KEY = os.getenv("GROK_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_MODEL = "grok-3" if os.getenv("GROK_API_KEY") else "gpt-4o-mini"

FX_RATES_FALLBACK = {"GBP": 1.28, "EUR": 1.08, "USD": 1.0, "CAD": 0.74, "NGN": 0.00065}
REC_QUEUE = ["Kotor", "Budva", "Tivat", "Ulcinj", "Herceg Novi", "Podgorica", "Zabljak"]
HOTEL_CACHE = {}
CACHE_TTL = 3600

# --- 1. State Definition ---
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    destination: str
    suggested_cities: List[str]
    check_in: str
    check_out: str
    guests: int
    rooms: int
    budget_max: float
    currency: str          
    currency_symbol: str   
    info_request: str      
    hotel_cursor: int      
    hotels: List[dict]     
    selected_hotel: dict
    room_options: List[dict] 
    final_room_type: str
    final_price_per_night: float
    final_total_price_local: float 
    final_total_price_usd: float   
    final_status: str
    requirements_complete: bool
    waiting_for_booking_confirmation: bool 

# --- 2. Schema ---
class TravelIntent(BaseModel):
    destination: Optional[str] = Field(description="City/Country. If asking to recommend, INFER the best one.")
    wants_different_city: Optional[bool] = Field(description="True if user says 'no', 'next', 'another place'.")
    info_query: Optional[str] = Field(description="If user asks 'tell me about hotel X' or 'what is Kotor like?'.")
    budget_change: Optional[str] = Field(description="'down' for cheaper, 'up' for premium.")
    check_in: Optional[str] = Field(description="YYYY-MM-DD date.")
    check_out: Optional[str] = Field(description="YYYY-MM-DD date.")
    guests: Optional[int] = Field(description="Guest count.")
    budget_max: Optional[float] = Field(description="Explicit new budget.")
    currency: Optional[str] = Field(description="Currency code.")

# --- 3. Helpers ---
def get_llm():
    return ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL if "grok" in LLM_MODEL else None,
        temperature=0.7
    )

def get_message_text(msg):
    content = ""
    if hasattr(msg, 'content'): content = msg.content
    elif isinstance(msg, dict): content = msg.get('content', '')
    else: content = str(msg)
    if isinstance(content, list):
        parts = [str(p) if isinstance(p, str) else str(p.get("text", "")) for p in content]
        return " ".join(parts)
    return str(content)

def generate_cache_key(city, check_in, guests, currency):
    raw = f"{city}|{check_in}|{guests}|{currency}".lower()
    return hashlib.md5(raw.encode()).hexdigest()

def get_live_rate(base_currency):
    base = base_currency.upper()
    if base in ["USD", "USDC"]: return 1.0
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids=usd-coin&vs_currencies={base.lower()}"
        response = requests.get(url, timeout=3).json()
        return 1.0 / response['usd-coin'][base.lower()]
    except:
        return FX_RATES_FALLBACK.get(base, 1.0)

def extract_hotel_selection(text):
    text = text.lower()
    # 1. Direct digit "1", "3"
    digit_match = re.search(r"\b(\d+)\b", text)
    if digit_match:
        val = int(digit_match.group(1))
        if 1 <= val <= 10: return val - 1
    # 2. Ordinals
    ordinals = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}
    for word, idx in ordinals.items():
        if word in text: return idx
    return None

# --- 4. Nodes ---

def parse_intent(state: AgentState):
    messages = state.get("messages", [])
    if not messages: return {}
    last_msg = get_message_text(messages[-1]).lower()
    
    # --- RESET LOGIC ---
    if "reset" in last_msg or "start over" in last_msg:
        return {
            "destination": None, "suggested_cities": [], "check_in": None, "check_out": None,
            "guests": None, "budget_max": None, "hotels": [], "hotel_cursor": 0,
            "selected_hotel": None, "room_options": [], "waiting_for_booking_confirmation": False,
            "info_request": None, "requirements_complete": False,
            "messages": [AIMessage(content="🔄 System reset. Where are we going next?")]
        }

    # --- SELECTION CHECK (Before LLM to catch 'book first one') ---
    is_selecting_room = state.get("selected_hotel") and state.get("room_options")
    if state.get("hotels") and not is_selecting_room:
        selection_idx = extract_hotel_selection(last_msg)
        if selection_idx is not None and 0 <= selection_idx < len(state["hotels"]):
            return {"selected_hotel": state["hotels"][selection_idx], "room_options": [], "waiting_for_booking_confirmation": False}

    # --- LLM EXTRACTION ---
    today = date.today().strftime("%Y-%m-%d")
    current_budget = state.get("budget_max", 500)
    llm = get_llm()
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"You are Nomad. Today: {today}. Extract details. If user asks 'pick a place', INFER best city. If info needed, set info_query. If budget change, set budget_change."
    
    intent_data = {}
    try:
        intent = structured_llm.invoke([SystemMessage(content=system_prompt)] + messages)
        
        if intent.info_query: return {"info_request": intent.info_query}

        if intent.budget_change == "down":
            new_budget = current_budget * 0.75
            intent_data.update({"budget_max": new_budget, "hotel_cursor": 0, "hotels": [], "messages": [AIMessage(content=f"📉 Budget lowered to {state.get('currency_symbol','$')}{int(new_budget)}. Searching...")]})
        elif intent.budget_change == "up":
            new_budget = current_budget * 1.5
            intent_data.update({"budget_max": new_budget, "hotel_cursor": 0, "hotels": [], "messages": [AIMessage(content=f"💎 Budget increased to {state.get('currency_symbol','$')}{int(new_budget)}. Searching...")]})
        
        # --- RECOMMENDATION LOGIC (FIXED) ---
        # Only recommend if explicitly asked (wants_different_city) 
        # OR if the LLM inferred a destination from a request like "pick a place".
        # We REMOVED the "or (not destination)" check that was firing on "Hello".
        past_cities = state.get("suggested_cities", [])
        
        if intent.wants_different_city and not intent.budget_change:
            if state.get("destination") and state.get("destination") not in past_cities: past_cities.append(state.get("destination"))
            next_city = next((c for c in REC_QUEUE if c not in past_cities), None)
            if next_city:
                intent_data.update({"destination": next_city, "suggested_cities": past_cities + [next_city], "hotel_cursor": 0, "hotels": [], "selected_hotel": None, "messages": [AIMessage(content=f"Let's check **{next_city}**...")]})
            else:
                intent_data["messages"] = [AIMessage(content="I'm out of suggestions! Do you have a city in mind?")]
        
        # Standard Destination Set (User named it, or LLM inferred it from "pick a place")
        elif intent.destination:
            intent_data.update({"destination": intent.destination.title(), "hotel_cursor": 0})

        if intent.check_in: intent_data["check_in"] = intent.check_in
        if intent.check_out: intent_data["check_out"] = intent.check_out
        if intent.guests: intent_data["guests"] = intent.guests
        if intent.budget_max and not intent.budget_change: intent_data["budget_max"] = intent.budget_max
        if intent.currency: 
            intent_data["currency"] = intent.currency.upper()
            intent_data["currency_symbol"] = {"USD": "$", "GBP": "£", "EUR": "€"}.get(intent.currency.upper(), "$")

    except Exception as e: print(f"LLM Error {e}")

    # Confirmation Logic
    if state.get("waiting_for_booking_confirmation"):
        if any(w in last_msg for w in ["yes", "proceed", "confirm", "book"]): return {} 
        if intent_data.get("check_in") or intent_data.get("check_out"):
            intent_data.update({"waiting_for_booking_confirmation": False, "final_room_type": None, "messages": [AIMessage(content="🗓️ Dates updated. Please re-select room.")]})
            return intent_data
        return {"waiting_for_booking_confirmation": False, "room_options": [], "messages": [AIMessage(content="🚫 Booking cancelled.")]}

    # Pagination
    if any(w in last_msg for w in ["more", "next", "other hotels"]) and not intent.wants_different_city:
        intent_data.update({"hotel_cursor": state.get("hotel_cursor", 0) + 5, "hotels": [], "selected_hotel": None, "room_options": []})

    # Date Fix
    if intent_data.get("check_in") and not intent_data.get("check_out") and not state.get("check_out"):
         try: intent_data["check_out"] = (datetime.strptime(intent_data["check_in"], "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
         except: pass

    return intent_data

def gather_requirements(state: AgentState):
    missing = []
    if not state.get("destination"): missing.append("Destination")
    if not state.get("check_in"): missing.append("Check-in Date")
    if not state.get("guests"): missing.append("Guest Count")
    if state.get("budget_max") is None: missing.append("Budget")

    if not missing: return {"requirements_complete": True}

    llm = get_llm()
    msg = llm.invoke(f"Ask user for missing travel details: {', '.join(missing)}. Be concise.")
    return {"requirements_complete": False, "messages": [msg]}

def consultant_node(state: AgentState):
    query = state.get("info_request")
    if not query: return {}
    context = "\n".join([f"- {h['name']} ({h.get('location','')})" for h in state.get("hotels", [])[:5]])
    prompt = f"User asks: '{query}'. Context:\n{context}\nAnswer, then tell them to reply 'Book the first one' or 'Show more'."
    response = get_llm().invoke(prompt)
    return {"info_request": None, "messages": [response]}

def search_hotels(state: AgentState):
    if not state.get("requirements_complete"): return {}
    if state.get("selected_hotel"): return {} 
    
    city = state.get("destination")
    cursor = state.get("hotel_cursor", 0)
    
    # Cache Check
    cache_key = generate_cache_key(city, state["check_in"], state["guests"], state.get("currency","USD"))
    cached = HOTEL_CACHE.get(cache_key)
    raw_data = cached["data"] if cached and time.time()-cached["timestamp"] < CACHE_TTL else []
    
    if not raw_data:
        try:
            headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
            # Location ID
            r1 = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/locations", headers=headers, params={"name": city, "locale": "en-us"})
            loc_data = r1.json()
            if loc_data:
                dest_id = loc_data[0].get("dest_id")
                params = {
                    "dest_id": dest_id, "dest_type": loc_data[0].get("dest_type"),
                    "checkin_date": state["check_in"], "checkout_date": state["check_out"],
                    "adults_number": str(state["guests"]), "room_number": str(state.get("rooms",1)),
                    "units": "metric", "filter_by_currency": state.get("currency","USD"), "order_by": "price", "locale": "en-us"
                }
                r2 = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/search", headers=headers, params=params)
                raw_data = r2.json().get("result", [])[:40]
                HOTEL_CACHE[cache_key] = {"timestamp": time.time(), "data": raw_data}
        except Exception: pass

    try:
        d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
        d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except: nights = 1

    processed = []
    for h in raw_data:
        try:
            price = float(h.get("min_total_price", 0))
            if price == 0: continue
            processed.append({
                "name": h.get("hotel_name"), "price": round(price/nights, 2), "total": price,
                "stars": int(h.get("class", 0)), "rating_str": f"{h.get('review_score','N/A')}/10",
                "location": f"{h.get('city_trans','')}"
            })
        except: pass
    
    # Filter & Sort
    budget = state.get("budget_max", 10000)
    valid = [h for h in processed if h["total"] <= budget]
    if not valid: valid = sorted(processed, key=lambda x: x["price"])[:20] 
    else: valid.sort(key=lambda x: (x["stars"], x["price"]), reverse=(budget>200))
    
    batch = valid[cursor : cursor + 5]
    if not batch:
        return {"hotel_cursor": 0, "messages": [AIMessage(content=f"That's all the hotels I found in {city}! Say 'reset' to start over.")]}

    msg = "\n".join([f"{i+1}. **{h['name']}** ({h['location']})\n   {state.get('currency_symbol','$')}{h['total']} Total - {h['rating_str']}" for i, h in enumerate(batch)])
    return {"hotels": batch, "messages": [AIMessage(content=f"Here are options in **{city}**:\n\n{msg}\n\nReply with 'Book 1', 'Next', or 'Tell me about 1'.")]}

def select_room(state: AgentState):
    if state.get("selected_hotel") and not state.get("room_options"):
        h = state["selected_hotel"]
        sym = state.get("currency_symbol", "$")
        # Dummy Rooms
        opts = [{"type": "Standard Room", "price": h["total"]}, {"type": "Suite", "price": h["total"]*1.5}]
        msg = "\n".join([f"{i+1}. {r['type']} - {sym}{r['price']:.2f}" for i, r in enumerate(opts)])
        return {"room_options": opts, "messages": [AIMessage(content=f"Hotel: **{h['name']}**. Pick a room:\n{msg}")]}
    
    last_msg = get_message_text(state["messages"][-1]).lower()
    idx = extract_hotel_selection(last_msg) 
    options = state.get("room_options", [])
    
    selected_room = None
    if idx is not None and 0 <= idx < len(options): selected_room = options[idx]
    elif "suite" in last_msg: selected_room = options[1]
    elif "standard" in last_msg: selected_room = options[0]
    
    if selected_room:
        rate = get_live_rate(state.get("currency", "USD"))
        usd_total = selected_room["price"] * rate
        msg = f"**Confirm Booking**\nHotel: {state['selected_hotel']['name']}\nRoom: {selected_room['type']}\nTotal: {usd_total:.2f} USDC (Locked)\n\nReply 'Yes' to book."
        return {"final_room_type": selected_room["type"], "final_total_price_usd": usd_total, "waiting_for_booking_confirmation": True, "messages": [AIMessage(content=msg)]}
    
    return {"messages": [AIMessage(content="Please reply '1' for Standard or '2' for Suite.")]}

def book_hotel(state: AgentState):
    if not state.get("waiting_for_booking_confirmation"): return {}
    details = f"{state['selected_hotel']['name']} - {state['final_room_type']}"
    res = warden_client.submit_booking(details, state["final_total_price_usd"], state["destination"], 0.0)
    tx = res.get("tx_hash", "0xMOCK")
    return {"final_status": "Booked", "waiting_for_booking_confirmation": False, "messages": [AIMessage(content=f"✅ Booked! ID: {res.get('booking_ref','???')}\nTX: https://basescan.org/tx/{tx}")]}

# --- 5. Routing ---
def route_step(state):
    if state.get("info_request"): return "consultant"
    
    # STOP if requirements are missing
    if not state.get("requirements_complete"): return "end" 
    
    if state.get("selected_hotel"): return "select_room"
    
    if state.get("final_room_type"):
        last = get_message_text(state["messages"][-1]).lower()
        if any(w in last for w in ["yes", "confirm"]): return "book"
        return "end"
        
    if not state.get("hotels"): return "search"
    return "select_room"

# --- 6. Graph ---
workflow = StateGraph(AgentState)
workflow.add_node("parse", parse_intent)
workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels)
workflow.add_node("select_room", select_room)
workflow.add_node("book", book_hotel)
workflow.add_node("consultant", consultant_node)

workflow.set_entry_point("parse")
workflow.add_edge("parse", "gather")
workflow.add_conditional_edges("gather", route_step, {
    "end": END, "search": "search", "select_room": "select_room", 
    "consultant": "consultant", "book": "book"
})
workflow.add_edge("search", END)
workflow.add_edge("consultant", END)
workflow.add_edge("select_room", END)
workflow.add_edge("book", END)

memory = MemorySaver()
workflow_app = workflow.compile(checkpointer=memory)