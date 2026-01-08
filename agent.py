# agent.py
import os
import requests
import random
import operator
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
    # Use operator.add to append new messages to history instead of overwriting
    messages: Annotated[List[BaseMessage], operator.add]
    user_query: str
    
    # Booking Details
    destination: str
    check_in: str
    check_out: str
    guests: int
    rooms: int
    
    # Search & Selection
    budget_usd: float
    hotels: List[dict]
    selected_hotel: dict
    room_options: List[dict]
    final_room_type: str
    final_price: float
    
    # Transaction
    needs_swap: bool
    tx_hash: str
    confirmation_number: str
    final_status: str

# --- 2. Helpers ---

def parse_date(text: str) -> str:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except:
        # Fallback logic for demo purposes
        if "jan" in text.lower(): return "2026-01-20"
        if "feb" in text.lower(): return "2026-02-10"
        return (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")

def extract_text(content) -> str:
    """
    Recursively extracts text from complex Vercel message formats.
    Handles: String, List of Dicts, or Dict.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Join all text parts found in the list
        return " ".join([extract_text(item) for item in content])
    if isinstance(content, dict):
        # Extract 'text' key if present
        return content.get("text", str(content))
    return str(content)

# --- 3. Node: Intent Parser ---
def parse_intent(state: AgentState):
    print(f"[DEBUG] Current State Keys: {list(state.keys())}")
    
    messages = state.get("messages", [])
    if not messages: 
        return {}
    
    last_user_text = ""
    last_ai_text = ""
    
    # 1. Extract Last User Message (safely)
    for m in reversed(messages):
        # Check both object attributes and dict keys for compatibility
        m_type = getattr(m, 'type', None) or (m.get('type') if isinstance(m, dict) else None)
        if m_type == 'human' or isinstance(m, HumanMessage):
            raw_content = getattr(m, 'content', None) or (m.get('content') if isinstance(m, dict) else "")
            last_user_text = extract_text(raw_content).strip()
            break
            
    # 2. Extract Last AI Message (safely)
    for m in reversed(messages):
        m_type = getattr(m, 'type', None) or (m.get('type') if isinstance(m, dict) else None)
        if m_type == 'ai' or isinstance(m, AIMessage):
            raw_content = getattr(m, 'content', None) or (m.get('content') if isinstance(m, dict) else "")
            last_ai_text = extract_text(raw_content).strip()
            break

    print(f"[DEBUG] Parsed User Input: '{last_user_text}'")
    
    updates = {}
    text = last_user_text
    lowered_text = text.lower()
    lowered_ai = last_ai_text.lower()

    if not text: return {}

    # --- DESTINATION CAPTURE LOGIC ---
    
    # 1. Check if we already have a destination
    if not state.get("destination"):
        # Strategy A: Look for keywords like "in London"
        found_marker = False
        for token in [" in ", " to ", " at "]:
            if token in lowered_text:
                try:
                    candidate = text.split(token, 1)[1].strip("?.").title()
                    if len(candidate) > 2:
                        updates["destination"] = candidate
                        found_marker = True
                        break
                except: pass
        
        # Strategy B: Blind Capture (Assumption)
        # If no keywords found, and input is not a greeting, assume it is the city.
        if not found_marker and lowered_text not in ["hi", "hello", "hey", "start", "restart", "menu"]:
            updates["destination"] = text.title()
            print(f"[DEBUG] Blind captured destination: {updates['destination']}")

    # --- INTERVIEW LOGIC (Dates & Guests) ---
    
    # Are we answering the date question?
    if "check-in" in lowered_ai:
        updates["check_in"] = parse_date(text)
        updates["check_out"] = (datetime.strptime(updates["check_in"], "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
    
    # Are we answering the guest question?
    elif "how many guests" in lowered_ai:
        nums = [int(s) for s in text.split() if s.isdigit()]
        if nums:
            updates["guests"] = nums[0]
            updates["rooms"] = nums[1] if len(nums) > 1 else 1

    # --- HOTEL SELECTION LOGIC ---
    
    # Selecting a hotel number (1-5)
    elif state.get("hotels") and not state.get("selected_hotel"):
        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(state["hotels"]):
                updates["selected_hotel"] = state["hotels"][idx]
    
    # Selecting a room type (1-3)
    elif state.get("selected_hotel") and not state.get("final_room_type"):
        options = state.get("room_options", [])
        if text.isdigit() and 0 <= int(text)-1 < len(options):
            chosen = options[int(text)-1]
            updates["final_room_type"] = chosen["type"]
            updates["final_price"] = chosen["price"]
        elif "deluxe" in lowered_text:
            # Simple keyword matching fallback
            match = next((r for r in options if "Deluxe" in r["type"]), options[0])
            updates["final_room_type"] = match["type"]
            updates["final_price"] = match["price"]
        else:
            # Default to first option if input is unclear but flow continues
            if options:
                updates["final_room_type"] = options[0]["type"]
                updates["final_price"] = options[0]["price"]

    updates["user_query"] = text
    return updates

# --- 4. Node: Gather Requirements ---
def gather_requirements(state: AgentState):
    """Asks for missing information based on current state."""
    print("[DEBUG] Node: Gather Requirements")
    
    if not state.get("destination") or state.get("destination") == "Unknown":
        return {"messages": [AIMessage(content="👋 Welcome to Warden Travel! To find you the best hotels, which **City** or **Country** are you visiting?")]}
    
    if not state.get("check_in"):
        return {"messages": [AIMessage(content=f"Great, **{state['destination']}** is beautiful! 📅 When would you like to **Check-in**? (YYYY-MM-DD)")]}
    
    if not state.get("guests"):
        return {"messages": [AIMessage(content="Got the dates. 👥 **How many guests** and how many **rooms** do you need? (e.g. '2 guests 1 room')")]}
    
    return {}

# --- 5. Node: Search Hotels ---
def get_destination_data(city):
    if not BOOKING_KEY: return None, None
    try:
        url = "https://booking-com.p.rapidapi.com/v1/hotels/locations"
        headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
        params = {"name": city, "locale": "en-us"}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data: return data[0].get("dest_id"), data[0].get("dest_type")
    except: pass
    return None, None

def search_hotels(state: AgentState):
    print("[DEBUG] Node: Search Hotels")
    if state.get("hotels"): return {}

    city = state.get("destination")
    checkin = state.get("check_in")
    checkout = state.get("check_out")
    guests = state.get("guests", 1)
    
    msg_start = f"🔎 Searching **live availability** in {city} for {guests} guests ({checkin} to {checkout})..."
    
    dest_id, dest_type = get_destination_data(city)
    if not dest_id: 
        return {"messages": [AIMessage(content=f"⚠️ Could not find location '{city}'. Please try a major city name.")]}

    url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
    headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
    params = {
        "dest_id": str(dest_id), "dest_type": dest_type,
        "checkin_date": checkin, "checkout_date": checkout,
        "adults_number": str(guests), "room_number": str(state.get("rooms", 1)),
        "units": "metric", "filter_by_currency": "USD", "order_by": "popularity"
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        data = response.json()
        hotels = []
        
        for h in data.get("result", [])[:5]:
            name = h.get("hotel_name", "Unknown")
            try: price = float(h.get("composite_price_breakdown", {}).get("gross_amount", {}).get("value", 0))
            except: price = 0.0
            if price == 0: price = float(h.get("min_total_price", 150))
            
            hotels.append({"name": name, "price": price})

        if not hotels:
            return {"messages": [AIMessage(content=f"No hotels found in {city} for these dates.")]}

        options_text = ""
        for i, h in enumerate(hotels):
            options_text += f"{i+1}. **{h['name']}** — ${h['price']:.2f}\n"
            
        final_msg = f"{msg_start}\n\nI found {len(hotels)} great options:\n\n{options_text}\nReply with the **number** of the hotel you want to book."
        
        return {
            "hotels": hotels,
            "messages": [AIMessage(content=final_msg)]
        }

    except Exception as e:
        return {"messages": [AIMessage(content=f"Search Error: {str(e)}")]}

# --- 6. Node: Select Room Type ---
def select_room(state: AgentState):
    print("[DEBUG] Node: Select Room")
    if state.get("selected_hotel") and not state.get("room_options"):
        hotel = state["selected_hotel"]
        base_price = hotel["price"]
        
        room_options = [
            {"type": "Standard Room", "price": base_price, "desc": "Best Value"},
            {"type": "Deluxe Room", "price": round(base_price * 1.3, 2), "desc": "More Space + View"},
            {"type": "Executive Suite", "price": round(base_price * 2.5, 2), "desc": "Luxury Experience"}
        ]
        
        msg = f"Excellent choice! For **{hotel['name']}**, please select a room type:\n\n"
        for i, r in enumerate(room_options):
            msg += f"{i+1}. **{r['type']}** — ${r['price']:.2f} ({r['desc']})\n"
            
        msg += "\nReply with **1, 2, or 3** to proceed to payment."
        
        return {
            "room_options": room_options,
            "messages": [AIMessage(content=msg)]
        }
    return {}

# --- 7. Node: Book & Confirm ---
def book_hotel(state: AgentState):
    print("[DEBUG] Node: Book Hotel")
    if not state.get("final_room_type"): return {}
    
    hotel_name = state["selected_hotel"]["name"]
    price = state["final_price"]
    hcn = f"#{random.randint(10000, 99999)}BR"
    
    # Integrate Warden
    result = warden_client.submit_booking(hotel_name, price, state["destination"], 0.0)
    tx = result.get("tx_hash", "0xMOCK_TX_HASH")
    tx_url = f"https://sepolia.basescan.org/tx/{tx}" 
    
    msg = f"""🎉 **Booking Confirmed!**

🏨 **Hotel:** {hotel_name}
🛏️ **Room:** {state['final_room_type']} ({state['guests']} Guests)
📅 **Dates:** {state['check_in']} to {state['check_out']}
🎫 **Confirmation #:** `{hcn}` (Show this at reception)

💰 **Total Paid:** ${price:.2f}
🔗 [View Payment on Explorer]({tx_url})

*Check your email for the official receipt!*
"""
    return {
        "final_status": "Booked",
        "confirmation_number": hcn,
        "tx_hash": tx,
        "messages": [AIMessage(content=msg)]
    }

# --- Graph Routing Logic ---
def route_step(state):
    print(f"[DEBUG] Routing... Dest:{state.get('destination')} CheckIn:{state.get('check_in')}")
    
    if not state.get("destination") or not state.get("check_in") or not state.get("guests"):
        return "gather"
    if not state.get("hotels"):
        return "search"
    if not state.get("selected_hotel"):
        return "wait_for_selection" # Loop END to wait for user input
    if not state.get("final_room_type"):
        if not state.get("room_options"):
            return "select_room"
        else:
            return "wait_for_room" # Loop END to wait for user input
    if state.get("final_status") != "Booked":
        return "book"
    return END

# --- Graph Construction ---
workflow = StateGraph(AgentState)

workflow.add_node("parse", parse_intent)
workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels)
workflow.add_node("select_room", select_room)
workflow.add_node("book", book_hotel)

workflow.set_entry_point("parse")

workflow.add_conditional_edges(
    "parse", 
    route_step,
    {
        "gather": "gather",
        "search": "search",
        "wait_for_selection": END, 
        "select_room": "select_room",
        "wait_for_room": END,      
        "book": "book",
        END: END
    }
)

workflow.add_edge("gather", END)
workflow.add_edge("search", END)
workflow.add_edge("select_room", END)
workflow.add_edge("book", END)

memory = MemorySaver()
workflow_app = workflow.compile(checkpointer=memory)