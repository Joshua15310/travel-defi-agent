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

# Email confirmation imports
try:
    import sib_api_v3_sdk
    from sib_api_v3_sdk.rest import ApiException as BrevoApiException
    BREVO_AVAILABLE = True
except ImportError:
    BREVO_AVAILABLE = False
    print("[WARNING] Brevo/Sendinblue SDK not available. Email confirmations disabled.")

load_dotenv()

# --- CONFIGURATION ---
BOOKING_KEY = os.getenv("BOOKING_API_KEY")
AMADEUS_API_KEY = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")
ONEINCH_API_KEY = os.getenv("ONEINCH_API_KEY")  # For token swaps
BREVO_API_KEY = os.getenv("BREVO_API_KEY")  # For email confirmations
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "bookings@wardentravelagent.com")
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "Warden Travel Agent")

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
    cabin_options: dict  # Available cabin classes with sample flights
    
    # Hotel Details (reuse from before)
    check_in: str
    check_out: str
    nights: int  # Number of nights to stay
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
    
    # User Contact
    user_email: str
    
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
    nights: Optional[int] = Field(None, description="Number of nights to stay (e.g. 3, 5, 7)")
    guests: Optional[int] = Field(None, description="Number of travelers")
    budget_max: Optional[float] = Field(None, description="Total budget for trip")
    currency: Optional[str] = Field(None, description="Currency code")
    cabin_class: Optional[str] = Field(None, description="economy, business, or first")
    user_email: Optional[str] = Field(None, description="User's email address for booking confirmation")

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
    except Exception as e:
        print(f"[LLM ERROR] Failed to initialize {LLM_MODEL}: {e}. Using fallback gpt-4o-mini")
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

# --- 4.6. Email Confirmation Function ---
def send_booking_confirmation_email(user_email, booking_details):
    """Send booking confirmation email via Brevo/Sendinblue"""
    if not BREVO_AVAILABLE or not BREVO_API_KEY:
        print("[EMAIL] Brevo not configured. Skipping email confirmation.")
        return False
    
    try:
        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = BREVO_API_KEY
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))
        
        # Build email content
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #4CAF50;">🎉 Booking Confirmed!</h1>
            <p>Your trip is booked! Here are your details:</p>
            
            <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
                <h2>📋 Booking Reference: {booking_details.get('booking_ref', 'N/A')}</h2>
                
                {f'''<h3>✈️ Flight Details</h3>
                <p><strong>Ticket Number:</strong> {booking_details.get('flight_ticket')}</p>
                <p><strong>Flight:</strong> {booking_details.get('flight_info')}</p>''' if booking_details.get('flight_info') else ''}
                
                {f'''<h3>🏨 Hotel Details</h3>
                <p><strong>Confirmation:</strong> {booking_details.get('hotel_confirmation')}</p>
                <p><strong>Hotel:</strong> {booking_details.get('hotel_info')}</p>''' if booking_details.get('hotel_info') else ''}
                
                <h3>💳 Payment Confirmation</h3>
                <p><strong>Amount Paid:</strong> {booking_details.get('amount_usdc', 0):.2f} USDC</p>
                <p><strong>Transaction:</strong> <a href="https://sepolia.basescan.org/tx/{booking_details.get('tx_hash', '')}">View on BaseScan</a></p>
            </div>
            
            <p><strong>Important:</strong> Save this email for your records!</p>
            <p>Have a great trip! ✈️🏨</p>
            
            <hr style="margin: 30px 0;">
            <p style="font-size: 12px; color: #666;">Warden Travel Agent - Crypto-powered travel booking</p>
        </body>
        </html>
        """
        
        send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": user_email}],
            sender={"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
            subject=f"✅ Booking Confirmed - {booking_details.get('booking_ref', 'Your Trip')}",
            html_content=html_content
        )
        
        api_instance.send_transac_email(send_smtp_email)
        print(f"[EMAIL] Confirmation sent to {user_email}")
        return True
        
    except BrevoApiException as e:
        print(f"[EMAIL ERROR] Failed to send: {e}")
        return False
    except Exception as e:
        print(f"[EMAIL ERROR] Unexpected error: {e}")
        return False

# --- 4.7. Date Validation Function ---
def validate_dates(departure_date=None, return_date=None, check_in=None, check_out=None):
    """Validate that dates are not in the past and check-out is after check-in"""
    today = datetime.now().date()
    errors = []
    
    try:
        if departure_date:
            dep = datetime.strptime(departure_date, "%Y-%m-%d").date()
            if dep < today:
                errors.append(f"Departure date ({departure_date}) is in the past")
        
        if return_date:
            ret = datetime.strptime(return_date, "%Y-%m-%d").date()
            if ret < today:
                errors.append(f"Return date ({return_date}) is in the past")
            if departure_date and ret <= dep:
                errors.append("Return date must be after departure date")
        
        if check_in:
            ci = datetime.strptime(check_in, "%Y-%m-%d").date()
            if ci < today:
                errors.append(f"Check-in date ({check_in}) is in the past")
        
        if check_out:
            co = datetime.strptime(check_out, "%Y-%m-%d").date()
            if co < today:
                errors.append(f"Check-out date ({check_out}) is in the past")
            if check_in and co <= ci:
                errors.append("Check-out date must be after check-in date")
    
    except ValueError as e:
        errors.append(f"Invalid date format: {e}")
    
    return errors

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
    
    # RESET - Only check if last message is from USER (HumanMessage), not agent's own messages
    last_human_msg = None
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            last_human_msg = get_message_text(msg).lower()
            break
    
    if last_human_msg and ("start over" in last_human_msg or "reset" in last_human_msg or "new search" in last_human_msg):
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

    # PAGINATION - Only trigger on explicit pagination requests
    # Avoid false positives like "5 nights after checking" → "after" being mistaken for navigation
    pagination_keywords = ["show more", "see more", "more options", "more flights", "more hotels", "other options"]
    if any(keyword in last_msg for keyword in pagination_keywords) or (last_msg.strip() in ["more", "next"]):
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

    # CONFIRMATION - Check if we're waiting for booking confirmation
    if state.get("waiting_for_booking_confirmation"):
        # Check if last message is from user (HumanMessage or dict with type='human')
        last_message = state["messages"][-1]
        is_human = isinstance(last_message, HumanMessage) or (isinstance(last_message, dict) and last_message.get("type") == "human")
        
        if not is_human:
            return {}  # Don't process agent's own messages
        
        print(f"[PARSE_INTENT] Confirmation wait - checking message: '{last_msg}'")
        if any(w in last_msg for w in ["yes", "proceed", "confirm", "book it", "pay", "ok"]):
            print("[PARSE_INTENT] User confirmed, returning empty dict to proceed")
            return {}  # User confirmed, proceed with existing state
        
        # User said something else during confirmation wait
        if any(w in last_msg for w in ["no", "cancel", "change", "start over", "modify"]):
            print("[PARSE_INTENT] User wants to change")
            return {
                "waiting_for_booking_confirmation": False,
                "messages": [AIMessage(content="No problem! What would you like to change?")]
            }
        
        # User didn't clearly confirm or deny - prompt them again
        print(f"[PARSE_INTENT] Message '{last_msg}' not recognized as confirmation")
        return {
            "messages": [AIMessage(content="⚠️ Please reply **'yes'** or **'confirm'** to complete the booking, or say **'change'** to modify.")]
        }

    # EXTRACT INTENT
    today_str = date.today().strftime("%Y-%m-%d")
    
    llm = get_llm()
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"""You are a travel assistant. Today is {today_str}.

Detect trip type (IMPORTANT - Be specific!):
- "flight" or "fly" or "plane" ONLY → flight_only
- "hotel" or "accommodation" or "stay" ONLY → hotel_only  
- "trip" or "vacation" or "travel" or "package" or mentions BOTH origin AND destination cities → complete_trip
- If user mentions traveling FROM somewhere TO somewhere → complete_trip (they need flight + hotel)

💡 USER CONTEXT: We provide travel research and booking information.
We search real flights (Amadeus) and hotels (Booking.com) with accurate prices,
then give users direct links and instructions to book on major platforms.

CRITICAL DETECTION RULES:
- "Find me a trip from London to Paris" → complete_trip (mentions origin + destination = needs flight)
- "Book a trip to Paris" → complete_trip (implies travel from somewhere)
- "Find flights to Paris" → flight_only (only wants flights)
- "Book hotel in Paris" → hotel_only (only wants hotel)

Extract:
- origin: Departure city (for flights)
- destination: Arrival city
- departure_date/return_date: Flight dates
- check_in/check_out: Hotel dates
- nights: Number of nights to stay (extract from "3 nights", "5 days", "a week" = 7 nights)
- guests: Number of travelers (extract from "2 adults", "3 people", "1 adult and 2 children" etc - count total number)
- budget_max: Total budget
- currency: USD, GBP, EUR, etc.
- cabin_class: economy (default), business, or first

Duration/Nights extraction:
- "3 nights" → nights=3
- "5 days" → nights=5
- "a week" → nights=7
- "weekend" → nights=2

Date parsing rules (CRITICAL - Calculate carefully!):
Today is {datetime.now().strftime("%A, %B %d, %Y")} ({datetime.now().strftime("%Y-%m-%d")})

Examples:
- "tomorrow" → Add 1 day: {(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")}
- "in 3 days" → Add 3 days: {(datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")}
- "next week" → Add 7 days: {(datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")}
- "next Friday" → Find NEXT Friday (not this Friday if today is Friday). Calculate days until Friday comes again.
  * Today is {datetime.now().strftime("%A")}, so next Friday is {((datetime.now() + timedelta(days=(4 - datetime.now().weekday() + 7) % 7 if datetime.now().weekday() != 4 else 7)).strftime("%Y-%m-%d"))}
- "next Monday" → Find NEXT Monday. Calculate days until Monday comes again.
  * Next Monday: {((datetime.now() + timedelta(days=(0 - datetime.now().weekday() + 7) % 7 if datetime.now().weekday() != 0 else 7)).strftime("%Y-%m-%d"))}

IMPORTANT:
- Always return dates in YYYY-MM-DD format
- NEVER return dates in the past (before {datetime.now().strftime("%Y-%m-%d")})
- For check-out: MUST be AFTER check-in date
- Count carefully - "next Friday" means the Friday that comes next, not today even if today is Friday

Examples:
- "Fly from London to Paris tomorrow" → trip_type=flight_only, origin=London, destination=Paris
- "Book hotel in Tokyo for 3 nights" → trip_type=hotel_only, destination=Tokyo, nights=3
- "2 adults flying to Paris" → guests=2
- "Plan trip from NYC to Dubai next week" → trip_type=complete_trip, origin=NYC, destination=Dubai"""
    
    intent_data = {}
    try:
        intent = structured_llm.invoke([SystemMessage(content=system_prompt)] + messages[-3:])
        
        current_year = datetime.now().year
        
        if intent.trip_type: 
            intent_data["trip_type"] = intent.trip_type
        if intent.origin: 
            intent_data["origin"] = intent.origin.title()
        if intent.destination: 
            intent_data["destination"] = intent.destination.title()
        if intent.departure_date:
            # Fix year if LLM returned wrong year
            parsed_date = datetime.strptime(intent.departure_date, "%Y-%m-%d")
            if parsed_date.year < current_year:
                parsed_date = parsed_date.replace(year=current_year)
                intent.departure_date = parsed_date.strftime("%Y-%m-%d")
            intent_data["departure_date"] = intent.departure_date
        if intent.return_date:
            # Fix year if LLM returned wrong year
            parsed_date = datetime.strptime(intent.return_date, "%Y-%m-%d")
            if parsed_date.year < current_year:
                parsed_date = parsed_date.replace(year=current_year)
                intent.return_date = parsed_date.strftime("%Y-%m-%d")
            intent_data["return_date"] = intent.return_date
            intent_data["trip_mode"] = "round_trip"
        elif intent.departure_date:
            intent_data["trip_mode"] = "one_way"
        if intent.check_in:
            # Fix year if LLM returned wrong year
            parsed_date = datetime.strptime(intent.check_in, "%Y-%m-%d")
            if parsed_date.year < current_year:
                parsed_date = parsed_date.replace(year=current_year)
                intent.check_in = parsed_date.strftime("%Y-%m-%d")
            intent_data["check_in"] = intent.check_in
        if intent.check_out:
            # Fix year if LLM returned wrong year
            parsed_date = datetime.strptime(intent.check_out, "%Y-%m-%d")
            if parsed_date.year < current_year:
                parsed_date = parsed_date.replace(year=current_year)
                intent.check_out = parsed_date.strftime("%Y-%m-%d")
            intent_data["check_out"] = intent.check_out
        if intent.nights:
            intent_data["nights"] = intent.nights
        if intent.guests: 
            intent_data["guests"] = intent.guests
        if intent.budget_max: 
            intent_data["budget_max"] = intent.budget_max
        if intent.currency:
            curr = intent.currency.upper()
            intent_data["currency"] = curr
            symbols = {"USD": "$", "GBP": "£", "EUR": "€", "NGN": "₦", "CAD": "C$", "AUD": "A$", "JPY": "¥"}
            intent_data["currency_symbol"] = symbols.get(curr, "$")
        if intent.user_email:
            intent_data["user_email"] = intent.user_email
        if intent.cabin_class:
            cabin_input = intent.cabin_class.lower().replace(" ", "_").replace("-", "_")
            # Map common variations and abbreviations
            cabin_map = {
                "economy": "economy",
                "eco": "economy",
                "econ": "economy",
                "economy_class": "economy",
                "coach": "economy",
                "premium": "premium_economy",
                "premium_economy": "premium_economy",
                "premium_eco": "premium_economy",
                "prem": "premium_economy",
                "business": "business",
                "biz": "business",
                "business_class": "business",
                "first": "first",
                "first_class": "first",
                "firstclass": "first",
                "1st": "first",
                "1": "economy",
                "2": "premium_economy", 
                "3": "business",
                "4": "first"
            }
            intent_data["cabin_class"] = cabin_map.get(cabin_input, cabin_input)
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

    # REMOVED AUTO-DATES - Agent should NEVER auto-set return dates
    # User must explicitly provide return_date and check_out date
    # This prevents unwanted "7 nights" assumptions

    # Validate dates (check not in past and logical order)
    date_errors = validate_dates(
        departure_date=state.get("departure_date") or intent_data.get("departure_date"),
        return_date=state.get("return_date") or intent_data.get("return_date"),
        check_in=state.get("check_in") or intent_data.get("check_in"),
        check_out=state.get("check_out") or intent_data.get("check_out")
    )
    
    if date_errors:
        error_msg = "❌ **Date validation failed:**\n\n" + "\n".join(f"• {err}" for err in date_errors)
        error_msg += "\n\n💡 Please provide valid future dates."
        return {
            "messages": [AIMessage(content=error_msg)]
        }

    return intent_data

# --- 6. Node: Requirements Gatherer ---
def gather_requirements(state: AgentState):
    trip_type = state.get("trip_type")
    
    # Smart default: If user provides origin (FROM somewhere), default to complete_trip
    # This catches "Find me a trip from London to Paris" even if LLM doesn't set trip_type
    if not trip_type and state.get("origin") and state.get("destination"):
        trip_type = "complete_trip"
        state = {**state, "trip_type": "complete_trip"}
    
    if not trip_type:
        # Only send welcome message if this is the first interaction (no messages yet or only 1 message)
        existing_messages = state.get("messages", [])
        if len(existing_messages) <= 1:  # 0 or 1 message means first interaction
            return {
                "requirements_complete": False,
                "messages": [AIMessage(content="👋 **Welcome to Warden Travel Research!**\n\nI'll help you find the best flights and hotels for your trip.\n\n✈️ **Flight search** - Find flights to your destination\n🏨 **Hotel search** - Find accommodation\n🌍 **Complete trip** - Search flights + hotels together\n\n💡 I'll search real availability and prices, then provide you with booking links and instructions.\n\nExample: 'Find me a trip from London to Paris'")]
            }
        else:
            # Don't repeat welcome message in ongoing conversations
            return {
                "requirements_complete": False,
                "messages": []
            }
    
    missing = []
    
    # Check flight requirements
    if trip_type in ["flight_only", "complete_trip"]:
        if not state.get("origin"): 
            missing.append("Departure City")
        if not state.get("departure_date"): 
            missing.append("Departure Date")
    
    # Check hotel requirements - ask for NIGHTS instead of check-out date
    if trip_type in ["hotel_only", "complete_trip"]:
        if not state.get("check_in"): 
            missing.append("Check-in Date")
        if not state.get("nights"):  # Ask for duration instead of check-out
            missing.append("Number of Nights")
    
    # Common requirements
    if not state.get("destination"): 
        missing.append("Destination")
    if not state.get("guests"): 
        missing.append("Number of Travelers")
    if state.get("budget_max") is None: 
        missing.append("Budget")

    if not missing:
        # Calculate return_date and check_out based on nights
        updates = {}
        
        if not state.get("currency"):
            # Calculate rooms: 2 guests per room, round up
            guest_count = state.get("guests", 2)
            rooms_needed = (guest_count + 1) // 2
            updates["currency"] = "USD"
            updates["currency_symbol"] = "$"
            updates["rooms"] = rooms_needed
        
        # Calculate dates based on nights
        if state.get("nights"):
            nights = state["nights"]
            
            # For complete_trip: Only set check-in/check-out for hotel
            # DO NOT set return_date - we only book ONE-WAY flights
            if trip_type == "complete_trip" and state.get("departure_date"):
                try:
                    dep = datetime.strptime(state["departure_date"], "%Y-%m-%d")
                    updates["check_in"] = state["departure_date"]  # Check-in same as departure
                    updates["check_out"] = (dep + timedelta(days=nights)).strftime("%Y-%m-%d")
                    updates["trip_mode"] = "one_way"  # Force one-way flights
                except:
                    pass
            
            # For hotel_only: check_out = check_in + nights
            if trip_type == "hotel_only" and state.get("check_in") and not state.get("check_out"):
                try:
                    ci = datetime.strptime(state["check_in"], "%Y-%m-%d")
                    updates["check_out"] = (ci + timedelta(days=nights)).strftime("%Y-%m-%d")
                except:
                    pass
        
        updates["requirements_complete"] = True
        return updates
    
    # If not all requirements met, don't mark as complete
    if not state.get("requirements_complete"):
        pass  # Continue to ask for missing items

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
        elif "Number of Nights" in missing:
            msg = f"🌙 How many nights do you plan to stay? (e.g. 3, 5, 7)"
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
    guests = state.get("guests", 2)  # Default to 2 (consistent with hotel search)
    currency = state.get("currency", "USD")
    symbol = state.get("currency_symbol", "$")
    
    # If user hasn't selected cabin class yet, search all classes and show options
    if not state.get("cabin_class"):
        print(f"[FLIGHT SEARCH] Searching all cabin classes for {origin} -> {destination}")
        
        cabin_classes = ["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"]
        cabin_results = {}
        
        for cabin in cabin_classes:
            flights = search_flights_amadeus(
                origin, destination, departure_date,
                return_date, guests, cabin
            )
            if flights:
                cabin_results[cabin] = flights[0]  # Get cheapest option per class
        
        if not cabin_results:
            return {
                "messages": [AIMessage(content=f"😔 No flights found from **{origin}** to **{destination}** on {departure_date}. Try different dates?")]
            }
        
        # Convert prices from USD to local currency
        # Amadeus returns USD prices, so if user wants GBP: price_gbp = price_usd / rate_gbp_to_usd
        # If user wants USD/USDC, rate = 1.0 so no conversion needed
        rate = get_live_rate(currency)
        
        # Build cabin class selection message with price comparison
        msg_parts = [f"✈️ **Available Cabin Classes** for {origin} → {destination}:\n"]
        
        cabin_display = {
            "ECONOMY": "🪑 Economy",
            "PREMIUM_ECONOMY": "✨ Premium Economy", 
            "BUSINESS": "💼 Business",
            "FIRST": "👑 First Class"
        }
        
        # Store prices for comparison
        cabin_prices_local = {}
        for cabin, flight in cabin_results.items():
            # Flight prices from Amadeus are in USD
            price_usd = flight["price"]
            # Convert USD to local currency: if GBP, 1 GBP = 1.27 USD, so 100 USD = 100/1.27 = 78.74 GBP
            price_local = round(price_usd / rate, 2) if rate != 1.0 else price_usd
            cabin_prices_local[cabin] = price_local
            # Also add price_local to the flight dict for safety
            flight["price_local"] = price_local
            msg_parts.append(f"\n{cabin_display.get(cabin, cabin)}: **{symbol}{price_local:,.2f}**")
        
        # Add savings context
        if "ECONOMY" in cabin_prices_local and "BUSINESS" in cabin_prices_local:
            economy_price = cabin_prices_local["ECONOMY"]
            business_price = cabin_prices_local["BUSINESS"]
            savings = round((1 - economy_price / business_price) * 100) if business_price > 0 else 0
            msg_parts.append(f"\n\n💰 Save {savings}% by choosing Economy over Business")
        
        msg_parts.append("\n\n💡 **Reply with your preferred cabin class** (e.g., 'economy', 'business', 'first class')")
        
        return {
            "messages": [AIMessage(content="".join(msg_parts))],
            "cabin_options": cabin_results
        }
    
    # User has selected cabin class, search that specific class
    cabin = state.get("cabin_class", "economy").upper()
    cursor = state.get("flight_cursor", 0)
    
    print(f"[FLIGHT SEARCH] {origin} -> {destination} on {departure_date} ({cabin})")
    
    flights = search_flights_amadeus(
        origin, destination, departure_date,
        return_date, guests, cabin
    )
    
    if not flights:
        return {
            "messages": [AIMessage(content=f"😔 No flights found for {cabin.lower().replace('_', ' ')} class. Try a different cabin class?")]
        }
    
    # Convert USD prices to local currency
    # Amadeus returns USD, so for GBP: if 1 GBP = 1.27 USD, then 100 USD = 100/1.27 GBP
    rate = get_live_rate(currency)
    for flight in flights:
        price_usd = flight["price"]
        flight["price_local"] = round(price_usd / rate, 2) if rate != 1.0 else price_usd
    
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
    
    # Format message with budget awareness
    trip_mode = "Round trip" if return_date else "One way"
    options = "\n\n".join([
        f"**{i+1}. {f['airline']} {f['flight_number']}** ✈️ {symbol}{f['price_local']}\n   🕐 {f['departure_time']} - {f['arrival_time']} | ⏱️ {f['duration']} | {f['stops']}"
        for i, f in enumerate(batch)
    ])
    
    budget_msg = ""
    if budget:
        cheapest = min(batch, key=lambda x: x['price_local'])
        remaining = budget - cheapest['price_local']
        if remaining > 0:
            budget_msg = f"\n\n💰 **Budget Status:** {symbol}{remaining:.2f} remaining for hotel (after cheapest flight)"
        else:
            budget_msg = f"\n\n⚠️ **Budget Alert:** Even the cheapest flight uses your full budget. Consider increasing budget for hotel costs."
    
    msg = f"✈️ **Flights from {origin} to {destination}** ({trip_mode})\n📅 {departure_date}{' - ' + return_date if return_date else ''}\n\n{options}{budget_msg}\n\n📝 Reply with the **number** to select (e.g. '1'), or say **'next'** for more."
    
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
        flight_cost = state["selected_flight"].get("price_local", state["selected_flight"].get("price", 0))
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
    # Special case: FLIGHT_ONLY - Provide booking information
    if state.get("trip_type") == "flight_only" and state.get("selected_flight") and not state.get("waiting_for_booking_confirmation"):
        f = state["selected_flight"]
        currency = state.get("currency", "USD")
        sym = state.get("currency_symbol", "$")
        
        flight_total_local = f.get("price_local", f.get("price", 0))
        cabin_display = f.get('cabin', 'ECONOMY').replace('_', ' ').title()
        
        summary_msg = f"""📋 **YOUR FLIGHT BOOKING INFORMATION**

✅ Here's everything you need to book this flight!

---
✈️ **FLIGHT TO BOOK**

**Flight Details:**
• Airline: {f.get('airline', 'Airline')} Flight {f.get('flight_number', 'N/A')}
• Route: {state['origin']} → {state['destination']}
• Date: {state['departure_date']}
• Departure: {f.get('departure_time', 'TBA')}
• Arrival: {f.get('arrival_time', 'TBA')}
• Duration: {f.get('duration', 'N/A')}
• Stops: {f.get('stops', 'Direct')}
• Cabin Class: {cabin_display}
• Passengers: {state.get('guests', 2)} traveler(s)
• Estimated Price: {sym}{flight_total_local:.2f} {currency}

**Step-by-Step Booking Instructions:**

**Option 1: Google Flights (Recommended)**
1. Go to https://www.google.com/flights
2. Enter: {state['origin']} → {state['destination']}
3. Date: {state['departure_date']}
4. Passengers: {state.get('guests', 2)}
5. Look for {f.get('airline', 'Airline')} flight {f.get('flight_number', 'N/A')} at {f.get('departure_time', 'TBA')}
6. Compare prices across booking sites shown
7. Click "Select" → Complete booking

**Option 2: Book Direct with Airline**
1. Visit {f.get('airline', 'airline')}.com
2. Search same route and date
3. Find flight {f.get('flight_number', 'N/A')}
4. Choose {cabin_display} class
5. Complete booking (often gets you loyalty points!)

**Option 3: Kayak (Multi-Site Comparison)**
1. Go to https://www.kayak.com/flights
2. Enter same search criteria
3. Filter by {f.get('airline', 'Airline')}
4. Find best price for this flight
5. Book through preferred site

💡 **Pro Booking Tips:**
✅ **Price:** {sym}{state.get('final_flight_price', 0):.2f} is current estimate - book within 24h to lock it in
✅ **Compare:** Check all three platforms above (prices can vary by {sym}20-50+)
✅ **Baggage:** Verify what's included before booking (can add {sym}25-100 if not)
✅ **Insurance:** Consider travel insurance ({sym}15-40) for flexibility
✅ **Timing:** Book on Tuesday/Wednesday mornings for best prices
✅ **Seats:** Standard selection often free, premium costs extra
✅ **Airport:** Arrive 2-3 hours early for international flights

🔔 **Price Alert:** Set up price alerts on Google Flights if not booking immediately

---

🌐 **Quick Links:**
• [Google Flights](https://www.google.com/flights) - Best for comparison
• [Kayak](https://www.kayak.com) - Multi-platform search
• [{f.get('airline', 'Airline')}]({f.get('airline', 'airline')}.com) - Direct booking
• [Skyscanner](https://www.skyscanner.com) - Alternative comparison

---

💡 **Why We Don't Book Directly**

Currently, Warden Hub agents can search and provide information but cannot process payments for bookings. We've found the best option for you - now you can book with confidence using the major booking platforms above!

---

🌟 **Need Different Options?**

Say **'start over'** to search again
Say **'show more flights'** for other options

Safe travels! ✈️"""
        
        return {
            "final_flight_price": flight_total_local,
            "final_hotel_price": 0,
            "final_room_type": "N/A",
            "final_total_price_local": flight_total_local,
            "final_total_price_usd": 0,
            "waiting_for_booking_confirmation": True,
            "messages": [AIMessage(content=summary_msg)]
        }
    
    # Regular flow: Hotel room selection
    if state.get("selected_hotel") and not state.get("room_options"):
        hotel = state["selected_hotel"]
        sym = state.get("currency_symbol", "$")
        currency = state.get("currency", "USD")
        
        # Calculate nights for budget projection
        try:
            d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
            d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
            nights = max(1, (d2 - d1).days)
        except:
            nights = 2
        
        room_options = [
            {"type": "Standard Room", "price": hotel["price"]},
            {"type": "Deluxe Suite", "price": round(hotel["price"] * 1.4, 2)}
        ]
        
        # Calculate total costs for each option (no platform fees - information only)
        standard_total = round(room_options[0]['price'] * nights, 2)
        deluxe_total = round(room_options[1]['price'] * nights, 2)
        
        # Add flight cost if applicable
        if state.get("selected_flight"):
            flight_cost = state["selected_flight"].get("price_local", state["selected_flight"].get("price", 0))
            standard_total += flight_cost
            deluxe_total += flight_cost
        
        # Budget check
        budget = state.get("budget_max")
        budget_msg = ""
        if budget:
            if standard_total > budget:
                budget_msg = f"\n\n⚠️ **Budget Alert:** Standard room total ({sym}{standard_total:.2f}) exceeds your budget of {sym}{budget}. Consider reducing nights or finding a cheaper hotel."
            elif deluxe_total > budget:
                budget_msg = f"\n\n💡 **Budget Note:** Only Standard room fits your budget of {sym}{budget}. Deluxe would be {sym}{deluxe_total:.2f}."
            else:
                budget_msg = f"\n\n✅ **Within Budget:** Both options fit your {sym}{budget} budget!"
        
        rooms_msg = f"""🏨 **{hotel['name']}** - Great choice!

Please select a room type:

**1. Standard Room**
   💵 {sym}{room_options[0]['price']:.2f}/night × {nights} nights = {sym}{room_options[0]['price'] * nights:.2f}
   📊 Estimated total: {sym}{standard_total:.2f} {currency}
   
**2. Deluxe Suite** ⭐
   💵 {sym}{room_options[1]['price']:.2f}/night × {nights} nights = {sym}{room_options[1]['price'] * nights:.2f}
   📊 Estimated total: {sym}{deluxe_total:.2f} {currency}
   ✨ Upgraded amenities, better views{budget_msg}

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
    
    # Calculate totals with platform fee
    try:
        d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
        d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except:
        nights = 2
    
    hotel_total_local = selected_room["price"] * nights
    currency = state.get("currency", "USD")
    sym = state.get("currency_symbol", "$")
    
    # Fetch live rate for currency conversion
    rate = get_live_rate(currency)
    hotel_total_usd = round(hotel_total_local * rate, 2)
    
    # Calculate grand total (including platform fee)
    flight_total_local = 0
    flight_total_usd = 0
    
    if state.get("selected_flight"):
        flight_total_local = state["selected_flight"].get("price_local", state["selected_flight"].get("price", 0))
        flight_total_usd = round(flight_total_local * rate, 2)
    
    # Simple calculation: flight + hotel = total
    # NOTE: This is information-only pricing. We don't process payments.
    # Users will pay final amounts when booking on actual platforms.
    subtotal_local = hotel_total_local + flight_total_local
    grand_total_local = subtotal_local
    grand_total_usd = hotel_total_usd + flight_total_usd
    
    # Build summary (information only - no payments)
    rate_info = f"💰 **Price Estimate in {currency}**\n_(Current exchange rate: 1 {currency} = {rate:.4f} USD as of {time.strftime('%H:%M UTC')})_" if currency not in ["USD", "USDC"] else ""
    
    # Dynamic summary title based on trip type
    trip_type = state.get("trip_type", "")
    if trip_type == "flight_only":
        summary_title = "📋 **Flight Booking Summary**\n"
    elif trip_type == "hotel_only":
        summary_title = "📋 **Hotel Booking Summary**\n"
    else:
        summary_title = "📋 **Complete Trip Summary**\n"
    summary_parts = [summary_title]
    
    if state.get("selected_flight"):
        f = state["selected_flight"]
        cabin_display = f.get('cabin', 'ECONOMY').replace('_', ' ').title()
        summary_parts.append(f"""✈️ **Flight**
• {f['airline']} {f['flight_number']}
• {state['origin']} → {state['destination']}
• {state['departure_date']} at {f['departure_time']}
• Duration: {f['duration']} | {f['stops']}
• Cabin: {cabin_display}
• Cost: {sym}{flight_total_local:.2f} {currency}
""")
    
    hotel_name = state.get('selected_hotel', {}).get('name', 'Hotel') if state.get('selected_hotel') else 'Hotel'
    summary_parts.append(f"""🏨 **Hotel**
• {hotel_name}
• {selected_room['type']}
• {state['check_in']} to {state['check_out']} ({nights} night{'s' if nights != 1 else ''})
• {sym}{selected_room['price']}/night × {nights} = **{sym}{hotel_total_local:.2f} {currency}**

👥 **Guests:** {state.get('guests', 2)}

---

💰 **Estimated Total Cost:**
```
Flight:  {sym}{flight_total_local:.2f} {currency}
Hotel:   {sym}{hotel_total_local:.2f} {currency}
         ─────────────────
Total:   {sym}{grand_total_local:.2f} {currency}
```

⚠️ **Important:** These are estimated prices from our search. Actual prices may vary slightly when booking. Always verify the final price before completing your purchase on the booking platform.

---

📝 **What to Do Next:**

✅ Reply **'yes'** or **'confirm'** to see complete booking details
🔄 Say **'change'** or **'start over'** to modify your selection""")
    
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

# --- 10. Node: Provide Booking Information (Modified from book_trip) ---
def book_trip(state: AgentState):
    """
    Provides complete booking information and instructions to users.
    Does NOT make actual bookings - gives users all details to book themselves.
    """
    if not state.get("waiting_for_booking_confirmation"):
        return {}
    
    currency = state.get("currency", "USD")
    sym = state.get("currency_symbol", "$")
    total_local = state["final_total_price_local"]
    
    # Build comprehensive booking guide
    confirmation_parts = ["📋 **YOUR COMPLETE BOOKING INFORMATION**\n"]
    confirmation_parts.append("✅ Here's everything you need to book your trip!\n")
    
    # Flight Booking Information
    if state.get("selected_flight"):
        f = state["selected_flight"]
        cabin_display = f.get('cabin', 'ECONOMY').replace('_', ' ').title()
        
        confirmation_parts.append(f"""---
✈️ **FLIGHT TO BOOK**

**Flight Details:**
• Airline: {f.get('airline', 'Airline')} Flight {f.get('flight_number', 'N/A')}
• Route: {state['origin']} → {state['destination']}
• Date: {state['departure_date']}
• Departure: {f.get('departure_time', 'TBA')}
• Arrival: {f.get('arrival_time', 'TBA')}
• Duration: {f.get('duration', 'N/A')}
• Stops: {f.get('stops', 'Direct')}
• Cabin Class: {cabin_display}
• Passengers: {state.get('guests', 2)} traveler(s)
• Price: {sym}{state.get('final_flight_price', 0):.2f} {currency}

**Step-by-Step Flight Booking:**

1. **Google Flights:** https://www.google.com/flights
   • Search: {state['origin']} → {state['destination']}
   • Date: {state['departure_date']}
   • Find: {f.get('airline', 'Airline')} {f.get('flight_number', 'N/A')}
   • Compare prices and book

2. **OR Direct:** {f.get('airline', 'airline')}.com
   • Same search criteria
   • Often best for loyalty points/changes

3. **OR Kayak:** https://www.kayak.com/flights
   • Multi-platform comparison
   • Price tracking available

💡 **Flight Booking Tips:**
✅ Current price: {sym}{state.get('final_flight_price', 0):.2f} - book soon to secure it
✅ Compare all platforms (prices can differ by £20-100)
✅ Check baggage allowance (can add £25-100 if not included)
✅ Consider travel insurance (£15-40) for peace of mind
✅ Book Tuesday/Wednesday mornings for best rates
""")
    
    # Hotel Booking Information
    if state.get("selected_hotel"):
        h = state["selected_hotel"]
        try:
            d1 = datetime.strptime(state['check_in'], "%Y-%m-%d")
            d2 = datetime.strptime(state['check_out'], "%Y-%m-%d")
            nights = max(1, (d2 - d1).days)
        except:
            nights = 2
        
        confirmation_parts.append(f"""---
🏨 **HOTEL TO BOOK**

**Hotel Details:**
• Property: {h.get('name', 'Hotel')}
• Room Type: {state['final_room_type']}
• Check-in: {state['check_in']}
• Check-out: {state['check_out']}
• Duration: {nights} night(s)
• Guests: {state.get('guests', 2)} guest(s)
• Price: {sym}{state.get('final_hotel_price', 0):.2f} {currency} total

**How to Book This Hotel:**
1. Visit: https://www.booking.com
2. Enter: "{h.get('name', 'Hotel')}"
3. Select dates:
   - Check-in: {state['check_in']}
   - Check-out: {state['check_out']}
4. Guests: {state.get('guests', 2)}
5. Choose: {state['final_room_type']}
6. Complete booking and payment

💡 **Booking Tips:**
- Compare prices on Booking.com, Hotels.com, and hotel's website
- Check cancellation policy before booking
- Consider booking refundable rates for flexibility
- Some hotels offer discounts for direct bookings
""")
    
    # Total Cost Summary
    confirmation_parts.append(f"""---
💰 **TOTAL COST BREAKDOWN**

```
{"Flight:" if state.get("selected_flight") else ""} {sym}{state.get('final_flight_price', 0):.2f} {currency if state.get("selected_flight") else ""}
{"Hotel:" if state.get("selected_hotel") else ""} {sym}{state.get('final_hotel_price', 0):.2f} {currency if state.get("selected_hotel") else ""}
                    ─────────────
Estimated Total:    {sym}{total_local:.2f} {currency}
```

⚠️ **Note:** Actual prices may vary slightly when booking.
Always verify final price before completing payment.

---

📝 **BOOKING CHECKLIST**

Before you book, make sure you have:
✓ Valid passport/ID
✓ Payment method (credit/debit card)
✓ Email address for confirmations
✓ Emergency contact information
✓ Travel insurance (recommended)

📞 **Travel Tips:**
• Book flights and hotels separately for best prices
• Check visa requirements for your destination
• Arrive at airport 2-3 hours before departure
• Save all booking confirmations
• Consider travel insurance for trip protection

🌐 **Recommended Booking Sites:**

**For Flights:**
- https://www.google.com/flights (Price comparison)
- https://www.kayak.com (Multi-site search)
- Airline direct websites (Best for changes/support)

**For Hotels:**
- https://www.booking.com (Widest selection)
- https://www.hotels.com (Rewards program)
- Hotel direct websites (Sometimes better deals)

---

💡 **Why We Don't Book Directly**

Currently, Warden Hub agents can search and provide information but cannot process payments for bookings. We've found the best options for you - now you can book with confidence using the major booking platforms above!

---

🌟 **Need Different Options?**

Say **'start over'** to search again
Say **'show more flights'** or **'show more hotels'** for other options

Safe travels! ✈️🏨""")
    
    confirmation_msg = "\n".join(confirmation_parts)
    
    return {
        "final_status": "Information Provided",
        "flight_booked": False,
        "hotel_booked": False,
        "waiting_for_booking_confirmation": False,
        "messages": [AIMessage(content=confirmation_msg)]
    }

# --- 11. Node: Consultant ---
def consultant_node(state: AgentState):
    query = state.get("info_request")
    if not query:
        return {}
    
    context = ""
    if state.get("flights"):
        flight_list = [f"{f['airline']} {f['flight_number']}" for f in state['flights'][:5]]
        context = f"User viewing flights: {flight_list}"
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
    print(f"[ROUTE_STEP] trip_type={state.get('trip_type')}, waiting_confirm={state.get('waiting_for_booking_confirmation')}, final_room={state.get('final_room_type')}")
    if state.get("messages"):
        last_msg_type = type(state["messages"][-1]).__name__
        last_msg_content = get_message_text(state["messages"][-1])[:50]
        print(f"[ROUTE_STEP] Last message: {last_msg_type} - '{last_msg_content}'")
    
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
        # After flight selected, go to summary (select_room generates summary)
        if not state.get("waiting_for_booking_confirmation"):
            return "select_room"  # Reuse for summary generation
        if state.get("waiting_for_booking_confirmation"):
            # Check if last message is from user (handle both HumanMessage and dict)
            last_message = state["messages"][-1]
            is_human = isinstance(last_message, HumanMessage) or (isinstance(last_message, dict) and last_message.get("type") == "human")
            
            if is_human:
                last_msg = get_message_text(last_message).lower()
                print(f"[ROUTE_STEP FLIGHT_ONLY] Checking confirmation: '{last_msg}'")
                if any(w in last_msg for w in ["yes", "confirm", "proceed", "book", "ok"]):
                    print("[ROUTE_STEP] CONFIRMATION DETECTED - Routing to book")
                    return "book"
            print(f"[ROUTE_STEP FLIGHT_ONLY] Message type: {type(last_message).__name__}, is_human: {is_human}, waiting for confirmation")
            return "end"
        return "end"
    
    # HOTEL ONLY FLOW
    elif trip_type == "hotel_only":
        if not state.get("hotels"):
            return "search_hotels"
        if not state.get("selected_hotel"):
            return "end"
        if not state.get("final_room_type"):
            return "select_room"
        # After room selected, should show summary and wait for confirmation
        if not state.get("waiting_for_booking_confirmation"):
            return "end"  # Wait for user to see summary
        if state.get("waiting_for_booking_confirmation"):
            # Check if last message is from user (handle both HumanMessage and dict)
            last_message = state["messages"][-1]
            is_human = isinstance(last_message, HumanMessage) or (isinstance(last_message, dict) and last_message.get("type") == "human")
            
            if is_human:
                last_msg = get_message_text(last_message).lower()
                print(f"[ROUTE_STEP HOTEL_ONLY] Checking confirmation: '{last_msg}'")
                if any(w in last_msg for w in ["yes", "confirm", "proceed", "book", "ok"]):
                    print("[ROUTE_STEP] CONFIRMATION DETECTED - Routing to book")
                    return "book"
            print(f"[ROUTE_STEP HOTEL_ONLY] Message type: {type(last_message).__name__}, is_human: {is_human}, waiting for confirmation")
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
        # After room selected, should show summary and wait for confirmation
        if not state.get("waiting_for_booking_confirmation"):
            return "end"  # Wait for user to see summary
        if state.get("waiting_for_booking_confirmation"):
            # Check if last message is from user (handle both HumanMessage and dict)
            last_message = state["messages"][-1]
            is_human = isinstance(last_message, HumanMessage) or (isinstance(last_message, dict) and last_message.get("type") == "human")
            
            if is_human:
                last_msg = get_message_text(last_message).lower()
                print(f"[ROUTE_STEP COMPLETE_TRIP] Checking confirmation: '{last_msg}'")
                if any(w in last_msg for w in ["yes", "confirm", "proceed", "book", "ok"]):
                    print("[ROUTE_STEP] CONFIRMATION DETECTED - Routing to book")
                    return "book"
            print(f"[ROUTE_STEP COMPLETE_TRIP] Message type: {type(last_message).__name__}, is_human: {is_human}, waiting for confirmation")
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

workflow.add_conditional_edges(
    "search_flights",
    lambda state: "end" if state.get("cabin_options") or state.get("flights") else "parse",
    {"end": END, "parse": "parse"}
)

workflow.add_conditional_edges(
    "search_hotels",
    lambda state: "end" if state.get("hotels") else "parse",
    {"end": END, "parse": "parse"}
)

workflow.add_conditional_edges(
    "select_room",
    lambda state: "end" if (state.get("room_options") and not state.get("final_room_type")) or state.get("waiting_for_booking_confirmation") else "parse",
    {"end": END, "parse": "parse"}
)

workflow.add_edge("book", END)

workflow.add_conditional_edges(
    "consultant",
    lambda state: "end",
    {"end": END}
)

memory = MemorySaver()
workflow_app = workflow.compile(checkpointer=memory)

# --- Add Metadata for Warden Registration ---
# Set agent metadata that Warden Studio will read
workflow_app.name = "Warden Travel Research"
workflow_app.description = "AI travel assistant that searches real flights (Amadeus) and hotels (Booking.com) worldwide and provides booking links and instructions"

# --- LangGraph entrypoint (EXPORTED) ---

# If you already created a StateGraph somewhere above like:
# workflow = StateGraph(AgentState)
# ... add nodes/edges ...
# then compile it here.
app = workflow_app
graph = workflow_app
 # optional alias for compatibility

