import requests
import uuid
import time

# URL of your deployed agent
BASE_URL = "https://crypto-defi-agent.onrender.com/agent"

def run_chat_test():
    # Generate a random Thread ID for this specific conversation
    thread_id = str(uuid.uuid4())
    print(f"--- Starting CTO Test (Thread: {thread_id}) ---")

    # --- STEP 1: Ask for hotels ---
    print("\n[User]: Book a hotel in London for $300")
    
    response = requests.post(
        f"{BASE_URL}/invoke",
        json={
            "input": {"messages": [{"content": "Book a hotel in London for $300", "type": "human"}]},
            # THIS LINE FIXES YOUR ERROR:
            "config": {"configurable": {"thread_id": thread_id}} 
        }
    )

    if response.status_code != 200:
        print(f"❌ Error: {response.text}")
        return

    # Print Agent's Reply
    data = response.json()
    latest_msg = data["output"]["messages"][-1]["content"]
    print(f"[Agent]: {latest_msg}")

    # --- STEP 2: Select the first hotel ---
    # Pause briefly to simulate reading
    time.sleep(2)
    print("\n[User]: 1")
    
    response = requests.post(
        f"{BASE_URL}/invoke",
        json={
            "input": {"messages": [{"content": "1", "type": "human"}]},
            # We send the SAME thread_id so it remembers the hotels
            "config": {"configurable": {"thread_id": thread_id}} 
        }
    )

    # Print Agent's Confirmation
    data = response.json()
    latest_msg = data["output"]["messages"][-1]["content"]
    print(f"[Agent]: {latest_msg}")

if __name__ == "__main__":
    run_chat_test()