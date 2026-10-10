from __future__ import annotations

"""
Rutoken PKCS#11 helper (Windows, rtpkcs11ecp.dll) — 24/7 safe version

Цели:
- Сервис 24/7: никаких падений наружу (исключения ловим и превращаем в state).
- Данные актуальные: опрос у тебя ~ раз в 5 минут => TTL кэша по умолчанию 300с.
- Токен могут вытащить/вставить: token_status должен корректно меняться.
- Сертификат может истечь: is_valid считается по now при каждом get_state().
- Thread-safe: все обращения к DLL под RLock.
- PKCS#11 init/finalize: глобальный ref-count, чтобы не словить double-init/finalize.
- Сохранена оригинальная логика:
  - парсим subject через SubjectParser => получаем inn/kpp/egais_id
  - latest_rsa выбираем среди RSA с inn in VALID_INN
  - latest_gost выбираем просто по end
"""

import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import arrow
from asn1crypto import x509
from ctypes import (
    WinDLL,
    Structure,
    POINTER,
    byref,
    cast,
    create_string_buffer,
    sizeof,
    c_ulong,
    c_void_p,
    c_ubyte,
    c_bool,
)
from const import VALID_INN

# =========================================================
# CONFIG (правь здесь)
# =========================================================

DLL_PATH = Path(__file__).resolve().parent / "lib" / "rtpkcs11ecp.dll"

# У тебя сервис просит новые данные примерно раз в 5 минут
STATE_TTL_SEC = 300

# Если произошла ошибка общения с токеном/драйвером, не долбим DLL
ERROR_COOLDOWN_SEC = 5

# Небольшой retry для "временных" сбоев
RETRY_COUNT = 2
RETRY_DELAY_SEC = 0.2


# При необходимости сохраняй поведение твоего кода (+3 часа)
SHIFT_HOURS = 3

# =========================================================
# PKCS#11 CONSTANTS
# =========================================================

TOKEN_CLASSES = {
    0x0: "S", 0x1: "ECP", 0x2: "LITE", 0x3: "WEB",
    0x4: "PINPAD", 0x5: "MIKRON", 0x9: "ECPDUAL",
}

CKA_CLASS = 0x00000000
CKA_VALUE = 0x00000011
CKO_CERTIFICATE = 0x00000001
CKF_SERIAL_SESSION = 0x00000004

CKR_OK = 0x00000000
CKR_ATTRIBUTE_TYPE_INVALID = 0x00000030

MEMORY_THRESHOLD = 65536


# =========================================================
# ctypes structs
# =========================================================

class CK_ATTRIBUTE(Structure):
    _fields_ = [
        ("type", c_ulong),
        ("pValue", c_void_p),
        ("ulValueLen", c_ulong),
    ]


class CK_TOKEN_INFO_EXTENDED(Structure):
    _fields_ = [
        ("ulSizeofThisStructure", c_ulong),
        ("ulTokenType", c_ulong),
        ("ulProtocolNumber", c_ulong),
        ("ulMicrocodeNumber", c_ulong),
        ("ulOrderNumber", c_ulong),
        ("flags", c_ulong),
        ("ulMaxAdminPinLen", c_ulong),
        ("ulMinAdminPinLen", c_ulong),
        ("ulMaxUserPinLen", c_ulong),
        ("ulMinUserPinLen", c_ulong),
        ("ulMaxAdminRetryCount", c_ulong),
        ("ulAdminRetryCountLeft", c_ulong),
        ("ulMaxUserRetryCount", c_ulong),
        ("ulUserRetryCountLeft", c_ulong),
        ("serialNumber", c_ubyte * 8),
        ("ulTotalMemory", c_ulong),
        ("ulFreeMemory", c_ulong),
        ("ATR", c_ubyte * 64),
        ("ulATRLen", c_ulong),
        ("ulTokenClass", c_ulong),
    ]


# =========================================================
# Subject parsing — как в оригинале
# =========================================================

class SubjectParser:
    RSA_KEYS = {
        "CN": "egais_id",
        "OU": "inn",
        "L": "kpp",
        "common_name": "egais_id",
        "organizational_unit_name": "inn",
        "locality_name": "kpp",
    }

    GOST_KEYS = {
        "CN": "fio",
        "common_name": "fio",
        "1.2.643.100.3": "snils",
        "E": "email",
        "email_address": "email",
    }

    @staticmethod
    def parse(subject: Any, cert_type: str) -> Dict[str, Any]:
        keys = SubjectParser.RSA_KEYS if cert_type == "RSA" else SubjectParser.GOST_KEYS
        if not isinstance(subject, dict):
            return {}
        # как у тебя: {keys.get(k,k): v for k,v in subject.items() if k in keys}
        return {keys.get(k, k): v for k, v in subject.items() if k in keys}  # type: ignore


# =========================================================
# Helpers
# =========================================================

def safe_format_date(dt: Optional[arrow.Arrow]) -> str:
    try:
        return dt.format("YYYY-MM-DD HH:mm:ss") if dt else ""
    except Exception:
        return ""


def _check(rv: int, stage: str) -> None:
    if rv != CKR_OK:
        raise RuntimeError(f"{stage}: rv=0x{rv:08X}")


def _determine_cert_type(signature_algorithm: str) -> str:
    alg = (signature_algorithm or "").lower()
    return "RSA" if "rsa" in alg else "GOST" if ("gost" in alg or "1.2.643.7.1.1.3.2" in alg) else "N/A"


def _parse_certificate_der(cert_der: bytes) -> Optional[Dict[str, Any]]:
    """
    Возвращает dict, совместимый с твоим оригинальным to_dict()+SubjectParser,
    либо None если не сертификат/битый.
    """
    try:
        cert = x509.Certificate.load(cert_der)
        sig_alg = cert["signature_algorithm"]["algorithm"].native
        cert_type = _determine_cert_type(sig_alg)

        subject = cert["tbs_certificate"]["subject"].native
        serial_number = cert["tbs_certificate"]["serial_number"].native

        nb_native = cert["tbs_certificate"]["validity"]["not_before"].native
        na_native = cert["tbs_certificate"]["validity"]["not_after"].native

        not_valid_before = arrow.get(str(nb_native)).shift(hours=SHIFT_HOURS)
        not_valid_after = arrow.get(str(na_native)).shift(hours=SHIFT_HOURS)

        return {
            "type": cert_type,
            "algorithm": sig_alg,
            "start": not_valid_before,
            "end": not_valid_after,
            "sn": serial_number,
            "subject": subject,
        }
    except Exception:
        return None


# =========================================================
# Global PKCS#11 init/finalize (ref-count)
# =========================================================

_pkcs_lock = threading.RLock()
_init_lock = threading.Lock()
_init_count = 0


# =========================================================
# Rutoken — main
# =========================================================

class Rutoken:
    """
    24/7 safe Rutoken reader.
    Внешний API:
      - get_state() -> Dict (никогда не бросает)
      - is_valid(state) -> bool
    """

    def __init__(self, dll_path: Path = DLL_PATH):
        self._dll_path = Path(dll_path)
        if not self._dll_path.exists():
            raise RuntimeError(f"PKCS#11 DLL not found: {self._dll_path}")

        self.dll = WinDLL(str(self._dll_path))
        self._setup_functions()
        self._initialize_global()

        # cache/cooldown
        self._state_lock = threading.Lock()
        self._last_state: Optional[Dict[str, Any]] = None
        self._last_probe_ts = 0.0
        self._cooldown_until = 0.0

    # ----------- lifecycle -----------
    def close(self) -> None:
        self._finalize_global()

    def __enter__(self) -> "Rutoken":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def refresh(self) -> None:
        """Принудительно сбросить TTL-кэш (следующий get_state пойдёт в токен)."""
        with self._state_lock:
            self._last_probe_ts = 0.0

    # ----------- safe public API -----------
    def get_state(self) -> Dict[str, Any]:
        """
        Возвращает состояние. Никогда не бросает.
        token_status:
          - 'ok' -> есть токен/данные
          - 'un' -> токена нет / слотов нет / не удалось прочитать (после retries)
        """
        now = time.time()
        with self._state_lock:
            # если в cooldown — возвращаем последнее (если было), иначе минимальный un
            if now < self._cooldown_until:
                return self._last_state if self._last_state is not None else {"token_status": "un"}

            # TTL cache
            if self._last_state is not None and (now - self._last_probe_ts) < STATE_TTL_SEC:
                # is_valid считаем "живьём", не кэшируем
                st = dict(self._last_state)
                st["is_valid"] = self.is_valid(st)
                return st

        # вне lock читаем токен (чтобы не блокировать другие потоки на долгий IO)
        state = self._probe_state_safe()

        with self._state_lock:
            self._last_state = state
            self._last_probe_ts = time.time()
            # is_valid тоже добавим
            state2 = dict(state)
            state2["is_valid"] = self.is_valid(state2)
            self._last_state = state2
            return state2

    def egais_id(self):
        state = self.get_state()
        return state.get('egais_id', '')

    def is_valid(self, state: Dict[str, Any]) -> bool:
        """
        Валиден если:
        - token_status == ok
        - rsa_end > now и gost_end > now
        """
        if state.get("token_status") != "ok":
            return False

        now = arrow.now()

        rsa_str = state.get("rsa") or ""
        gost_str = state.get("gost") or ""

        try:
            rsa_end = arrow.get(rsa_str) if rsa_str else arrow.get("2000-01-01")
        except Exception:
            rsa_end = arrow.get("2000-01-01")

        try:
            gost_end = arrow.get(gost_str) if gost_str else arrow.get("2000-01-01")
        except Exception:
            gost_end = arrow.get("2000-01-01")

        return rsa_end > now and gost_end > now

    # ----------- internal safe probe -----------
    def _probe_state_safe(self) -> Dict[str, Any]:
        last_err: Optional[str] = None

        for attempt in range(RETRY_COUNT + 1):
            try:
                st = self._probe_state()
                # если всё ок — убираем cooldown
                with self._state_lock:
                    self._cooldown_until = 0.0
                return st
            except Exception as e:
                last_err = str(e)
                if attempt >= RETRY_COUNT:
                    with self._state_lock:
                        self._cooldown_until = time.time() + ERROR_COOLDOWN_SEC
                    # "глобально по дефолту": сервис не ломаем, возвращаем un + error
                    return {
                        "token_status": "un",
                        "error": last_err,
                        "timestamp": arrow.now().isoformat(),
                    }
                time.sleep(RETRY_DELAY_SEC)

        return {"token_status": "un", "error": last_err or "unknown"}

    # ----------- internal actual probe (may raise) -----------
    def _probe_state(self) -> Dict[str, Any]:
        slots = self._get_slot_list()
        if not slots:
            return {"token_status": "un"}

        # Оригинальная логика: перебираем слоты и возвращаем как только нашли rsa/gost или token_info
        for slot_id in slots:
            token_info = self._get_token_info_extended(slot_id)

            with self._session(slot_id) as session:
                rsa, gost = self._get_latest_certs_from_session(session)

            if rsa or gost or token_info:
                data: Dict[str, Any] = {
                    "inn": "",
                    "kpp": "",
                    "egais_id": "",
                    "rsa": "",
                    "gost": "",
                    "token_ver": "",
                    "total_memory": "",
                    "free_memory": "",
                    "token_status": "ok",
                    "timestamp": arrow.now().isoformat(),
                }

                if rsa:
                    data["inn"] = rsa.get("inn", "") or ""
                    data["kpp"] = rsa.get("kpp", "") or ""
                    data["egais_id"] = rsa.get("egais_id", "") or ""
                    data["rsa"] = safe_format_date(rsa.get("end"))

                if gost:
                    data["gost"] = safe_format_date(gost.get("end"))

                if token_info:
                    data.update(token_info)

                return data

        return {"token_status": "un"}

    # ----------- pkcs session helper -----------
    class _SessionCtx:
        def __init__(self, outer: "Rutoken", slot_id: int):
            self.outer = outer
            self.slot_id = slot_id
            self.session = c_ulong(0)

        def __enter__(self) -> int:
            with _pkcs_lock:
                rv = self.outer.dll.C_OpenSession(
                    c_ulong(self.slot_id),
                    c_ulong(CKF_SERIAL_SESSION),
                    None,
                    None,
                    byref(self.session),
                )
                _check(rv, "C_OpenSession")
            return int(self.session.value)

        def __exit__(self, exc_type, exc, tb) -> None:
            with _pkcs_lock:
                if self.session.value:
                    rv = self.outer.dll.C_CloseSession(self.session)
                    # CloseSession может вернуть ошибку при выдернутом токене — не пробрасываем дальше
                    # но внутри probe мы ловим исключения, так что можно и check'нуть.
                    # Стабильность выше если не рушить закрытием.
                    if rv == CKR_OK:
                        self.session.value = 0

    def _session(self, slot_id: int) -> "Rutoken._SessionCtx":
        return Rutoken._SessionCtx(self, slot_id)

    # ----------- pkcs init/finalize -----------
    def _initialize_global(self) -> None:
        global _init_count
        with _init_lock:
            if _init_count == 0:
                _check(self.dll.C_Initialize(None), "C_Initialize")
            _init_count += 1

    def _finalize_global(self) -> None:
        global _init_count
        with _init_lock:
            if _init_count <= 0:
                return
            _init_count -= 1
            if _init_count == 0:
                # Finalize ошибки не должны валить сервис
                self.dll.C_Finalize(None)

    # ----------- function signatures -----------
    def _setup_functions(self) -> None:
        funcs = {
            "C_Initialize": ([c_void_p], c_ulong),
            "C_Finalize": ([c_void_p], c_ulong),
            "C_GetSlotList": ([c_bool, POINTER(c_ulong), POINTER(c_ulong)], c_ulong),
            "C_OpenSession": ([c_ulong, c_ulong, c_void_p, c_void_p, POINTER(c_ulong)], c_ulong),
            "C_CloseSession": ([c_ulong], c_ulong),
            "C_GetAttributeValue": ([c_ulong, c_ulong, POINTER(CK_ATTRIBUTE), c_ulong], c_ulong),
            "C_FindObjectsInit": ([c_ulong, POINTER(CK_ATTRIBUTE), c_ulong], c_ulong),
            "C_FindObjects": ([c_ulong, POINTER(c_ulong), c_ulong, POINTER(c_ulong)], c_ulong),
            "C_FindObjectsFinal": ([c_ulong], c_ulong),
            "C_EX_GetTokenInfoExtended": ([c_ulong, POINTER(CK_TOKEN_INFO_EXTENDED)], c_ulong),
        }
        for fname, (argtypes, restype) in funcs.items():
            func = getattr(self.dll, fname)
            func.argtypes = argtypes
            func.restype = restype

    # ----------- low-level ops -----------
    def _get_slot_list(self) -> List[int]:
        with _pkcs_lock:
            slot_count = c_ulong(0)
            _check(self.dll.C_GetSlotList(True, None, byref(slot_count)), "C_GetSlotList(count)")

            if slot_count.value == 0:
                return []

            slot_list = (c_ulong * slot_count.value)()
            _check(self.dll.C_GetSlotList(True, slot_list, byref(slot_count)), "C_GetSlotList(list)")
            return [int(x) for x in slot_list]

    def _get_token_info_extended(self, slot_id: int) -> Dict[str, Any]:
        token_info = CK_TOKEN_INFO_EXTENDED(ulSizeofThisStructure=c_ulong(sizeof(CK_TOKEN_INFO_EXTENDED)))
        with _pkcs_lock:
            _check(self.dll.C_EX_GetTokenInfoExtended(c_ulong(slot_id), byref(token_info)), "C_EX_GetTokenInfoExtended")

        token_type = TOKEN_CLASSES.get(int(token_info.ulTokenType), "UNKNOWN")
        version = (
            "3.0" if token_type == "ECP" and int(token_info.ulTotalMemory) > MEMORY_THRESHOLD
            else "2.0" if token_type == "ECP"
            else "Lite"
        )
        return {
            "token_ver": version,
            "total_memory": int(token_info.ulTotalMemory),
            "free_memory": int(token_info.ulFreeMemory),
        }

    def _find_certificates(self, session: int) -> List[int]:
        with _pkcs_lock:
            cko_certificate = c_ulong(CKO_CERTIFICATE)
            template = CK_ATTRIBUTE(
                type=c_ulong(CKA_CLASS),
                pValue=cast(byref(cko_certificate), c_void_p),
                ulValueLen=c_ulong(sizeof(c_ulong)),
            )

            _check(self.dll.C_FindObjectsInit(c_ulong(session), byref(template), c_ulong(1)), "C_FindObjectsInit")

            certificates: List[int] = []
            obj_handle = c_ulong(0)
            obj_count = c_ulong(0)

            try:
                while True:
                    _check(self.dll.C_FindObjects(c_ulong(session), byref(obj_handle), c_ulong(1), byref(obj_count)), "C_FindObjects")
                    if obj_count.value == 0:
                        break
                    certificates.append(int(obj_handle.value))
            finally:
                # Final может упасть при выдернутом токене — но мы в probe ловим исключения
                self.dll.C_FindObjectsFinal(c_ulong(session))

            return certificates

    def _get_attribute_value(self, session: int, obj_handle: int, attr_type: int) -> Optional[bytes]:
        with _pkcs_lock:
            attr = CK_ATTRIBUTE(type=c_ulong(attr_type), pValue=None, ulValueLen=c_ulong(0))
            rv = self.dll.C_GetAttributeValue(c_ulong(session), c_ulong(obj_handle), byref(attr), c_ulong(1))

            if rv == CKR_ATTRIBUTE_TYPE_INVALID:
                return None
            _check(rv, "C_GetAttributeValue(size)")

            if attr.ulValueLen == 0:
                return None

            buf = create_string_buffer(int(attr.ulValueLen))
            attr.pValue = cast(buf, c_void_p)
            # удержим буфер от GC
            attr._buf = buf  # type: ignore[attr-defined]

            _check(self.dll.C_GetAttributeValue(c_ulong(session), c_ulong(obj_handle), byref(attr), c_ulong(1)), "C_GetAttributeValue(value)")
            return bytes(buf.raw[: int(attr.ulValueLen)])

    def _get_latest_certs_from_session(self, session: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        cert_handles = self._find_certificates(session)

        slot_certs: Dict[str, List[Dict[str, Any]]] = {"RSA": [], "GOST": []}

        for handle in cert_handles:
            cert_der = self._get_attribute_value(session, handle, CKA_VALUE)
            if not cert_der:
                continue

            cert_dict = _parse_certificate_der(cert_der)
            if not cert_dict:
                continue

            if cert_dict["type"] not in ("RSA", "GOST"):
                continue

            # ВАЖНО: сохраняем оригинальную логику
            subj = cert_dict.pop("subject", {}) or {}
            cert_dict.update(SubjectParser.parse(subj, cert_dict["type"]))

            slot_certs[cert_dict["type"]].append(cert_dict)

        latest_rsa = max(
            (c for c in slot_certs["RSA"] if c.get("inn") in VALID_INN),
            key=lambda x: x["end"],
            default=None,
        )
        latest_gost = max(slot_certs["GOST"], key=lambda x: x["end"], default=None)

        return latest_rsa, latest_gost


# =========================================================
# Demo
# =========================================================

if __name__ == "__main__":
    with Rutoken() as rt:
        egais_id = rt.egais_id()
        print(egais_id)
