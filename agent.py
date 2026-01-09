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
LLM_MODEL = "grok-3" if os.getenv("GROK_API_KEY") else "gpt-4o-mini"

# Simple FX Rates for Demo (In production, use an Oracle)
FX_RATES = {"GBP": 1.28, "EUR": 1.08, "USD": 1.0, "CAD": 0.74, "AUD": 0.66, "NGN": 0.00065}

# --- 1. State Definition ---
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    destination: str
    check_in: str
    check_out: str
    guests: int
    rooms: int
    budget_max: float
    currency: str          # e.g., "GBP", "USD"
    currency_symbol: str   # e.g., "£", "$"
    hotels: List[dict]
    hotel_cursor: int      # For pagination ("more options")
    selected_hotel: dict
    room_options: List[dict] 
    final_room_type: str
    final_price_per_night: float
    final_total_price_local: float # Total in user's currency
    final_total_price_usd: float   # Total converted to USDC
    final_status: str
    date_just_set: bool 
    requirements_complete: bool
    trip_type: str
    waiting_for_booking_confirmation: bool 

# --- 2. Structured Output Schema ---
class TravelIntent(BaseModel):
    destination: Optional[str] = Field(description="City or country name.")
    check_in: Optional[str] = Field(description="YYYY-MM-DD date. 'weekend' = next Friday.")
    guests: Optional[int] = Field(description="Infer 2 for honeymoon/couple.")
    budget_max: Optional[float] = Field(description="Numeric budget value.")
    currency: Optional[str] = Field(description="Currency code: USD, GBP, EUR, NGN. Default USD.")
    sort_preference: Optional[str] = Field(description="'luxury', 'cheap', 'fancier', 'more'.")
    trip_context: Optional[str] = Field(description="'honeymoon', 'business', 'family'.")

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
    """Safely extracts text from Dicts, Objects, or Multimodal Lists."""
    content = ""
    if hasattr(msg, 'content'): content = msg.content
    elif isinstance(msg, dict): content = msg.get('content', '')
    else: content = str(msg)
    
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str): text_parts.append(part)
            elif isinstance(part, dict) and "text" in part: text_parts.append(str(part["text"]))
        return " ".join(text_parts)
    return str(content)

# --- 4. Node: Intelligent Intent Parser ---
def parse_intent(state: AgentState):
    messages = state.get("messages", [])
    if not messages: return {}
    last_msg = get_message_text(messages[-1]).lower()
    
    # Global Reset
    if "start over" in last_msg or "reset" in last_msg:
        return {
            "destination": None, "check_in": None, "check_out": None,
            "guests": None, "budget_max": None, "hotels": [], "hotel_cursor": 0,
            "selected_hotel": None, "room_options": [], "final_room_type": None,
            "waiting_for_booking_confirmation": False,
            "messages": [AIMessage(content="🔄 System reset. Where are we going next?")]
        }

    # Handling Confirmation Logic
    if state.get("waiting_for_booking_confirmation"):
        if any(w in last_msg for w in ["yes", "proceed", "confirm", "ok", "do it", "book", "pay"]):
             return {} 
        else:
            return {
                "waiting_for_booking_confirmation": False, 
                "room_options": [], 
                "messages": [AIMessage(content="🚫 Booking cancelled. Please select a hotel number again.")]
            }

    # --- HOTEL SELECTION LOGIC ---
    is_selecting_room = state.get("selected_hotel") and state.get("room_options")
    if state.get("hotels") and not is_selecting_room and last_msg.strip().isdigit():
        idx = int(last_msg) - 1
        # Adjust index based on cursor if we implemented full pagination
        # For simplicity, we assume the user sees 1-5 and types 1-5.
        if 0 <= idx < len(state["hotels"]):
            return {
                "selected_hotel": state["hotels"][idx],
                "room_options": [], 
                "waiting_for_booking_confirmation": False
            }

    # PAGINATION / REFINEMENT (The "Fancier" Fix)
    if "more" in last_msg or "fancier" in last_msg or "cheaper" in last_msg:
        # We trigger a re-search or cursor update. 
        # Returning "hotels": [] forces the search node to run again with new context
        return {"hotels": [], "room_options": [], "selected_hotel": None}

    today = date.today().strftime("%Y-%m-%d")
    llm = get_llm()
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"""
    You are an intelligent travel assistant. Today is {today}.
    
    RULES:
    1. Extract destination, dates, guests.
    2. Extract CURRENCY (e.g. "300 pounds" -> GBP, "400 euros" -> EUR). Default USD.
    3. Detect SORT PREFERENCE: If user says "fancier", "luxury", "better", set sort_preference='luxury'.
    """
    
    try:
        intent: TravelIntent = structured_llm.invoke([SystemMessage(content=system_prompt)] + messages)
        
        updates = {}
        if intent.destination: updates["destination"] = intent.destination.title()
        
        # Date Logic
        if intent.check_in: 
            updates["check_in"] = intent.check_in
            if intent.check_out: updates["check_out"] = intent.check_out
            else:
                try:
                    dt = datetime.strptime(intent.check_in, "%Y-%m-%d")
                    updates["check_out"] = (dt + timedelta(days=2)).strftime("%Y-%m-%d")
                except: pass

        if intent.guests: updates["guests"] = intent.guests
        if intent.budget_max: updates["budget_max"] = intent.budget_max
        if intent.trip_context: updates["trip_type"] = intent.trip_context
        
        # Currency Logic
        if intent.currency:
            curr = intent.currency.upper()
            updates["currency"] = curr
            symbols = {"USD": "$", "GBP": "£", "EUR": "€", "NGN": "₦"}
            updates["currency_symbol"] = symbols.get(curr, curr + " ")

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
        ("system", "Ask for missing details: {missing_fields}. Be concise."),
        MessagesPlaceholder(variable_name="messages"),
    ])
    chain = prompt | llm
    response = chain.invoke({"missing_fields": ", ".join(missing), "messages": state.get("messages", [])})
    return {"requirements_complete": False, "messages": [response]}

# --- 6. Node: Search Hotels (Smart Sort & Currency) ---
def _fetch_hotels_from_api(city, check_in, check_out, guests, rooms, currency):
    try:
        headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
        r = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/locations", 
                        headers=headers, params={"name": city, "locale": "en-us"}, timeout=10)
        data = r.json()
        if not data: return []
        
        dest_id, dest_type = data[0].get("dest_id"), data[0].get("dest_type")
        
        # We request data in the USER'S currency
        params = {
            "dest_id": dest_id, "dest_type": dest_type,
            "checkin_date": check_in, "checkout_date": check_out,
            "adults_number": str(guests), "room_number": str(rooms),
            "units": "metric", 
            "filter_by_currency": currency, # <--- DYNAMIC CURRENCY
            "order_by": "price", "locale": "en-us"
        }
        
        # Fetch MORE results (30) so we can sort them ourselves
        raw_data = []
        for attempt in range(3):
            try:
                res = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/search", 
                                headers=headers, params=params, timeout=15)
                raw_data = res.json().get("result", [])[:30] # Get top 30
                break
            except:
                if attempt < 2: time.sleep(2)
        return raw_data
    except: return []

def search_hotels(state: AgentState):
    if not state.get("requirements_complete"): return {}
    if state.get("selected_hotel"): return {} 
    if state.get("hotels"): return {} # Already have list? Keep it unless cleared.
    
    city = state.get("destination")
    rooms = state.get("rooms", 1)
    currency = state.get("currency", "USD")
    symbol = state.get("currency_symbol", "$")
    
    try:
        d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
        d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except: nights = 1

    # Fetch
    raw_data = _fetch_hotels_from_api(city, state["check_in"], state["check_out"], state["guests"], rooms, currency)
    used_city = city
    
    # Pivot logic
    if not raw_data:
        llm = get_llm()
        pivot_prompt = f"User wants hotels in '{city}' but search failed. Name the SINGLE best city/island hub inside '{city}'. Return ONLY name."
        new_city = llm.invoke(pivot_prompt).content.strip().replace(".", "")
        raw_data = _fetch_hotels_from_api(new_city, state["check_in"], state["check_out"], state["guests"], rooms, currency)
        if raw_data: used_city = new_city

    # Parse and Clean
    all_hotels = []
    for h in raw_data:
        try: 
            total_price = float(h.get("min_total_price", 0))
            if total_price == 0: continue
            price_per_night = round(total_price / nights, 2)
            
            # Star Rating
            stars = h.get("class", 0)
            star_val = int(stars) if stars else 0
            star_str = "⭐" * star_val if star_val > 0 else f"Rating: {h.get('review_score', 'New')}"

            all_hotels.append({
                "name": h.get("hotel_name"), 
                "price": price_per_night, # In Local Currency
                "total": total_price,
                "rating_str": star_str,
                "stars": star_val,
                "score": float(h.get("review_score") or 0)
            })
        except: pass
    
    # --- SMART SORTING (The "Quality" Fix) ---
    budget = state.get("budget_max", 10000)
    
    # 1. Filter by budget first
    valid_hotels = [h for h in all_hotels if h["price"] <= budget]
    
    # 2. If no hotels under budget, use fallback
    if not valid_hotels:
        valid_hotels = sorted(all_hotels, key=lambda x: x["price"])[:3]
        msg_intro = f"⚠️ I couldn't find anything strictly under {symbol}{budget}. Cheapest options:"
    else:
        # 3. If Budget is HIGH (> 200), sort by QUALITY (Stars > Score > Price)
        # This fixes the "cheap hostel" issue for rich clients
        if budget > 200:
            valid_hotels.sort(key=lambda x: (x["stars"], x["score"]), reverse=True)
            msg_intro = f"✨ Top options in **{used_city}** (Quality First):"
        else:
            # Low budget? Sort by Price (Ascending)
            valid_hotels.sort(key=lambda x: x["price"])
            msg_intro = f"🎉 Best value options in **{used_city}**:"

    final_list = valid_hotels[:5] # Show top 5

    options = "\n".join([f"{i+1}. **{h['name']}** - {symbol}{h['price']}/night ({h['rating_str']})" for i, h in enumerate(final_list)])
    msg = f"{msg_intro}\n\n{options}\n\nReply with the number to book (e.g., '1')."
    
    return {"hotels": final_list, "messages": [AIMessage(content=msg)]}

# --- 7. Node: Select Room & Calculate Conversion ---
def select_room(state: AgentState):
    # Case A: Present Options
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
    
    # Case B: Parse & Convert
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
        
        # 1. Local Total
        local_total = selected_room["price"] * nights
        currency = state.get("currency", "USD")
        sym = state.get("currency_symbol", "$")
        
        # 2. USDC Conversion (The "Pay on Base" Fix)
        # Convert Local -> USD
        rate = FX_RATES.get(currency, 1.0) # Get rate GBP->USD
        usd_total = local_total * rate
        
        msg = f"""Summary of your trip:
        
🏨 **Hotel:** {state['selected_hotel']['name']}
🛏️ **Room:** {selected_room['type']}
📅 **Duration:** {nights} Nights
💵 **Local Total:** {sym}{local_total:.2f}

🔄 **Payment Conversion:**
We accept payments in **USDC on Base**.
Exchange Rate: 1 {currency} ≈ {rate} USDC
**TOTAL TO PAY:** {usd_total:.2f} USDC

Reply 'Yes' or 'Confirm' to initiate the transaction."""

        return {
            "final_room_type": selected_room["type"],
            "final_total_price_local": local_total,
            "final_total_price_usd": usd_total,
            "waiting_for_booking_confirmation": True, 
            "messages": [AIMessage(content=msg)]
        }

    return {"messages": [AIMessage(content="⚠️ Please pick a room number ('1' or '2').")]}

# --- 8. Node: Book Hotel ---
def book_hotel(state: AgentState):
    if not state.get("waiting_for_booking_confirmation"): return {}
    
    # Use the USD Price for the blockchain intent
    details = f"{state['selected_hotel']['name']} ({state['final_room_type']}) [Chain: Base | Token: USDC]"
    res = warden_client.submit_booking(details, state["final_total_price_usd"], state["destination"], 0.0)
    tx = res.get("tx_hash", "0xMOCK")
    
    sym = state.get("currency_symbol", "$")
    msg = f"""✅ Success! Your trip is booked.

🏨 **Hotel:** {state['selected_hotel']['name']}
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
workflow.add_conditional_edges("gather", route_step, {
    "end": END, "search": "search", "select_room": "select_room", "book": "book"
})
workflow.add_edge("search", END); workflow.add_edge("select_room", END)
workflow.add_edge("book", END)

memory = MemorySaver(); workflow_app = workflow.compile(checkpointer=memory)