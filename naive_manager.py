import sys
import os
import json
import subprocess
import threading
import ctypes
from pathlib import Path
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QPlainTextEdit)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QTextCursor

# Полное скрытие консольных окон
CREATE_NO_WINDOW = 0x08000000


def get_app_dir():
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent


APP_DIR = get_app_dir()
CONFIG_PATH = APP_DIR / "config.json"
NAIVE_PATH = APP_DIR / "naive.exe"
REG_SETTINGS = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except:
        return False


def run_as_admin():
    if not is_admin():
        script = os.path.abspath(sys.argv[0])
        params = ' '.join([f'"{arg}"' for arg in sys.argv[1:]])
        try:
            # shell32.ShellExecuteW возвращает значение > 32 при успехе
            ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}" {params}', None, 1)
            if int(ret) > 32:
                # Если запуск второй копии инициирован успешно, немедленно убиваем текущую
                os._exit(0)
        except:
            pass  # Если пользователь отказал в UAC, управление вернется и приложение запустится без прав


class LoggerSignals(QObject):
    log_signal = pyqtSignal(str, str)


class NaiveGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.naive_proc = None
        self.signals = LoggerSignals()
        self.signals.log_signal.connect(self.log_to_ui)
        self.config = self.load_cfg()

        self.init_ui()

        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.refresh_ui_state)
        self.status_timer.start(1000)
        self.log("Info", "Navie Proxy Manager 3.9.6")
        self.log("Info", "A censorship protection utility based on Navie. Update the navie.exe version as needed.")
        self.log("Info","Actual repository: https://github.com/klzgrad/naiveproxy")
        # Начальная проверка прав
        if is_admin():
            self.log("System", "Running with Administrative privileges.")
        else:
            self.log("ERROR", "NO ADMIN PRIVILEGES! System proxy settings may not work.")

    def init_ui(self):
        self.setWindowTitle("NPM 3.9.6")
        self.setMinimumSize(1000, 550)
        self.setStyleSheet("""
            QMainWindow { background-color: #111; }
            QLabel { color: #CCC; font-family: 'Consolas'; font-size: 12px; }
            QPushButton { 
                background-color: #333; color: #eee; border: 1px solid #444;
                padding: 10px; font-family: 'Consolas'; font-size: 12px; font-weight: bold;
            }
            QPushButton:hover { background-color: #333; border: 1px solid #444; }
            QPushButton#active { color: #CCC; border: 1px solid #222; }
            QPushButton#run_all { background-color: #333; color: #CCC; border: 1px solid #444; }
            QPushButton#stop_all { background-color: #333; color: #CCC; border: 1px solid #444; }
            QPlainTextEdit { 
                background-color: #000; color: #EDEBED; border: 1px solid #222;
                font-family: 'Consolas'; font-size: 12px;
            }
        """)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Инфо-панель
        h_info = QHBoxLayout()
        self.lbl_admin = QLabel("ADMIN: OK" if is_admin() else "ADMIN: FAIL")
        self.lbl_admin.setStyleSheet("color: #5f5" if is_admin() else "color: #f55")
        self.lbl_n = QLabel("Naive: Stopped")
        self.lbl_p = QLabel("Proxy: Off")
        h_info.addWidget(self.lbl_admin)
        h_info.addSpacing(20)
        h_info.addWidget(self.lbl_n)
        h_info.addWidget(self.lbl_p)
        h_info.addStretch()
        layout.addLayout(h_info)

        # Строка управления (все кнопки в ряд)
        h_ctrl = QHBoxLayout()

        self.btn_master = QPushButton("⚡ Start Naive + SysProxy")
        self.btn_master.setMinimumWidth(150)
        self.btn_master.clicked.connect(self.toggle_master)
        h_ctrl.addWidget(self.btn_master)

        self.btn_n = QPushButton("Start Naive engine only")
        self.btn_n.clicked.connect(self.toggle_naive)
        h_ctrl.addWidget(self.btn_n)

        self.btn_p = QPushButton("Start SysProxy only")
        self.btn_p.clicked.connect(self.toggle_proxy)
        h_ctrl.addWidget(self.btn_p)

        self.btn_check = QPushButton("Check IP")
        self.btn_check.clicked.connect(self.on_check_ip)
        h_ctrl.addWidget(self.btn_check)

        self.btn_reload = QPushButton("Config reload")
        self.btn_reload.clicked.connect(self.on_reload_cfg)
        h_ctrl.addWidget(self.btn_reload)

        layout.addLayout(h_ctrl)

        # Лог
        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        # Ограничение лога: хранить только последние 1000 строк
        self.log_area.setMaximumBlockCount(1000)
        layout.addWidget(self.log_area)

    def log(self, level, msg):
        self.signals.log_signal.emit(level, msg)

    def log_to_ui(self, level, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        color = {"ERROR": "#f55", "OK": "#5f5", "System": "#5ff", "NAIVE": "#777"}.get(level, "#eee")
        self.log_area.appendHtml(f'<code style="color:#555">[{ts}]</code> <b style="color:{color}">[{level}]</b> {msg}')
        self.log_area.moveCursor(QTextCursor.MoveOperation.End)

    def load_cfg(self):
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f: return json.load(f)
        except:
            pass
        return None

    def is_naive_running(self):
        if self.naive_proc and self.naive_proc.poll() is None: return True
        r = subprocess.run('tasklist /fi "IMAGENAME eq naive.exe"', capture_output=True, text=True, shell=True,
                           creationflags=CREATE_NO_WINDOW)
        return "naive.exe" in r.stdout

    def is_proxy_on(self):
        r = subprocess.run(f'reg query "{REG_SETTINGS}" /v ProxyEnable', capture_output=True, text=True, shell=True,
                           creationflags=CREATE_NO_WINDOW)
        return "0x1" in r.stdout

    def refresh_ui_state(self):
        n_on = self.is_naive_running()
        p_on = self.is_proxy_on()

        # Обновление текста статусов
        self.lbl_n.setText(f"Naive: {'🟢 ACTIVE' if n_on else '🔴 STOPPED'}    ")
        self.lbl_p.setText(f"Proxy: {'🟢 ON' if p_on else '🔴 OFF'}")

        # Обновление кнопок компонентов
        self.btn_n.setText("Stop Naive" if n_on else "Start Navive")
        self.btn_n.setObjectName("active" if n_on else "")

        self.btn_p.setText("Stop SysProxy" if p_on else "Start SysProxy")
        self.btn_p.setObjectName("active" if p_on else "")

        # Обновление Master кнопки (Stop All / Start All)
        if n_on or p_on:
            self.btn_master.setText("🔴 Stop Navie + SysProxy")
            self.btn_master.setObjectName("stop_all")
        else:
            self.btn_master.setText("⚡ Start Navie + SysProxy")
            self.btn_master.setObjectName("run_all")

        # Принудительная перерисовка стилей
        for b in [self.btn_n, self.btn_p, self.btn_master]:
            b.setStyle(b.style())

    def toggle_naive(self):
        if self.is_naive_running():
            self.stop_naive_logic()
        else:
            self.start_naive_logic()

    def start_naive_logic(self):
        if not NAIVE_PATH.exists(): return self.log("ERROR", "naive.exe not found!")
        self.naive_proc = subprocess.Popen(
            [str(NAIVE_PATH), "--config", str(CONFIG_PATH)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW
        )
        threading.Thread(target=self.stream_logs, daemon=True).start()
        self.log("OK", "Naive engine started.")

    def stop_naive_logic(self):
        if self.naive_proc: self.naive_proc.terminate()
        subprocess.run("taskkill /f /im naive.exe /t", shell=True, creationflags=CREATE_NO_WINDOW)
        self.log("System", "Naive engine stopped.")

    def stream_logs(self):
        while self.naive_proc and self.naive_proc.poll() is None:
            line = self.naive_proc.stdout.readline()
            if line: self.log("NAIVE", line.strip())

    def toggle_proxy(self):
        new_state = 0 if self.is_proxy_on() else 1
        self.set_proxy_logic(new_state)

    def set_proxy_logic(self, state):
        port = "8080"
        if self.config:
            for l in self.config.get("listen", []):
                if "http" in l: port = l.split(":")[-1]

        cmd = f'reg add "{REG_SETTINGS}" /v ProxyEnable /t REG_DWORD /d {state} /f'
        if state == 1:
            cmd += f' & reg add "{REG_SETTINGS}" /v ProxyServer /t REG_SZ /d "127.0.0.1:{port}" /f'

        subprocess.run(cmd, shell=True, creationflags=CREATE_NO_WINDOW)
        self.log("System", f"System Proxy: {'ON' if state else 'OFF'}")

    def toggle_master(self):
        if self.is_naive_running() or self.is_proxy_on():
            self.set_proxy_logic(0)
            self.stop_naive_logic()
            self.log("System", "Master Stop: All services halted.")
        else:
            self.start_naive_logic()
            self.set_proxy_logic(1)
            self.log("OK", "Master Run: Everything is up.")

    def on_check_ip(self):
        def _check():
            self.log("System", "Checking IP via 127.0.0.1:8080...")
            r = subprocess.run("curl -s --proxy 127.0.0.1:8080 https://api.ipify.org", capture_output=True, text=True,
                               shell=True, creationflags=CREATE_NO_WINDOW)
            ip = r.stdout.strip()
            self.log("OK", f"External IP: {ip}" if ip else "Check failed. Is proxy on?")

        threading.Thread(target=_check, daemon=True).start()

    def on_reload_cfg(self):
        self.config = self.load_cfg()
        self.log("System", "Config reloaded.")

    def closeEvent(self, event):
        self.set_proxy_logic(0)
        self.stop_naive_logic()
        event.accept()


if __name__ == "__main__":
    run_as_admin()  # Попытка повышения прав

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    gui = NaiveGUI()
    gui.show()
    sys.exit(app.exec())