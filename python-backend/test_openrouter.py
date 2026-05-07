import requests
import os
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
model = os.getenv("PLANNER_MODEL")

headers = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "https://github.com/Koshigawarman/R26-SE-029",
    "X-Title": "AI Backend Builder",
}
payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": "You are a planner"},
        {"role": "user", "content": "Give me a student management app"}
    ],
    "temperature": 0.3,
    "max_tokens": 4096,
}

print(f"Testing model: {model}")
resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
print(resp.status_code)
print(resp.text)
