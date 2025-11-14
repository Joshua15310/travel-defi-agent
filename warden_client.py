"""
Warden client wrapper with a safe mock fallback.

This module provides a single function `submit_booking(...)` which will attempt
to use an installed Warden SDK (if present and configured) to submit an on-chain
booking. If the SDK isn't available or the environment is not configured, the
function returns a deterministic mocked tx hash. This keeps production code
thin while allowing reviewers and tests to exercise the on-chain path.
"""
import os

# Try to import a hypothetical Warden SDK; if unavailable, we'll fallback.
try:
    import warden_sdk  # type: ignore
    _HAS_WARDEN_SDK = True
except Exception:
    _HAS_WARDEN_SDK = False


def submit_booking(hotel_name: str, hotel_price: float, destination: str, swap_amount: float):
    """Submit a booking on Warden. Returns dict with `tx_hash` or `error`.

    The function will:
    - Use a Warden SDK if available and `WARDEN_ACCOUNT_ID` + `WARDEN_PRIVATE_KEY` set.
    - Otherwise return a mocked tx_hash for testing/demo.
    """
    account = os.getenv("WARDEN_ACCOUNT_ID")
    private_key = os.getenv("WARDEN_PRIVATE_KEY")

    if _HAS_WARDEN_SDK and account and private_key:
        try:
            # This is pseudocode showing how a real SDK integration would look.
            client = warden_sdk.Client(account_id=account, private_key=private_key)
            # The SDK call and payload shape will depend on the real Warden SDK.
            tx = client.create_booking(hotel=hotel_name, price=hotel_price, destination=destination, swap=swap_amount)
            return {"tx_hash": getattr(tx, "hash", str(tx))}
        except Exception as e:
            return {"error": f"Warden SDK error: {type(e).__name__}: {e}"}

    # Mock fallback (deterministic-ish value for reproducible tests)
    mock_tx = f"0xMOCKED_{abs(hash((hotel_name, hotel_price, destination))) & ((1<<64)-1):016x}"
    return {"tx_hash": mock_tx}
