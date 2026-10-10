# const.py
from pathlib import Path
import os
import sys
from helper_env import get_required, get_list

# Пути
SRC_PATH = Path(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_DRIVE = Path(Path.home().drive)

# Значения по умолчанию для переменных окружения
DEFAULT_APPDATA = Path.home() / "AppData" / "Roaming"  # %USERPROFILE%\AppData\Roaming
DEFAULT_PROGRAMDATA = SYSTEM_DRIVE / "/ProgramData"     # C:\ProgramData
DEFAULT_TEMP = Path.home() / "AppData" / "Local" / "Temp"  # %USERPROFILE%\AppData\Local\Temp

# Получаем переменные окружения с резервами
APPDATA_ROAMING = Path(os.getenv("APPDATA", DEFAULT_APPDATA))
PROGRAM_DATA = Path(os.getenv("PROGRAMDATA", DEFAULT_PROGRAMDATA))
TEMP_DIR = Path(os.getenv("TEMP", DEFAULT_TEMP))

# Пути самой программы и её комонентов
INSTALLED_EXE = APPDATA_ROAMING / 'UTMMonitor' / 'UTMMonitor.exe'

if INSTALLED_EXE.is_file():
    PROGRAM_EXE = INSTALLED_EXE
elif getattr(sys, 'frozen', False):
    PROGRAM_EXE = Path(sys.executable).resolve()
else:
    PROGRAM_EXE = INSTALLED_EXE

CONFIG_FILE = SRC_PATH / 'data' / 'config.ini'
MARKS_RESULT = SRC_PATH / 'data' / 'marks.txt'

# Пути временных папок программы
SEVEN_ZIP_FILE = SRC_PATH / 'lib' / '7z.exe'
SUB_UPDATE_FOLDER = TEMP_DIR / 'monitor_update'
UPDATE_MONITOR_BAT = SUB_UPDATE_FOLDER / 'update_monitor.bat'


# настройка остатков по бутылкам
DEL_RESTS_AFTER_MINUTES = 70

# Базовый путь для УТМ
UTM_BASE = SYSTEM_DRIVE / "/utm/transporter"

# Пути УТМ
UTM_LOG_DAYS = 7

UTM_LOG_DIR = UTM_BASE / "l"
UTM_LOG = UTM_BASE / UTM_LOG_DIR / "transport_info.log"
UTM_ARCHIVED = UTM_BASE / UTM_LOG_DIR / "archived"
UTM_BAT = UTM_BASE / "bin" / "run.bat"
UTM_EXE = UTM_BASE / "bin" / "utm.exe"
UTM_CONFING = UTM_BASE / "conf" / "transport.properties"

# Пути Анидеск
ANYDESK_PROGDATA = PROGRAM_DATA / 'AnyDesk' / 'system.conf'
ANYDESK_APPDATA = APPDATA_ROAMING / 'AnyDesk' / 'system.conf'


# Версии и URL
VERSION = get_required("VERSION")
URL_SERVER = get_required("URL_SERVER")

# Списки
VALID_INN = get_list("VALID_INN")
DISABLED_INN = get_list("DISABLED_INN")
