import requests
import json

# Test direct API call
url = "https://api.siliconflow.cn/v1/embeddings"
headers = {
    "Authorization": "Bearer sk-rar...tedt",
    "Content-Type": "application/json",
}
payload = {
    "model": "Qwen/Qwen3-Embedding-4B",
    "input": "快递几天到",
    "dimensions": 1024,
}

try:
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        embedding = data["data"][0]["embedding"]
        print(f"Embedding dim: {len(embedding)}")
    else:
        print(f"Error: {response.text}")
except Exception as e:
    print(f"Exception: {type(e).__name__}: {e}")