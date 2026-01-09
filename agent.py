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
    destination: Optional[str] = Field(description="City/Country. ONLY if user explicitly names one.")
    wants_more_hotels: Optional[bool] = Field(description="True if user says 'next', 'more', 'show others'.")
    wants_different_city: Optional[bool] = Field(description="True ONLY if user says 'different city', 'somewhere else'.")
    info_query: Optional[str] = Field(description="If user asks 'tell me about hotel X' or 'what is London like?'.")
    budget_change: Optional[str] = Field(description="'down' for cheaper, 'up' for premium.")
    check_in: Optional[str] = Field(description="YYYY-MM-DD date.")
    check_out: Optional[str] = Field(description="YYYY-MM-DD date.")
    guests: Optional[int] = Field(description="Guest count.")
    budget_max: Optional[float] = Field(description="Explicit new budget.")
    currency: Optional[str] = Field(description="Currency code.")
    rejection: Optional[bool] = Field(description="True if user says 'no', 'don't want that'.")

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
    digit_match = re.search(r"\b(\d+)\b", text)
    if digit_match:
        val = int(digit_match.group(1))
        # Valid selection is 1-10. This avoids confusing years (2025) or budgets (400) with choices.
        if 1 <= val <= 10: return val - 1
    ordinals = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}
    for word, idx in ordinals.items():
        if word in text: return idx
    return None

# --- 4. Nodes ---

def parse_intent(state: AgentState):
    messages = state.get("messages", [])
    if not messages: return {}
    last_msg = get_message_text(messages[-1]).lower()
    
    # 1. RESET Logic
    if "reset" in last_msg or "start over" in last_msg:
        return {
            "destination": None, "suggested_cities": [], "check_in": None, "check_out": None,
            "guests": None, "budget_max": None, "hotels": [], "hotel_cursor": 0,
            "selected_hotel": None, "room_options": [], "waiting_for_booking_confirmation": False,
            "info_request": None, "requirements_complete": False,
            "messages": [AIMessage(content="🔄 System reset! Where are we jetting off to next?")]
        }

    # 2. LLM EXTRACTION (Must run first to catch Info/Rejection intents)
    today = date.today().strftime("%Y-%m-%d")
    current_budget = state.get("budget_max", 500)
    llm = get_llm()
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"You are Nomad. Today: {today}. Extract travel details. Detect 'info request' vs 'booking'."
    
    intent_data = {}
    try:
        intent = structured_llm.invoke([SystemMessage(content=system_prompt)] + messages)
        
        # PRIORITY 1: Info Request ("Tell me about hotel 4")
        if intent.info_query: 
            # We explicitly clear selection so we don't accidentally book it
            return {"info_request": intent.info_query, "selected_hotel": None}

        # PRIORITY 2: Rejection ("No", "Back to list")
        if intent.rejection:
             return {"selected_hotel": None, "messages": [AIMessage(content="Okay! Here is the list again. Which one catches your eye?")]}

        # Standard Intent Processing
        if intent.budget_change == "down":
            new_budget = current_budget * 0.75
            intent_data.update({"budget_max": new_budget, "hotel_cursor": 0, "hotels": [], "messages": [AIMessage(content=f"📉 Got it. Looking for deals around {state.get('currency_symbol','$')}{int(new_budget)}...")]})
        elif intent.budget_change == "up":
            new_budget = current_budget * 1.5
            intent_data.update({"budget_max": new_budget, "hotel_cursor": 0, "hotels": [], "messages": [AIMessage(content=f"💎 Understood. Showing premium options around {state.get('currency_symbol','$')}{int(new_budget)}...")]})
        
        # Pagination Logic (Strict: Only if hotels exist)
        is_viewing_hotels = len(state.get("hotels", [])) > 0
        wants_pagination = intent.wants_more_hotels or ("next" in last_msg and is_viewing_hotels and not intent.wants_different_city)
        
        if wants_pagination:
             intent_data.update({"hotel_cursor": state.get("hotel_cursor", 0) + 5, "hotels": [], "selected_hotel": None, "room_options": []})
        
        # City Change Logic (Must be explicit)
        elif intent.wants_different_city or (not state.get("destination") and not intent.destination and "pick" in last_msg):
            past_cities = state.get("suggested_cities", [])
            if state.get("destination") and state.get("destination") not in past_cities: past_cities.append(state.get("destination"))
            next_city = next((c for c in REC_QUEUE if c not in past_cities), None)
            if next_city:
                intent_data.update({"destination": next_city, "suggested_cities": past_cities + [next_city], "hotel_cursor": 0, "hotels": [], "selected_hotel": None, "messages": [AIMessage(content=f"How about **{next_city}**? Let me check availability...")]})
            else:
                intent_data["messages"] = [AIMessage(content="I'm out of suggestions! Do you have a city in mind?")]
        
        elif intent.destination:
            intent_data.update({"destination": intent.destination.title(), "hotel_cursor": 0})

        if intent.check_in: intent_data["check_in"] = intent.check_in
        if intent.check_out: intent_data["check_out"] = intent.check_out
        if intent.guests: intent_data["guests"] = intent.guests
        if intent.budget_max and not intent.budget_change: intent_data["budget_max"] = intent.budget_max
        if intent.currency: 
            intent_data["currency"] = intent.currency.upper()
            intent_data["currency_symbol"] = {"USD": "$", "GBP": "£", "EUR": "€"}.get(intent.currency.upper(), "$")

    except Exception: pass

    # --- 3. SELECTION CHECK ---
    # Only runs if no info request was found
    is_selecting_room = state.get("selected_hotel") and state.get("room_options")
    if state.get("hotels") and not is_selecting_room and not intent_data:
        selection_idx = extract_hotel_selection(last_msg)
        if selection_idx is not None and 0 <= selection_idx < len(state["hotels"]):
            # CRITICAL FIX: Clear room_options so select_room knows to start fresh
            return {"selected_hotel": state["hotels"][selection_idx], "room_options": [], "waiting_for_booking_confirmation": False}

    # Confirmation Logic
    if state.get("waiting_for_booking_confirmation"):
        if any(w in last_msg for w in ["yes", "proceed", "confirm", "book", "ok", "do it"]): return {} 
        if intent_data.get("check_in") or intent_data.get("check_out"):
            intent_data.update({"waiting_for_booking_confirmation": False, "final_room_type": None, "messages": [AIMessage(content="🗓️ Dates updated. Please re-select room.")]})
            return intent_data
        return {"waiting_for_booking_confirmation": False, "room_options": [], "messages": [AIMessage(content="🚫 Booking cancelled.")]}

    # Date Fix
    if intent_data.get("check_in") and not intent_data.get("check_out") and not state.get("check_out"):
         try: intent_data["check_out"] = (datetime.strptime(intent_data["check_in"], "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
         except: pass

    return intent_data

def gather_requirements(state: AgentState):
    missing = []
    if not state.get("destination"): missing.append("Destination")
    if not state.get("check_in"): missing.append("Check-in Date")
    if not state.get("guests"): missing.append("Number of Guests")
    if state.get("budget_max") is None: missing.append("Budget")

    if not missing: return {"requirements_complete": True}

    llm = get_llm()
    current_context = ""
    if state.get("destination"): current_context += f"Destination is {state['destination']}. "
    
    prompt = f"""
    You are Nomad, a friendly and witty travel concierge.
    Current Context: {current_context}
    You need to ask the user for these missing details: {', '.join(missing)}.
    If 'Destination' is missing and it's the start of the chat, give a warm, short welcome intro before asking.
    If 'Destination' is KNOWN (e.g. London), acknowledge it enthusiastically and ask for the rest.
    Keep it conversational and fun.
    """
    msg = llm.invoke(prompt)
    return {"requirements_complete": False, "messages": [msg]}

def consultant_node(state: AgentState):
    query = state.get("info_request")
    if not query: return {}
    context = "\n".join([f"- {h['name']} ({h.get('location','')})" for h in state.get("hotels", [])[:5]])
    
    prompt = f"""
    User asks: '{query}'. 
    Context of available hotels:\n{context}
    
    Answer as a knowledgeable local guide. Be witty.
    IMPORTANT: End your response by explicitly asking: 
    "Do you want to book this one? Reply 'Yes' to proceed, or 'No' to see the list again."
    """
    response = get_llm().invoke(prompt)
    # The 'Yes' response will need to be handled by the router/parser in next turn
    return {"info_request": None, "messages": [response]}

def search_hotels(state: AgentState):
    if not state.get("requirements_complete"): return {}
    # If a hotel is selected, skip search (unless we are just browsing)
    if state.get("selected_hotel"): return {} 
    
    city = state.get("destination")
    cursor = state.get("hotel_cursor", 0)
    
    cache_key = generate_cache_key(city, state["check_in"], state["guests"], state.get("currency","USD"))
    cached = HOTEL_CACHE.get(cache_key)
    raw_data = cached["data"] if cached and time.time()-cached["timestamp"] < CACHE_TTL else []
    
    if not raw_data:
        try:
            headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
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
    
    budget = state.get("budget_max", 10000)
    valid = [h for h in processed if h["total"] <= budget]
    if not valid: valid = sorted(processed, key=lambda x: x["price"])[:20] 
    else: valid.sort(key=lambda x: (x["stars"], x["price"]), reverse=(budget>200))
    
    batch = valid[cursor : cursor + 5]
    if not batch:
        return {"hotel_cursor": 0, "messages": [AIMessage(content=f"That's all the hotels I found in **{city}**! 😕\n\nSay 'reset' to search elsewhere.")]}

    msg = "\n".join([f"{i+1}. **{h['name']}** ({h['location']})\n   {state.get('currency_symbol','$')}{h['total']} Total - {h['rating_str']}" for i, h in enumerate(batch)])
    
    intro = f"🎉 **Great choice!** Here are the best options I found in **{city}**:\n\n"
    outro = "\n\nReply with **'Book 1'**, **'Next'**, or ask **'Tell me about hotel 1'**!"
    return {"hotels": batch, "messages": [AIMessage(content=intro + msg + outro)]}

def select_room(state: AgentState):
    # This node is triggered if selected_hotel is SET and room_options is EMPTY.
    if state.get("selected_hotel") and not state.get("room_options"):
        h = state["selected_hotel"]
        sym = state.get("currency_symbol", "$")
        opts = [{"type": "Standard Room", "price": h["total"]}, {"type": "Suite", "price": h["total"]*1.5}]
        msg = "\n".join([f"{i+1}. **{r['type']}** - {sym}{r['price']:.2f}" for i, r in enumerate(opts)])
        return {"room_options": opts, "messages": [AIMessage(content=f"🏨 **{h['name']}** is a solid pick!\n\nWhich room do you prefer?\n\n{msg}\n\nReply '1' or '2'." )]}
    
    last_msg = get_message_text(state["messages"][-1]).lower()
    
    # Check for confirmation of the previous step
    if state.get("final_room_type") and any(w in last_msg for w in ["yes", "confirm", "proceed"]):
        return {} 

    idx = extract_hotel_selection(last_msg) 
    options = state.get("room_options", [])
    
    selected_room = None
    if idx is not None and 0 <= idx < len(options): selected_room = options[idx]
    elif "suite" in last_msg: selected_room = options[1]
    elif "standard" in last_msg: selected_room = options[0]
    
    if selected_room:
        rate = get_live_rate(state.get("currency", "USD"))
        usd_total = selected_room["price"] * rate
        
        msg = f"""📝 **Trip Summary**

🏨 **Hotel:** {state['selected_hotel']['name']}
📍 **Location:** {state['selected_hotel']['location']}
🛏️ **Room:** {selected_room['type']}
📅 **Dates:** {state['check_in']} to {state['check_out']}
💵 **Local Cost:** {state.get('currency_symbol','$')}{selected_room['price']:.2f}

🔄 **Crypto Payment:**
**Total:** {usd_total:.2f} USDC (Rate Locked 🔒)

Ready to fly? Reply **'Yes'** to book! 🚀"""
        
        return {"final_room_type": selected_room["type"], "final_total_price_usd": usd_total, "waiting_for_booking_confirmation": True, "messages": [AIMessage(content=msg)]}
    
    return {"messages": [AIMessage(content="🤔 I didn't catch that. Please reply '1' for Standard or '2' for Suite.")]}

def book_hotel(state: AgentState):
    if not state.get("waiting_for_booking_confirmation"): return {}
    details = f"{state['selected_hotel']['name']} - {state['final_room_type']}"
    res = warden_client.submit_booking(details, state["final_total_price_usd"], state["destination"], 0.0)
    tx = res.get("tx_hash", "0xMOCK")
    
    msg = f"""✅ **Booking Confirmed!**

Your trip to **{state['destination']}** is locked in. 🌍

🆔 **Booking ID:** {res.get('booking_ref','NOMAD-77X')}
🔗 **Transaction:** [View on BaseScan](https://basescan.org/tx/{tx})

Pack your bags! 🎒"""
    return {"final_status": "Booked", "waiting_for_booking_confirmation": False, "messages": [AIMessage(content=msg)]}

# --- 5. Routing ---
def route_step(state):
    if state.get("info_request"): return "consultant"
    if not state.get("requirements_complete"): return "end" 
    
    # If selected_hotel is set, go to select_room to show room options or confirm booking
    if state.get("selected_hotel"): return "select_room"
    
    if state.get("final_room_type"):
        last = get_message_text(state["messages"][-1]).lower()
        if any(w in last for w in ["yes", "confirm", "proceed", "book", "ok", "do it"]): return "book"
        return "end"
        
    if not state.get("hotels"): return "search"
    return "select_room" # Fallback if user typing random things while list is shown

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