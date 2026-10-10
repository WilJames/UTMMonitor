import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

from pathlib import Path
from xml.sax.saxutils import escape

from dotenv import load_dotenv
import PyInstaller.__main__


# Пути проекта
BUILD_DIR = Path(__file__).resolve().parent
ROOT = BUILD_DIR.parent
WORK = BUILD_DIR / "work"
PYINSTALLER_WORK = WORK / "pyinstaller"
OUTPUT = WORK / "output"

SPEC_FILE = BUILD_DIR / "main.spec"
ISS_FILE = BUILD_DIR / "UTM Monitor.iss"
ENV_FILE = ROOT / ".env"

load_dotenv(ENV_FILE)


def get_version():
    version = os.getenv("VERSION", "").strip()

    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError("В .env необходимо указать VERSION в формате X.Y.Z")

    return version


def create_version_rc(version):
    parts = [int(part) for part in version.split(".")]
    parts.append(0)

    version_tuple = ", ".join(map(str, parts))

    content = f"""VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=({version_tuple}),
        prodvers=({version_tuple}),
        mask=0x3F,
        flags=0x0,
        OS=0x4,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0)
    ),
    kids=[
        StringFileInfo([
            StringTable(
                u'040904B0',
                [
                    StringStruct(u'CompanyName', u'James Dev'),
                    StringStruct(u'FileDescription', u'UTMMonitor'),
                    StringStruct(u'FileVersion', u'{version}'),
                    StringStruct(u'InternalName', u'UTMMonitor'),
                    StringStruct(
                        u'LegalCopyright',
                        u'Copyright (c) The James Dev'
                    ),
                    StringStruct(u'OriginalFilename', u'UTMMonitor.exe'),
                    StringStruct(u'ProductName', u'UTMMonitor'),
                    StringStruct(u'ProductVersion', u'{version}')
                ]
            )
        ]),
        VarFileInfo([VarStruct(u'Translation', [0x0419, 1200])])
    ]
)"""

    with open(WORK / 'version.rc', 'w', encoding='utf-8') as f:
        f.write(content)


def create_manifest_xml(version):
    content = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1"
          manifestVersion="1.0">
    <assemblyIdentity
        type="win32"
        name="JamesDev.Dev.UTMMonitor"
        version="{escape(version)}.0"
        processorArchitecture="*"/>
    <dependency>
        <dependentAssembly>
            <assemblyIdentity
                type="win32"
                name="Microsoft.Windows.Common-Controls"
                version="6.0.0.0"
                processorArchitecture="*"
                publicKeyToken="6595b64144ccf1df"
                language="*"/>
        </dependentAssembly>
    </dependency>
    <compatibility
        xmlns="urn:schemas-microsoft-com:compatibility.v1">
        <application>
            <supportedOS Id="{{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}}"/>
            <supportedOS Id="{{1f676c76-80e1-4239-95bb-83d0f6d0da78}}"/>
            <supportedOS Id="{{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}}"/>
            <supportedOS Id="{{35138b9a-5d96-4fbd-8e2d-a2440225f93a}}"/>
        </application>
    </compatibility>
    <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
        <security>
            <requestedPrivileges>
                <requestedExecutionLevel
                    level="asInvoker"
                    uiAccess="false"/>
            </requestedPrivileges>
        </security>
    </trustInfo>
</assembly>"""

    with open(WORK / 'manifest.xml', 'w', encoding='utf-8') as f:
        f.write(content)


def prepare_workspace():
    if WORK.exists():
        shutil.rmtree(WORK)

    PYINSTALLER_WORK.mkdir(parents=True)
    OUTPUT.mkdir(parents=True)


def copy_runtime_env(app_dir):
    """Копирует .env в приложение без сборочных секретов."""

    if not ENV_FILE.is_file():
        print(".env не найден, копирование пропущено.")
        return

    excluded_keys = {
        "SIGN_CERT_PATH",
        "SIGN_CERT_PASSWORD",
        "SIGNTOOL_PATH",
        "ISCC_PATH",
        "UPDATE_SERVER_PATH",
        "UPDATE_INSTALLER_PATH",
        "SEND_BUILD"
    }

    output_lines = []

    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()

        # Оставляем комментарии и пустые строки.
        if not stripped or stripped.startswith("#") or "=" not in line:
            output_lines.append(line)
            continue

        key = line.split("=", 1)[0].strip()

        if key not in excluded_keys:
            output_lines.append(line)

    runtime_env = app_dir / ".env"
    runtime_env.write_text("\n".join(output_lines) + "\n", encoding="utf-8")

    print(f"Подготовлен пользовательский .env: {runtime_env}")


def build_project():
    print("Сборка PyInstaller...")

    PyInstaller.__main__.run([
        str(SPEC_FILE),
        "--clean",
        "--noconfirm",
        "--distpath", str(OUTPUT),
        "--workpath", str(PYINSTALLER_WORK)
    ])

    app_dir = OUTPUT / "UTMMonitor"

    if not (app_dir / "UTMMonitor.exe").is_file():
        raise RuntimeError(f"PyInstaller не создал ожидаемый файл: {app_dir / 'UTMMonitor.exe'}")

    # Настройки рядом с EXE, а не внутри исполняемого файла.
    copy_runtime_env(app_dir)

    return app_dir


def sign_executable(exe_path):
    cert_path = os.getenv("SIGN_CERT_PATH", "").strip()
    cert_password = os.getenv("SIGN_CERT_PASSWORD", "")
    signtool = os.getenv("SIGNTOOL_PATH", "").strip()

    if not cert_path or not cert_password or not signtool:
        raise RuntimeError("Для подписи укажи SIGN_CERT_PATH, SIGN_CERT_PASSWORD и SIGNTOOL_PATH в локальном .env")

    cert_path = Path(cert_path)
    signtool = Path(signtool)

    if not cert_path.is_file():
        raise FileNotFoundError(f"Сертификат не найден: {cert_path}")

    if not signtool.is_file():
        raise FileNotFoundError(f"signtool.exe не найден: {signtool}")

    command = [
        str(signtool),
        "sign",
        "/f", str(cert_path),
        "/p", cert_password,
        "/fd", "sha256",
        "/tr", "http://timestamp.digicert.com",
        "/td", "sha256",
        "/v", str(exe_path)
    ]

    print("Подписываю EXE...")
    subprocess.run(command, check=True)

    print("Подпись EXE выполнена.")


def create_archive(app_dir, version):
    archive_path = OUTPUT / "UTMMonitor.zip"

    print("Создаю ZIP...")

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in app_dir.rglob("*"):
            if path.is_file():
                archive_name = Path("UTMMonitor") / path.relative_to(app_dir)
                archive.write(path, archive_name.as_posix())

    md5 = hashlib.md5()

    with archive_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            md5.update(chunk)

    info = {
        "version": version,
        "md5": md5.hexdigest(),
    }

    with open(OUTPUT / "info.json", 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=4)

    print(f"Архив: {archive_path}")
    print(f"MD5: {md5.hexdigest()}")

    return archive_path


def build_installer():
    iscc = os.getenv("ISCC_PATH", "").strip()

    if iscc:
        compiler = Path(iscc)
    else:
        compiler = Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")

    if not compiler.is_file():
        print("Inno Setup не найден. Установщик пропущен.")
        print("При необходимости укажи ISCC_PATH в .env.")
        return

    if not ISS_FILE.is_file():
        raise FileNotFoundError(f"Не найден сценарий Inno Setup: {ISS_FILE}")

    env = os.environ.copy()

    print("Собираю установщик...")

    subprocess.run(
        [str(compiler), str(ISS_FILE)],
        cwd=BUILD_DIR,
        env=env,
        check=True,
    )

    print(f"Установщик: {OUTPUT / 'UTMMonitor.exe'}")


def send_server(src, dest):
    """Копирует файл в каталог назначения с отображением прогресса."""

    src = Path(src)
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_file = dest_dir / src.name
    file_size = src.stat().st_size
    copied = 0
    chunk_size = 1024 * 1024

    print(f"Копирование: {src.name} -> {dest_dir}")

    with src.open("rb") as src_file, dest_file.open("wb") as dst:
        while True:
            chunk = src_file.read(chunk_size)
            if not chunk:
                break

            dst.write(chunk)
            copied += len(chunk)

            if file_size:
                percent = copied / file_size * 100
                print(f"\r  Прогресс: {percent:.1f}%", end="", flush=True)

    print("\nКопирование завершено.")


def publish_build():
    """Отправляет архив, информацию о версии и установщик на сервер."""

    update_path = os.getenv("UPDATE_SERVER_PATH", "").strip()
    installer_path = os.getenv("UPDATE_INSTALLER_PATH", "").strip()

    archive_path = OUTPUT / "UTMMonitor.zip"
    info_path = OUTPUT / "info.json"
    installer_file = OUTPUT / "UTMMonitor.exe"

    if not update_path:
        print("UPDATE_SERVER_PATH не задан. Публикация пропущена.")
        return

    if os.getenv("UPDATE_SERVER_PATH", "false").strip().lower() != "true":
        print('Публикация выключена, пропускаю.')
        return

    for file_path in (archive_path, info_path):
        if not file_path.is_file():
            raise FileNotFoundError(f"Файл для публикации не найден: {file_path}")

    print("\nПубликация обновления...")

    send_server(archive_path, update_path)
    send_server(info_path, update_path)

    if installer_file.is_file():
        # Установщик отправляем в основной каталог обновлений.
        send_server(installer_file, update_path)

        # Дополнительно — в каталог с программами, если он указан.
        if installer_path:
            send_server(installer_file, installer_path)
    else:
        print("Установщик не найден, его отправка пропущена.")

    print("Публикация завершена.")


def main():
    parser = argparse.ArgumentParser(description="Сборка UTMMonitor")
    parser.add_argument("--sign", action="store_true", help="Подписать EXE сертификатом")
    parser.add_argument("--no-installer", action="store_true", help="Не собирать установщик")
    args = parser.parse_args()

    if not SPEC_FILE.is_file():
        raise FileNotFoundError(f"Не найден файл: {SPEC_FILE}")

    version = get_version()

    print(f"UTMMonitor {version}")
    print(f"Корень проекта: {ROOT}")

    prepare_workspace()
    create_version_rc(version)
    create_manifest_xml(version)

    app_dir = build_project()
    exe_path = app_dir / "UTMMonitor.exe"

    if args.sign:
        sign_executable(exe_path)

    create_archive(app_dir, version)

    if not args.no_installer:
        build_installer()

    publish_build()

    print()
    print("Сборка завершена.")
    print(f"Результаты: {OUTPUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
