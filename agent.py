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
    # Append-only message history to prevent overwriting
    messages: Annotated[List[BaseMessage], operator.add]
    user_query: str
    
    # Booking Parameters
    destination: str
    check_in: str
    check_out: str
    guests: int
    rooms: int
    
    # Search Results
    budget_usd: float
    hotels: List[dict]
    selected_hotel: dict
    room_options: List[dict]
    final_room_type: str
    final_price: float
    
    # Transaction Info
    needs_swap: bool
    tx_hash: str
    confirmation_number: str
    final_status: str

# --- 2. Helpers ---

def parse_date(text: str) -> str:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except:
        # Smart relative dates
        text = text.lower()
        today = date.today()
        if "tomorrow" in text: return (today + timedelta(days=1)).strftime("%Y-%m-%d")
        if "next week" in text: return (today + timedelta(days=7)).strftime("%Y-%m-%d")
        if "jan" in text: return "2026-01-20"
        if "feb" in text: return "2026-02-10"
        if "mar" in text: return "2026-03-15"
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")

def extract_text(content) -> str:
    """Robustly extracts text from any Vercel message format (String/List/Dict)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join([extract_text(item) for item in content])
    if isinstance(content, dict):
        return content.get("text", str(content))
    return str(content)

# --- 3. Node: Intent Parser (DYNAMIC & SMART) ---
def parse_intent(state: AgentState):
    """
    Parses user input and UPDATES state. 
    Handles: New Destinations, New Dates, and "Which date?" questions.
    """
    messages = state.get("messages", [])
    if not messages: return {}
    
    last_user_text = ""
    last_ai_text = ""
    
    # Extract latest messages safely
    for m in reversed(messages):
        m_type = getattr(m, 'type', '') or (m.get('type') if isinstance(m, dict) else '')
        if m_type == 'human' or isinstance(m, HumanMessage):
            raw = getattr(m, 'content', '') or (m.get('content') if isinstance(m, dict) else '')
            last_user_text = extract_text(raw).strip()
            break
            
    for m in reversed(messages):
        m_type = getattr(m, 'type', '') or (m.get('type') if isinstance(m, dict) else '')
        if m_type == 'ai' or isinstance(m, AIMessage):
            raw = getattr(m, 'content', '') or (m.get('content') if isinstance(m, dict) else '')
            last_ai_text = extract_text(raw).strip()
            break

    print(f"[DEBUG] User Input: '{last_user_text}'")
    
    updates = {}
    text = last_user_text
    lowered_text = text.lower()
    lowered_ai = last_ai_text.lower()

    if not text: return {}

    # --- A. HANDLE "WHICH DATE?" QUESTIONS ---
    # If user asks "Which dates are available?", reset the date so the agent asks for it.
    if "which" in lowered_text and ("date" in lowered_text or "day" in lowered_text):
        print("[DEBUG] User asked for dates. Resetting check_in.")
        updates["check_in"] = "" # Clear date to force 'gather_requirements'
        updates["hotels"] = []   # Clear hotels
        return updates

    # --- B. DETECT DESTINATION CHANGE ---
    new_dest = None
    found_marker = False
    
    for token in [" in ", " to ", " at ", "about "]:
        if token in lowered_text:
            try:
                candidate = text.split(token, 1)[1].strip("?.").title()
                if len(candidate) > 2 and not candidate[0].isdigit():
                    new_dest = candidate
                    found_marker = True
                    break
            except: pass
    
    # Blind capture for simple city names (e.g. "Abuja")
    if not found_marker and len(text.split()) < 3 and lowered_text not in ["hi", "hello", "yes", "no"]:
        if not text.isdigit():
            new_dest = text.title()

    if new_dest:
        print(f"[DEBUG] New Destination: {new_dest}")
        updates["destination"] = new_dest
        updates["hotels"] = []         # Force re-search
        updates["selected_hotel"] = {} # Clear selection
        updates["final_room_type"] = ""

    # --- C. DETECT DATE/GUEST CHANGE ---
    if "check-in" in lowered_ai or "date" in lowered_text or "march" in lowered_text or "february" in lowered_text:
        updates["check_in"] = parse_date(text)
        updates["check_out"] = (datetime.strptime(updates["check_in"], "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
        updates["hotels"] = [] # Force re-search
        
    elif "guests" in lowered_ai or "guest" in lowered_text:
        nums = [int(s) for s in text.split() if s.isdigit()]
        if nums:
            updates["guests"] = nums[0]
            updates["rooms"] = nums[1] if len(nums) > 1 else 1
            updates["hotels"] = [] # Force re-search

    # --- D. SELECTION LOGIC ---
    if state.get("hotels") and not updates.get("hotels") == []:
        if text.isdigit():
            idx = int(text) - 1
            if not state.get("selected_hotel"):
                if 0 <= idx < len(state["hotels"]):
                    updates["selected_hotel"] = state["hotels"][idx]
            elif not state.get("final_room_type"):
                options = state.get("room_options", [])
                if 0 <= idx < len(options):
                    updates["final_room_type"] = options[idx]["type"]
                    updates["final_price"] = options[idx]["price"]

    updates["user_query"] = text
    return updates

# --- 4. Node: Gather Requirements ---
def gather_requirements(state: AgentState):
    if not state.get("destination"):
        return {"messages": [AIMessage(content="👋 Welcome to Warden Travel! Which **City** or **Country** are you visiting?")]}
    if not state.get("check_in"):
        # If user asked "Which date?", this message will now trigger:
        return {"messages": [AIMessage(content=f"I can check any dates for **{state['destination']}**. When would you like to go? (e.g., 'March 1st' or 'Tomorrow')")]}
    if not state.get("guests"):
        return {"messages": [AIMessage(content="Got the dates. 👥 **How many guests** and how many **rooms**?")]}
    return {}

# --- 5. Node: Search Hotels (Live API) ---
def get_destination_data(city):
    if not BOOKING_KEY: return None, None
    try:
        url = "https://booking-com.p.rapidapi.com/v1/hotels/locations"
        headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
        params = {"name": city, "locale": "en-us"}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        data = r.json()
        if data: return data[0].get("dest_id"), data[0].get("dest_type")
    except: pass
    return None, None

def search_hotels(state: AgentState):
    if state.get("hotels"): return {}

    city = state.get("destination")
    checkin = state.get("check_in")
    checkout = state.get("check_out")
    guests = state.get("guests", 1)
    
    msg_start = f"🔎 Searching **live availability** in {city} ({checkin})..."
    
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
            
            score = h.get("review_score", 0) or 0
            rating = "⭐" * int(round(score/2)) if score else "New"
            
            hotels.append({"name": name, "price": price, "rating": rating})

        if not hotels:
            # Helpful message if search fails
            return {"messages": [AIMessage(content=f"😔 No hotels found in **{city}** for {checkin}.\n\nYou can say **'What about Abuja?'** or **'Check March 5th'** to try again.")]}

        options_text = ""
        for i, h in enumerate(hotels):
            options_text += f"{i+1}. **{h['name']}** — ${h['price']:.2f} {h['rating']}\n"
            
        final_msg = f"{msg_start}\n\nI found {len(hotels)} great options:\n\n{options_text}\nReply with the **number** of the hotel you want to book."
        
        return {
            "hotels": hotels,
            "messages": [AIMessage(content=final_msg)]
        }

    except Exception as e:
        return {"messages": [AIMessage(content=f"Search Error: {str(e)}")]}

# --- 6. Node: Select Room ---
def select_room(state: AgentState):
    if state.get("selected_hotel") and not state.get("room_options"):
        hotel = state["selected_hotel"]
        base_price = hotel["price"]
        
        room_options = [
            {"type": "Standard Room", "price": base_price},
            {"type": "Deluxe Room", "price": round(base_price * 1.3, 2)},
            {"type": "Executive Suite", "price": round(base_price * 2.5, 2)}
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
    return {
        "final_status": "Booked",
        "confirmation_number": hcn,
        "messages": [AIMessage(content=msg)]
    }

# --- Routing ---
def route_step(state):
    if not state.get("destination") or not state.get("check_in") or not state.get("guests"):
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