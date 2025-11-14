# Crypto Travel Booker ✈️

**AI agent that books hotels under budget using USDC on Warden Protocol.**

> **Submitted for Warden $1M Agent Incentive Program — $10K Early Bird Eligible**

---

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


## Tech Stack
| Component | Tool |
|---------|------|
| AI Brain | **Grok-beta (xAI)** ← *Official xAI model* |
| Hotel Data | **Booking.com API** |
| Crypto Swap | **1inch API logic** |
| On-Chain | **Warden Protocol** (Space-ready) |
| Language | Python |

> **Powered by Grok AI (xAI) — fully aligned with Warden & xAI ecosystem**

---

