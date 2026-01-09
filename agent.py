# agent.py
import os
import requests
import time
import operator
import hashlib
import json
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
LLM_MODEL = "grok-3" if os.getenv("GROK_API_KEY") else "gpt-4o-mini"

# Fallback Rates
FX_RATES_FALLBACK = {"GBP": 1.28, "EUR": 1.08, "USD": 1.0, "CAD": 0.74, "NGN": 0.00065}

# Recommendation Queue (The "Playlist" of cities)
REC_QUEUE = ["Kotor", "Budva", "Tivat", "Ulcinj", "Herceg Novi", "Podgorica", "Zabljak"]

# --- GLOBAL CACHE ---
HOTEL_CACHE = {}
CACHE_TTL = 3600

# --- 1. State Definition ---
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    destination: str
    suggested_cities: List[str] # <--- NEW: Tracks what we've already shown
    check_in: str
    check_out: str
    guests: int
    rooms: int
    budget_max: float
    currency: str          
    currency_symbol: str   
    
    # Cache & Pagination
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
    trip_type: str
    waiting_for_booking_confirmation: bool 

# --- 2. Structured Output Schema ---
class TravelIntent(BaseModel):
    destination: Optional[str] = Field(description="City/Country. If user asks to recommend, INFER the best one.")
    wants_different_city: Optional[bool] = Field(description="True if user says 'no', 'next', 'another place', 'don't like this city'.")
    check_in: Optional[str] = Field(description="YYYY-MM-DD date.")
    check_out: Optional[str] = Field(description="YYYY-MM-DD date.")
    guests: Optional[int] = Field(description="Guest count.")
    budget_max: Optional[float] = Field(description="Budget.")
    currency: Optional[str] = Field(description="Currency code.")
    trip_context: Optional[str] = Field(description="Context.")

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

# --- 4. Node: Intelligent Intent Parser ---
def parse_intent(state: AgentState):
    messages = state.get("messages", [])
    if not messages: return {}
    last_msg = get_message_text(messages[-1]).lower()
    
    # 1. Reset
    if "start over" in last_msg or "reset" in last_msg:
        return {
            "destination": None, "suggested_cities": [], "check_in": None, "check_out": None,
            "guests": None, "budget_max": None, "hotels": [], "hotel_cursor": 0,
            "selected_hotel": None, "room_options": [], "waiting_for_booking_confirmation": False,
            "messages": [AIMessage(content="🔄 System reset. Where are we going next?")]
        }

    # 2. Extract Intent
    today = date.today().strftime("%Y-%m-%d")
    current_checkin = state.get("check_in", "Not set")
    current_checkout = state.get("check_out", "Not set")
    
    llm = get_llm()
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"""
    You are Nomad. Today is {today}. Itinerary: {current_checkin} to {current_checkout}.
    RULES:
    1. Extract destination, dates, guests.
    2. If user says "add 2 days", calculate new check_out.
    3. If user says "pick a place" or "recommend", leave destination EMPTY (logic handles it).
    4. If user says "no", "next place", "don't like it", set 'wants_different_city' = True.
    """
    
    intent_data = {}
    try:
        intent: TravelIntent = structured_llm.invoke([SystemMessage(content=system_prompt)] + messages)
        
        # --- A. HANDLE "PICK ANOTHER PLACE" ---
        past_cities = state.get("suggested_cities", [])
        
        # Logic: If user specifically wants a CHANGE, or if NO destination is set yet (and asking for one)
        wants_recommendation = intent.wants_different_city or (not state.get("destination") and not intent.destination)
        
        if wants_recommendation:
            # If we currently have a city, add it to 'past' so we don't repeat it
            if state.get("destination") and state.get("destination") not in past_cities:
                past_cities.append(state.get("destination"))
            
            # Find next city in queue that hasn't been suggested
            next_city = None
            for city in REC_QUEUE:
                if city not in past_cities:
                    next_city = city
                    break
            
            if next_city:
                intent_data["destination"] = next_city
                intent_data["suggested_cities"] = past_cities + [next_city]
                intent_data["hotel_cursor"] = 0 # Reset pagination
                intent_data["hotels"] = []      # Clear old hotels
                intent_data["selected_hotel"] = None # Clear old selection
                intent_data["room_options"] = []
                # Provide a transition message
                intent_data["messages"] = [AIMessage(content=f"Okay, let's look at **{next_city}** instead. Checking availability...")]
            else:
                intent_data["messages"] = [AIMessage(content="I've gone through my top recommendations! Do you have a specific city in mind?")]

        # --- B. HANDLE STANDARD EXTRACTION ---
        elif intent.destination: 
            intent_data["destination"] = intent.destination.title()
            intent_data["hotel_cursor"] = 0
        
        if intent.check_in: intent_data["check_in"] = intent.check_in
        if intent.check_out: intent_data["check_out"] = intent.check_out
        if intent.guests: intent_data["guests"] = intent.guests
        if intent.budget_max: intent_data["budget_max"] = intent.budget_max
        if intent.currency:
            curr = intent.currency.upper()
            intent_data["currency"] = curr
            symbols = {"USD": "$", "GBP": "£", "EUR": "€", "NGN": "₦"}
            intent_data["currency_symbol"] = symbols.get(curr, curr + " ")
        if intent.trip_context: intent_data["trip_type"] = intent.trip_context

    except Exception as e: print(f"LLM Error: {e}")

    # 3. Confirmation Logic
    if state.get("waiting_for_booking_confirmation"):
        if any(w in last_msg for w in ["yes", "proceed", "confirm", "book", "pay"]): return {} 
        if intent_data.get("check_in") or intent_data.get("check_out"):
            updates = intent_data
            updates["waiting_for_booking_confirmation"] = False
            updates["final_room_type"] = None 
            updates["messages"] = [AIMessage(content=f"🗓️ Dates updated. Please re-select your room.")]
            return updates
        # If changing city during confirmation, the destination logic above handles it implicitly by overwriting 'destination'
        return {"waiting_for_booking_confirmation": False, "room_options": [], "messages": [AIMessage(content="🚫 Booking cancelled.")]}

    # 4. Pagination (More Hotels)
    if any(w in last_msg for w in ["more", "next", "other hotels", "fancier", "cheaper"]) and not intent.wants_different_city:
        current_cursor = state.get("hotel_cursor", 0)
        return {"hotel_cursor": current_cursor + 5, "hotels": [], "selected_hotel": None, "room_options": []}

    # 5. Hotel Selection
    is_selecting_room = state.get("selected_hotel") and state.get("room_options")
    if state.get("hotels") and not is_selecting_room and last_msg.strip().isdigit():
        idx = int(last_msg) - 1
        if 0 <= idx < len(state["hotels"]):
            return {"selected_hotel": state["hotels"][idx], "room_options": [], "waiting_for_booking_confirmation": False}

    # 6. Final Cleanups
    updates = intent_data
    if updates.get("check_in") and not updates.get("check_out") and not state.get("check_out"):
         try:
            dt = datetime.strptime(updates["check_in"], "%Y-%m-%d")
            updates["check_out"] = (dt + timedelta(days=2)).strftime("%Y-%m-%d")
         except: pass

    return updates

# --- 5. Node: Conversational Gatherer ---
def gather_requirements(state: AgentState):
    missing = []
    if not state.get("destination"): missing.append("Destination")
    if not state.get("check_in"): missing.append("Check-in Date")
    if not state.get("guests"): missing.append("Guest Count")
    if state.get("budget_max") is None: missing.append("Budget")

    if not missing: return {"requirements_complete": True}

    llm = get_llm()
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are Nomad. Ask for missing details: {missing_fields}. Be concise."),
        MessagesPlaceholder(variable_name="messages"),
    ])
    chain = prompt | llm
    response = chain.invoke({"missing_fields": ", ".join(missing), "messages": state.get("messages", [])})
    return {"requirements_complete": False, "messages": [response]}

# --- 6. Node: Search Hotels ---
def _fetch_hotels_raw(city, check_in, check_out, guests, rooms, currency):
    cache_key = generate_cache_key(city, check_in, guests, currency)
    cached_entry = HOTEL_CACHE.get(cache_key)
    if cached_entry and time.time() - cached_entry["timestamp"] < CACHE_TTL:
        return cached_entry["data"]
    try:
        headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
        r = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/locations", 
                        headers=headers, params={"name": city, "locale": "en-us"}, timeout=10)
        data = r.json()
        if not data: return []
        dest_id, dest_type = data[0].get("dest_id"), data[0].get("dest_type")
        params = {
            "dest_id": dest_id, "dest_type": dest_type,
            "checkin_date": check_in, "checkout_date": check_out,
            "adults_number": str(guests), "room_number": str(rooms),
            "units": "metric", "filter_by_currency": currency, 
            "order_by": "price", "locale": "en-us"
        }
        res = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/search", 
                           headers=headers, params=params, timeout=15)
        raw_results = res.json().get("result", [])[:40]
        HOTEL_CACHE[cache_key] = {"timestamp": time.time(), "data": raw_results}
        return raw_results
    except: return []

def search_hotels(state: AgentState):
    if not state.get("requirements_complete"): return {}
    if state.get("selected_hotel"): return {} 
    
    city = state.get("destination")
    rooms = state.get("rooms", 1)
    currency = state.get("currency", "USD")
    symbol = state.get("currency_symbol", "$")
    cursor = state.get("hotel_cursor", 0)
    
    try:
        d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
        d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except: nights = 1

    raw_data = _fetch_hotels_raw(city, state["check_in"], state["check_out"], state["guests"], rooms, currency)
    used_city = city
    
    # Logic: If no results, try pivoting (simplified here)
    if not raw_data and cursor == 0:
        pass # In production, add city pivot logic here

    all_processed = []
    for h in raw_data:
        try: 
            total_price = float(h.get("min_total_price", 0))
            if total_price == 0: continue
            price_per_night = round(total_price / nights, 2)
            stars = int(h.get("class", 0))
            star_str = "⭐" * stars if stars > 0 else f"Rating: {h.get('review_score', 'N/A')}"
            all_processed.append({
                "name": h.get("hotel_name"), "price": price_per_night, 
                "total": total_price, "rating_str": star_str, "stars": stars
            })
        except: pass
    
    budget = state.get("budget_max", 10000)
    valid_hotels = [h for h in all_processed if h["price"] <= budget]
    if not valid_hotels:
        valid_hotels = sorted(all_processed, key=lambda x: x["price"])[:20]
        msg_intro = f"⚠️ Nothing under {symbol}{budget}. Cheapest nearby:"
    else:
        if budget > 200:
            valid_hotels.sort(key=lambda x: (x["stars"], x["price"]), reverse=True)
            msg_intro = f"✨ Top options in **{used_city}** (Luxury First):"
        else:
            valid_hotels.sort(key=lambda x: x["price"])
            msg_intro = f"🎉 Best value options in **{used_city}**:"

    batch = valid_hotels[cursor : cursor + 5]
    if not batch:
        return {"hotel_cursor": 0, "messages": [AIMessage(content=f"That's all the hotels I found in {used_city}!")]}

    options = "\n".join([f"{i+1}. **{h['name']}** - {symbol}{h['price']}/night ({h['rating_str']})" for i, h in enumerate(batch)])
    msg = f"{msg_intro}\n\n{options}\n\nReply with the number to book." if cursor == 0 else f"Here are **5 more options**:\n\n{options}\n\nReply with the number to book."
    return {"hotels": batch, "messages": [AIMessage(content=msg)]}

# --- 7. Node: Select Room ---
def select_room(state: AgentState):
    if state.get("selected_hotel") and not state.get("room_options"):
        h = state["selected_hotel"]
        sym = state.get("currency_symbol", "$")
        room_options = [
            {"type": "Standard Room", "price": h["price"]}, 
            {"type": "Ocean View Suite", "price": round(h["price"]*1.5, 2)}
        ]
        rooms_list = "\n".join([f"{i+1}. **{r['type']}** - {sym}{r['price']}/night" for i, r in enumerate(room_options)])
        msg = f"Great! For **{h['name']}**, please choose a room:\n\n{rooms_list}\n\nReply with '1' or '2'."
        return {"room_options": room_options, "messages": [AIMessage(content=msg)]}
    
    last_msg = get_message_text(state["messages"][-1]).lower()
    options = state.get("room_options", [])
    
    selected_room = None
    if last_msg.strip().isdigit():
        idx = int(last_msg) - 1
        if 0 <= idx < len(options): selected_room = options[idx]
    elif "suite" in last_msg or "ocean" in last_msg: selected_room = options[1]
    elif "standard" in last_msg or "basic" in last_msg: selected_room = options[0]
         
    if selected_room:
        try:
            d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
            d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
            nights = max(1, (d2 - d1).days)
        except: nights = 1
        
        local_total = selected_room["price"] * nights
        currency = state.get("currency", "USD")
        sym = state.get("currency_symbol", "$")
        rate = get_live_rate(currency)
        usd_total = local_total * rate
        
        msg = f"""Summary of your trip:
        
🏨 **Hotel:** {state['selected_hotel']['name']}
🛏️ **Room:** {selected_room['type']}
📅 **Duration:** {nights} Nights ({state['check_in']} to {state['check_out']})
💵 **Local Cost:** {sym}{local_total:.2f}

🔄 **Payment:**
We process payments in **USDC on Base**.
Rate: 1 {currency} ≈ {rate:.4f} USDC
**TOTAL TO PAY:** {usd_total:.2f} USDC

Reply 'Yes' or 'Confirm' to execute the booking.
(Or say "add 2 days" to extend your stay)"""
        return {"final_room_type": selected_room["type"], "final_total_price_local": local_total, "final_total_price_usd": usd_total, "waiting_for_booking_confirmation": True, "messages": [AIMessage(content=msg)]}

    return {"messages": [AIMessage(content="⚠️ Please reply '1' for Standard or '2' for Suite.")]}

# --- 8. Node: Book Hotel ---
def book_hotel(state: AgentState):
    if not state.get("waiting_for_booking_confirmation"): return {}
    details = f"{state['selected_hotel']['name']} ({state['final_room_type']}) [Chain: Base | Token: USDC]"
    res = warden_client.submit_booking(details, state["final_total_price_usd"], state["destination"], 0.0)
    tx = res.get("tx_hash", "0xMOCK")
    booking_id = res.get("booking_ref", f"BK-{tx[-6:].upper()}")
    sym = state.get("currency_symbol", "$")
    msg = f"""✅ Success! Your trip is booked.

🏨 **Hotel:** {state['selected_hotel']['name']}
🆔 **Booking ID:** {booking_id}
💰 **Paid:** {state['final_total_price_usd']:.2f} USDC
(Approx {sym}{state['final_total_price_local']:.2f})

🔗 **Proof of Transaction:**
[View on BaseScan](https://sepolia.basescan.org/tx/{tx})

Safe travels! ✈️"""
    return {"final_status": "Booked", "waiting_for_booking_confirmation": False, "messages": [AIMessage(content=msg)]}

# --- 9. Routing ---
def route_step(state):
    if not state.get("requirements_complete"): return "end"
    if not state.get("hotels"): return "search"
    if not state.get("selected_hotel"): return "end"
    if state.get("final_room_type"):
        if state.get("waiting_for_booking_confirmation"):
            last_msg = get_message_text(state["messages"][-1]).lower()
            if any(w in last_msg for w in ["yes", "proceed", "confirm", "ok", "do it"]): return "book"
            else: return "end"
        else: return "select_room"
    return "select_room"

workflow = StateGraph(AgentState)
workflow.add_node("parse", parse_intent); workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels); workflow.add_node("select_room", select_room)
workflow.add_node("book", book_hotel)
workflow.set_entry_point("parse")
workflow.add_edge("parse", "gather")
workflow.add_conditional_edges("gather", route_step, {"end": END, "search": "search", "select_room": "select_room", "book": "book"})
workflow.add_edge("search", END); workflow.add_edge("select_room", END); workflow.add_edge("book", END)
memory = MemorySaver(); workflow_app = workflow.compile(checkpointer=memory)