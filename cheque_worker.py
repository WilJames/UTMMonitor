import threading
import time
import arrow
import logging
import httpx
import const
from typing import List
from db_cheque import ChequeDataBase
from chequeclass import parse_cheques, find_cheques_dekl, Cheque
from config import ConfigManager

from const import UTM_LOG, UTM_ARCHIVED, URL_SERVER


logging.getLogger('httpx').setLevel(logging.WARNING)
debug_flag = False


class ChequeWorker:
    """Фоновый обработчик чеков из логов."""

    CHECK_INTERVAL = 10  # Интервал проверки в секундах
    TICK = 0.5    # Шаг таймера в секундах

    def __init__(self, exit_flag: threading.Event):

        self.exit_flag = exit_flag
        self.logger = logging.getLogger('egais check')
        self.config = ConfigManager()

    def start(self):
        """Запускает рабочий цикл в отдельном потоке."""
        threading.Thread(target=self._run, name="ChequeWorkerThread", daemon=True).start()
        self.logger.info("Cheque Worker запущен")

    def _run(self):
        """Основной рабочий цикл."""
        try:
            next_check_time = time.monotonic()
            next_day_check = time.monotonic()
            next_dekl_check = time.monotonic()

            while not self.exit_flag.is_set():
                cheques_total = []

                if time.monotonic() >= next_day_check:
                    try:
                        c1 = self._process_archived_cheques()  # Обработка архивов при старте
                        cheques_total.extend(c1)
                    except Exception as e:
                        self.logger.error(f'Глобальная ошибка в ChequeWorker обработка архивов: {e}')

                    next_day_check = time.monotonic() + (60 * 60 * 6)  # следующая проверка чеков в архивах каждые 6 часов

                if time.monotonic() >= next_dekl_check:
                    try:
                        c2 = self._process_dekl_cheques()  # Обработка декларанта при старте
                        cheques_total.extend(c2)
                    except Exception as e:
                        self.logger.error(f'Глобальная ошибка в ChequeWorker обработка архивов декларанта: {e}')

                    next_dekl_check = time.monotonic() + (60 * 60 * 6)  # следующая проверка чеков в декларанте каждые 6 часов

                if time.monotonic() >= next_check_time:
                    try:
                        c3 = self._process_current_cheques()
                        cheques_total.extend(c3)
                    except Exception as e:
                        self.logger.error(f'Глобальная ошибка в ChequeWorker: {e}')

                    next_check_time = time.monotonic() + self.CHECK_INTERVAL

                if cheques_total:
                    self.add_new_and_send(cheques_total)

                time.sleep(self.TICK)  # Шаг таймера

        except Exception as e:
            self.logger.error(f"Критическая ошибка в ChequeWorker: {e}")

    def get_dekl_path(self):
        base = const.SYSTEM_DRIVE
        candidates = [
            base / "/Program Files (x86)" / "Deklarant",
            base / "/Program Files" / "Deklarant",
        ]

        for path in candidates:
            if path.exists():
                return path

        return None

    def _process_dekl_cheques(self) -> List[Cheque]:
        try:
            dekl_path = self.get_dekl_path()
            if not dekl_path:
                self.logger.debug('Папки декларанта нет на этом компьютере')
                return []

            INN = self.config.inn
            EGAIS_ID = self.config.egais_id

            if not EGAIS_ID or not INN:
                self.logger.debug("Нет егаис ид или ИНН для поиска папки декларанта с чеками")
                return []

            check_dir = dekl_path / INN / 'XML' / EGAIS_ID / 'check'
            if not check_dir.exists():
                self.logger.debug(f"Папка с чеками декларанта не найдена: {check_dir}")
                return []

            self.logger.debug('Обработка чеков из декларанта запущена')
            cheques = find_cheques_dekl(check_dir)

            if cheques:
                self.logger.debug(f'Найдено в декларанте чеков: {len(cheques)}')
                return cheques
            else:
                return []

        except Exception as e:
            self.logger.error(f'Ошибка обработки в декларанта: {e}')
            return []

    def _process_archived_cheques(self) -> List[Cheque]:  # type: ignore
        try:
            if not UTM_ARCHIVED.exists():
                self.logger.debug(f"Папка архивов {UTM_ARCHIVED} не найдена")
                return []

            for log_gz in UTM_ARCHIVED.glob("transport_info*.log.gz"):
                self.logger.debug(f"Обработка архива: {log_gz}")

                cheques = parse_cheques(log_gz)
                if cheques:
                    return cheques
                else:
                    return []

        except Exception as e:
            self.logger.error(f'Ошибка при разборе и отравке чеков из архива: {e}')
            return []

    def _process_current_cheques(self) -> List[Cheque]:
        try:
            if not UTM_LOG.exists():
                self.logger.warning(f'Log file {UTM_LOG} not found')
                return []

            cheques = parse_cheques(UTM_LOG)

            if cheques:
                return cheques
            else:
                return []

        except Exception as e:
            self.logger.error(f'Ошибка при парсинге чеков из лога: {e}')
            return []

    def add_new_and_send(self, cheques: List[Cheque]):
        EGAIS_ID = self.config.egais_id
        if not EGAIS_ID:
            self.logger.debug("Нет Егаис ИД для вставки новых чеков")
            return

        db = None

        try:
            db = ChequeDataBase(EGAIS_ID)

            db.add_cheques(cheques)
            self._send_cheques(db)

        except Exception as e:
            self.logger.error(f"При добавлении нового чека или отправки новых чеков: {e}")
        finally:
            if db:
                db.close()
                self.logger.debug("База данных закрыта")

    def _send_cheques(self, db: ChequeDataBase):
        """Отправляет неотправленные чеки на сервер."""
        new_cheques = db.get_unsend_cheques()
        if not new_cheques:
            return

        self.logger.info(f"Найдено новых чеков для отправки: {len(new_cheques)}")
        send_ok = 0

        for cheque in new_cheques:
            if debug_flag:
                result_sending = 200
                # print("Эмуляция отправки чека")
            else:
                result_sending = self._send_to_server(cheque)

            if result_sending == 200:
                send_ok += 1
                db.set_cheque_send(cheque)

        self.logger.info(f'Успешно отправлено: {send_ok}/{len(new_cheques)}')

    def _send_to_server(self, cheque: Cheque) -> int:
        """Отправляет чек на сервер в сжатом виде.

        Args:
            cheque: Объект Cheque для отправки.

        Returns:
            int: Код состояния HTTP (200 — успех, -1 — ошибка).
        """
        if not self.config.egais_id:
            self.logger.warning("Отсутствует egais_id, отправка чека невозможна")
            return -1

        try:
            # Подготовка данных
            compressed_data = cheque.json_gzip()
            headers = self.config.get_headers({'Content-Encoding': 'gzip'})

            # Форматирование даты с обработкой ошибки
            try:
                datestr = arrow.get(cheque.dt, 'DDMMYYHHmmss').format('YYYY-MM-DD HH:mm:ss')
            except arrow.ParserError:
                datestr = cheque.dt
                self.logger.debug(f"Не удалось распарсить дату чека {cheque.dt}, используется исходная")

            # Логирование перед отправкой
            log_msg = f"Отправка чека: ID={cheque.log_id}, Type={cheque.type}, Date={datestr}, Num={cheque.n_cheque}"
            self.logger.info(log_msg)

            # Адаптивный таймаут: 10 секунд на подключение, 30 секунд на запись
            timeout = httpx.Timeout(10, write=30)
            with httpx.Client(transport=httpx.HTTPTransport(retries=2)) as client:
                response = client.post(
                    f"{URL_SERVER}/rclient/sellcheque",
                    data=compressed_data,  # type: ignore
                    headers=headers,
                    timeout=timeout
                )
                status_code = response.status_code

                if status_code == 200:
                    self.logger.info(f"Чек {cheque.log_id} успешно отправлен")
                else:
                    self.logger.warning(f"Ошибка отправки чека {cheque.log_id}, код: {status_code}, ответ: {response.text[:100]}")
                return status_code

        except httpx.TimeoutException as e:
            self.logger.error(f"Таймаут при отправке чека {cheque.log_id}: {e}")
            return -1
        except httpx.RequestError as e:
            self.logger.error(f"Ошибка сети при отправке чека {cheque.log_id}: {e}")
            return -1
        except Exception as e:
            self.logger.error(f"Неизвестная ошибка при отправке чека {cheque.log_id}: {type(e).__name__}: {e}")
            return -1
