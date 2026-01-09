# agent.py
import os
import requests
import time
import operator
from datetime import date, timedelta, datetime
from typing import TypedDict, List, Optional, Annotated

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI

# Standard Pydantic
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

# --- 1. State Definition ---
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    destination: str
    check_in: str
    check_out: str
    guests: int
    rooms: int
    budget_max: float
    budget_min: float
    hotels: List[dict]
    selected_hotel: dict
    room_options: List[dict] 
    final_room_type: str
    final_price_per_night: float
    final_total_price: float 
    final_status: str
    date_just_set: bool 
    requirements_complete: bool
    trip_type: str
    waiting_for_booking_confirmation: bool 

# --- 2. Structured Output Schema ---
class TravelIntent(BaseModel):
    destination: Optional[str] = Field(description="City or country name. None if asking for suggestions.")
    check_in: Optional[str] = Field(description="YYYY-MM-DD date. If 'weekend', pick next Friday.")
    check_out: Optional[str] = Field(description="YYYY-MM-DD date. If 'weekend', pick Sunday.")
    guests: Optional[int] = Field(description="Number of people. Infer 2 for honeymoon/couple.")
    budget_max: Optional[float] = Field(description="Maximum price per night in USD.")
    trip_context: Optional[str] = Field(description="Context: 'honeymoon', 'business', 'family', or 'solo'.")

# --- 3. Helpers ---
def get_llm():
    if not LLM_API_KEY:
        print("⚠️ Warning: No LLM API Key found.")
    return ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL if "grok" in LLM_MODEL else None,
        temperature=0.7
    )

def get_message_text(msg):
    """Safely extracts text from Dicts, Objects, or Multimodal Lists."""
    content = ""
    if hasattr(msg, 'content'):
        content = msg.content
    elif isinstance(msg, dict):
        content = msg.get('content', '')
    else:
        content = str(msg)
    
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                text_parts.append(str(part["text"]))
        return " ".join(text_parts)
    return str(content)

# --- 4. Node: Intelligent Intent Parser ---
def parse_intent(state: AgentState):
    messages = state.get("messages", [])
    if not messages: return {}
    
    last_msg = get_message_text(messages[-1]).lower()
    
    # Global Reset
    if "start over" in last_msg or "reset" in last_msg:
        return {
            "destination": None, "check_in": None, "check_out": None,
            "guests": None, "budget_max": None, "hotels": [],
            "selected_hotel": None, "room_options": [], "final_room_type": None,
            "waiting_for_booking_confirmation": False,
            "messages": [AIMessage(content="🔄 System reset. Where are we going next?")]
        }

    # Handling Confirmation Logic
    if state.get("waiting_for_booking_confirmation"):
        if any(word in last_msg for word in ["yes", "proceed", "confirm", "ok", "do it", "book"]):
             return {} # Pass to book_hotel
        else:
            return {
                "waiting_for_booking_confirmation": False, 
                "room_options": [], 
                "messages": [AIMessage(content="🚫 Booking cancelled. Please select a hotel number again.")]
            }

    # --- HOTEL SELECTION LOGIC (The Collision Fix) ---
    # We only treat a number as a "Hotel Selection" if we are NOT already looking at rooms.
    # If room_options is populated, the user is picking a room, so we SKIP this block.
    is_selecting_room = state.get("selected_hotel") and state.get("room_options")
    
    if state.get("hotels") and not is_selecting_room and last_msg.strip().isdigit():
        idx = int(last_msg) - 1
        if 0 <= idx < len(state["hotels"]):
            return {
                "selected_hotel": state["hotels"][idx],
                "room_options": [], # Clear old options for safety
                "final_room_type": None,
                "waiting_for_booking_confirmation": False
            }

    today = date.today().strftime("%Y-%m-%d")
    llm = get_llm()
    structured_llm = llm.with_structured_output(TravelIntent)
    
    system_prompt = f"""
    You are an intelligent travel assistant. Today is {today}.
    
    CRITICAL DATE RULES:
    1. If user says "weekend" or "next weekend":
       - Check-in = Next Friday.
       - Check-out = The Sunday after that Friday (2 nights).
    2. If duration is specific (e.g. "3 days"), calculate Check-out = Check-in + 3 days.
    
    CONTEXT RULES:
    1. "Honeymoon" or "Couple" = 2 guests.
    2. "Family" = Infer count (default 3 or 4).
    """
    
    try:
        intent: TravelIntent = structured_llm.invoke([SystemMessage(content=system_prompt)] + messages)
        
        updates = {}
        if intent.destination: updates["destination"] = intent.destination.title()
        
        if intent.check_in: 
            updates["check_in"] = intent.check_in
            if intent.check_out:
                updates["check_out"] = intent.check_out
            else:
                try:
                    dt = datetime.strptime(intent.check_in, "%Y-%m-%d")
                    updates["check_out"] = (dt + timedelta(days=2)).strftime("%Y-%m-%d")
                except: pass

        if intent.guests: updates["guests"] = intent.guests
        if intent.budget_max: updates["budget_max"] = intent.budget_max
        if intent.trip_context: updates["trip_type"] = intent.trip_context

        return updates
    except Exception as e:
        print(f"LLM Error: {e}")
        return {} 

# --- 5. Node: Conversational Gatherer ---
def gather_requirements(state: AgentState):
    missing = []
    if not state.get("destination"): missing.append("Destination")
    if not state.get("check_in"): missing.append("Check-in Date")
    if not state.get("guests"): missing.append("Guest Count")
    if state.get("budget_max") is None: missing.append("Budget")

    if not missing:
        return {"requirements_complete": True}

    llm = get_llm()
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are Nomad, a witty travel agent. Ask for missing details: {missing_fields}. Be concise."),
        MessagesPlaceholder(variable_name="messages"),
    ])
    
    chain = prompt | llm
    response = chain.invoke({"missing_fields": ", ".join(missing), "messages": state.get("messages", [])})
    return {"requirements_complete": False, "messages": [response]}

# --- 6. Node: Search Hotels ---
def _fetch_hotels_from_api(city, check_in, check_out, guests, rooms):
    try:
        headers = {"X-RapidAPI-Key": BOOKING_KEY, "X-RapidAPI-Host": "booking-com.p.rapidapi.com"}
        r = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/locations", 
                        headers=headers, params={"name": city, "locale": "en-us"}, timeout=10)
        data = r.json()
        if not data: return []
        
        dest_id, dest_type = data[0].get("dest_id"), data[0].get("dest_type")
        params = {
            "dest_id": dest_id, "dest_type": dest_type,
            "checkin_date": check_in, "checkout_date": check_out,
            "adults_number": str(guests), "room_number": str(rooms),
            "units": "metric", "filter_by_currency": "USD", "order_by": "price", "locale": "en-us"
        }
        
        raw_data = []
        for attempt in range(3):
            try:
                res = requests.get("https://booking-com.p.rapidapi.com/v1/hotels/search", 
                                headers=headers, params=params, timeout=15)
                raw_data = res.json().get("result", [])[:15]
                break
            except:
                if attempt < 2: time.sleep(2)
        return raw_data
    except: return []

def search_hotels(state: AgentState):
    if not state.get("requirements_complete"): return {}
    # Stop redundant searching if we already have a selected hotel and haven't reset
    if state.get("selected_hotel"): return {} 
    if state.get("hotels"): return {} 
    
    city = state.get("destination")
    rooms = state.get("rooms", 1)
    
    try:
        d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
        d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except: nights = 1

    raw_data = _fetch_hotels_from_api(city, state["check_in"], state["check_out"], state["guests"], rooms)
    used_city = city
    
    if not raw_data:
        llm = get_llm()
        pivot_prompt = f"User wants hotels in '{city}' but search failed. Name the SINGLE best city/island hub inside '{city}' with hotels. Return ONLY the name."
        new_city = llm.invoke(pivot_prompt).content.strip().replace(".", "")
        raw_data = _fetch_hotels_from_api(new_city, state["check_in"], state["check_out"], state["guests"], rooms)
        if raw_data: used_city = new_city

    all_hotels = []
    for h in raw_data:
        try: 
            total_price = float(h.get("min_total_price", 0))
            if total_price == 0: continue
            price_per_night = round(total_price / nights, 2)
            
            stars = h.get("class", 0)
            if stars > 0:
                star_str = "⭐" * int(stars)
            else:
                score = h.get("review_score", 0)
                star_str = f"Rating: {score}" if score else "New"

            all_hotels.append({
                "name": h.get("hotel_name"), "price": price_per_night,
                "total": total_price, "rating": star_str
            })
        except: pass
    
    all_hotels.sort(key=lambda x: x["price"])
    budget = state.get("budget_max", 10000)
    final_list = [h for h in all_hotels if h["price"] <= budget][:5]
    
    msg_intro = ""
    if not final_list:
        final_list = all_hotels[:3]
        if not final_list:
            return {"messages": [AIMessage(content=f"😔 I couldn't find any hotels in '{city}' or nearby.")]}
        msg_intro = f"⚠️ I couldn't find anything under **${budget}** in {used_city}. Cheapest options:"
    else:
        msg_intro = f"🎉 Options in **{used_city}** under **${budget}/night**:"

    options = "\n".join([f"{i+1}. **{h['name']}** - ${h['price']}/night ({h['rating']})" for i, h in enumerate(final_list)])
    msg = f"{msg_intro}\n\n{options}\n\nReply with the number to book (e.g., '1')."
    return {"hotels": final_list, "messages": [AIMessage(content=msg)]}

# --- 7. Node: Select Room & Calculate Total ---
def select_room(state: AgentState):
    # Case A: Present Options (Hotel selected, BUT room_options is empty)
    if state.get("selected_hotel") and not state.get("room_options"):
        h = state["selected_hotel"]
        room_options = [
            {"type": "Standard Room", "price": h["price"]}, 
            {"type": "Ocean View Suite", "price": round(h["price"]*1.5, 2)}
        ]
        rooms_list = "\n".join([f"{i+1}. **{r['type']}** - ${r['price']}/night" for i, r in enumerate(room_options)])
        msg = f"Great! For **{h['name']}**, please choose a room:\n\n{rooms_list}\n\nReply with '1' or '2'."
        return {"room_options": room_options, "messages": [AIMessage(content=msg)]}
    
    # Case B: Parse Selection
    last_msg = get_message_text(state["messages"][-1]).lower()
    options = state.get("room_options", [])
    
    selected_room = None
    if last_msg.strip().isdigit():
        idx = int(last_msg) - 1
        if 0 <= idx < len(options): selected_room = options[idx]
    elif "suite" in last_msg or "ocean" in last_msg:
         selected_room = options[1]
    elif "standard" in last_msg or "basic" in last_msg:
         selected_room = options[0]
         
    if selected_room:
        try:
            d1 = datetime.strptime(state["check_in"], "%Y-%m-%d")
            d2 = datetime.strptime(state["check_out"], "%Y-%m-%d")
            nights = max(1, (d2 - d1).days)
        except: nights = 1
        
        total_cost = selected_room["price"] * nights
        
        msg = f"""Summary of your trip:
        
🏨 **Hotel:** {state['selected_hotel']['name']}
🛏️ **Room:** {selected_room['type']}
📅 **Duration:** {nights} Nights ({state['check_in']} to {state['check_out']})
💵 **Nightly Rate:** ${selected_room['price']}

💰 **TOTAL TO PAY: ${total_cost:.2f}**

Reply 'Yes' or 'Confirm' to proceed with payment."""

        return {
            "final_room_type": selected_room["type"],
            "final_price_per_night": selected_room["price"],
            "final_total_price": total_cost,
            "waiting_for_booking_confirmation": True, 
            "messages": [AIMessage(content=msg)]
        }

    return {"messages": [AIMessage(content="⚠️ I didn't catch that. Please reply with '1' for Standard or '2' for Suite.")]}

# --- 8. Node: Book Hotel ---
def book_hotel(state: AgentState):
    if not state.get("waiting_for_booking_confirmation"): return {}
    
    details = f"{state['selected_hotel']['name']} ({state['final_room_type']})"
    res = warden_client.submit_booking(details, state["final_total_price"], state["destination"], 0.0)
    tx = res.get("tx_hash", "0xMOCK")
    
    context = state.get("trip_type", "trip")
    congrats = "💍 Congratulations to you and your partner!" if context == "honeymoon" else "✅ Success! Your trip is booked."

    msg = f"""{congrats}

🏨 **Hotel:** {state['selected_hotel']['name']}
🛏️ **Room:** {state['final_room_type']}
📅 **Dates:** {state['check_in']} to {state['check_out']}
💰 **Paid:** ${state['final_total_price']:.2f}

🔗 **Proof of Transaction:**
[View on BaseScan](https://sepolia.basescan.org/tx/{tx})

Safe travels! ✈️"""
    
    return {"final_status": "Booked", "waiting_for_booking_confirmation": False, "messages": [AIMessage(content=msg)]}

# --- 9. Routing ---
def route_step(state):
    if not state.get("requirements_complete"): return "end"
    if not state.get("hotels"): return "search"
    if not state.get("selected_hotel"): return "end"
    
    # If a room type is selected, we are in confirmation mode
    if state.get("final_room_type"):
        if state.get("waiting_for_booking_confirmation"):
            last_msg = get_message_text(state["messages"][-1]).lower()
            if any(w in last_msg for w in ["yes", "proceed", "confirm", "ok", "do it"]):
                return "book"
            else:
                return "end" # Wait for confirmation
        else:
            return "select_room" # Should have been cleared, but safety fallback
            
    return "select_room"

workflow = StateGraph(AgentState)
workflow.add_node("parse", parse_intent); workflow.add_node("gather", gather_requirements)
workflow.add_node("search", search_hotels); workflow.add_node("select_room", select_room)
workflow.add_node("book", book_hotel)

workflow.set_entry_point("parse")
workflow.add_edge("parse", "gather")
workflow.add_conditional_edges("gather", route_step, {
    "end": END, "search": "search", "select_room": "select_room", "book": "book"
})
workflow.add_edge("search", END); workflow.add_edge("select_room", END)
workflow.add_edge("book", END)

memory = MemorySaver(); workflow_app = workflow.compile(checkpointer=memory)