import time
import signal
import sys
import ctypes
import threading
from logger import logger

# ипорты после логера
from config import ConfigManager

from cheque_worker import ChequeWorker
from utm_marks_check import MarkWorker
from egais import EgaisDocWorker, EgaisRestWorker
from archive import LogWorker
from version_checker import VersionWorker
from egais_runner import EgaisRunnerWorker
from marks_worker_code import AmcWorker
from const import DISABLED_INN

# Флаг, который будет использоваться для выхода из основного цикла
exit_flag = threading.Event()


def get_mutex_name():
    return "Global\\UTMMonitorByJamesDev"


def already_running():
    mutex_name = get_mutex_name()
    try:
        # Попытка создания мьютекса
        _ = ctypes.windll.kernel32.CreateMutexA(None, False, mutex_name.encode())
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            # Если мьютекс уже существует, программа уже запущена
            return True
        else:
            return False
    except Exception:
        return False


def handler_stop_signals(signum, frame):
    global exit_flag
    logger.info(f"Программа закрыта с сигналом: {signal.Signals(signum).name} ({signum})")
    exit_flag.set()


# Регистрируем обработчик сигналов
for sig in [signal.SIGINT,   # Прерывание (Ctrl+C)
            signal.SIGTERM,  # Завершение
            signal.SIGABRT,  # Аварийное завершение
            signal.SIGBREAK]:  # Прерывание (Ctrl+Break)
    try:
        signal.signal(sig, handler_stop_signals)
    except Exception as e:
        logger.warning(f"Сигнал закрытия не поддреживается {sig}: {e}")


def main():
    logger.info('=' * 60)
    logger.info("Приложение запущено")

    # первая проверка версии программы и сразу же обновление работает только для .exe
    if getattr(sys, 'frozen', False):
        version_worker = VersionWorker(exit_flag)
        if version_worker._check_and_download_update():
            version_worker._start_update()
            sys.exit(0)
        else:
            version_worker.start()

    config = ConfigManager()
    config.maintain_autostart()
    config.update_config_utm()
    config.update_data()

    if config.inn in DISABLED_INN:
        logger.info("Приложение выключается, ИНН не валидное")
        sys.exit(0)

    egais_runner_worker = EgaisRunnerWorker(exit_flag)
    egais_runner_worker.start()

    worker_cheque = ChequeWorker(exit_flag)
    worker_cheque.start()

    worker_log = LogWorker(exit_flag)
    worker_log.start()

    worker_mark = MarkWorker(exit_flag)
    worker_mark.start()

    worker_egais_doc = EgaisDocWorker(exit_flag)
    worker_egais_doc.start()

    worker_egais_rest = EgaisRestWorker(exit_flag)
    worker_egais_rest.start()

    worker_amc = AmcWorker(exit_flag)  # автоматом с сервера
    worker_amc.start()

    CHECK_INTERVAL = 60 * 5  # обновление конфига раз в 5 минут
    CHECK_INTERVAL_HOURS = 60 * 60  # обновление раз в час
    AUTORUN_CHECK_INTERVAL = 60  # проверка программы в автозагрузке, раз в 60 секунд

    now = time.monotonic()
    next_check_time = now + CHECK_INTERVAL  # Время следующей проверки
    next_autorun_check_time = now
    next_anydesk_check_time = now
    next_utmport_check_time = now

    try:
        while not exit_flag.is_set():
            now = time.monotonic()

            if config.inn in DISABLED_INN:
                config.disable_autostart()
                logger.info("Приложение выключается, ИНН не валидное")
                exit_flag.set()
                break

            if now >= next_utmport_check_time:
                config.update_utm_port()
                next_utmport_check_time = now + CHECK_INTERVAL_HOURS

            if now >= next_check_time:
                logger.debug('Цикл обновления конфига')
                config.update_utm_data()
                next_check_time = now + CHECK_INTERVAL  # Перенос времени следующей проверки

            if now >= next_autorun_check_time:
                config.maintain_autostart()
                next_autorun_check_time = now + AUTORUN_CHECK_INTERVAL

            if now >= next_anydesk_check_time:
                config.update_anydesk_id()
                next_anydesk_check_time = now + CHECK_INTERVAL_HOURS

            time.sleep(0.5)  # Поддержание главного потока
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping the application... from main Ctrl+C or termination signal")
        exit_flag.set()
    except Exception as e:
        logger.error(f"Ошибка в основном потоке программы: {e}")
        exit_flag.set()
    finally:
        logger.info("Программа аварийно закрыта")


if __name__ == '__main__':
    if already_running():
        sys.exit(1)

    main()
