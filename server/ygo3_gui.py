"""GUI launcher, BP manager, Card IDs generator and LM Studio NPC AI manager for YGO3."""

from __future__ import annotations

import eel
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    requests = None
    HTTPAdapter = None
    Retry = None


# ============================================================
# PATHS AND CONSTANTS - PYINSTALLER COMPATIBLE
# ============================================================

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(sys._MEIPASS)
else:
    APP_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = APP_DIR

WEB_DIR = RESOURCE_DIR / "web"

CONFIG_FILE = APP_DIR / "ygo3_launcher_config.json"

DEFAULT_SERVER = APP_DIR / "ygo3_server.py"
if not DEFAULT_SERVER.exists():
    DEFAULT_SERVER = RESOURCE_DIR / "ygo3_server.py"

DEFAULT_CARD_IDS_FILE = APP_DIR / "card-ids.json"

MAX_BP = 0xFFFFFFFF

LLM_RESPONSE_BEGIN = "[LLM_RESPONSE_BEGIN]"
LLM_RESPONSE_END = "[LLM_RESPONSE_END]"
LLM_RESPONSE_SINGLE = "[LLM_RESPONSE]"

YGOPRO_API_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
REQUEST_TIMEOUT = (15, 120)
USER_AGENT = "CardJsonGenerator/2.0"

DEFAULTS = {
    "server_script": str(DEFAULT_SERVER),
    "python_executable": "",
    "game_dir": "",
    "ocg_dll": "",
    "card_database": "",
    "script_dir": "",
    "data_dir": str(APP_DIR / "data"),
    "web_dir": str(APP_DIR / "www"),
    "card_ids_output": str(DEFAULT_CARD_IDS_FILE),
    "host": "",
    "front_address": "",
    "gate_port": "",
    "front_port": "",
    "duel_port": "",
    "http_port": "",
    "user_id": "1001",
    "npc_ai_enabled": "true",
    "lm_studio_url": "http://localhost:1234/v1",
    "lm_studio_model": "",
    "npc_ai_timeout": "30",
    "gui_theme": "dark",
    "gui_font_family": "Inter",
    "gui_font_size": "14",
}

config = DEFAULTS.copy()

server_process: subprocess.Popen | None = None
server_lock = threading.RLock()
bp_lock = threading.Lock()
card_generator_lock = threading.Lock()

available_models: list[str] = []
loaded_model_instances: dict[str, list[str]] = {}

card_name_cache: dict[int, str] = {}
card_db: sqlite3.Connection | None = None
card_db_path: str | None = None


# ============================================================
# FRONTEND EVENTS
# ============================================================


def emit(event_name: str, *args):
    """Safely call a JavaScript function exposed to Eel."""
    try:
        getattr(eel, event_name)(*args)
    except Exception:
        pass


def server_log(message):
    emit("on_server_log", str(message))


def ai_log(message):
    emit("on_ai_log", str(message))


def card_log(message):
    emit("on_card_log", str(message))


def status_changed(running: bool, message: str = ""):
    emit("on_server_status", bool(running), str(message))


# ============================================================
# CONFIGURATION
# ============================================================


def load_config():
    global config

    config = DEFAULTS.copy()

    try:
        if CONFIG_FILE.exists():
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

            if isinstance(loaded, dict):
                if isinstance(loaded.get("config"), dict):
                    loaded = loaded["config"]

                config.update(
                    {
                        key: str(value)
                        for key, value in loaded.items()
                        if key in DEFAULTS
                    }
                )

    except (OSError, json.JSONDecodeError) as exc:
        server_log(f"Could not read config; defaults loaded: {exc}")

    return config


def save_config_data(values: dict | None = None):
    global config

    if values is not None:
        if not isinstance(values, dict):
            raise ValueError("Configuration must be an object.")

        for key, value in values.items():
            if key in DEFAULTS:
                config[key] = str(value)

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    return config


@eel.expose
def get_local_runtime_file_path(file_name, create_if_missing=False):
    """
    Resolves a file path inside the directory where the GUI launcher/executable
    is running without creating the file prematurely.
    """
    try:
        file_str = str(file_name).strip()
        target_path = APP_DIR / file_str

        if file_str == "ygo3_server.py":
            if not target_path.exists() and (RESOURCE_DIR / "ygo3_server.py").exists():
                target_path = RESOURCE_DIR / "ygo3_server.py"
        elif create_if_missing:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            target_path.touch(exist_ok=True)

        server_log(f"Resolved local file path: {target_path}")
        return {"success": True, "path": str(target_path)}
    except Exception as exc:
        server_log(f"Failed to resolve local file path '{file_name}': {exc}")
        return {"success": False, "error": str(exc)}


@eel.expose
def get_local_runtime_path(folder_name):
    """
    Resolves and physically creates a local directory inside the folder
    where the GUI launcher/executable is running.
    """
    try:
        base_dir = APP_DIR
        target_path = base_dir / str(folder_name).strip()

        target_path.mkdir(parents=True, exist_ok=True)

        server_log(f"Created/Resolved local directory: {target_path}")
        return {"success": True, "path": str(target_path)}
    except Exception as exc:
        server_log(f"Failed to create local directory '{folder_name}': {exc}")
        return {"success": False, "error": str(exc)}


@eel.expose
def get_config():
    return dict(config)


@eel.expose
def ygo3_launcher_config():
    """
    Compatibility endpoint for frontend code that calls
    eel.ygo3_launcher_config().
    """
    return get_config()


@eel.expose
def save_config(values):
    try:
        save_config_data(values)
        server_log(f"Settings saved to {CONFIG_FILE}")

        return {"success": True, "config": dict(config)}

    except Exception as exc:
        return {"success": False, "error": str(exc)}


@eel.expose
def import_launcher_config(text):
    """Import configuration JSON supplied by the frontend."""
    try:
        if isinstance(text, dict):
            imported = text
        else:
            imported = json.loads(str(text))

        if not isinstance(imported, dict):
            raise ValueError("The imported configuration must be a JSON object.")

        if isinstance(imported.get("config"), dict):
            imported = imported["config"]

        allowed = {
            key: str(value) for key, value in imported.items() if key in DEFAULTS
        }

        if not allowed:
            raise ValueError("No recognised YGO3 configuration settings were found.")

        save_config_data(allowed)

        server_log(f"Imported {len(allowed)} configuration settings.")

        return {"success": True, "config": dict(config), "imported": len(allowed)}

    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"Invalid JSON configuration: {exc}"}

    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ============================================================
# ENVIRONMENT
# ============================================================


def environment():
    """Build the exact YGO3 environment variables used by the original app."""
    env = os.environ.copy()

    mappings = (
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
        ("http_port", "YGO3_HTTP_PORT"),
    )

    for config_key, env_key in mappings:
        value = config.get(config_key, "").strip()
        if value:
            env[env_key] = value

    env["YGO3_NPC_AI_ENABLED"] = (
        "1"
        if config.get("npc_ai_enabled", "true").lower() in ("true", "1", "yes", "on")
        else "0"
    )

    env["YGO3_LM_STUDIO_URL"] = config.get(
        "lm_studio_url", "http://localhost:1234/v1"
    ).rstrip("/")

    env["YGO3_LM_STUDIO_MODEL"] = config.get("lm_studio_model", "")
    env["YGO3_NPC_AI_TIMEOUT"] = config.get("npc_ai_timeout", "30")

    return env


# ============================================================
# PYTHON DISCOVERY AND VALIDATION
# ============================================================


def python_info(executable: str):
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
        raise RuntimeError("Python returned no version information.")

    bits, version, actual_exe = lines[-1].split("|", 2)

    return bits == "32", version, actual_exe


def check_python_executable(executable: str):
    is_32, version, actual_exe = python_info(executable)

    if not is_32:
        raise RuntimeError(
            f"Selected Python is 64-bit (Python {version}). "
            "YGO3 requires 32-bit Python."
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
            "Python is 32-bit, but PyCryptodome is missing or cannot be imported.\n\n"
            "Install it using:\n\n"
            f'"{actual_exe}" -m pip install pycryptodome'
            f"\n\nPython output:\n{error}"
        )

    return {
        "success": True,
        "version": version,
        "executable": actual_exe,
        "architecture": "32-bit",
        "blowfish": True,
    }


@eel.expose
def check_python():
    executable = config.get("python_executable", "").strip()

    if not executable:
        return {
            "success": False,
            "error": "Select or find a 32-bit Python executable first.",
        }

    try:
        result = check_python_executable(executable)

        server_log(
            f"Verified 32-bit Python {result['version']}: {result['executable']}"
        )
        server_log("Verified Crypto.Cipher.Blowfish")

        return result

    except Exception as exc:
        server_log(f"Python check failed: {exc}")
        return {"success": False, "error": str(exc)}


def python_candidates():
    candidates = []
    seen = set()

    def add(path):
        if not path:
            return

        path = str(Path(os.path.expandvars(str(path))).expanduser())
        key = os.path.normcase(os.path.abspath(path))

        if key not in seen and Path(path).is_file():
            seen.add(key)
            candidates.append(path)

    if struct.calcsize("P") * 8 == 32:
        add(sys.executable)

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
                match = re.search(r"([A-Za-z]:\\.*?python(?:w)?\.exe)\s*$", line, re.I)

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
                            index = 0

                            while True:
                                try:
                                    version = winreg.EnumKey(key, index)
                                    index += 1
                                except OSError:
                                    break

                                try:
                                    with winreg.OpenKey(
                                        key,
                                        version + r"\InstallPath",
                                        0,
                                        winreg.KEY_READ | view,
                                    ) as install_key:
                                        install, _ = winreg.QueryValueEx(
                                            install_key, None
                                        )
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
                [
                    Path(root) / "Programs" / "Python",
                    Path(root) / "Python",
                ]
            )

    roots.extend([Path("C:/Python"), Path("C:/")])

    for root in roots:
        if root.is_dir():
            for pattern in (
                "Python*-32/python.exe",
                "Python3*/python.exe",
                "Python*/python.exe",
            ):
                try:
                    for path in root.glob(pattern):
                        add(path)
                except OSError:
                    pass

    verified = []

    for candidate in candidates:
        try:
            is_32, _, _ = python_info(candidate)

            if is_32:
                verified.append(candidate)

        except Exception:
            continue

    return verified


@eel.expose
def find_32bit_python():
    server_log("Searching for installed 32-bit Python interpreters...")

    candidates = python_candidates()
    preferred = []

    for candidate in candidates:
        normalised = candidate.replace("/", "\\").lower()

        if any(
            marker in normalised
            for marker in (
                "\\appdata\\local\\programs\\python\\",
                "\\program files\\",
                "\\program files (x86)\\",
                "\\python314-32\\",
                "\\python313-32\\",
                "\\python312-32\\",
            )
        ):
            preferred.append(candidate)

    ordered = preferred + [p for p in candidates if p not in preferred]

    if not ordered:
        return {
            "success": False,
            "error": (
                "No working 32-bit Python installation was found. "
                "Install 32-bit Python for Windows, then try again."
            ),
        }

    selected = ordered[0]
    config["python_executable"] = selected
    save_config_data()

    server_log(f"Selected preferred Python: {selected}")

    if len(ordered) > 1:
        server_log(
            "Other verified 32-bit interpreters found:\n  " + "\n  ".join(ordered[1:])
        )

    return {"success": True, "selected": selected, "candidates": ordered}


def selected_python():
    executable = config.get("python_executable", "").strip()

    if not executable:
        raise RuntimeError(
            "No Python interpreter selected. "
            "Use Find 32-bit or configure python.exe."
        )

    result = check_python_executable(executable)

    return result["executable"]


# ============================================================
# SERVER PATH AND STATUS
# ============================================================


def server_path():
    path = Path(config.get("server_script", "")).expanduser()

    if not path.is_file():
        raise FileNotFoundError(f"Server script not found: {path}")

    return path


def server_is_running():
    return server_process is not None and server_process.poll() is None


@eel.expose
def get_server_status():
    return {
        "running": server_is_running(),
        "pid": server_process.pid if server_is_running() else None,
    }


# ============================================================
# SERVER OUTPUT READER
# ============================================================


def read_server_output(proc):
    capturing = False
    response_lines = []

    try:
        if proc.stdout:
            for line in proc.stdout:
                stripped = line.rstrip("\r\n")
                server_log(stripped)
                remaining = stripped

                while remaining:
                    if not capturing:
                        begin_pos = remaining.find(LLM_RESPONSE_BEGIN)
                        single_pos = remaining.find(LLM_RESPONSE_SINGLE)

                        if begin_pos == -1 and single_pos == -1:
                            break

                        if single_pos != -1 and (
                            begin_pos == -1 or single_pos < begin_pos
                        ):
                            content = remaining[
                                single_pos + len(LLM_RESPONSE_SINGLE) :
                            ].strip()

                            if content:
                                ai_log(content + "\n\n" + "-" * 60 + "\n\n")

                            break

                        capturing = True
                        response_lines = []
                        remaining = remaining[begin_pos + len(LLM_RESPONSE_BEGIN) :]

                    else:
                        end_pos = remaining.find(LLM_RESPONSE_END)

                        if end_pos == -1:
                            if remaining:
                                response_lines.append(remaining)
                            break

                        content = remaining[:end_pos]

                        if content:
                            response_lines.append(content)

                        response = "\n".join(response_lines).strip()

                        if response:
                            ai_log(response + "\n\n" + "-" * 60 + "\n\n")

                        capturing = False
                        response_lines = []
                        remaining = remaining[end_pos + len(LLM_RESPONSE_END) :]

    except (OSError, ValueError) as exc:
        server_log(f"Output reader error: {exc}")

    finally:
        if capturing and response_lines:
            response = "\n".join(response_lines).strip()

            if response:
                ai_log(response + "\n\n" + "-" * 60 + "\n\n")

        code = proc.wait()

        with server_lock:
            if server_process is proc:
                status_changed(False, f"Server stopped (exit {code}).")

        server_log(f"Server process exited with code {code}.")


# ============================================================
# SERVER START / STOP
# ============================================================


@eel.expose
def start_server():
    global server_process

    with server_lock:
        if server_is_running():
            return {"success": False, "error": "The server is already running."}

        try:
            save_config_data()

            script = server_path()
            python_exe = selected_python()

            proc = subprocess.Popen(
                [python_exe, str(script)],
                cwd=str(script.parent),
                env=environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            server_process = proc

            status_changed(True, "Server running.")
            server_log(f"Starting {script} with 32-bit Python: {python_exe}")

            threading.Thread(
                target=read_server_output,
                args=(proc,),
                daemon=True,
                name="YGO3ServerOutput",
            ).start()

            return {"success": True, "pid": proc.pid, "message": "Server started."}

        except Exception as exc:
            server_process = None

            status_changed(False, "Server failed to start.")
            server_log(f"Could not start server: {exc}")

            return {"success": False, "error": str(exc)}


@eel.expose
def stop_server():
    global server_process

    with server_lock:
        proc = server_process

        if proc is None or proc.poll() is not None:
            server_process = None
            status_changed(False, "Server is stopped.")

            return {"success": False, "error": "The server is not running."}

        try:
            proc.terminate()
            server_log("Stop requested...")

            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

            if server_process is proc:
                server_process = None

            status_changed(False, "Server stopped.")

            return {"success": True, "message": "Server stopped."}

        except Exception as exc:
            server_log(f"Could not stop server: {exc}")

            return {"success": False, "error": str(exc)}


# ============================================================
# BP MANAGEMENT
# ============================================================


@eel.expose
def grant_bp(amount_value, user_id_value):
    global server_process

    if not bp_lock.acquire(blocking=False):
        return {"success": False, "error": "A BP operation is already in progress."}

    was_running = False

    try:
        amount = int(str(amount_value).strip())
        user_id = int(str(user_id_value).strip())

        if amount <= 0:
            raise ValueError("Enter a positive BP amount.")

        if amount > MAX_BP:
            raise ValueError(f"BP amount cannot exceed {MAX_BP:,}.")

        if user_id < 0:
            raise ValueError("User ID must be zero or greater.")

        save_config_data()

        script = server_path()
        python_exe = selected_python()
        was_running = server_is_running()

        if was_running:
            server_log("Stopping server so the saved BP balance can be updated...")
            stop_result = stop_server()

            if not stop_result["success"]:
                raise RuntimeError(stop_result["error"])

        command = [python_exe, str(script), "--grant-bp", str(amount), str(user_id)]
        server_log(f"Granting {amount:,} BP to user ID {user_id}...")

        result = subprocess.run(
            command,
            cwd=str(script.parent),
            env=environment(),
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )

        output = (result.stdout + result.stderr).strip()

        if output:
            server_log(output)

        if result.returncode != 0:
            raise RuntimeError(output or "Unknown error occurred during BP grant.")

        server_log(f"Successfully granted {amount:,} BP to user ID {user_id}.")

        return {
            "success": True,
            "message": f"Granted {amount:,} BP to user {user_id}.",
            "output": output,
        }

    except Exception as exc:
        server_log(f"BP grant error: {exc}")

        return {"success": False, "error": str(exc)}

    finally:
        if was_running:
            server_log("Restarting server after BP operation...")
            restart_result = start_server()

            if not restart_result["success"]:
                server_log(
                    "WARNING: BP operation finished, but server restart "
                    f"failed: {restart_result.get('error')}"
                )

        bp_lock.release()


# ============================================================
# YGOPRODECK CARD IDS GENERATOR
# ============================================================


def create_ygopro_session():
    if requests is None:
        raise RuntimeError(
            "The requests library is required. Install it with: pip install requests"
        )

    session = requests.Session()

    retry_strategy = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
    )

    return session


def process_cards(cards):
    processed_cards = []
    seen_card_ids = set()

    for card in cards:
        if not isinstance(card, dict):
            continue

        card_id = card.get("id")

        if not isinstance(card_id, int) or isinstance(card_id, bool) or card_id <= 0:
            continue

        if card_id in seen_card_ids:
            continue

        seen_card_ids.add(card_id)
        new_card = {"id": card_id}

        images = card.get("card_images")

        if isinstance(images, list):
            image_ids = []
            seen_image_ids = set()

            for image in images:
                if not isinstance(image, dict):
                    continue

                image_id = image.get("id")

                if (
                    isinstance(image_id, int)
                    and not isinstance(image_id, bool)
                    and image_id > 0
                    and image_id not in seen_image_ids
                ):
                    image_ids.append({"id": image_id})
                    seen_image_ids.add(image_id)

            if image_ids:
                new_card["card_images"] = image_ids

        misc_info = card.get("misc_info")

        if isinstance(misc_info, list):
            konami_ids = []
            seen_konami_ids = set()

            for info in misc_info:
                if not isinstance(info, dict):
                    continue

                konami_id = info.get("konami_id")

                if (
                    isinstance(konami_id, int)
                    and not isinstance(konami_id, bool)
                    and konami_id > 0
                    and konami_id not in seen_konami_ids
                ):
                    konami_ids.append({"konami_id": konami_id})
                    seen_konami_ids.add(konami_id)

            if konami_ids:
                new_card["misc_info"] = konami_ids

        processed_cards.append(new_card)

    return processed_cards


def generate_card_ids_json(output_file: Path, log):
    temp_file = output_file.with_suffix(output_file.suffix + ".tmp")
    error_message = None

    try:
        output_dir = output_file.parent

        log(f"Checking directory: {output_dir}")

        if not output_dir.is_dir():
            output_dir.mkdir(parents=True, exist_ok=True)

        log("Output directory confirmed.")
        log("")
        log("Connecting to YGOPRODeck API...")
        log(f"API URL: {YGOPRO_API_URL}")

        with create_ygopro_session() as session:
            response = session.get(
                YGOPRO_API_URL,
                params={"misc": "yes"},
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            log("Download completed successfully.")
            log("Parsing API response...")

            try:
                api_data = response.json()
            except ValueError as exc:
                raise ValueError("The API returned invalid JSON.") from exc

        if not isinstance(api_data, dict):
            raise ValueError("Unexpected API response structure.")

        cards = api_data.get("data")

        if not isinstance(cards, list) or not cards:
            raise ValueError("The API response does not contain a valid card list.")

        log(f"Retrieved {len(cards):,} cards.")
        log("")
        log("Processing card information...")

        processed_cards = process_cards(cards)

        if not processed_cards:
            raise ValueError("No valid cards were found in the API response.")

        formatted_data = {"data": processed_cards}

        log(f"Successfully processed {len(processed_cards):,} cards.")
        log("")
        log("Writing temporary JSON file...")

        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(formatted_data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())

        log("Verifying generated JSON...")

        with open(temp_file, "r", encoding="utf-8") as file:
            verification = json.load(file)

        verified_cards = (
            verification.get("data") if isinstance(verification, dict) else None
        )

        if not isinstance(verified_cards, list) or len(verified_cards) != len(
            processed_cards
        ):
            raise ValueError("Generated JSON verification failed.")

        log("Saving final JSON file...")

        try:
            os.replace(temp_file, output_file)

        except PermissionError as exc:
            raise PermissionError(
                f"Cannot replace the existing JSON file:\n{output_file}\n\n"
                "Check whether the file is open in another program "
                "or is read-only."
            ) from exc

        log("")
        log("=" * 55)
        log("GENERATION COMPLETED SUCCESSFULLY!")
        log("=" * 55)
        log(f"Total cards: {len(processed_cards):,}")
        log(f"Output file: {output_file}")
        log(f"File size: {output_file.stat().st_size:,} bytes")
        log("=" * 55)

        return True, None

    except requests.exceptions.Timeout:
        error_message = "The API request timed out."

    except requests.exceptions.HTTPError as exc:
        error_message = f"HTTP error:\n{exc}"

    except requests.exceptions.RequestException as exc:
        error_message = f"API connection failed:\n{exc}"

    except (PermissionError, ValueError, TypeError) as exc:
        error_message = str(exc)

    except OSError as exc:
        error_message = f"File system error:\n{exc}"

    except Exception as exc:
        error_message = f"Unexpected error:\n{type(exc).__name__}: {exc}"

    finally:
        try:
            if temp_file.exists():
                temp_file.unlink()
        except OSError:
            pass

    log("")
    log("=" * 55)
    log("GENERATION FAILED")
    log("=" * 55)
    log(error_message)
    log("=" * 55)

    return False, error_message


@eel.expose
def generate_card_ids(output_path=None):
    if not card_generator_lock.acquire(blocking=False):
        return {"success": False, "error": "Card ID generation is already running."}

    if requests is None:
        card_generator_lock.release()

        return {
            "success": False,
            "error": "Install requests first: pip install requests",
        }

    try:
        chosen_path = (
            str(output_path).strip()
            if output_path
            else config.get("card_ids_output", str(DEFAULT_CARD_IDS_FILE))
        )

        if not chosen_path:
            raise ValueError("Specify an output JSON file path.")

        output_file = Path(chosen_path).expanduser()

        config["card_ids_output"] = str(output_file)
        save_config_data()

    except Exception as exc:
        card_generator_lock.release()

        return {"success": False, "error": str(exc)}

    def worker():
        try:
            card_log("=" * 55)
            card_log("Starting generation process...")
            card_log("=" * 55)

            success, error = generate_card_ids_json(output_file, card_log)

            emit(
                "on_card_generation_finished",
                bool(success),
                error or "",
                str(output_file),
            )

        finally:
            card_generator_lock.release()

    threading.Thread(target=worker, daemon=True, name="CardIDGenerator").start()

    return {
        "success": True,
        "message": "Card ID generation started.",
        "output": str(output_file),
    }


# ============================================================
# LM STUDIO API
# ============================================================


def lm_studio_api_root():
    base_url = (
        config.get("lm_studio_url", "http://localhost:1234/v1").strip().rstrip("/")
    )

    if not base_url:
        raise ValueError("Enter the LM Studio API URL first.")

    if not base_url.startswith(("http://", "https://")):
        raise ValueError("The LM Studio API URL must start with http:// or https://.")

    if base_url.endswith("/v1"):
        base_url = base_url[:-3]

    return base_url.rstrip("/")


def lm_studio_request(url, method="GET", data=None, timeout=60):
    body = None
    headers = {"Accept": "application/json"}

    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")

    if not raw.strip():
        return {}

    return json.loads(raw)


def get_loaded_instances(models):
    loaded = {}

    for model in models:
        if not isinstance(model, dict):
            continue

        if model.get("type") != "llm":
            continue

        model_id = str(model.get("key", "")).strip()

        if not model_id:
            continue

        instances = []

        for instance in model.get("loaded_instances", []):
            if isinstance(instance, dict) and instance.get("id"):
                instances.append(str(instance["id"]))

        if instances:
            loaded[model_id] = instances

    return loaded


@eel.expose
def test_lm_studio():
    global available_models, loaded_model_instances

    try:
        api_root = lm_studio_api_root()
        payload = lm_studio_request(api_root + "/api/v1/models", timeout=30)
        models = payload.get("models", [])

        model_ids = [
            str(model.get("key", "")).strip()
            for model in models
            if (
                isinstance(model, dict)
                and model.get("type") == "llm"
                and model.get("key")
            )
        ]

        if not model_ids:
            raise RuntimeError(
                "No downloaded language models were found. "
                "Download an LLM in LM Studio first."
            )

        available_models = model_ids
        loaded_model_instances = get_loaded_instances(models)

        selected = config.get("lm_studio_model", "").strip()

        if selected not in model_ids:
            selected = model_ids[0]

        config["lm_studio_model"] = selected
        save_config_data()

        server_log("LM Studio connection successful.")
        server_log("Available models: " + ", ".join(model_ids))

        if loaded_model_instances:
            server_log(
                "Currently loaded models: " + ", ".join(loaded_model_instances.keys())
            )
        else:
            server_log("No language models are currently loaded.")

        return {
            "success": True,
            "models": model_ids,
            "selected": selected,
            "loaded": loaded_model_instances,
        }

    except Exception as exc:
        server_log(f"LM Studio connection failed: {exc}")

        return {"success": False, "error": str(exc)}


@eel.expose
def save_ai_settings(enabled, url, model, timeout):
    try:
        timeout_value = int(timeout)

        if timeout_value <= 0:
            raise ValueError("Request timeout must be greater than zero.")

        if url and not str(url).startswith(("http://", "https://")):
            raise ValueError("LM Studio URL must start with http:// or https://.")

        config["npc_ai_enabled"] = (
            "true" if str(enabled).lower() in ("true", "1", "yes", "on") else "false"
        )

        config["lm_studio_url"] = str(url).strip()
        config["lm_studio_model"] = str(model).strip()
        config["npc_ai_timeout"] = str(timeout_value)

        save_config_data()

        return {"success": True, "config": dict(config)}

    except Exception as exc:
        return {"success": False, "error": str(exc)}


@eel.expose
def toggle_model(model_id):
    global loaded_model_instances

    model_id = str(model_id).strip()

    if not model_id or model_id not in available_models:
        return {
            "success": False,
            "error": "Refresh Models and select a valid downloaded model first.",
        }

    try:
        api_root = lm_studio_api_root()
        payload = lm_studio_request(api_root + "/api/v1/models", timeout=30)
        models = payload.get("models", [])

        loaded_model_instances = get_loaded_instances(models)
        selected_instances = loaded_model_instances.get(model_id, [])

        if selected_instances:
            for instance_id in selected_instances:
                server_log(f"Unloading {model_id}, instance {instance_id}")

                lm_studio_request(
                    api_root + "/api/v1/models/unload",
                    method="POST",
                    data={"instance_id": instance_id},
                    timeout=120,
                )

            server_log(f"Unloaded model: {model_id}")
            action = "unloaded"

        else:
            for other_model, instance_ids in loaded_model_instances.items():
                if other_model == model_id:
                    continue

                for instance_id in instance_ids:
                    server_log(
                        f"Unloading previous model {other_model}, "
                        f"instance {instance_id}"
                    )

                    lm_studio_request(
                        api_root + "/api/v1/models/unload",
                        method="POST",
                        data={"instance_id": instance_id},
                        timeout=120,
                    )

            server_log(f"Loading {model_id} into memory...")

            load_result = lm_studio_request(
                api_root + "/api/v1/models/load",
                method="POST",
                data={"model": model_id},
                timeout=600,
            )

            if load_result.get("status") != "loaded":
                raise RuntimeError(
                    "LM Studio did not confirm that the model loaded. "
                    f"Response: {load_result}"
                )

            server_log(f"Successfully loaded model: {model_id}")

            load_time = load_result.get("load_time_seconds")

            if isinstance(load_time, (int, float)):
                server_log(f"Model loading time: {load_time:.1f} seconds")

            if load_result.get("instance_id"):
                server_log(f"LM Studio instance ID: {load_result['instance_id']}")

            action = "loaded"

        refreshed = lm_studio_request(api_root + "/api/v1/models", timeout=30)
        loaded_model_instances = get_loaded_instances(refreshed.get("models", []))

        if action == "loaded":
            config["lm_studio_model"] = model_id
            save_config_data()

        return {
            "success": True,
            "action": action,
            "model": model_id,
            "loaded": loaded_model_instances,
        }

    except Exception as exc:
        server_log(f"LM Studio model operation failed: {exc}")

        return {"success": False, "error": str(exc)}


# ============================================================
# CARD NAME LOOKUP AND AI LOG ANNOTATION
# ============================================================


def card_name(code: int):
    global card_db, card_db_path

    if code in card_name_cache:
        return card_name_cache[code]

    name = None

    try:
        path = config.get("card_database", "").strip()

        if path and Path(path).is_file():
            if card_db_path != path:
                if card_db is not None:
                    card_db.close()

                card_db = sqlite3.connect(
                    f"file:{Path(path).as_posix()}?mode=ro",
                    uri=True,
                    check_same_thread=False,
                )

                card_db_path = path

            row = card_db.execute(
                "SELECT name FROM texts WHERE id = ?", (int(code),)
            ).fetchone()

            name = row[0] if row else None

    except (sqlite3.Error, OSError, ValueError, TypeError):
        name = None

    if name:
        card_name_cache[code] = name

    return name


def annotate_cards(text):
    def name_it(match):
        number = match.group(1)
        name = card_name(int(number))

        return f"{name} ({number})" if name else number

    lines = []

    for line in str(text).split("\n"):
        head = line.split(":", 1)[0]

        if head in ("CARD", "REASON", "THINKING"):
            code = re.search(r"'code': (\d+)", line)

            if head == "CARD" and code:
                match = re.match(r"(\d+)", code.group(1))
                label = name_it(match) if match else code.group(1)

                if "'can_direct': True" in line:
                    label += ", direct attack"

                line = "CARD: " + label

            else:
                line = re.sub(
                    r"(?<![\w(])(\d{6,9})(?![\w)])",
                    lambda match: name_it(match),
                    line,
                )

        lines.append(line)

    return "\n".join(lines)


@eel.expose
def annotate_ai_text(text):
    try:
        return annotate_cards(str(text))
    except Exception:
        return str(text)


# ============================================================
# CLEAR LOGS / SHUTDOWN
# ============================================================


@eel.expose
def clear_server_logs():
    emit("on_clear_server_logs")
    return {"success": True}


@eel.expose
def clear_ai_logs():
    emit("on_clear_ai_logs")
    return {"success": True}


def shutdown():
    global server_process, card_db

    with server_lock:
        proc = server_process

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)

            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        server_process = None

    try:
        if card_db is not None:
            card_db.close()
            card_db = None

    except sqlite3.Error:
        pass


# ============================================================
# START EEL
# ============================================================


def on_close(page, sockets):
    shutdown()
    sys.exit(0)


if __name__ == "__main__":
    load_config()

    if not WEB_DIR.is_dir():
        raise FileNotFoundError(
            f"Web folder not found: {WEB_DIR}\n"
            "Make sure the web folder is beside main.py "
            "or included in the PyInstaller build."
        )

    eel.init(str(WEB_DIR))

    try:
        eel.start(
            "index.html",
            size=(1220, 860),
            port=0,
            close_callback=on_close,
        )

    finally:
        shutdown()
