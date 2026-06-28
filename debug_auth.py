from dotenv import load_dotenv
load_dotenv()
import os, time, base64, hashlib, hmac
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import requests

api_key = os.getenv("KALSHI_API_KEY", "").strip()
secret_path = os.getenv("KALSHI_API_SECRET", "").strip()
base_url = os.getenv("KALSHI_API_BASE_URL", "").strip()

print(f"API Key:    {api_key[:8]}...{api_key[-4:]}")
print(f"Key file:   {secret_path}")
print(f"Key exists: {Path(secret_path).exists()}")
print(f"Base URL:   {base_url}")

# Read the key
key_path = Path(secret_path)
if not key_path.is_absolute():
    key_path = Path(__file__).resolve().parent / secret_path
    
print(f"Full path:  {key_path}")
print(f"File exists: {key_path.exists()}")

key_bytes = key_path.read_bytes()
print(f"Key starts with: {key_bytes[:30]}")

# Try loading it
try:
    private_key = serialization.load_pem_private_key(key_bytes, password=None)
    print("Key loaded: OK")
except Exception as e:
    print(f"Key load FAILED: {e}")

# Build auth headers
timestamp = str(int(time.time() * 1000))
method = "GET"
path = "/portfolio/balance"
message = f"{timestamp}{method}{path}".encode("utf-8")

signature = private_key.sign(
    message,
    padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.DIGEST_LENGTH
    ),
    hashes.SHA256()
)
sig_b64 = base64.b64encode(signature).decode("ascii")

headers = {
    "KALSHI-ACCESS-KEY": api_key,
    "KALSHI-ACCESS-TIMESTAMP": timestamp,
    "KALSHI-ACCESS-SIGNATURE": sig_b64,
    "Content-Type": "application/json",
}

print(f"\nTimestamp:  {timestamp}")
print(f"Message:    {timestamp}{method}{path}")
print(f"Sig (first 20): {sig_b64[:20]}...")

# Make the request
url = f"{base_url}/portfolio/balance"
print(f"\nRequesting: {url}")
r = requests.get(url, headers=headers)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:300]}")