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

    range_match = re.search(r'(\d+)\s*-\s*(\d+)', text)
    if not range_match: range_match = re.search(r'(\d+)\s+to\s+(\d+)', text)
    
    if range_match:
        updates["budget_min"] = float(range_match.group(1))
        updates["budget_max"] = float(range_match.group(2))
        return updates

    under_match = re.search(r'(?:under|below|less than)\s*(\d+)', text)
    if under_match:
        updates["budget_min"] = 0.0
        updates["budget_max"] = float(under_match.group(1))
        return updates

    over_match = re.search(r'(?:above|over|more than)\s*(\d+)', text)
    if over_match:
        updates["budget_min"] = float(over_match.group(1))
        updates["budget_max"] = 20000.0
        return updates
        
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
        updates["budget_max"] = 0.0
        updates["hotels"] = []
        return updates

    if "which" in lowered_text and ("date" in lowered_text or "day" in lowered_text):
        updates["check_in"] = ""
        updates["hotels"] = []
        return updates

    # --- 2. BUDGET ---
    budget_updates = parse_budget(text)
    if budget_updates:
        updates.update(budget_updates)
        updates["hotels"] = [] 
        return updates

    # --- 3. DATES ---
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

    # --- 6. DESTINATION ---
    new_dest = None
    found_marker = False
    
    for token in [" in ", " to ", " at ", "about "]:
        if token in lowered_text:
            try:
                candidate = text.split(token, 1)[1].strip("?.").title()
                forbidden = ["hi", "hello", "start", "budget", "usd", "limit", "no", "yes", "change", "date"] + date_kws
                if len(candidate) > 2 and not any(k in candidate.lower() for k in forbidden):
                    new_dest = candidate
                    found_marker = True
                    break
            except: pass
    
    if not found_marker:
        forbidden = ["hi", "hello", "start", "budget", "usd", "limit", "no", "yes", "change", "date"] + date_kws
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

# --- 4. Node: Gather Requirements ---
def gather_requirements(state: AgentState):
    # 1. Destination
    if not state.get("destination"):
        return {"messages": [AIMessage(content="👋 Welcome to Warden Travel! Which City or Country are you visiting?")]}
    
    # 2. Date
    if not state.get("check_in"):
        return {"messages": [AIMessage(content=f"Great, {state['destination']} is beautiful! 📅 When would you like to Check-in? (YYYY-MM-DD) or say 'Monday'")]}
    
    # 3. Guests
    if not state.get("guests"):
        intro = f"The date for {state['destination']} is {state['check_in']}, got it.\n\n" if state.get("date_just_set") else ""
        # Use line breaks instead of bolding for guest examples
        return {"messages": [AIMessage(content=f"{intro}👥 How many guests and how many rooms do you need?\nExamples:\n- 2 guests 1 room\n- 3 guests 3 rooms")]}

    # 4. Budget - CLEAN PLAIN TEXT VERSION
    if not state.get("budget_max"):
        return {"messages": [AIMessage(content="💰 What is your budget per night?\n\nExamples:\n- $100 to $200\n- under $300\n- above $300\n- no limit")]}

    return {}

# --- 5. Node: Search Hotels ---
def get_destination_data(city):
    if not BOOKING_KEY: return None, None
    try:
        url = "https://booking-com.p.rapidapi.com/v1/hotels/locations"
        r = requests.get(url, headers={"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}, params={"name": city, "locale": "en-us"}, timeout=10)
        data = r.json()
        if data: return data[0].get("dest_id"), data[0].get("dest_type")
    except: pass
    return None, None

def search_hotels(state: AgentState):
    if state.get("hotels"): return {}

    city, checkin, guests = state.get("destination"), state.get("check_in"), state.get("guests", 1)
    b_min, b_max = state.get("budget_min", 0), state.get("budget_max", 20000) 
    
    dest_id, dest_type = get_destination_data(city)
    if not dest_id: return {"messages": [AIMessage(content=f"⚠️ Could not find location '{city}'.")]}

    url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
    params = {"dest_id": str(dest_id), "dest_type": dest_type, "checkin_date": checkin, "checkout_date": state["check_out"], "adults_number": str(guests), "units": "metric", "filter_by_currency": "USD", "order_by": "price"}
    
    try:
        r = requests.get(url, headers={"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}, params=params, timeout=20)
        raw_data = r.json().get("result", [])[:60]
        hotels = []
        for h in raw_data:
            try: price = float(h.get("composite_price_breakdown", {}).get("gross_amount", {}).get("value", h.get("min_total_price", 150)))
            except: price = 150.0
            if b_min <= price <= b_max:
                hotels.append({"name": h.get("hotel_name", "Hotel"), "price": price, "rating": "⭐" * int(round((h.get("review_score", 0) or 0)/2)) or "New"})
                if len(hotels) >= 5: break 
        
        if not hotels:
            return {"messages": [AIMessage(content=f"😔 Found hotels in {city}, but none matching your budget. Try saying 'Change budget'.")]}
        
        msg = f"🔎 Found options in {city} for {checkin}:\n\n" + "\n".join([f"{i+1}. {h['name']} - ${h['price']:.2f} {h['rating']}" for i, h in enumerate(hotels)])
        return {"hotels": hotels, "messages": [AIMessage(content=msg + "\n\nReply with the number to book.")]}
    except Exception as e: return {"messages": [AIMessage(content=f"Search Error: {str(e)}")]}

# --- 6. Node: Select Room ---
def select_room(state: AgentState):
    if state.get("selected_hotel") and not state.get("room_options"):
        hotel = state["selected_hotel"]
        base = hotel["price"]
        room_options = [{"type": "Standard", "price": base}, {"type": "Deluxe", "price": round(base * 1.3, 2)}]
        msg = f"For {hotel['name']}, select a room:\n1. Standard - ${base}\n2. Deluxe - ${room_options[1]['price']}\nReply with 1 or 2."
        return {"room_options": room_options, "messages": [AIMessage(content=msg)]}
    return {}

# --- 7. Node: Book Hotel ---
def book_hotel(state: AgentState):
    if not state.get("final_room_type"): return {}
    res = warden_client.submit_booking(state["selected_hotel"]["name"], state["final_price"], state["destination"], 0.0)
    tx = res.get("tx_hash", "0xMOCK_TX")
    msg = f"🎉 Booking Confirmed!\n\nHotel: {state['selected_hotel']['name']}\nPrice: ${state['final_price']}\n[View Transaction](https://sepolia.basescan.org/tx/{tx})"
    return {"final_status": "Booked", "messages": [AIMessage(content=msg)]}

# --- Routing ---
def route_step(state):
    if not state.get("destination") or not state.get("check_in") or not state.get("guests") or state.get("budget_max") is None: return "gather"
    if not state.get("hotels"): return "search"
    if not state.get("selected_hotel"): return END
    if not state.get("final_room_type"): return "select_room" if not state.get("room_options") else END
    return "book"

workflow = StateGraph(AgentState)
workflow.add_node("parse", parse_intent); workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels); workflow.add_node("select_room", select_room)
workflow.add_node("book", book_hotel); workflow.set_entry_point("parse")
workflow.add_conditional_edges("parse", route_step, {"gather":"gather","search":"search","select_room":"select_room","book":"book",END:END})
workflow.add_edge("gather", END); workflow.add_edge("search", END); workflow.add_edge("select_room", END); workflow.add_edge("book", END)
memory = MemorySaver(); workflow_app = workflow.compile(checkpointer=memory)