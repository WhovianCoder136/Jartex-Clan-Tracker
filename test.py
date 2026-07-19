import requests

url = "https://stats.jartexnetwork.com/api/clans/matrixxx"
response = requests.get(url)

if response.status_code == 200:
    data = response.json()
    print("Available Keys in the API:", data.keys())
    print("\nFull Data:")
    print(data)
else:
    print(f"Failed to fetch. Status code: {response.status_code}")
