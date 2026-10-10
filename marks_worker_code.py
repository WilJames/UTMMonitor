import aiohttp
import asyncio
import gzip
import json
import threading
import time
import random
import logging
import httpx
from typing import Dict, List, Tuple, Any
from logger import logger

from config import ConfigManager
from const import URL_SERVER
from db_cheque import AmcDataBase


logging.getLogger('httpx').setLevel(logging.WARNING)


URL_UTM = 'http://127.0.0.1:8080'
MAX_RETRIES = 10  # Максимальное количество попыток
MIN_CONCURRENT_TASKS = 5
MAX_CONCURRENT_TASKS = 10  # Ограничение на количество одновременно выполняемых задач
BASE_DELAY = 1.0
MAX_DELAY = 60.0
CHUNK_SIZE = 50
DELAY_BETWEEN_CHUNKS = 10

# Таймауты для запроса: 10 секунд на соединение и отправку
request_timeout = aiohttp.ClientTimeout(
    total=None,  # Не ограничиваем общий таймаут, так как будем контролировать чтение отдельно
    connect=15.0,  # 60 секунд на установку соединения
    sock_connect=10.0,  # 10 секунд на подключение сокета
    sock_read=60.0  # 60 секунд на чтение ответа из сокета
)


def is_utm_online(timeout: int = 4) -> bool:
    """Проверяет доступность UTM."""
    try:
        response = httpx.get(f'{URL_UTM}/diagnosis', headers={"accept": "text/xml"}, timeout=timeout)
        return response.status_code == 200
    except (httpx.RequestError, httpx.TimeoutException):
        return False


async def adaptive_sleep(attempt: int, base_delay: float = BASE_DELAY, max_delay: float = MAX_DELAY):
    delay = min(max_delay, base_delay * (2 ** attempt))
    jitter = random.uniform(0, delay)
    await asyncio.sleep(jitter)


async def fetch_mark(session: aiohttp.ClientSession, mark: str, retries: int = MAX_RETRIES) -> Tuple[Dict, int]:
    kol_errors = 0

    for attempt in range(retries):
        try:
            async with session.get(f'{URL_UTM}/api/mark/check', params={'code': mark}, timeout=request_timeout) as response:
                if response.status != 200:
                    raise ValueError(f"Статус {response.status}: {await response.text()}")

                result = await response.json()
                logger.debug(f"Успешно {mark.ljust(150)}, попытка {attempt + 1}")

                return result, kol_errors
        except Exception as e:
            kol_errors += 1

            if attempt in {0, 4, 9, 24} or attempt % 25 == 0:
                logger.debug(f"Ошибка  {mark.ljust(150)}, попытка {attempt + 1}/{retries}: {type(e).__name__}: {e}")

            if attempt < retries - 1:  # Не ждем после последней попытки
                await adaptive_sleep(kol_errors)

    logger.warning(f"Исчерпаны {retries} попыток для марки {mark}")
    return {'owner': True, 'code': mark}, kol_errors


async def process_mark(session: aiohttp.ClientSession, mark: str, free_marks: List, sold_marks: List, errors_kount: List, semaphore: asyncio.Semaphore):
    async with semaphore:
        result_dict, kol_errors_value = await fetch_mark(session, mark)

    if kol_errors_value > 0:
        errors_kount.append(kol_errors_value)

    owner = result_dict.get('owner', True)

    if owner:
        free_marks.append(mark)
    else:
        sold_marks.append(mark)


def adjust_concurrency(current: int, errors: int, total: int, min_limit=MIN_CONCURRENT_TASKS, max_limit=MAX_CONCURRENT_TASKS) -> int:
    if total == 0:
        return current  # ничего не меняем

    error_rate = errors / total

    if error_rate > 0.3 and current > min_limit:
        return current - 1
    elif error_rate < 0.05 and current < max_limit:
        return current + 1

    return current


async def check_marks(codes: List[str]) -> List[str]:
    """Асинхронно проверяет список марок и возвращает результат."""
    logger.debug("Запуск проверки марок")

    if not is_utm_online():
        logger.debug("УТМ не запущен или нет к нему доступа!")
        return []

    kol_marks = len(codes)

    if kol_marks == 0:
        logger.debug("Список марок для проверки пуст")
        return []

    try:
        start_time = time.time()  # Засекаем начальное время
        logger.info(f"Начало проверки {kol_marks} марок")
        free_marks, sold_marks = [], []
        concurrency = MAX_CONCURRENT_TASKS
        total_chunks = (kol_marks + CHUNK_SIZE - 1) // CHUNK_SIZE

        for i in range(0, kol_marks, CHUNK_SIZE):
            chunk = codes[i:i + CHUNK_SIZE]
            current_chunk_num = i // CHUNK_SIZE + 1

            logger.info(f"Обработка чанка {current_chunk_num}/{total_chunks}: {len(chunk)} марок")
            semaphore = asyncio.Semaphore(concurrency)

            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=concurrency)) as session:
                logger.debug("Сессия aiohttp открыта")

                chunk_free, chunk_sold, errors_kount = [], [], []
                tasks = [process_mark(session, mark, chunk_free, chunk_sold, errors_kount, semaphore) for mark in chunk]
                await asyncio.gather(*tasks)

                free_marks.extend(chunk_free)
                sold_marks.extend(chunk_sold)

            concurrency = adjust_concurrency(concurrency, sum(errors_kount), len(chunk))
            logger.info(f"Текущий уровень параллелизма: {concurrency}, было ошибок: {sum(errors_kount)}")

            if i + CHUNK_SIZE < kol_marks:
                logger.info(f"Ожидание {DELAY_BETWEEN_CHUNKS} сек перед следующим чанком...")
                await asyncio.sleep(DELAY_BETWEEN_CHUNKS)

            if not is_utm_online():
                logger.warning("UTM временно не отвечает. Ожидание 30 сек перед повтором...")
                await asyncio.sleep(30)

        total_time = time.time() - start_time  # Общее время в секундах

        logger.info(f"Проверка завершена. Общее время проверки для {len(codes)} марок - {total_time:.2f} секунд")
        return free_marks

    except Exception as e:
        logger.error(f"Неизвестная ошибка при проверке марок: {type(e).__name__}: {e}")
        return []


class AmcWorker:
    """## Фоновый обработчик помарочных остатков
    Автоматическая проверка марок на остатках в егаисе
    """

    def __init__(self, exit_flag: threading.Event, first_run_after_sec=60 * 10):
        self.exit_flag = exit_flag
        self.logger = logging.getLogger('egais check')
        self.loop = asyncio.new_event_loop()
        self.config = ConfigManager()
        self.first_run_after_sec = first_run_after_sec
        self.set_url_utm()

    def set_url_utm(self):
        global URL_UTM

        URL_UTM = self.config.utm_url

    def start(self):
        """Запускает рабочий цикл в отдельном потоке."""
        threading.Thread(target=self._run, name=f"{self.__class__.__name__}Thread", daemon=True).start()
        self.logger.info(f"{self.__class__.__name__} запущен")

    def _run(self):
        """Основной рабочий цикл."""
        CHECK_SEND_INTERVAL = 60 * 5  # 1 раз в 5 минут
        CHECK_INTERVAL = 60 * 20  # Интервал проверки 1 раз 20 минут
        TICK = 0.5    # Шаг таймера в секундах

        asyncio.set_event_loop(self.loop)
        self.logger.debug('Ивент луп установлен')

        next_check_time = time.monotonic() + (self.first_run_after_sec)  # первый запуск через 10 минут после старта программы
        next_send_time = time.monotonic()

        while not self.exit_flag.is_set():
            if time.monotonic() >= next_check_time:
                try:
                    self._process()
                except Exception as e:
                    self.logger.error(f'Global error _process: {e}')

                next_check_time = time.monotonic() + CHECK_INTERVAL

            if time.monotonic() >= next_send_time:
                try:
                    self._check_and_send_server()
                except Exception as e:
                    self.logger.error(f'Global error _check_and_send_server: {e}')

                next_send_time = time.monotonic() + CHECK_SEND_INTERVAL

            time.sleep(TICK)  # Шаг таймера

    def _process(self):
        '''До думать вариант с пустыми остатками которые тоже могут быть'''
        egais_id = self.config.egais_id
        if not egais_id:
            self.logger.warning("Отсутствует egais_id, проверка марок невозможна")
            return

        settings = self._get_settings()
        if not settings.get('marks_check', False):
            self.logger.info('Пропуск помарочной проверки, выключено на сервере')
            return

        db = None

        try:
            db = AmcDataBase(egais_id)

            if db.is_data_outdated():  # проверяется в базе, что марки уже больше 4 часов не обновлялись
                self.logger.debug('Получение списка марок')
                codes = self._get_codes(egais_id)

                if len(codes) == 0:
                    return

                self.logger.info(f'Марок для проверки: {len(codes)}')

                result = self.loop.run_until_complete(check_marks(codes))

                if result:
                    db.update_bottle_stocks(result)

        except Exception as e:
            self.logger.error(f"Ошибка при получении и проверки марок: {e}")
        finally:
            if db is not None:
                db.close()
                self.logger.debug("База данных закрыта")

    def _check_and_send_server(self):
        egais_id = self.config.egais_id
        if not egais_id:
            self.logger.warning("Отсутствует egais_id, проверка марок невозможна")
            return

        db = None

        try:
            db = AmcDataBase(egais_id)

            if db.check_if_needs_to_send():
                result = db.get_bottle_stocks()

                if self._send_codes_server(result, egais_id):
                    db.mark_as_sent()

        except Exception as e:
            self.logger.error(f"Ошибка при отправке марок: {e}")
        finally:
            if db is not None:
                db.close()
                self.logger.debug("База данных закрыта")

    def _get_settings(self) -> Dict[str, Any]:
        url = f'{URL_SERVER}/rclient/get_settings_remote'

        try:
            headers = self.config.get_headers({'Content-Encoding': 'gzip'})

            with httpx.Client(timeout=10, transport=httpx.HTTPTransport(retries=3)) as client:
                response = client.get(url, headers=headers)

                if response.status_code == 200:
                    return response.json()
                else:
                    return {}

        except Exception as e:
            self.logger.error(f'Ошибка получения настроек с сервера: {e}')
            return {}

    def _get_codes(self, egais_id) -> List[str]:
        # запрос и получени json с документами которые надо отправить в УТМ
        timeout = httpx.Timeout(10, read=300)
        url = f'{URL_SERVER}/rclient/get-marks/{egais_id}'

        try:
            headers = self.config.get_headers({'Content-Encoding': 'gzip'})

            with httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=3)) as client:
                response = client.get(url, headers=headers)

                if response.status_code == 200:
                    return response.json()
                else:
                    return []

        except Exception as e:
            self.logger.error(f'Ошибка получения списка марок для проверки: {e}')
            return []

    def _send_codes_server(self, codes: List[str], egais_id: str):
        timeout = httpx.Timeout(10, write=300)
        url = f'{URL_SERVER}/rclient/get-marks/{egais_id}'

        try:
            json_data = json.dumps(codes, ensure_ascii=False)
            compressed_data = gzip.compress(json_data.encode('utf-8'))

            headers = self.config.get_headers({'Content-Encoding': 'gzip'})

            with httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=3)) as client:
                response = client.post(url, data=compressed_data, headers=headers)  # type: ignore

                if response.status_code == 200:
                    self.logger.debug("Марки успешно отправлены на сервер")
                    return True
                else:
                    self.logger.error(f"Ошибка отправки кодов, статус: {response.status_code}, ответ: {response.text}")
                    return False
        except Exception as e:
            self.logger.error(f'Ошибка при отправке марок: {e}')
            return False
