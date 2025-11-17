# Warden DeFi Swap Utility 💰

This repository contains a modular Python tool designed specifically for Warden Protocol AI Agents. It provides optimized logic for calculating and retrieving the best available token swap routes from DEX aggregators.

## 🎯 Purpose

The primary function of this utility is to abstract the complexity of interacting with the 1inch API (or similar DEX aggregators) and return the most efficient swap path, which the Warden Agent can then use to construct a secure on-chain transaction.

## ✨ Key Features

* **Optimal Routing:** Integrates 1inch API to analyze multiple DEXs for the best rate.
* **Warden-Ready:** Outputs data structures easily consumed by Warden Agent Kit for transaction signing.
* **Environment Secure:** Reads API keys from environment variables (e.g., `ONEINCH_API_KEY`).

## 🔗 Usage Example

This tool is designed to be integrated as a **Tool** within a LangGraph agent workflow.