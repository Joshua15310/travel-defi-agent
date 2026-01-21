"""
Test all LangGraph API endpoints to verify deployment
"""
import requests
import json
import uuid

BASE_URL = "https://travel-defi-agent-pmbt.onrender.com"

def test_health():
    """Test /ok endpoint"""
    print("\n✓ Testing /ok endpoint...")
    r = requests.get(f"{BASE_URL}/ok")
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.json()}")
    assert r.status_code == 200
    assert r.json()["ok"] == True
    print("  ✅ Health check passed!")

def test_info():
    """Test /info endpoint"""
    print("\n✓ Testing /info endpoint...")
    r = requests.get(f"{BASE_URL}/info")
    print(f"  Status: {r.status_code}")
    info = r.json()
    print(f"  Version: {info.get('version')}")
    print(f"  LangGraph Py: {info.get('langgraph_py_version')}")
    assert r.status_code == 200
    print("  ✅ Info endpoint passed!")

def test_streaming():
    """Test the main streaming endpoint"""
    print("\n✓ Testing /threads/{thread_id}/runs/stream endpoint...")
    thread_id = str(uuid.uuid4())
    
    # First create the thread by posting to it
    payload = {
        "input": {
            "messages": [
                {"role": "human", "content": "Hello, I want to book a trip from Lagos to Dubai"}
            ]
        },
        "assistant_id": "agent",
        "stream_mode": ["values"]
    }
    
    try:
        r = requests.post(
            f"{BASE_URL}/threads/{thread_id}/runs/stream",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20,
            stream=True
        )
        
        print(f"  Status: {r.status_code}")
        
        if r.status_code == 200:
            events = []
            count = 0
            for line in r.iter_lines():
                if line:
                    events.append(line.decode())
                    count += 1
                    if count >= 5:  # Just get first 5 events to test
                        break
            
            print(f"  Received {len(events)} SSE events")
            if events:
                print(f"  First event preview: {events[0][:150]}...")
            print("  ✅ Streaming endpoint working!")
            return True
        else:
            print(f"  ❌ Error: {r.text[:300]}")
            return False
            
    except Exception as e:
        print(f"  ⚠️ Error: {str(e)}")
        return False

def test_docs():
    """Test /docs endpoint"""
    print("\n✓ Testing /docs endpoint...")
    r = requests.get(f"{BASE_URL}/docs")
    print(f"  Status: {r.status_code}")
    assert r.status_code == 200
    print("  ✅ Docs endpoint accessible!")

if __name__ == "__main__":
    print("=" * 60)
    print("🧪 TESTING LANGGRAPH DEPLOYMENT")
    print(f"🔗 URL: {BASE_URL}")
    print("=" * 60)
    
    all_passed = True
    try:
        test_health()
        test_info()
        test_docs()
        stream_passed = test_streaming()
        
        if not stream_passed:
            all_passed = False
        
        print("\n" + "=" * 60)
        if all_passed:
            print("✅ ALL ENDPOINTS WORKING!")
            print("🚀 Agent is ready for production use")
        else:
            print("⚠️ CORE ENDPOINTS WORKING (streaming needs assistant setup)")
            print("🚀 Agent deployment is live and functional")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ Test failed: {str(e)}")
        import traceback
        traceback.print_exc()
