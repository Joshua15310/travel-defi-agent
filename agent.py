# agent.py - Complete Travel Agent (Flights + Hotels + Itinerary)
import os
import requests
import time
import operator
import hashlib
import json
import re
from datetime import date, timedelta, datetime
from typing import TypedDict, List, Optional, Annotated

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI

from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

import warden_client

load_dotenv()

# --- CONFIGURATION ---
BOOKING_KEY = os.getenv("BOOKING_API_KEY")
AMADEUS_API_KEY = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")
ONEINCH_API_KEY = os.getenv("ONEINCH_API_KEY")  # For token swaps

# Production Mode Flag
PRODUCTION_MODE = os.getenv("PRODUCTION_MODE", "false").lower() == "true"

LLM_BASE_URL = "https://api.x.ai/v1" 
LLM_API_KEY = os.getenv("GROK_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_MODEL = "grok-3" if os.getenv("GROK_API_KEY") else "gpt-4o-mini"

# Fallback Rates
FX_RATES_FALLBACK = {
    "GBP": 1.27, "EUR": 1.09, "USD": 1.0, "CAD": 0.73, 
    "NGN": 0.00063, "USDC": 1.0, "AUD": 0.66, "JPY": 0.0069
}

# --- GLOBAL CACHE ---
HOTEL_CACHE = {}
FLIGHT_CACHE = {}
AMADEUS_TOKEN_CACHE = {"token": None, "expires_at": 0}
CACHE_TTL = 3600
RATE_CACHE = {}
RATE_CACHE_TTL = 300

# --- AIRPORT CODES (Common ones - expandable) ---
AIRPORT_CODES = {
    "london": "LHR", "paris": "CDG", "new york": "JFK", "los angeles": "LAX",
    "dubai": "DXB", "tokyo": "NRT", "singapore": "SIN", "hong kong": "HKG",
    "sydney": "SYD", "mumbai": "BOM", "delhi": "DEL", "lagos": "LOS",
    "cairo": "CAI", "johannesburg": "JNB", "barcelona": "BCN", "rome": "FCO",
    "amsterdam": "AMS", "frankfurt": "FRA", "toronto": "YYZ", "miami": "MIA",
    "san francisco": "SFO", "chicago": "ORD", "boston": "BOS", "seattle": "SEA"
}

# --- 1. Enhanced State Definition ---
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    
    # Trip Type
    trip_type: str  # "flight_only", "hotel_only", "complete_trip"
    
    # Flight Details
    origin: str
    destination: str
    departure_date: str
    return_date: str  # For round trips
    trip_mode: str  # "one_way" or "round_trip"
    cabin_class: str  # "economy", "business", "first"
    
    # Shared Details
    guests: int
    budget_max: float
    currency: str
    currency_symbol: str
    
    # Flight Selection
    flights: List[dict]
    flight_cursor: int
    selected_flight: dict
    
    # Hotel Details (reuse from before)
    check_in: str
    check_out: str
    rooms: int
    hotels: List[dict]
    hotel_cursor: int
    selected_hotel: dict
    room_options: List[dict]
    
    # Final Selections
    final_flight_price: float
    final_hotel_price: float
    final_room_type: str
    final_total_price_local: float
    final_total_price_usd: float
    
    # Booking State
    requirements_complete: bool
    flight_booked: bool
    hotel_booked: bool
    waiting_for_booking_confirmation: bool
    final_status: str
    
    # Misc
    info_request: str

# --- 2. Structured Output Schema ---
class TravelIntent(BaseModel):
    trip_type: Optional[str] = Field(None, description="flight_only, hotel_only, or complete_trip")
    origin: Optional[str] = Field(None, description="Departure city for flights")
    destination: Optional[str] = Field(None, description="Arrival city")
    departure_date: Optional[str] = Field(None, description="Flight departure date YYYY-MM-DD")
    return_date: Optional[str] = Field(None, description="Return flight date YYYY-MM-DD")
    check_in: Optional[str] = Field(None, description="Hotel check-in YYYY-MM-DD")
    check_out: Optional[str] = Field(None, description="Hotel check-out YYYY-MM-DD")
    guests: Optional[int] = Field(None, description="Number of travelers")
    budget_max: Optional[float] = Field(None, description="Total budget for trip")
    currency: Optional[str] = Field(None, description="Currency code")
    cabin_class: Optional[str] = Field(None, description="economy, business, or first")

# --- 3. Helper Functions ---
def get_llm():
    try:
        return ChatOpenAI(
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL if "grok" in LLM_MODEL.lower() else None,
            temperature=0.7,
            timeout=30
        )
    except:
        return ChatOpenAI(model="gpt-4o-mini", temperature=0.7, timeout=30)

def get_message_text(msg):
    if msg is None: return ""
    content = ""
    if hasattr(msg, 'content'): 
        content = msg.content
    elif isinstance(msg, dict): 
        content = msg.get('content', '')
    else: 
        content = str(msg)
    
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str): 
                parts.append(p)
            elif isinstance(p, dict): 
                parts.append(p.get("text", ""))
            else: 
                parts.append(str(p))
        return " ".join(parts)
    return str(content)

def get_live_rate(base_currency):
    base = base_currency.upper()
    if base in ["USD", "USDC"]: 
        return 1.0
    
    cache_key = f"rate_{base}"
    if cache_key in RATE_CACHE:
        cached = RATE_CACHE[cache_key]
        if time.time() - cached["timestamp"] < RATE_CACHE_TTL:
            return cached["rate"]
    
    rate = None
    try:
        url = f"https://open.exchangerate-api.com/v6/latest/{base}"
        response = requests.get(url, timeout=5)
        data = response.json()
        if data.get("result") == "success" and "USD" in data.get("rates", {}):
            rate = data["rates"]["USD"]
    except:
        pass
    
    if not rate:
        try:
            url = f"https://api.frankfurter.app/latest?from={base}&to=USD"
            response = requests.get(url, timeout=5)
            data = response.json()
            if "rates" in data and "USD" in data["rates"]:
                rate = data["rates"]["USD"]
        except:
            pass
    
    if not rate:
        rate = FX_RATES_FALLBACK.get(base, 1.0)
    
    RATE_CACHE[cache_key] = {"rate": rate, "timestamp": time.time()}
    return rate

def get_airport_code(city_name):
    """Convert city name to IATA airport code"""
    city = city_name.lower().strip()
    return AIRPORT_CODES.get(city, city.upper()[:3])

# --- 4.5. 1inch Swap Functions (For Multi-Currency Payments) ---
def get_1inch_quote(from_token, to_token, amount, chain_id=8453):
    """
    Get swap quote from 1inch API
    chain_id: 8453 = Base Network
    from_token: Token address (e.g., USDC address on Base)
    to_token: Token address (e.g., another stablecoin)
    amount: Amount in smallest unit (wei for most tokens)
    """
    if not ONEINCH_API_KEY:
        print("[1INCH] No API key - swap disabled")
        return None
    
    try:
        url = f"https://api.1inch.dev/swap/v6.0/{chain_id}/quote"
        headers = {
            "Authorization": f"Bearer {ONEINCH_API_KEY}",
            "accept": "application/json"
        }
        params = {
            "src": from_token,
            "dst": to_token,
            "amount": str(amount)
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        data = response.json()
        
        if "dstAmount" in data:
            print(f"[1INCH] Quote: {amount} -> {data['dstAmount']}")
            return data
        else:
            print(f"[1INCH ERROR] {data}")
            return None
    except Exception as e:
        print(f"[1INCH ERROR] {e}")
        return None

def execute_1inch_swap(from_token, to_token, amount, from_address, slippage=1):
    """
    Execute swap on 1inch (only in production mode)
    Returns: transaction data or None
    """
    if not PRODUCTION_MODE:
        print("[1INCH] Test mode - swap skipped")
        return {"status": "mock", "tx_hash": "0xMOCK_SWAP"}
    
    if not ONEINCH_API_KEY:
        print("[1INCH] No API key configured")
        return None
    
    try:
        url = f"https://api.1inch.dev/swap/v6.0/8453/swap"
        headers = {
            "Authorization": f"Bearer {ONEINCH_API_KEY}",
            "accept": "application/json"
        }
        params = {
            "src": from_token,
            "dst": to_token,
            "amount": str(amount),
            "from": from_address,
            "slippage": slippage,
            "disableEstimate": "true"
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=15)
        data = response.json()
        
        if "tx" in data:
            print(f"[1INCH] Swap prepared: {data['tx']}")
            return data
        else:
            print(f"[1INCH ERROR] {data}")
            return None
    except Exception as e:
        print(f"[1INCH ERROR] {e}")
        return None

# Base Network Token Addresses (for reference)
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base
BASE_WETH = "0x4200000000000000000000000000000000000006"  # Wrapped ETH on Base

# --- 4. Amadeus API Functions ---
def get_amadeus_token():
    """Get OAuth token for Amadeus API"""
    global AMADEUS_TOKEN_CACHE
    
    if AMADEUS_TOKEN_CACHE["token"] and time.time() < AMADEUS_TOKEN_CACHE["expires_at"]:
        return AMADEUS_TOKEN_CACHE["token"]
    
    if not AMADEUS_API_KEY or not AMADEUS_API_SECRET:
        print("[AMADEUS] No API credentials - using mock data")
        return None
    
    try:
        # Use PRODUCTION endpoint if in production mode, otherwise TEST
        base_url = "https://api.amadeus.com" if PRODUCTION_MODE else "https://test.api.amadeus.com"
        url = f"{base_url}/v1/security/oauth2/token"
        
        data = {
            "grant_type": "client_credentials",
            "client_id": AMADEUS_API_KEY,
            "client_secret": AMADEUS_API_SECRET
        }
        response = requests.post(url, data=data, timeout=10)
        result = response.json()
        
        token = result.get("access_token")
        expires_in = result.get("expires_in", 1800)
        
        AMADEUS_TOKEN_CACHE["token"] = token
        AMADEUS_TOKEN_CACHE["expires_at"] = time.time() + expires_in - 60
        
        mode = "PRODUCTION" if PRODUCTION_MODE else "TEST"
        print(f"[AMADEUS {mode}] Token obtained, expires in {expires_in}s")
        return token
    except Exception as e:
        print(f"[AMADEUS ERROR] Token fetch failed: {e}")
        return None

def search_flights_amadeus(origin, destination, departure_date, return_date=None, adults=1, cabin="ECONOMY"):
    """Search flights using Amadeus API"""
    cache_key = hashlib.md5(f"{origin}|{destination}|{departure_date}|{return_date}|{adults}".encode()).hexdigest()
    
    if cache_key in FLIGHT_CACHE:
        cached = FLIGHT_CACHE[cache_key]
        if time.time() - cached["timestamp"] < CACHE_TTL:
            print(f"[FLIGHT CACHE HIT] {origin} -> {destination}")
            return cached["data"]
    
    token = get_amadeus_token()
    
    if not token:
        # Mock flight data (only in test mode)
        if not PRODUCTION_MODE:
            print("[MOCK FLIGHTS] Using demo data")
            base_price = 150 if not return_date else 280
            mock_flights = [
                {
                    "id": "MOCK1",
                    "airline": "British Airways",
                    "flight_number": "BA 307",
                    "departure_time": "10:00 AM",
                    "arrival_time": "2:00 PM",
                    "duration": "4h 0m",
                    "price": base_price,
                    "stops": "Direct",
                    "cabin": cabin,
                    "is_mock": True
                },
                {
                    "id": "MOCK2",
                    "airline": "Air France",
                    "flight_number": "AF 1234",
                    "departure_time": "2:30 PM",
                    "arrival_time": "6:45 PM",
                    "duration": "4h 15m",
                    "price": base_price + 50,
                    "stops": "Direct",
                    "cabin": cabin,
                    "is_mock": True
                }
            ]
            return mock_flights
        else:
            print("[PRODUCTION ERROR] Cannot proceed without Amadeus credentials")
            return []
    
    try:
        # Use production or test endpoint based on mode
        base_url = "https://api.amadeus.com" if PRODUCTION_MODE else "https://test.api.amadeus.com"
        url = f"{base_url}/v2/shopping/flight-offers"
        
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "originLocationCode": get_airport_code(origin),
            "destinationLocationCode": get_airport_code(destination),
            "departureDate": departure_date,
            "adults": str(adults),
            "travelClass": cabin,
            "max": 10,
            "currencyCode": "USD"
        }
        
        if return_date:
            params["returnDate"] = return_date
        
        response = requests.get(url, headers=headers, params=params, timeout=20)
        data = response.json()
        
        if "data" not in data:
            print(f"[AMADEUS] No flights found")
            return []
        
        flights = []
        for offer in data["data"][:10]:
            try:
                itinerary = offer["itineraries"][0]
                segment = itinerary["segments"][0]
                
                flight_info = {
                    "id": offer["id"],
                    "airline": segment["carrierCode"],
                    "flight_number": f"{segment['carrierCode']} {segment['number']}",
                    "departure_time": segment["departure"]["at"].split("T")[1][:5],
                    "arrival_time": segment["arrival"]["at"].split("T")[1][:5],
                    "duration": itinerary["duration"].replace("PT", "").lower(),
                    "price": float(offer["price"]["total"]),
                    "stops": "Direct" if len(itinerary["segments"]) == 1 else f"{len(itinerary['segments'])-1} stop(s)",
                    "cabin": cabin,
                    "is_mock": False,
                    "booking_token": offer.get("id")  # Real booking token for production
                }
                flights.append(flight_info)
            except:
                continue
        
        FLIGHT_CACHE[cache_key] = {"timestamp": time.time(), "data": flights}
        return flights
        
    except Exception as e:
        print(f"[AMADEUS ERROR] Flight search failed: {e}")
        return []

# --- 5. Node: Intent Parser ---
def parse_intent(state: AgentState):
    messages = state.get("messages", [])
    if not messages: 
        return {}
    
    last_msg = get_message_text(messages[-1]).lower()
    
    # RESET
    if "start over" in last_msg or "reset" in last_msg or "new search" in last_msg:
        return {
            "trip_type": None, "origin": None, "destination": None,
            "departure_date": None, "return_date": None, "check_in": None,
            "check_out": None, "guests": None, "budget_max": None,
            "currency": "USD", "currency_symbol": "$",
            "flights": [], "flight_cursor": 0, "selected_flight": None,
            "hotels": [], "hotel_cursor": 0, "selected_hotel": None,
            "room_options": [], "requirements_complete": False,
            "flight_booked": False, "hotel_booked": False,
            "waiting_for_booking_confirmation": False, "info_request": None,
            "messages": [AIMessage(content="🔄 **Reset complete!** Let's start fresh.\n\nWhat would you like to book?\n• ✈️ **Flight only**\n• 🏨 **Hotel only**\n• 🌍 **Complete trip** (flight + hotel)")]
        }

    # INFO REQUEST
    if any(phrase in last_msg for phrase in ["tell me about", "what is", "info on", "describe", "more info"]):
        match = re.search(r"\b(\d+)\b", last_msg)
        if match:
            idx = int(match.group(1)) - 1
            if state.get("flights") and not state.get("selected_flight"):
                if 0 <= idx < len(state["flights"]):
                    flight = state["flights"][idx]
                    return {"info_request": f"Tell me about flight {flight['flight_number']}"}
            elif state.get("hotels") and not state.get("selected_hotel"):
                if 0 <= idx < len(state["hotels"]):
                    hotel = state["hotels"][idx]
                    return {"info_request": f"Tell me about {hotel['name']}"}
        return {"info_request": last_msg}

    # PAGINATION
    if any(w in last_msg for w in ["more", "next", "other", "show more"]):
        if state.get("flights") and not state.get("selected_flight"):
            return {
                "flight_cursor": state.get("flight_cursor", 0) + 5,
                "flights": []
            }
        elif state.get("hotels") and not state.get("selected_hotel"):
            return {
                "hotel_cursor": state.get("hotel_cursor", 0) + 5,
                "hotels": []
            }

    # CONFIRMATION
    if state.get("waiting_for_booking_confirmation"):
        if any(w in last_msg for w in ["yes", "proceed", "confirm", "book it", "pay", "ok"]):
            return {}
        return {
            "waiting_for_booking_confirmation": False,
            "messages": [AIMessage(content="No problem! What would you like to change?")]
        }

    # EXTRACT INTENT
    today_str = date.today().strftime("%Y-%m-%d")
    
    llm = get_llm()
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"""You are a travel assistant. Today is {today_str}.

Detect trip type:
- "flight" or "fly" or "plane" → flight_only
- "hotel" or "accommodation" or "stay" → hotel_only  
- "trip" or "vacation" or "travel" or both flight+hotel mentioned → complete_trip

Extract:
- origin: Departure city (for flights)
- destination: Arrival city
- departure_date/return_date: Flight dates
- check_in/check_out: Hotel dates
- guests: Number of travelers
- budget_max: Total budget
- currency: USD, GBP, EUR, etc.
- cabin_class: economy (default), business, or first

Examples:
- "Fly from London to Paris tomorrow" → trip_type=flight_only, origin=London, destination=Paris
- "Book hotel in Tokyo" → trip_type=hotel_only, destination=Tokyo
- "Plan trip from NYC to Dubai next week" → trip_type=complete_trip, origin=NYC, destination=Dubai"""
    
    intent_data = {}
    try:
        intent = structured_llm.invoke([SystemMessage(content=system_prompt)] + messages[-3:])
        
        if intent.trip_type: 
            intent_data["trip_type"] = intent.trip_type
        if intent.origin: 
            intent_data["origin"] = intent.origin.title()
        if intent.destination: 
            intent_data["destination"] = intent.destination.title()
        if intent.departure_date: 
            intent_data["departure_date"] = intent.departure_date
        if intent.return_date: 
            intent_data["return_date"] = intent.return_date
            intent_data["trip_mode"] = "round_trip"
        elif intent.departure_date:
            intent_data["trip_mode"] = "one_way"
        if intent.check_in: 
            intent_data["check_in"] = intent.check_in
        if intent.check_out: 
            intent_data["check_out"] = intent.check_out
        if intent.guests: 
            intent_data["guests"] = intent.guests
        if intent.budget_max: 
            intent_data["budget_max"] = intent.budget_max
        if intent.currency:
            curr = intent.currency.upper()
            intent_data["currency"] = curr
            symbols = {"USD": "$", "GBP": "£", "EUR": "€", "NGN": "₦", "CAD": "C$", "AUD": "A$", "JPY": "¥"}
            intent_data["currency_symbol"] = symbols.get(curr, "$")
        if intent.cabin_class:
            intent_data["cabin_class"] = intent.cabin_class.lower()
    except Exception as e:
        print(f"[INTENT ERROR] {e}")

    # SELECTION
    if last_msg.strip().isdigit():
        idx = int(last_msg.strip()) - 1
        
        # Select flight
        if state.get("flights") and not state.get("selected_flight"):
            if 0 <= idx < len(state["flights"]):
                return {"selected_flight": state["flights"][idx]}
        
        # Select hotel
        elif state.get("hotels") and not state.get("selected_hotel"):
            if 0 <= idx < len(state["hotels"]):
                return {"selected_hotel": state["hotels"][idx]}

    # AUTO-DATES
    if intent_data.get("departure_date") and not intent_data.get("return_date") and state.get("trip_type") == "complete_trip":
        try:
            dep = datetime.strptime(intent_data["departure_date"], "%Y-%m-%d")
            intent_data["return_date"] = (dep + timedelta(days=7)).strftime("%Y-%m-%d")
        except:
            pass

    if intent_data.get("departure_date") and not intent_data.get("check_in"):
        intent_data["check_in"] = intent_data["departure_date"]
    
    if intent_data.get("return_date") and not intent_data.get("check_out"):
        intent_data["check_out"] = intent_data["return_date"]

    return intent_data

# --- 6. Node: Requirements Gatherer ---
def gather_requirements(state: AgentState):
    trip_type = state.get("trip_type")
    
    if not trip_type:
        return {
            "requirements_complete": False,
            "messages": [AIMessage(content="👋 **Welcome to Warden Travel!**\n\nWhat would you like to book today?\n\n✈️ **Flight only** - Just book a flight\n🏨 **Hotel only** - Just book accommodation\n🌍 **Complete trip** - Flight + Hotel package\n\nExample: 'Book a complete trip from London to Paris'")]
        }
    
    missing = []
    
    # Check flight requirements
    if trip_type in ["flight_only", "complete_trip"]:
        if not state.get("origin"): 
            missing.append("Departure City")
        if not state.get("departure_date"): 
            missing.append("Departure Date")
    
    # Check hotel requirements
    if trip_type in ["hotel_only", "complete_trip"]:
        if not state.get("check_in"): 
            missing.append("Check-in Date")
    
    # Common requirements
    if not state.get("destination"): 
        missing.append("Destination")
    if not state.get("guests"): 
        missing.append("Number of Travelers")
    if state.get("budget_max") is None: 
        missing.append("Budget")

    if not missing:
        if not state.get("currency"):
            return {
                "requirements_complete": True,
                "currency": "USD",
                "currency_symbol": "$",
                "cabin_class": state.get("cabin_class", "economy"),
                "rooms": state.get("guests", 2)
            }
        return {"requirements_complete": True}

    # Ask for missing
    llm = get_llm()
    
    context = []
    if state.get("trip_type"): 
        context.append(f"Type: {state['trip_type'].replace('_', ' ').title()}")
    if state.get("origin"): 
        context.append(f"From: {state['origin']}")
    if state.get("destination"): 
        context.append(f"To: {state['destination']}")
    if state.get("departure_date"): 
        context.append(f"Departure: {state['departure_date']}")
    if state.get("guests"): 
        context.append(f"Travelers: {state['guests']}")
    if state.get("budget_max"):
        sym = state.get("currency_symbol", "$")
        context.append(f"Budget: {sym}{state['budget_max']}")
    
    context_str = "\n".join(context) if context else "Starting fresh"
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", f"""You are a friendly travel assistant.

Current details:
{context_str}

Ask for ONLY the first missing item: {', '.join(missing)}

Be warm, brief (1-2 sentences). Provide examples.
If asking for budget, mention currency options (e.g. "400 pounds", "300 euros")."""),
        MessagesPlaceholder(variable_name="messages"),
    ])
    
    chain = prompt | llm
    try:
        response = chain.invoke({"messages": state.get("messages", [])})
        return {
            "requirements_complete": False,
            "messages": [response]
        }
    except:
        # Fallback
        if "Departure City" in missing:
            msg = f"✈️ Which city are you flying **from**? (e.g. London, New York, Dubai)"
        elif "Destination" in missing:
            msg = f"🌍 Where would you like to go?"
        elif "Departure Date" in missing:
            msg = f"📅 When would you like to depart? (e.g. 2026-02-15, tomorrow, next Friday)"
        elif "Check-in Date" in missing:
            msg = f"📅 When would you like to check in? (e.g. 2026-02-15)"
        elif "Number of Travelers" in missing:
            msg = f"👥 How many travelers? (e.g. 1, 2, 4)"
        else:
            msg = f"💰 What's your total budget for this trip? (e.g. '500 dollars', '400 pounds', '1000 euros')"
        
        return {
            "requirements_complete": False,
            "messages": [AIMessage(content=msg)]
        }

# --- 7. Node: Search Flights ---
def search_flights(state: AgentState):
    if not state.get("requirements_complete"):
        return {}
    if state.get("trip_type") not in ["flight_only", "complete_trip"]:
        return {}
    if state.get("selected_flight"):
        return {}
    
    origin = state.get("origin")
    destination = state.get("destination")
    departure_date = state.get("departure_date")
    return_date = state.get("return_date")
    guests = state.get("guests", 1)
    cabin = state.get("cabin_class", "economy").upper()
    currency = state.get("currency", "USD")
    symbol = state.get("currency_symbol", "$")
    cursor = state.get("flight_cursor", 0)
    
    print(f"[FLIGHT SEARCH] {origin} -> {destination} on {departure_date}")
    
    flights = search_flights_amadeus(
        origin, destination, departure_date,
        return_date, guests, cabin
    )
    
    if not flights:
        return {
            "messages": [AIMessage(content=f"😔 No flights found from **{origin}** to **{destination}** on {departure_date}. Try different dates?")]
        }
    
    # Convert to local currency
    rate = get_live_rate(currency)
    for flight in flights:
        flight["price_local"] = round(flight["price"] / rate, 2)
    
    # Filter by budget if set
    budget = state.get("budget_max")
    if budget:
        flights = [f for f in flights if f["price_local"] <= budget]
    
    if not flights:
        return {
            "messages": [AIMessage(content=f"😔 No flights within your budget of {symbol}{budget}. Try increasing your budget?")]
        }
    
    # Pagination
    batch = flights[cursor:cursor + 5]
    
    if not batch:
        return {
            "flight_cursor": 0,
            "messages": [AIMessage(content="That's all the flights! Say **'start over'** to search again.")]
        }
    
    # Format message
    trip_mode = "Round trip" if return_date else "One way"
    options = "\n\n".join([
        f"**{i+1}. {f['airline']} {f['flight_number']}** ✈️ {symbol}{f['price_local']}\n   🕐 {f['departure_time']} - {f['arrival_time']} | ⏱️ {f['duration']} | {f['stops']}"
        for i, f in enumerate(batch)
    ])
    
    msg = f"✈️ **Flights from {origin} to {destination}** ({trip_mode})\n📅 {departure_date}{' - ' + return_date if return_date else ''}\n\n{options}\n\n📝 Reply with the **number** to select (e.g. '1'), or say **'next'** for more."
    
    return {
        "flights": batch,
        "messages": [AIMessage(content=msg)]
    }

# --- 8. Node: Search Hotels (from existing code) ---
def search_hotels(state: AgentState):
    if not state.get("requirements_complete"):
        return {}
    if state.get("trip_type") == "flight_only":
        return {}
    if state.get("selected_hotel"):
        return {}
    
    # If complete_trip, only search after flight is selected
    if state.get("trip_type") == "complete_trip" and not state.get("selected_flight"):
        return {}
    
    destination = state.get("destination")
    check_in = state.get("check_in")
    check_out = state.get("check_out")
    guests = state.get("guests", 2)
    rooms = state.get("rooms", 1)
    currency = state.get("currency", "USD")
    symbol = state.get("currency_symbol", "$")
    cursor = state.get("hotel_cursor", 0)
    
    if not BOOKING_KEY:
        return {
            "messages": [AIMessage(content="❌ Hotel search unavailable. Please configure BOOKING_API_KEY.")]
        }
    
    # Calculate nights
    try:
        d1 = datetime.strptime(check_in, "%Y-%m-%d")
        d2 = datetime.strptime(check_out, "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except:
        nights = 2
    
    # Fetch hotels (using existing cache logic)
    cache_key = hashlib.md5(f"{destination}|{check_in}|{guests}|{currency}".encode()).hexdigest()
    
    if cache_key in HOTEL_CACHE:
        cached = HOTEL_CACHE[cache_key]
        if time.time() - cached["timestamp"] < CACHE_TTL:
            raw_data = cached["data"]
        else:
            raw_data = []
    else:
        raw_data = []
    
    if not raw_data:
        try:
            headers = {
                "X-RapidAPI-Key": BOOKING_KEY,
                "X-RapidAPI-Host": "booking-com.p.rapidapi.com"
            }
            
            # Get destination ID
            r = requests.get(
                "https://booking-com.p.rapidapi.com/v1/hotels/locations",
                headers=headers,
                params={"name": destination, "locale": "en-us"},
                timeout=10
            )
            data = r.json()
            
            if not data:
                return {
                    "messages": [AIMessage(content=f"😔 Couldn't find hotels in **{destination}**.")]
                }
            
            dest_id = data[0].get("dest_id")
            dest_type = data[0].get("dest_type", "city")
            
            # Search hotels
            params = {
                "dest_id": str(dest_id),
                "dest_type": dest_type,
                "checkin_date": check_in,
                "checkout_date": check_out,
                "adults_number": str(guests),
                "room_number": str(rooms),
                "units": "metric",
                "filter_by_currency": currency,
                "order_by": "price",
                "locale": "en-us"
            }
            
            res = requests.get(
                "https://booking-com.p.rapidapi.com/v1/hotels/search",
                headers=headers,
                params=params,
                timeout=20
            )
            
            raw_data = res.json().get("result", [])[:50]
            HOTEL_CACHE[cache_key] = {"timestamp": time.time(), "data": raw_data}
        except Exception as e:
            print(f"[HOTEL ERROR] {e}")
            return {
                "messages": [AIMessage(content=f"😔 Hotel search failed. Please try again.")]
            }
    
    # Process hotels
    all_hotels = []
    for h in raw_data:
        try:
            total = float(h.get("min_total_price", 0))
            if total == 0:
                continue
            
            price_per_night = round(total / nights, 2)
            stars = int(h.get("class", 0))
            rating = h.get("review_score", 0) or 0
            
            star_display = "⭐" * stars if stars > 0 else f"Rating: {rating}/10"
            
            all_hotels.append({
                "name": h.get("hotel_name", "Hotel"),
                "price": price_per_night,
                "total": total,
                "rating_str": star_display,
                "stars": stars
            })
        except:
            continue
    
    # Filter by remaining budget (if complete trip)
    if state.get("trip_type") == "complete_trip" and state.get("selected_flight"):
        flight_cost = state["selected_flight"]["price_local"]
        budget = state.get("budget_max", 10000)
        remaining_budget = budget - flight_cost
        all_hotels = [h for h in all_hotels if h["price"] <= remaining_budget]
    
    if not all_hotels:
        return {
            "messages": [AIMessage(content=f"😔 No hotels available within your budget.")]
        }
    
    # Sort
    budget = state.get("budget_max", 10000)
    if budget > 200:
        all_hotels.sort(key=lambda x: (x["stars"], -x["price"]), reverse=True)
        msg_intro = f"✨ **Top Hotels** in {destination} (Best First):"
    else:
        all_hotels.sort(key=lambda x: x["price"])
        msg_intro = f"💰 **Best Value Hotels** in {destination} (Cheapest First):"
    
    # Pagination
    batch = all_hotels[cursor:cursor + 5]
    
    if not batch:
        return {
            "hotel_cursor": 0,
            "messages": [AIMessage(content="That's all the hotels! Say **'start over'** to search again.")]
        }
    
    # Format message
    options = "\n\n".join([
        f"**{i+1}. {h['name']}** 💵 {symbol}{h['price']}/night | {h['rating_str']}"
        for i, h in enumerate(batch)
    ])
    
    msg = f"{msg_intro}\n\n{options}\n\n📝 Reply with the **number** to select, or say **'next'** for more."
    
    return {
        "hotels": batch,
        "messages": [AIMessage(content=msg)]
    }

# --- 9. Node: Select Room ---
def select_room(state: AgentState):
    if state.get("selected_hotel") and not state.get("room_options"):
        hotel = state["selected_hotel"]
        sym = state.get("currency_symbol", "$")
        
        room_options = [
            {"type": "Standard Room", "price": hotel["price"]},
            {"type": "Deluxe Suite", "price": round(hotel["price"] * 1.4, 2)}
        ]
        
        rooms_msg = f"""🏨 **{hotel['name']}** - Great choice!

Please select a room type:

**1. Standard Room**
   💵 {sym}{room_options[0]['price']}/night
   
**2. Deluxe Suite**
   💵 {sym}{room_options[1]['price']}/night
   ✨ Upgraded amenities

Reply with **'1'** or **'2'**"""
        
        return {
            "room_options": room_options,
            "messages": [AIMessage(content=rooms_msg)]
        }
    
    last_msg = get_message_text(state["messages"][-1]).lower()
    options = state.get("room_options", [])
    
    if not options:
        return {}
    
    selected_room = None
    if last_msg.strip() in ["1", "one", "standard"]:
        selected_room = options[0]
    elif last_msg.strip() in ["2", "two", "deluxe", "suite"]:
        selected_room = options[1]
    
    if not selected_room:
        return {
            "messages": [AIMessage(content="⚠️ Please reply with **'1'** for Standard or **'2'** for Deluxe Suite.")]
        }
    
    # Calculate totals
    try:
        d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
        d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except:
        nights = 2
    
    hotel_total_local = selected_room["price"] * nights
    currency = state.get("currency", "USD")
    sym = state.get("currency_symbol", "$")
    
    # Fetch live rate
    rate = get_live_rate(currency)
    hotel_total_usd = round(hotel_total_local * rate, 2)
    
    # Calculate grand total
    flight_total_local = 0
    flight_total_usd = 0
    
    if state.get("selected_flight"):
        flight_total_local = state["selected_flight"]["price_local"]
        flight_total_usd = round(flight_total_local * rate, 2)
    
    grand_total_local = hotel_total_local + flight_total_local
    grand_total_usd = hotel_total_usd + flight_total_usd
    
    # Build summary
    if currency in ["USD", "USDC"]:
        rate_info = "💵 Payment in **USDC** (1:1 with USD)"
    else:
        rate_info = f"""💱 **Live Exchange Rate**
1 {currency} = {rate:.4f} USD/USDC
_(Rate updated {time.strftime('%H:%M UTC')})_"""
    
    summary_parts = ["📋 **Complete Trip Summary**\n"]
    
    if state.get("selected_flight"):
        f = state["selected_flight"]
        summary_parts.append(f"""✈️ **Flight**
• {f['airline']} {f['flight_number']}
• {state['origin']} → {state['destination']}
• {state['departure_date']} at {f['departure_time']}
• Duration: {f['duration']} | {f['stops']}
• Cost: {sym}{flight_total_local:.2f} {currency}
""")
    
    summary_parts.append(f"""🏨 **Hotel**
• {state['selected_hotel']['name']}
• {selected_room['type']}
• {state['check_in']} to {state['check_out']} ({nights} night{'s' if nights != 1 else ''})
• {sym}{selected_room['price']}/night × {nights} = **{sym}{hotel_total_local:.2f} {currency}**

👥 **Guests:** {state.get('guests', 2)}

💰 **Total Cost:** {sym}{grand_total_local:.2f} {currency}

{rate_info}

💳 **Total Payment: {grand_total_usd:.2f} USDC** on Base Network

---

✅ Reply **'yes'** or **'confirm'** to complete booking
🔄 Say **'change'** or **'start over'** to modify""")
    
    summary_msg = "\n".join(summary_parts)
    
    return {
        "final_room_type": selected_room["type"],
        "final_hotel_price": hotel_total_local,
        "final_flight_price": flight_total_local,
        "final_total_price_local": grand_total_local,
        "final_total_price_usd": grand_total_usd,
        "waiting_for_booking_confirmation": True,
        "messages": [AIMessage(content=summary_msg)]
    }

# --- 10. Node: Book Complete Trip ---
def book_trip(state: AgentState):
    if not state.get("waiting_for_booking_confirmation"):
        return {}
    
    currency = state.get("currency", "USD")
    sym = state.get("currency_symbol", "$")
    total_usd = state["final_total_price_usd"]
    total_local = state["final_total_price_local"]
    
    # Build booking details
    booking_items = []
    
    if state.get("selected_flight"):
        f = state["selected_flight"]
        booking_items.append(f"Flight: {f['airline']} {f['flight_number']} ({state['origin']}->{state['destination']})")
    
    if state.get("selected_hotel"):
        h = state["selected_hotel"]
        booking_items.append(f"Hotel: {h['name']} - {state['final_room_type']}")
    
    booking_description = " | ".join(booking_items) + " [Base/USDC]"
    
    try:
        result = warden_client.submit_booking(
            hotel_name=booking_description,
            hotel_price=total_usd,
            destination=state["destination"],
            swap_amount=0.0
        )
        
        tx_hash = result.get("tx_hash", "0xMOCK")
        booking_ref = result.get("booking_ref", f"WRD-{tx_hash[-8:].upper()}")
        
        # Generate unique ticket/confirmation numbers
        import random
        
        # In production, use real booking references
        if PRODUCTION_MODE and state.get("selected_flight") and not state["selected_flight"].get("is_mock"):
            # Real flight booking would return actual PNR/ticket number
            flight_ticket_number = f"TKT-{state['selected_flight'].get('booking_token', '')[:6].upper()}"
        else:
            flight_ticket_number = f"TKT-{random.randint(100000, 999999)}" if state.get("selected_flight") else None
        
        hotel_confirmation = f"HTL-{random.randint(100000, 999999)}" if state.get("selected_hotel") else None
        
        # Build confirmation message with detailed booking info
        confirmation_parts = ["🎉 **BOOKING CONFIRMED!**\n\n✅ Your trip is booked! Save these details:\n"]
        
        confirmation_parts.append(f"📋 **Master Booking Reference:** `{booking_ref}`\n")
        
        if state.get("selected_flight"):
            f = state["selected_flight"]
            confirmation_parts.append(f"""---
✈️ **FLIGHT BOOKING DETAILS**

🎫 **E-Ticket Number:** `{flight_ticket_number}`
📍 **Confirmation Code:** `{booking_ref[:6]}`

**Flight Information:**
• Airline: {f['airline']} Flight {f['flight_number']}
• Route: {state['origin']} → {state['destination']}
• Date: {state['departure_date']}
• Departure Time: {f['departure_time']}
• Arrival Time: {f['arrival_time']}
• Duration: {f['duration']}
• Cabin Class: {state.get('cabin_class', 'Economy').title()}
• Baggage: 1 checked bag included
• Passengers: {state.get('guests', 2)} traveler(s)

💵 Flight Cost: {sym}{state.get('final_flight_price', 0):.2f} {currency}

**Airport Check-in:**
🕐 Arrive 2-3 hours before departure
📱 Show this ticket number: `{flight_ticket_number}`
🆔 Bring valid ID/Passport
""")
        
        if state.get("selected_hotel"):
            h = state["selected_hotel"]
            try:
                d1 = datetime.strptime(state['check_in'], "%Y-%m-%d")
                d2 = datetime.strptime(state['check_out'], "%Y-%m-%d")
                nights = max(1, (d2 - d1).days)
            except:
                nights = 2
            
            confirmation_parts.append(f"""---
🏨 **HOTEL BOOKING DETAILS**

🔑 **Confirmation Number:** `{hotel_confirmation}`
📧 **Reservation Code:** `{booking_ref[-6:]}`

**Hotel Information:**
• Property: {h['name']}
• Room Type: {state['final_room_type']}
• Check-in: {state['check_in']} (After 3:00 PM)
• Check-out: {state['check_out']} (Before 11:00 AM)
• Duration: {nights} night(s)
• Guests: {state.get('guests', 2)} guest(s)

💵 Hotel Cost: {sym}{state.get('final_hotel_price', 0):.2f} {currency}

**Hotel Check-in Instructions:**
🕒 Check-in after 3:00 PM
📱 Show confirmation: `{hotel_confirmation}`
🆔 Bring valid ID/Credit card
💳 Deposit may be required
""")
        
        confirmation_parts.append(f"""---
💳 **PAYMENT SUMMARY**

Total Paid: **{total_usd:.2f} USDC** (Base Network)
Equivalent: {sym}{total_local:.2f} {currency}

🔗 **Blockchain Proof:**
[View Transaction on BaseScan](https://sepolia.basescan.org/tx/{tx_hash})

---

📧 **Important Reminders:**
• Screenshot or save these booking numbers
• Check-in online 24 hours before flight
• Arrive at airport 2-3 hours early
• Bring valid ID and payment method
• Contact hotel directly for special requests

🆘 **Need Help?**
Having issues? Quote your booking ref: `{booking_ref}`

---

🌟 **Thank you for booking with Warden Travel!**

Ready for another trip? Say **'start over'** or **'new search'**

Safe travels! ✈️🏨""")
        
        confirmation_msg = "\n".join(confirmation_parts)
        
        return {
            "final_status": "Booked",
            "flight_booked": True,
            "hotel_booked": True,
            "waiting_for_booking_confirmation": False,
            "messages": [AIMessage(content=confirmation_msg)]
        }
        
    except Exception as e:
        print(f"[BOOKING ERROR] {e}")
        return {
            "final_status": "Failed",
            "waiting_for_booking_confirmation": False,
            "messages": [AIMessage(content=f"❌ Booking failed: {str(e)}\n\nPlease try again or contact support.")]
        }

# --- 11. Node: Consultant ---
def consultant_node(state: AgentState):
    query = state.get("info_request")
    if not query:
        return {}
    
    context = ""
    if state.get("flights"):
        context = f"User viewing flights: {[f'{f['airline']} {f['flight_number']}' for f in state['flights'][:5]]}"
    elif state.get("hotels"):
        context = f"User viewing hotels: {[h['name'] for h in state['hotels'][:5]]}"
    
    prompt = f"""User question: "{query}"
Context: {context}

Provide helpful information. Be enthusiastic but honest.
End by asking if they want to book (reply with number) or see more options.
Keep under 100 words."""
    
    try:
        response = get_llm().invoke(prompt)
        return {
            "info_request": None,
            "messages": [response]
        }
    except:
        return {
            "info_request": None,
            "messages": [AIMessage(content="I'd be happy to help! Could you rephrase your question?")]
        }

# --- 12. Routing Logic ---
def route_step(state):
    if state.get("info_request"):
        return "consultant"
    
    if not state.get("requirements_complete"):
        return "end"
    
    trip_type = state.get("trip_type")
    
    # FLIGHT ONLY FLOW
    if trip_type == "flight_only":
        if not state.get("selected_flight"):
            if not state.get("flights"):
                return "search_flights"
            return "end"
        if state.get("waiting_for_booking_confirmation"):
            last_msg = get_message_text(state["messages"][-1]).lower()
            if any(w in last_msg for w in ["yes", "confirm", "proceed", "book", "ok"]):
                return "book"
            return "end"
        return "book"
    
    # HOTEL ONLY FLOW
    elif trip_type == "hotel_only":
        if not state.get("hotels"):
            return "search_hotels"
        if not state.get("selected_hotel"):
            return "end"
        if not state.get("final_room_type"):
            return "select_room"
        if state.get("waiting_for_booking_confirmation"):
            last_msg = get_message_text(state["messages"][-1]).lower()
            if any(w in last_msg for w in ["yes", "confirm", "proceed", "book", "ok"]):
                return "book"
            return "end"
        return "end"
    
    # COMPLETE TRIP FLOW
    elif trip_type == "complete_trip":
        if not state.get("selected_flight"):
            if not state.get("flights"):
                return "search_flights"
            return "end"
        if not state.get("hotels"):
            return "search_hotels"
        if not state.get("selected_hotel"):
            return "end"
        if not state.get("final_room_type"):
            return "select_room"
        if state.get("waiting_for_booking_confirmation"):
            last_msg = get_message_text(state["messages"][-1]).lower()
            if any(w in last_msg for w in ["yes", "confirm", "proceed", "book", "ok"]):
                return "book"
            return "end"
        return "end"
    
    return "end"

# --- 13. Build Workflow ---
workflow = StateGraph(AgentState)

workflow.add_node("parse", parse_intent)
workflow.add_node("gather", gather_requirements)
workflow.add_node("search_flights", search_flights)
workflow.add_node("search_hotels", search_hotels)
workflow.add_node("select_room", select_room)
workflow.add_node("book", book_trip)
workflow.add_node("consultant", consultant_node)

workflow.set_entry_point("parse")
workflow.add_edge("parse", "gather")

workflow.add_conditional_edges(
    "gather",
    route_step,
    {
        "end": END,
        "search_flights": "search_flights",
        "search_hotels": "search_hotels",
        "select_room": "select_room",
        "book": "book",
        "consultant": "consultant"
    }
)

workflow.add_edge("search_flights", END)
workflow.add_edge("search_hotels", END)
workflow.add_edge("select_room", END)
workflow.add_edge("book", END)
workflow.add_edge("consultant", END)

memory = MemorySaver()
workflow_app = workflow.compile(checkpointer=memory)