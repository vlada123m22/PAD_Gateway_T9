import jwt
import requests
import json

# ------------------ CONFIG ------------------
BASE_URL = "http://127.0.0.1:8000"
JWT_SECRET = "your-secret-key-change-in-production"
JWT_ALGORITHM = "HS256"

# ------------------ CREATE DUMMY TOKEN ------------------
payload = {
    "user_id": "123",
    "username": "testuser",
    "roles": ["player"],
    "character_id": "char123",
    "exp": 4102444800  # far future
}

token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
headers = {"Authorization": f"Bearer {token}"}

print("Generated JWT token:")
print(token)
print("\n---\n")

def print_response(resp):
    print(resp.status_code)
    try:
        print(json.dumps(resp.json(), indent=2))
    except json.JSONDecodeError:
        print(resp.text)

# ------------------ TEST GET SHOP ITEMS ------------------
print("1️⃣ GET /api/shop/items")
resp = requests.get(f"{BASE_URL}/api/shop/items", headers=headers)
print_response(resp)
print("\n---\n")

# ------------------ TEST BUY ITEM ------------------
print("2️⃣ POST /api/shop/buy (buy 1 item_id=1)")
buy_payload = {"item_id": 1, "quantity": 1}
resp = requests.post(f"{BASE_URL}/api/shop/buy", headers=headers, json=buy_payload)
print_response(resp)
print("\n---\n")

# ------------------ TEST INVENTORY ------------------
print("3️⃣ GET /api/shop/inventory")
resp = requests.get(f"{BASE_URL}/api/shop/inventory", headers=headers)
print_response(resp)
print("\n---\n")

# ------------------ TEST MOCK CHARACTER ROLE ------------------
print("4️⃣ GET /api/characters/char123/role (mock)")
resp = requests.get(f"{BASE_URL}/api/characters/char123/role")
print_response(resp)
