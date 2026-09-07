"""Инструмент для получения погоды через Яндекс Погоду (open-meteo / yandex).
"""
import urllib.request
import json

TOOL = {
    "name": "weather",
    "description": "Получение текущей погоды и прогноза по координатам или названию города.",
    "methods": {
        "get_weather": {
            "description": "Получить погоду в указанном месте",
            "params": [
                {"name": "latitude", "type": "float", "required": False, "default": 46.3197,
                 "description": "Широта (по умолчанию Ленинградская: 46.3197)"},
                {"name": "longitude", "type": "float", "required": False, "default": 39.3808,
                 "description": "Долгота (по умолчанию Ленинградская: 39.3808)"}
            ]
        }
    }
}

def get_weather(latitude=46.3197, longitude=39.3808):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current_weather=true&daily=precipitation_sum,temperature_2m_max,temperature_2m_min&timezone=auto"
    try:
        req = urllib.request.urlopen(url, timeout=10)
        data = json.loads(req.read().decode("utf-8"))
        curr = data.get("current_weather", {})
        daily = data.get("daily", {})
        return {"current": curr, "daily": daily}
    except Exception as e:
        return {"error": str(e)}
