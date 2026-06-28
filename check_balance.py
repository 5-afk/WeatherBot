from dotenv import load_dotenv
load_dotenv()
import os
from src.kalshi_client import KalshiClient

# Temporarily check against production to verify real balance
os.environ["KALSHI_ENV"] = "prod"
k = KalshiClient()
print(f"Checking: {k.base_url}")
b = k.get_balance()
if b:
    print(f"Kalshi balance: ${b:.2f}")
else:
    print("Balance fetch failed")