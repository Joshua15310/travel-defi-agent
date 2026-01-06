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
    
    # Iterate backwards to find the last real message
    for m in reversed(messages):
        # Handle message objects (HumanMessage) or dicts
        content = getattr(m, 'content', m.get('content', '')) if isinstance(m, (object, dict)) else ""
        if content and content.strip():
            text = content.strip()
            break
            
    # Default values
    destination = "Unknown"
    budget = 400.0

    if not text:
        return {"destination": destination, "budget_usd": budget, "user_query": ""}

    lowered = text.lower()
    
    # 1. Extract Destination (Robustly)
    for token in [" in ", " to ", " at ", " for "]:
        if token in lowered:
            try:
                # Split and clean punctuation
                parts = lowered.split(token, 1)
                if len(parts) > 1:
                    destination = parts[1].split()[0].strip("?.,!\"'").capitalize()
                    break
            except IndexError:
                pass

    # Fallback: Use last word if marker search failed
    if destination == "Unknown":
        words = text.split()
        if words:
            destination = words[-1].strip("?.,!\"'").capitalize()

    # 2. Extract Budget (Fixing the $500" quote bug)
    if "$" in text:
        try:
            raw_budget = text.split("$", 1)[1].split()[0]
            # Remove quotes, commas, and punctuation
            clean_budget = raw_budget.replace(",", "").replace('"', '').replace("'", "").strip("?.,!")
            budget = float(clean_budget)
        except Exception:
            pass

    print(f"[DEBUG] Found User Query: '{text}' -> Dest: {destination}, Budget: {budget}")
    return {
        "user_query": text,
        "destination": destination,
        "budget_usd": budget
    }


def get_destination_data(city):
    """Returns both ID and Type to support Countries (England) & Cities."""
    if not BOOKING_KEY:
        return None, None

    try:
        url = "https://booking-com.p.rapidapi.com/v1/hotels/locations"
        headers = {
            "X-RapidAPI-Key": BOOKING_KEY,
            "X-RapidAPI-Host": "booking-com.p.rapidapi.com"
        }
        # CRITICAL FIX: Use 'en-us' to prevent 422 errors on free tier
        params = {"name": city, "locale": "en-us"}

        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        
        data = r.json()
        if data:
            # Take the first best match (City, Region, or Country)
            first = data[0]
            return first.get("dest_id"), first.get("dest_type")
            
    except Exception as e:
        print(f"[ERROR] Failed to get destination data for {city}: {e}")
    return None, None

def search_hotels(state):
    city = state.get("destination", "Unknown")
    budget = state.get("budget_usd", 400.0)
    
    # Dynamic Lookup (ID + Type)
    dest_id, dest_type = get_destination_data(city)
    
    # Fallback to Paris if lookup fails
    if not dest_id:
        print(f"[WARN] No destination found for {city}. Using fallback.")
        dest_id = "-1746443"
        dest_type = "city"

    tomorrow = date.today() + timedelta(days=1)
    next_day = tomorrow + timedelta(days=1)

    url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
    headers = {
        "X-RapidAPI-Key": os.getenv("BOOKING_API_KEY"),
        "X-RapidAPI-Host": "booking-com.p.rapidapi.com"
    }
    
    # CRITICAL FIX: Clean parameters (No order_by, en-us locale)
    params = {
        "dest_id": str(dest_id),
        "dest_type": dest_type,
        "checkin_date": tomorrow.strftime("%Y-%m-%d"),
        "checkout_date": next_day.strftime("%Y-%m-%d"),
        "adults_number": "1",
        "room_number": "1",
        "units": "metric",
        "locale": "en-us"
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        
        if response.status_code != 200:
            print(f"[ERROR] API Fail {response.status_code}: {response.url}")
            raise Exception(f"API Error {response.status_code}")
        
        data = response.json()
        hotels = []
        
        for h in data.get("result", [])[:5]:
            name = h.get("hotel_name", "Unknown Hotel")
            
            # Robust Price Extraction
            raw_price = h.get("min_total_price")
            if raw_price is None:
                 raw_price = h.get("price_breakdown", {}).get("all_inclusive_price", 0)
            
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                price = 0.0

            if price > 0 and price <= budget:
                hotels.append({"name": name, "price": price})

        if not hotels:
            msg = f"No hotels found in {city} under ${budget}."
            return {
                "hotels": [],
                "final_status": "No hotels found",
                "messages": [HumanMessage(content=msg)]
            }

        message = f"Top hotels in {city}:\n" + "\n".join([f"{h['name']} - ${h['price']}/night" for h in hotels])
        
        return {
            "hotels": hotels,
            "hotel_name": hotels[0]["name"],
            "hotel_price": hotels[0]["price"],
            "messages": [HumanMessage(content=message)]
        }

    except Exception as e:
        fallback = {"name": f"Mock Hotel in {city}", "price": 150.0}
        msg = f"Live search failed ({str(e)}). Using demo data:\n{fallback['name']} - ${fallback['price']}/night"
        return {
            "hotels": [fallback],
            "hotel_name": fallback["name"],
            "hotel_price": fallback["price"],
            "messages": [HumanMessage(content=msg)]
        }

def check_swap(state):
    return {"needs_swap": False}

def book_hotel(state):
    hotels = state.get("hotels", [])
    destination = state.get("destination", "Unknown")

    if not hotels:
        return {
            "final_status": "No booking made",
            "messages": [HumanMessage(content="No hotels available to book.")]
        }

    chosen = hotels[0]
    result = warden_client.submit_booking(chosen["name"], chosen["price"], destination, 0.0)
    tx = result.get("tx_hash", "0xMOCK")

    return {
        "final_status": "Booked",
        "tx_hash": tx,
        "messages": [HumanMessage(content=f"Booked {chosen['name']} in {destination} for ${chosen['price']}. TX {tx}")]
    }

workflow_app = build_workflow(parse_intent, search_hotels, check_swap, book_hotel)

if __name__ == "__main__":
    import time
    print("\n✅ Agent finished. Entering standby...")
    while True:
        time.sleep(600)