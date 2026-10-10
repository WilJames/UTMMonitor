import sys
from pathlib import Path
import os
from dotenv import load_dotenv


# В исходниках .env лежит в корне проекта.
# В собранном приложении — рядом с exe.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def get_required(name):
    """Получить обязательную переменную окружения."""
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")

    return value


def get_list(name):
    """Получить список значений, разделённых запятыми."""
    value = get_required(name)
    return [item.strip() for item in value.split(",") if item.strip()]
