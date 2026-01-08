# agent.py
import os
import requests
import random
from dotenv import load_dotenv
from datetime import date, timedelta, datetime
from typing import TypedDict, List, Union, Optional

from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

import warden_client

load_dotenv()

BOOKING_KEY = os.getenv("BOOKING_API_KEY")

# --- 1. Enhanced State Management ---
class AgentState(TypedDict, total=False):
    messages: List[BaseMessage]
    user_query: str
    
    # Booking Details
    destination: str
    check_in: str
    check_out: str
    guests: int
    rooms: int
    
    # Search & Selection
    budget_usd: float
    hotels: List[dict]          # The live list from API
    selected_hotel: dict        # The specific hotel user picked
    room_options: List[dict]    # The calculated room tiers (Standard/Deluxe)
    final_room_type: str        # The chosen room
    final_price: float
    
    # Transaction
    needs_swap: bool
    tx_hash: str
    confirmation_number: str

# --- 2. Helper: Date Parser ---
def parse_date(text: str) -> str:
    # Simple parser: accepts "YYYY-MM-DD" or assumes relative days if simple numbers
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except:
        # Fallback: if user says "20", assume "2026-01-20" (Demo logic)
        # For production, you'd want a robust library like dateparser
        if "jan" in text.lower(): return "2026-01-20"
        if "feb" in text.lower(): return "2026-02-10"
        return (date.today() + timedelta(days=1)).strftime("%Y-%m-%d") # Default tomorrow

# --- 3. Node: Intent Parser & Router ---
def parse_intent(state: AgentState):
    """
    Determines what the user is answering based on previous context.
    """
    messages = state.get("messages", [])
    if not messages: return {}
    
    last_user_msg = ""
    last_ai_msg = ""
    
    # Get last user input
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            last_user_msg = m.content.strip()
            break
            
    # Get last AI question to know what we are answering
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            last_ai_msg = m.content
            break

    updates = {}
    text = last_user_msg.lower()

    # --- Context-Aware Extraction ---
    
    # 1. Answering Destination?
    if "where would you like to go" in last_ai_msg.lower() or "city or country" in last_ai_msg.lower():
        updates["destination"] = last_user_msg.title()
    
    # 2. Answering Dates?
    elif "check-in" in last_ai_msg.lower():
        updates["check_in"] = parse_date(last_user_msg)
        # Auto-set checkout to +2 days for demo simplicity if not specified
        updates["check_out"] = (datetime.strptime(updates["check_in"], "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")

    # 3. Answering Guests/Rooms?
    elif "how many guests" in last_ai_msg.lower():
        nums = [int(s) for s in last_user_msg.split() if s.isdigit()]
        if nums:
            updates["guests"] = nums[0]
            updates["rooms"] = nums[1] if len(nums) > 1 else 1

    # 4. Picking a Hotel? (e.g., "Option 1")
    elif state.get("hotels") and not state.get("selected_hotel"):
        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(state["hotels"]):
                updates["selected_hotel"] = state["hotels"][idx]
    
    # 5. Picking a Room Type? (e.g., "Standard" or "1")
    elif state.get("selected_hotel") and not state.get("final_room_type"):
        options = state.get("room_options", [])
        if text.isdigit() and 0 <= int(text)-1 < len(options):
            chosen = options[int(text)-1]
            updates["final_room_type"] = chosen["type"]
            updates["final_price"] = chosen["price"]
        elif "deluxe" in text:
            # Simple keyword matching
            match = next((r for r in options if "Deluxe" in r["type"]), options[0])
            updates["final_room_type"] = match["type"]
            updates["final_price"] = match["price"]
        else:
            # Default to first option
            updates["final_room_type"] = options[0]["type"]
            updates["final_price"] = options[0]["price"]

    # Initial Prompt Extraction (if starting new)
    if not state.get("destination") and not updates.get("destination"):
        if " in " in text:
            try: updates["destination"] = text.split(" in ")[1].strip("?.").title()
            except: pass

    updates["user_query"] = last_user_msg
    return updates

# --- 4. Node: Gather Requirements (The Interview) ---
def gather_requirements(state: AgentState):
    """
    Ensures we have all necessary details before searching.
    """
    # 1. Check Destination
    if not state.get("destination") or state.get("destination") == "Unknown":
        return {"messages": [AIMessage(content="👋 Welcome to Warden Travel! To find you the best hotels, which **City** or **Country** are you visiting?")]}
    
    # 2. Check Dates
    if not state.get("check_in"):
        return {"messages": [AIMessage(content=f"Great, **{state['destination']}** is beautiful! 📅 When would you like to **Check-in**? (YYYY-MM-DD)")]}
    
    # 3. Check Guests/Rooms
    if not state.get("guests"):
        return {"messages": [AIMessage(content="Got the dates. 👥 **How many guests** are travelling, and how many **rooms** do you need? (e.g. '2 guests 1 room')")]}
    
    # All good? Pass to search
    return {}

# --- 5. Node: Search Hotels (Live API) ---
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
    # Only run if we don't have hotels yet
    if state.get("hotels"): return {}

    city = state.get("destination")
    checkin = state.get("check_in")
    checkout = state.get("check_out")
    guests = state.get("guests", 1)
    
    msg_start = f"🔎 Searching **live availability** in {city} for {guests} guests ({checkin} to {checkout})..."
    
    dest_id, dest_type = get_destination_data(city)
    if not dest_id: 
        return {"messages": [AIMessage(content=f"⚠️ Could not find location '{city}'. Please try a major city name.")]}

    # API Call
    url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
    headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
    params = {
        "dest_id": str(dest_id), "dest_type": dest_type,
        "checkin_date": checkin, "checkout_date": checkout,
        "adults_number": str(guests), "room_number": str(state.get("rooms", 1)),
        "units": "metric", "filter_by_currency": "USD", "order_by": "price"
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        data = response.json()
        hotels = []
        
        # Parse Results
        for h in data.get("result", [])[:5]:
            name = h.get("hotel_name", "Unknown")
            # Try to get gross price
            try: price = float(h.get("composite_price_breakdown", {}).get("gross_amount", {}).get("value", 0))
            except: price = 0.0
            
            # Fallback if price is 0 (some APIs vary)
            if price == 0: price = float(h.get("min_total_price", 150))
            
            rating = "⭐" * int(round(h.get("review_score", 0)/2)) # Convert 10-scale to 5-stars
            if not rating: rating = "⭐⭐⭐"
            
            hotels.append({"name": name, "price": price, "rating": rating})

        if not hotels:
            return {"messages": [AIMessage(content=f"No hotels found in {city} for these dates.")]}

        # Format Output
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

# --- 6. Node: Select Room Type ---
def select_room(state: AgentState):
    # If we have a hotel but no room type, show options
    if state.get("selected_hotel") and not state.get("room_options"):
        hotel = state["selected_hotel"]
        base_price = hotel["price"]
        
        # Calculate Real-ish Tiers based on the live base price
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
    if not state.get("final_room_type"): return {}
    
    # Simulate Blockchain Tx
    hotel_name = state["selected_hotel"]["name"]
    price = state["final_price"]
    
    # Generate Mock Hotel Confirmation Number (HCN)
    hcn = f"#{random.randint(10000, 99999)}BR"
    
    result = warden_client.submit_booking(hotel_name, price, state["destination"], 0.0)
    tx = result.get("tx_hash", "0xMOCK_TX_HASH")
    tx_url = f"https://sepolia.basescan.org/tx/{tx}" # Assuming Base Sepolia for Warden
    
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

# --- Graph Construction ---
def route_step(state):
    # Logic to decide "What to do next?"
    if not state.get("destination") or not state.get("check_in") or not state.get("guests"):
        return "gather"
    if not state.get("hotels"):
        return "search"
    if not state.get("selected_hotel"):
        return "wait_for_selection" # Loop back to END, wait for user input
    if not state.get("final_room_type"):
        if not state.get("room_options"):
            return "select_room"
        else:
            return "wait_for_room" # Loop back, wait for user input
    if state.get("final_status") != "Booked":
        return "book"
    return END

workflow = StateGraph(AgentState)

workflow.add_node("parse", parse_intent)
workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels)
workflow.add_node("select_room", select_room)
workflow.add_node("book", book_hotel)

workflow.set_entry_point("parse")

# Conditional Edges determine flow
workflow.add_conditional_edges(
    "parse", 
    route_step,
    {
        "gather": "gather",
        "search": "search",
        "wait_for_selection": END, # Stop and show list
        "select_room": "select_room",
        "wait_for_room": END,      # Stop and show room options
        "book": "book",
        END: END
    }
)

workflow.add_edge("gather", END)      # Ask question, then stop
workflow.add_edge("search", END)      # Show results, then stop
workflow.add_edge("select_room", END) # Show rooms, then stop
workflow.add_edge("book", END)        # Show receipt, then stop

memory = MemorySaver()
workflow_app = workflow.compile(checkpointer=memory)