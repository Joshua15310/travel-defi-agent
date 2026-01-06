# agent.py - Crypto Travel Booker
import os
import requests
from dotenv import load_dotenv
from datetime import date, timedelta
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
import warden_client

from workflow.graph import build_workflow

load_dotenv()

BOOKING_KEY = os.getenv("BOOKING_API_KEY")
GROK_API_KEY = os.getenv("GROK_API_KEY")

llm = None
if GROK_API_KEY:
    llm = ChatOpenAI(
        model="grok-3",
        openai_api_key=GROK_API_KEY,
        openai_api_base="https://api.x.ai/v1"
    )

def parse_intent(state):
    """Robustly find the user request by searching backwards through history."""
    messages = state.get("messages", [])
    text = ""
    
    # NEW LOGIC: Iterate backwards to skip blank UI noise
    for m in reversed(messages):
        # Handle both object and dict styles
        if isinstance(m, dict):
            content = m.get('content', '')
        else:
            content = getattr(m, 'content', '')
        if content.strip():
            text = content.strip()
            break
            
    # Default values
    destination = "Unknown"
    budget = 400.0

    if not text:
        return {"destination": destination, "budget_usd": budget, "user_query": ""}

    lowered = text.lower()
    # Markers to find the city
    for token in [" in ", " to ", " at ", " for "]:
        if token in lowered:
            # Extract word after token and capitalize
            destination = lowered.split(token, 1)[1].split()[0].strip("?.").capitalize()
            break

    # Robust Fallback: if no markers, use the last word (e.g. user just typed "Lagos")
    if destination == "Unknown":
        words = text.split()
        if words:
            destination = words[-1].strip("?.").capitalize()

    # Budget extraction
    if "$" in text:
        try:
            budget = float(text.split("$", 1)[1].split()[0].replace(",", ""))
        except:
            pass

    print(f"[DEBUG] Found User Query: '{text}' -> Destination: {destination}")
    return {
        "user_query": text,
        "destination": destination,
        "budget_usd": budget
    }


def get_destination_id(city):
    if not BOOKING_KEY:
        return None

    try:
        url = "https://booking-com.p.rapidapi.com/v1/hotels/locations"
        headers = {
            "X-RapidAPI-Key": BOOKING_KEY,
            "X-RapidAPI-Host": "booking-com.p.rapidapi.com"
        }
        params = {"name": city, "locale": "en-gb"}

        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        for loc in r.json():
            if loc.get("dest_type") == "city":
                return loc.get("dest_id")
    except Exception as e:
        print(f"[ERROR] Failed to get destination ID for {city}: {e}")
    return None

def search_hotels(state):
    city = state["destination"]

    dest_id = get_destination_id(city)
    if not dest_id:
        # Fallback to mock hotels if no dest_id
        print(f"[WARN] No destination ID found for {city}. Using mock hotels.")
        hotels = [
            {"name": f"Mock Hotel 1 in {city}", "price": 100.0},
            {"name": f"Mock Hotel 2 in {city}", "price": 150.0},
            {"name": f"Mock Hotel 3 in {city}", "price": 200.0},
            {"name": f"Mock Hotel 4 in {city}", "price": 250.0},
            {"name": f"Mock Hotel 5 in {city}", "price": 300.0},
        ]
        lines = [f"{h['name']} - ${h['price']}/night" for h in hotels]
        message = f"Top hotels in {city} (mock data):\n" + "\n".join(lines)
        return {
            "hotels": hotels,
            "hotel_name": hotels[0]["name"],
            "hotel_price": hotels[0]["price"],
            "messages": [HumanMessage(content=message)]
        }

    tomorrow = date.today() + timedelta(days=1)
    next_day = tomorrow + timedelta(days=1)

    try:
        url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
        headers = {
            "X-RapidAPI-Key": BOOKING_KEY,
            "X-RapidAPI-Host": "booking-com.p.rapidapi.com"
        }
        params = {
            "dest_id": dest_id,
            "dest_type": "city",
            "checkin_date": tomorrow.strftime("%Y-%m-%d"),
            "checkout_date": next_day.strftime("%Y-%m-%d"),
            "adults_number": "1",
            "room_number": "1",
            "order_by": "price",
            "filter_by_currency": "USD",
            "locale": "en-gb"
        }

        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        if not data.get("result"):
            # Fallback to mock if no results
            print(f"[WARN] No hotels found for {city} via API. Using mock hotels.")
            hotels = [
                {"name": f"Mock Hotel 1 in {city}", "price": 100.0},
                {"name": f"Mock Hotel 2 in {city}", "price": 150.0},
                {"name": f"Mock Hotel 3 in {city}", "price": 200.0},
                {"name": f"Mock Hotel 4 in {city}", "price": 250.0},
                {"name": f"Mock Hotel 5 in {city}", "price": 300.0},
            ]
            lines = [f"{h['name']} - ${h['price']}/night" for h in hotels]
            message = f"Top hotels in {city} (mock data):\n" + "\n".join(lines)
            return {
                "hotels": hotels,
                "hotel_name": hotels[0]["name"],
                "hotel_price": hotels[0]["price"],
                "messages": [HumanMessage(content=message)]
            }

        hotels = []
        lines = []

        for h in data["result"][:5]:
            name = h.get("hotel_name", "Unknown Hotel")
            price = float(h.get("price_breakdown", {}).get("all_inclusive_price", 0))
            hotels.append({"name": name, "price": price})
            lines.append(f"{name} - ${price}/night")

        message = f"Top hotels in {city}:\n" + "\n".join(lines)

        return {
            "hotels": hotels,
            "hotel_name": hotels[0]["name"],
            "hotel_price": hotels[0]["price"],
            "messages": [HumanMessage(content=message)]
        }
    except Exception as e:
        print(f"[ERROR] Failed to search hotels for {city}: {e}. Using mock hotels.")
        hotels = [
            {"name": f"Mock Hotel 1 in {city}", "price": 100.0},
            {"name": f"Mock Hotel 2 in {city}", "price": 150.0},
            {"name": f"Mock Hotel 3 in {city}", "price": 200.0},
            {"name": f"Mock Hotel 4 in {city}", "price": 250.0},
            {"name": f"Mock Hotel 5 in {city}", "price": 300.0},
        ]
        lines = [f"{h['name']} - ${h['price']}/night" for h in hotels]
        message = f"Top hotels in {city} (mock data due to error):\n" + "\n".join(lines)
        return {
            "hotels": hotels,
            "hotel_name": hotels[0]["name"],
            "hotel_price": hotels[0]["price"],
            "messages": [HumanMessage(content=message)]
        }


def check_swap(state):
    return {"needs_swap": False}

def book_hotel(state):
    hotels = state.get("hotels", [])

    if not hotels:
        return {
            "final_status": "No booking made",
            "messages": [HumanMessage(content="No hotels available to book.")]
        }

    chosen = hotels[0]

    result = warden_client.submit_booking(
        chosen["name"],
        chosen["price"],
        state["destination"],
        0.0
    )

    tx = result.get("tx_hash", "0xMOCK")

    return {
        "final_status": "Booked",
        "tx_hash": tx,
        "messages": [
            HumanMessage(
                content=f"Booked {chosen['name']} in {state['destination']} for ${chosen['price']}. TX {tx}"
            )
        ]
    }

# ... (all your node functions: parse_intent, search_hotels, etc.)

# === BUILD WORKFLOW ===
workflow_app = build_workflow(parse_intent, search_hotels, check_swap, book_hotel)

# === STANDBY MODE ===
if __name__ == "__main__":
    import time
    print("\n✅ Agent finished. Entering standby...")
    while True:
        time.sleep(600)

