# agent.py
import os
import requests
import random
import operator
import re
from dotenv import load_dotenv
from datetime import date, timedelta, datetime
from typing import TypedDict, List, Union, Optional, Annotated

from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

import warden_client

load_dotenv()

BOOKING_KEY = os.getenv("BOOKING_API_KEY")

# --- 1. Enhanced State Management ---
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    user_query: str
    destination: str
    check_in: str
    check_out: str
    guests: int
    rooms: int
    budget_min: float
    budget_max: float
    hotels: List[dict]
    selected_hotel: dict
    room_options: List[dict]
    final_room_type: str
    final_price: float
    needs_swap: bool
    tx_hash: str
    confirmation_number: str
    final_status: str
    date_just_set: bool 

# --- 2. Helpers ---

def parse_date(text: str) -> str:
    text = text.lower().strip()
    today = date.today()
    try:
        words = text.replace(",", " ").split()
        for w in words:
            try:
                return datetime.strptime(w, "%Y-%m-%d").strftime("%Y-%m-%d")
            except: continue
    except: pass

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, day in enumerate(weekdays):
        if day in text:
            current_weekday = today.weekday()
            days_ahead = i - current_weekday
            if days_ahead <= 0: days_ahead += 7
            return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    if "tomorrow" in text: return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "next week" in text: return (today + timedelta(days=7)).strftime("%Y-%m-%d")
    return (today + timedelta(days=1)).strftime("%Y-%m-%d")

def extract_text(content) -> str:
    if isinstance(content, str): return content
    if isinstance(content, list): return " ".join([extract_text(item) for item in content])
    if isinstance(content, dict): return content.get("text", str(content))
    return str(content)

def parse_budget(text: str):
    text = text.lower().replace("$", "").replace(",", "")
    updates = {}
    
    if "no limit" in text or "unlimited" in text:
        updates["budget_min"] = 0.0
        updates["budget_max"] = 20000.0
        return updates

    # Pattern: "100-200" or "100 to 200"
    range_match = re.search(r'(\d+)\s*-\s*(\d+)', text)
    if not range_match: range_match = re.search(r'(\d+)\s+to\s+(\d+)', text)
    
    if range_match:
        updates["budget_min"] = float(range_match.group(1))
        updates["budget_max"] = float(range_match.group(2))
        return updates

    # Pattern: "under 300"
    under_match = re.search(r'(?:under|below|less than)\s*(\d+)', text)
    if under_match:
        updates["budget_min"] = 0.0
        updates["budget_max"] = float(under_match.group(1))
        return updates

    # Pattern: "above 300"
    over_match = re.search(r'(?:above|over|more than)\s*(\d+)', text)
    if over_match:
        updates["budget_min"] = float(over_match.group(1))
        updates["budget_max"] = 20000.0
        return updates
        
    # Just a number
    if text.strip().isdigit():
        val = float(text)
        if val > 30: 
            updates["budget_min"] = 0.0
            updates["budget_max"] = val
            return updates

    return {}

# --- 3. Node: Intent Parser (Strict Hierarchy) ---
def parse_intent(state: AgentState):
    messages = state.get("messages", [])
    if not messages: return {}
    
    last_user_text = ""
    last_ai_text = ""
    
    for m in reversed(messages):
        m_type = getattr(m, 'type', '') or (m.get('type') if isinstance(m, dict) else '')
        if m_type == 'human' or isinstance(m, HumanMessage):
            last_user_text = extract_text(getattr(m, 'content', '') or m.get('content', '')).strip()
            break
            
    for m in reversed(messages):
        m_type = getattr(m, 'type', '') or (m.get('type') if isinstance(m, dict) else '')
        if m_type == 'ai' or isinstance(m, AIMessage):
            last_ai_text = extract_text(getattr(m, 'content', '') or m.get('content', '')).strip()
            break

    updates = {}
    text = last_user_text
    lowered_text = text.lower()
    lowered_ai = last_ai_text.lower()
    
    updates["date_just_set"] = False 

    if not text: return {}

    # --- 1. COMMANDS (Reset/Change) ---
    if "change" in lowered_text and ("date" in lowered_text or "day" in lowered_text):
        updates["check_in"] = ""
        updates["hotels"] = []
        return updates
        
    if "change" in lowered_text and "budget" in lowered_text:
        updates["budget_max"] = 0.0 # Force reset
        updates["hotels"] = []
        return updates

    if "which" in lowered_text and ("date" in lowered_text or "day" in lowered_text):
        updates["check_in"] = ""
        updates["hotels"] = []
        return updates

    # --- 2. BUDGET (High Priority) ---
    budget_updates = parse_budget(text)
    if budget_updates:
        updates.update(budget_updates)
        updates["hotels"] = [] 
        return updates

    # --- 3. DATES (Strict) ---
    date_kws = ["jan", "feb", "mar", "apr", "tomorrow", "next", "monday", "tuesday", "wednesday", "thursday", "friday", "year", "week"]
    is_date_input = any(k in lowered_text for k in date_kws) or (any(char.isdigit() for char in text) and ("-" in text or "/" in text))
    
    if "check-in" in lowered_ai or is_date_input:
        parsed = parse_date(text)
        if parsed:
            updates["check_in"] = parsed
            updates["check_out"] = (datetime.strptime(parsed, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
            updates["hotels"] = []
            updates["date_just_set"] = True
            return updates

    # --- 4. GUESTS ---
    if "guests" in lowered_ai or "guest" in lowered_text:
        nums = [int(s) for s in text.split() if s.isdigit()]
        if nums:
            updates["guests"] = nums[0]
            updates["rooms"] = nums[1] if len(nums) > 1 else 1
            updates["hotels"] = []
            return updates

    # --- 5. SELECTION ---
    if state.get("hotels") and text.isdigit():
        idx = int(text) - 1
        if not state.get("selected_hotel"):
            if 0 <= idx < len(state["hotels"]):
                updates["selected_hotel"] = state["hotels"][idx]
                return updates
        elif not state.get("final_room_type"):
            options = state.get("room_options", [])
            if 0 <= idx < len(options):
                updates["final_room_type"] = options[idx]["type"]
                updates["final_price"] = options[idx]["price"]
                return updates

    # --- 6. DESTINATION (Last Resort) ---
    new_dest = None
    found_marker = False
    
    for token in [" in ", " to ", " at ", "about "]:
        if token in lowered_text:
            try:
                candidate = text.split(token, 1)[1].strip("?.").title()
                if len(candidate) > 2 and not any(char.isdigit() for char in candidate):
                    new_dest = candidate
                    found_marker = True
                    break
            except: pass
    
    if not found_marker:
        forbidden = ["hi", "hello", "start", "budget", "usd", "limit", "no", "yes", "change", "date"]
        has_forbidden = any(f in lowered_text for f in forbidden)
        
        if not has_forbidden and len(text.split()) < 4 and not any(char.isdigit() for char in text):
            new_dest = text.title()

    if new_dest:
        updates["destination"] = new_dest
        updates["hotels"] = []
        updates["selected_hotel"] = {}
        updates["final_room_type"] = ""

    updates["user_query"] = text
    return updates

# --- 4. Node: Gather Requirements (Fixed Text) ---
def gather_requirements(state: AgentState):
    if not state.get("destination"):
        return {"messages": [AIMessage(content="👋 Welcome to Warden Travel! Which **City** or **Country** are you visiting?")]}
    
    if not state.get("check_in"):
        return {"messages": [AIMessage(content=f"Great, **{state['destination']}** is beautiful! 📅 When would you like to **Check-in**? (YYYY-MM-DD) or just say 'Monday'")]}
    
    if not state.get("guests"):
        intro = ""
        if state.get("date_just_set"):
            intro = f"The date for {state['user_query']} is **{state['check_in']}**, got it.\n\n"
        return {"messages": [AIMessage(content=f"{intro}👥 **How many guests** and how many **rooms** do you need? (e.g. 2 guests 1 room)")]}

    # Clean text: No asterisks in the examples to avoid broken formatting
    if not state.get("budget_max"):
        return {"messages": [AIMessage(content="💰 What is your **budget per night**? (e.g. $100-$200, under $300, above $300, or no limit).")]}

    return {}

# --- 5. Node: Search Hotels (Deep Search Fix) ---
def get_destination_data(city):
    if not BOOKING_KEY: return None, None
    try:
        url = "https://booking-com.p.rapidapi.com/v1/hotels/locations"
        headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
        r = requests.get(url, headers=headers, params={"name": city, "locale": "en-us"}, timeout=10)
        data = r.json()
        if data: return data[0].get("dest_id"), data[0].get("dest_type")
    except: pass
    return None, None

def search_hotels(state: AgentState):
    if state.get("hotels"): return {}

    city = state.get("destination")
    checkin = state.get("check_in")
    guests = state.get("guests", 1)
    b_min = state.get("budget_min", 0)
    b_max = state.get("budget_max", 20000) 
    
    msg_start = f"🔎 Searching hotels in **{city}** ({checkin}) "
    if b_max >= 20000: msg_start += "with **no limit**..."
    else: msg_start += f"within **${b_min:.0f}-${b_max:.0f}**..."
    
    dest_id, dest_type = get_destination_data(city)
    if not dest_id: 
        return {"messages": [AIMessage(content=f"⚠️ Could not find location '{city}'.")]}

    url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
    headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
    params = {
        "dest_id": str(dest_id), "dest_type": dest_type,
        "checkin_date": checkin, "checkout_date": state["check_out"],
        "adults_number": str(guests), "room_number": str(state.get("rooms", 1)),
        "units": "metric", "filter_by_currency": "USD", "order_by": "price"
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        # Fetch 50 results (instead of 20) to increase chances of finding budget options
        raw_data = response.json().get("result", [])[:50] 
        hotels = []
        
        for h in raw_data:
            try: price = float(h.get("composite_price_breakdown", {}).get("gross_amount", {}).get("value", 0))
            except: price = 0.0
            if price == 0: price = float(h.get("min_total_price", 150))
            
            # BUDGET FILTER
            if b_min <= price <= b_max:
                name = h.get("hotel_name", "Unknown")
                score = h.get("review_score", 0) or 0
                rating = "⭐" * int(round(score/2)) if score else "New"
                hotels.append({"name": name, "price": price, "rating": rating})
                if len(hotels) >= 5: break 

        if not hotels:
            # Better error message for London (empty due to price)
            if raw_data:
                return {"messages": [AIMessage(content=f"😔 I found hotels in {city}, but **none were under ${b_max:.0f}**.\n\nTry saying **'Change budget'** to increase it.")]}
            else:
                return {"messages": [AIMessage(content=f"😔 No hotels found in {city} for these dates.\nTry saying **'Change date'**.")]}

        options_text = ""
        for i, h in enumerate(hotels):
            options_text += f"{i+1}. **{h['name']}** — ${h['price']:.2f} {h['rating']}\n"
            
        final_msg = f"{msg_start}\n\nI found {len(hotels)} options:\n\n{options_text}\nReply with the **number** to book."
        return {"hotels": hotels, "messages": [AIMessage(content=final_msg)]}

    except Exception as e:
        return {"messages": [AIMessage(content=f"Search Error: {str(e)}")]}

# --- 6. Node: Select Room ---
def select_room(state: AgentState):
    if state.get("selected_hotel") and not state.get("room_options"):
        hotel = state["selected_hotel"]
        base = hotel["price"]
        
        room_options = [
            {"type": "Standard", "price": base},
            {"type": "Deluxe", "price": round(base * 1.3, 2)},
            {"type": "Executive", "price": round(base * 2.5, 2)}
        ]
        
        msg = f"For **{hotel['name']}**, select a room:\n\n"
        for i, r in enumerate(room_options):
            msg += f"{i+1}. **{r['type']}** — ${r['price']:.2f}\n"
        msg += "\nReply with **1, 2, or 3**."
        return {"room_options": room_options, "messages": [AIMessage(content=msg)]}
    return {}

# --- 7. Node: Book Hotel ---
def book_hotel(state: AgentState):
    if not state.get("final_room_type"): return {}
    hotel = state["selected_hotel"]["name"]
    price = state["final_price"]
    hcn = f"#{random.randint(10000, 99999)}BR"
    
    result = warden_client.submit_booking(hotel, price, state["destination"], 0.0)
    tx = result.get("tx_hash", "0xMOCK_TX")
    tx_url = f"https://sepolia.basescan.org/tx/{tx}"
    
    msg = f"""🎉 **Booking Confirmed!**

🏨 **{hotel}** ({state['final_room_type']})
📍 **{state['destination']}**
📅 **{state['check_in']}**
🎫 Ref: `{hcn}`

💰 Paid: ${price:.2f}
🔗 [View Transaction]({tx_url})
"""
    return {"final_status": "Booked", "confirmation_number": hcn, "messages": [AIMessage(content=msg)]}

# --- Routing ---
def route_step(state):
    # Only force "gather" if essential info is missing. 
    # budget_max check must be explicitly strict (not 0.0)
    if not state.get("destination") or not state.get("check_in") or not state.get("guests"):
        return "gather"
    
    if state.get("budget_max") is None: # 0.0 is valid, None is not
        return "gather"
    if state.get("budget_max") == 0.0 and not state.get("hotels"): # Catch reset state
        return "gather"

    if not state.get("hotels") and not state.get("selected_hotel"):
        return "search"
        
    if state.get("hotels") == [] and not state.get("selected_hotel"):
        return END

    if not state.get("selected_hotel"): return "wait_for_selection"
    
    if not state.get("final_room_type"):
        if not state.get("room_options"): return "select_room"
        else: return "wait_for_room"
        
    if state.get("final_status") != "Booked": return "book"
    return END

# --- Graph ---
workflow = StateGraph(AgentState)
workflow.add_node("parse", parse_intent); workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels); workflow.add_node("select_room", select_room)
workflow.add_node("book", book_hotel); workflow.set_entry_point("parse")
workflow.add_conditional_edges("parse", route_step, {"gather":"gather","search":"search","select_room":"select_room","book":"book",END:END})
workflow.add_edge("gather", END); workflow.add_edge("search", END); workflow.add_edge("select_room", END); workflow.add_edge("book", END)
memory = MemorySaver(); workflow_app = workflow.compile(checkpointer=memory)