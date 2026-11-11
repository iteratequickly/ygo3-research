"""CustomTkinter launcher and BP manager for the YGO3 private server."""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError as exc:
    raise SystemExit(
        "CustomTkinter is required. Install it with: python -m pip install customtkinter"
    ) from exc

APP_DIR = Path(sys.argv[0]).resolve().parent
CONFIG_FILE = APP_DIR / "ygo3_launcher_config.json"
DEFAULT_SERVER = APP_DIR / "ygo3_server.py"
MAX_BP = 0xFFFFFFFF

DEFAULTS = {
    "server_script": str(DEFAULT_SERVER),
    "python_executable": "",
    "game_dir": "",
    "ocg_dll": "",
    "card_database": "",
    "script_dir": "",
    "data_dir": str(APP_DIR / "data"),
    "web_dir": str(APP_DIR / "www"),
    "host": "",
    "front_address": "",
    "gate_port": "",
    "front_port": "",
    "duel_port": "",
    "http_port": "",
    "user_id": "",
}


def load_config():
    data = DEFAULTS.copy()
    try:
        if CONFIG_FILE.exists():
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update({k: str(v) for k, v in loaded.items() if k in data})
    except (OSError, json.JSONDecodeError) as exc:
        messagebox.showwarning(
            "Config", f"Could not read config; defaults loaded.\n{exc}"
        )
    return data


class Launcher(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("YGO3 — Server Control")
        self.geometry("900x900")
        self.minsize(800, 780)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.config_values = load_config()
        self.entries = {}
        self.process = None
        self.log_queue = queue.Queue()
        self._closing = False

        self._build()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain_log_queue)

    # ============================================================
    # GUI
    # ============================================================

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=20, pady=(18, 8), sticky="ew")

        ctk.CTkLabel(
            header, text="YGO3 Server", font=ctk.CTkFont(size=24, weight="bold")
        ).pack(side="left")

        self.status = ctk.CTkLabel(header, text="● Stopped", text_color="#d45b5b")
        self.status.pack(side="right", padx=8)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=1, column=0, padx=20, pady=8, sticky="nsew")

        self.tabs.add("Server Settings")
        self.tabs.add("BP Manager")
        self.tabs.add("Server Logs")

        settings_tab = self.tabs.tab("Server Settings")
        bp_tab = self.tabs.tab("BP Manager")
        logs_tab = self.tabs.tab("Server Logs")

        # -------------------- Server Settings --------------------

        paths = ctk.CTkFrame(settings_tab)
        paths.pack(fill="x", padx=10, pady=10)
        paths.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            paths,
            text="Directories, files and Python",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, columnspan=4, padx=12, pady=(10, 6), sticky="w")

        ctk.CTkLabel(
            paths, text="32-bit Python executable", width=190, anchor="w"
        ).grid(row=1, column=0, padx=(12, 6), pady=5, sticky="w")

        py_ent = ctk.CTkEntry(paths, placeholder_text="Select 32-bit python.exe")
        py_ent.insert(0, self.config_values.get("python_executable", ""))
        py_ent.grid(row=1, column=1, padx=6, pady=5, sticky="ew")
        self.entries["python_executable"] = py_ent

        ctk.CTkButton(
            paths, text="Browse…", width=80, command=self._browse_python
        ).grid(row=1, column=2, padx=4, pady=5)

        py_actions = ctk.CTkFrame(paths, fg_color="transparent")
        py_actions.grid(row=1, column=3, padx=(2, 12), pady=5, sticky="e")

        ctk.CTkButton(
            py_actions, text="Find 32-bit", width=100, command=self.find_32bit_python
        ).pack(side="left", padx=3)

        ctk.CTkButton(
            py_actions, text="Check", width=65, command=self.check_python
        ).pack(side="left", padx=3)

        fields = [
            ("server_script", "Server file (ygo3_server.py)", "file"),
            ("game_dir", "YGO3 game directory", "dir"),
            ("ocg_dll", "Ocgcore DLL", "file"),
            ("card_database", "Cards database (cards.cdb)", "file"),
            ("script_dir", "Script directory", "dir"),
            ("data_dir", "Data save directory", "dir"),
            ("web_dir", "Web files directory", "dir"),
        ]

        for row, (key, label, kind) in enumerate(fields, start=2):
            ctk.CTkLabel(paths, text=label, width=190, anchor="w").grid(
                row=row, column=0, padx=(12, 6), pady=5, sticky="w"
            )

            ent = ctk.CTkEntry(paths)
            ent.insert(0, self.config_values.get(key, ""))
            ent.grid(row=row, column=1, columnspan=2, padx=6, pady=5, sticky="ew")
            self.entries[key] = ent

            ctk.CTkButton(
                paths,
                text="Browse…",
                width=90,
                command=lambda k=key, t=kind: self._browse(k, t),
            ).grid(row=row, column=3, padx=(6, 12), pady=5)

        settings = ctk.CTkFrame(settings_tab)
        settings.pack(fill="x", padx=10, pady=8)
        settings.grid_columnconfigure(1, weight=1)
        settings.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            settings, text="Network & Ports", font=ctk.CTkFont(size=16, weight="bold")
        ).grid(row=0, column=0, columnspan=4, padx=12, pady=(10, 4), sticky="w")

        net_fields = [
            ("host", "Bind address", 150, 1),
            ("front_address", "Front address", 150, 3),
            ("gate_port", "Gate Port", 100, 1),
            ("front_port", "Front Port", 100, 3),
            ("duel_port", "Duel Port", 100, 1),
            ("http_port", "HTTP Port", 100, 3),
        ]

        for idx, (key, label, width, col_offset) in enumerate(net_fields):
            row_idx = (idx // 2) + 1
            lbl_col = 0 if col_offset == 1 else 2
            ent_col = 1 if col_offset == 1 else 3

            ctk.CTkLabel(settings, text=label, width=110, anchor="w").grid(
                row=row_idx, column=lbl_col, padx=(12, 5), pady=5, sticky="w"
            )

            ent = ctk.CTkEntry(settings, width=width)
            ent.insert(0, self.config_values.get(key, ""))
            ent.grid(row=row_idx, column=ent_col, padx=5, pady=5, sticky="w")
            self.entries[key] = ent

        controls = ctk.CTkFrame(settings_tab, fg_color="transparent")
        controls.pack(fill="x", padx=10, pady=10)

        self.start_btn = ctk.CTkButton(
            controls, text="Start server", command=self.start_server
        )
        self.start_btn.pack(side="left", padx=4)

        self.stop_btn = ctk.CTkButton(
            controls,
            text="Stop",
            fg_color="#a33d3d",
            hover_color="#842e2e",
            command=self.stop_server,
            state="disabled",
        )
        self.stop_btn.pack(side="left", padx=4)

        # -------------------- BP Manager --------------------

        bp = ctk.CTkFrame(bp_tab)
        bp.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(
            bp, text="Add BP to shop balance", font=ctk.CTkFont(size=16, weight="bold")
        ).grid(row=0, column=0, columnspan=5, padx=10, pady=(10, 4), sticky="w")

        ctk.CTkLabel(bp, text="User ID").grid(row=1, column=0, padx=10, pady=8)

        self.user_entry = ctk.CTkEntry(bp, width=100)
        self.user_entry.insert(0, self.config_values.get("user_id", "1001"))
        self.user_entry.grid(row=1, column=1, padx=5, pady=8)

        ctk.CTkLabel(bp, text="BP to add").grid(row=1, column=2, padx=10, pady=8)

        self.bp_entry = ctk.CTkEntry(bp, width=140, placeholder_text="e.g. 500")
        self.bp_entry.grid(row=1, column=3, padx=5, pady=8)

        ctk.CTkButton(bp, text="Add BP", command=self.add_bp).grid(
            row=1, column=4, padx=10, pady=8
        )

        ctk.CTkLabel(
            bp,
            text=(
                f"Hard safety ceiling: {MAX_BP:,} BP "
                "(unsigned 32-bit protocol limit). Excess grants are clipped "
                "to the remaining room."
            ),
            text_color="gray",
            wraplength=680,
            justify="left",
        ).grid(row=2, column=0, columnspan=5, padx=10, pady=(0, 10), sticky="w")

        # -------------------- Server Logs --------------------

        logs_tab.grid_columnconfigure(0, weight=1)
        logs_tab.grid_rowconfigure(1, weight=1)

        log_header = ctk.CTkFrame(logs_tab, fg_color="transparent")
        log_header.grid(row=0, column=0, padx=10, pady=(10, 4), sticky="ew")

        ctk.CTkLabel(
            log_header, text="Server output", font=ctk.CTkFont(size=15, weight="bold")
        ).pack(side="left")

        ctk.CTkButton(
            log_header, text="Clear logs", width=90, command=self.clear_logs
        ).pack(side="right")

        self.log = ctk.CTkTextbox(logs_tab, wrap="word")
        self.log.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

        # -------------------- Footer --------------------

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, padx=20, pady=(2, 14), sticky="ew")

        ctk.CTkButton(
            footer, text="Save settings", command=self.save_config, width=130
        ).pack(side="left")

        ctk.CTkLabel(
            footer, text=f"Settings file: {CONFIG_FILE.name}", text_color="gray"
        ).pack(side="right")

    # ============================================================
    # FILE BROWSING
    # ============================================================

    def _browse_python(self):
        chosen = filedialog.askopenfilename(
            title="Select a 32-bit Python interpreter",
            initialdir=str(APP_DIR),
            filetypes=[
                ("Python executable", "python.exe"),
                ("Executable", "*.exe"),
                ("All files", "*.*"),
            ],
        )

        if chosen:
            self.entries["python_executable"].delete(0, "end")
            self.entries["python_executable"].insert(0, chosen)
            self.check_python()

    def _browse(self, key, kind):
        current = self.entries[key].get().strip()

        if kind == "dir":
            chosen = filedialog.askdirectory(initialdir=current or str(APP_DIR))
        else:
            chosen = filedialog.askopenfilename(
                initialdir=(str(Path(current).parent) if current else str(APP_DIR))
            )

        if chosen:
            self.entries[key].delete(0, "end")
            self.entries[key].insert(0, chosen)

    # ============================================================
    # PYTHON CHECKING
    # ============================================================

    @staticmethod
    def _python_info(executable):
        """Return (is_32_bit, version_text, executable_path) or raise."""

        exe = str(Path(executable).expanduser())

        if not Path(exe).is_file():
            raise FileNotFoundError(f"Python executable not found: {exe}")

        code = (
            "import struct,sys;"
            "print(('32' if struct.calcsize('P')*8==32 else '64')+'|'+"
            "sys.version.split()[0]+'|'+sys.executable)"
        )

        result = subprocess.run(
            [exe, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        if result.returncode:
            raise RuntimeError(
                (result.stderr or result.stdout).strip() or "Could not run Python."
            )

        lines = result.stdout.strip().splitlines()

        if not lines:
            raise RuntimeError("The selected Python returned no version information.")

        bits, version, actual_exe = lines[-1].split("|", 2)

        return bits == "32", version, actual_exe

    def check_python(self, quiet=False):
        """Check Python architecture and required Blowfish dependency."""

        exe = self.entries["python_executable"].get().strip()

        if not exe:
            if not quiet:
                messagebox.showwarning(
                    "Python check", "Select or find a 32-bit Python executable first."
                )
            return False

        try:
            is_32, version, actual_exe = self._python_info(exe)

            if not is_32:
                raise RuntimeError(
                    f"Selected Python is 64-bit (Python {version}). "
                    "YGO3 server requires 32-bit Python."
                )

            result = subprocess.run(
                [
                    actual_exe,
                    "-c",
                    "from Crypto.Cipher import Blowfish; print('Crypto OK')",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            if result.returncode != 0:
                error = (result.stderr or result.stdout).strip()

                raise RuntimeError(
                    "Python is 32-bit, but PyCryptodome is missing "
                    "or cannot be imported.\n\n"
                    "Install it for this interpreter using:\n\n"
                    f'"{actual_exe}" -m pip install pycryptodome'
                    f"\n\nPython output:\n{error}"
                )

            self._log(f"Verified 32-bit Python {version}: {actual_exe}")
            self._log("Verified Crypto.Cipher.Blowfish")

            if not quiet:
                messagebox.showinfo(
                    "Python check",
                    f"Python verified successfully.\n\n"
                    f"Version: {version}\n"
                    f"Executable: {actual_exe}\n\n"
                    "Architecture: 32-bit\n"
                    "Blowfish module: Installed",
                )

            return True

        except Exception as exc:
            self._log(f"Python check failed: {exc}")

            if not quiet:
                messagebox.showerror("Python check", str(exc))

            return False

    # ============================================================
    # PYTHON DISCOVERY
    # ============================================================

    def _python_candidates(self):
        """Find installed 32-bit Python interpreters."""

        candidates = []
        seen = set()

        def add(path):
            if not path:
                return

            p = str(Path(os.path.expandvars(str(path))).expanduser())

            key = os.path.normcase(os.path.abspath(p))

            if key not in seen and Path(p).is_file():
                seen.add(key)
                candidates.append(p)

        try:
            if struct.calcsize("P") * 8 == 32:
                add(sys.executable)
        except Exception:
            pass

        for name in ("python.exe", "python3.exe"):
            add(shutil.which(name))

        launcher = shutil.which("py.exe") or shutil.which("py")

        if launcher:
            try:
                result = subprocess.run(
                    [launcher, "-0p"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )

                for line in (result.stdout + "\n" + result.stderr).splitlines():

                    match = re.search(
                        r"([A-Za-z]:\\.*?python(?:w)?\.exe)\s*$", line, re.I
                    )

                    if match and ("-32" in line or "32 bit" in line.lower()):
                        add(match.group(1))

            except (OSError, subprocess.SubprocessError):
                pass

        if os.name == "nt":
            try:
                import winreg

                roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)

                views = [0]

                if hasattr(winreg, "KEY_WOW64_32KEY"):
                    views.append(winreg.KEY_WOW64_32KEY)

                for root in roots:
                    for view in views:
                        base = r"Software\Python\PythonCore"

                        try:
                            with winreg.OpenKey(
                                root, base, 0, winreg.KEY_READ | view
                            ) as key:

                                i = 0

                                while True:
                                    try:
                                        version = winreg.EnumKey(key, i)
                                        i += 1
                                    except OSError:
                                        break

                                    try:
                                        with winreg.OpenKey(
                                            key,
                                            version + r"\InstallPath",
                                            0,
                                            winreg.KEY_READ | view,
                                        ) as ik:

                                            install, _ = winreg.QueryValueEx(ik, None)

                                            add(Path(install) / "python.exe")

                                    except OSError:
                                        continue

                        except OSError:
                            continue

            except ImportError:
                pass

        roots = []

        for env_name in ("LOCALAPPDATA", "ProgramFiles(x86)", "ProgramFiles"):
            root = os.environ.get(env_name)

            if root:
                roots.extend(
                    [Path(root) / "Programs" / "Python", Path(root) / "Python"]
                )

        roots.extend([Path("C:/Python"), Path("C:/")])

        for root in roots:
            if root.is_dir():
                try:
                    for pattern in (
                        "Python*-32/python.exe",
                        "Python3*/python.exe",
                        "Python*/python.exe",
                    ):
                        for path in root.glob(pattern):
                            add(path)

                except OSError:
                    continue

        verified = []

        for candidate in candidates:
            try:
                is_32, _, _ = self._python_info(candidate)

                if is_32:
                    verified.append(candidate)

            except Exception:
                continue

        return verified

    def find_32bit_python(self):
        """Prefer normal Windows Python over MSYS2 Python."""

        self._log("Searching for installed 32-bit Python interpreters…")

        candidates = self._python_candidates()

        preferred = []

        for candidate in candidates:
            normalised = candidate.replace("/", "\\").lower()

            if (
                "\\appdata\\local\\programs\\python\\" in normalised
                or "\\program files\\" in normalised
                or "\\program files (x86)\\" in normalised
                or "\\python314-32\\" in normalised
                or "\\python313-32\\" in normalised
                or "\\python312-32\\" in normalised
            ):
                preferred.append(candidate)

        ordered = preferred + [p for p in candidates if p not in preferred]

        if not ordered:
            messagebox.showwarning(
                "32-bit Python not found",
                "No working 32-bit Python installation was found.\n\n"
                "Install 32-bit Python for Windows, then click "
                "Find 32-bit again or browse to its python.exe.",
            )
            return

        selected = ordered[0]

        self.entries["python_executable"].delete(0, "end")
        self.entries["python_executable"].insert(0, selected)

        self.check_python()

        self._log(f"Selected preferred Python: {selected}")

        if len(ordered) > 1:
            self._log(
                "Other verified 32-bit interpreters found:\n  "
                + "\n  ".join(ordered[1:])
            )

    # ============================================================
    # CONFIGURATION AND ENVIRONMENT
    # ============================================================

    def _values(self):
        values = {k: e.get().strip() for k, e in self.entries.items()}

        values["user_id"] = self.user_entry.get().strip()

        return values

    def save_config(self, quiet=False):
        values = self._values()

        try:
            CONFIG_FILE.write_text(
                json.dumps(values, indent=2) + "\n", encoding="utf-8"
            )

            self.config_values = values

            if not quiet:
                self._log(f"Settings saved to {CONFIG_FILE}")

            return True

        except OSError as exc:
            messagebox.showerror("Save settings", str(exc))
            return False

    def _environment(self):
        v = self._values()
        env = os.environ.copy()

        for key, env_key in (
            ("game_dir", "YGO3_GAME_DIR"),
            ("ocg_dll", "YGO3_DLL_PATH"),
            ("card_database", "YGO3_CARD_DB"),
            ("script_dir", "YGO3_SCRIPT_DIR"),
            ("data_dir", "YGO3_DATA_DIR"),
            ("web_dir", "YGO3_WEB_DIR"),
            ("host", "YGO3_HOST"),
            ("front_address", "YGO3_FRONT_ADDRESS"),
            ("gate_port", "YGO3_GATE_PORT"),
            ("front_port", "YGO3_FRONT_PORT"),
            ("duel_port", "YGO3_DUEL_PORT"),
            ("port", "YGO3_HTTP_PORT"),
            ("http_port", "YGO3_HTTP_PORT"),
        ):
            if v.get(key):
                env[env_key] = v[key]

        return env

    def _server_path(self):
        path = Path(self.entries["server_script"].get()).expanduser()

        if not path.is_file():
            raise FileNotFoundError(f"Server script not found: {path}")

        return path

    def _selected_python(self):
        exe = self.entries["python_executable"].get().strip()

        if not exe:
            raise RuntimeError(
                "No Python interpreter selected. Click Find 32-bit "
                "or browse to a 32-bit python.exe."
            )

        if not self.check_python(quiet=True):
            raise RuntimeError(
                "The selected Python failed its checks. "
                "Open the Server Logs tab for details, then click Check."
            )

        is_32, version, actual_exe = self._python_info(exe)

        if not is_32:
            raise RuntimeError(
                f"The selected interpreter is 64-bit (Python {version}). "
                "YGO3 server operations require 32-bit Python."
            )

        return actual_exe

    # ============================================================
    # SERVER START / STOP / OUTPUT
    # ============================================================

    def start_server(self):
        if self.process and self.process.poll() is None:
            return

        if not self.save_config(quiet=True):
            return

        try:
            script = self._server_path()
            python_exe = self._selected_python()

            proc = subprocess.Popen(
                [python_exe, str(script)],
                cwd=str(script.parent),
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

        except Exception as exc:
            self.tabs.set("Server Logs")
            self._log(f"Could not start server: {exc}")
            messagebox.showerror("Start server", str(exc))
            return

        self.process = proc

        self.status.configure(text="● Running", text_color="#38a169")

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        self.tabs.set("Server Logs")

        self._log(f"Starting {script} with 32-bit Python: {python_exe}")

        threading.Thread(target=self._read_output, args=(proc,), daemon=True).start()

    def _read_output(self, proc):
        try:
            if proc.stdout:
                for line in proc.stdout:
                    self._log(line.rstrip())

        except (OSError, ValueError) as exc:
            self._log(f"Output reader error: {exc}")

        code = proc.wait()

        if not self._closing:
            try:
                self.after(0, self._server_exited, code, proc)
            except tk.TclError:
                pass

    def _server_exited(self, code, proc=None):
        if proc is not None and proc is not self.process:
            return

        self.status.configure(text=f"● Stopped (exit {code})", text_color="#d45b5b")

        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

        self._log(f"Server process exited with code {code}.")

    def stop_server(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self._log("Stop requested…")

    def _log(self, text):
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def clear_logs(self):
        self.log.delete("1.0", "end")

    def _close(self):
        self._closing = True
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                pass
        self.destroy()

    # ============================================================
    # BP MANAGEMENT
    # ============================================================

    def add_bp(self):
        try:
            amount = int(self.bp_entry.get().strip())

            user_id = int(self.user_entry.get().strip())

            if amount <= 0:
                raise ValueError("Enter a positive BP amount.")

            if user_id < 0:
                raise ValueError("User ID must be zero or greater.")

            script = self._server_path()
            python_exe = self._selected_python()

            if not self.save_config(quiet=True):
                return

            was_running = bool(self.process and self.process.poll() is None)

            if was_running:
                if not messagebox.askyesno(
                    "Restart required",
                    "The server must be stopped before editing its saved BP balance, "
                    "otherwise its in-memory state could overwrite the grant.\n\n"
                    "This will disconnect connected players. Stop it, grant BP, and restart?",
                ):
                    return

                proc = self.process
                proc.terminate()

                try:
                    proc.wait(timeout=15)

                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

                self._server_exited(proc.returncode, proc)

                self._log("Server stopped so the BP balance can be safely updated.")

            cmd = [python_exe, str(script), "--grant-bp", str(amount), str(user_id)]

            result = subprocess.run(
                cmd,
                cwd=str(script.parent),
                env=self._environment(),
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )

            output = (result.stdout + result.stderr).strip()

            self._log(output or f"BP grant exited with code {result.returncode}")

            if result.returncode == 0:
                messagebox.showinfo(
                    "Success",
                    f"Successfully granted {amount:,} BP to user ID {user_id}.",
                )
            else:
                messagebox.showerror(
                    "BP Grant Failed",
                    output or "Unknown error occurred during BP grant.",
                )

            if was_running:
                self.start_server()

        except Exception as exc:
            messagebox.showerror("Error", str(exc))
            self._log(f"BP grant error: {exc}")

    def _drain_log_queue(self):
        while not self.log_queue.empty():
            try:
                line = self.log_queue.get_nowait()
                self._log(line)
            except queue.Empty:
                break
        self.after(100, self._drain_log_queue)


if __name__ == "__main__":
    app = Launcher()
    app.mainloop()
