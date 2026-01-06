# agent.py - Crypto Travel Booker
# Orchestrates the booking flow using LangGraph workflow_app
# This file focuses on node implementations and CLI entry point.
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

# === PASTE THE 1INCH FUNCTION HERE ===
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
# === END OF 1INCH FUNCTION ===

# === API Keys & LLM Configuration ===
GROK_API_KEY = os.getenv("GROK_API_KEY")
BOOKING_KEY = os.getenv("BOOKING_API_KEY")

# Create a Grok client if possible.
llm = None
if GROK_API_KEY:
    try:
        llm = ChatOpenAI(
            model="grok-3",  # Updated to use the correct model
            openai_api_key=GROK_API_KEY,
            openai_api_base="https://api.x.ai/v1"
        )
        print("[INIT] Connected to xAI (Grok) successfully.")
    except Exception as e:
        print(f"Warning: failed to initialize xAI: {e}")
        llm = None

# === STATE ===
# The AgentState is now defined in workflow/graph.py to avoid circular imports.

# === 1. Parse User ===
def parse_intent(state):
    state.setdefault("destination", "unknown")
    state.setdefault("budget_usd", 0.0)
    
    # 1. Find the last REAL message (ignore empty ones from the UI)
    messages = state.get("messages", [])
    query = ""
    for m in reversed(messages):
        content = getattr(m, 'content', m.get('content', '')) if isinstance(m, (object, dict)) else ""
        if content and content.strip():
            query = content
            break
            
    if not query:
        print("[WARN] No valid user message found. Defaulting to Paris.")
        return {
            "user_query": "",
            "destination": "Paris",
            "budget_usd": 400.0
        }

    # 2. Logic to extract City & Budget
    destination = "Paris" # Default
    budget = 400.0
    
    # Simple Rule-Based Parsing
    lower_query = query.lower()
    
    # Extract destination city (look for common travel cities or extract name)
    # This logic now handles extracting names even if not in a pre-defined list
    markers = ["to ", "in ", "at "]
    for marker in markers:
        if marker in lower_query:
            dest_part = lower_query.split(marker, 1)[-1].strip()
            # Take the first word or words until a budget marker
            destination = dest_part.split("$")[0].strip().title()
            break
            
    # Extract Budget ($)
    if "$" in lower_query:
        try:
            budget_str = lower_query.split("$")[-1]
            budget = float(''.join(filter(str.isdigit, budget_str.split()[0])))
        except:
            pass

    print(f"[PARSE] Extracted destination='{destination}', budget=${budget}")
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
        print(f"[SEARCH] Looking up destination ID for: {city_name}")
        response = requests.get(url, headers=headers, params=querystring, timeout=10)
        locations = response.json()
        
        # Look for the first result that matches 'city'
        for loc in locations:
            if loc.get("dest_type") == "city":
                print(f"[SEARCH] Found ID {loc.get('dest_id')} for {city_name}")
                return loc.get("dest_id")
    except Exception as e:
        print(f"[ERROR] Location lookup failed: {e}")
    return None

# === 2. Search Hotels on Booking.com ===
def search_hotels(state, live=True):
    """Search for hotels on Booking.com using a dynamic ID lookup."""
    target_city = state.get("destination", "Paris")
    
    # 1. DYNAMICALLY find the ID for the city instead of a hardcoded list
    dest_id = get_destination_id(target_city)
    
    # Check Key and provide fallback if needed
    if not BOOKING_KEY or not dest_id:
        if not BOOKING_KEY:
            print("[SEARCH] No API key found. Using mocked fallback.")
        else:
            print(f"[WARN] Could not find ID for {target_city}. Using mocked fallback.")
            
        return {
            "hotel_name": "Budget Hotel",
            "hotel_price": 180.0,
            "messages": [HumanMessage(content=f"Found Budget Hotel in {target_city} for $180.0/night")]
        }

    # 2. Calculate Dates
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
        print(f"[SEARCH] Live Query to Booking.com for '{target_city}' (ID: {dest_id})...")
        response = requests.get(url, headers=headers, params=querystring, timeout=10)
        data = response.json()
        
        if data.get("result") and len(data["result"]) > 0:
            hotel = data["result"][0]
            name = hotel.get("hotel_name", "Budget Hotel")
            try:
                price = float(hotel.get("price_breakdown", {}).get("all_inclusive_price", 180.0))
                if price < 10: price = 180.0
            except:
                price = 180.0
            print(f"[SEARCH] Success! Found {name} for ${price}/night")
        else:
            print("[SEARCH] No results from API. Using mocked fallback.")
            name, price = "Budget Hotel", 180.0
            
    except Exception as e:
        print(f"[ERROR] Live search failed: {e}")
        name, price = "Budget Hotel", 180.0

    return {
        "hotel_name": name,
        "hotel_price": price,
        "messages": [HumanMessage(content=f"Found {name} in {target_city} for ${price}/night")]
    }


# === 3. Check Swap ===
def check_swap(state):
    """Calculate swap amount needed. Returns swap details and error messages."""
    try:
        hotel_price = state.get("hotel_price", 0.0)
        budget = state.get("budget_usd", 0.0)
        
        if hotel_price > budget:
            print(f"[SWAP] Budget check failed: hotel ${hotel_price} > budget ${budget}")
            return {
                "needs_swap": False,
                "final_status": "Budget too low!",
                "messages": [HumanMessage(content="Not enough budget. Try a cheaper destination.")]
            }

        swap_needed = hotel_price - (budget * 0.8)
        if swap_needed <= 0:
            print(f"[SWAP] No swap needed: sufficient USD balance ({budget} > {hotel_price})")
            return {
                "needs_swap": False,
                "swap_amount": 0,
                "messages": [HumanMessage(content="You have enough USD!")]
            }

        usdc_needed = swap_needed * 1.01
        print(f"[SWAP] Swap needed: ${usdc_needed:.2f} USDC (1% buffer included)")

        quote = get_1inch_quote(usdc_needed)
        if "error" not in quote:
            eth_out = float(quote.get('toAmount', 0)) / 1e18
            agent_message = f"Swapping {round(usdc_needed, 2):.2f} USDC for ~{eth_out:.6f} ETH via 1inch."
        else:
            agent_message = f"Swapping {round(usdc_needed, 2):.2f} USDC -> USD via 1inch. (Quote failed: {quote['error']})"

        return {
            "needs_swap": True,
            "swap_amount": round(usdc_needed, 2),
            "messages": [HumanMessage(content=agent_message)]
        }
    except Exception as e:
        print(f"[ERROR] check_swap failed: {type(e).__name__}: {e}")
        return {
            "needs_swap": False,
            "swap_amount": 0,
            "final_status": "Swap calculation error",
            "messages": [HumanMessage(content="Error calculating swap. Using available balance.")]
        }

# === 4. Book ===
def book_hotel(state):
    """Create booking confirmation. Attempt to perform on-chain booking via Warden."""
    try:
        hotel_name = state.get("hotel_name", "Unknown Hotel")
        hotel_price = state.get("hotel_price", 0.0)
        destination = state.get("destination", "Unknown")
        swap_amount = state.get("swap_amount", 0.0)

        if not hotel_name or hotel_price <= 0:
            print(f"[ERROR] Invalid booking state: hotel_name='{hotel_name}', price=${hotel_price}")
            return {
                "final_status": "Invalid booking details",
                "messages": [HumanMessage(content="Booking failed: invalid hotel details.")]
            }

        print(f"[BOOK] Confirming booking: {hotel_name} ({destination}) for ${hotel_price}")
        if swap_amount > 0:
            print(f"[BOOK] Swap: ${swap_amount} USDC")

        result = warden_client.submit_booking(hotel_name, hotel_price, destination, swap_amount)

        if result.get("tx_hash"):
            tx = result["tx_hash"]
            print(f"[BOOK] Warden tx: {tx}")
            agent_message = f"Booking confirmed on Warden! Paid with USDC. Enjoy {destination}!\nTransaction: {tx}"
            return {
                "final_status": f"Booked {hotel_name} for ${hotel_price}",
                "tx_hash": tx,
                "messages": [HumanMessage(content=agent_message)]
            }
        else:
            error_message = result.get("error", "An unknown error occurred.")
            print(f"[BOOK] Warden returned error: {error_message}")
            return {"final_status": f"Booking failed: {error_message}"}
    except Exception as e:
        print(f"[ERROR] book_hotel failed: {type(e).__name__}: {e}")
        return {
            "final_status": "Booking error",
            "messages": [HumanMessage(content="Booking failed. Please try again.")]
        }


# === BUILD WORKFLOW ===
workflow_app = build_workflow(parse_intent, search_hotels, check_swap, book_hotel)


# === CLI ENTRY POINT ===
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crypto Travel Agent CLI")
    parser.add_argument("cmd", nargs="?", default="test",
                        help="Command: test|run|parse|search|swap|book|debug")
    parser.add_argument("--message", "-m", dest="message", default=None,
                        help="Custom user message to use instead of the default test prompt")
    parser.add_argument("--live", action="store_true", help="Enable live API calls (must be explicit)")
    args = parser.parse_args()

    def run_workflow_once(test_input, live=False):
        """Execute the LangGraph workflow and stream output."""
        got_output = False
        try:
            for output in workflow_app.stream(test_input):
                for value in output.values():
                    if "messages" in value and value["messages"]:
                        print("Agent:", value["messages"][-1].content)
                        got_output = True
        except Exception as e:
            print("Streaming error:", type(e).__name__, str(e))

        if not got_output:
            # synchronous fallback
            state = {"messages": test_input["messages"]}
            parsed = parse_intent(state)
            state.update(parsed)
            print("Agent: Parsed ->", f"destination={state.get('destination')}", f"budget=${state.get('budget_usd')}")

            search_res = search_hotels(state, live=live)
            state.update(search_res)
            if search_res.get("messages"):
                print("Agent:", search_res["messages"][-1].content)

            swap_res = check_swap(state)
            state.update(swap_res)
            if swap_res.get("messages"):
                print("Agent:", swap_res["messages"][-1].content)

            book_res = book_hotel(state)
            state.update(book_res)
            if book_res.get("messages"):
                print("Agent:", book_res["messages"][-1].content)

    # Default test input
    default_message = "Book me a hotel in Tokyo under $300 using crypto"
    user_message = args.message if args.message is not None else default_message
    test_input = {"messages": [HumanMessage(content=user_message)]}

    live_flag = args.live
    cmd = args.cmd.lower()

    if cmd == "test":
        print("Crypto Travel Agent (test)\n")
        run_workflow_once(test_input, live=False)
        print("\nAgent ready for Warden Hub! Submit for $10K.")
    elif cmd == "run":
        print("Crypto Travel Agent (run) - live API calls enabled:" , live_flag, "\n")
        run_workflow_once(test_input, live=live_flag)
    elif cmd == "parse":
        print("Parse-only:\n")
        state = {"messages": test_input["messages"]}
        print(parse_intent(state))
    elif cmd == "search":
        print("Search-only:\n")
        state = {"messages": test_input["messages"]}
        state.update(parse_intent(state))
        print(search_hotels(state, live=live_flag))
    elif cmd == "swap":
        print("Swap-only:\n")
        state = {"messages": test_input["messages"]}
        state.update(parse_intent(state))
        state.update(search_hotels(state, live=live_flag))
        print(check_swap(state))
    elif cmd == "book":
        print("Book-only:\n")
        state = {"messages": test_input["messages"]}
        state.update(parse_intent(state))
        state.update(search_hotels(state))
        state.update(check_swap(state))
        print(book_hotel(state))
    elif cmd == "debug":
        print("Debug: printing internal state flow\n")
        run_workflow_once(test_input)
        print("\n-- Done debug --")
    else:
        print(f"Unknown command '{cmd}'. Use one of: test, run, parse, search, swap, book, debug.")

    # === STANDBY MODE (Keeps Render "Live") ===
    import time
    print("\n✅ Agent finished successfully. Entering standby mode to keep Render active...")
    while True:
        time.sleep(600)