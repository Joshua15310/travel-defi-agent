# agent.py - Warden Travel Agent (Production Ready)
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

LLM_BASE_URL = "https://api.x.ai/v1" 
LLM_API_KEY = os.getenv("GROK_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_MODEL = "grok-3" if os.getenv("GROK_API_KEY") else "gpt-4o-mini"

# Fallback Rates (Updated Jan 2026)
FX_RATES_FALLBACK = {
    "GBP": 1.27, "EUR": 1.09, "USD": 1.0, "CAD": 0.73, 
    "NGN": 0.00063, "USDC": 1.0, "AUD": 0.66, "JPY": 0.0069
}

# --- GLOBAL CACHE ---
HOTEL_CACHE = {}
CACHE_TTL = 3600  # 1 hour
RATE_CACHE = {}
RATE_CACHE_TTL = 300  # 5 minutes

# --- 1. State Definition ---
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    destination: str
    check_in: str
    check_out: str
    guests: int
    rooms: int
    budget_max: float
    currency: str          
    currency_symbol: str   
    hotel_cursor: int      
    hotels: List[dict]     
    selected_hotel: dict
    room_options: List[dict] 
    final_room_type: str
    final_price_per_night: float
    final_total_price_local: float 
    final_total_price_usd: float   
    final_status: str
    requirements_complete: bool
    waiting_for_booking_confirmation: bool 
    info_request: str

# --- 2. Structured Output Schema ---
class TravelIntent(BaseModel):
    destination: Optional[str] = Field(None, description="City or country name")
    check_in: Optional[str] = Field(None, description="YYYY-MM-DD date")
    check_out: Optional[str] = Field(None, description="YYYY-MM-DD date")
    guests: Optional[int] = Field(None, description="Number of guests (infer 2 for couples)")
    budget_max: Optional[float] = Field(None, description="Maximum budget value")
    currency: Optional[str] = Field(None, description="Currency code: USD, GBP, EUR, NGN, CAD")

# --- 3. Helper Functions ---
def get_llm():
    """Initialize LLM with proper error handling"""
    try:
        return ChatOpenAI(
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL if "grok" in LLM_MODEL.lower() else None,
            temperature=0.7,
            timeout=30
        )
    except Exception as e:
        print(f"[LLM ERROR] Failed to initialize: {e}")
        # Fallback to GPT-4o-mini if Grok fails
        return ChatOpenAI(model="gpt-4o-mini", temperature=0.7, timeout=30)

def get_message_text(msg):
    """Safely extract text from any message format"""
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

def generate_cache_key(city, check_in, guests, currency):
    """Generate unique cache key for hotel searches"""
    raw = f"{city}|{check_in}|{guests}|{currency}".lower()
    return hashlib.md5(raw.encode()).hexdigest()

def get_live_rate(base_currency):
    """
    Fetch live exchange rate from base_currency to USD/USDC.
    Returns: Rate (e.g., 1 GBP = 1.27 USD)
    """
    base = base_currency.upper()
    if base in ["USD", "USDC"]: 
        return 1.0
    
    # Check cache
    cache_key = f"rate_{base}"
    if cache_key in RATE_CACHE:
        cached = RATE_CACHE[cache_key]
        if time.time() - cached["timestamp"] < RATE_CACHE_TTL:
            print(f"[RATE CACHE] {base}: {cached['rate']}")
            return cached["rate"]
    
    rate = None
    
    # Method 1: exchangerate-api
    try:
        url = f"https://open.exchangerate-api.com/v6/latest/{base}"
        response = requests.get(url, timeout=5)
        data = response.json()
        if data.get("result") == "success" and "USD" in data.get("rates", {}):
            rate = data["rates"]["USD"]
            print(f"[LIVE RATE] {base} to USD: {rate}")
    except Exception as e:
        print(f"[RATE ERROR] exchangerate-api: {e}")
    
    # Method 2: Frankfurter fallback
    if not rate:
        try:
            url = f"https://api.frankfurter.app/latest?from={base}&to=USD"
            response = requests.get(url, timeout=5)
            data = response.json()
            if "rates" in data and "USD" in data["rates"]:
                rate = data["rates"]["USD"]
                print(f"[LIVE RATE] {base} to USD: {rate} (Frankfurter)")
        except Exception as e:
            print(f"[RATE ERROR] Frankfurter: {e}")
    
    # Fallback to hardcoded
    if not rate:
        rate = FX_RATES_FALLBACK.get(base, 1.0)
        print(f"[FALLBACK RATE] {base}: {rate}")
    
    # Cache result
    RATE_CACHE[cache_key] = {"rate": rate, "timestamp": time.time()}
    return rate

def parse_flexible_date(text, today=None):
    """Parse natural language dates"""
    if today is None:
        today = date.today()
    
    text = text.lower().strip()
    
    # Try YYYY-MM-DD format first
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except:
        pass
    
    # Relative dates
    if "tomorrow" in text:
        return today + timedelta(days=1)
    if "next week" in text:
        return today + timedelta(days=7)
    if "next month" in text:
        return today + timedelta(days=30)
    
    # Weekdays
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, day in enumerate(weekdays):
        if day in text:
            current = today.weekday()
            days_ahead = i - current
            if days_ahead <= 0:
                days_ahead += 7
            return today + timedelta(days=days_ahead)
    
    # Default: tomorrow
    return today + timedelta(days=1)

# --- 4. Node: Intent Parser ---
def parse_intent(state: AgentState):
    """Parse user intent with comprehensive extraction"""
    messages = state.get("messages", [])
    if not messages: 
        return {}
    
    last_msg = get_message_text(messages[-1]).lower()
    
    # 1. RESET command
    if "start over" in last_msg or "reset" in last_msg or "new search" in last_msg:
        return {
            "destination": None, "check_in": None, "check_out": None,
            "guests": None, "budget_max": None, "currency": "USD",
            "currency_symbol": "$", "hotels": [], "hotel_cursor": 0, 
            "selected_hotel": None, "room_options": [], 
            "waiting_for_booking_confirmation": False, "info_request": None,
            "requirements_complete": False,
            "messages": [AIMessage(content="🔄 **Reset complete!** Let's start fresh. Where would you like to go?")]
        }

    # 2. INFO REQUEST (must come before selection to handle "tell me about 3")
    if any(phrase in last_msg for phrase in ["tell me about", "what is", "info on", "describe", "more info"]):
        match = re.search(r"\b(\d+)\b", last_msg)
        if match and state.get("hotels"):
            idx = int(match.group(1)) - 1
            if 0 <= idx < len(state["hotels"]):
                hotel = state["hotels"][idx]
                return {"info_request": f"Tell me about {hotel['name']}", "selected_hotel": None}
        return {"info_request": last_msg}

    # 3. PAGINATION
    if any(w in last_msg for w in ["more", "next", "other", "show more", "different"]):
        return {
            "hotel_cursor": state.get("hotel_cursor", 0) + 5,
            "hotels": [],
            "selected_hotel": None,
            "room_options": []
        }

    # 4. CONFIRMATION HANDLING
    if state.get("waiting_for_booking_confirmation"):
        if any(w in last_msg for w in ["yes", "proceed", "confirm", "book it", "pay", "ok"]):
            return {}  # Allow booking to proceed
        # User changed their mind or wants to modify
        return {
            "waiting_for_booking_confirmation": False,
            "messages": [AIMessage(content="No problem! What would you like to change? (destination, dates, budget, or say 'start over')")]
        }

    # 5. EXTRACT INTENT using LLM
    today_str = date.today().strftime("%Y-%m-%d")
    current_checkin = state.get("check_in", "Not set")
    current_checkout = state.get("check_out", "Not set")
    
    llm = get_llm()
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"""You are a travel assistant. Today is {today_str}.
Current booking: {current_checkin} to {current_checkout}.

Extract from user message:
1. destination: City/country name
2. check_in/check_out: YYYY-MM-DD format
3. guests: Number (default 2 for couples/honeymoon)
4. budget_max: Numeric value
5. currency: Detect from context:
   - "pounds" or "£" → GBP
   - "euros" or "€" → EUR  
   - "naira" or "₦" → NGN
   - "dollars" or "$" → USD
   - Default → USD

Examples:
- "London for 400 pounds" → destination=London, budget_max=400, currency=GBP
- "Paris next Friday for 2 people" → destination=Paris, check_in=(next Friday), guests=2"""
    
    intent_data = {}
    try:
        intent = structured_llm.invoke([
            SystemMessage(content=system_prompt)
        ] + messages[-3:])  # Only last 3 messages for context
        
        if intent.destination: 
            intent_data["destination"] = intent.destination.title()
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
    except Exception as e:
        print(f"[INTENT ERROR] {e}")

    # 6. HOTEL/ROOM SELECTION
    if state.get("hotels") and last_msg.strip().isdigit():
        idx = int(last_msg.strip()) - 1
        
        # Selecting hotel
        if not state.get("selected_hotel"):
            if 0 <= idx < len(state["hotels"]):
                return {
                    "selected_hotel": state["hotels"][idx],
                    "room_options": [],
                    "waiting_for_booking_confirmation": False
                }
        
        # Selecting room
        elif state.get("room_options") and not state.get("final_room_type"):
            options = state["room_options"]
            if 0 <= idx < len(options):
                # This will be handled by select_room node
                return {}

    # 7. AUTO-CALCULATE CHECKOUT if only check-in provided
    if intent_data.get("check_in") and not intent_data.get("check_out") and not state.get("check_out"):
        try:
            checkin = datetime.strptime(intent_data["check_in"], "%Y-%m-%d")
            intent_data["check_out"] = (checkin + timedelta(days=2)).strftime("%Y-%m-%d")
        except:
            pass

    # 8. RESET HOTEL SEARCH on new destination
    if intent_data.get("destination"):
        intent_data["hotel_cursor"] = 0
        intent_data["hotels"] = []
        intent_data["selected_hotel"] = None

    return intent_data

# --- 5. Node: Requirements Gatherer ---
def gather_requirements(state: AgentState):
    """Conversational requirement gathering with smart defaults"""
    missing = []
    if not state.get("destination"): 
        missing.append("Destination")
    if not state.get("check_in"): 
        missing.append("Check-in Date")
    if not state.get("guests"): 
        missing.append("Number of Guests")
    if state.get("budget_max") is None: 
        missing.append("Budget")

    # All requirements met
    if not missing:
        # Set default currency if not specified
        if not state.get("currency"):
            return {
                "requirements_complete": True,
                "currency": "USD",
                "currency_symbol": "$"
            }
        return {"requirements_complete": True}

    # Ask for missing requirements using LLM
    llm = get_llm()
    
    context_info = []
    if state.get("destination"):
        context_info.append(f"Destination: {state['destination']}")
    if state.get("check_in"):
        context_info.append(f"Check-in: {state['check_in']}")
    if state.get("guests"):
        context_info.append(f"Guests: {state['guests']}")
    if state.get("budget_max"):
        sym = state.get("currency_symbol", "$")
        context_info.append(f"Budget: {sym}{state['budget_max']}")
    
    context_str = "\n".join(context_info) if context_info else "Starting fresh"
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", f"""You are Nomad, a friendly travel assistant.

Current booking details:
{context_str}

Ask for ONLY the first missing item from: {', '.join(missing)}

Guidelines:
- Be warm and conversational
- If asking for budget, mention they can specify currency (e.g. "400 pounds", "300 euros")
- If asking for dates, suggest formats: "YYYY-MM-DD, 'tomorrow', 'next Friday'"
- If asking for guests, suggest "1", "2", or "2 adults 1 child"
- Keep it brief (1-2 sentences max)"""),
        MessagesPlaceholder(variable_name="messages"),
    ])
    
    chain = prompt | llm
    try:
        response = chain.invoke({"messages": state.get("messages", [])})
        return {
            "requirements_complete": False,
            "messages": [response]
        }
    except Exception as e:
        print(f"[GATHER ERROR] {e}")
        # Fallback to manual prompt
        if "Destination" in missing:
            msg = "👋 Welcome! Which **city** or **country** would you like to visit?"
        elif "Check-in Date" in missing:
            msg = f"Great choice, **{state.get('destination', 'there')}**! 📅 When would you like to check in? (e.g. 2026-02-15, tomorrow, next Friday)"
        elif "Number of Guests" in missing:
            msg = "👥 How many **guests** will be traveling? (e.g. 1, 2, 3)"
        else:
            msg = "💰 What's your **budget per night**? You can specify currency (e.g. '400 pounds', '300 euros', '500 dollars')"
        
        return {
            "requirements_complete": False,
            "messages": [AIMessage(content=msg)]
        }

# --- 6. Node: Consultant ---
def consultant_node(state: AgentState):
    """Provide detailed info about hotels or destinations"""
    query = state.get("info_request")
    if not query: 
        return {}
    
    context = ""
    if state.get("hotels"):
        hotel_list = [f"{i+1}. {h['name']}" for i, h in enumerate(state["hotels"][:5])]
        context = f"User is viewing these hotels:\n" + "\n".join(hotel_list)
    
    prompt = f"""User question: "{query}"

{context}

Respond as a knowledgeable local travel guide. Provide helpful, accurate information about the hotel or destination. Be enthusiastic but honest.

End by asking: "Would you like to book this hotel? Reply with the number (e.g. '1') to select it, or say 'next' for more options."

Keep response under 100 words."""
    
    try:
        response = get_llm().invoke(prompt)
        return {
            "info_request": None,
            "messages": [response]
        }
    except Exception as e:
        print(f"[CONSULTANT ERROR] {e}")
        return {
            "info_request": None,
            "messages": [AIMessage(content="I'd be happy to help! Could you rephrase your question?")]
        }

# --- 7. Node: Hotel Search ---
def _fetch_hotels_raw(city, check_in, check_out, guests, rooms, currency):
    """Fetch hotels from Booking.com API with caching"""
    cache_key = generate_cache_key(city, check_in, guests, currency)
    cached = HOTEL_CACHE.get(cache_key)
    
    if cached and time.time() - cached["timestamp"] < CACHE_TTL:
        print(f"[CACHE HIT] {city}")
        return cached["data"]
    
    if not BOOKING_KEY:
        print("[ERROR] No BOOKING_API_KEY configured")
        return []
    
    try:
        headers = {
            "X-RapidAPI-Key": BOOKING_KEY,
            "X-RapidAPI-Host": "booking-com.p.rapidapi.com"
        }
        
        # Step 1: Get destination ID
        r = requests.get(
            "https://booking-com.p.rapidapi.com/v1/hotels/locations",
            headers=headers,
            params={"name": city, "locale": "en-us"},
            timeout=10
        )
        data = r.json()
        
        if not data or not isinstance(data, list):
            print(f"[API ERROR] No location found for {city}")
            return []
        
        dest_id = data[0].get("dest_id")
        dest_type = data[0].get("dest_type", "city")
        
        # Step 2: Search hotels
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
        
        if res.status_code != 200:
            print(f"[API ERROR] Status {res.status_code}: {res.text[:200]}")
            return []
        
        results = res.json().get("result", [])[:50]
        HOTEL_CACHE[cache_key] = {"timestamp": time.time(), "data": results}
        
        return results
        
    except Exception as e:
        print(f"[FETCH ERROR] {e}")
        return []

def search_hotels(state: AgentState):
    """Search and filter hotels based on user criteria"""
    if not state.get("requirements_complete"):
        return {}
    if state.get("selected_hotel"):
        return {}
    
    city = state.get("destination")
    guests = state.get("guests", 2)
    rooms = state.get("rooms", 1)
    currency = state.get("currency", "USD")
    symbol = state.get("currency_symbol", "$")
    cursor = state.get("hotel_cursor", 0)
    
    # Calculate nights
    try:
        d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
        d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except:
        nights = 2
    
    # Fetch hotels
    raw_data = _fetch_hotels_raw(
        city, state["check_in"], state["check_out"],
        guests, rooms, currency
    )
    
    if not raw_data:
        return {
            "messages": [AIMessage(content=f"😔 Sorry, I couldn't find hotels in **{city}**. Try a different city or check your dates.")]
        }
    
    # Process results
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
                "stars": stars,
                "rating_num": rating
            })
        except:
            continue
    
    # Filter by budget
    budget = state.get("budget_max", 10000)
    valid_hotels = [h for h in all_hotels if h["price"] <= budget]
    
    if not valid_hotels:
        # Show cheapest options instead
        valid_hotels = sorted(all_hotels, key=lambda x: x["price"])[:10]
        msg_intro = f"⚠️ No hotels under {symbol}{budget}/night. Here are the cheapest options in **{city}**:"
    else:
        # Sort by quality for expensive budgets, price for cheap budgets
        if budget > 200:
            valid_hotels.sort(key=lambda x: (x["stars"], -x["price"]), reverse=True)
            msg_intro = f"✨ **Top Luxury Hotels** in {city} (Best First):"
        else:
            valid_hotels.sort(key=lambda x: x["price"])
            msg_intro = f"💰 **Best Value Hotels** in {city} (Cheapest First):"
    
    # Pagination
    batch = valid_hotels[cursor:cursor + 5]
    
    if not batch:
        return {
            "hotel_cursor": 0,
            "messages": [AIMessage(content=f"That's all the hotels I found! Say **'start over'** to search again.")]
        }
    
    # Format message
    options = "\n".join([
        f"**{i+1}. {h['name']}**\n   💵 {symbol}{h['price']}/night | {h['rating_str']}"
        for i, h in enumerate(batch)
    ])
    
    msg = f"{msg_intro}\n\n{options}\n\n📝 Reply with the **number** to book (e.g. '1'), or say **'next'** for more options."
    
    return {
        "hotels": batch,
        "messages": [AIMessage(content=msg)]
    }

# --- 8. Node: Room Selection ---
def select_room(state: AgentState):
    """Handle room type selection and show payment summary"""
    
    # Step 1: Show room options if hotel selected but no rooms shown yet
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
    
    # Step 2: Process room selection
    last_msg = get_message_text(state["messages"][-1]).lower()
    options = state.get("room_options", [])
    
    if not options:
        return {}
    
    # Detect selection
    selected_room = None
    if last_msg.strip() in ["1", "one", "standard"]:
        selected_room = options[0]
    elif last_msg.strip() in ["2", "two", "deluxe", "suite"]:
        selected_room = options[1]
    
    if not selected_room:
        return {
            "messages": [AIMessage(content="⚠️ Please reply with **'1'** for Standard or **'2'** for Deluxe Suite.")]
        }
    
    # Step 3: Calculate totals and show payment summary
    try:
        d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
        d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except:
        nights = 2
    
    local_total = selected_room["price"] * nights
    currency = state.get("currency", "USD")
    sym = state.get("currency_symbol", "$")
    
    # Fetch live exchange rate
    print(f"[PAYMENT] Fetching rate for {currency}")
    rate = get_live_rate(currency)
    usd_total = round(local_total * rate, 2)
    
    # Build rate display
    if currency in ["USD", "USDC"]:
        rate_info = "💵 Payment in **USDC** (1:1 with USD)"
    else:
        rate_info = f"""💱 **Live Exchange Rate**
1 {currency} = {rate:.4f} USD/USDC
_(Rate updated {time.strftime('%H:%M UTC')})_"""
    
    summary_msg = f"""📋 **Booking Summary**

🏨 **Hotel:** {state['selected_hotel']['name']}
🛏️ **Room:** {selected_room['type']}
📅 **Dates:** {state['check_in']} to {state['check_out']} ({nights} night{'s' if nights != 1 else ''})
👥 **Guests:** {state.get('guests', 2)}

💰 **Pricing:**
{sym}{selected_room['price']}/night × {nights} nights = **{sym}{local_total:.2f} {currency}**

{rate_info}

💳 **Total Payment: {usd_total:.2f} USDC** on Base Network

---

✅ Reply **'yes'** or **'confirm'** to complete booking
🔄 Say **'change dates'** or **'start over'** to modify"""
    
    return {
        "final_room_type": selected_room["type"],
        "final_price_per_night": selected_room["price"],
        "final_total_price_local": local_total,
        "final_total_price_usd": usd_total,
        "waiting_for_booking_confirmation": True,
        "messages": [AIMessage(content=summary_msg)]
    }

# --- 9. Node: Book Hotel ---
def book_hotel(state: AgentState):
    """Submit booking to Warden Protocol"""
    if not state.get("waiting_for_booking_confirmation"):
        return {}
    
    hotel_name = state["selected_hotel"]["name"]
    room_type = state["final_room_type"]
    amount_usdc = state["final_total_price_usd"]
    destination = state["destination"]
    
    details = f"{hotel_name} - {room_type} [Base/USDC]"
    
    try:
        # Call warden_client with correct parameter names
        # The function signature is: submit_booking(hotel_name, hotel_price, destination, swap_amount)
        result = warden_client.submit_booking(
            hotel_name=details,
            hotel_price=amount_usdc,  # Changed from price_usd to hotel_price
            destination=destination,
            swap_amount=0.0
        )
        
        tx_hash = result.get("tx_hash", "0xMOCK")
        booking_ref = result.get("booking_ref", f"WRD-{tx_hash[-8:].upper()}")
        
        sym = state.get("currency_symbol", "$")
        currency = state.get("currency", "USD")
        
        confirmation_msg = f"""🎉 **Booking Confirmed!**

✅ Your reservation is complete and payment has been processed.

📋 **Booking Details:**
• **Booking ID:** `{booking_ref}`
• **Hotel:** {hotel_name}
• **Room:** {room_type}
• **Check-in:** {state['check_in']}
• **Check-out:** {state['check_out']}
• **Guests:** {state.get('guests', 2)}

💳 **Payment Receipt:**
• **Paid:** {amount_usdc:.2f} USDC on Base Network
• **Equivalent:** {sym}{state['final_total_price_local']:.2f} {currency}

🔗 **Transaction Proof:**
[View on BaseScan](https://sepolia.basescan.org/tx/{tx_hash})

---

🌟 Thank you for booking with Warden Travel!
Need another booking? Say **'start over'** or **'new search'**

Safe travels! ✈️"""
        
        return {
            "final_status": "Booked",
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

# --- 10. Routing Logic ---
def route_step(state):
    """Intelligent routing between nodes"""
    
    # Route to consultant if info requested
    if state.get("info_request"):
        return "consultant"
    
    # Gather requirements if incomplete
    if not state.get("requirements_complete"):
        return "end"
    
    # Search for hotels if none loaded
    if not state.get("hotels"):
        return "search"
    
    # Stay on search if user hasn't selected a hotel yet
    if not state.get("selected_hotel"):
        return "end"
    
    # Handle room selection and booking flow
    if state.get("final_room_type"):
        if state.get("waiting_for_booking_confirmation"):
            last_msg = get_message_text(state["messages"][-1]).lower()
            if any(word in last_msg for word in ["yes", "confirm", "proceed", "book", "ok", "do it"]):
                return "book"
            else:
                return "end"
        else:
            # Room selected, payment processed
            return "end"
    
    # Still in room selection phase
    return "select_room"

# --- 11. Build Workflow ---
workflow = StateGraph(AgentState)

# Add nodes
workflow.add_node("parse", parse_intent)
workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels)
workflow.add_node("select_room", select_room)
workflow.add_node("book", book_hotel)
workflow.add_node("consultant", consultant_node)

# Set entry point
workflow.set_entry_point("parse")

# Add edges
workflow.add_edge("parse", "gather")

workflow.add_conditional_edges(
    "gather",
    route_step,
    {
        "end": END,
        "search": "search",
        "select_room": "select_room",
        "book": "book",
        "consultant": "consultant"
    }
)

workflow.add_edge("search", END)
workflow.add_edge("select_room", END)
workflow.add_edge("book", END)
workflow.add_edge("consultant", END)

# Compile with memory
memory = MemorySaver()
workflow_app = workflow.compile(checkpointer=memory)