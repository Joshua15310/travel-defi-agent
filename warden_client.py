"""
Warden Protocol integration with Smart Contract support.
Updated to include real blockchain transaction submission.
"""
import os
import json
from typing import Dict, Optional
from web3 import Web3
from eth_account import Account

# ============================================================================
# CONFIGURATION
# ============================================================================
TESTNET_MAX_SPEND_USD = 500.0
PRODUCTION_MODE = os.getenv("PRODUCTION_MODE", "false").lower() == "true"

# Smart Contract Configuration
WARDEN_CONTRACT_ADDRESS = os.getenv("WARDEN_CONTRACT_ADDRESS", "0x0000000000000000000000000000000000000000")

# Contract ABI (Warden will provide this - example structure)
WARDEN_CONTRACT_ABI = [
    {
        "inputs": [
            {"name": "bookingDetails", "type": "string"},
            {"name": "priceUSD", "type": "uint256"},
            {"name": "userAddress", "type": "address"}
        ],
        "name": "createBooking",
        "outputs": [{"name": "bookingId", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "bookingId", "type": "uint256"},
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"}
        ],
        "name": "BookingCreated",
        "type": "event"
    }
]


class WardenBookingClient:
    """Smart Contract-enabled Warden client for real blockchain bookings."""

    def __init__(self, account_id: str, private_key: str, testnet: bool = True):
        """Initialize Warden client with Web3 and smart contract.

        Args:
            account_id: Your wallet address (0x...)
            private_key: Your private key (0x...)
            testnet: If True, use Base Sepolia; else Base mainnet
        """
        self.account_id = account_id
        self.private_key = private_key
        self.testnet = testnet
        
        # Initialize Web3
        if testnet:
            rpc_url = os.getenv("WARDEN_RPC_URL", "https://sepolia.base.org")
            self.chain_id = 84532  # Base Sepolia
        else:
            rpc_url = os.getenv("WARDEN_RPC_URL", "https://mainnet.base.org")
            self.chain_id = 8453  # Base Mainnet
        
        try:
            self.w3 = Web3(Web3.HTTPProvider(rpc_url))
            if not self.w3.is_connected():
                print(f"[WARN] Failed to connect to {rpc_url}. Using mock mode.")
                self.w3 = None
            else:
                print(f"[WARDEN] Connected to {'testnet' if testnet else 'mainnet'}")
                
                # Initialize smart contract
                if WARDEN_CONTRACT_ADDRESS and WARDEN_CONTRACT_ADDRESS != "0x0000000000000000000000000000000000000000":
                    self.contract = self.w3.eth.contract(
                        address=Web3.to_checksum_address(WARDEN_CONTRACT_ADDRESS),
                        abi=WARDEN_CONTRACT_ABI
                    )
                else:
                    print("[WARN] No contract address configured. Using mock mode.")
                    self.contract = None
        except Exception as e:
            print(f"[ERROR] Web3 initialization failed: {e}")
            self.w3 = None
            self.contract = None

    def build_booking_tx(self, hotel_name: str, hotel_price: float, destination: str, swap_amount: float) -> Dict:
        """Build a smart contract transaction for booking.

        Args:
            hotel_name: Hotel name
            hotel_price: Price in USD
            destination: Destination city
            swap_amount: Swap amount (usually 0)

        Returns:
            Dict with transaction data or error
        """
        # Guardrail: Testnet spend limit
        if self.testnet and hotel_price > TESTNET_MAX_SPEND_USD:
            return {"error": f"Booking exceeds testnet limit (${hotel_price} > ${TESTNET_MAX_SPEND_USD})"}

        # If no Web3 connection, return mock
        if not self.w3 or not self.contract:
            return self._mock_booking_tx(hotel_name, hotel_price, destination, swap_amount)

        try:
            # Prepare booking details
            booking_details = json.dumps({
                "hotel": hotel_name,
                "destination": destination,
                "price_usd": hotel_price,
                "swap_amount": swap_amount
            })
            
            # Convert price to Wei (assuming 1 USD = 1 USDC = 10^6 units for USDC)
            # Note: USDC has 6 decimals, not 18 like ETH
            price_in_usdc_units = int(hotel_price * 10**6)
            
            # Get user's address from environment (or from state)
            user_address = os.getenv("USER_WALLET_ADDRESS", self.account_id)
            
            # Build transaction
            tx = self.contract.functions.createBooking(
                booking_details,
                price_in_usdc_units,
                Web3.to_checksum_address(user_address)
            ).build_transaction({
                'from': Web3.to_checksum_address(self.account_id),
                'nonce': self.w3.eth.get_transaction_count(Web3.to_checksum_address(self.account_id)),
                'gas': 200000,  # Estimate gas
                'gasPrice': self.w3.eth.gas_price,
                'chainId': self.chain_id
            })
            
            print(f"[WARDEN] Built transaction for {hotel_name} (${hotel_price})")
            return {"tx": tx, "status": "unsigned"}

        except Exception as e:
            print(f"[ERROR] Transaction build failed: {e}")
            return self._mock_booking_tx(hotel_name, hotel_price, destination, swap_amount)

    def _mock_booking_tx(self, hotel_name: str, hotel_price: float, destination: str, swap_amount: float) -> Dict:
        """Generate mock transaction (for testing without real contract)."""
        mock_tx_hash = f"0xMOCK_{abs(hash((hotel_name, hotel_price, destination))) & ((1<<64)-1):016x}"
        print(f"[MOCK] Generated mock booking: {mock_tx_hash}")
        return {
            "tx": {
                "to": "0xMOCK_CONTRACT",
                "data": json.dumps({
                    "action": "book_hotel",
                    "hotel": hotel_name,
                    "price_usd": hotel_price,
                    "destination": destination
                }),
                "value": 0
            },
            "tx_hash": mock_tx_hash,
            "status": "mock"
        }

    def sign_transaction(self, tx_data: Dict) -> Dict:
        """Sign transaction with private key.

        Args:
            tx_data: Unsigned transaction

        Returns:
            Dict with signed transaction
        """
        # If mock transaction, return as-is
        if tx_data.get("status") == "mock":
            return {"signed_tx": tx_data, "signature": "0xMOCK_SIG"}

        if not self.w3:
            return {"signed_tx": tx_data, "signature": "0xMOCK_SIG"}

        try:
            # Sign transaction
            signed = self.w3.eth.account.sign_transaction(
                tx_data.get("tx", {}),
                private_key=self.private_key
            )
            
            print(f"[WARDEN] Transaction signed by {self.account_id}")
            return {
                "signed_tx": signed.rawTransaction,
                "signature": signed.signature.hex(),
                "tx_hash": signed.hash.hex()
            }

        except Exception as e:
            print(f"[ERROR] Signing failed: {e}")
            return {"error": f"Sign failed: {e}"}

    def submit_transaction(self, signed_tx_data: Dict) -> Dict:
        """Submit signed transaction to blockchain.

        Args:
            signed_tx_data: Signed transaction data

        Returns:
            Dict with transaction hash and status
        """
        # If mock, return immediately
        if isinstance(signed_tx_data.get("signed_tx"), dict) and signed_tx_data["signed_tx"].get("status") == "mock":
            return {
                "tx_hash": signed_tx_data["signed_tx"]["tx_hash"],
                "status": "mock_success",
                "network": "testnet" if self.testnet else "mainnet"
            }

        if not self.w3:
            return {
                "tx_hash": "0xMOCK_SUBMITTED",
                "status": "mock_success",
                "network": "testnet"
            }

        try:
            # Submit raw transaction
            raw_tx = signed_tx_data.get("signed_tx")
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            tx_hash_hex = tx_hash.hex()
            
            print(f"[WARDEN] Transaction submitted: {tx_hash_hex}")
            
            # Wait for receipt (optional - can be done asynchronously)
            if PRODUCTION_MODE:
                print("[WARDEN] Waiting for confirmation...")
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                status = "success" if receipt.status == 1 else "failed"
            else:
                status = "pending"
            
            return {
                "tx_hash": tx_hash_hex,
                "status": status,
                "network": "testnet" if self.testnet else "mainnet"
            }

        except Exception as e:
            print(f"[ERROR] Transaction submission failed: {e}")
            return {"error": f"Submit failed: {e}"}

    def fetch_transaction_status(self, tx_hash: str) -> Dict:
        """Check transaction status on blockchain.

        Args:
            tx_hash: Transaction hash

        Returns:
            Dict with status and confirmations
        """
        if "MOCK" in tx_hash:
            return {
                "tx_hash": tx_hash,
                "status": "confirmed",
                "confirmations": 3,
                "mock": True
            }

        if not self.w3:
            return {"error": "No Web3 connection"}

        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
            return {
                "tx_hash": tx_hash,
                "status": "success" if receipt.status == 1 else "failed",
                "confirmations": self.w3.eth.block_number - receipt.blockNumber,
                "block_number": receipt.blockNumber,
                "gas_used": receipt.gasUsed
            }
        except Exception as e:
            return {"tx_hash": tx_hash, "status": "pending", "error": str(e)}


def submit_booking(hotel_name: str, hotel_price: float, destination: str, swap_amount: float) -> Dict:
    """Main entry point: Create blockchain booking transaction.

    This function is called by the agent's book_trip() node.

    Args:
        hotel_name: Hotel/flight details
        hotel_price: Total price in USD
        destination: Destination city
        swap_amount: Swap amount (usually 0 for direct USDC)

    Returns:
        Dict with tx_hash, booking_ref, and status
    """
    account_id = os.getenv("WARDEN_ACCOUNT_ID")
    private_key = os.getenv("WARDEN_PRIVATE_KEY")
    testnet = not PRODUCTION_MODE

    # If no credentials, use mock
    if not account_id or not private_key:
        print("[WARDEN] No credentials configured. Using mock booking.")
        client = WardenBookingClient("0xMOCK_ACCOUNT", "0xMOCK_KEY", testnet=True)
    else:
        client = WardenBookingClient(account_id, private_key, testnet=testnet)

    # Step 1: Build transaction
    print(f"[WARDEN] Building booking tx: {hotel_name} (${hotel_price}) in {destination}")
    tx_result = client.build_booking_tx(hotel_name, hotel_price, destination, swap_amount)
    
    if "error" in tx_result:
        print(f"[WARDEN] Build failed: {tx_result['error']}")
        return tx_result

    # Step 2: Sign transaction
    print("[WARDEN] Signing transaction...")
    sign_result = client.sign_transaction(tx_result)
    
    if "error" in sign_result:
        print(f"[WARDEN] Sign failed: {sign_result['error']}")
        return sign_result

    # Step 3: Submit to blockchain
    print("[WARDEN] Submitting to blockchain...")
    submit_result = client.submit_transaction(sign_result)
    
    if "error" in submit_result:
        print(f"[WARDEN] Submit failed: {submit_result['error']}")
        return submit_result

    tx_hash = submit_result.get("tx_hash", "")
    print(f"[WARDEN] Booking submitted! tx_hash={tx_hash}")

    # Generate booking reference
    booking_ref = f"WRD-{tx_hash[-8:].upper()}" if tx_hash else "WRD-ERROR"

    return {
        "tx_hash": tx_hash,
        "booking_ref": booking_ref,
        "status": submit_result.get("status", "unknown"),
        "network": submit_result.get("network", "testnet")
    }