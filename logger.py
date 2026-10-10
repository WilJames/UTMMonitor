import logging
import configparser
import atexit
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler
import threading
import time
from const import CONFIG_FILE


# ======================================================
# НАСТРОЙКИ (менять ТОЛЬКО ЭТО)
# ======================================================

LOGGER_NAME = "egais check"

LOG_FORMAT = ( "%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s" )

DEFAULT_LOG_LEVEL = logging.INFO
BACKUP_DAYS = 7
HOT_RELOAD_INTERVAL = 5  # секунд

# Где лежит logger.py → рядом папка logs
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

# Опционально: config.ini с уровнем логирования
# CONFIG_FILE = BASE_DIR / "config.ini"

# ======================================================
# ВНУТРЕННЯЯ ЛОГИКА (НЕ ТРОГАТЬ)
# ======================================================

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _get_log_level() -> int:
    if not CONFIG_FILE.exists():
        return DEFAULT_LOG_LEVEL

    try:
        config = configparser.ConfigParser()
        config.read(CONFIG_FILE, encoding="utf-8")

        level = config.get(
            "DEFAULT",
            "log_level",
            fallback=""
        ).upper()

        return _LOG_LEVELS.get(level, DEFAULT_LOG_LEVEL)

    except (OSError, configparser.Error):
        return DEFAULT_LOG_LEVEL


def _watch_log_level(logger: logging.Logger):
    last_level = logger.level

    while True:
        time.sleep(HOT_RELOAD_INTERVAL)

        new_level = _get_log_level()
        if new_level != last_level:
            logger.setLevel(new_level)
            logger.info(
                "Log level changed to %s",
                logging.getLevelName(new_level)
            )
            last_level = new_level


def _create_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)

    if getattr(logger, "_configured", False):
        return logger

    logger.setLevel(_get_log_level())
    logger.propagate = False

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_file = LOG_DIR / "logfile.log"

    handler = TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=BACKUP_DAYS,
        encoding="utf-8",
        delay=True,
        utc=False,
    )

    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)

    def _shutdown():
        for h in logger.handlers[:]:
            try:
                h.flush()
                h.close()
            finally:
                logger.removeHandler(h)

    atexit.register(_shutdown)

    logger._configured = True  # type: ignore

    # горячая смена режима лога из confing.ini
    watcher = threading.Thread(
        target=_watch_log_level,
        args=(logger,),
        daemon=True
    )
    watcher.start()

    return logger


# ======================================================
# ГОТОВЫЙ ЛОГГЕР
# ======================================================

logger = _create_logger()
