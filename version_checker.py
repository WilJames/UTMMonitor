import threading
import time
import logging
import httpx
import hashlib
import subprocess
from pathlib import Path
import shutil
from const import URL_SERVER, VERSION, SUB_UPDATE_FOLDER, SEVEN_ZIP_FILE, UPDATE_MONITOR_BAT


class VersionWorker:
    """Фоновый обработчик проверки и установки обновлений программы."""
    CHECK_INTERVAL = 60 * 30  # каждые полчаса проверяем обновление программы
    TICK = 1.0            # Шаг таймера в секундах
    RETRIES = 3           # Количество повторных попыток
    CHUNK_SIZE = 1024 * 1024  # 1 МБ для потоковой загрузки

    def __init__(self, exit_flag: threading.Event):
        self.exit_flag = exit_flag
        self.logger = logging.getLogger('egais check')

    def start(self):
        """Запускает рабочий цикл в отдельном потоке."""
        thread = threading.Thread(target=self._run, name="VersionWorkerThread", daemon=True)
        thread.start()

    def _run(self):
        """Основной рабочий цикл."""
        self.logger.info("Version Worker запущен в автоматическом режиме")

        next_check = time.monotonic() + self.CHECK_INTERVAL  # Время следующей проверки, сдвигаю на следущий цикл, из за проверки при запуске программы

        while not self.exit_flag.is_set():
            if time.monotonic() >= next_check:
                try:
                    if self._check_and_download_update():
                        self._start_update()  # Запуск обновления

                except Exception as e:
                    self.logger.error(f"Ошибка в цикле проверки версии: {e}")

                next_check = time.monotonic() + self.CHECK_INTERVAL  # Перенос времени следующей проверки

            time.sleep(self.TICK)  # Шаг таймера

    def _calc_md5(self, file_path: Path) -> str:
        """Вычисляет MD5 хэш файла."""
        hash_md5 = hashlib.md5()
        try:
            with file_path.open("rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except (IOError, OSError) as e:
            self.logger.error(f"Ошибка чтения файла {file_path} для MD5: {e}")
            return ""

    def _check_and_download_update(self) -> bool:
        """Проверяет версию программы и загружает обновление, если оно доступно."""
        url = f"{URL_SERVER}/rclient/version"
        for attempt in range(1, self.RETRIES + 1):
            try:
                self.logger.debug(f"Проверка версии, попытка {attempt}")
                with httpx.Client(timeout=10) as client:
                    response = client.get(url)
                    response.raise_for_status()
                    data = response.json()

                if not all(key in data for key in ['md5', 'version']):
                    self.logger.warning("Ответ сервера не содержит 'md5' или 'version'")
                    return False

                new_ver, md5 = data['version'], data['md5']
                self.logger.debug(f"Текущая версия: {VERSION}, Доступная: {new_ver}")

                if new_ver != VERSION:
                    self.logger.info(f"Обнаружена новая версия {new_ver}, загрузка...")
                    return self._download_update(md5)
                self.logger.debug("Программа актуальна")
                return False

            except httpx.HTTPStatusError as e:
                self.logger.error(f"Сервер вернул ошибку: {e.response.status_code}")
            except httpx.RequestError as e:
                self.logger.error(f"Ошибка сети: {e}")
            except ValueError as e:
                self.logger.error(f"Ошибка разбора JSON: {e}")
            time.sleep(2)
        self.logger.warning(f"Не удалось проверить версию после {self.RETRIES} попыток")
        return False

    def _download_update(self, expected_md5: str) -> bool:
        """Загружает обновление с проверкой MD5."""
        url = f"{URL_SERVER}/rclient/download-update"
        dest_file = SUB_UPDATE_FOLDER / "UTMMonitor.zip"

        try:
            SUB_UPDATE_FOLDER.mkdir(parents=True, exist_ok=True)
            if dest_file.exists():
                dest_file.unlink()
        except (IOError, OSError) as e:
            self.logger.error(f"Ошибка подготовки директории {SUB_UPDATE_FOLDER}: {e}")
            return False

        timeout = httpx.Timeout(10, read=600)

        for attempt in range(1, self.RETRIES + 1):
            try:
                self.logger.debug(f"Загрузка обновления, попытка {attempt}")
                with httpx.stream("GET", url, timeout=timeout) as response:
                    response.raise_for_status()
                    with dest_file.open("wb") as f:
                        for chunk in response.iter_bytes(self.CHUNK_SIZE):
                            f.write(chunk)

                downloaded_md5 = self._calc_md5(dest_file)
                if downloaded_md5 == expected_md5:
                    self.logger.info("Обновление загружено, MD5 проверен")
                    return True
                self.logger.warning(f"MD5 не совпадает: ожидалось {expected_md5}, получено {downloaded_md5}")
                dest_file.unlink()
            except httpx.HTTPStatusError as e:
                self.logger.error(f"Ошибка загрузки: {e.response.status_code}")
            except (httpx.RequestError, IOError) as e:
                self.logger.error(f"Ошибка при загрузке: {e}")
            time.sleep(2)
        self.logger.warning(f"Не удалось загрузить обновление после {self.RETRIES} попыток")
        return False

    def _create_bat_update(self):
        bat_text = f"""@echo off
set TEMP_DIR=%TEMP%\\{SUB_UPDATE_FOLDER.name}

:wait_for_process
tasklist /FI "IMAGENAME eq UTMMonitor.exe" 2>NUL | find /I "UTMMonitor.exe" >NUL
if %ERRORLEVEL% equ 0 (
    timeout /t 2 >nul
    goto wait_for_process
)

"%TEMP_DIR%\\7z.exe" x "%TEMP%\\{SUB_UPDATE_FOLDER.name}\\UTMMonitor.zip" -o"%APPDATA%" -y
timeout /t 2 >nul
start "" "%APPDATA%\\UTMMonitor\\UTMMonitor.exe"
exit"""

        try:
            with UPDATE_MONITOR_BAT.open('w', encoding='utf-8') as f:
                f.write(bat_text)
            return UPDATE_MONITOR_BAT
        except (IOError, OSError) as e:
            self.logger.error(f"Ошибка создания BAT-файла: {e}")
            return None

    def _start_update(self):
        """Запускает процесс обновления."""
        dest_file = SUB_UPDATE_FOLDER / "UTMMonitor.zip"
        if not dest_file.exists():
            self.logger.error(f"Файл обновления {dest_file} не найден")
            return

        try:
            if not (SUB_UPDATE_FOLDER / SEVEN_ZIP_FILE.name).exists():
                shutil.copy(SEVEN_ZIP_FILE, SUB_UPDATE_FOLDER)
                self.logger.debug(f"Скопирован 7z.exe в {SUB_UPDATE_FOLDER}")

            bat_file = self._create_bat_update()
            if bat_file and bat_file.exists():
                self.logger.info("Запуск обновления")
                subprocess.Popen([str(bat_file)], shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
                self.logger.info("Программа завершает работу для обновления")
                self.exit_flag.set()
        except (shutil.Error, OSError) as e:
            self.logger.error(f"Ошибка при запуске обновления: {e}")
