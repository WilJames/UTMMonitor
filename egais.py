from typing import List, Tuple, Dict, Any, Optional
import arrow
import httpx
from io import StringIO
import xml.etree.ElementTree as ET
from config import ConfigManager
import time
import threading
import logging
from datetime import timedelta
from logger import logger
from const import URL_SERVER
from db_cheque import RestsDataBase
import json
import gzip
from const import SYSTEM_DRIVE, DEL_RESTS_AFTER_MINUTES


logging.getLogger('httpx').setLevel(logging.WARNING)

XML_QUERY = '''<?xml version="1.0" encoding="UTF-8"?>
<ns:Documents Version="1.0"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:ns="http://fsrar.ru/WEGAIS/WB_DOC_SINGLE_01"
    xmlns:qp="http://fsrar.ru/WEGAIS/QueryParameters">
    <ns:Owner>
        <ns:FSRAR_ID>{FSRAR_ID}</ns:FSRAR_ID>
    </ns:Owner>
    <ns:Document>
        <ns:QueryRests_Mini></ns:QueryRests_Mini>
    </ns:Document>
</ns:Documents>'''

XML_QUERY_V3 = '''<?xml version="1.0" encoding="UTF-8"?>
<ns:Documents Version="1.0"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:ns="http://fsrar.ru/WEGAIS/WB_DOC_SINGLE_01"
    xmlns:qp="http://fsrar.ru/WEGAIS/QueryParameters">
    <ns:Owner>
        <ns:FSRAR_ID>{FSRAR_ID}</ns:FSRAR_ID>
    </ns:Owner>
    <ns:Document>
        <ns:QueryRests_Mini_v3></ns:QueryRests_Mini_v3>
    </ns:Document>
</ns:Documents>'''

ticket_ns = {'tc': 'http://fsrar.ru/WEGAIS/Ticket'}


def parse_date(rest_date: str):
    """
    Парсит строку даты в объект Arrow. Возвращает минимальную дату Arrow при ошибке или пустом вводе.

    :param rest_date: Строка с датой в формате, поддерживаемом Arrow.
    :return: Объект Arrow с распарсенной датой или Arrow.min при ошибке/пустом вводе.
    """
    if not rest_date:  # Проверяем None, "" или другие falsy значения
        return arrow.Arrow.min

    try:
        return arrow.get(rest_date)
    except arrow.parser.ParserError:  # Ограничиваемся конкретной ошибкой парсинга
        return arrow.Arrow.min


class Egais:
    def __init__(self):
        self.config = ConfigManager()
        self.url = self.config.utm_url
        self.logger = logging.getLogger('egais check')

    def _get_doc_type(self, xml_string: str) -> str:
        """
        Получение типа документа из XML с использованием ElementTree
        Предполагается фиксированное использование namespace 'ns' для Documents, Document и типа
        """
        try:
            # Парсим XML
            root = ET.fromstring(xml_string)

            # Получаем пространства имен
            namespaces = {}
            for _, ns in ET.iterparse(StringIO(xml_string), events=['start-ns']):
                namespaces[ns[0]] = ns[1]

            if 'ns' not in namespaces:
                raise ValueError("Namespace 'ns' not found in XML")

            # Ищем строго <ns:Document> внутри <ns:Documents>
            document = root.find('ns:Document', namespaces)

            if document is None:
                raise ValueError("<ns:Document> element not found")

            # Берем первый дочерний элемент
            for child in document:
                if child.tag:
                    # Убираем namespace из имени тега
                    tag_name = child.tag.split('}')[-1]
                    return tag_name.upper()

            self.logger.warning('Тип документа не определен, нет дочернего элемента.')
            return 'UNKNOWN'  # Если нет дочерних элементов

        except Exception as e:
            self.logger.error(f'Получение типа документа с помощью ElementTree: {e}')
            return 'ERROR'

    def _get_http(self, url_part: str, isFull=False) -> str:
        """
        Выполняет GET HTTP-запрос и возвращает содержимое ответа в случае успеха.

        :param url: URL для запроса.
        :return: Текст ответа или пустая строка при ошибке.
        """
        try:
            if isFull:
                url = url_part
            else:
                url = f'{self.url}/{url_part}'

            response = httpx.get(url, headers={"accept": "text/xml"}, timeout=15)
            if response.status_code == 200:
                return response.text
            else:
                return ''
        except Exception as e:
            self.logger.error(f'Get http to UTM, url: {url} - {e}')

        return ''

    def _post_file(self, xml: str) -> str:
        '''Отправка файла в УТМ и возврат UID документаоборота'''
        doc_type = self._get_doc_type(xml)
        if doc_type in ['ERROR', 'UNKNOWN']:
            return ''

        self.logger.debug(f'Попытка отправки файла в UTM: {doc_type}')
        xml_file = {'xml_file': ('temp.xml', xml, 'text/xml')}

        try:
            r = httpx.post(f'{self.url}/opt/in/{doc_type}', files=xml_file, timeout=15)

            if r.status_code == 200:
                self.logger.info(f'Документ отправлен в UTM, тип: {doc_type}')

                root = ET.fromstring(r.text)
                text = root.findtext('.//url', '').strip()
                return text
            else:
                return ''
        except Exception as e:
            self.logger.error(f"Post File Error: {e}")
            return ''

    def _del_doc(self, url_part: str, isFull=False):
        try:
            if isFull:
                url = url_part
            else:
                url = f'{self.url}/opt/out/{url_part}'

            response = httpx.delete(url, headers={"accept": "text/plain"}, timeout=10)
            return response.status_code == 200
        except Exception as e:
            self.logger.error(f'Del http to UTM, url: {url} - {e}')

        return False

    def _get_out_docs(self) -> str:
        # <A>
        # <url replyId="3bfe7f5d-b0e0-431f-94d9-79eba200c110">http://127.0.0.1:8080/opt/in/QueryRests_Mini/6</url>
        # <ver>1</ver>
        # </A>
        return self._get_http('opt/in')

    def is_online(self):
        try:
            response = httpx.head(f'{self.url}/home', timeout=4)
            return response.status_code == 200
        except:  # noqa: E722
            return False


class EgaisRests(Egais):
    def __init__(self):
        super().__init__()

        self.logger = logging.getLogger('egais check')
        self.config = ConfigManager()

    def send_rest_query(self) -> str:
        UID = ''

        if self.is_online() and self.config.egais_id:
            if self.request_exist():
                return 'request_exist'

            xml_file = XML_QUERY_V3.format(FSRAR_ID=self.config.egais_id)
            UID = self._post_file(xml_file)

            self.logger.debug(f'Отправка XML запроса остатков в УТМ, UID: {UID}')

        return UID

    def request_exist(self):
        text = self._get_out_docs()
        status = 'QUERYRESTS_MINI' in text.upper()
        if status:
            self.logger.debug('Запрос на остатки уже в исходящих висит')

        return status

    def get_rests(self):
        """
        Получает последнюю запись остатков (по дате) из системы УТМ.

        :return: Кортеж (rests_date, data), где rests_date — самая свежая дата,
                а data — данные остатков.
        """
        default_response = ("", [], None)  # Дефолтное значение для возврата

        if not self.is_online():
            return default_response

        for version in ['_v3', '_v2', '']:
            xml = self._get_http(f'opt/out/type/ReplyRests_Mini{version}')
            if xml != '':
                break

        if xml == '':
            return default_response

        last_url, del_urls = self._get_urls(xml)

        if not last_url:
            return default_response

        try:
            self.logger.debug(f'Последний документ остатков: {last_url}')

            xml_data = self._get_http(last_url, True)
            rest_data = self._parse_xml_rests(xml_data, last_url)

            return rest_data

        except Exception as e:
            self.logger.error(f"Ошибка при парсинге остатков: {e}")
            return default_response

        finally:
            self.logger.debug(f'Документов остатков для удаления: {len(del_urls)}')
            # удаляю старые документы старше суток даже последний документ уже после обработки данных
            for url in del_urls:
                try:
                    self._del_doc(url, True)
                except:  # noqa: E722
                    pass

    def get_dekl_path(self):
        candidates = [
            SYSTEM_DRIVE / "/Program Files (x86)" / "Deklarant",
            SYSTEM_DRIVE / "/Program Files" / "Deklarant",
        ]

        for path in candidates:
            if path.exists():
                return path

        return None

    def _get_urls(self, xml: str):
        latest_url = None
        old_urls = []

        try:
            root = ET.fromstring(xml)
            now = arrow.now()
            # urls = [x.text.strip() for x in root.findall('.//url') if x.text]
            # return urls

            # dekl_exist = self.get_dekl_path()

            # Проходим все <url>
            for url_elem in root.findall('.//url'):
                url_text = url_elem.text.strip() if url_elem.text else ''
                timestamp_str = url_elem.get('timestamp', '')

                if not url_text or not timestamp_str:
                    continue

                self.logger.debug(f'{url_text}, {timestamp_str}')

                try:
                    # Преобразуем строку с миллисекундами и смещением
                    timestamp = arrow.get(timestamp_str)
                except ValueError:
                    self.logger.warning(f"Не удалось распарсить timestamp остатков: {timestamp_str}")
                    continue

                # Проверка на документы старше 70 минут
                # так как другие проги за это время уже обработают документ если что
                # если нет декларанта, то сразу добавляем документ на удаление
                # if not dekl_exist:
                #     old_urls.append(url_text)
                # elif now - timestamp > timedelta(minutes=DEL_RESTS_AFTER_MINUTES):
                #     old_urls.append(url_text)

                if now - timestamp > timedelta(minutes=DEL_RESTS_AFTER_MINUTES):
                    old_urls.append(url_text)

                latest_url = url_text  # каждый раз обновляется, в итоге останется последний

            return latest_url, old_urls

        except ET.ParseError as e:
            self.logger.error(f"Error parsing XML in _get_urls: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error in _get_urls: {e}")

        return None, []

    def _parse_xml_rests(self, xml: str, last_url: str) -> Tuple[str, List[Tuple], str]:
        """
        Парсит XML, чтобы получить дату остатков и список бутылок.

        :param xml: Строка с содержимым XML.
        :return: Кортеж (rests_date, data, last_url), где rests_date — строка даты,
                а data — список бутылок (AlcCode, Inform1RegId, Inform2RegId, Quantity).
        """
        data = []
        rests_date = ""

        if not xml:
            return rests_date, data, last_url

        try:
            root = ET.fromstring(xml)

            ns_text = 'ReplyRests_Mini'

            if '_v3' in xml.lower():
                ns_text += '_v3'
            elif '_v2' in xml.lower():
                ns_text += '_v2'

            # Префикс пространства имен
            ns = {'rst': f'http://fsrar.ru/WEGAIS/{ns_text}'}

            # Получаем дату остатков
            rests_date_elem = root.find(".//rst:RestsDate", ns)
            rests_date = rests_date_elem.text if rests_date_elem is not None else ""

            # Парсим позиции бутылок
            for pos in root.findall(".//rst:StockPosition", ns):
                try:
                    alc_code = pos.find("rst:AlcCode", ns)
                    fa = pos.find("rst:Inform1RegId", ns)
                    fb = pos.find("rst:Inform2RegId", ns)
                    quantity = pos.find("rst:Quantity", ns)

                    if alc_code is not None and fa is not None and fb is not None and quantity is not None:
                        data.append((
                            alc_code.text.strip(),  # type: ignore
                            fa.text.strip(),  # type: ignore
                            fb.text.strip(),  # type: ignore
                            int(float(quantity.text.strip())),  # type: ignore
                            rests_date
                        ))
                except Exception:
                    continue

        except ET.ParseError as e:
            self.logger.error(f"XML Parse Error in _parse_xml_rests: {e}")

        return rests_date, data, last_url  # type: ignore

    def _delete_tickets(self):
        '''Возвращает список полный URL с тикитами которые есть в словаре для проверки'''
        try:
            if not self.is_online():
                return

            xml_url_tickets = self._get_http('opt/out/type/Ticket')

            root = ET.fromstring(xml_url_tickets)

            for url in root.findall('.//url'):
                url_text = url.text.strip()  # type: ignore # Текст внутри тега <url>

                xml_ticket = self._get_http(url_text, True)

                if not xml_ticket:
                    continue

                try:
                    root = ET.fromstring(xml_ticket)

                    rs = root.find('.//tc:Result', ticket_ns)

                    if rs:
                        DocType = root.findtext('.//tc:DocType', '', ticket_ns).strip()
                        Comment = root.findtext('.//tc:Comments', '', ticket_ns).strip()

                        bool_doc_type = 'queryrests_mini' in DocType.lower() or 'queryrests' in DocType.lower()
                        bool_comment = 'производится не чаще' in Comment.lower() or 'уже существует документ' in Comment.lower()

                        if bool_doc_type and bool_comment:
                            try:
                                self._del_doc(url_text, True)
                            except:  # noqa: E722
                                pass

                except Exception as e:
                    self.logger.error(f"Error parsing ticket XML: {e}")
        except:  # noqa: E722
            pass


class EgaisDoc(Egais):
    def __init__(self):
        super().__init__()
        self.tickets = {}
        self.logger = logging.getLogger('egais check')
        self.config = ConfigManager()

    @property
    def tickets_in_works(self):
        return bool(self.tickets)

    def check_tickets(self):
        """Обновляет тикеты и очищает завершенные."""

        utm_tickets = self._get_tickets()
        to_remove = []  # Список UID, которые нужно удалить

        for ticket in utm_tickets:
            UID = ticket.get('TransportId', '')

            if UID and UID in self.tickets:
                # сразу удаляем документ из УТМа чтобы не терять времени
                self._del_doc(ticket['Url'], True)

                self.send_ticket(ticket)

                # обновляем счетчики в словаре запросов
                self.tickets[UID]['ReceivedDocs'] += 1
                self.tickets[UID]['TotalDocs'] = ticket['TotalDocs']

                # Если все документы получены, добавляем UID в список на удаление
                if self.tickets[UID]["ReceivedDocs"] >= self.tickets[UID]["TotalDocs"]:
                    to_remove.append(UID)

        # Удаляем завершенные UID
        for UID in to_remove:
            del self.tickets[UID]

    def send_ticket(self, ticket: Dict):
        if not self.config.egais_id:
            self.logger.warning("Отсутствует egais_id, отправка тикетов невозможна")
            return

        try:
            json_data = json.dumps(ticket, ensure_ascii=False)
            compressed_data = gzip.compress(json_data.encode('utf-8'))

            timeout = httpx.Timeout(10, write=30)
            url = f'{URL_SERVER}/rclient/ticket'
            headers = self.config.get_headers({'Content-Encoding': 'gzip'})

            self.logger.debug(f'Попытка отправить тикет, heders={headers}')

            with httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=3)) as client:
                response = client.post(url, data=compressed_data, headers=headers)  # type: ignore

                if response.status_code != 200:
                    logger.warning(f'Тикет не отправлен, код ответа: {response.status_code}')
                else:
                    logger.debug('Тикет отправлен')

        except Exception as e:
            logger.error(f'Error send ticket to server, error = {e}')

    def _get_tickets(self) -> List[Dict]:
        '''Возращает список с разобранными тикетами которые были в словаре проверки'''
        url_tickets = self._get_url_tickets()

        tickets = []

        for url in url_tickets:
            ticket_dict = self._get_ticket_data(url)
            if ticket_dict:
                tickets.append(ticket_dict)

        return tickets

    def _get_url_tickets(self) -> List[str]:
        '''Возвращает список полный URL с тикитами которые есть в словаре для проверки'''
        tickets = []

        try:
            xml_url_tickets = self._get_http('opt/out/type/Ticket')

            root = ET.fromstring(xml_url_tickets)

            for url in root.findall('.//url'):
                url_text = url.text.strip()  # type: ignore # Текст внутри тега <url>
                reply_id = url.get('replyId')  # Значение атрибута replyId

                if reply_id and reply_id in self.tickets:
                    tickets.append(url_text)

        except Exception as e:
            self.logger.error(f"Error parsing ticket list: {e}")

        return tickets

    def _get_ticket_data(self, url: str) -> Dict:
        xml_ticket = self._get_http(url, True)

        if not xml_ticket:
            return {}

        try:
            root = ET.fromstring(xml_ticket)

            rs = root.find('.//tc:Result', ticket_ns)

            if rs:
                Result = root.findtext('.//tc:Conclusion', '', ticket_ns).strip()
                Comment = root.findtext('.//tc:Comments', '', ticket_ns).strip()
                DocNum = 1
                TotalDocs = 2 if Result.lower() == 'accepted' else 1
            else:
                Result = root.findtext('.//tc:OperationResult/tc:OperationResult', '', ticket_ns).strip()
                Comment = root.findtext('.//tc:OperationComment', '', ticket_ns).strip()
                DocNum = 2
                TotalDocs = 2

            data = {
                'Id': url.split('/')[-1],
                'DocType': root.findtext('.//tc:DocType', '', ticket_ns).strip(),
                'TransportId': root.findtext('.//tc:TransportId', '', ticket_ns).strip(),
                'RegID': root.findtext('.//tc:RegID', '', ticket_ns).strip(),
                'Result': Result,
                'Comment': Comment,
                'DocNum': DocNum,
                'TotalDocs': TotalDocs,
                'Url': url
            }

            return data
        except Exception as e:
            self.logger.error(f"Error parsing ticket XML: {e}")

        return {}

    def send_docs_utm(self):
        # тут забираем xml Документы тикетов для отправки, будут лежать в папке или в базе
        if self.is_online():
            documents = self._get_docs_from_server()

            if len(documents) == 0:
                return

            self.logger.info(f'Отправка документов в УТМ, кол-во: {len(documents)}')

            for xml_doc in documents:
                UID = self._post_file(xml_doc)

                if UID:
                    self.tickets[UID] = {
                        'ReceivedDocs': 0,
                        'TotalDocs': 2
                    }

        else:
            logger.warning('Отправка документов в УТМ невозможна, УТМ не запущен.')

    def _get_docs_from_server(self):
        # запрос и получени json с документами которые надо отправить в УТМ
        timeout = httpx.Timeout(10, read=300)
        url = f'{URL_SERVER}/rclient/docs'

        if not self.config.egais_id:
            return []

        try:
            headers = self.config.get_headers({'Content-Encoding': 'gzip'})
            with httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=3)) as client:
                response = client.get(url, headers=headers)

                if response.status_code == 200:
                    return response.json()
                else:
                    return []
        except Exception as e:
            self.logger.error(f'Получение списка документов с сервера: {e}')
            return []


class EgaisDocWorker:
    """## Монитор входящих документов егаис
    Мониторит входящие документы в утм, удаляет лишние доки с ошибками по частым запроса по остаткам
    """
    def __init__(self, exit_flag: threading.Event):
        self.egais = EgaisDoc()
        self.exit_flag = exit_flag
        self.logger = logging.getLogger('egais check')

    def start(self):
        """Запускает рабочий поток."""
        worker_thread = threading.Thread(target=self._run, name="EgaisDocWorkerThread", daemon=True)
        worker_thread.start()
        self.logger.info("EgaisDoc Worker запущен")

    def _run(self):
        """Основной цикл работы."""
        now = time.monotonic()
        next_income_docs_time = now
        next_out_docs_time = now

        try:
            while not self.exit_flag.is_set():
                now = time.monotonic()

                # Проверка документов раз в 3 секунды
                if now >= next_income_docs_time:
                    try:
                        self.income_tickets_work()
                    except Exception as e:
                        self.logger.error(f"EgaisDocWorker _run ticket: {e}")

                    next_income_docs_time = now + 3

                # Получение XML для отправки раз в 5 минут
                if now >= next_out_docs_time:  # 1 раз в 5 минут идет проверка на новый запрос
                    try:
                        self.check_out_docs()
                    except Exception as e:
                        self.logger.error(f"Error in EgaisDocWorker send_file_to_utm: {e}")

                    next_out_docs_time = now + (60 * 5)

                time.sleep(0.5)  # Шаг таймера

        except Exception as e:
            self.logger.error(f"Ошибка в цикле обновления: {e}")

    def income_tickets_work(self):
        if self.egais.tickets_in_works:
            self.egais.check_tickets()

    def check_out_docs(self):
        # проверка и отправка документов в УТМ
        self.egais.send_docs_utm()


class EgaisRestWorker:
    """## Кол-во бутылок по егаису
    Проверяет и мониторит остатки по кол-венному учетом бутылок в егаисе
    """
    def __init__(self, exit_flag: threading.Event):
        self.egais = EgaisRests()
        self.exit_flag = exit_flag
        self.logger = logging.getLogger('egais check')
        self.config = ConfigManager()
        self.last_url = None
        self.last_send_query: Optional[arrow.Arrow] = None

    def start(self):
        """Запускает рабочий поток."""
        worker_thread = threading.Thread(target=self._run, name="EgaisRestWorkerThread", daemon=True)
        worker_thread.start()
        self.logger.info("EgaisRest Worker запущен")

    def _run(self):
        """Основной цикл работы."""

        min20 = 60 * 20  # 20 минут
        min5 = 60 * 5  # 5 минут

        now = time.monotonic()
        next_income_docs_time = now
        next_out_docs_time = now
        next_del_tickets_time = now
        next_send_ost_time = now

        try:
            while not self.exit_flag.is_set():
                now = time.monotonic()

                # Проверка входящих документов в УТМе раз в 3 секунды
                if now >= next_income_docs_time:
                    try:
                        self.check_rests()
                    except Exception as e:
                        self.logger.error(f"Error in Rests responce: {e}")

                    next_income_docs_time = now + 3

                # Проверка раз в 5 минут для отправки остатков на сервер
                if now >= next_send_ost_time:
                    try:
                        self.send_rests()
                    except Exception as e:
                        self.logger.error(f"Error in send_rest: {e}")

                    next_send_ost_time = now + min5

                # Проверка раз в 20 минут, в базе доп проверка на раз в 1 час
                if now >= next_out_docs_time:  # 1 раз в 20 минут идет проверка на новый запрос
                    try:
                        self.send_rest_query()
                    except Exception as e:
                        self.logger.error(f"Error in send_rest: {e}")

                    next_out_docs_time = now + min20

                # проверка на удаление документов раз в 60 секунд
                if now >= next_del_tickets_time:
                    try:
                        self.egais._delete_tickets()
                    except Exception as e:
                        self.logger.error(f"Error in delete old tickets: {e}")

                    next_del_tickets_time = now + 60

                time.sleep(0.5)  # Шаг таймера

        except Exception as e:
            self.logger.error(f"Ошибка в цикле обновления: {e}")

    def check_rests(self):
        # проверка входящих документов по остаткам в УТМе
        date_rest, rest_list, last_url = self.egais.get_rests()

        db = None

        try:
            if not date_rest:
                return

            if self.last_url == last_url:
                return

            egais_id = self.config.egais_id
            if not egais_id:
                return

            db = RestsDataBase(egais_id)

            db.update_bottle_stocks(rest_list, date_rest)
            self.last_url = last_url

        except Exception as e:
            self.logger.error(f"При обновлении побутылочных остатков: {e}")
        finally:
            if db is not None:
                db.close()
                self.logger.debug("База данных закрыта")

    def send_rests(self):
        egais_id = self.config.egais_id
        if not egais_id:
            self.logger.warning("Отсутствует egais_id, отправка остатков невозможна")
            return

        db = None

        try:
            db = RestsDataBase(egais_id)
            if db.check_if_needs_to_send():
                self.logger.debug('Нужно отправить остатки на сервер')

                osts = db.get_bottle_stocks()

                status = self._send_rests(osts)
                if status == 200:
                    db.mark_as_sent()

        except Exception as e:
            self.logger.error(f"Ошибка при отправке кол-венных остатков: {e}")
        finally:
            if db is not None:
                db.close()
                self.logger.debug("База данных закрыта")

    def _send_rests(self, rests: List[Tuple]) -> int:
        if not self.config.egais_id:
            self.logger.warning("Отсутствует egais_id, отправка остатков невозможна")
            return -1

        try:
            timeout = httpx.Timeout(10, write=120)
            url = f'{URL_SERVER}/rclient/rests'

            json_data = json.dumps(rests, ensure_ascii=False)
            compressed_data = gzip.compress(json_data.encode('utf-8'))

            headers = self.config.get_headers({'Content-Encoding': 'gzip'})

            self.logger.debug('Попытка отправить остатки.')

            with httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=3)) as client:
                response = client.post(url, data=compressed_data, headers=headers)  # type: ignore

                status_code = response.status_code

                if status_code != 200:
                    self.logger.warning(f'Остатки не отправлены, статус ответа: {status_code}')
                else:
                    self.logger.debug('Остатки успешно отправлены')

                return status_code

        except Exception as e:
            self.logger.error(f'Ошибка при отправке остатков: {e}, всего записей: {len(rests)}')

        return -1

    def send_rest_query(self):
        # проверка на нужды отправки нового запроса

        egais_id = self.config.egais_id
        if not egais_id:
            self.logger.warning("Отсутствует egais_id, отправка запроса остатков невозможна")
            return

        settings = self._get_settings()
        ost_check = settings.get('ost_check', True)

        if not ost_check:
            self.logger.info('Проверка кол-венных остатков по егаису выключено на сервере')
            return

        ost_check_iterval = settings.get('ost_check_iterval', None)

        db = None

        try:
            db = RestsDataBase(egais_id, ost_check_iterval)
            if db.need_send_new_query(self.last_send_query):
                UID = self.egais.send_rest_query()
                if UID != '':
                    self.last_send_query = arrow.now()

        except Exception as e:
            self.logger.error(f"Ошибка при отправке запроса кол-венных остатков: {e}")
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

# if __name__ == '__main__':
    # import logging

    # exit_flag = threading.Event()
    # # worker_egais_doc = EgaisDocWorker(exit_flag)
    # # worker_egais_doc.start()

    # worker_egais_rest = EgaisRestWorker(exit_flag)
    # worker_egais_rest.start()

    # try:
    #     logger.info("Application started")
    #     while not exit_flag.is_set():
    #         config.update_data_in_loop()

    #         time.sleep(0.5)  # Поддержание главного потока
    # except KeyboardInterrupt:
    #     logger.info("Stopping the application...")
    #     exit_flag.set()
    #     logging.shutdown()
