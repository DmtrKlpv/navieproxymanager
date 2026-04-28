#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NaiveProxy Manager v3.9.5 - Console Only, Errors/Warnings Always Visible
"""
import os
import sys
import subprocess
import time
import ctypes
import threading
import json
import shutil
import signal
import re
from pathlib import Path
from datetime import datetime
from collections import deque
from urllib.parse import urlparse, parse_qs


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    BLACK = '\033[30m'
    BG_YELLOW = '\033[43m'
    END = '\033[0m'


# Включение ANSI для Windows 10+
if os.name == 'nt':
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except:
        pass


# === ПРОВЕРКА АДМИНА ===
def is_admin():
    """Корректная проверка прав администратора"""
    try:
        if os.name == 'nt':
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        return os.geteuid() == 0
    except:
        return False


def run_as_admin():
    """Перезапуск с правами админа с сохранением пути и аргументов"""
    try:
        params = " ".join(f'"{arg}"' for arg in sys.argv[1:])
        exe = sys.executable
        script = os.path.abspath(sys.argv[0])
        work_dir = os.path.dirname(script)

        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, f'"{script}" {params}',
            work_dir, 1
        )

        if result <= 32:
            print(f"\n{Colors.RED}Требуется запуск от имени Администратора!{Colors.END}")
            print("Нажмите ПКМ на файле → 'Запуск от имени администратора'")
            input("\nНажмите Enter для выхода...")
            sys.exit(1)

        time.sleep(1)
        sys.exit(0)

    except Exception as e:
        print(f"\n{Colors.RED}Ошибка: {e}{Colors.END}")
        input("\nНажмите Enter для выхода...")
        sys.exit(1)


def get_app_dir():
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent


APP_DIR = get_app_dir()
CONFIG_PATH = APP_DIR / "config.json"
NAIVE_PATH = APP_DIR / "naive.exe"
LOG_PATH = APP_DIR / "naive.log"
REG_SETTINGS = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# Флаг для предотвращения двойного вызова cleanup
_shutdown_in_progress = False
naive_proc = None


# === ПАРСИНГ ИНФОРМАЦИИ О ПОДКЛЮЧЕНИИ ===
def parse_proxy_info(config):
    """
    Извлекает имя подключения, хост, порт и UDP-статус из config.json
    Возвращает: (name, host, port, udp_enabled) или (None, None, None, None)
    """
    if not config:
        return None, None, None, None

    proxy = config.get("proxy", "")
    if not proxy:
        return None, None, None, None

    # Извлекаем имя (после #)
    name = None
    if "#" in proxy:
        name = proxy.split("#")[-1].strip()

    # Парсим URL для извлечения хоста, порта и параметров
    host, port, udp_enabled = None, None, None
    try:
        # Удаляем фрагмент (#...) для корректного парсинга
        url_part = proxy.split("#")[0] if "#" in proxy else proxy

        parsed = urlparse(url_part)

        # Хост и порт из netloc (может быть user:pass@host:port)
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = netloc.split("@", 1)[1]  # Убираем user:pass@

        # Обработка IPv6 [host]:port или host:port
        if netloc.startswith("["):
            match = re.match(r'\[([^\]]+)\]:(\d+)', netloc)
            if match:
                host, port = match.group(1), match.group(2)
        elif ":" in netloc:
            parts = netloc.rsplit(":", 1)
            if len(parts) == 2:
                host, port = parts[0], parts[1]
        else:
            host = netloc
            port = "443"  # default

        # Парсим параметры (?udp=1&...)
        if parsed.query:
            params = parse_qs(parsed.query)
            if "udp" in params:
                udp_val = params["udp"][0].lower()
                udp_enabled = udp_val in ("1", "true", "yes", "on")
    except:
        pass

    return name, host, port, udp_enabled


# === КОРРЕКТНОЕ ЗАВЕРШЕНИЕ ===
def refresh_proxy_settings():
    """Корректное обновление настроек прокси через WinINET с Add-Type"""
    ps_script = '''
    $sig = '[DllImport("wininet.dll",SetLastError=true)] public static extern bool InternetSetOption(IntPtr,int,IntPtr,int);'
    try {
        Add-Type -MemberDefinition $sig -Name Wininet -Namespace Win32 -PassThru -ErrorAction Stop | Out-Null
        [Win32.Wininet]::InternetSetOption(0,39,0,0) | Out-Null
        [Win32.Wininet]::InternetSetOption(0,37,0,0) | Out-Null
    } catch:
        pass
    '''
    try:
        subprocess.run(
            ['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
    except:
        pass


def stop_naive(wait: bool = True):
    """Останавливает naive с корректным ожиданием завершения"""
    global naive_proc
    if naive_proc:
        try:
            if naive_proc.poll() is None:
                naive_proc.terminate()
                if wait:
                    for _ in range(30):
                        if naive_proc.poll() is not None:
                            break
                        time.sleep(0.1)
                    if naive_proc.poll() is None:
                        naive_proc.kill()
                        try:
                            naive_proc.wait(timeout=2)
                        except:
                            pass
        except Exception as e:
            log.add(f"Ошибка остановки naive: {e}", "ERROR")
        finally:
            naive_proc = None

    if os.name == 'nt':
        subprocess.run(
            "taskkill /f /im naive.exe /t >nul 2>&1",
            shell=True, creationflags=subprocess.CREATE_NO_WINDOW
        )
    log.add("naiveproxy остановлен", "OK")


def set_system_proxy(enable: bool, config=None):
    hp, sp = parse_listen_ports(config)
    if enable:
        ps = f"http=127.0.0.1:{hp};https=127.0.0.1:{hp};socks=127.0.0.1:{sp}"
        run_cmd(f'reg add "{REG_SETTINGS}" /v ProxyEnable /t REG_DWORD /d 1 /f')
        run_cmd(f'reg add "{REG_SETTINGS}" /v ProxyServer /t REG_SZ /d "{ps}" /f')
        run_cmd(f'reg add "{REG_SETTINGS}" /v ProxyOverride /t REG_SZ /d "<local>" /f')
        log.add(f"Системный прокси ON (HTTP:{hp})", "OK")
    else:
        run_cmd(f'reg add "{REG_SETTINGS}" /v ProxyEnable /t REG_DWORD /d 0 /f')
        log.add("Системный прокси OFF", "OK")
    refresh_proxy_settings()


def cleanup(show_notification: bool = True):
    """Полная остановка с уведомлением и паузой"""
    global _shutdown_in_progress
    if _shutdown_in_progress:
        return
    _shutdown_in_progress = True

    if show_notification:
        log.add("Завершение работы...", "SYSTEM")
        print(f"\n{Colors.YELLOW}Остановка сервисов...{Colors.END}", flush=True)

    stop_naive(wait=True)
    set_system_proxy(False)

    if show_notification:
        time.sleep(0.5)
        print(f"{Colors.GREEN}Готово. До свидания!{Colors.END}\n", flush=True)
        time.sleep(0.3)


# === Обработчик закрытия консоли (Windows) ===
if os.name == 'nt':
    def console_handler(ctrl_type):
        if ctrl_type in (0, 2):
            cleanup(show_notification=True)
            time.sleep(0.2)
        return False

    try:
        HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        _handler = HandlerRoutine(console_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True)
    except:
        pass


# === Обработчик сигналов для Linux/macOS ===
def signal_handler(sig, frame):
    cleanup(show_notification=True)
    sys.exit(0)

try:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
except:
    pass


# === Логгер (Ошибки и предупреждения ВСЕГДА видны, кроме логов) ===
class Logger:
    # Уровни, которые ВСЕГДА выводятся в консоль (независимо от show_console)
    FORCE_CONSOLE_LEVELS = {"ERROR", "WARN"}
    # Уровни, которые выводятся по умолчанию
    DEFAULT_CONSOLE_LEVELS = {"ERROR", "WARN", "OK", "SYSTEM"}

    def __init__(self, max_lines=200):
        self.logs = deque(maxlen=max_lines)
        self.lock = threading.Lock()

    def add(self, msg: str, level: str = "INFO", show_console: bool = None):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] [{level}] {msg}"

        with self.lock:
            self.logs.append(entry)

        # === ИСПРАВЛЕНО: ошибки и предупреждения ВСЕГДА показываются ===
        if level in self.FORCE_CONSOLE_LEVELS:
            show_console = True
        elif show_console is None:
            show_console = level in self.DEFAULT_CONSOLE_LEVELS

        if show_console:
            try:
                color = ""
                if level == "ERROR":
                    color = Colors.RED
                elif level == "OK":
                    color = Colors.GREEN
                elif level == "WARN":
                    color = Colors.YELLOW
                elif level == "SYSTEM":
                    color = Colors.CYAN
                # === Без иконок, только цвет ===
                with threading.Lock():
                    sys.stdout.write(f'\r\033[K{color}{entry}{Colors.END}\n')
                    sys.stdout.flush()
            except:
                pass

    def notify(self, msg: str, level: str = "INFO"):
        self.add(msg, level, show_console=True)

    def debug(self, msg: str):
        self.add(msg, "DEBUG", show_console=False)

    def get_logs(self, lines: int = 100) -> str:
        with self.lock:
            return "\n".join(list(self.logs)[-lines:])

    def clear(self):
        with self.lock:
            self.logs.clear()
        self.add("Лог очищен", "SYSTEM")

    def save_to_file(self, path: str):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.get_logs(1000))
            return True
        except:
            return False


log = Logger()


def run_cmd(cmd, show_output=False):
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' and not show_output else 0
        res = subprocess.run(cmd, shell=True, capture_output=not show_output, text=True, creationflags=flags)
        return res.returncode == 0, res.stdout + res.stderr
    except:
        return False, ""


def parse_listen_ports(config):
    hp, sp = "8080", "1080"
    if not config:
        return hp, sp
    for addr in config.get("listen", []):
        if "http://" in addr:
            hp = addr.split(":")[-1]
        elif "socks://" in addr:
            sp = addr.split(":")[-1]
    return hp, sp


def is_proxy_enabled():
    s, o = run_cmd(f'reg query "{REG_SETTINGS}" /v ProxyEnable')
    return s and "0x1" in o


def start_naive():
    global naive_proc
    if naive_proc and naive_proc.poll() is None:
        return True
    if not NAIVE_PATH.exists():
        log.notify("naive.exe не найден!", "ERROR")
        return False
    try:
        naive_proc = subprocess.Popen(
            [str(NAIVE_PATH), "--config", str(CONFIG_PATH)],
            cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )

        def read_output():
            while naive_proc and naive_proc.poll() is None:
                line = naive_proc.stdout.readline()
                if line:
                    # Логи naive — только во внутренний лог, не в консоль
                    log.add(line.strip(), "NAIVE", show_console=False)
                time.sleep(0.05)

        threading.Thread(target=read_output, daemon=True).start()
        time.sleep(1)
        if naive_proc.poll() is None:
            log.notify("naiveproxy запущен", "OK")
            return True
        else:
            log.notify(f"naiveproxy завершился с кодом {naive_proc.poll()}", "ERROR")
            return False
    except Exception as e:
        log.notify(f"Ошибка запуска: {e}", "ERROR")
        return False


def view_logs():
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        cols, rows = shutil.get_terminal_size()

        print(f"{Colors.BG_YELLOW}{Colors.BLACK} ПРОСМОТР ЛОГОВ {Colors.END}".center(cols))
        print(f"{Colors.CYAN}{'=' * cols}{Colors.END}")

        logs = log.get_logs(rows - 10)
        if logs:
            for line in logs.split('\n'):
                if "[ERROR]" in line:
                    print(f"{Colors.RED}{line}{Colors.END}")
                elif "[OK]" in line:
                    print(f"{Colors.GREEN}{line}{Colors.END}")
                elif "[WARN]" in line:
                    print(f"{Colors.YELLOW}{line}{Colors.END}")
                elif "[SYSTEM]" in line:
                    print(f"{Colors.CYAN}{line}{Colors.END}")
                else:
                    print(f"{line}")
        else:
            print(f"{Colors.YELLOW}Лог пуст{Colors.END}")

        print(f"{Colors.CYAN}{'=' * cols}{Colors.END}")
        print(f"{Colors.YELLOW} [R] Обновить  [C] Очистить  [S] Сохранить  [B] Назад {Colors.END}")

        ch = input(f"{Colors.BOLD}> {Colors.END}").strip().lower()
        if ch == 'b':
            break
        elif ch == 'c':
            log.clear()
        elif ch == 's':
            if log.save_to_file(LOG_PATH):
                print(f"{Colors.GREEN}Лог сохранён: {LOG_PATH}{Colors.END}")
            else:
                print(f"{Colors.RED}Ошибка сохранения{Colors.END}")
            time.sleep(1)


def show_menu(cfg, running, proxy_on):
    w = min(shutil.get_terminal_size()[0], 60)
    os.system('cls' if os.name == 'nt' else 'clear')

    # Заголовок программы
    print(f"{Colors.BOLD}{Colors.CYAN}NaiveProxy Manager v3.9.5{Colors.END}")
    print("=" * w)

    # === ИНФОРМАЦИЯ О ПОДКЛЮЧЕНИИ ИЗ КОНФИГА ===
    if cfg:
        name, host, port, udp_enabled = parse_proxy_info(cfg)
        if name or host:
            info_parts = []
            if name:
                info_parts.append(f"{name}")
            if host:
                info_parts.append(f"{host}")
            if port:
                info_parts.append(f":{port}")

            # === Статус UDP ===
            if udp_enabled is not None:
                udp_status = f"UDP:ON" if udp_enabled else f"UDP:OFF"
                info_parts.append(udp_status)

            print("Подключение: " + " ".join(info_parts))
            print("-" * w)

    # Статусы
    st_n = f"{Colors.GREEN}RUN{Colors.END}" if running else f"{Colors.RED}STOP{Colors.END}"
    st_p = f"{Colors.GREEN}ON{Colors.END}" if proxy_on else f"{Colors.RED}OFF{Colors.END}"
    if cfg:
        hp, sp = parse_listen_ports(cfg)
        print(f"naive: {st_n} | SysProxy: {st_p} | HTTP:{hp} SOCKS:{sp}")
    print("-" * w)

    # Меню
    print(" 1. Запустить только naive (без прокси)")
    print(" 2. Остановить только naive")
    print(" 3. Запустить naive + включить системный прокси")
    print(" 4. Остановить naive + выключить системный прокси")
    print(" 5. Перезагрузить config.json")
    print(" 6. Проверить внешний IP через прокси (curl)")
    print(" 7. Просмотреть логи")
    print(" 8. Инфо / Справка")
    print(" 0. Выход")
    print("=" * w)


def show_info():
    os.system('cls' if os.name == 'nt' else 'clear')
    w = min(shutil.get_terminal_size()[0], 70)
    print(f"{Colors.BOLD}{Colors.CYAN}📋 NaiveProxy Manager v3.9.5 - Справка{Colors.END}")
    print("=" * w)
    print("""
🔹 ОПИСАНИЕ:
   Консольный менеджер для управления NaiveProxy с автоматической
   настройкой системного прокси в Windows.

🔹 ТРЕБОВАНИЯ:
   • Запуск от имени Администратора (для изменения прокси)
   • Python 3.7+
   • naive.exe и config.json в одной папке со скриптом

🔹 КОМАНДЫ МЕНЮ:
   [1] Запустить только naive (без прокси)
   [2] Остановить только naive
   [3] Запустить naive + включить системный прокси
   [4] Остановить naive + выключить системный прокси
   [5] Перезагрузить config.json
   [6] Проверить внешний IP через прокси
   [7] Просмотреть логи (полноэкранный режим)
   [8] Эта справка
   [0] Выход с корректной остановкой

🔹 КЛАВИШИ В ПРОСМОТРЕ ЛОГОВ:
   [R] Обновить • [C] Очистить • [S] Сохранить в файл • [B] Назад

🔹 ЛОГИРОВАНИЕ:
   • Ошибки и предупреждения — ВСЕГДА видны в главном окне (цветом)
   • Статусы (ОК, SYSTEM) — видны в главном окне
   • Детальные логи naive — только в просмотре [7]

🔹 ЗАВЕРШЕНИЕ:
   • При нажатии на крестик окна → авто-остановка
   • При Ctrl+C → авто-остановка
   • При выборе [0] → авто-остановка
    """)
    print("=" * w)
    input(f"{Colors.YELLOW}Нажмите Enter для возврата в меню...{Colors.END}")


def main():
    # === ПРЕДУПРЕЖДЕНИЕ ОБ АДМИНЕ В ГЛАВНОМ ОКНЕ ===
    if not is_admin():
        print(
            f"{Colors.YELLOW}Предупреждение: Для работы системного прокси требуются права Администратора{Colors.END}")
        print(f"{Colors.YELLOW}  Будет запрошено повышение прав...{Colors.END}\n")
        time.sleep(1.5)
        run_as_admin()

    print(f"{Colors.CYAN}=== NaiveProxy Manager v3.9.5 запущен ==={Colors.END}\n")

    config = None

    def load_cfg():
        nonlocal config
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    log.notify("Конфиг загружен", "SYSTEM")
            else:
                log.notify("config.json не найден!", "ERROR")
        except Exception as e:
            log.notify(f"Ошибка в конфиге: {e}", "ERROR")

    load_cfg()

    try:
        while True:
            running = (naive_proc and naive_proc.poll() is None)
            if not running:
                s, _ = run_cmd('tasklist /fi "IMAGENAME eq naive.exe" | findstr naive.exe')
                running = bool(s)

            proxy_on = is_proxy_enabled()
            show_menu(config, running, proxy_on)

            ch = input(f"{Colors.YELLOW}Action [0-8]: {Colors.END}").strip()

            if ch == "1":
                start_naive()
                time.sleep(0.6)
            elif ch == "2":
                stop_naive()
                time.sleep(0.6)
            elif ch == "3":
                if start_naive():
                    set_system_proxy(True, config)
                time.sleep(0.6)
            elif ch == "4":
                stop_naive()
                set_system_proxy(False)
                time.sleep(0.6)
            elif ch == "5":
                load_cfg()
                time.sleep(0.6)
            elif ch == "6":
                hp, _ = parse_listen_ports(config)
                s, o = run_cmd(f"curl -s --proxy 127.0.0.1:{hp} https://api.ipify.org")
                result = o.strip() if s and o.strip() else "Failed (curl missing or proxy down)"
                print(f"\n{Colors.CYAN}External IP: {result}{Colors.END}")
                log.add(f"IP check: {result}", "INFO")
                time.sleep(2)
            elif ch == "7":
                view_logs()
            elif ch == "8":
                show_info()
            elif ch == "0":
                cleanup(show_notification=True)
                break
            else:
                log.add(f"Неизвестная команда: {ch}", "WARN")
                time.sleep(1)

    except KeyboardInterrupt:
        cleanup(show_notification=True)
    except Exception as e:
        log.notify(f"Сбой: {e}", "ERROR")
        cleanup(show_notification=False)
        raise
    finally:
        if not _shutdown_in_progress:
            cleanup(show_notification=False)


if __name__ == "__main__":
    main()