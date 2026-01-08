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

# --- 3. Parsing Logic ---

def parse_budget(text: str):
    text = text.lower().replace("$", "").replace(",", "")
    updates = {}
    if "no limit" in text or "unlimited" in text:
        updates["budget_min"] = 0.0
        updates["budget_max"] = 20000.0
        return updates
    
    nums = re.findall(r'\d+', text)
    is_likely_budget = "budget" in text or "price" in text or "cost" in text or any(float(n) > 30 for n in nums)
    
    if len(nums) >= 2 and is_likely_budget:
        updates["budget_min"] = float(nums[0])
        updates["budget_max"] = float(nums[1])
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

# --- 4. Node: Intent Parser ---
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

    if "change" in lowered_text and ("date" in lowered_text or "day" in lowered_text):
        updates["check_in"] = ""
        updates["hotels"] = []
        return updates
    if "change" in lowered_text and "budget" in lowered_text:
        updates["budget_max"] = None
        updates["hotels"] = []
        return updates

    # PRIORITY: GUESTS
    is_guest_answer = "guest" in lowered_text or "room" in lowered_text or "how many" in lowered_ai
    if is_guest_answer:
        nums = [int(s) for s in text.split() if s.isdigit()]
        if nums:
            updates["guests"] = nums[0]
            updates["rooms"] = nums[1] if len(nums) > 1 else 1
            updates["hotels"] = []
            return updates 

    budget_updates = parse_budget(text)
    if budget_updates:
        updates.update(budget_updates)
        updates["hotels"] = [] 
        return updates

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

    if state.get("hotels") and text.isdigit():
        idx = int(text) - 1
        if not state.get("selected_hotel") and 0 <= idx < len(state["hotels"]):
            updates["selected_hotel"] = state["hotels"][idx]
            return updates
        elif not state.get("final_room_type"):
            options = state.get("room_options", [])
            if 0 <= idx < len(options):
                updates["final_room_type"] = options[idx]["type"]
                updates["final_price"] = options[idx]["price"]
                return updates

    new_dest = None
    if not state.get("destination") or " in " in lowered_text:
        for token in [" in ", " to ", " at ", "about "]:
            if token in lowered_text:
                try:
                    candidate = text.split(token, 1)[1].strip("?.").title()
                    forbidden = ["hi", "hello", "start", "budget", "usd", "limit", "no", "yes", "change", "date"] + date_kws
                    if len(candidate) > 2 and not any(k in candidate.lower() for k in forbidden):
                        new_dest = candidate
                        break
                except: pass
        if not new_dest:
            forbidden = ["hi", "hello", "start", "budget", "usd", "limit", "no", "yes", "change", "date"] + date_kws
            if not any(f in lowered_text for f in forbidden) and len(text.split()) < 4 and not any(char.isdigit() for char in text):
                new_dest = text.title()

    if new_dest:
        updates["destination"] = new_dest
        updates["hotels"] = []
        updates["selected_hotel"] = {}
    return updates

# --- 5. Node: Gather Requirements ---
def gather_requirements(state: AgentState):
    if not state.get("destination"):
        return {"messages": [AIMessage(content="👋 Welcome to Warden Travel! Which City or Country are you visiting?")]}
    if not state.get("check_in"):
        return {"messages": [AIMessage(content=f"Great, {state['destination']} is beautiful! 📅 When would you like to Check-in? (YYYY-MM-DD) or say 'Monday'")]}
    if not state.get("guests"):
        intro = f"The date for {state['destination']} is {state['check_in']}, got it.\n\n" if state.get("date_just_set") else ""
        return {"messages": [AIMessage(content=f"{intro}👥 How many guests and how many rooms do you need?\n\nExamples:\n- 2 guests 1 room\n- 3 guests 3 rooms")]}
    if state.get("budget_max") is None:
        return {"messages": [AIMessage(content="💰 What is your budget per night?\n\nExamples:\n- My budget is between $400 and $500\n- My budget is between $400 to $500\n- My budget is under $300\n- My budget is above $300\n- no limit")]}
    return {}

# --- 6. Node: Search Hotels (With SMART FALLBACK) ---
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
    # Retrieve room count, default to 1 if missing
    rooms = state.get("rooms", 1)

    dest_id, dest_type = get_destination_data(city)
    if not dest_id: return {"messages": [AIMessage(content=f"⚠️ Could not find location '{city}'.")]}
    
    url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
    
    # --- FIX: ADDED 'locale' AND 'room_number' ---
    params = {
        "dest_id": str(dest_id), 
        "dest_type": dest_type, 
        "checkin_date": checkin, 
        "checkout_date": state["check_out"], 
        "adults_number": str(guests),
        "room_number": str(rooms),      # <-- Added required field
        "units": "metric", 
        "filter_by_currency": "USD", 
        "order_by": "price",
        "locale": "en-us"               # <-- Added required field
    }
    
    try:
        print(f"DEBUG: Searching hotels in {city} (ID: {dest_id})")
        
        r = requests.get(url, headers={"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}, params=params, timeout=20)
        
        print(f"DEBUG: API Status Code: {r.status_code}")
        if r.status_code != 200:
            print(f"DEBUG: API Error Response: {r.text}")
            return {"messages": [AIMessage(content=f"⚠️ API Error ({r.status_code}): {r.text[:100]}")]}

        raw_data = r.json().get("result", [])[:100]
        
        if not raw_data:
            print(f"DEBUG: API returned 200 OK but 'result' list is empty. Full response: {r.json()}")

        all_parsed_hotels = []
        filtered_hotels = []

        for h in raw_data:
            try: price = float(h.get("composite_price_breakdown", {}).get("gross_amount", {}).get("value", h.get("min_total_price", 150)))
            except: price = 150.0
            
            # Create Hotel Object
            name = h.get("hotel_name", "Hotel")
            score = h.get("review_score", 0) or 0
            rating = "⭐" * int(round(score/2)) if score else "New"
            hotel_obj = {"name": name, "price": price, "rating": rating}
            
            # Add to full list (for fallback)
            all_parsed_hotels.append(hotel_obj)

            # Strict Filter
            if b_min <= price <= b_max:
                filtered_hotels.append(hotel_obj)
                if len(filtered_hotels) >= 5: break 
        
        # --- LOGIC BRANCH ---
        
        # 1. Success Case
        if filtered_hotels:
            final_list = filtered_hotels
            msg_intro = f"🔎 Found options in {city} for {checkin}:"

        # 2. Fallback Case (Found hotels, but all too expensive)
        elif all_parsed_hotels:
            all_parsed_hotels.sort(key=lambda x: x["price"])
            final_list = all_parsed_hotels[:5]
            min_found = final_list[0]['price']
            msg_intro = f"😔 I couldn't find anything strictly under **${b_max:.0f}**.\nThe cheapest option starts at **${min_found:.2f}**.\n\nHere are the lowest price options I found:"
        
        # 3. Total Failure Case
        else:
            return {"messages": [AIMessage(content=f"😔 No hotels found in {city} at all. (API returned 0 results)")]}

        # Format output
        options_list = "\n".join([f"- {i+1}. {h['name']} - ${h['price']:.2f} {h['rating']}" for i, h in enumerate(final_list)])
        msg = f"{msg_intro}\n\n{options_list}\n\nReply with the number to book."
        return {"hotels": final_list, "messages": [AIMessage(content=msg)]}

    except Exception as e: 
        print(f"DEBUG: Exception in search_hotels: {str(e)}")
        return {"messages": [AIMessage(content=f"Search Error: {str(e)}")]}
def select_room(state: AgentState):
    if state.get("selected_hotel") and not state.get("room_options"):
        hotel = state["selected_hotel"]
        base = hotel["price"]
        room_options = [{"type": "Standard", "price": base}, {"type": "Deluxe", "price": round(base * 1.3, 2)}]
        rooms_list = "\n".join([f"- {i+1}. {r['type']} - ${r['price']}" for i, r in enumerate(room_options)])
        msg = f"For {hotel['name']}, select a room:\n{rooms_list}\n\nReply with 1 or 2."
        return {"room_options": room_options, "messages": [AIMessage(content=msg)]}
    return {}

def book_hotel(state: AgentState):
    if not state.get("final_room_type"): return {}
    res = warden_client.submit_booking(state["selected_hotel"]["name"], state["final_price"], state["destination"], 0.0)
    tx = res.get("tx_hash", "0xMOCK_TX")
    msg = f"🎉 Booking Confirmed!\n\nHotel: {state['selected_hotel']['name']}\nPrice: ${state['final_price']}\n[View Transaction](https://sepolia.basescan.org/tx/{tx})"
    return {"final_status": "Booked", "messages": [AIMessage(content=msg)]}

# --- 7. Routing ---
def route_step(state):
    if not state.get("destination"): return "gather"
    if not state.get("check_in"): return "gather"
    if not state.get("guests") or state.get("guests") <= 0: return "gather"
    if state.get("budget_max") is None: return "gather"
    if not state.get("hotels") and not state.get("selected_hotel"): return "search"
    if not state.get("selected_hotel"): return END
    if not state.get("final_room_type"): return "select_room" if not state.get("room_options") else END
    return "book"

# --- 8. Workflow ---
workflow = StateGraph(AgentState)
workflow.add_node("parse", parse_intent); workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels); workflow.add_node("select_room", select_room)
workflow.add_node("book", book_hotel); workflow.set_entry_point("parse")
workflow.add_conditional_edges("parse", route_step, {"gather":"gather","search":"search","select_room":"select_room","book":"book",END:END})
workflow.add_edge("gather", END); workflow.add_edge("search", END); workflow.add_edge("select_room", END); workflow.add_edge("book", END)
memory = MemorySaver(); workflow_app = workflow.compile(checkpointer=memory)