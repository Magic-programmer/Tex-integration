
import json
import os
import requests

BASE_URL = "https://gktex.aspro.cloud/api/v1/module/task/tasks/list"
API_KEY = os.getenv("ASPRO_API_KEY")

if not API_KEY:
    raise SystemExit(
        "Не найден ASPRO_API_KEY. "
        "Сначала задай переменную окружения."
    )

try:
    response = requests.get(
        BASE_URL,
        params={
            "api_key": API_KEY,
            "page": 1,
            "limit": 5,
        },
        timeout=30,
    )

    print("HTTP status:", response.status_code)

    if response.status_code != 200:
        print("Ответ сервера:", response.text[:1000])
        raise SystemExit("Запрос не выполнен. Проверь права API и адрес.")

    data = response.json()

    if "error" in data:
        print("API вернул ошибку:", data["error"])
        raise SystemExit(1)

    # Сохраняем результат локально для изучения структуры.
    # Файл может содержать рабочие данные CRM.
    with open("tasks_preview.json", "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)

    result = data.get("response", {})
    items = result.get("items", [])

    print("Всего записей по ответу API:", result.get("total"))
    print("Записей в этой странице:", len(items))
    print("Ключи ответа:", list(result.keys()))

    if items:
        print("Поля первой задачи:")
        print(json.dumps(
            list(items[0].keys()),
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(
            "Список items пуст или имеет другую структуру. "
            "Проверь tasks_preview.json локально."
        )

    print("Результат сохранён в tasks_preview.json")

except requests.RequestException as exc:
    print("Ошибка соединения:", type(exc).__name__, str(exc))
    raise SystemExit(1)
except ValueError:
    print("Сервер вернул ответ, который не удалось разобрать как JSON.")
    raise SystemExit(1)
