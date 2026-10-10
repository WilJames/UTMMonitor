import threading
import time
import subprocess
from subprocess import STARTUPINFO, STARTF_USESHOWWINDOW, SW_HIDE
import logging
import psutil
from time import sleep
import win32serviceutil  # type: ignore
from rutoken import Rutoken
from const import UTM_BAT, SRC_PATH

SW_SHOWMINNOACTIVE = 7  # свернуто и неактивно


class EgaisRunnerWorker:
    """Фоновый worker для проверки и запуска службы UTM Transport."""

    # CHECK_INTERVAL = 60 * 5  # раз в 5 минут
    CHECK_INTERVAL = 60 * 30  # првоерка УТМ каждые 30 минут
    SERVICE_NAME = "Transport"
    PROCESS_NAME = "utm.exe"
    run_bat = SRC_PATH / "scripts/start_utm.bat"
    stop_bat = SRC_PATH / "scripts/stop_utm.bat"

    def __init__(self, exit_flag: threading.Event):

        self.exit_flag = exit_flag
        self.logger = logging.getLogger('egais check')

    def start(self):
        """Запускает рабочий цикл в отдельном потоке."""
        threading.Thread(target=self._run, name="EgaisRunnerWorkerThread", daemon=True).start()

    def _run(self):
        """Основной цикл проверки и запуска службы."""
        self.logger.info("Egais Runner Worker запущен")
        next_check = time.monotonic()

        while not self.exit_flag.is_set():
            now = time.monotonic()

            if now >= next_check:
                if not self.is_running():
                    self.start_utm()

                next_check = now + self.CHECK_INTERVAL
            time.sleep(0.5)

    def is_running(self) -> bool:
        try:
            return win32serviceutil.QueryServiceStatus(self.SERVICE_NAME)[1] == 4
        except Exception:
            return any(
                p.info['name'] and p.info['name'].lower() == self.PROCESS_NAME
                for p in psutil.process_iter(['name'])
            )

    def _run_bat(self, bat_path) -> None:
        si = STARTUPINFO()
        si.dwFlags |= STARTF_USESHOWWINDOW
        si.wShowWindow = SW_HIDE

        subprocess.run(
            ["cmd.exe", "/c", str(bat_path)],
            startupinfo=si,
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True,
            text=True,
            encoding="cp1251",
            check=True,
        )

    def start_utm(self, timeout=120):
        if self.is_running():
            return

        if not self.run_bat.exists():
            self.logger.warning(f"run.bat не найден по пути: {UTM_BAT}")
            return

        with Rutoken() as rt:
            state = rt.get_state()
            if not rt.is_valid(state):
                self.logger.warning('Рутокена нет или закончились сертификаты, запуск утм невозможен')
                return

        # logger.info("Запуск УТМ")
        self._run_bat(self.run_bat)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_running():
                return
            sleep(1)

        self.logger.warning("UTM start timeout")
