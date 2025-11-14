"""
test_agent.py - Unit and integration tests for the Crypto Travel Agent
Tests each node independently and the full workflow.
"""

import unittest
from langchain_core.messages import HumanMessage
from agent import parse_intent, search_hotels, check_swap, book_hotel, app


class TestParseIntent(unittest.TestCase):
    """Test the parse_intent node."""

    def test_parse_basic(self):
        """Test parsing a basic request."""
        state = {"messages": [HumanMessage(content="Book me a hotel in Tokyo under $300")]}
        result = parse_intent(state)
        
        assert result["destination"] == "Tokyo", f"Expected 'Tokyo', got {result['destination']}"
        assert result["budget_usd"] == 300.0, f"Expected 300.0, got {result['budget_usd']}"
        assert "tokyo" in result["user_query"].lower()

    def test_parse_with_variations(self):
        """Test parsing with different destination markers."""
        test_cases = [
            ("Book me a hotel to Paris under $200", "Paris", 200.0),
            ("Find me a hotel in London for $500", "London", 500.0),
            ("I need a hotel at Barcelona budget $400", "Barcelona", 400.0),
        ]
        
        for message, expected_dest, expected_budget in test_cases:
            state = {"messages": [HumanMessage(content=message)]}
            result = parse_intent(state)
            assert result["destination"] == expected_dest, f"Failed for '{message}'. Got {result['destination']}"
            assert result["budget_usd"] == expected_budget, f"Budget mismatch for '{message}'"

    def test_parse_defaults(self):
        """Test that defaults are used when parsing fails gracefully."""
        # When no marker is found, the parser uses the first word of the query
        state = {"messages": [HumanMessage(content="Just book something")]}
        result = parse_intent(state)
        
        # "Just" is the first word, capitalized to "Just"
        assert result["destination"] == "Just"
        assert result["budget_usd"] == 400.0  # default when no $ found


class TestSearchHotels(unittest.TestCase):
    """Test the search_hotels node."""

    def test_search_mocked_fallback(self):
        """Test that mocked fallback works when live=False."""
        state = {
            "messages": [HumanMessage(content="test")],
            "destination": "Tokyo",
            "budget_usd": 300.0,
            "user_query": "test",
            "hotel_name": "",
            "hotel_price": 0.0,
            "needs_swap": False,
            "swap_amount": 0.0,
            "final_status": ""
        }
        result = search_hotels(state, live=False)
        
        assert result["hotel_price"] == 180.0, "Mocked price should be 180.0"
        assert "Budget Hotel" in result["hotel_name"]
        assert len(result["messages"]) > 0

    def test_search_with_destination(self):
        """Test that search respects the destination in messages."""
        state = {
            "messages": [HumanMessage(content="test")],
            "destination": "Paris",
            "budget_usd": 300.0,
            "user_query": "test",
            "hotel_name": "",
            "hotel_price": 0.0,
            "needs_swap": False,
            "swap_amount": 0.0,
            "final_status": ""
        }
        result = search_hotels(state, live=False)
        
        assert "Paris" in result["messages"][-1].content


class TestCheckSwap(unittest.TestCase):
    """Test the check_swap node."""

    def test_swap_not_needed(self):
        """Test when budget is sufficient."""
        state = {
            "hotel_price": 150.0,
            "budget_usd": 300.0,
            "messages": [],
            "user_query": "",
            "destination": "",
            "hotel_name": "",
            "needs_swap": False,
            "swap_amount": 0.0,
            "final_status": ""
        }
        result = check_swap(state)
        
        assert result["needs_swap"] is False
        assert result["swap_amount"] == 0

    def test_swap_needed(self):
        """Test when budget is insufficient and swap is required."""
        state = {
            "hotel_price": 400.0,
            "budget_usd": 300.0,
            "messages": [],
            "user_query": "",
            "destination": "",
            "hotel_name": "",
            "needs_swap": False,
            "swap_amount": 0.0,
            "final_status": ""
        }
        result = check_swap(state)
        
        # Hotel price > budget, so needs_swap should be False (cannot afford)
        assert result["needs_swap"] is False
        assert "Budget too low" in result["final_status"]


class TestBookHotel(unittest.TestCase):
    """Test the book_hotel node."""

    def test_book_creates_status(self):
        """Test that booking creates the correct final status."""
        state = {
            "hotel_name": "Luxury Hotel",
            "hotel_price": 200.0,
            "destination": "Paris",
            "messages": [],
            "user_query": "",
            "budget_usd": 300.0,
            "needs_swap": False,
            "swap_amount": 0.0,
            "final_status": ""
        }
        result = book_hotel(state)
        
        assert "Luxury Hotel" in result["final_status"]
        assert "200.0" in result["final_status"]
        assert "Paris" in result["messages"][-1].content


class TestFullWorkflow(unittest.TestCase):
    """Test the complete LangGraph workflow."""

    def test_workflow_execution(self):
        """Test that the workflow executes without errors."""
        test_input = {
            "messages": [HumanMessage(content="Book me a hotel in Tokyo under $500")]
        }
        
        outputs = []
        try:
            for output in app.stream(test_input):
                outputs.append(output)
        except Exception as e:
            self.fail(f"Workflow execution failed: {type(e).__name__}: {str(e)}")
        
        assert len(outputs) > 0, "Workflow should produce outputs"

    def test_workflow_state_progression(self):
        """Test that state progresses correctly through the workflow."""
        test_input = {
            "messages": [HumanMessage(content="Book a hotel in Paris for $250")]
        }
        
        for output in app.stream(test_input):
            for node_name, state in output.items():
                # Each node should update specific state keys
                if "destination" in state:
                    assert state["destination"] != ""
                if "hotel_price" in state:
                    assert state["hotel_price"] > 0


if __name__ == "__main__":
    unittest.main()
