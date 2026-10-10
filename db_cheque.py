import sqlite3
import arrow
from pathlib import Path
from chequeclass import Cheque, Bottle
from typing import List, Tuple, Optional
import logging
from const import SRC_PATH


class DataBase:
    """Базовый класс для работы с SQLite базами данных."""

    def __init__(self, egais_id: str, db_name: str):
        self.logger = logging.getLogger('egais check')

        self.db_name: str = db_name
        self._db_path: Optional[Path] = None
        self._connection: Optional[sqlite3.Connection] = None

        self._set_db_path(egais_id)

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self._create_connect()
        return self._connection  # type: ignore

    @property
    def cursor(self) -> sqlite3.Cursor:
        return self.connection.cursor()

    def commit(self):
        """Коммитит изменения в базе данных."""
        try:
            self.connection.commit()
        except sqlite3.Error as e:
            self.logger.error(f"Ошибка commit в {self.db_name}: {e}")

    def rollback(self):
        try:
            self.connection.rollback()
        except sqlite3.Error as e:
            self.logger.error(f"Ошибка rollback в {self.db_name}: {e}")

    def close(self):
        if self._connection:
            try:
                self._connection.close()
                self.logger.debug(f"Соединение с {self.db_name} закрыто")
            except sqlite3.Error as e:
                self.logger.error(f"Ошибка закрытия {self.db_name}: {e}")
            finally:
                self._connection = None

    def _set_db_path(self, egais_id: str):
        path = SRC_PATH / 'data' / egais_id
        path.mkdir(parents=True, exist_ok=True)

        self._db_path = path / self.db_name
        self._create_connect()

    def _create_connect(self):
        """Создает соединение с базой данных."""
        self.logger.debug(f"Создание соединения с {self.db_name}")

        try:
            self._connection = sqlite3.connect(self._db_path)  # type: ignore
        except sqlite3.Error as e:
            self.logger.error(f"Не удалось подключиться к {self.db_name}: {e}")
            # raise


class ChequeDataBase(DataBase):
    """Класс для работы с базой данных чеков."""

    def __init__(self, EGAIS_ID: str):
        super().__init__(EGAIS_ID, 'cheque.db')

        self.create_tables_if_not_exist()
        self.clear_old_cheque()

    def create_tables_if_not_exist(self):
        """Создает таблицы, если они не существуют."""
        try:
            cursor = self.cursor

            # Таблица для чеков с добавленным столбцом send_status
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS Cheques (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                dt TEXT NOT NULL,
                n_kassa TEXT NOT NULL,
                n_smena TEXT NOT NULL,
                n_cheque TEXT NOT NULL,
                send_status INTEGER NOT NULL DEFAULT 0
            )
            ''')

            # Таблица для бутылок, связанная с таблицей чеков
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS Bottles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cheque_id INTEGER NOT NULL,
                barcode TEXT NOT NULL,
                price REAL NOT NULL,
                volume REAL,
                FOREIGN KEY (cheque_id) REFERENCES Cheques(id) ON DELETE CASCADE
            )
            ''')

            self.commit()
        except Exception as e:
            self.logger.error(f'Ошбика создания пустых баз: {e}')

    def clear_old_cheque_old(self):
        """Удаляет отправленные чеки старше 90 дней."""
        self.logger.debug("Очистка старых чеков")

        try:
            cursor = self.cursor

            cursor.execute("DELETE FROM Cheques WHERE DATE(log_date) < DATE('now', '-90 days') AND send_status = 1")
            self.logger.debug(f"Удалено {cursor.rowcount} старых чеков")
            self.commit()
        except Exception as e:
            self.rollback()
            self.logger.debug(f'Ошибка удаления чеков старше 90 дней, откат изменений: {e}')

    def clear_old_cheque(self):
        """Удаляет отправленные чеки старше 60 дней по полю dt (формат DDMMYYHHmmss)."""
        self.logger.debug("Очистка старых чеков")

        try:
            cursor = self.cursor

            # Пороговая дата — 60 дней назад
            threshold = arrow.now().shift(days=-60)

            # Получаем отправленные чеки
            cursor.execute("SELECT id, dt FROM Cheques WHERE send_status = 1")
            rows = cursor.fetchall()

            ids_to_delete = []

            for cheque_id, dt_str in rows:
                try:
                    cheque_time = arrow.get(dt_str, "DDMMYYHHmmss")
                    if cheque_time < threshold:
                        ids_to_delete.append(cheque_id)
                except Exception as parse_error:
                    self.logger.debug(f"Ошибка парсинга dt='{dt_str}' для cheque_id={cheque_id}: {parse_error}")

            if ids_to_delete:
                try:
                    cursor.executemany("DELETE FROM Cheques WHERE id = ?", [(cid,) for cid in ids_to_delete])
                    self.commit()

                    self.logger.debug(f"Удалено {cursor.rowcount} старых чеков")
                except Exception as e:
                    self.rollback()
                    self.logger.debug(f"Ошибка удаления чеков старше 90 дней, откат изменений: {e}")
            else:
                self.logger.debug("Нет чеков для удаления")

        except Exception as e:
            self.logger.debug(f"Ошибка во время удаления чеков старше 90 дней: {e}")

    def cheque_exists(self, cheque: Cheque) -> bool:
        """Проверяет, существует ли чек в базе."""
        try:
            cursor = self.cursor
            cursor.execute("SELECT 1 FROM Cheques WHERE log_id = ?", (cheque.log_id,))
            return bool(cursor.fetchone())
        except Exception as e:
            self.logger.error(f'Ошибка выполенения запроса проверки сущестования чека в базе: {e}')
            return False

    def set_cheque_send(self, cheque: Cheque):
        """Помечает чек как отправленный."""
        self.logger.debug(f"Установка статуса отправки для чека {cheque.log_id}")

        try:
            self.cursor.execute("UPDATE Cheques SET send_status = 1 WHERE log_id = ?", (cheque.log_id,))
            self.commit()
        except Exception as e:
            self.logger.error(f'Ошибка установки статуса отправки чека: {e}')
            self.rollback()

    def add_cheques(self, cheques_list: List[Cheque]):
        """Добавляет список чеков в базу."""
        if not cheques_list:
            return

        cheques_list_new = [x for x in cheques_list if not self.cheque_exists(x)]

        if len(cheques_list_new) > 0:
            self.logger.info(f'Новых чеков для вставки в базу: {len(cheques_list_new)}')

            for cheque in cheques_list_new:
                self.add_cheque(cheque)

    def add_cheque(self, cheque: Cheque):
        if self.cheque_exists(cheque):
            return

        try:
            datestr = arrow.get(cheque.dt, 'DDMMYYHHmmss').format('YYYY-MM-DD HH:mm:ss')
        except arrow.ParserError:
            datestr = cheque.dt

        self.logger.info(f"Добавление нового чека: Type={cheque.type}, Date={datestr}, Num={cheque.n_cheque}")

        try:
            cursor = self.cursor

            # Вставка чека в таблицу Cheques
            cursor.execute('''
            INSERT INTO Cheques (log_id, type, dt, n_kassa, n_smena, n_cheque, send_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (cheque.log_id, cheque.type, cheque.dt, cheque.n_kassa, cheque.n_smena, cheque.n_cheque, 0))

            # Получаем ID вставленного чека
            cheque_id = cursor.lastrowid

            bottles_data = [(cheque_id, bottle.barcode, bottle.price, bottle.volume) for bottle in cheque.bottles]

            # Мультивставка бутылок в таблицу Bottles
            cursor.executemany('''
            INSERT INTO Bottles (cheque_id, barcode, price, volume)
            VALUES (?, ?, ?, ?)
            ''', bottles_data)

            self.commit()
        except Exception as e:
            self.logger.error(f"Ошибка вставки нового чека в базу: {e}")
            self.rollback()

    def get_unsend_cheques(self) -> List[Cheque]:
        """Возвращает список неотправленных чеков."""
        cheques = []

        try:
            cursor = self.cursor
            cursor.execute("SELECT * FROM Cheques WHERE send_status = 0")
            cheques_data = cursor.fetchall()

            for data in cheques_data:
                cheque_id = data[0]
                cursor.execute("SELECT * FROM Bottles WHERE cheque_id = ?", (cheque_id,))
                bottles_data = cursor.fetchall()

                bottles = [Bottle(barcode=b[2], price=b[3], volume=b[4]) for b in bottles_data]

                log_id = data[1]
                _type = data[2]
                dt = data[3]
                n_kassa = data[4]
                n_smena = data[5]
                n_cheque = data[6]

                cheque = Cheque(
                    type=_type,
                    dt=dt,
                    n_kassa=n_kassa,
                    n_smena=n_smena,
                    n_cheque=n_cheque,
                    log_id=log_id,
                    bottles=bottles
                )
                cheques.append(cheque)

        except Exception as e:
            self.logger.error(f"Ошибка выборки неотправленных чеков: {e}")

        if len(cheques) > 0:
            self.logger.debug(f"Выбрано {len(cheques)} неотправленных чеков")

        return cheques


class RestsDataBase(DataBase):
    """Класс для работы с базой данных остатков.
    CHECK_INTERVAL в секунах, 24 часа
    """

    def __init__(self, EGAIS_ID: str, CHECK_INTERVAL: Optional[int] = None):
        super().__init__(EGAIS_ID, 'rests.db')

        self.CHECK_INTERVAL = CHECK_INTERVAL or (24 * 60 * 60) + (5 * 60)  # 24 часа + 5 минут в секундах

        self.create_tables_if_not_exist()

    def create_tables_if_not_exist(self):
        """Создает таблицу остатков, если она не существует."""

        # Таблица для остатков бутылок
        try:
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS BottleStocks (
                ALCCODE TEXT NOT NULL,
                FA TEXT NOT NULL,
                FB TEXT NOT NULL,
                KOL INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                send_status INTEGER NOT NULL DEFAULT 0
            )
            ''')

            self.commit()
        except Exception as e:
            self.logger.error(f'Ошибка создания пустой таблицы остатков: {e}')

    def mark_as_sent(self):
        """Помечает все остатки как отправленные."""
        self.logger.debug("Пометка остатков как отправленных")

        try:
            cursor = self.cursor
            cursor.execute("UPDATE BottleStocks SET send_status = 1")
            self.logger.debug(f"Помечено {cursor.rowcount} записей")
            self.commit()
        except Exception as e:
            self.rollback()
            self.logger.debug(f'Ошибка пометки остатков как отправленных: {e}')

    def check_if_needs_to_send(self) -> bool:
        """Проверяет наличие неотправленных остатков."""

        try:
            cursor = self.cursor
            cursor.execute("SELECT COUNT(*) FROM BottleStocks WHERE send_status = 0")
            count = cursor.fetchone()[0]
            return count > 0
        except Exception as e:
            self.logger.error(f'Ошибка запроса проверки неотправленных остатков: {e}')
            return False

    # Обновление остатков бутылок
    def update_bottle_stocks(self, stocks: List[Tuple], updated_at: str):
        """Обновляет остатки в базе данных."""
        cursor = self.cursor

        try:
            cursor.execute("SELECT updated_at FROM BottleStocks LIMIT 1")
            current_update = cursor.fetchone()
        except Exception as e:
            current_update = None
            self.logger.error(f'Ошибка выборки последней даты обновления остатков: {e}')

        # Сравниваем даты как объекты datetime
        if current_update:
            try:
                current_dt = arrow.get(current_update[0])
                new_dt = arrow.get(updated_at)

                if current_dt >= new_dt:
                    return
            except arrow.ParserError as e:
                self.logger.error(f"Ошибка парсинга даты остатков: {e}")
                return

        self.logger.debug("Обновление остатков")

        try:
            # Очищаем старые остатки
            cursor.execute("DELETE FROM BottleStocks")

            # Вставляем данные пачкой
            cursor.executemany('''
            INSERT INTO BottleStocks (ALCCODE, FA, FB, KOL, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ''', stocks)

            self.commit()
            self.logger.info(f"Обновлено {cursor.rowcount} записей остатков")
        except Exception as e:
            self.logger.error(f'Ошибка очистки и вставки новых остатков: {e}')

    def need_send_new_query(self, last_send: Optional[arrow.Arrow] = None) -> bool:
        """Проверяет, нужно ли отправить новый запрос на остатки."""
        try:
            cursor = self.cursor
            cursor.execute("SELECT updated_at FROM BottleStocks LIMIT 1")
            result = cursor.fetchone()
        except Exception as e:
            result = None
            self.logger.error(f'Ошибка выборки последнего обновления остатков: {e}')

        current_time = arrow.utcnow().shift(hours=3)

        # Сравниваем даты как объекты datetime
        if result:
            try:
                last_update = arrow.get(result[0]) or last_send
                diff = (current_time - last_update).total_seconds()
                return diff >= self.CHECK_INTERVAL
            except arrow.ParserError as e:
                self.logger.error(f"Ошибка парсинга даты последнего обновления: {e}")

        if last_send:
            diff = (current_time - last_send).total_seconds()
            return diff >= self.CHECK_INTERVAL

        return True

    def get_bottle_stocks(self) -> List[Tuple]:
        """
        Получает текущие остатки из базы.

        :return: Список кортежей, где каждый кортеж содержит (ALCCODE, FA, FB, KOL, updated_at).
        """
        try:
            cursor = self.cursor
            cursor.execute("SELECT ALCCODE, FA, FB, KOL, updated_at FROM BottleStocks")
            stocks = cursor.fetchall()

            self.logger.debug(f"Выбрано {len(stocks)} записей остатков")
            return stocks
        except sqlite3.Error as e:
            self.logger.error(f"Ошибка получения остатков: {e}")
            return []


class AmcDataBase(DataBase):
    """Класс для работы с базой данных остатков побутылочно."""
    CHECK_INTERVAL = 4

    def __init__(self, EGAIS_ID: str):
        super().__init__(EGAIS_ID, 'amc.db')

        self.create_tables_if_not_exist()

    def create_tables_if_not_exist(self):
        """Создает таблицу остатков, если она не существует."""

        # Таблица для остатков бутылок
        try:
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS AMC (
                CODE TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                send_status INTEGER NOT NULL DEFAULT 0
            )
            ''')

            self.commit()
        except sqlite3.Error as e:
            self.logger.error(f"Ошибка создания таблицы: {e}")

    def mark_as_sent(self):
        """Помечает все остатки как отправленные."""
        try:
            self.logger.debug("Пометка побутылочных остатков как отправленных")
            cursor = self.cursor
            cursor.execute("UPDATE AMC SET send_status = 1")
            self.logger.debug(f"Помечено {cursor.rowcount} записей")
            self.commit()
        except sqlite3.Error as e:
            self.logger.error(f"Ошибка при пометке остатков: {e}")

    def check_if_needs_to_send(self) -> bool:
        """Проверяет наличие неотправленных остатков."""
        try:
            cursor = self.cursor
            cursor.execute("SELECT COUNT(*) FROM AMC WHERE send_status = 0")
            count = cursor.fetchone()[0]
            return count > 0
        except sqlite3.Error as e:
            self.logger.error(f"Ошибка проверки неотправленных остатков: {e}")
            return False

    # Обновление остатков бутылок
    def update_bottle_stocks(self, stocks: List[str]):
        """Обновляет остатки в базе данных."""
        if not stocks:
            return

        self.logger.debug("Обновление побутылочных остатков")

        try:
            cursor = self.cursor
            # Очищаем полностью старые остатки

            cursor.execute("DELETE FROM AMC")

            # Вставляем новые данные пачкой
            cursor.executemany("INSERT INTO AMC (CODE) VALUES (?)", [(stock.strip(),) for stock in stocks])

            self.commit()
            self.logger.info(f"Обновлено {cursor.rowcount} записей остатков")
        except sqlite3.Error as e:
            self.logger.error(f"Ошибка обновления остатков: {e}")
            self.rollback()

    def get_bottle_stocks(self) -> List[str]:
        """
        Получает текущие остатки из базы.

        :return: Список строк с кодами остатков (CODE).
        """
        self.logger.debug("Получение остатков")

        try:
            cursor = self.cursor
            cursor.execute("SELECT CODE FROM AMC")
            stocks = [row[0].strip() for row in cursor.fetchall()]
            self.logger.debug(f"Всего бутылок: {len(stocks)}")
            return stocks
        except sqlite3.Error as e:
            self.logger.error(f"Ошибка получения остатков: {e}")
            return []

    def is_data_outdated(self) -> bool:
        """
        Проверяет, устарели ли данные в таблице (обновлены более N часов назад).

        :return: True, если данные старше N часов, иначе False.
        """
        try:
            cursor = self.cursor
            cursor.execute("SELECT updated_at FROM AMC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                self.logger.debug("Нет записей в таблице AMC — считаем как устаревшие")
                return True

            updated_at = arrow.get(row[0])  # Автоматически определяет формат
            now = arrow.utcnow()  # текущее время в UTC
            outdated = now.shift(hours=-self.CHECK_INTERVAL) > updated_at

            self.logger.debug(f"Последнее обновление остатков помарочно: {updated_at} (устарели: {outdated})")
            return outdated

        except Exception as e:
            self.logger.error(f"Ошибка при проверке актуальности данных помарочных остатков: {e}")
            return True  # на всякий случай считаем устаревшими при ошибке
