import requests


def get_weather(city):
    # Hardcoded secret - SonarQube should flag this as a VULNERABILITY.
    api_key = "hardcoded-demo-secret-do-not-use-in-real-code"
    response = requests.get(
        f"https://api.example.com/weather?city={city}&key={api_key}"
    )
    return response.json()
