"""CustomTkinter launcher, BP manager, Card IDs generator and LM Studio NPC AI manager for YGO3."""

from __future__ import annotations

import json
from datetime import datetime
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import queue
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError as exc:
    raise SystemExit(
        "CustomTkinter is required. Install with: "
        "python -m pip install customtkinter"
    ) from exc

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


APP_DIR = Path(sys.argv[0]).resolve().parent
CONFIG_FILE = APP_DIR / "ygo3_launcher_config.json"
DEFAULT_SERVER = APP_DIR / "ygo3_server.py"
MAX_BP = 0xFFFFFFFF

LLM_RESPONSE_BEGIN = "[LLM_RESPONSE_BEGIN]"
LLM_RESPONSE_END = "[LLM_RESPONSE_END]"
LLM_RESPONSE_SINGLE = "[LLM_RESPONSE]"

# YGOPRODeck API Constants
YGOPRO_API_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
DEFAULT_CARD_IDS_FILE = APP_DIR / "card-ids.json"
REQUEST_TIMEOUT = (15, 120)
USER_AGENT = "CardJsonGenerator/2.0"

# Centralised colours keep the interface consistent and make later restyling easy.
COLOURS = {
    "background": "#0B1020",
    "surface": "#121B2D",
    "surface_alt": "#18243A",
    "surface_deep": "#080E1A",
    "border": "#263650",
    "text": "#E8EEF8",
    "muted": "#91A0B8",
    "accent": "#3978E8",
    "accent_hover": "#2D65C9",
    "success": "#35B779",
    "warning": "#D6A23D",
    "danger": "#D45B65",
    "danger_hover": "#B74650",
}


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
}


def load_config():
    data = DEFAULTS.copy()

    try:
        if CONFIG_FILE.exists():
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(
                    {key: str(value) for key, value in loaded.items() if key in data}
                )
    except (OSError, json.JSONDecodeError) as exc:
        messagebox.showwarning(
            "Config",
            f"Could not read config; defaults loaded.\n{exc}",
        )

    return data


# ============================================================
# CARD IDS API & PROCESSING HELPERS
# ============================================================


def create_ygopro_session():
    """Create an HTTP session with automatic retries."""
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
    """Extract Card ID, Card image IDs, and Konami IDs."""
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

        # Card Image IDs
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

        # Konami IDs
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
    """Download YGOPRODeck card information, process cards, and save JSON."""
    temp_file = output_file.with_suffix(output_file.suffix + ".tmp")

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
            except ValueError as exception:
                raise ValueError("The API returned invalid JSON.") from exception

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
        except PermissionError as exception:
            raise PermissionError(
                f"Cannot replace the existing JSON file:\n{output_file}\n\n"
                "Check whether the file is open in another program or is read-only."
            ) from exception

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
    except requests.exceptions.HTTPError as exception:
        error_message = f"HTTP error:\n{exception}"
    except requests.exceptions.RequestException as exception:
        error_message = f"API connection failed:\n{exception}"
    except (PermissionError, ValueError, TypeError) as exception:
        error_message = str(exception)
    except OSError as exception:
        error_message = f"File system error:\n{exception}"
    except Exception as exception:
        error_message = f"Unexpected error:\n{type(exception).__name__}: {exception}"
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


class Launcher(ctk.CTk):

    def __init__(self):
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        super().__init__()

        self.title("YGO3 | Server Control Dashboard")
        self.geometry("1220x860")
        self.minsize(1000, 720)
        self.configure(fg_color=COLOURS["background"])

        self.config_values = load_config()
        self.entries = {}

        self.process = None
        self.log_queue = queue.Queue()
        self.ai_log_queue = queue.Queue()
        self.card_id_queue = queue.Queue()
        self._closing = False
        self._card_gen_running = False

        self.available_models = []

        # LM Studio model state.
        self._model_load_in_progress = False
        self._loading_model = None
        self._active_model_id = None
        self._active_model_instance_id = None

        # Maps model IDs to their currently loaded instance IDs.
        self.loaded_model_instances = {}

        self._build()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain_log_queue)
        self.after(100, self._drain_ai_log_queue)
        self.after(100, self._drain_card_id_queue)

    # ============================================================
    # GUI HELPERS
    # ============================================================

    def _make_panel(self, parent, **kwargs):
        return ctk.CTkFrame(
            parent,
            fg_color=COLOURS["surface"],
            corner_radius=14,
            border_width=1,
            border_color=COLOURS["border"],
            **kwargs,
        )

    def _section_label(self, parent, text, **kwargs):
        return ctk.CTkLabel(
            parent,
            text=text,
            text_color=COLOURS["text"],
            font=ctk.CTkFont(size=16, weight="bold"),
            **kwargs,
        )

    def _style_textbox(self, textbox):
        textbox.configure(
            fg_color=COLOURS["surface_deep"],
            border_color=COLOURS["border"],
            border_width=1,
            corner_radius=10,
            text_color=COLOURS["text"],
            scrollbar_button_color=COLOURS["surface_alt"],
            scrollbar_button_hover_color=COLOURS["accent"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )

    def _append_styled_log(self, textbox, text, default_tag="info"):
        """Append timestamped, colour-coded output to a log console."""
        try:
            content = str(text).rstrip("\n")
            if not content:
                return
            timestamp = datetime.now().strftime("%H:%M:%S")
            lines = content.splitlines()
            textbox.insert("end", f"{timestamp}  ", "timestamp")
            for line in lines:
                textbox.insert("end", line + "\n", default_tag)

            textbox.insert("end", "\n")
            textbox.see("end")

        except (tk.TclError, AttributeError):
            pass

    # ============================================================
    # GUI LAYOUT & DASHBOARD NAVIGATION
    # ============================================================

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --------------------------------------------------------
        # LEFT NAVIGATION MENU
        # --------------------------------------------------------
        nav_frame = ctk.CTkFrame(
            self,
            fg_color=COLOURS["surface"],
            corner_radius=0,
            border_width=1,
            border_color=COLOURS["border"],
            width=240,
        )
        nav_frame.grid(row=0, column=0, sticky="nsew")
        nav_frame.grid_rowconfigure(7, weight=1)

        # Logo / App Title Area
        brand_area = ctk.CTkFrame(nav_frame, fg_color="transparent")
        brand_area.pack(fill="x", padx=20, pady=(24, 16))

        ctk.CTkLabel(
            brand_area,
            text="YGO3",
            text_color=COLOURS["text"],
            font=ctk.CTkFont(size=26, weight="bold"),
        ).pack(anchor="w")

        ctk.CTkLabel(
            brand_area,
            text="SERVER CONTROL DASHBOARD",
            text_color=COLOURS["muted"],
            font=ctk.CTkFont(size=9, weight="bold"),
        ).pack(anchor="w", pady=(1, 0))

        # Status Badge inside Navigation
        status_box = ctk.CTkFrame(
            nav_frame,
            fg_color=COLOURS["surface_deep"],
            corner_radius=10,
            border_width=1,
            border_color=COLOURS["border"],
        )
        status_box.pack(fill="x", padx=16, pady=(0, 20))

        ctk.CTkLabel(
            status_box,
            text="SERVER STATUS",
            text_color=COLOURS["muted"],
            font=ctk.CTkFont(size=10, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 0))

        self.status = ctk.CTkLabel(
            status_box,
            text="●  STOPPED",
            text_color=COLOURS["danger"],
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.status.pack(anchor="w", padx=12, pady=(2, 8))

        # Navigation Buttons
        nav_buttons_container = ctk.CTkFrame(nav_frame, fg_color="transparent")
        nav_buttons_container.pack(fill="x", padx=12, pady=10)

        self.nav_buttons = {}
        nav_items = [
            ("Server Settings", "⚙  Server Settings"),
            ("BP Manager", "💰  BP Manager"),
            ("Card IDs", "🃏  Card IDs"),
            ("NPC AI", "🤖  NPC AI (LM Studio)"),
            ("Server Logs", "📋  Server Logs"),
            ("LM Studio Logs", "📊  AI Logs"),
        ]

        for name, label in nav_items:
            btn = ctk.CTkButton(
                nav_buttons_container,
                text=label,
                anchor="w",
                height=38,
                fg_color="transparent",
                text_color=COLOURS["text"],
                hover_color=COLOURS["surface_alt"],
                font=ctk.CTkFont(size=13, weight="bold"),
                command=lambda n=name: self._show_tab(n),
            )
            btn.pack(fill="x", pady=3)
            self.nav_buttons[name] = btn

        # Bottom Menu Info / Config file label
        footer_info = ctk.CTkFrame(nav_frame, fg_color="transparent")
        footer_info.pack(side="bottom", fill="x", padx=16, pady=16)

        ctk.CTkLabel(
            footer_info,
            text=f"CONFIG: {CONFIG_FILE.name}",
            text_color=COLOURS["muted"],
            font=ctk.CTkFont(size=9, weight="bold"),
            wraplength=200,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        ctk.CTkButton(
            footer_info,
            text="Save Settings",
            command=self.save_config,
            height=34,
            fg_color=COLOURS["accent"],
            hover_color=COLOURS["accent_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(fill="x")

        # --------------------------------------------------------
        # RIGHT CONTENT AREA (DYNAMIC CONTAINER)
        # --------------------------------------------------------
        self.content_area = ctk.CTkFrame(
            self, fg_color=COLOURS["background"], corner_radius=0
        )
        self.content_area.grid(row=0, column=1, sticky="nsew")
        self.content_area.grid_columnconfigure(0, weight=1)
        self.content_area.grid_rowconfigure(0, weight=1)

        # Build all tab views inside the content container frames
        self.tab_frames = {}
        for name, _ in nav_items:
            frame = ctk.CTkFrame(self.content_area, fg_color="transparent")
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(0, weight=1)
            self.tab_frames[name] = frame

        self._build_server_settings(self.tab_frames["Server Settings"])
        self._build_bp_manager(self.tab_frames["BP Manager"])
        self._build_card_ids_tab(self.tab_frames["Card IDs"])
        self._build_npc_ai_tab(self.tab_frames["NPC AI"])
        self._build_logs(self.tab_frames["Server Logs"])
        self._build_ai_logs(self.tab_frames["LM Studio Logs"])

        # Show default landing tab
        self._show_tab("Server Settings")

    def _show_tab(self, name):
        """Switch views by raising the selected frame and styling active nav button."""
        for tab_name, frame in self.tab_frames.items():
            if tab_name == name:
                frame.tkraise()
            else:
                frame.lower()

        for btn_name, btn in self.nav_buttons.items():
            if btn_name == name:
                btn.configure(
                    fg_color=COLOURS["surface_alt"], text_color=COLOURS["accent"]
                )
            else:
                btn.configure(fg_color="transparent", text_color=COLOURS["text"])

    # ============================================================
    # SERVER SETTINGS
    # ============================================================

    def _build_server_settings(self, tab):
        container = ctk.CTkScrollableFrame(
            tab, fg_color="transparent", scrollbar_button_color=COLOURS["surface_alt"]
        )
        container.pack(fill="both", expand=True, padx=20, pady=20)
        container.grid_columnconfigure(0, weight=1)

        paths = self._make_panel(container)
        paths.pack(fill="x", pady=(0, 12))
        paths.grid_columnconfigure(1, weight=1)

        self._section_label(paths, "Directories, files and Python").grid(
            row=0, column=0, columnspan=4, padx=16, pady=(16, 8), sticky="w"
        )

        ctk.CTkLabel(
            paths,
            text="32-bit Python executable",
            width=190,
            anchor="w",
            text_color=COLOURS["muted"],
        ).grid(row=1, column=0, padx=(16, 6), pady=8, sticky="w")

        py_ent = ctk.CTkEntry(
            paths,
            placeholder_text="Select 32-bit python.exe",
            height=34,
            border_color=COLOURS["border"],
        )
        py_ent.insert(0, self.config_values.get("python_executable", ""))
        py_ent.grid(row=1, column=1, padx=6, pady=8, sticky="ew")
        self.entries["python_executable"] = py_ent

        ctk.CTkButton(
            paths, text="Browse...", width=86, height=34, command=self._browse_python
        ).grid(row=1, column=2, padx=4, pady=8)

        py_actions = ctk.CTkFrame(paths, fg_color="transparent")
        py_actions.grid(row=1, column=3, padx=(2, 16), pady=8, sticky="e")

        ctk.CTkButton(
            py_actions,
            text="Find 32-bit",
            width=95,
            height=34,
            command=self.find_32bit_python,
        ).pack(side="left", padx=3)

        ctk.CTkButton(
            py_actions,
            text="Check",
            width=60,
            height=34,
            fg_color=COLOURS["surface_alt"],
            hover_color=COLOURS["border"],
            command=self.check_python,
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
            ctk.CTkLabel(
                paths, text=label, width=190, anchor="w", text_color=COLOURS["muted"]
            ).grid(row=row, column=0, padx=(16, 6), pady=8, sticky="w")

            ent = ctk.CTkEntry(paths, height=34, border_color=COLOURS["border"])
            ent.insert(0, self.config_values.get(key, ""))
            ent.grid(row=row, column=1, columnspan=2, padx=6, pady=8, sticky="ew")
            self.entries[key] = ent

            ctk.CTkButton(
                paths,
                text="Browse...",
                width=90,
                height=34,
                command=lambda k=key, t=kind: self._browse(k, t),
            ).grid(row=row, column=3, padx=(6, 16), pady=8)

        settings = self._make_panel(container)
        settings.pack(fill="x", pady=(0, 12))
        settings.grid_columnconfigure(1, weight=1)
        settings.grid_columnconfigure(3, weight=1)

        self._section_label(settings, "Network & Ports").grid(
            row=0, column=0, columnspan=4, padx=16, pady=(16, 8), sticky="w"
        )

        net_fields = [
            ("host", "Bind address"),
            ("front_address", "Front address"),
            ("gate_port", "Gate Port"),
            ("front_port", "Front Port"),
            ("duel_port", "Duel Port"),
            ("http_port", "HTTP Port"),
        ]

        for idx, (key, label) in enumerate(net_fields):
            row = (idx // 2) + 1
            left = idx % 2 == 0
            label_col = 0 if left else 2
            entry_col = 1 if left else 3

            ctk.CTkLabel(
                settings, text=label, width=110, anchor="w", text_color=COLOURS["muted"]
            ).grid(row=row, column=label_col, padx=(16, 5), pady=8, sticky="w")

            ent = ctk.CTkEntry(settings, height=34, border_color=COLOURS["border"])
            ent.insert(0, self.config_values.get(key, ""))
            ent.grid(row=row, column=entry_col, padx=5, pady=8, sticky="ew")
            self.entries[key] = ent

        controls = ctk.CTkFrame(container, fg_color="transparent")
        controls.pack(fill="x", pady=(4, 16))

        self.start_btn = ctk.CTkButton(
            controls,
            text="▶  Start server",
            height=42,
            width=160,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_server,
        )
        self.start_btn.pack(side="left", padx=(0, 10))

        self.stop_btn = ctk.CTkButton(
            controls,
            text="■  Stop server",
            height=42,
            width=150,
            fg_color=COLOURS["danger"],
            hover_color=COLOURS["danger_hover"],
            command=self.stop_server,
            state="disabled",
        )
        self.stop_btn.pack(side="left", padx=4)

    # ============================================================
    # BP MANAGER
    # ============================================================

    def _build_bp_manager(self, tab):
        container = ctk.CTkFrame(tab, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)
        container.grid_columnconfigure(0, weight=1)

        bp = self._make_panel(container)
        bp.pack(fill="x", pady=(0, 12))
        bp.grid_columnconfigure(1, weight=1)

        self._section_label(bp, "Add BP to shop balance").grid(
            row=0, column=0, columnspan=5, padx=16, pady=(16, 12), sticky="w"
        )

        ctk.CTkLabel(bp, text="User ID", text_color=COLOURS["muted"]).grid(
            row=1, column=0, padx=(16, 8), pady=12
        )

        self.user_entry = ctk.CTkEntry(
            bp, width=120, height=38, border_color=COLOURS["border"]
        )
        self.user_entry.insert(0, self.config_values.get("user_id", "1001"))
        self.user_entry.grid(row=1, column=1, padx=5, pady=12, sticky="w")

        ctk.CTkLabel(bp, text="BP to add", text_color=COLOURS["muted"]).grid(
            row=1, column=2, padx=12, pady=12
        )

        self.bp_entry = ctk.CTkEntry(
            bp,
            width=160,
            height=38,
            placeholder_text="e.g. 500",
            border_color=COLOURS["border"],
        )
        self.bp_entry.grid(row=1, column=3, padx=5, pady=12)

        ctk.CTkButton(
            bp,
            text="Add BP",
            height=38,
            width=120,
            command=self.add_bp,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=1, column=4, padx=(12, 16), pady=12)

        ctk.CTkLabel(
            bp,
            text=(
                f"Hard safety ceiling: {MAX_BP:,} BP "
                "(unsigned 32-bit protocol limit)."
            ),
            text_color=COLOURS["muted"],
            wraplength=700,
            justify="left",
        ).grid(row=2, column=0, columnspan=5, padx=16, pady=(0, 16), sticky="w")

    # ============================================================
    # CARD IDS GENERATOR TAB
    # ============================================================

    def _build_card_ids_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        # Top control panel
        top_panel = self._make_panel(tab)
        top_panel.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 12))
        top_panel.grid_columnconfigure(1, weight=1)

        self._section_label(top_panel, "YGOPRODeck Card IDs Generator").grid(
            row=0, column=0, columnspan=3, padx=16, pady=(16, 4), sticky="w"
        )

        ctk.CTkLabel(
            top_panel,
            text="Download card IDs, artwork image IDs, and Konami IDs from YGOPRODeck into card-ids.json.",
            text_color=COLOURS["muted"],
            font=ctk.CTkFont(size=12),
        ).grid(row=1, column=0, columnspan=3, padx=16, pady=(0, 12), sticky="w")

        ctk.CTkLabel(
            top_panel,
            text="Output JSON path",
            width=140,
            anchor="w",
            text_color=COLOURS["muted"],
        ).grid(row=2, column=0, padx=(16, 6), pady=8, sticky="w")

        out_entry = ctk.CTkEntry(
            top_panel,
            height=36,
            border_color=COLOURS["border"],
        )
        out_entry.insert(
            0, self.config_values.get("card_ids_output", str(DEFAULT_CARD_IDS_FILE))
        )
        out_entry.grid(row=2, column=1, padx=6, pady=8, sticky="ew")
        self.entries["card_ids_output"] = out_entry

        ctk.CTkButton(
            top_panel,
            text="Browse...",
            width=90,
            height=36,
            fg_color=COLOURS["surface_alt"],
            hover_color=COLOURS["border"],
            command=self._browse_card_ids_output,
        ).grid(row=2, column=2, padx=(6, 16), pady=8)

        action_row = ctk.CTkFrame(top_panel, fg_color="transparent")
        action_row.grid(
            row=3, column=0, columnspan=3, padx=16, pady=(8, 12), sticky="ew"
        )
        action_row.grid_columnconfigure(1, weight=1)

        self.generate_card_ids_btn = ctk.CTkButton(
            action_row,
            text="⬇  Download & Generate JSON",
            height=40,
            width=230,
            fg_color=COLOURS["success"],
            hover_color="#289460",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_card_id_generation,
        )
        self.generate_card_ids_btn.grid(row=0, column=0, padx=(0, 14), sticky="w")

        self.card_id_progress = ctk.CTkProgressBar(
            action_row,
            progress_color=COLOURS["success"],
            fg_color=COLOURS["surface_deep"],
        )
        self.card_id_progress.grid(row=0, column=1, sticky="ew")
        self.card_id_progress.set(0)

        # Bottom log panel
        log_panel = self._make_panel(tab)
        log_panel.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 20))
        log_panel.grid_columnconfigure(0, weight=1)
        log_panel.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(
            log_panel,
            fg_color=COLOURS["surface_alt"],
            corner_radius=10,
        )
        toolbar.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 10))

        ctk.CTkLabel(
            toolbar,
            text="GENERATION OUTPUT",
            text_color=COLOURS["text"],
            font=ctk.CTkFont(size=10, weight="bold"),
        ).pack(side="left", padx=12, pady=9)

        ctk.CTkButton(
            toolbar,
            text="Clear",
            width=75,
            height=28,
            fg_color=COLOURS["danger"],
            hover_color=COLOURS["danger_hover"],
            command=self._clear_card_id_log,
        ).pack(side="right", padx=8, pady=5)

        self.card_id_textbox = ctk.CTkTextbox(
            log_panel,
            wrap="word",
            activate_scrollbars=True,
            border_width=1,
            corner_radius=10,
        )
        self._style_textbox(self.card_id_textbox)
        self.card_id_textbox.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.card_id_textbox.configure(state="disabled")

        self._log_card_id("Ready to fetch card IDs from YGOPRODeck API.")

    def _browse_card_ids_output(self):
        current = self.entries["card_ids_output"].get().strip()
        initial_dir = str(Path(current).parent) if current else str(APP_DIR)
        chosen = filedialog.asksaveasfilename(
            title="Save card-ids.json",
            initialdir=initial_dir,
            initialfile="card-ids.json",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if chosen:
            self.entries["card_ids_output"].delete(0, "end")
            self.entries["card_ids_output"].insert(0, chosen)

    def _log_card_id(self, message):
        try:
            self.card_id_textbox.configure(state="normal")
            self.card_id_textbox.insert("end", str(message) + "\n")
            self.card_id_textbox.see("end")
            self.card_id_textbox.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass

    def _clear_card_id_log(self):
        try:
            self.card_id_textbox.configure(state="normal")
            self.card_id_textbox.delete("1.0", "end")
            self.card_id_textbox.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass

    def _drain_card_id_queue(self):
        while True:
            try:
                msg = self.card_id_queue.get_nowait()
            except queue.Empty:
                break

            kind = msg[0]
            if kind == "log":
                self._log_card_id(msg[1])
            elif kind == "done":
                self._finish_card_id_generation(msg[1], msg[2], msg[3])

        if not self._closing:
            try:
                self.after(100, self._drain_card_id_queue)
            except tk.TclError:
                pass

    def start_card_id_generation(self):
        if self._card_gen_running:
            return

        if not HAS_REQUESTS:
            messagebox.showerror(
                "Missing Dependency",
                "The 'requests' library is required to download card IDs.\n\n"
                "Install it with:\npython -m pip install requests",
            )
            return

        out_str = self.entries["card_ids_output"].get().strip()
        if not out_str:
            messagebox.showwarning(
                "Card IDs", "Specify an output JSON file path first."
            )
            return

        output_path = Path(out_str).expanduser()
        self.save_config(quiet=True)

        self._card_gen_running = True
        self.generate_card_ids_btn.configure(state="disabled")
        self.card_id_progress.configure(mode="indeterminate")
        self.card_id_progress.start()

        self._log_card_id("")
        self._log_card_id("=" * 55)
        self._log_card_id("Starting generation process...")
        self._log_card_id("=" * 55)

        def worker():
            def worker_log(text):
                self.card_id_queue.put(("log", text))

            success, error = generate_card_ids_json(output_path, worker_log)
            self.card_id_queue.put(("done", success, error, str(output_path)))

        threading.Thread(target=worker, daemon=True, name="CardIDGenerator").start()

    def _finish_card_id_generation(self, success, error, output_path_str):
        if self._closing:
            return

        self.card_id_progress.stop()
        self.card_id_progress.configure(mode="determinate")
        self.card_id_progress.set(1 if success else 0)
        self.generate_card_ids_btn.configure(state="normal")
        self._card_gen_running = False

        if success:
            self._log(f"Generated card IDs JSON: {output_path_str}")
            messagebox.showinfo(
                "Generation Successful",
                f"card-ids.json generated successfully!\n\nSaved to:\n{output_path_str}",
            )
        else:
            messagebox.showerror(
                "Generation Failed",
                error or "An unexpected error occurred.",
            )

    # ============================================================
    # NPC AI TAB
    # ============================================================

    def _build_npc_ai_tab(self, tab):
        container = ctk.CTkScrollableFrame(
            tab, fg_color="transparent", scrollbar_button_color=COLOURS["surface_alt"]
        )
        container.pack(fill="both", expand=True, padx=20, pady=20)
        container.grid_columnconfigure(0, weight=1)

        panel = self._make_panel(container)
        panel.pack(fill="x", pady=(0, 12))
        panel.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            panel,
            text="Local NPC AI  /  LM Studio",
            text_color=COLOURS["text"],
            font=ctk.CTkFont(size=19, weight="bold"),
        ).grid(row=0, column=0, columnspan=4, padx=16, pady=(16, 12), sticky="w")

        self.npc_ai_enabled = tk.BooleanVar(
            value=self.config_values.get("npc_ai_enabled", "true").lower()
            in ("true", "1", "yes", "on")
        )

        ctk.CTkCheckBox(
            panel,
            text="Enable AI-controlled NPCs",
            variable=self.npc_ai_enabled,
            text_color=COLOURS["text"],
        ).grid(row=1, column=0, columnspan=4, padx=16, pady=10, sticky="w")

        ctk.CTkLabel(
            panel,
            text="LM Studio API URL",
            width=180,
            anchor="w",
            text_color=COLOURS["muted"],
        ).grid(row=2, column=0, padx=(16, 6), pady=10, sticky="w")

        url_entry = ctk.CTkEntry(
            panel,
            height=38,
            placeholder_text="http://localhost:1234/v1",
            border_color=COLOURS["border"],
        )
        url_entry.insert(
            0, self.config_values.get("lm_studio_url", "http://localhost:1234/v1")
        )
        url_entry.grid(
            row=2, column=1, columnspan=3, padx=(6, 16), pady=10, sticky="ew"
        )
        self.entries["lm_studio_url"] = url_entry

        ctk.CTkLabel(
            panel,
            text="Select AI Model",
            width=180,
            anchor="w",
            text_color=COLOURS["muted"],
        ).grid(row=3, column=0, padx=(16, 6), pady=10, sticky="w")

        saved_model = self.config_values.get("lm_studio_model", "").strip()
        placeholder = "Click Refresh Models to load models"

        self.model_dropdown = ctk.CTkComboBox(
            panel,
            values=[saved_model] if saved_model else [placeholder],
            state="readonly",
            command=self._on_model_selected,
            height=38,
            border_color=COLOURS["border"],
        )
        self.model_dropdown.set(saved_model or placeholder)
        self.model_dropdown.grid(row=3, column=1, padx=(6, 5), pady=10, sticky="ew")

        self.model_action_btn = ctk.CTkButton(
            panel,
            text="Load Model",
            width=120,
            height=38,
            command=self._toggle_selected_model,
        )
        self.model_action_btn.grid(row=3, column=2, padx=5, pady=10)

        self.refresh_models_btn = ctk.CTkButton(
            panel,
            text="Refresh Models",
            width=130,
            height=38,
            fg_color=COLOURS["surface_alt"],
            hover_color=COLOURS["border"],
            command=self.test_lm_studio,
        )
        self.refresh_models_btn.grid(row=3, column=3, padx=(5, 16), pady=10)

        ctk.CTkLabel(
            panel,
            text="Request timeout (seconds)",
            width=180,
            anchor="w",
            text_color=COLOURS["muted"],
        ).grid(row=4, column=0, padx=(16, 6), pady=10, sticky="w")

        timeout_entry = ctk.CTkEntry(
            panel, height=38, placeholder_text="30", border_color=COLOURS["border"]
        )
        timeout_entry.insert(0, self.config_values.get("npc_ai_timeout", "30"))
        timeout_entry.grid(
            row=4, column=1, columnspan=3, padx=(6, 16), pady=10, sticky="ew"
        )
        self.entries["npc_ai_timeout"] = timeout_entry

        buttons = ctk.CTkFrame(panel, fg_color="transparent")
        buttons.grid(row=5, column=0, columnspan=4, padx=12, pady=14, sticky="w")

        ctk.CTkButton(
            buttons, text="Test LM Studio", height=38, command=self.test_lm_studio
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            buttons,
            text="Save AI settings",
            height=38,
            fg_color=COLOURS["surface_alt"],
            hover_color=COLOURS["border"],
            command=self.save_config,
        ).pack(side="left", padx=4)

        self.ai_status = ctk.CTkLabel(
            panel,
            text="AI status: Not tested",
            text_color=COLOURS["muted"],
            wraplength=700,
        )
        self.ai_status.grid(
            row=6, column=0, columnspan=4, padx=16, pady=(0, 16), sticky="w"
        )

        help_panel = self._make_panel(container)
        help_panel.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            help_panel,
            text=(
                "LM Studio must be running with its local server enabled. "
                "Click Refresh Models to retrieve downloaded language models. "
                "Choose a model from the dropdown, then click Load Model. "
                "To unload the selected active model, click Unload Model. "
                "Switching models unloads the previous model first."
            ),
            text_color=COLOURS["muted"],
            wraplength=760,
            justify="left",
        ).pack(fill="x", padx=16, pady=16)

    # ============================================================
    # LM STUDIO API HELPERS
    # ============================================================

    def _lm_studio_api_root(self):
        """Convert the configured /v1 URL into the LM Studio server root."""
        base_url = self.entries["lm_studio_url"].get().strip().rstrip("/")

        if not base_url:
            raise ValueError("Enter the LM Studio API URL first.")

        if not base_url.startswith(("http://", "https://")):
            raise ValueError(
                "The LM Studio API URL must start with http:// or https://."
            )

        if base_url.endswith("/v1"):
            base_url = base_url[:-3]

        return base_url.rstrip("/")

    @staticmethod
    def _lm_studio_request(url, method="GET", data=None, timeout=60):
        """Send a JSON request to LM Studio's native API."""
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

    def _get_loaded_instances(self, models):
        """Return {model_id: [instance_id, ...]} from an LM Studio model list."""
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

    def _update_model_action_button(self):
        """Set the button text based on the selected model's loaded state."""
        if not hasattr(self, "model_action_btn"):
            return

        if self._model_load_in_progress:
            self.model_action_btn.configure(text="Working...", state="disabled")
            return

        selected_model = self.model_dropdown.get().strip()
        loaded = self.loaded_model_instances

        if selected_model in loaded:
            button_text = "Unload Model"
        elif loaded:
            button_text = "Switch Model"
        else:
            button_text = "Load Model"

        self.model_action_btn.configure(text=button_text, state="normal")

    def _on_model_selected(self, selected_model):
        """Changing the dropdown only changes the selection, never model state."""
        selected_model = str(selected_model).strip()

        if self._model_load_in_progress:
            return

        self._log(f"Selected LM Studio model: {selected_model}")
        self._update_model_action_button()

    # ============================================================
    # LOAD / UNLOAD BUTTON
    # ============================================================

    def _toggle_selected_model(self):
        """Load, unload, or switch models when the button is clicked."""
        if self._model_load_in_progress:
            return

        selected_model = self.model_dropdown.get().strip()

        if not selected_model or selected_model not in self.available_models:
            messagebox.showwarning(
                "LM Studio",
                "Refresh Models and select a valid downloaded model first.",
            )
            return

        self._model_load_in_progress = True
        self._loading_model = selected_model

        self.model_dropdown.configure(state="disabled")
        self.refresh_models_btn.configure(state="disabled")
        self.model_action_btn.configure(text="Working...", state="disabled")

        try:
            api_root = self._lm_studio_api_root()
        except ValueError as exc:
            self._model_load_in_progress = False
            self._loading_model = None
            self.model_dropdown.configure(state="readonly")
            self.refresh_models_btn.configure(state="normal")
            self._update_model_action_button()
            messagebox.showerror("LM Studio", str(exc))
            return

        self.ai_status.configure(
            text=f"AI status: Checking model state for {selected_model}...",
            text_color=COLOURS["warning"],
        )

        self._log(f"Model action requested: {selected_model}")

        def worker():
            action = None
            load_result = None

            try:
                payload = self._lm_studio_request(
                    api_root + "/api/v1/models", timeout=30
                )
                models = payload.get("models", [])
                loaded = self._get_loaded_instances(models)
                selected_instances = loaded.get(selected_model, [])

                if selected_instances:
                    action = "unload"

                    for instance_id in selected_instances:
                        self._log(f"Unloading {selected_model}, instance {instance_id}")
                        self._lm_studio_request(
                            api_root + "/api/v1/models/unload",
                            method="POST",
                            data={"instance_id": instance_id},
                            timeout=120,
                        )

                    self._log(f"Unloaded model: {selected_model}")

                else:
                    action = "load"

                    for other_model, instance_ids in loaded.items():
                        if other_model == selected_model:
                            continue

                        for instance_id in instance_ids:
                            self._log(
                                f"Unloading previous model {other_model}, "
                                f"instance {instance_id}"
                            )
                            self._lm_studio_request(
                                api_root + "/api/v1/models/unload",
                                method="POST",
                                data={"instance_id": instance_id},
                                timeout=120,
                            )

                    self._log(f"Loading {selected_model} into memory...")
                    load_result = self._lm_studio_request(
                        api_root + "/api/v1/models/load",
                        method="POST",
                        data={"model": selected_model},
                        timeout=600,
                    )

                    if load_result.get("status") != "loaded":
                        raise RuntimeError(
                            "LM Studio did not confirm that the model loaded. "
                            f"Response: {load_result}"
                        )

                    self._log(f"Successfully loaded model: {selected_model}")

                refreshed = self._lm_studio_request(
                    api_root + "/api/v1/models", timeout=30
                )
                refreshed_loaded = self._get_loaded_instances(
                    refreshed.get("models", [])
                )

                self.after(
                    0,
                    self._model_action_finished,
                    True,
                    selected_model,
                    action,
                    load_result,
                    refreshed_loaded,
                    None,
                )

            except Exception as exc:
                error = str(exc)
                refreshed_loaded = None

                try:
                    refreshed = self._lm_studio_request(
                        api_root + "/api/v1/models", timeout=20
                    )
                    refreshed_loaded = self._get_loaded_instances(
                        refreshed.get("models", [])
                    )
                except Exception:
                    pass

                try:
                    self.after(
                        0,
                        self._model_action_finished,
                        False,
                        selected_model,
                        action,
                        load_result,
                        refreshed_loaded,
                        error,
                    )
                except tk.TclError:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _model_action_finished(
        self, success, model_id, action, load_result, refreshed_loaded, error
    ):
        """Update the interface after a load/unload/switch operation."""
        if self._closing:
            return

        self._model_load_in_progress = False
        self._loading_model = None

        self.model_dropdown.configure(state="readonly")
        self.refresh_models_btn.configure(state="normal")

        if refreshed_loaded is not None:
            self.loaded_model_instances = refreshed_loaded

        if self.loaded_model_instances:
            if model_id in self.loaded_model_instances:
                self._active_model_id = model_id
            else:
                self._active_model_id = next(iter(self.loaded_model_instances))

            instances = self.loaded_model_instances.get(self._active_model_id, [])
            self._active_model_instance_id = instances[0] if instances else None
        else:
            self._active_model_id = None
            self._active_model_instance_id = None

        if success:
            if action == "load":
                load_time = (
                    load_result.get("load_time_seconds")
                    if isinstance(load_result, dict)
                    else None
                )
                status = f"AI status: Model loaded and ready: {model_id}"

                if isinstance(load_time, (int, float)):
                    status += f" ({load_time:.1f} seconds)"

                self.ai_status.configure(text=status, text_color=COLOURS["success"])

                instance_id = (
                    load_result.get("instance_id")
                    if isinstance(load_result, dict)
                    else None
                )

                if instance_id:
                    self._log(f"LM Studio instance ID: {instance_id}")

                if isinstance(load_time, (int, float)):
                    self._log(f"Model loading time: {load_time:.1f} seconds")

                self._log(f"Successfully loaded model: {model_id}")

            else:
                self.ai_status.configure(
                    text=f"AI status: Model unloaded: {model_id}",
                    text_color=COLOURS["success"],
                )
                self._log(f"Successfully unloaded model: {model_id}")

            self.save_config(quiet=True)

        else:
            self.ai_status.configure(
                text=f"AI status: Model operation failed — {error}",
                text_color=COLOURS["danger"],
            )
            self._log(f"LM Studio model operation failed: {error}")

        self._update_model_action_button()

    # ============================================================
    # SHARED LOG CONSOLE DESIGN
    # ============================================================

    def _build_log_console(self, tab, title, subtitle, is_ai=False):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        page = ctk.CTkFrame(tab, fg_color="transparent")
        page.grid(row=0, column=0, sticky="nsew", padx=24, pady=24)
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        heading = ctk.CTkFrame(page, fg_color="transparent")
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        heading.grid_columnconfigure(0, weight=1)

        title_area = ctk.CTkFrame(heading, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            title_area,
            text=title,
            text_color=COLOURS["text"],
            font=ctk.CTkFont(size=24, weight="bold"),
        ).pack(anchor="w")

        ctk.CTkLabel(
            title_area,
            text=subtitle,
            text_color=COLOURS["muted"],
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", pady=(4, 0))

        console = self._make_panel(page)
        console.grid(row=1, column=0, sticky="nsew")
        console.grid_columnconfigure(0, weight=1)
        console.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(
            console,
            fg_color=COLOURS["surface_alt"],
            corner_radius=10,
        )
        toolbar.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=14,
            pady=(14, 10),
        )
        toolbar.grid_columnconfigure(1, weight=1)

        indicator_area = ctk.CTkFrame(
            toolbar,
            fg_color="transparent",
        )
        indicator_area.pack(side="left", padx=12, pady=9)

        indicator = ctk.CTkLabel(
            indicator_area,
            text="●",
            text_color=COLOURS["success"],
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        indicator.pack(side="left", padx=(0, 7))

        ctk.CTkLabel(
            indicator_area,
            text="LIVE OUTPUT",
            text_color=COLOURS["text"],
            font=ctk.CTkFont(size=10, weight="bold"),
        ).pack(side="left")

        actions = ctk.CTkFrame(
            toolbar,
            fg_color="transparent",
        )
        actions.pack(side="right", padx=8, pady=5)

        textbox = ctk.CTkTextbox(
            console,
            wrap="word",
            activate_scrollbars=True,
            border_width=1,
            corner_radius=10,
        )
        self._style_textbox(textbox)

        textbox.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=14,
            pady=(0, 10),
        )

        textbox.tag_config("timestamp", foreground=COLOURS["muted"])
        textbox.tag_config("info", foreground=COLOURS["text"])
        textbox.tag_config("success", foreground=COLOURS["success"])
        textbox.tag_config("warning", foreground=COLOURS["warning"])
        textbox.tag_config("error", foreground=COLOURS["danger"])
        textbox.tag_config("ai", foreground=COLOURS["success"])

        if is_ai:
            self.ai_log_textbox = textbox
            clear_command = self.clear_ai_logs
            copy_command = lambda: self._copy_log(self.ai_log_textbox)
        else:
            self.log = textbox
            clear_command = self.clear_logs
            copy_command = lambda: self._copy_log(self.log)

        ctk.CTkButton(
            actions,
            text="Copy",
            width=75,
            height=30,
            fg_color=COLOURS["surface"],
            hover_color=COLOURS["border"],
            text_color=COLOURS["text"],
            command=copy_command,
        ).pack(side="left", padx=3)

        ctk.CTkButton(
            actions,
            text="Clear",
            width=75,
            height=30,
            fg_color=COLOURS["danger"],
            hover_color=COLOURS["danger_hover"],
            command=clear_command,
        ).pack(side="left", padx=3)

        footer = ctk.CTkFrame(
            console,
            fg_color="transparent",
        )
        footer.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=16,
            pady=(0, 12),
        )

        ctk.CTkLabel(
            footer,
            text="YGO3  /  " + ("LM STUDIO" if is_ai else "SERVER"),
            text_color=COLOURS["muted"],
            font=ctk.CTkFont(size=10, weight="bold"),
        ).pack(side="left")

        ctk.CTkLabel(
            footer,
            text="Auto-scroll enabled",
            text_color=COLOURS["muted"],
            font=ctk.CTkFont(size=10),
        ).pack(side="right")

    def _build_logs(self, tab):
        self._build_log_console(
            tab,
            "Server Logs",
            "Monitor server startup, connections, output and errors.",
            is_ai=False,
        )

    def _build_ai_logs(self, tab):
        self._build_log_console(
            tab,
            "AI Logs",
            "NPC decisions and responses captured from LM Studio.",
            is_ai=True,
        )

    def _copy_log(self, textbox):
        try:
            content = textbox.get("1.0", "end-1c")

            if not content.strip():
                return

            self.clipboard_clear()
            self.clipboard_append(content)

        except tk.TclError:
            pass

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
                initialdir=str(Path(current).parent) if current else str(APP_DIR)
            )

        if chosen:
            self.entries[key].delete(0, "end")
            self.entries[key].insert(0, chosen)

    # ============================================================
    # PYTHON CHECKING
    # ============================================================

    @staticmethod
    def _python_info(executable):
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
        exe = self.entries["python_executable"].get().strip()

        if not exe:
            if not quiet:
                messagebox.showwarning(
                    "Python check",
                    "Select or find a 32-bit Python executable first.",
                )
            return False

        try:
            is_32, version, actual_exe = self._python_info(exe)

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
                    "Python is 32-bit, but PyCryptodome is missing "
                    "or cannot be imported.\n\n"
                    "Install it using:\n\n"
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
                    [
                        Path(root) / "Programs" / "Python",
                        Path(root) / "Python",
                    ]
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
        self._log("Searching for installed 32-bit Python interpreters...")

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
                "Find 32-bit again.",
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
    # CONFIGURATION
    # ============================================================

    def _values(self):
        values = {key: entry.get().strip() for key, entry in self.entries.items()}
        values["lm_studio_model"] = self.model_dropdown.get().strip()
        values["user_id"] = self.user_entry.get().strip()
        values["npc_ai_enabled"] = "true" if self.npc_ai_enabled.get() else "false"
        return values

    def save_config(self, quiet=False):
        values = self._values()

        if values.get("lm_studio_model", "").startswith("Click "):
            values["lm_studio_model"] = ""

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
        values = self._values()
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
            ("http_port", "YGO3_HTTP_PORT"),
        ):
            if values.get(key):
                env[env_key] = values[key]

        env["YGO3_NPC_AI_ENABLED"] = (
            "1"
            if values.get("npc_ai_enabled", "true").lower()
            in ("true", "1", "yes", "on")
            else "0"
        )
        env["YGO3_LM_STUDIO_URL"] = values.get(
            "lm_studio_url", "http://localhost:1234/v1"
        ).rstrip("/")
        env["YGO3_LM_STUDIO_MODEL"] = values.get("lm_studio_model", "")
        env["YGO3_NPC_AI_TIMEOUT"] = values.get("npc_ai_timeout", "30")
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
                "No Python interpreter selected. "
                "Click Find 32-bit or browse to python.exe."
            )

        if not self.check_python(quiet=True):
            raise RuntimeError(
                "The selected Python failed its checks. "
                "Open Server Logs for details."
            )

        is_32, version, actual_exe = self._python_info(exe)

        if not is_32:
            raise RuntimeError(
                f"The selected interpreter is 64-bit (Python {version})."
            )

        return actual_exe

    # ============================================================
    # LM STUDIO CONNECTION AND MODEL DISCOVERY
    # ============================================================

    def test_lm_studio(self):
        """Retrieve downloaded language models from LM Studio's native API."""
        if self._model_load_in_progress:
            return

        try:
            api_root = self._lm_studio_api_root()
        except ValueError as exc:
            messagebox.showwarning("LM Studio", str(exc))
            return

        self.ai_status.configure(
            text="AI status: Connecting and retrieving available models...",
            text_color=COLOURS["warning"],
        )
        self.refresh_models_btn.configure(state="disabled", text="Loading...")
        self.model_action_btn.configure(state="disabled")

        def worker():
            try:
                payload = self._lm_studio_request(
                    api_root + "/api/v1/models", timeout=20
                )
                models = payload.get("models", [])

                model_ids = [
                    str(model.get("key", "")).strip()
                    for model in models
                    if isinstance(model, dict)
                    and model.get("type") == "llm"
                    and model.get("key")
                ]
                loaded = self._get_loaded_instances(models)

                if not model_ids:
                    raise RuntimeError(
                        "No downloaded language models were found. "
                        "Download an LLM in LM Studio first."
                    )

                self.after(0, self._lm_studio_test_result, True, model_ids, loaded)

            except Exception as exc:
                try:
                    self.after(0, self._lm_studio_test_result, False, str(exc), None)
                except tk.TclError:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _lm_studio_test_result(self, success, result, loaded):
        if self._closing:
            return

        self.refresh_models_btn.configure(state="normal", text="Refresh Models")

        if success:
            model_ids = result
            self.available_models = model_ids
            self.loaded_model_instances = loaded or {}

            current_model = self.model_dropdown.get().strip()
            saved_model = self.config_values.get("lm_studio_model", "").strip()

            if current_model in model_ids:
                selected_model = current_model
            elif saved_model in model_ids:
                selected_model = saved_model
            else:
                selected_model = model_ids[0]

            self.model_dropdown.configure(values=model_ids)
            self.model_dropdown.set(selected_model)

            if self.loaded_model_instances:
                if selected_model in self.loaded_model_instances:
                    self._active_model_id = selected_model
                else:
                    self._active_model_id = next(iter(self.loaded_model_instances))

                instances = self.loaded_model_instances.get(self._active_model_id, [])
                self._active_model_instance_id = instances[0] if instances else None
            else:
                self._active_model_id = None
                self._active_model_instance_id = None

            self.ai_status.configure(
                text=(
                    "AI status: Connected. "
                    f"{len(model_ids)} model(s) available. "
                    f"Selected: {selected_model}"
                ),
                text_color=COLOURS["success"],
            )

            self._log("LM Studio connection successful.")
            self._log("Available models: " + ", ".join(model_ids))

            if self.loaded_model_instances:
                self._log(
                    "Currently loaded models: "
                    + ", ".join(self.loaded_model_instances.keys())
                )
            else:
                self._log("No language models are currently loaded.")

            self._log(f"Selected model: {selected_model}")

        else:
            self.ai_status.configure(
                text=f"AI status: Connection failed — {result}",
                text_color=COLOURS["danger"],
            )
            self._log(f"LM Studio connection failed: {result}")

        self._update_model_action_button()

    # ============================================================
    # SERVER START / STOP
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
            self._show_tab("Server Logs")
            self._log(f"Could not start server: {exc}")
            messagebox.showerror("Start server", str(exc))
            return

        self.process = proc
        self.status.configure(text="●  RUNNING", text_color=COLOURS["success"])
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._show_tab("Server Logs")
        self._log(f"Starting {script} with 32-bit Python: {python_exe}")

        threading.Thread(target=self._read_output, args=(proc,), daemon=True).start()

    def _read_output(self, proc):
        """
        Send all child-process output to Server Logs.

        Only content between the explicit LLM markers is sent
        to the LM Studio Logs tab.
        """
        capturing = False
        response_lines = []

        try:
            if proc.stdout:
                for line in proc.stdout:
                    stripped = line.rstrip("\r\n")
                    self.log_queue.put(stripped)
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
                                    self.ai_log_queue.put(
                                        content + "\n\n" + "-" * 60 + "\n\n"
                                    )
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
                                self.ai_log_queue.put(
                                    response + "\n\n" + "-" * 60 + "\n\n"
                                )

                            capturing = False
                            response_lines = []
                            remaining = remaining[end_pos + len(LLM_RESPONSE_END) :]

        except (OSError, ValueError) as exc:
            self.log_queue.put(f"Output reader error: {exc}")

        finally:
            if capturing and response_lines:
                response = "\n".join(response_lines).strip()
                if response:
                    self.ai_log_queue.put(response + "\n\n" + "-" * 60 + "\n\n")

            code = proc.wait()

            if not self._closing:
                try:
                    self.after(0, self._server_exited, code, proc)
                except tk.TclError:
                    pass

    def _server_exited(self, code, proc=None):
        if proc is not None and proc is not self.process:
            return

        self.status.configure(
            text=f"●  STOPPED  (exit {code})",
            text_color=COLOURS["danger"],
        )
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._log(f"Server process exited with code {code}.")

    def stop_server(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self._log("Stop requested...")

    # ============================================================
    # BP MANAGEMENT
    # ============================================================

    def add_bp(self):
        try:
            amount = int(self.bp_entry.get().strip())
            user_id = int(self.user_entry.get().strip())

            if amount <= 0:
                raise ValueError("Enter a positive BP amount.")

            if amount > MAX_BP:
                raise ValueError(f"BP amount cannot exceed {MAX_BP:,}.")

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
                    "The server must be stopped before editing its "
                    "saved BP balance. This will disconnect connected "
                    "players. Stop it, grant BP, and restart?",
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

    # ============================================================
    # CARD ANNOTATION HELPERS
    # ============================================================

    def _card_name(self, code):
        """Look up a card name in cards.cdb."""
        cache = self.__dict__.setdefault("_card_name_cache", {})

        if code in cache:
            return cache[code]

        name = None

        try:
            path = str(self._values().get("card_database", "")).strip()

            if path and Path(path).is_file():
                if self.__dict__.get("_card_db_path") != path:
                    old = self.__dict__.get("_card_db")
                    if old is not None:
                        old.close()

                    self._card_db = sqlite3.connect(
                        f"file:{Path(path).as_posix()}?mode=ro",
                        uri=True,
                        check_same_thread=False,
                    )
                    self._card_db_path = path

                row = self._card_db.execute(
                    "SELECT name FROM texts WHERE id = ?", (int(code),)
                ).fetchone()
                name = row[0] if row else None

        except (sqlite3.Error, OSError, ValueError, TypeError):
            name = None

        if name:
            cache[code] = name

        return name

    def _annotate_cards(self, text):
        """Annotate card numbers in log entries."""

        def name_it(match):
            number = match.group(1)
            name = self._card_name(int(number))
            return f"{name} ({number})" if name else number

        lines = []

        for line in text.split("\n"):
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

    # ============================================================
    # SERVER LOGGING
    # ============================================================

    def _log(self, text):
        if threading.current_thread() is not threading.main_thread():
            self.log_queue.put(str(text))
            return

        self._append_styled_log(
            self.log,
            text,
            default_tag="info",
        )

    def clear_logs(self):
        self.log.delete("1.0", "end")

    def _drain_log_queue(self):
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._log(line)

        if not self._closing:
            try:
                self.after(100, self._drain_log_queue)
            except tk.TclError:
                pass

    # ============================================================
    # AI LOGGING
    # ============================================================

    def _log_ai(self, text):
        if threading.current_thread() is not threading.main_thread():
            self.ai_log_queue.put(str(text))
            return

        try:
            text = self._annotate_cards(str(text))
        except Exception:
            text = str(text)

        self._append_styled_log(
            self.ai_log_textbox,
            text,
            default_tag="ai",
        )

    def clear_ai_logs(self):
        self.ai_log_textbox.delete("1.0", "end")

    def _drain_ai_log_queue(self):
        while True:
            try:
                line = self.ai_log_queue.get_nowait()
            except queue.Empty:
                break
            self._log_ai(line)

        if not self._closing:
            try:
                self.after(100, self._drain_ai_log_queue)
            except tk.TclError:
                pass

    # ============================================================
    # SHUTDOWN
    # ============================================================

    def _close(self):
        if self._card_gen_running:
            if not messagebox.askyesno(
                "Download in progress",
                "Card IDs are currently being downloaded from YGOPRODeck.\n\n"
                "Are you sure you want to close the dashboard anyway?",
            ):
                return

        self._closing = True

        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

        try:
            card_db = self.__dict__.get("_card_db")
            if card_db is not None:
                card_db.close()
        except sqlite3.Error:
            pass

        self.destroy()


if __name__ == "__main__":
    app = Launcher()
    app.mainloop()
