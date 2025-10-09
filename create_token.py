import jwt
from datetime import datetime, timedelta

# Use the same secret and algorithm as in your FastAPI app
JWT_SECRET = "your-secret-key-change-in-production"
JWT_ALGORITHM = "HS256"

# Dummy payload
payload = {
    "user_id": "123",
    "username": "testuser",
    "roles": ["player"],
    "character_id": "char123",
    # Set token to expire far in the future
    "exp": 4102444800
}

token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
print(token)
