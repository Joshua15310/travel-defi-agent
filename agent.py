# agent.py - Crypto Travel Booker (Final Fix)
import os
import requests
from dotenv import load_dotenv
from datetime import date, timedelta
from typing import TypedDict, List, Optional

# LangChain / LangGraph Imports
from langchain_core.messages import HumanMessage, BaseMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

import warden_client

load_dotenv()

BOOKING_KEY = os.getenv("BOOKING_API_KEY")
GROK_API_KEY = os.getenv("GROK_API_KEY")

# --- CRITICAL FIX HERE ---
# added 'total=False'. This fixes the 422 Error by making fields optional.
class AgentState(TypedDict, total=False):
    messages: List[BaseMessage]
    user_query: str
    destination: str
    budget_usd: float
    hotels: List[dict]
    hotel_name: Optional[str]
    hotel_price: Optional[float]
    needs_swap: bool
    swap_amount: float
    final_status: str
    tx_hash: str

# --- Node Functions ---

def parse_intent(state):
    """Extracts text and preserves state for multi-turn conversation."""
    messages = state.get("messages", [])
    text = ""
    
    # Find user's latest message
    for m in reversed(messages):
        if hasattr(m, 'content'):
            content = m.content
        elif isinstance(m, dict) and 'content' in m:
            content = m['content']
        else:
            content = ""
        if content and content.strip():
            text = content.strip()
            break
            
    # Preserve existing state safely
    current_dest = state.get("destination", "Unknown")
    current_budget = state.get("budget_usd", 400.0)
    
    if not text:
        return {"user_query": ""}

    # Check for selection (e.g. "1")
    is_selection = False
    if text.isdigit() and len(text) < 2:
        is_selection = True
    elif text.lower().startswith("option ") or text.lower() in ["first", "second", "third"]:
        is_selection = True
        
    if is_selection:
        return {"user_query": text}

    # Normal Search Parsing
    destination = current_dest
    budget = current_budget
    lowered = text.lower()
    
    for token in [" in ", " to ", " at ", " for "]:
        if token in lowered:
            try:
                parts = lowered.split(token, 1)
                if len(parts) > 1:
                    raw_dest = parts[1].split()[0].strip("?.,!\"'")
                    if raw_dest:
                        destination = raw_dest.capitalize()
                    break
            except IndexError:
                pass

    if destination == "Unknown" or destination == current_dest:
        words = text.split()
        if words and len(words) > 1 and "hotel" in lowered:
             candidate = words[-1].strip("?.,!\"'").capitalize()
             if candidate.lower() not in ["hotel", "booking", "room"]:
                 destination = candidate

    if "$" in text:
        try:
            raw_budget = text.split("$", 1)[1].split()[0]
            clean_budget = raw_budget.replace(",", "").replace('"', '').replace("'", "").strip("?.,!")
            budget = float(clean_budget)
        except Exception:
            pass

    print(f"[DEBUG] User Query: '{text}' -> Dest: {destination}, Budget: {budget}")
    return {
        "user_query": text,
        "destination": destination,
        "budget_usd": budget
    }


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
    except Exception as e:
        print(f"[ERROR] Dest lookup failed: {e}")
    return None, None

def search_hotels(state):
    user_query = state.get("user_query", "").lower()
    existing_hotels = state.get("hotels", [])
    
    # MODE 1: SELECTION
    if existing_hotels and (user_query.isdigit() or user_query in ["first", "second", "third", "1", "2", "3", "4", "5"]):
        try:
            index = -1
            if user_query.isdigit(): index = int(user_query) - 1
            elif "first" in user_query: index = 0
            elif "second" in user_query: index = 1
            elif "third" in user_query: index = 2
            
            if 0 <= index < len(existing_hotels):
                selected = existing_hotels[index]
                msg = f"✅ You selected: {selected['name']} (${selected['price']}). Proceeding to book..."
                return {
                    "hotel_name": selected["name"],
                    "hotel_price": selected["price"],
                    "messages": [HumanMessage(content=msg)]
                }
            else:
                 return {"messages": [HumanMessage(content="⚠️ Invalid number. Please choose 1-5.")]}
        except Exception:
            pass

    # MODE 2: NEW SEARCH
    city = state.get("destination", "Unknown")
    budget = state.get("budget_usd", 400.0)
    
    if city == "Unknown":
        return {"messages": [HumanMessage(content="Where would you like to go?")]}

    if not BOOKING_KEY:
        fallback = [
            {"name": f"Mock Hotel A in {city}", "price": 150.0},
            {"name": f"Mock Hotel B in {city}", "price": 250.0},
            {"name": f"Mock Hotel C in {city}", "price": 90.0}
        ]
        msg = f"Found hotels in {city}:\n1. {fallback[0]['name']} - ${fallback[0]['price']}\n2. {fallback[1]['name']} - ${fallback[1]['price']}\n3. {fallback[2]['name']} - ${fallback[2]['price']}\n\nReply with 1, 2, or 3 to book."
        return {
            "hotels": fallback,
            "hotel_name": None,
            "messages": [HumanMessage(content=msg)]
        }
    
    dest_id, dest_type = get_destination_data(city)
    if not dest_id: dest_id, dest_type = "-2601889", "city" # London Default

    tomorrow = date.today() + timedelta(days=1)
    next_day = tomorrow + timedelta(days=1)
    url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
    headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
    params = {
        "dest_id": str(dest_id), "dest_type": dest_type,
        "checkin_date": tomorrow.strftime("%Y-%m-%d"), "checkout_date": next_day.strftime("%Y-%m-%d"),
        "adults_number": "1", "room_number": "1", "units": "metric", "locale": "en-us",
        "filter_by_currency": "USD", "order_by": "popularity"
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        data = response.json()
        hotels = []
        for h in data.get("result", [])[:5]:
            name = h.get("hotel_name", "Unknown")
            raw_price = h.get("min_total_price")
            if raw_price is None: raw_price = h.get("price_breakdown", {}).get("all_inclusive_price", 0)
            try: price = float(raw_price)
            except: price = 0.0
            if 0 < price <= budget: hotels.append({"name": name, "price": price})

        if not hotels:
            return {"hotels": [], "messages": [HumanMessage(content=f"No hotels found in {city} under ${budget}.")]}

        options = [f"{i+1}. {h['name']} - ${h['price']}" for i, h in enumerate(hotels)]
        msg = f"found {len(hotels)} hotels in {city} under ${budget}:\n\n" + "\n".join(options) + "\n\nReply with the number (e.g., '1') to book."
        
        return {
            "hotels": hotels,
            "hotel_name": None,
            "messages": [HumanMessage(content=msg)]
        }
    except Exception as e:
        return {"messages": [HumanMessage(content=f"Search failed: {e}")]}

def check_swap(state):
    return {"needs_swap": False}

def book_hotel(state):
    hotel_name = state.get("hotel_name")
    hotel_price = state.get("hotel_price")
    destination = state.get("destination", "Unknown")

    if not hotel_name:
        return {"final_status": "waiting"}

    result = warden_client.submit_booking(hotel_name, hotel_price, destination, 0.0)
    tx = result.get("tx_hash", "0xMOCK")

    return {
        "final_status": "Booked",
        "tx_hash": tx,
        "messages": [HumanMessage(content=f"🎉 Successfully booked {hotel_name} for ${hotel_price}. Transaction: {tx}")]
    }

# --- Build Graph ---
def build_agent():
    workflow = StateGraph(AgentState)
    
    workflow.add_node("parse", parse_intent)
    workflow.add_node("search", search_hotels)
    workflow.add_node("swap", check_swap)
    workflow.add_node("book", book_hotel)

    workflow.set_entry_point("parse")
    workflow.add_edge("parse", "search")
    
    def route_after_search(state):
        if state.get("hotel_name"): return "swap"
        return END

    workflow.add_conditional_edges("search", route_after_search, {"swap": "swap", END: END})
    workflow.add_edge("swap", "book")
    workflow.add_edge("book", END)
    
    memory = MemorySaver()
    return workflow.compile(checkpointer=memory)

workflow_app = build_agent()

if __name__ == "__main__":
    import time
    print("\n✅ Agent finished. Entering standby...")
    while True: time.sleep(600)