import aiohttp
import asyncio
import httpx
import gzip
import time
import random
import threading
import logging
from typing import Dict, Tuple, List, Any
from logger import logger
from config import ConfigManager
from const import URL_SERVER, MARKS_RESULT

logging.getLogger('httpx').setLevel(logging.WARNING)

URL_UTM = 'http://127.0.0.1:8080'
# URL_UTM = 'http://192.0.0.177:8080'
MAX_RETRIES = 10  # Максимальное количество попыток
MAX_CONCURRENT_TASKS = 15  # Ограничение на количество одновременно выполняемых задач
BASE_DELAY = 1.0
MAX_DELAY = 60.0
MIN_CONCURRENT_TASKS = 5
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
        response = httpx.head(f'{URL_UTM}/home', timeout=timeout)
        return response.status_code == 200
    except (httpx.RequestError, httpx.TimeoutException):
        return False


async def adaptive_sleep(attempt: int, base_delay: float = BASE_DELAY, max_delay: float = MAX_DELAY):
    delay = min(max_delay, base_delay * (2 ** attempt))
    jitter = random.uniform(0, delay)
    await asyncio.sleep(jitter)


async def fetch_mark(session: aiohttp.ClientSession, mark: str, retries: int = MAX_RETRIES) -> Tuple[Dict[str, Any], int]:
    kol_errors = 0

    for attempt in range(retries):
        try:
            async with session.get(f'{URL_UTM}/api/mark/check', params={'code': mark}, timeout=request_timeout) as response:
                if response.status != 200:
                    raise ValueError(f"Статус {response.status}: {await response.text()}")

                result: Dict[str, Any] = await response.json()
                logger.debug(f"Марка проверена успешно {mark.ljust(150)}, попытка {attempt + 1}")

                return result, kol_errors
        except Exception as e:
            kol_errors += 1

            if attempt in {0, 4, 9, 24} or attempt % 25 == 0:
                logger.debug(f"Ошибка проверки марки {mark.ljust(150)}, попытка {attempt + 1}/{retries}: {type(e).__name__}: {e}")

            if attempt < retries - 1:  # Не ждем после последней попытки
                await adaptive_sleep(kol_errors)

    logger.warning(f"Исчерпаны {retries} попыток для марки {mark}")
    return {'owner': True, 'code': f'{mark} - error'}, kol_errors


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


async def check_marks(marks_text: str) -> str:
    logger.debug("Запуск проверки марок")
    if not is_utm_online():
        logger.debug("UTM не доступен")
        return "УТМ не запущен или нет к нему доступа!"

    try:
        codes = list(set(x.upper().strip() for x in marks_text.split()))
        if not codes:
            logger.debug("Список марок пуст")
            return "Нет марок для проверки"

        kol_marks = len(codes)
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
                chunk_free, chunk_sold, errors_kount = [], [], []
                tasks = [process_mark(session, mark, chunk_free, chunk_sold, errors_kount, semaphore) for mark in chunk]
                await asyncio.gather(*tasks)

                free_marks.extend(chunk_free)
                sold_marks.extend(chunk_sold)

            concurrency = adjust_concurrency(concurrency, sum(errors_kount), len(chunk))
            logger.info(f"Текущий уровень параллелизма: {concurrency}, было ошибок: {sum(errors_kount)}")

            if free_marks:
                with open('temp_chank.txt', 'w', encoding='utf-8') as f:
                    f.write('\n'.join(free_marks))

            if i + CHUNK_SIZE < kol_marks:
                logger.info(f"Ожидание {DELAY_BETWEEN_CHUNKS} сек перед следующим чанком...")
                await asyncio.sleep(DELAY_BETWEEN_CHUNKS)

            if not is_utm_online():
                logger.warning("UTM временно не отвечает. Ожидание 30 сек перед повтором...")
                await asyncio.sleep(30)

        logger.info(f"Проверка завершена: свободных марок: {len(free_marks)}, проданных: {len(sold_marks)}")
        return '\n'.join(free_marks) if free_marks else "Все марки проданы"

    except Exception as e:
        logger.error(f"Неизвестная ошибка при проверке марок: {type(e).__name__}: {e}")
        return f"Ошибка при выполнении запросов проверки марок: {e}"


class MarkWorker:
    """## Воркер для проверки марок из папки на сервере
    """
    def __init__(self, exit_flag: threading.Event):
        self.exit_flag = exit_flag
        self.logger = logging.getLogger('egais check')
        self.loop = asyncio.new_event_loop()
        self.config = ConfigManager()

    def set_url_utm(self):
        global URL_UTM

        URL_UTM = self.config.utm_url

    def start(self):
        """Запускает рабочий цикл в отдельном потоке."""
        self.set_url_utm()

        worker_thread = threading.Thread(target=self._run, name="MarkWorkerThread", daemon=True)
        worker_thread.start()
        self.logger.info("Mark Worker запущен")

    def start_manual(self):
        """Основной рабочий цикл."""
        self.logger.setLevel(logging.DEBUG)
        asyncio.set_event_loop(self.loop)

        print('Ручная проверка марок')

        try:
            # Проверка сохраненного файла
            # if MARKS_RESULT.exists():
            #     self._retry_saved_result()

            # Проверка новых марок
            with open('m.txt', 'r', encoding='utf-8') as f:
                marks = f.read()

            print(f'Марок загружено: {len(marks.split())}')

            if marks:
                print('Проверка запущена')

                result = self.loop.run_until_complete(check_marks(marks))

                with open('result.txt', 'w', encoding='utf-8') as f:
                    f.write(result)

        except Exception as e:
            self.logger.error(f"Ошибка в цикле проверки марок: {e}")

    def _run(self):
        """Основной рабочий цикл."""
        CHECK_INTERVAL = 60 * 5  # каждые 5 минут
        TICK = 0.5    # Шаг таймера в секундах

        asyncio.set_event_loop(self.loop)

        next_check_time = time.monotonic()

        while not self.exit_flag.is_set():
            if time.monotonic() >= next_check_time:
                try:
                    # Проверка сохраненного файла
                    if MARKS_RESULT.exists():
                        self._retry_saved_result()

                    # Получение новой пачки марок для ручной проверки из папки на сервере
                    status_work, marks = self._get_makrs_work()

                    if status_work == 200 and marks:
                        result = self.loop.run_until_complete(check_marks(marks))
                        self._send_result_with_retries(result)

                except Exception as e:
                    self.logger.error(f"Ошибка в цикле проверки марок: {e}")

                next_check_time = time.monotonic() + CHECK_INTERVAL

            time.sleep(TICK)  # Шаг таймера

    def _retry_saved_result(self):
        """Пытается отправить сохраненный результат из файла и удаляет его при успехе."""
        try:
            with MARKS_RESULT.open('r', encoding='utf-8') as f:
                saved_result = f.read().strip()

            if saved_result:
                self.logger.info("Обнаружен сохраненный результат, попытка повторной отправки")
                status = self._send_file_to_server(saved_result)

                if status == 200:
                    MARKS_RESULT.unlink()
                    self.logger.info(f"Сохраненный результат успешно отправлен, файл {MARKS_RESULT} удален")
                else:
                    self.logger.warning(f"Не удалось отправить сохраненный результат, код: {status}")

        except (IOError, OSError) as e:
            self.logger.error(f"Ошибка чтения сохраненного файла {MARKS_RESULT}: {e}")

    def _send_result_with_retries(self, result: str, max_retries: int = 5, retry_interval: float = 10):
        """Отправляет результат проверки с повторными попытками."""
        if not result:
            return

        retry_count = 0
        # retry_count = max_retries  # для тестов на сохранение и переотправку
        while retry_count < max_retries:
            try:
                ret_status = self._send_file_to_server(result)
                if ret_status == 200:
                    self.logger.debug("Результат проверки марок успешно отправлен")
                    break

                self.logger.warning(f"Ошибка отправки результата, код: {ret_status}")
            except Exception as e:
                self.logger.error(f"Ошибка отправки результата: {e}")

            retry_count += 1
            time.sleep(retry_interval)
        else:
            self.logger.error(f"Не удалось отправить результат после {max_retries} попыток, сохраняю в файл.")
            self._save_result_to_file(result)

    def _save_result_to_file(self, result: str):
        """Сохраняет результат проверки в файл."""
        try:
            MARKS_RESULT.parent.mkdir(parents=True, exist_ok=True)  # Создаем директорию, если её нет
            with MARKS_RESULT.open('w', encoding='utf-8') as f:
                f.write(result)
            self.logger.info(f"Результат сохранен в файл {MARKS_RESULT}")
        except (IOError, OSError) as e:
            self.logger.error(f"Ошибка сохранения результата в файл {MARKS_RESULT}: {e}")

    def _get_makrs_work(self) -> Tuple[int, str]:
        if not self.config.egais_id:
            self.logger.warning("Отсутствует egais_id, запрос на проверку марок невозможен")
            return -1, ''

        url = f"{URL_SERVER}/rclient/checkmarks"
        headers = self.config.get_headers({'Content-Encoding': 'gzip'})
        timeout = httpx.Timeout(10, read=60)

        try:
            self.logger.debug("Запрос марок для проверки из файла с сервера")
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url, headers=headers)
                if response.status_code in (200, 204):
                    # 200 - есть данные, 204 - нет данных (успех без содержимого)
                    text = response.text.strip() if response.status_code == 200 else ''
                    self.logger.debug(f"Ответ запроса марок из файла, Код ответа:{response.status_code}, Марок: {len(text.split())}")
                    return response.status_code, text
                # Если код не 200 или 204, считаем это ошибкой
                self.logger.warning(f"Ошибка сервера: {response.status_code}, ответ: {response.text[:100]}")
                return response.status_code, ''

        except httpx.TimeoutException as e:
            self.logger.error(f"Таймаут при запросе марок: {e}")
            return -1, ''
        except httpx.RequestError as e:
            self.logger.error(f"Ошибка сети при запросе марок: {e}")
            return -1, ''
        except Exception as e:
            self.logger.error(f"Неизвестная ошибка при запросе марок: {type(e).__name__}: {e}")
            return -1, ''

    def _send_file_to_server(self, data: str) -> int:
        if not self.config.egais_id:
            self.logger.warning("Отсутствует egais_id, отправка проверенных марок невозможна")
            return -1

        url = f'{URL_SERVER}/rclient/checkmarks'
        headers = self.config.get_headers({'Content-Encoding': 'gzip'})
        timeout = httpx.Timeout(10, write=120)

        try:
            compressed_data = gzip.compress(data.encode('utf-8'))
            self.logger.debug(f"Отправка данных марок: {len(data)} символов, сжато до {len(compressed_data)} байт")

            # Сжатие данных внутри try для обработки возможных ошибок
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, data=compressed_data, headers=headers)  # type: ignore
                response.raise_for_status()
                self.logger.debug(f"Данные марок успешно отправлены, код: {response.status_code}")
                return response.status_code

        except gzip.BadGzipFile as e:
            self.logger.error(f"Ошибка сжатия данных марок: {e}")
            return -1
        except httpx.HTTPStatusError as e:
            self.logger.warning(f"Ошибка сервера при отправке марок: {e.response.status_code}, ответ: {e.response.text[:100]}")
            return -1
        except httpx.TimeoutException as e:
            self.logger.error(f"Таймаут при отправке марок: {e}")
            return -1
        except httpx.RequestError as e:
            self.logger.error(f"Ошибка сети при отправке марок: {e}")
            return -1
        except Exception as e:
            self.logger.error(f"Неизвестная ошибка при отправке марок: {type(e).__name__}: {e}")
            return -1


if __name__ == '__main__':
    exit_flag = threading.Event()
    a = MarkWorker(exit_flag)
    a.start_manual()

    try:
        while not exit_flag.is_set():
            time.sleep(0.5)  # Поддержание главного потока

    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping the application... from main Ctrl+C or termination signal")
        exit_flag.set()
    except Exception as e:
        logger.error(f"Ошибка в основном потоке программы: {e}")
        exit_flag.set()
    finally:
        logger.info("Программа аварийно закрыта")
