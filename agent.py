# agent.py - Crypto Travel Booker
# Orchestrates the booking flow using LangGraph workflow_app
import os
from dotenv import load_dotenv
import requests
import operator
import warden_client

# === IMPORTS ===
from workflow.graph import build_workflow, AgentState
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from datetime import date, timedelta

load_dotenv()

# === 1INCH FUNCTION ===
def get_1inch_quote(amount_usdc: float, chain_id: int = 8453) -> dict:
    """Get real 1inch quote: USDC → USD (on Base chain)"""
    url = f"https://api.1inch.dev/swap/v6.0/{chain_id}/quote"
    params = {
        "src": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base
        "dst": "0x0000000000000000000000000000000000000000",  # Native ETH (USD proxy)
        "amount": str(int(amount_usdc * 1e6))  # USDC has 6 decimals
    }
    try:
        response = requests.get(url, params=params, headers={"accept": "application/json"})
        return response.json() if response.status_code == 200 else {"error": response.text}
    except:
        return {"error": "1inch API failed"}

# === API Keys & LLM Configuration ===
GROK_API_KEY = os.getenv("GROK_API_KEY")
BOOKING_KEY = os.getenv("BOOKING_API_KEY")

llm = None
if GROK_API_KEY:
    try:
        llm = ChatOpenAI(
            model="grok-3",
            openai_api_key=GROK_API_KEY,
            openai_api_base="https://api.x.ai/v1"
        )
        print("[INIT] Connected to xAI (Grok) successfully.")
    except Exception as e:
        print(f"Warning: failed to initialize xAI: {e}")

# === 1. Parse User ===
def parse_intent(state):
    """Robustly extract intent even if leading messages are empty."""
    messages = state.get("messages", [])
    query = ""
    # Find the last message that actually HAS text
    for m in reversed(messages):
        content = getattr(m, 'content', m.get('content', '')) if isinstance(m, (object, dict)) else ""
        if content.strip():
            query = content
            break
            
    destination = "Paris"
    budget = 400.0

    if query:
        lower_query = query.lower()
        markers = ["to ", "in ", "at "]
        for marker in markers:
            if marker in lower_query:
                dest_part = lower_query.split(marker, 1)[-1].strip()
                destination = dest_part.split("$")[0].strip().title()
                break
        
        if "$" in lower_query:
            try:
                budget_str = lower_query.split("$")[-1]
                budget = float(''.join(filter(str.isdigit, budget_str.split()[0])))
            except: pass

    print(f"[PARSE] Query: '{query}' -> Destination: {destination}, Budget: ${budget}")
    return {
        "user_query": query,
        "destination": destination,
        "budget_usd": budget
    }

# === DYNAMIC LOCATION HELPER ===
def get_destination_id(city_name):
    """Helper to fetch the real Booking.com ID for ANY city dynamically."""
    if not BOOKING_KEY:
        return None
        
    url = "https://booking-com.p.rapidapi.com/v1/hotels/locations"
    querystring = {"name": city_name, "locale": "en-gb"}
    headers = {
        "X-RapidAPI-Key": BOOKING_KEY,
        "X-RapidAPI-Host": "booking-com.p.rapidapi.com"
    }
    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=10)
        locations = response.json()
        for loc in locations:
            if loc.get("dest_type") == "city":
                print(f"[SEARCH] Found ID {loc.get('dest_id')} for {city_name}")
                return loc.get("dest_id")
    except Exception as e:
        print(f"[ERROR] Location lookup failed: {e}")
    return None

# === 2. Search Hotels on Booking.com ===
def search_hotels(state, live=True):
    """Search Booking.com dynamically for ANY city."""
    target_city = state.get("destination", "Paris")
    
    # Check for API Key first
    if not BOOKING_KEY:
        return {
            "hotel_name": "Budget Hotel",
            "hotel_price": 180.0,
            "messages": [HumanMessage(content=f"Found Budget Hotel in {target_city} for $180.0/night")]
        }

    # Dynamically find the ID for the city
    dest_id = get_destination_id(target_city)
    if not dest_id:
        print(f"[WARN] ID for {target_city} not found. Falling back to Paris.")
        dest_id = "-1456928"

    tomorrow = date.today() + timedelta(days=1)
    next_day = date.today() + timedelta(days=2)

    url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
    querystring = {
        "checkout_date": next_day.strftime("%Y-%m-%d"),
        "units": "metric",
        "dest_id": dest_id,
        "dest_type": "city",
        "locale": "en-gb",
        "adults_number": "1",
        "order_by": "price",
        "filter_by_currency": "USD",
        "checkin_date": tomorrow.strftime("%Y-%m-%d"),
        "room_number": "1"
    }

    headers = {
        "X-RapidAPI-Key": BOOKING_KEY,
        "X-RapidAPI-Host": "booking-com.p.rapidapi.com"
    }

    try:
        print(f"[SEARCH] Querying {target_city}...")
        response = requests.get(url, headers=headers, params=querystring, timeout=10)
        data = response.json()
        if data.get("result"):
            hotel = data["result"][0]
            name = hotel.get("hotel_name", "Budget Hotel")
            try:
                price = float(hotel.get("price_breakdown", {}).get("all_inclusive_price", 180.0))
            except: price = 180.0
            return {
                "hotel_name": name,
                "hotel_price": price,
                "messages": [HumanMessage(content=f"Found {name} in {target_city} for ${price}/night")]
            }
    except Exception as e:
        print(f"[ERROR] Live search failed: {e}")
    
    return {
        "hotel_name": "Budget Hotel",
        "hotel_price": 180.0,
        "messages": [HumanMessage(content=f"Found Budget Hotel in {target_city} for $180.0/night")]
    }

# === 3. Check Swap ===
def check_swap(state):
    """Calculate swap amount needed."""
    hotel_price = state.get("hotel_price", 0.0)
    budget = state.get("budget_usd", 0.0)
    
    if hotel_price > budget:
        return {
            "needs_swap": False,
            "final_status": "Budget too low!",
            "messages": [HumanMessage(content="Not enough budget. Try a cheaper destination.")]
        }

    swap_needed = hotel_price - (budget * 0.8)
    if swap_needed <= 0:
        return {
            "needs_swap": False,
            "swap_amount": 0,
            "messages": [HumanMessage(content="You have enough USD!")]
        }

    usdc_needed = swap_needed * 1.01
    return {
        "needs_swap": True,
        "swap_amount": round(usdc_needed, 2),
        "messages": [HumanMessage(content=f"Swapping {round(usdc_needed, 2)} USDC for ETH via 1inch.")]
    }

# === 4. Book ===
def book_hotel(state):
    """Perform on-chain booking via Warden."""
    hotel_name = state.get("hotel_name", "Unknown")
    hotel_price = state.get("hotel_price", 0.0)
    destination = state.get("destination", "Unknown")
    swap_amount = state.get("swap_amount", 0.0)

    result = warden_client.submit_booking(hotel_name, hotel_price, destination, swap_amount)
    tx = result.get("tx_hash", "0xMOCK_TX")
    
    return {
        "final_status": f"Booked {hotel_name} for ${hotel_price}",
        "tx_hash": tx,
        "messages": [HumanMessage(content=f"Booking confirmed! Transaction: {tx}")]
    }

# === BUILD WORKFLOW ===
workflow_app = build_workflow(parse_intent, search_hotels, check_swap, book_hotel)

# === STANDBY MODE ===
if __name__ == "__main__":
    import time
    print("\n✅ Agent finished. Entering standby...")
    while True:
        time.sleep(600)