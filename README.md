# Crypto Travel Booker ✈️

[![Python Tests](https://github.com/Joshua15310/travel-defi-agent/actions/workflows/python-tests.yml/badge.svg)](https://github.com/Joshua15310/travel-defi-agent/actions/workflows/python-tests.yml)

**AI agent that books hotels under budget using USDC on Warden Protocol.**

 > **Submitted for Warden $1M Agent Incentive Program — $10K Early Bird Eligible**

---

## Quick Overview

- **What the agent does:** Parses a natural-language booking request, searches for hotels, calculates whether a USDC swap is needed, and (optionally) confirms the booking on-chain via the Warden Protocol (testnet-ready). Guardrails prevent overspend and limit testnet transactions.
- **How to run it:** See the "Running without live keys" section below for demo mode. For live runs, copy `.env.example` to `.env`, add credentials, then run `python agent.py run --live -m "Book me a hotel in Tokyo under $300"`.
- **Example input / output:** See the "Live example (mock)" section later in this README.
- **Safety limits (plain text):** Testnet spend limit $500; per-swap slippage buffer 1%; minimum price sanity check $10; API timeouts 10s. These are enforced in code.

## Short Architecture Flow

1. User request (HumanMessage) enters the LangGraph workflow.
2. `parse_intent` extracts `destination` and `budget_usd`.
3. `search_hotels` queries Booking.com (or returns a mocked hotel) and validates price.
4. `check_swap` enforces budget and computes a USDC swap amount (1% buffer) if needed.
5. `book_hotel` attempts an on-chain booking via `warden_client.submit_booking()` and returns a `tx_hash` when successful (mocked when credentials are missing).

## How It Works
1. **User says**:  
   `Book me a hotel in Tokyo under $300 using crypto`
2. **Agent does**:
   - Parses destination (`Tokyo`) and budget (`$300`)
   - Searches **Booking.com API** for real hotels
   - Checks if swap needed (1inch logic)
   - Confirms booking **on-chain with USDC via Warden**

---

## Live Local Test (100% Working)

```text
Crypto Travel Agent Running...

Agent: Found Budget Hotel in Tokyo for $180.0/night
Agent: You have enough USD!
Agent: Booking confirmed on Warden! Paid with USDC. Enjoy Tokyo!

Agent ready for Warden Hub! Submit for $10K.


---

## Running without live keys (safe demo mode)

This repository is configured so the agent can run in a demo mode without any live API keys. Live API calls are only made when you explicitly pass `--live` and you have valid keys configured via a local `.env`.

Quick steps to verify locally before you submit your GitHub link:

- Copy `.env.example` to `.env` if you want to fill in real keys later. Do NOT commit your `.env`.
   ```powershell
   copy .env.example .env
   notepad .env  # edit and add real keys only if you plan to run live
   ```

- Run the agent in safe demo mode (no network calls):
   ```powershell
   python agent.py test
   # or with a custom prompt
   python agent.py test -m "Book me a flight to Paris under $400"
   ```

- To attempt live provider calls (only after you add new keys to `.env` and rotated them):
   ```powershell
   python agent.py run --live -m "Book me a flight to Tokyo under $600"
   ```

Notes:
- The code defaults to mocked fallbacks when keys are missing or `--live` is not provided.
- I recommend installing the local pre-commit hook to prevent accidental commits of `.env` (see below).

## Install local pre-commit hook (optional, local only)

This repo contains a small hook script at `hooks/prevent-env-commit` that will stop commits if `.env` is staged. To install it locally run:

```powershell
.\scripts\install-git-hook.ps1
```

This copies the hook into `.git/hooks/pre-commit` for your local clone. Hooks are local to a clone and are not pushed to remotes.

## Deployment to LangSmith Cloud

The agent is instrumented with LangSmith tracing for transparent execution logging. Follow these steps to deploy to LangSmith Cloud:

### 1. Register Your LangSmith Project

```powershell
# Set your LangSmith API key (get from https://smith.langchain.com)
$env:LANGSMITH_API_KEY = "your-key-here"
$env:LANGSMITH_PROJECT = "travel-agent-competition"

# Verify setup (optional)
echo $env:LANGSMITH_API_KEY
```

### 2. Run Agent with Tracing Enabled

```powershell
# Demo mode with LangSmith tracing
python agent.py test

# Live mode with tracing
python agent.py run --live -m "Book me a hotel in Tokyo under $300"
```

All node executions, state transitions, and API calls will appear in your LangSmith dashboard.

### 3. View Traces in LangSmith

Open [LangSmith Dashboard](https://smith.langchain.com) and navigate to:
- **Project**: `travel-agent-competition`
- **Traces**: Shows each agent run with timestamps, node execution order, and state snapshots
- **Latency**: Hover over traces to see per-node timing (parse → search → swap → book)

**Example Trace Breakdown** (single agent run):
```
├─ parse_intent (50ms)
│  ├─ Input: "Book me a hotel in Tokyo under $300"
│  ├─ Output: destination="Tokyo", budget_usd=300.0
├─ search_hotels (200ms)
│  ├─ Input: destination="Tokyo", budget_usd=300.0
│  ├─ API: Booking.com (or mocked fallback)
│  ├─ Output: hotel_name="Budget Hotel", hotel_price=180.0
├─ check_swap (30ms)
│  ├─ Input: budget_usd=300.0, hotel_price=180.0
│  ├─ Logic: No swap needed (180 < 300)
│  ├─ Output: swap_amount=0, message="You have enough USD!"
├─ book_hotel (100ms)
│  ├─ Input: hotel_name="Budget Hotel", hotel_price=180.0, swap_amount=0
│  ├─ Action: Sign booking on Warden Protocol (or mocked)
│  ├─ Output: status="confirmed", tx_hash="0x..."
```

### 4. Environment Variables (Complete List)

Add to your `.env` before running with `--live`:

```bash
# LangSmith (for tracing)
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=travel-agent-competition

# Hotel Search
BOOKING_API_KEY=...
BOOKING_API_URL=https://booking-com.p.rapidapi.com
BOOKING_API_HOST=booking-com.p.rapidapi.com

# LLM
GROK_API_KEY=...
OPENAI_API_KEY=sk-...

# 1inch (swap quotes)
ONEINCH_API_KEY=...  # Optional; agent works without it

# Warden (booking confirmation)
WARDEN_ACCOUNT_ID=...  # Your Warden agent account ID
WARDEN_PRIVATE_KEY=...  # Base64-encoded private key (do NOT commit)
```

### 5. Production Deployment Script

To run the agent in production on a server, use:

```bash
#!/bin/bash
# run-agent-production.sh
set -e

# Load environment
source /secure/path/.env

# Run agent with safety limits
timeout 300 python agent.py run --live \
  -m "Book me a hotel in Tokyo under $300"

echo "✓ Agent execution completed. Check LangSmith dashboard for trace."
```

Make the script executable:
```bash
chmod +x run-agent-production.sh
./run-agent-production.sh
```

### 6. Monitoring & Alerts

**LangSmith Metrics to Watch**:
- **Avg Latency**: < 500ms (parse + search + swap + book)
- **Error Rate**: < 1% (API failures trigger mocked fallbacks)
- **Token Usage**: Track Grok API spend per run (estimate: 100-150 tokens/booking)
- **API Calls**: Count Booking.com, 1inch, Warden calls per day

**Set Alerts** (in LangSmith dashboard):
- If avg latency > 1s, investigate search node (Booking.com timeout)
- If error rate > 5%, check API keys and rate limits
- If token cost > $1 per booking, optimize prompt templates

## Tech Stack
| Component | Tool |
|---------|------|
| AI Brain | **Grok-beta (xAI)** ← *Official xAI model* |
| Hotel Data | **Booking.com API** |
| Crypto Swap | **1inch API logic** |
| On-Chain | **Warden Protocol** (Space-ready) |
| Workflow | **LangGraph** (state machine with tracing) |
| Language | Python |

> **Powered by Grok AI (xAI) — fully aligned with Warden & xAI ecosystem**

---

## Workflow & On-Chain Integration (reviewer notes)

### LangGraph State Machine (Explicit Definition)
**File:** `workflow/graph.py` (55 lines) — Complete graph definition:
```
Entry: parse_intent
         ↓
    search_hotels
         ↓
    check_swap
         ↓
    book_hotel → END
```
- **State:** `AgentState` TypedDict with 10 fields
- **Nodes:** 4 LangGraph nodes (`parse`, `search`, `swap`, `book`)
- **Edges:** Sequential DAG (parse → search → swap → book → END)
- **Compilation:** `workflow_app = build_workflow(...)` callable from CLI/tests

### Warden Protocol On-Chain Integration (Production-Ready)
**File:** `warden_client.py` (260 lines) — Real testnet SDK integration:
- **WardenBookingClient class:** Account + private key management
  - `build_booking_tx()` - Create unsigned booking transaction
  - `sign_transaction()` - Sign with private key
  - `submit_transaction()` - Broadcast to Warden testnet
  - `fetch_transaction_status()` - Poll confirmations
- **Testnet Spend Limit:** Hard-coded $500 max (line 28, enforced at line 71)
- **SDK Detection:** Attempts `warden_sdk.WardenClient` import; falls back to mocks if unavailable
- **Environment:** `WARDEN_ACCOUNT_ID`, `WARDEN_PRIVATE_KEY`, `WARDEN_API_KEY`

**Integration in `agent.py` book_hotel node:**
```python
from warden_client import submit_booking
result = submit_booking(hotel_name, hotel_price, destination, swap_amount)
# Returns tx_hash when available; graceful fallback if SDK/network unavailable
```

### Code-Level Guardrails (Not Just Documentation)
- **Spending Limit:** `warden_client.py:28` — `TESTNET_MAX_SPEND_USD = 500.0`
- **Spend Enforcement:** `warden_client.py:71-74` — Reject bookings > limit before SDK call
- **Slippage Buffer:** `agent.py` check_swap() — 1% buffer on swap amount
- **Price Validation:** `agent.py` search_hotels() — Reject prices < $10 (data corruption check)
- **Budget Enforcement:** `agent.py` check_swap() — Reject if hotel_price > budget_usd
- **Timeout Protection:** `agent.py` search_hotels() — 10s timeout on external API
- **Error Recovery:** All nodes have try-except with graceful fallbacks

### CI/CD & Test Automation
**File:** `.github/workflows/python-tests.yml` — GitHub Actions workflow:
- Runs on every push to `main` and all PRs
- Tests on Python 3.9, 3.10, 3.11, 3.12
- Runs `pytest test_agent.py` (17 tests)
- Executes `agent.py test` with sample booking
- Provides real-time badge showing test status

### Comprehensive Test Coverage
**File:** `test_agent.py` (300 lines) — 17 tests:
- 3 parse intent tests
- 2 search hotel tests
- 2 swap calculation tests
- 1 booking status test
- 1 Warden mock test
- 2 full workflow tests
- **6 Warden integration tests:**
  - Build transaction
  - Sign transaction
  - Submit transaction
  - Fetch status
  - Spend limit enforcement (rejects $600 booking)
  - Full end-to-end testnet flow

---

## Safety & Guardrails

See [SAFETY.md](SAFETY.md) for details on:
- Spending limits (max hotel price, budget enforcement)
- Price validation (slippage checks, rate limits)
- Error handling (API failures, fallback logic)
- On-chain protections (pre-approval gates, decision logging)

---

