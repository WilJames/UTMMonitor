from dataclasses import dataclass, asdict, field
from typing import List, Optional, Dict
import arrow
import re
from uuid import NAMESPACE_DNS, uuid3
import gzip
import zipfile
import json
from pathlib import Path
import logging
import xml.etree.ElementTree as ET

logger = logging.getLogger('egais check')

log_pattern = re.compile(
    r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*WorkflowChequeController\s*-\s*Сохранение данных(.+)',
    flags=re.I
)
che_title_pattern = re.compile(r'(\w+)=(.*?)(?=, \w+=|$)', flags=re.I)
bottle_pattern = re.compile(r'bottle\s*{(.*?)}', flags=re.I)
cheque_pattern = re.compile(r'ChequeV\d+\((.*?)\)', flags=re.I)

cheque_data_pattern = re.compile(r'(\w+)=(\[.*?\])', re.I)

valid_category = ['bottles', 'caterings']
valid_title_keys = [
    'n_kassa',
    'n_smena',
    'n_cheque',
    'dt',
    'type'
]

ns = {'ns': 'http://fsrar.ru/WEGAIS/WB_DOC_SINGLE_01', 'ck': 'http://fsrar.ru/WEGAIS/ChequeV4'}


@dataclass
class Bottle:
    barcode: str
    price: float
    volume: Optional[float] = None


@dataclass
class Cheque:
    type: str = ""
    dt: str = ""
    n_kassa: str = ""
    n_smena: str = ""
    n_cheque: str = ""
    bottles: List[Bottle] = field(default_factory=list)
    log_id: str = ""

    def generate_unique_id(self) -> None:
        """Генерация уникального идентификатора и установка его в поля объекта."""
        if not self.log_id:
            data = asdict(self)
            # original_dt = data.get("dt", "")

            # Извлекаем только дату без времени
            # data["dt"] = original_dt[:6] if len(original_dt) >= 6 else original_dt

            # Сортировка бутылок по barcode, price, volume
            bottles = data.get("bottles", [])
            sorted_bottles = sorted(
                bottles,
                key=lambda b: (b["barcode"], b["price"], b.get("volume") or 0)
            )
            data["bottles"] = sorted_bottles

            # Преобразуем в JSON с сортировкой ключей
            json_str = json.dumps(data, ensure_ascii=False, sort_keys=True)

            uid = uuid3(NAMESPACE_DNS, json_str)
            self.log_id = uid.hex

    def json_gzip(self) -> bytes:
        """Сериализует объект Cheque в JSON и сжимает его с использованием gzip."""
        data_dict = asdict(self)

        json_data = json.dumps(data_dict, ensure_ascii=False)
        compressed_data = gzip.compress(json_data.encode('utf-8'))
        return compressed_data


def _get_right_key(key: str) -> str:
    """Приведение ключа к единому виду."""
    mapping = {
        'касса': 'n_kassa',
        'смена': 'n_smena',
        'чек': 'n_cheque',
        'дата': 'dt',
        'kassa': 'n_kassa',
        'shift': 'n_smena',
        'number': 'n_cheque',
        'cashboxid': 'n_kassa',
        'sequenceid': 'n_smena',
        'ticketid': 'n_cheque',
        'type': 'type',
        'chequedate': 'dt',
        'date': 'dt'
    }
    return mapping.get(key.strip().lower(), key.strip())


def _parse_date_cheque(log_datetime: arrow.Arrow, date_str: str) -> str:
    """Парсинг даты чека."""
    replacer = {
        'MSK': '+03:00'
    }

    formats = [
        'ddd MMM DD HH:mm:ss ZZ YYYY',  # 'Mon Feb 10 08:38:54 +03:00 2025'
        'ddd MMM DD HH:mm:ss ZZZZZ YYYY',   # 'Mon Feb 10 08:03:32 GMT+03:00 2025'
        'DDMMYYHHmmss',
        'DDMMYYHHmm'                    # '0902251132'
    ]

    # re_date = r'ddd[\s+]MMM[\s+]DD[\s+]HH:mm:ss[\s+]ZZ[\s+]YYYY'

    date_str_rep = date_str

    for k, v in replacer.items():
        date_str_rep = date_str.replace(k, v)

    for fmt in formats:
        try:
            parsed_datetime = arrow.get(date_str_rep, fmt)
            return parsed_datetime.format('DDMMYYHHmmss')
        except arrow.ParserError:
            continue

    # Логируем ошибку, если ни один формат не подошел
    logger.error(f"Error parsing date: {date_str}, returning log datetime.")
    return log_datetime.format('DDMMYYHHmmss')


def _parse_volume(value, as_ml: bool) -> Optional[float]:
    """Парсит объем бутылки и возвращает его в литрах."""
    if not value:
        return None

    try:
        volume = float(value)
        return volume / 1000 if as_ml else volume
    except (ValueError, TypeError):
        return None


def _parse_bottle_data(match: str, as_ml: bool) -> Dict:
    """Парсинг данных одной бутылки."""
    parsers = {
        'barcode': str.upper,
        'price': float,
        'volume': lambda v: _parse_volume(v, as_ml)
    }
    bottle_data = {}
    for item in match.split(','):
        key, value = map(str.strip, item.split('='))
        key = key.lower()

        if key in parsers:
            bottle_data[key] = parsers[key](value)
    return bottle_data


def _parse_bottles(bottles_string, as_ml=False):
    """Парсит строку с данными о бутылках и возвращает список объектов Bottle.

    Args:
        bottles_string (str): Строка с информацией о бутылках.
        as_ml (bool): Если True, объемы интерпретируются как миллилитры (по умолчанию False).

    Returns:
        list: Список объектов Bottle, отсортированный по штрихкоду.
    """
    # Если строка пустая, возвращаем пустой список
    if not bottles_string:
        return []

    # Ищем все совпадения по шаблону в нижнем регистре
    matches = bottle_pattern.findall(bottles_string.lower())

    # Создаем пустой список для бутылок
    bottles = []

    # Обрабатываем каждое совпадение
    for match in matches:
        # Парсим данные бутылки в словарь
        bottle_data = _parse_bottle_data(match, as_ml)

        # Проверяем, есть ли марка в данных
        if 'barcode' in bottle_data:
            # Создаем объект Bottle и добавляем его в список
            bottles.append(Bottle(**bottle_data))

    # Сортируем список по марке
    bottles.sort(key=lambda x: x.barcode)

    return bottles


def _parse_cheque_title(log_datetime: arrow.Arrow, cheque_data: str) -> Dict:
    """Парсинг заголовка чека и приведение ключей к единому виду."""
    handlers = {
        'dt': lambda v: _parse_date_cheque(log_datetime, v),
        'type': lambda v: 'Продажа' if v.lower() == 'sale' else 'Возврат'
    }
    data_dict = {}
    try:
        for key, value in che_title_pattern.findall(cheque_data):
            key_new = _get_right_key(key)
            if key_new in valid_title_keys:
                handler = handlers.get(key_new, str.strip)
                data_dict[key_new] = handler(value)
    except Exception as e:
        logger.error(f"Ошибка парсинга заголовка: {e}, данные: {cheque_data}")
    return data_dict


def _sell_or_voz(bottles: List[Bottle]) -> str:
    type_sell = 'Продажа'

    try:
        b = bottles[0]

        if b.price < 0.0:
            type_sell = 'Возврат'
    except Exception as e:
        logger.info(f'Ошибка определения запасного типа продажи: {e}')

    return type_sell


def _parse_cheque(log_datetime: arrow.Arrow, cheque_str: str) -> Optional[Cheque]:
    """Парсинг чека из строки."""
    cheque_match = cheque_pattern.search(cheque_str)

    if not cheque_match:
        logger.debug(f"Строка не соответствует формату чека: {cheque_str}")
        return

    cheque_data = cheque_match.group(1)
    bottles_list = []

    for type_data, bottle in cheque_data_pattern.findall(cheque_data):
        type_data = type_data.lower().strip()

        if type_data in valid_category:
            bottles = _parse_bottles(bottle, type_data == 'caterings')
            bottles_list.extend(bottles)

    if not bottles_list:
        logger.debug(f"Не найдены бутылки в чеке: {cheque_data}")
        return

    cheque_title = _parse_cheque_title(log_datetime, cheque_data)

    cheque = Cheque(
        type=cheque_title.get('type', _sell_or_voz(bottles_list)),
        dt=cheque_title.get('dt', log_datetime.format('DDMMYYHHmmss')),
        n_kassa=cheque_title.get('n_kassa', ''),
        n_smena=cheque_title.get('n_smena', ''),
        n_cheque=cheque_title.get('n_cheque', ''),
        # log_date=log_datetime.format('YYYY-MM-DD HH:mm:ss.SSS'),
        bottles=bottles_list
    )
    cheque.generate_unique_id()
    return cheque


def parse_cheques(file_path: Path) -> List[Cheque]:
    """Парсит чеки из лог-файла (.log или .gz).

    Args:
        file_path (Path): Путь к файлу (.log или .gz).

    Returns:
        list: Список объектов Cheque, извлеченных из лога.
    """
    cheques = []
    is_gz = file_path.suffix == '.gz'

    opener = gzip.open if is_gz else open
    mode = 'rt' if is_gz else 'r'

    try:
        # Открываем файл в зависимости от расширения
        with opener(file_path, mode, encoding='utf-8', errors='ignore') as f:
            for line in f:
                match = log_pattern.search(line)  # type: ignore

                if not match:
                    continue

                log_datetime, line_data = match.group(1), match.group(2)

                try:
                    log_datetime = arrow.get(log_datetime, 'YYYY-MM-DD HH:mm:ss')
                except Exception as e:
                    log_datetime = arrow.now()
                    logger.warning(f'Не удалось распарсить дату в строке: {e}')

                parsed = _parse_cheque(log_datetime, line_data)

                if parsed:
                    cheques.append(parsed)
                else:
                    logger.warning(f"Не удалось распарсить чек из строки: {line_data}")

        if len(cheques) > 0:
            logger.debug(f'Кол-во чеков распарсено: {len(cheques)}')

    except Exception as e:
        logger.error(f"Ошибка обработки {file_path}: {type(e).__name__}: {e}")

    return cheques


def parse_dekl(xml: str, deep_days: int) -> Optional[Cheque]:
    threshold = arrow.now().shift(days=deep_days)

    try:
        root = ET.fromstring(xml)

        bottles = root.findall('.//ck:Bottle', ns)
        if bottles:
            try:
                dt = arrow.get(root.findtext('.//ck:Date', '', ns).strip())
            except:  # noqa: E722
                return None

            if dt < threshold:
                return None

            cheque = Cheque(
                type=root.findtext('.//ck:Type', '', ns).strip(),
                dt=dt.format('DDMMYYHHmmss'),
                n_kassa=root.findtext('.//ck:Kassa', '', ns).strip(),
                n_smena=root.findtext('.//ck:Shift', '', ns).strip(),
                n_cheque=root.findtext('.//ck:Number', '', ns).strip(),
                bottles=[
                    Bottle(
                        barcode=b.findtext('ck:Barcode', '', ns).strip(),
                        price=float(b.findtext('ck:Price', '0.0', ns).strip()),
                        volume=None
                    ) for b in bottles
                ]
            )
            cheque.generate_unique_id()
            return cheque

        # Альтернативная структура (без namespace)
        bottles = root.findall('.//Bottle')
        if bottles:
            try:
                dt = arrow.get(root.attrib.get('datetime', ''), "DDMMYYHHmm")
            except:  # noqa: E722
                return None

            if dt < threshold:
                return None

            type_sell = 'Продажа'

            cheque = Cheque(
                type=type_sell,
                dt=dt.format('DDMMYYHHmmss'),
                n_kassa=root.attrib.get('kassa', ''),
                n_smena=root.attrib.get('shift', ''),
                n_cheque=root.attrib.get('number', ''),
                bottles=[],
            )

            for b in bottles:
                if b.attrib.get('acttype') == '-1':
                    cheque.type = 'Возврат'

                cheque.bottles.append(Bottle(
                    barcode=b.attrib.get('barcode', ''),
                    price=float(b.attrib.get('price', '0')),
                    volume=None
                ))

            cheque.generate_unique_id()
            return cheque
    except Exception as e:
        logger.error(f"Ошибка обработки чека из декларанта: {type(e).__name__}: {e}")

    return None


def find_cheques_dekl(check_dir: Path) -> List[Cheque]:
    deep_days = -30
    threshold_ts = arrow.now().shift(days=deep_days).timestamp()
    cheques_list = []

    for check_zip in check_dir.glob("*.zip"):
        try:
            # Фильтруем по дате модификации файла
            if check_zip.stat().st_mtime < threshold_ts:
                continue

            with zipfile.ZipFile(check_zip, 'r') as zip_file:
                # Берём первый .xml файл в архиве
                xml_file_name = next((name for name in zip_file.namelist() if name.endswith('.xml')), None)

                if xml_file_name:
                    with zip_file.open(xml_file_name) as f:
                        xml_data = f.read().decode('utf-8', errors='ignore')
                        chek = parse_dekl(xml_data, deep_days)
                        if chek:
                            cheques_list.append(chek)

        except (zipfile.BadZipFile, UnicodeDecodeError, ET.ParseError) as e:
            logger.error(f"Ошибка при обработке {check_zip.name}: {e}")
            # logger.info(xml_data)
            continue
        except Exception as e:
            logger.error(f"Неизвестная ошибка при обработке {check_zip.name}: {e}")
            # logger.info(xml_data)
            continue

    return cheques_list
