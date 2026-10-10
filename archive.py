from zipfile import ZipFile, ZIP_DEFLATED
from pathlib import Path
from typing import Dict, Optional, Tuple
from io import BytesIO
import os
import arrow
import threading
import time
import logging
import httpx
from config import ConfigManager
from const import UTM_LOG_DIR, URL_SERVER
from logger import LOG_DIR


logging.getLogger('httpx').setLevel(logging.WARNING)


class LogWorker:
    """Фоновый обработчик логов для отправки на сервер."""

    CHECK_INTERVAL = 60 * 5  # Интервал проверки
    MINI_CHECK_INTERVAL = 60 * 60  # 1 час
    TICK = 0.5    # Шаг таймера в секундах
    PLACEHOLDER_TEXT = "Logs are not available."

    def __init__(self, exit_flag: threading.Event):
        self.exit_flag = exit_flag
        self.logger = logging.getLogger('egais check')
        self.config = ConfigManager()

    def start(self):
        """Запускает рабочий цикл в отдельном потоке."""
        threading.Thread(target=self._run, name="LogWorkerThread", daemon=True).start()
        self.logger.info("Log Worker запущен")

    def _run(self):
        next_full_check = time.monotonic()
        next_mini_check = time.monotonic()

        while not self.exit_flag.is_set():
            now = time.monotonic()

            if now >= next_full_check:
                try:
                    self._process_logs()
                except Exception as e:
                    self.logger.error(f'Глобальная ошибка full logs: {e}')
                next_full_check = now + self.CHECK_INTERVAL

            if now >= next_mini_check:
                try:
                    self._process_mini_logs()
                except Exception as e:
                    self.logger.error(f'Глобальная ошибка mini logs: {e}')
                next_mini_check = now + self.MINI_CHECK_INTERVAL

            time.sleep(self.TICK)

    def _process_logs(self):
        """Обрабатывает и отправляет логи."""
        data_work = self._get_data_work()
        if not data_work:
            return

        for log_type, log_dir in [("utm", UTM_LOG_DIR), ("prog", LOG_DIR)]:
            if data_work.get(log_type):
                file_obj = self._create_file_obj(log_type, log_dir)
                self._send_file_to_server(file_obj)

    def _process_mini_logs(self):
        if not self.config.egais_id:
            return

        # UTM: только transport_info.log
        utm_file = UTM_LOG_DIR / "transport_info.log"

        # PROGRAM: последний актуальный лог
        prog_file = self._get_latest_log_file(LOG_DIR)

        file_obj = self._create_mini_zip_obj(utm_file, prog_file)
        self._send_file_to_server(file_obj)

    def _get_latest_log_file(self, log_dir: Path) -> Optional[Path]:
        if not log_dir.exists():
            return None

        files = [
            f for f in log_dir.iterdir()
            if f.is_file() and f.suffix in {".log"}
        ]

        if not files:
            return None

        return max(files, key=lambda f: f.stat().st_mtime)

    def _create_mini_zip_obj(self, utm_file: Optional[Path], prog_file: Optional[Path]) -> Dict[str, Tuple[str, bytes]]:
        zip_buffer = BytesIO()
        zip_name = "mini_log.zip"
        added_any = False

        with ZipFile(zip_buffer, 'w', ZIP_DEFLATED) as zf:

            if utm_file and utm_file.exists():
                try:
                    zf.write(
                        utm_file,
                        arcname=Path("utm") / utm_file.name
                    )
                    added_any = True
                except Exception as e:
                    self.logger.warning(
                        f"Не удалось добавить UTM лог {utm_file}: {e}"
                    )

            if prog_file and prog_file.exists():
                try:
                    zf.write(
                        prog_file,
                        arcname=Path("prog") / prog_file.name
                    )
                    added_any = True
                except Exception as e:
                    self.logger.warning(
                        f"Не удалось добавить PROG лог {prog_file}: {e}"
                    )

        if not added_any:
            self.logger.warning("Mini-архив пуст, добавляем заглушку")
            return {"log_zip": (zip_name, self._create_placeholder_zip())}

        zip_buffer.seek(0)
        return {"log_zip": (zip_name, zip_buffer.getvalue())}

    def _send_file_to_server(self, file_obj: Dict[str, Tuple[str, bytes]]):
        if not self.config.egais_id:
            self.logger.warning("Отсутствует egais_id, отправка архива с логами невозможна")
            return

        headers = self.config.get_headers()
        file_size_mb = len(file_obj['log_zip'][1]) / (1024 * 1024)
        timeout = httpx.Timeout(10, write=max(60, file_size_mb * 120))

        url = f"{URL_SERVER}/rclient/logs"
        try:
            with httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=3)) as client:
                response = client.post(url, files=file_obj, headers=headers)
            response.raise_for_status()  # Вызывает исключение для кодов != 200-299
            self.logger.debug(f"Лог {file_obj['log_zip'][0]} успешно отправлен")
        except httpx.HTTPStatusError as e:
            self.logger.error(f"Ошибка отправки лога: код {e.response.status_code}, ответ: {e.response.text[:100]}")
        except (httpx.TimeoutException, httpx.RequestError) as e:
            self.logger.error(f"Ошибка сети при отправке лога: {e}")
        except Exception as e:
            self.logger.error(f"Неизвестная ошибка при отправке лога: {e}")

    def _get_data_work(self) -> Dict:
        """Получает данные о необходимости отправки логов."""
        if not self.config.egais_id:
            self.logger.warning("Отсутствует egais_id, запрос логов невозможен")
            return {}

        url = f"{URL_SERVER}/rclient/logs"
        try:
            response = httpx.get(url, headers=self.config.get_headers(), timeout=10)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            self.logger.warning(f"Не удалось проверить задание логов: код {e.response.status_code}")
        except Exception as e:
            self.logger.error(f"Ошибка получения задания логов: {e}")
        return {}

    def _create_file_obj(self, log_type: str, log_dir: Path) -> Dict[str, Tuple[str, bytes]]:
        """Создает объект файла для отправки."""
        zip_bytes = self._get_log_zip_bytes(log_dir)
        timestamp = arrow.now().format("YYYY-MM-DD HH-mm-ss")
        zip_name = f"{log_type}_log_{timestamp}.zip"
        return {"log_zip": (zip_name, zip_bytes)}

    def _get_log_zip_bytes(self, log_dir: Path) -> bytes:
        """Создает ZIP-архив из директории логов."""
        if not log_dir.exists():
            self.logger.info(f"Директория логов {log_dir} не существует")
            return self._create_placeholder_zip()

        try:
            # Создаём BytesIO для хранения архива в памяти
            zip_buffer = BytesIO()

            # Создаём ZipFile с использованием BytesIO
            with ZipFile(zip_buffer, 'w', ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(log_dir):
                    rel_root = Path(root).relative_to(log_dir.parent)
                    for file in files:
                        file_path = Path(root) / file
                        arcname = rel_root / file
                        zf.write(file_path, arcname)

            # После записи архива перемещаем указатель в начало и возвращаем байты
            zip_buffer.seek(0)
            return zip_buffer.getvalue()

        except Exception as e:
            self.logger.error(f"Ошибка создания архива из {log_dir}: {e}")
            return self._create_placeholder_zip()

    def _create_placeholder_zip(self) -> bytes:
        """Создает архив-заглушку, если логов нет."""
        try:
            zip_buffer = BytesIO()

            with ZipFile(zip_buffer, 'w', ZIP_DEFLATED) as zf:
                zf.writestr("placeholder.txt", self.PLACEHOLDER_TEXT)

            zip_buffer.seek(0)
            return zip_buffer.getvalue()
        except Exception as e:
            self.logger.error(f"Ошибка создания заглушки: {e}")
            return b""
