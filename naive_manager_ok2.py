#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NaiveProxy Manager v3.0 - с просмотром логов
Сборка: pip install pystray pillow pyinstaller
        pyinstaller --onefile --console --name "NaiveProxyManager" naive_manager.py
"""
import os
import sys
import subprocess
import time
import ctypes
import signal
import threading
import json
import traceback
from pathlib import Path
from datetime import datetime
from collections import deque


def get_app_dir():
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent


APP_DIR = get_app_dir()
CONFIG_PATH = APP_DIR / "config.json"
NAIVE_PATH = APP_DIR / "naive.exe"
LOG_PATH = APP_DIR / "naive.log"
REG_SETTINGS = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"


# === Логгер ===
class Logger:
    def __init__(self, max_lines=500):
        self.logs = deque(maxlen=max_lines)
        self.lock = threading.Lock()

    def add(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] [{level}] {msg}"
        with self.lock:
            self.logs.append(entry)
        print(entry)  # дублируем в консоль

    def get_logs(self, lines: int = 50) -> str:
        with self.lock:
            return "\n".join(list(self.logs)[-lines:])

    def clear(self):
        with self.lock:
            self.logs.clear()
        self.add("Log cleared", "SYSTEM")


log = Logger()

# === Трей ===
try:
    import pystray
    from PIL import Image, ImageDraw

    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    log.add("pystray/Pillow not installed, tray disabled", "WARN")


# === Безопасный ввод ===
def safe_input(prompt=""):
    try:
        if sys.stdin is None or sys.stdin.closed:
            return None
        return input(prompt)
    except (EOFError, RuntimeError, OSError):
        return None


# === Утилиты ===
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def run_cmd(cmd, show_output=False):
    try:
        flags = subprocess.CREATE_NO_WINDOW if not show_output else 0
        res = subprocess.run(cmd, shell=True, capture_output=not show_output, text=True, creationflags=flags)
        return res.returncode == 0, res.stdout + res.stderr
    except Exception as e:
        return False, str(e)


def parse_listen_ports(config):
    hp, sp = "8080", "1080"
    for addr in config.get("listen", []):
        if "http://" in addr:
            hp = addr.split(":")[-1]
        elif "socks://" in addr:
            sp = addr.split(":")[-1]
    return hp, sp


def refresh_proxy_settings():
    """Мгновенное применение настроек прокси"""
    ps_script = '''
    $sig = @'
    [DllImport("wininet.dll", SetLastError=true)]
    public static extern bool InternetSetOption(IntPtr hInternet, int dwOption, IntPtr lpBuffer, int dwBufferLength);
'@
    try:
        $wininet = Add-Type -MemberDefinition $sig -Name "Wininet" -Namespace "Win32" -PassThru -ErrorAction Stop
        $wininet::InternetSetOption(0, 39, 0, 0)
        $wininet::InternetSetOption(0, 37, 0, 0)
    } catch { pass }
    '''
    try:
        subprocess.run(['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
                       capture_output=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
    except:
        pass


def set_system_proxy(enable: bool, config=None):
    hp, sp = parse_listen_ports(config) if config else ("8080", "1080")
    if enable:
        ps = f"http=http://127.0.0.1:{hp};https=http://127.0.0.1:{hp};socks=socks://127.0.0.1:{sp}"
        run_cmd(f'reg add "{REG_SETTINGS}" /v ProxyEnable /t REG_DWORD /d 1 /f')
        run_cmd(f'reg add "{REG_SETTINGS}" /v ProxyServer /t REG_SZ /d "{ps}" /f')
        run_cmd(f'reg add "{REG_SETTINGS}" /v ProxyOverride /t REG_SZ /d "<local>" /f')
        log.add(f"System proxy ON (HTTP:{hp} | SOCKS:{sp})", "OK")
    else:
        run_cmd(f'reg add "{REG_SETTINGS}" /v ProxyEnable /t REG_DWORD /d 0 /f')
        log.add("System proxy OFF", "OK")
    refresh_proxy_settings()


def is_proxy_enabled():
    s, o = run_cmd(f'reg query "{REG_SETTINGS}" /v ProxyEnable')
    return s and "0x1" in o


# === Управление naive ===
naive_proc = None
naive_output_thread = None


def start_naive():
    global naive_proc, naive_output_thread

    log.add(f"APP_DIR: {APP_DIR}", "DEBUG")
    log.add(f"CONFIG_PATH exists: {CONFIG_PATH.exists()}", "DEBUG")
    log.add(f"NAIVE_PATH exists: {NAIVE_PATH.exists()}", "DEBUG")

    if not NAIVE_PATH.exists():
        log.add(f"naive.exe not found: {NAIVE_PATH}", "ERROR")
        return False

    if naive_proc and naive_proc.poll() is None:
        log.add("naiveproxy already running", "WARN")
        return True

    # Читаем конфиг для проверки
    cfg = load_config()
    if cfg:
        proxy = cfg.get("proxy", "")
        log.add(f"Proxy URL: {proxy[:50]}...", "DEBUG")

    cmd = [str(NAIVE_PATH), "--config", str(CONFIG_PATH)]
    log.add(f"Starting: {' '.join(cmd)}", "INFO")

    try:
        naive_proc = subprocess.Popen(
            cmd,
            cwd=APP_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        # Читаем вывод в фоне
        def read_output():
            while naive_proc and naive_proc.poll() is None:
                line = naive_proc.stdout.readline()
                if line:
                    log.add(line.strip(), "NAIVE")
                time.sleep(0.05)
            # Дочитываем остаток
            for line in naive_proc.stdout:
                if line:
                    log.add(line.strip(), "NAIVE")

        naive_output_thread = threading.Thread(target=read_output, daemon=True)
        naive_output_thread.start()

        time.sleep(2)
        if naive_proc.poll() is None:
            log.add("naiveproxy started successfully", "OK")
            return True
        else:
            log.add(f"naiveproxy exited with code {naive_proc.poll()}", "ERROR")
            return False
    except Exception as e:
        log.add(f"Start error: {e}", "ERROR")
        traceback.print_exc()
        return False


def stop_naive():
    global naive_proc
    if naive_proc and naive_proc.poll() is None:
        naive_proc.terminate()
        time.sleep(1)
        if naive_proc.poll() is None:
            naive_proc.kill()
        log.add("naiveproxy stopped", "OK")
        naive_proc = None
    run_cmd("taskkill /f /im naive.exe /t", show_output=False)


# === Трей ===
tray_icon = None


def create_tray_icon():
    img = Image.new('RGB', (64, 64), color=(30, 144, 255))
    ImageDraw.Draw(img).text((20, 15), "N", fill=(255, 255, 255), font_size=32)
    return img


def on_tray_quit():
    stop_naive()
    set_system_proxy(False)
    if tray_icon:
        tray_icon.stop()


def on_tray_start():
    c = load_config()
    if c and start_naive():
        set_system_proxy(True, c)


def on_tray_stop():
    stop_naive()
    set_system_proxy(False)


def run_tray():
    global tray_icon
    if not HAS_TRAY:
        return
    tray_icon = pystray.Icon("NaiveProxy", create_tray_icon(), "NaiveProxy", pystray.Menu(
        pystray.MenuItem("Start All", on_tray_start),
        pystray.MenuItem("Stop All", on_tray_stop),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_tray_quit)
    ))
    tray_icon.run()


# === Конфиг ===
def load_config():
    if not CONFIG_PATH.exists():
        log.add(f"config.json missing: {CONFIG_PATH}", "ERROR")
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.add(f"Invalid config.json: {e}", "ERROR")
        return None


# === Просмотр логов ===
def view_logs():
    """Показать последние логи с постраничной навигацией"""
    while True:
        print("\n" + "=" * 60)
        print("📋 LOG VIEWER (last 50 lines)")
        print("=" * 60)
        logs = log.get_logs(50)
        if logs:
            print(logs)
        else:
            print("(no logs yet)")
        print("-" * 60)
        print("[R] Refresh  [C] Clear  [B] Back  [S] Save to file")
        print("=" * 60)

        ch = safe_input("Action: ").strip().lower()
        if ch in ("r", ""):
            continue  # обновить
        elif ch == "c":
            log.clear()
        elif ch == "b":
            break
        elif ch == "s":
            save_logs()
        else:
            print("Invalid")
            time.sleep(0.5)


def save_logs():
    """Сохранить логи в файл"""
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write(log.get_logs(1000))
        print(f"[OK] Logs saved to {LOG_PATH}")
    except Exception as e:
        print(f"[ERROR] Save failed: {e}")
    safe_input("Press Enter...")


# === Меню ===
def show_menu(cfg, running, proxy_on):
    print("\n" + "=" * 50)
    print("NaiveProxy Manager v3.0")
    print("=" * 50)
    if cfg:
        proxy_val = cfg.get("proxy", "")
        srv = proxy_val.split("@")[-1].split("?")[0].split("#")[0] if "@" in proxy_val else "N/A"
        name = proxy_val.split("#")[-1] if "#" in proxy_val else ""
        hp, sp = parse_listen_ports(cfg)
        print(f"Server: {srv}")
        if name:
            print(f"Name: {name}")
        print(f"Ports: HTTP:{hp} | SOCKS:{sp}")
    print(f"naive: {'RUN' if running else 'STOP'} | SysProxy: {'ON' if proxy_on else 'OFF'}")
    print("-" * 50)
    print("1. Start naive only")
    print("2. Stop naive only")
    print("3. Start naive + System Proxy ON")
    print("4. Stop naive + System Proxy OFF")
    print("5. Reload config")
    print("6. Check IP (curl)")
    print("7. View Logs 📋")
    print("8. Switch to Tray mode")
    print("0. Exit")
    print("=" * 50)


def check_ip(cfg):
    if not cfg:
        return
    hp, _ = parse_listen_ports(cfg)
    print("\nChecking...")
    s, o = run_cmd(f"curl -s --proxy 127.0.0.1:{hp} https://api.ipify.org")
    result = o.strip() if s and o.strip() else "Failed (curl missing or proxy down)"
    print(f"External IP: {result}")
    log.add(f"IP check: {result}", "INFO")


# === Main ===
def main():
    log.add("=== NaiveProxy Manager started ===", "SYSTEM")

    if not is_admin():
        log.add("Run as Administrator for system proxy!", "WARN")
        time.sleep(2)

    cfg = load_config()
    if not cfg:
        log.add("Failed to load config, exiting", "ERROR")
        sys.exit(1)

    def cleanup(sig, frame):
        log.add("Cleanup on exit...", "SYSTEM")
        stop_naive()
        set_system_proxy(False)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)

    if sys.stdin is None or sys.stdin.closed:
        log.add("Console unavailable, starting in Tray mode", "INFO")
        run_tray()
        return

    if "--tray" in sys.argv:
        run_tray()
        return

    while True:
        try:
            running = naive_proc and naive_proc.poll() is None
            if not running:
                s, _ = run_cmd('tasklist /fi "IMAGENAME eq naive.exe" | findstr naive.exe')
                running = s
            proxy_on = is_proxy_enabled()
            show_menu(cfg, running, proxy_on)

            ch = safe_input("Action [0-8]: ")
            if ch is None:
                log.add("Input closed, switching to tray", "INFO")
                threading.Thread(target=run_tray, daemon=True).start()
                while True: time.sleep(0.5)
                break

            if ch == "1":
                start_naive()
            elif ch == "2":
                stop_naive()
            elif ch == "3":
                if start_naive():
                    set_system_proxy(True, cfg)
            elif ch == "4":
                stop_naive()
                set_system_proxy(False)
            elif ch == "5":
                cfg = load_config() or cfg
                log.add("Config reloaded", "OK")
            elif ch == "6":
                check_ip(cfg)
            elif ch == "7":
                view_logs()
            elif ch == "8":
                log.add("Switching to tray mode", "INFO")
                threading.Thread(target=run_tray, daemon=True).start()
                while True: time.sleep(0.5)
                break
            elif ch == "0":
                log.add("Goodbye!", "SYSTEM")
                cleanup(None, None)
            else:
                log.add("Invalid choice", "WARN")

            safe_input("\nPress Enter...")
        except Exception as e:
            log.add(f"CRASH: {e}", "ERROR")
            traceback.print_exc()
            safe_input("Press Enter to exit...")
            break


if __name__ == "__main__":
    main()