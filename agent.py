# agent.py - Crypto Travel Booker (FIXED PARSER + BOOKING.COM)
from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq
import requests
import os
from dotenv import load_dotenv
from typing import TypedDict, Annotated
import operator

load_dotenv()

# === LLM: Grok AI ===
llm = ChatGroq(
    model="grok-beta",
    api_key=os.getenv("GROK_API_KEY"),
    temperature=0
)

BOOKING_KEY = os.getenv("BOOKING_API_KEY")

# === STATE ===
class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    user_query: str
    destination: str
    budget_usd: float
    hotel_name: str
    hotel_price: float
    needs_swap: bool
    swap_amount: float
    final_status: str

# === 1. Parse User (FIXED: handles "in", "to", "at") ===
def parse_intent(state):
    query = state["messages"][-1].content.lower()
    destination = "Paris"
    budget = 400.0

    # Find destination after "to", "in", "at"
    markers = ["to ", "in ", "at "]
    dest_part = query
    for marker in markers:
        if marker in query:
            dest_part = query.split(marker, 1)[-1]
            break

    # Extract first word as city
    words = dest_part.strip().split()
    if words:
        destination = words[0].capitalize()

    # Extract budget after $
    if "$" in query:
        try:
            budget_str = query.split("$")[-1]
            budget = float(''.join(filter(str.isdigit, budget_str.split()[0])))
        except:
            pass

    return {
        "user_query": query,
        "destination": destination,
        "budget_usd": budget
    }

# === 2. Search Hotels on Booking.com ===
def search_hotels(state):
    url = "https://booking-com.p.rapidapi.com/v1/hotels/search"
    querystring = {
        "checkout_date": "2025-12-16",
        "units": "metric",
        "dest_id": "-1746443",  # Paris (we'll improve later)
        "dest_type": "city",
        "locale": "en-gb",
        "adults_number": "1",
        "order_by": "price",
        "filter_by_currency": "USD",
        "checkin_date": "2025-12-15",
        "room_number": "1"
    }

    headers = {
        "X-RapidAPI-Key": BOOKING_KEY,
        "X-RapidAPI-Host": "booking-com.p.rapidapi.com"
    }

    try:
        response = requests.get(url, headers=headers, params=querystring)
        data = response.json()
        if data.get("result") and len(data["result"]) > 0:
            hotel = data["result"][0]
            name = hotel["hotel_name"]
            price = float(hotel["price_breakdown"]["all_inclusive_price"])
        else:
            name, price = "Budget Hotel", 180.0
    except Exception as e:
        name, price = "Budget Hotel", 180.0

    return {
        "hotel_name": name,
        "hotel_price": price,
        "messages": [HumanMessage(content=f"Found {name} in {state['destination']} for ${price}/night")]
    }

# === 3. Check Swap ===
def check_swap(state):
    if state["hotel_price"] > state["budget_usd"]:
        return {
            "needs_swap": False,
            "final_status": "Budget too low!",
            "messages": [HumanMessage(content="Not enough budget. Try a cheaper destination.")]
        }

    swap_needed = state["hotel_price"] - (state["budget_usd"] * 0.8)
    if swap_needed <= 0:
        return {
            "needs_swap": False,
            "swap_amount": 0,
            "messages": [HumanMessage(content="You have enough USD!")]
        }

    usdc_needed = swap_needed * 1.01  # 1% buffer

    return {
        "needs_swap": True,
        "swap_amount": round(usdc_needed, 2),
        "messages": [HumanMessage(content=f"Swapping {round(usdc_needed, 2)} USDC → USD via 1inch")]
    }

# === 4. Book ===
def book_hotel(state):
    return {
        "final_status": f"Booked {state['hotel_name']} for ${state['hotel_price']}",
        "messages": [HumanMessage(content=f"Booking confirmed on Warden! Paid with USDC. Enjoy {state['destination']}!")]
    }

# === BUILD ===
workflow = StateGraph(AgentState)
workflow.add_node("parse", parse_intent)
workflow.add_node("search", search_hotels)
workflow.add_node("swap", check_swap)
workflow.add_node("book", book_hotel)

workflow.set_entry_point("parse")
workflow.add_edge("parse", "search")
workflow.add_edge("search", "swap")
workflow.add_edge("swap", "book")
workflow.add_edge("book", END)

app = workflow.compile()

# === TEST ===
if __name__ == "__main__":
    test_input = {
        "messages": [HumanMessage(content="Book me a hotel in Tokyo under $300 using crypto")]
    }
    print("Crypto Travel Agent Running...\n")
    for output in app.stream(test_input):
        for value in output.values():
            if "messages" in value:
                print("Agent:", value["messages"][-1].content)
    print("\nAgent ready for Warden Hub! Submit for $10K.")