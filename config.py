import winreg
import configparser
import logging
import httpx
import arrow
import platform
import sys
import socket
import xml.etree.ElementTree as ET
from typing import Dict, Optional, Any
from rutoken import Rutoken

from const import CONFIG_FILE, UTM_CONFING, ANYDESK_PROGDATA, ANYDESK_APPDATA, VALID_INN, DISABLED_INN, VERSION, UTM_LOG_DAYS, PROGRAM_EXE

logging.getLogger('httpx').setLevel(logging.WARNING)

try:
    startup_approved_supported = float(platform.release()) >= 6.2
except ValueError:
    startup_approved_supported = False  # или True, в зависимости от логики

is_frozen_executable = getattr(sys, 'frozen', False)


class ConfigManager:
    _instance = None  # Хранит единственный экземпляр класса

    def __new__(cls, *args, **kwargs):
        """Singleton: Возвращает единственный экземпляр класса."""
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Инициализация класса (выполняется только один раз)."""
        if hasattr(self, '_initialized'):  # Инициализация выполняется только один раз
            return

        self._initialized = True
        self.logger = logging.getLogger('egais check')

        self.utm_ip = ''
        self.utm_port = ''
        self.inn = ''
        self.kpp = ''
        self.egais_id = ''
        self.anydesk_id = ''
        self.rsa = ''
        self.gost = ''
        self.token_ver = ''
        self.total_memory = ''
        self.free_memory = ''
        self.token_status = ''
        self.log_level = ''
        self.utm_version = ''

        self.defaults = {
            'utm_ip': '127.0.0.1',
            'utm_port': '8080',
            'inn': '',
            'kpp': '',
            'egais_id': '',
            'anydesk_id': '',
            'rsa': '',
            'gost': '',
            'token_ver': '',
            'total_memory': '',
            'free_memory': '',
            'token_status': '',
            'log_level': 'INFO',
            'utm_version': ''
        }

        # Путь к файлу конфигурации
        self.config = configparser.ConfigParser()

        # Обеспечение существования файла конфигурации
        self._ensure_config_file_exists()

        # Попытка прочитать конфиг
        try:
            read_files = self.config.read(CONFIG_FILE, encoding='utf-8')
            if not read_files or 'DEFAULT' not in self.config:
                raise configparser.MissingSectionHeaderError(str(CONFIG_FILE), 1, "No [DEFAULT] section found")

        except (configparser.MissingSectionHeaderError, configparser.ParsingError, UnicodeDecodeError) as e:
            self.logger.error(f"Ошибка чтения config.ini: {e}")
            self.logger.warning("Повреждённый конфиг будет сброшен.")

            try:
                CONFIG_FILE.unlink()  # Удаляем повреждённый файл
            except Exception as delete_err:
                self.logger.error(f"Ошибка при удалении повреждённого config.ini: {delete_err}")

            self._create_default_config()
            self.config.read(CONFIG_FILE, encoding='utf-8')

        # Заполняем конфиг значениями по умолчанию, если их нет
        self.config['DEFAULT'].update({k: v for k, v in self.defaults.items() if k not in self.config['DEFAULT']})
        self.save_config()

        # Загружаем параметры в атрибуты объекта
        self._load_config()

    def _ensure_config_file_exists(self) -> None:
        """Создаёт папки и файл конфигурации, если их нет."""
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if not CONFIG_FILE.exists():
                self._create_default_config()
        except Exception as e:
            self.logger.error(f"Ошибка при создании файла конфигурации: {e}")

    def _create_default_config(self) -> None:
        """Создаёт файл конфигурации с дефолтными значениями."""
        try:
            self.config['DEFAULT'] = self.defaults
            self.save_config()
        except Exception as e:
            self.logger.error(f"Ошибка при создании файла конфигурации: {e}")

    def _load_config(self) -> None:
        """Загружает параметры из конфигурационного файла в атрибуты класса."""
        for key in self.defaults.keys():
            setattr(self, key, self.config['DEFAULT'].get(key, self.defaults[key]))

    def reset_conf(self) -> None:
        """Сбрасывает конфигурацию до значений по умолчанию."""
        self.logger.warning('Сбрасываем конфигурацию до значений по умолчанию.')
        self._create_default_config()
        self.config.read(CONFIG_FILE)
        self._load_config()

    def save_config(self) -> None:
        """Сохраняет текущую конфигурацию в файл."""
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)
            self.logger.info("Конфигурация успешно сохранена.")
        except Exception as e:
            self.logger.error(f"Ошибка при сохранении конфигурации: {e}")

    def get_param(self, param: str, default=None):
        """
        Возвращает значение параметра из конфигурации.

        Args:
            param (str): Имя параметра.
            default (Any): Значение по умолчанию, если параметр не найден.

        Returns:
            Any: Значение параметра.
        """
        return getattr(self, param, default)

    def update_param(self, param: str, value) -> bool:
        """
        Обновляет значение параметра в конфигурации.

        Args:
            param (str): Имя параметра.
            value (Any): Новое значение параметра.

        Returns:
            bool: True, если значение было обновлено, иначе False.
        """
        current_value = self.get_param(param)

        # Обновляем только если значение отличается и не пустое
        if value and str(value) != current_value:
            setattr(self, param, str(value))

            self.config['DEFAULT'][param] = str(value)
            self.save_config()
            self.logger.info(f"Параметр: {param}={current_value} обновлён на {value}.")
            return True
        else:
            self.logger.debug(f"Параметр: {param}={current_value} не изменён.")
            return False

    @property
    def utm_url(self):
        return f'http://{self.utm_ip}:{self.utm_port}'

    def get_port_utm(self):
        """Чтение порта из конфигурационного файла или использование значения по умолчанию."""
        try:
            param_port = self.get_param('utm_port', '8080')
            port = int(param_port)  # type: ignore
            return port
        except ValueError:
            self.logger.error('Неверный формат порта UTM, используем порт по умолчанию: 8080')
            return 8080

    def update_utm_port(self):
        port_int = self.get_port_utm()

        try:
            config_utm = configparser.ConfigParser()

            if not UTM_CONFING.exists():
                return port_int

            with open(UTM_CONFING, 'r', encoding='utf-8') as f:
                data = f.read()

            data = f'[DEF]\n{data}'

            config_utm.read_string(data)

            port = config_utm['DEF'].get('web.server.port', '8080')
            port_int = int(port)
        except Exception as e:
            self.logger.error(f'Ошибка чтения порта УТМ из файла конфигурации: {e}')

        self.update_param('utm_port', str(port_int))

    def update_config_utm(self):
        """
        Обновляет .properties-файл, изменяя только server.tomcat.accesslog.enabled и server.tomcat.accesslog.max-days,
        сохраняя оригинальную структуру файла.

        :param file_path: Путь к файлу .properties
        :return: True, если обновление успешно, иначе False
        """
        if not UTM_CONFING.exists():
            self.logger.error(f"Файл {UTM_CONFING} не найден")
            return

        try:
            # Читаем исходный файл как список строк
            with open(UTM_CONFING, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            is_changed = False
            updated_lines = []

            for line in lines:
                stripped_line = line.strip()
                if '=' not in stripped_line or stripped_line.startswith('#'):
                    updated_lines.append(line)  # Сохраняем комментарии и строки без '='
                    continue

                key, value = map(str.strip, stripped_line.split('=', 1))

                if key == 'server.tomcat.accesslog.enabled':
                    if value != 'true':
                        line = f"{key}=true\n"
                        is_changed = True
                elif key == 'server.tomcat.accesslog.max-days':
                    try:
                        max_days = max(int(value), UTM_LOG_DAYS)
                        if max_days > UTM_LOG_DAYS:
                            max_days = UTM_LOG_DAYS

                    except ValueError:
                        max_days = UTM_LOG_DAYS

                    if value != str(max_days):
                        line = f"{key}={max_days}\n"
                        is_changed = True

                updated_lines.append(line)

            # Записываем только если были изменения
            if is_changed:
                with open(UTM_CONFING, 'w', encoding='utf-8') as f:
                    f.writelines(updated_lines)

                self.logger.info(f"Файл {UTM_CONFING} успешно обновлён")
            else:
                self.logger.debug(f"Изменения в файле {UTM_CONFING} не потребовались")

        except Exception as e:
            self.logger.error(f"Ошибка обработки конфига UTM, файла {UTM_CONFING}: {e}")

    def update_anydesk_id(self):
        anydesk_id = ''

        conf_paths = [
            ANYDESK_PROGDATA,
            ANYDESK_APPDATA,
        ]

        for path in conf_paths:
            if not path.exists():
                continue

            try:
                # Чтение содержимого файла
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    data = f.read().strip()
                    if not data:  # Пропускаем пустые файлы
                        continue

                # Парсинг содержимого файла
                config_anydesk = configparser.ConfigParser()
                config_anydesk.read_string(f'[DEF]\n{data}')  # Добавляем секцию для корректного парсинга
                anydesk_id = config_anydesk.get('DEF', 'ad.anynet.id', fallback='')

                if anydesk_id:  # Если ID найден, выходим из цикла
                    break

            except Exception as e:
                self.logger.debug(f'Ошибка обработки файла {path}: {e}')

        self.update_param('anydesk_id', anydesk_id)

    def check_autorun(self):
        """
        Проверяет наличие ключа реестра UTMMonitor в автозапуске для текущего пользователя и создаёт его, если он отсутствует.
        """

        if not PROGRAM_EXE.exists():
            self.logger.error(f'Файл программы не найден: {PROGRAM_EXE}')
            return

        HKCU = winreg.HKEY_CURRENT_USER
        subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
        value_name = "UTMMonitor"
        value_data = f'\"{PROGRAM_EXE}\"'

        try:
            # Открываем ветку для проверки
            with winreg.ConnectRegistry(None, HKCU) as reg:
                with winreg.OpenKey(reg, subkey, 0, winreg.KEY_READ) as key:
                    try:
                        current_value, _ = winreg.QueryValueEx(key, value_name)
                        if current_value == value_data:
                            self.logger.debug(f"Ключ {value_name} уже существует с корректным значением: {current_value}")
                            return
                        else:
                            self.logger.info(f"Ключ {value_name} существует, но значение отличается: {current_value}. Обновляем.")
                    except FileNotFoundError:
                        self.logger.info(f"Ключ {value_name} не найден, создаём новый.")

                # Создаём или обновляем ключ
                with winreg.OpenKey(reg, subkey, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, value_data)
                    self.logger.info(f"Ключ {value_name} успешно создан/обновлён с значением: {value_data}")

        except Exception as e:
            self.logger.error(f"Ошибка работы с реестром: {e}")

    def ensure_autostart_enabled(self):
        """
        Проверяет, отключён ли автозапуск UTMMonitor в StartupApproved, и включает его, если нужно.
        """
        if not startup_approved_supported:
            return

        HKCU = winreg.HKEY_CURRENT_USER
        subkey = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
        value_name = "UTMMonitor"

        try:
            with winreg.ConnectRegistry(None, HKCU) as reg:
                with winreg.OpenKey(reg, subkey, 0, winreg.KEY_READ) as key:
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                        if value[0] == 3:  # Отключён
                            self.logger.info("Автозапуск UTMMonitor отключён пользователем, включаем")
                        else:
                            return
                    except FileNotFoundError:
                        return

                # Включаем автозапуск, если он отключён
                with winreg.OpenKey(reg, subkey, 0, winreg.KEY_SET_VALUE) as key:
                    # Устанавливаем 12-байтовое значение: [2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
                    array = [2]
                    array.extend([0 for _ in range(11)])
                    enabled_value = bytes(array)
                    winreg.SetValueEx(key, value_name, 0, winreg.REG_BINARY, enabled_value)
                    self.logger.info("Автозапуск UTMMonitor включён в StartupApproved")

        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.error(f"Ошибка работы с StartupApproved: {e}")

    def disable_autostart(self):
        """
        Отключает автозапуск UTMMonitor для текущего пользователя.
        Удаляет ключ из Run и помечает как отключённый в StartupApproved.
        """

        HKCU = winreg.HKEY_CURRENT_USER
        run_subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
        approved_subkey = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
        value_name = "UTMMonitor"

        try:
            # Удаляем ключ из Run
            with winreg.ConnectRegistry(None, HKCU) as reg:
                try:
                    with winreg.OpenKey(reg, run_subkey, 0, winreg.KEY_SET_VALUE) as key:
                        winreg.DeleteValue(key, value_name)
                        self.logger.info(f"Ключ {value_name} удалён из Run")
                except FileNotFoundError:
                    self.logger.debug(f"Ключ {value_name} отсутствует в Run")

                # Помечаем как отключённый в StartupApproved (если поддерживается)
                if startup_approved_supported:
                    try:
                        with winreg.OpenKey(reg, approved_subkey, 0, winreg.KEY_SET_VALUE) as key:
                            # 12 байт: [3, 0, 0, 0, ...] = отключено
                            disabled_value = bytes([3] + [0] * 11)
                            winreg.SetValueEx(key, value_name, 0, winreg.REG_BINARY, disabled_value)
                            self.logger.info("Автозапуск UTMMonitor помечен как отключённый в StartupApproved")
                    except FileNotFoundError:
                        self.logger.debug("Раздел StartupApproved отсутствует")

        except Exception as e:
            self.logger.error(f"Ошибка при отключении автозапуска: {e}")

    def maintain_autostart(self):
        """Поддерживает автозапуск UTMMonitor включённым."""

        if is_frozen_executable:
            if self.inn in DISABLED_INN:
                self.disable_autostart()
            else:
                self.check_autorun()       # Проверяем и создаём ключ в Run
                self.ensure_autostart_enabled()  # Проверяем и включаем в StartupApproved

    def update_data(self):
        self.update_utm_port()
        self.update_utm_data()

    def update_utm_data(self):
        """Попытка получения свежих данных INN, KPP и EGAIS_ID с UTM."""
        data = self.get_data_from_rutoken()

        if data['egais_id'] == '':
            data = self.get_data_from_utm()

        self.update_param('inn', data['inn'])
        self.update_param('kpp', data['kpp'])
        self.update_param('egais_id', data['egais_id'])
        self.update_param('rsa', data['rsa'])
        self.update_param('gost', data['gost'])
        self.update_param('token_ver', data['token_ver'])
        self.update_param('total_memory', data['total_memory'])
        self.update_param('free_memory', data['free_memory'])
        self.update_param('token_status', data['token_status'])
        self.update_param('utm_version', self.get_utm_version())

    def get_utm_version(self) -> str:
        self.logger.debug('Получение версии из UTM')

        try:
            r = httpx.get(f'{self.utm_url}/info/version', timeout=10)
            text = r.text.strip()
            build_no = text.split("b")[-1].lstrip("0")

        except Exception:
            build_no = ''

        return build_no

    def get_data_from_utm(self):
        """Попытка получения свежих данных INN, KPP и EGAIS_ID с UTM."""
        self.logger.debug('Получение данных из UTM')

        data = {'inn': '', 'kpp': '', 'egais_id': '', 'rsa': '', 'gost': '', 'token_ver': '', 'total_memory': '', 'free_memory': '', 'token_status': 'un'}

        try:
            r = httpx.get(f'{self.utm_url}/diagnosis', timeout=10)

            if r.status_code == 200:
                # Разбираем XML ответ с помощью ElementTree
                root = ET.fromstring(r.text)

                # Извлечение и безопасное получение данных
                inn = root.findtext('OU', '').strip()
                kpp = root.findtext('L', '').strip()
                egais_id = root.findtext('CN', '').strip()

                self.logger.debug(f'inn={inn}, kpp={kpp}, egais_id={egais_id}')

                if inn in VALID_INN:
                    rsa, gost = self.get_sert_info()
                    self.logger.debug(f'rsa={rsa}, gost={gost}')

                    data['inn'] = inn
                    data['kpp'] = kpp
                    data['egais_id'] = egais_id
                    data['rsa'] = rsa
                    data['gost'] = gost

                    if any((rsa, gost)):
                        data['token_status'] = 'ok'
            else:
                self.logger.error(f'Статус ответа от UTM, URL: {self.utm_url}, STATUS_CODE: {r.status_code}')

        except httpx.ConnectTimeout:
            self.logger.info(f'UTM не отвечает (таймаут подключения): {self.utm_url}')

        except httpx.ConnectError as e:
            self.logger.info(f'UTM не запущен или недоступен: {self.utm_url}, ошибка: {e}')

        except (socket.error, OSError) as e:
            self.logger.info(f'Сетевое исключение при подключении к UTM: {e}')

        except Exception as e:
            self.logger.error(f'Неизвестная ошибка при подключении к UTM: {self.utm_url}, ERROR: {e}')

        return data

    def get_sert_info(self):
        """Получение сертификатной информации из UTM."""
        def fetch_certificate(url):
            try:
                response = httpx.get(url, timeout=30)
                if response.status_code == 200:
                    cert_data = response.json().get('to', '')
                    return arrow.get(cert_data, 'DD.MM.YYYY HH:mm:ssZ').format('YYYY-MM-DD HH:mm:SS') if cert_data else ''
                else:
                    return ''
            except Exception as e:
                self.logger.warning(f'Ошибка при запросе по сертификату {url}: {e}')
            return ''

        rsa_end_date = fetch_certificate(f'{self.utm_url}/api/rsa/orginfo')
        gost_end_date = fetch_certificate(f'{self.utm_url}/api/gost/orginfo')

        return rsa_end_date, gost_end_date

    def get_data_from_rutoken(self):
        """Попытка получения данных INN, KPP и EGAIS_ID с Rutoken."""
        self.logger.debug('Получение данных из Rutoken')

        data = {'inn': '', 'kpp': '', 'egais_id': '', 'rsa': '', 'gost': '', 'token_ver': '', 'total_memory': '', 'free_memory': '', 'token_status': 'un'}

        try:
            with Rutoken() as rt:
                state = rt.get_state()
                data.update(state)

        except Exception as e:
            self.logger.error(f'Ошибка при получении данных TT с Rutoken: {e}')

        return data

    @property
    def tt_data(self) -> Dict[str, Any]:
        """Возвращает актуальные данные INN, KPP и EGAIS_ID."""
        return {
            'inn': self.inn,
            'kpp': self.kpp,
            'egais_id': self.egais_id,
            'version': VERSION,
            'anydesk_id': self.anydesk_id,
            'rsa': self.rsa,
            'gost': self.gost,
            'token_ver': self.token_ver,
            'total_memory': self.total_memory,
            'free_memory': self.free_memory,
            'token_status': self.token_status,
            'utm_version': self.utm_version
        }

    def get_headers(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Формирует заголовки для запроса.

        Args:
            params (dict, optional): Дополнительные параметры для заголовков. По умолчанию None.

        Returns:
            dict: Заголовки для запроса.
        """
        headers = {
            'User-Agent': 'sells report'
        }

        tt = self.tt_data

        headers.update({
            'x-inn': tt.get('inn', ''),
            'x-kpp': tt.get('kpp', ''),
            'x-egais-id': tt.get('egais_id', ''),
            'x-version': tt.get('version', ''),
            'x-anydesk-id': tt.get('anydesk_id', ''),
            'x-rsa': tt.get('rsa', ''),
            'x-gost': tt.get('gost', ''),
            'x-token-ver': tt.get('token_ver', ''),
            'x-total-memory': tt.get('total_memory', ''),
            'x-free-memory': tt.get('free_memory', ''),
            'x-token-status': tt.get('token_status', ''),
            'x-utm-version': tt.get('utm_version', '')
        })

        if params:
            headers.update(params)

        return headers


if __name__ == "__main__":
    config = ConfigManager()
    config.update_utm_data()

    # Получение данных INN, KPP и EGAIS_ID
    tt_data = config.tt_data
    print(f'Полученные данные: {tt_data}')
    print(config.utm_url)
