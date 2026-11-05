
"""
YO3 Client Explorer v1.0

Read-only static analysis for the yo3.exe client.

Features:
- File hashes and entropy analysis
- ASCII and UTF-16LE string extraction
- Searchable, sortable strings table
- Network address and marker scanning
- Portable Executable metadata analysis
- JSON and TXT report export

"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import threading
from collections import Counter
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk


APP_NAME = "YO3 Client Explorer"
VERSION = "1.0"


NETWORK_MARKERS = [
    b"connect",
    b"send",
    b"recv",
    b"socket",
    b"WSAStartup",
    b"connectex",
    b"sendto",
    b"recvfrom",
    b"InternetOpen",
    b"InternetConnect",
    b"WinHttp",
    b"gethostbyname",
    b"getaddrinfo",
    b"packet",
    b"encrypt",
    b"decrypt",
    b"server",
    b"login",
    b"network",
]


URL_RE = re.compile(
    rb"https?://[a-zA-Z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    re.I,
)

IP_RE = re.compile(
    rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"
)

DOMAIN_RE = re.compile(
    rb"(?i)(?<![a-z0-9-])"
    rb"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    rb"[a-z]{2,}(?![a-z0-9-])"
)

PATH_RE = re.compile(
    rb"[A-Za-z]:\\[^\x00\r\n]{2,180}"
)


def entropy(data: bytes) -> float:
    """Calculate Shannon entropy for a byte sequence."""
    if not data:
        return 0.0

    counts = Counter(data)
    length = len(data)

    return -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )


def extract_strings(data: bytes, minimum: int = 4) -> list[dict]:
    """Extract printable ASCII and UTF-16LE strings."""
    found = []

    ascii_re = re.compile(rb"[\x20-\x7e]{%d,}" % minimum)
    utf16_re = re.compile(rb"(?:[\x20-\x7e]\x00){%d,}" % minimum)

    for match in ascii_re.finditer(data):
        found.append({
            "offset": match.start(),
            "encoding": "ASCII",
            "text": match.group().decode("ascii", errors="replace"),
        })

    for match in utf16_re.finditer(data):
        found.append({
            "offset": match.start(),
            "encoding": "UTF-16LE",
            "text": match.group().decode("utf-16le", errors="replace"),
        })

    return sorted(found, key=lambda item: item["offset"])


def valid_ip(raw: bytes) -> bool:
    """Check whether a byte string contains a valid IPv4 address."""
    try:
        parts = raw.decode("ascii").split(".")

        return (
            len(parts) == 4
            and all(
                part.isdigit() and 0 <= int(part) <= 255
                for part in parts
            )
        )

    except (ValueError, UnicodeDecodeError):
        return False


def scan_addresses(data: bytes) -> dict:
    """Find URLs, IPv4 addresses, domains and Windows paths."""
    urls = sorted({
        match.decode("ascii", errors="replace")
        for match in URL_RE.findall(data)
    })

    ips = sorted({
        match.decode("ascii")
        for match in IP_RE.findall(data)
        if valid_ip(match)
    })

    domains = sorted({
        match.decode("ascii", errors="replace").lower()
        for match in DOMAIN_RE.findall(data)
    })

    paths = sorted({
        match.decode("ascii", errors="replace")
        for match in PATH_RE.findall(data)
    })

    return {
        "urls": urls,
        "ips": ips,
        "domains": domains,
        "windows_paths": paths,
    }


def scan_markers(data: bytes) -> list[dict]:
    """Find occurrences of common networking-related strings."""
    lower = data.lower()
    found = []

    for marker in NETWORK_MARKERS:
        start = 0
        needle = marker.lower()

        while True:
            pos = lower.find(needle, start)

            if pos < 0:
                break

            found.append({
                "marker": marker.decode("ascii"),
                "offset": pos,
            })

            start = pos + 1

    return sorted(found, key=lambda item: item["offset"])


def analyse_pe(path: str) -> dict:
    """Extract Portable Executable metadata using optional pefile."""
    try:
        import pefile
    except ImportError:
        return {
            "available": False,
            "message": (
                "Optional dependency missing: "
                "python -m pip install pefile"
            ),
        }

    try:
        pe = pefile.PE(path, fast_load=False)

        try:
            sections = []

            for sec in pe.sections:
                sections.append({
                    "name": sec.Name.rstrip(b"\0").decode(
                        "ascii", errors="replace"
                    ),
                    "virtual_address": hex(sec.VirtualAddress),
                    "virtual_size": sec.Misc_VirtualSize,
                    "raw_offset": sec.PointerToRawData,
                    "raw_size": sec.SizeOfRawData,
                    "entropy": round(sec.get_entropy(), 4),
                    "characteristics": hex(sec.Characteristics),
                })

            imports = []

            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
                dll = entry.dll.decode(errors="replace")

                for imp in entry.imports:
                    name = (
                        imp.name.decode(errors="replace")
                        if imp.name
                        else f"Ordinal {imp.ordinal}"
                    )

                    imports.append(f"{dll}!{name}")

            exports = []

            export_dir = getattr(
                pe, "DIRECTORY_ENTRY_EXPORT", None
            )

            if export_dir:
                for sym in export_dir.symbols:
                    exports.append(
                        sym.name.decode(errors="replace")
                        if sym.name
                        else f"Ordinal {sym.ordinal}"
                    )

            resources = []

            resource_dir = getattr(
                pe, "DIRECTORY_ENTRY_RESOURCE", None
            )

            def walk(directory, prefix=""):
                for entry in directory.entries:
                    label = (
                        str(entry.name)
                        if entry.name is not None
                        else str(entry.id)
                    )

                    current = prefix + "/" + label

                    if hasattr(entry, "directory"):
                        walk(entry.directory, current)

                    elif hasattr(entry, "data"):
                        resources.append({
                            "name": current,
                            "size": entry.data.struct.Size,
                            "rva": hex(
                                entry.data.struct.OffsetToData
                            ),
                        })

            if resource_dir:
                walk(resource_dir)

            return {
                "available": True,
                "machine": hex(pe.FILE_HEADER.Machine),
                "timestamp": pe.FILE_HEADER.TimeDateStamp,
                "subsystem": pe.OPTIONAL_HEADER.Subsystem,
                "entry_point": hex(
                    pe.OPTIONAL_HEADER.AddressOfEntryPoint
                ),
                "image_base": hex(
                    pe.OPTIONAL_HEADER.ImageBase
                ),
                "sections": sections,
                "imports": imports,
                "exports": exports,
                "resources": resources,
            }

        finally:
            pe.close()

    except Exception as exc:
        return {
            "available": False,
            "message": str(exc),
        }


def analyse_file(path: str):
    """Read a file and generate the complete analysis report."""
    with open(path, "rb") as file:
        data = file.read()

    report = {
        "file": os.path.abspath(path),
        "filename": os.path.basename(path),
        "size": len(data),
        "modified": datetime.fromtimestamp(
            os.path.getmtime(path)
        ).isoformat(),
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "entropy": round(entropy(data), 5),
        "strings": extract_strings(data),
        "addresses": scan_addresses(data),
        "network_markers": scan_markers(data),
        "pe": analyse_pe(path),
    }

    return report, data


class YO3Explorer:

    def __init__(self, root: ctk.CTk):
        self.root = root

        self.root.title(f"{APP_NAME} v{VERSION}")
        self.root.geometry("1320x850")
        self.root.minsize(950, 650)

        self.report = None
        self.raw_data = b""
        self.current_path = None

        self.result_queue = queue.Queue()

        self.string_sort_column = "offset"
        self.string_sort_reverse = False

        self._build_ui()
        self.root.after(100, self._poll_worker)

    def _build_ui(self):
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(
            self.root,
            corner_radius=0,
        )

        toolbar.grid(
            row=0,
            column=0,
            sticky="ew",
        )

        toolbar.grid_columnconfigure(0, weight=1)

        brand = ctk.CTkLabel(
            toolbar,
            text="YO3  /  CLIENT EXPLORER",
            font=ctk.CTkFont(
                size=18,
                weight="bold",
            ),
        )

        brand.grid(
            row=0,
            column=0,
            padx=18,
            pady=14,
            sticky="w",
        )

        actions = ctk.CTkFrame(
            toolbar,
            fg_color="transparent",
        )

        actions.grid(
            row=0,
            column=1,
            padx=12,
            pady=8,
            sticky="e",
        )

        ctk.CTkButton(
            actions,
            text="Open Client File",
            command=self.open_file,
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            actions,
            text="Export JSON",
            command=self.export_json,
            fg_color="transparent",
            border_width=1,
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            actions,
            text="Export TXT",
            command=self.export_txt,
            fg_color="transparent",
            border_width=1,
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            actions,
            text="About",
            command=self.about,
            fg_color="transparent",
            border_width=1,
        ).pack(side="left", padx=4)

        self.tabs = ctk.CTkTabview(self.root)

        self.tabs.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=10,
            pady=10,
        )

        self.tab_widgets = {}

        for name in (
            "Overview",
            "Strings",
            "Network",
            "Portable Executable Analysis",
        ):
            self.tab_widgets[name] = self.tabs.add(name)

        self.summary_text = self._make_text(
            self.tab_widgets["Overview"]
        )

        self._build_strings_view(
            self.tab_widgets["Strings"]
        )

        self.network_text = self._make_text(
            self.tab_widgets["Network"]
        )

        self.pe_text = self._make_text(
            self.tab_widgets["Portable Executable Analysis"]
        )

        statusbar = ctk.CTkFrame(
            self.root,
            corner_radius=0,
        )

        statusbar.grid(
            row=2,
            column=0,
            sticky="ew",
        )

        self.status = tk.StringVar(
            value="Ready | Open a client executable, DLL, or data file"
        )

        ctk.CTkLabel(
            statusbar,
            textvariable=self.status,
            anchor="w",
            text_color=("gray35", "gray75"),
        ).pack(
            fill="x",
            padx=12,
            pady=7,
        )

    def _make_text(self, parent):
        widget = ctk.CTkTextbox(
            parent,
            wrap="none",
            font=("Consolas", 12),
        )

        widget.pack(
            fill="both",
            expand=True,
            padx=8,
            pady=8,
        )

        widget.configure(state="disabled")

        return widget

    @staticmethod
    def _set_text(widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    # ---------------------------------------------------------
    # STRINGS TAB
    # ---------------------------------------------------------

    def _build_strings_view(self, parent):
        """Build the searchable, sortable strings table."""

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(
            parent,
            fg_color="transparent",
        )

        toolbar.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=8,
            pady=(8, 4),
        )

        toolbar.grid_columnconfigure(0, weight=1)

        self.string_search_var = tk.StringVar()

        self.string_search_var.trace_add(
            "write",
            self._filter_strings,
        )

        self.string_search = ctk.CTkEntry(
            toolbar,
            textvariable=self.string_search_var,
            placeholder_text="Search strings, offsets or encoding...",
            height=34,
        )

        self.string_search.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=(0, 10),
        )

        self.string_count_var = tk.StringVar(
            value="0 strings"
        )

        ctk.CTkLabel(
            toolbar,
            textvariable=self.string_count_var,
            anchor="e",
        ).grid(
            row=0,
            column=1,
            padx=8,
        )

        ctk.CTkButton(
            toolbar,
            text="Clear",
            width=75,
            command=lambda: self.string_search_var.set(""),
            fg_color="transparent",
            border_width=1,
        ).grid(
            row=0,
            column=2,
            padx=(4, 0),
        )

        table_frame = ctk.CTkFrame(parent)

        table_frame.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=8,
            pady=(4, 8),
        )

        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        style = ttk.Style()

        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "YO3.Treeview",
            background="#202020",
            foreground="#eeeeee",
            fieldbackground="#202020",
            rowheight=27,
            borderwidth=0,
            font=("Consolas", 10),
        )

        style.configure(
            "YO3.Treeview.Heading",
            background="#303030",
            foreground="#ffffff",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
        )

        style.map(
            "YO3.Treeview",
            background=[("selected", "#245a91")],
            foreground=[("selected", "#ffffff")],
        )

        self.strings_tree = ttk.Treeview(
            table_frame,
            columns=("offset", "encoding", "text"),
            show="headings",
            style="YO3.Treeview",
            selectmode="browse",
        )

        self.strings_tree.heading(
            "offset",
            text="Offset",
            command=lambda: self._sort_strings("offset"),
        )

        self.strings_tree.heading(
            "encoding",
            text="Type",
            command=lambda: self._sort_strings("encoding"),
        )

        self.strings_tree.heading(
            "text",
            text="Extracted String",
            command=lambda: self._sort_strings("text"),
        )

        self.strings_tree.column(
            "offset",
            width=115,
            minwidth=100,
            stretch=False,
            anchor="w",
        )

        self.strings_tree.column(
            "encoding",
            width=110,
            minwidth=95,
            stretch=False,
            anchor="w",
        )

        self.strings_tree.column(
            "text",
            width=850,
            minwidth=250,
            stretch=True,
            anchor="w",
        )

        vertical_scroll = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.strings_tree.yview,
        )

        horizontal_scroll = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=self.strings_tree.xview,
        )

        self.strings_tree.configure(
            yscrollcommand=vertical_scroll.set,
            xscrollcommand=horizontal_scroll.set,
        )

        self.strings_tree.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        vertical_scroll.grid(
            row=0,
            column=1,
            sticky="ns",
        )

        horizontal_scroll.grid(
            row=1,
            column=0,
            sticky="ew",
        )

        self.strings_tree.bind(
            "<Double-1>",
            self._copy_selected_string,
        )

        self.strings_tree.bind(
            "<Control-c>",
            self._copy_selected_string,
        )

        self.strings_tree.bind(
            "<Control-C>",
            self._copy_selected_string,
        )

        self.all_strings = []

    def _populate_strings(self):
        """Load all extracted strings into the table."""
        self.all_strings = self.report["strings"]

        self.string_search_var.set("")
        self._refresh_strings_table()

    def _filter_strings(self, *_):
        """Filter table rows using the search box."""
        self._refresh_strings_table()

    def _refresh_strings_table(self):
        """Refresh visible rows using the current filter."""
        if not hasattr(self, "strings_tree"):
            return

        query = self.string_search_var.get().strip().lower()

        for item in self.strings_tree.get_children():
            self.strings_tree.delete(item)

        visible = 0

        for index, item in enumerate(self.all_strings):
            offset = f"{item['offset']:08X}"
            encoding = item["encoding"]
            text = item["text"]

            searchable = (
                f"{offset} {encoding} {text}"
            ).lower()

            if query and query not in searchable:
                continue

            self.strings_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    offset,
                    encoding,
                    text,
                ),
            )

            visible += 1

        self.string_count_var.set(
            f"{visible:,} of {len(self.all_strings):,} strings"
        )

    def _sort_strings(self, column):
        """Sort strings by the selected column."""
        if self.string_sort_column == column:
            self.string_sort_reverse = not self.string_sort_reverse
        else:
            self.string_sort_column = column
            self.string_sort_reverse = False

        if column == "offset":
            key_function = lambda item: item["offset"]
        elif column == "encoding":
            key_function = lambda item: item["encoding"].lower()
        else:
            key_function = lambda item: item["text"].lower()

        self.all_strings.sort(
            key=key_function,
            reverse=self.string_sort_reverse,
        )

        self._refresh_strings_table()

    def _copy_selected_string(self, event=None):
        """Copy the selected extracted string to the clipboard."""
        selected = self.strings_tree.selection()

        if not selected:
            return

        values = self.strings_tree.item(
            selected[0],
            "values",
        )

        if len(values) < 3:
            return

        extracted_text = values[2]

        self.root.clipboard_clear()
        self.root.clipboard_append(extracted_text)

        self.status.set("Extracted string copied to clipboard.")

        return "break"

    def _strings_export_text(self):
        """Return all strings in a readable text format."""
        lines = [
            f"{'Offset':<12} {'Type':<10} Extracted String",
            "-" * 100,
        ]

        for item in self.all_strings:
            lines.append(
                f"{item['offset']:08X}  "
                f"{item['encoding']:<10} "
                f"{item['text']}"
            )

        return "\n".join(lines) if self.all_strings else "No strings found."

    # ---------------------------------------------------------
    # FILE LOADING
    # ---------------------------------------------------------

    def open_file(self):
        path = filedialog.askopenfilename(
            title="Select YO3 client file",
            filetypes=[
                (
                    "Binaries",
                    "*.exe *.dll *.ocx *.sys",
                ),
                ("All files", "*.*"),
            ],
        )

        if not path:
            return

        self.status.set("Analysing file in background...")
        self.root.configure(cursor="watch")

        threading.Thread(
            target=self._analyse_worker,
            args=(path,),
            daemon=True,
        ).start()

    def _analyse_worker(self, path):
        try:
            report, data = analyse_file(path)

            self.result_queue.put(
                ("success", path, report, data)
            )

        except Exception as exc:
            self.result_queue.put(
                ("error", str(exc))
            )

    def _poll_worker(self):
        try:
            item = self.result_queue.get_nowait()

        except queue.Empty:
            self.root.after(
                100,
                self._poll_worker,
            )
            return

        self.root.configure(cursor="")

        if item[0] == "error":
            self.status.set("Analysis failed.")

            messagebox.showerror(
                "Analysis error",
                item[1],
            )

        else:
            _, path, self.report, self.raw_data = item

            self.current_path = path

            self.populate()

            self.status.set(
                f"Loaded {os.path.basename(path)} "
                f"({len(self.raw_data):,} bytes)"
            )

        self.root.after(
            100,
            self._poll_worker,
        )

    # ---------------------------------------------------------
    # POPULATE ANALYSIS TABS
    # ---------------------------------------------------------

    def populate(self):
        r = self.report

        summary = [
            f"{APP_NAME.upper()} v{VERSION}",
            "=" * 75,
            "",
            f"File: {r['file']}",
            f"Size: {r['size']:,} bytes",
            f"Modified: {r['modified']}",
            "",
            "FILE HASHES",
            "-" * 75,
            f"MD5:    {r['md5']}",
            f"SHA1:   {r['sha1']}",
            f"SHA256: {r['sha256']}",
            "",
            "FILE ANALYSIS",
            "-" * 75,
            f"Entropy: {r['entropy']} bits per byte",
            f"Extracted strings: {len(r['strings']):,}",
            f"Network marker hits: {len(r['network_markers']):,}",
            (
                "Portable Executable metadata available: "
                f"{r['pe']['available']}"
            ),
            "",
            (
                "High entropy can indicate compression or encryption; "
                "it is not proof."
            ),
            (
                "Portable Executable metadata is available for "
                "supported Windows executable files."
            ),
        ]

        self._set_text(
            self.summary_text,
            "\n".join(summary),
        )

        self._populate_strings()

        addresses = r["addresses"]
        lines = []

        for label, key in (
            ("URLS", "urls"),
            ("IP ADDRESSES", "ips"),
            ("DOMAINS", "domains"),
            ("WINDOWS PATHS", "windows_paths"),
        ):
            lines += [
                label,
                "=" * 65,
            ] + addresses[key] + [""]

        lines += [
            "NETWORK MARKERS",
            "=" * 65,
        ]

        lines += [
            f"{item['offset']:08X}  {item['marker']}"
            for item in r["network_markers"]
        ]

        self._set_text(
            self.network_text,
            "\n".join(lines),
        )

        pe = r["pe"]

        lines = [
            "PORTABLE EXECUTABLE ANALYSIS",
            "=" * 75,
        ]

        if not pe["available"]:
            lines.append(
                pe.get(
                    "message",
                    "Portable Executable analysis unavailable.",
                )
            )

        else:
            for key in (
                "machine",
                "timestamp",
                "subsystem",
                "entry_point",
                "image_base",
            ):
                lines.append(
                    f"{key}: {pe[key]}"
                )

            lines += [
                "",
                "SECTIONS",
                "-" * 75,
            ]

            for section in pe["sections"]:
                lines.append(
                    f"{section['name']} | "
                    f"RAW={section['raw_offset']:#x} | "
                    f"RVA={section['virtual_address']} | "
                    f"raw={section['raw_size']} | "
                    f"virtual={section['virtual_size']} | "
                    f"entropy={section['entropy']}"
                )

            lines += [
                "",
                "IMPORTS",
                "-" * 75,
            ] + pe["imports"]

            lines += [
                "",
                "EXPORTS",
                "-" * 75,
            ] + pe["exports"]

            lines += [
                "",
                "RESOURCES",
                "-" * 75,
            ]

            lines += [
                (
                    f"{resource['name']} | "
                    f"{resource['size']} bytes | "
                    f"RVA {resource['rva']}"
                )
                for resource in pe["resources"]
            ]

        self._set_text(
            self.pe_text,
            "\n".join(lines),
        )

    # ---------------------------------------------------------
    # EXPORT
    # ---------------------------------------------------------

    def export_json(self):
        if not self.report:
            messagebox.showinfo(
                "Export",
                "Analyse a file first.",
            )
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[
                ("JSON", "*.json"),
            ],
        )

        if not path:
            return

        try:
            with open(
                path,
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    self.report,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )

            messagebox.showinfo(
                "Export",
                "JSON report saved.",
            )

        except Exception as exc:
            messagebox.showerror(
                "Export error",
                str(exc),
            )

    def export_txt(self):
        if not self.report:
            messagebox.showinfo(
                "Export",
                "Analyse a file first.",
            )
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[
                ("Text", "*.txt"),
            ],
        )

        if not path:
            return

        sections = [
            ("OVERVIEW", self.summary_text.get("1.0", "end-1c")),
            ("STRINGS", self._strings_export_text()),
            ("NETWORK", self.network_text.get("1.0", "end-1c")),
            (
                "PORTABLE EXECUTABLE ANALYSIS",
                self.pe_text.get("1.0", "end-1c"),
            ),
        ]

        try:
            with open(
                path,
                "w",
                encoding="utf-8",
            ) as file:
                for name, content in sections:
                    file.write(
                        f"\n{'=' * 75}\n{name}\n"
                    )

                    file.write(content + "\n")

            messagebox.showinfo(
                "Export",
                "Text report saved.",
            )

        except Exception as exc:
            messagebox.showerror(
                "Export error",
                str(exc),
            )

    # ---------------------------------------------------------
    # ABOUT
    # ---------------------------------------------------------

    def about(self):
        messagebox.showinfo(
            APP_NAME,
            f"{APP_NAME} v{VERSION}\n\n"
            "CustomTkinter interface for offline, "
            "read-only static analysis.\n\n"
            "Features: file hashes, entropy, ASCII/UTF-16LE "
            "strings, searchable strings table, address and "
            "network-marker scans, Portable Executable metadata, "
            "JSON/TXT export.\n\n"
            "This tool does not modify files or connect to "
            "game servers.",
        )


if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()

    app = YO3Explorer(root)

    root.mainloop()
