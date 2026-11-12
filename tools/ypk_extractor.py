
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path, PurePosixPath
import struct
import zlib
import binascii
import threading


# ============================================================
# CONSTANTS AND THEME
# ============================================================

SIGNATURE = b"YO\x03\x04"

# Modern dark colour palette.
COLORS = {
    "background": "#111318",
    "panel": "#1B1F27",
    "panel_light": "#242A35",
    "border": "#303744",
    "accent": "#6C8CFF",
    "accent_hover": "#829DFF",
    "success": "#55D69E",
    "warning": "#F2BD62",
    "error": "#FF6B78",
    "text": "#E8ECF4",
    "muted": "#929BAA",
    "log_bg": "#141820",
}


class YPKExtractor:

    def __init__(self, root):

        self.root = root

        # Configure the main application window.
        self.root.title("YPK Extractor")
        self.root.geometry("850x650")
        self.root.minsize(650, 520)
        self.root.configure(bg=COLORS["background"])

        # Application state.
        self.archives = []
        self.output_dir = None
        self.is_extracting = False

        # Configure ttk styles before creating widgets.
        self.configure_styles()

        # Build the interface.
        self.create_gui()

    # ========================================================
    # THEME CONFIGURATION
    # ========================================================

    def configure_styles(self):

        """Configure ttk widgets to use a modern dark appearance."""

        style = ttk.Style()

        # The clam theme allows more colour customisation.
        style.theme_use("clam")

        # General frame styling.
        style.configure(
            "TFrame",
            background=COLORS["background"]
        )

        # Panel/card styling.
        style.configure(
            "Card.TFrame",
            background=COLORS["panel"]
        )

        # General label styling.
        style.configure(
            "TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Segoe UI", 10)
        )

        # Header styling.
        style.configure(
            "Title.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Segoe UI", 21, "bold")
        )

        style.configure(
            "Subtitle.TLabel",
            background=COLORS["background"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 10)
        )

        # Card title styling.
        style.configure(
            "CardTitle.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=("Segoe UI", 11, "bold")
        )

        # Information labels.
        style.configure(
            "Info.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 9)
        )

        # Primary action button.
        style.configure(
            "Accent.TButton",
            background=COLORS["accent"],
            foreground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            padding=(14, 11),
            borderwidth=0
        )

        style.map(
            "Accent.TButton",
            background=[
                ("disabled", "#414B65"),
                ("active", COLORS["accent_hover"]),
                ("pressed", COLORS["accent_hover"])
            ],
            foreground=[
                ("disabled", "#A0A8BA"),
                ("!disabled", "#FFFFFF")
            ]
        )

        # Secondary buttons.
        style.configure(
            "Secondary.TButton",
            background=COLORS["panel_light"],
            foreground=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            padding=(12, 9),
            borderwidth=0
        )

        style.map(
            "Secondary.TButton",
            background=[
                ("active", "#343D4C"),
                ("pressed", "#343D4C")
            ]
        )

        # Clear log button.
        style.configure(
            "Clear.TButton",
            background="#3A252C",
            foreground=COLORS["error"],
            font=("Segoe UI", 9, "bold"),
            padding=(10, 7),
            borderwidth=0
        )

        style.map(
            "Clear.TButton",
            background=[
                ("active", "#51313A"),
                ("pressed", "#51313A")
            ]
        )

        # Progress bar styling.
        style.configure(
            "Horizontal.TProgressbar",
            background=COLORS["accent"],
            troughcolor=COLORS["panel_light"],
            bordercolor=COLORS["panel"],
            lightcolor=COLORS["accent"],
            darkcolor=COLORS["accent"],
            thickness=10
        )

    # ========================================================
    # GUI CONSTRUCTION
    # ========================================================

    def create_gui(self):

        """Create and arrange all application interface elements."""

        main = ttk.Frame(self.root, padding=22)
        main.pack(fill="both", expand=True)

        # ----------------------------------------------------
        # HEADER
        # ----------------------------------------------------

        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, 20))

        ttk.Label(
            header,
            text="YPK Extractor",
            style="Title.TLabel"
        ).pack(anchor="w")

        ttk.Label(
            header,
            text="Extract and manage your archive files.",
            style="Subtitle.TLabel"
        ).pack(anchor="w", pady=(4, 0))

        # ----------------------------------------------------
        # ARCHIVE SELECTION CARD
        # ----------------------------------------------------

        archive_card = ttk.Frame(
            main,
            style="Card.TFrame",
            padding=15
        )
        archive_card.pack(fill="x", pady=(0, 12))

        ttk.Label(
            archive_card,
            text="01  |  Source Archives",
            style="CardTitle.TLabel"
        ).pack(anchor="w", pady=(0, 10))

        archive_row = ttk.Frame(
            archive_card,
            style="Card.TFrame"
        )
        archive_row.pack(fill="x")

        ttk.Button(
            archive_row,
            text="Browse Archives",
            style="Secondary.TButton",
            command=self.select_archives
        ).pack(side="left")

        self.archive_label = ttk.Label(
            archive_row,
            text="No archives selected",
            style="Info.TLabel"
        )
        self.archive_label.pack(
            side="left",
            padx=12,
            fill="x",
            expand=True
        )

        # ----------------------------------------------------
        # OUTPUT FOLDER CARD
        # ----------------------------------------------------

        output_card = ttk.Frame(
            main,
            style="Card.TFrame",
            padding=15
        )
        output_card.pack(fill="x", pady=(0, 12))

        ttk.Label(
            output_card,
            text="02  |  Extraction Destination",
            style="CardTitle.TLabel"
        ).pack(anchor="w", pady=(0, 10))

        output_row = ttk.Frame(
            output_card,
            style="Card.TFrame"
        )
        output_row.pack(fill="x")

        ttk.Button(
            output_row,
            text="Choose Folder",
            style="Secondary.TButton",
            command=self.select_output
        ).pack(side="left")

        self.output_label = ttk.Label(
            output_row,
            text="No output folder selected",
            style="Info.TLabel"
        )
        self.output_label.pack(
            side="left",
            padx=12,
            fill="x",
            expand=True
        )

        # ----------------------------------------------------
        # EXTRACTION CONTROLS
        # ----------------------------------------------------

        controls = ttk.Frame(main)
        controls.pack(fill="x", pady=(5, 12))

        self.extract_button = ttk.Button(
            controls,
            text="▶   Extract Archives",
            style="Accent.TButton",
            command=self.start_extraction
        )
        self.extract_button.pack(
            side="left",
            fill="x",
            expand=True
        )

        # Progress bar showing the number of completed archives.
        self.progress = ttk.Progressbar(
            main,
            mode="determinate",
            style="Horizontal.TProgressbar"
        )
        self.progress.pack(fill="x", pady=(0, 7))

        # Status text underneath the progress bar.
        self.status_label = ttk.Label(
            main,
            text="Ready",
            style="Subtitle.TLabel"
        )
        self.status_label.pack(anchor="w", pady=(0, 12))

        # ----------------------------------------------------
        # LOG SECTION
        # ----------------------------------------------------

        log_header = ttk.Frame(main)
        log_header.pack(fill="x", pady=(0, 7))

        ttk.Label(
            log_header,
            text="Extraction Log",
            font=("Segoe UI", 11, "bold")
        ).pack(side="left")

        # Clear Log button.
        self.clear_log_button = ttk.Button(
            log_header,
            text="✕  Clear Log",
            style="Clear.TButton",
            command=self.clear_log
        )
        self.clear_log_button.pack(side="right")

        # Container for the log and its scrollbar.
        log_container = tk.Frame(
            main,
            bg=COLORS["log_bg"],
            highlightbackground=COLORS["border"],
            highlightthickness=1
        )
        log_container.pack(fill="both", expand=True)

        # Scrollbar for navigating longer extraction logs.
        scrollbar = ttk.Scrollbar(
            log_container,
            orient="vertical"
        )
        scrollbar.pack(side="right", fill="y")

        # Main log text widget.
        self.log_box = tk.Text(
            log_container,
            height=12,
            bg=COLORS["log_bg"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            selectbackground=COLORS["accent"],
            font=("Consolas", 9),
            wrap="word",
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=10,
            state="disabled",
            yscrollcommand=scrollbar.set
        )
        self.log_box.pack(
            side="left",
            fill="both",
            expand=True
        )

        scrollbar.config(command=self.log_box.yview)

        # ----------------------------------------------------
        # LOG MESSAGE COLOURS
        # ----------------------------------------------------

        self.log_box.tag_configure(
            "success",
            foreground=COLORS["success"]
        )

        self.log_box.tag_configure(
            "warning",
            foreground=COLORS["warning"]
        )

        self.log_box.tag_configure(
            "error",
            foreground=COLORS["error"]
        )

        self.log_box.tag_configure(
            "normal",
            foreground=COLORS["text"]
        )

        # Initial application message.
        self.log("YPK Extractor initialised.", "success")
        self.log("Select archives and an output folder to begin.")

    # ========================================================
    # FILE SELECTION
    # ========================================================

    def select_archives(self):

        """Allow the user to select one or more YPK archives."""

        files = filedialog.askopenfilenames(
            title="Select YPK Archives",
            filetypes=[
                ("YPK Archives", "*.ypk"),
                ("All Files", "*.*")
            ]
        )

        if files:

            self.archives = [
                Path(f) for f in files
            ]

            self.archive_label.config(
                text=f"{len(self.archives)} archive(s) selected"
            )

            self.log(
                f"Selected {len(self.archives)} archive(s).",
                "success"
            )

    def select_output(self):

        """Choose the directory where extracted files will be saved."""

        folder = filedialog.askdirectory(
            title="Choose Extraction Destination"
        )

        if folder:

            self.output_dir = Path(folder)

            self.output_label.config(
                text=str(self.output_dir)
            )

            self.log(
                f"Output directory: {self.output_dir}",
                "success"
            )

    # ========================================================
    # LOGGING
    # ========================================================

    def log(self, message, level="normal"):

        """
        Add a message to the extraction log.

        Tkinter widgets must only be modified from the main thread.
        root.after() safely schedules the update when extraction
        is running in a background thread.
        """

        def update():

            self.log_box.config(state="normal")

            self.log_box.insert(
                "end",
                message + "\n",
                level
            )

            # Automatically scroll to the newest message.
            self.log_box.see("end")

            self.log_box.config(state="disabled")

        self.root.after(0, update)

    def clear_log(self):

        """Clear all messages from the extraction log."""

        # Keep the log widget disabled outside this operation.
        self.log_box.config(state="normal")

        self.log_box.delete("1.0", "end")

        self.log_box.config(state="disabled")

        # Clearing the log does not cancel extraction.
        self.status_label.config(
            text="Log cleared."
        )

    # ========================================================
    # ARCHIVE EXTRACTION
    # ========================================================

    def extract_archive(self, archive_path):

        """Read and extract the entries inside a YPK archive."""

        self.log(
            f"Opening: {archive_path.name}",
            "normal"
        )

        # Load the complete archive into memory.
        data = archive_path.read_bytes()

        # Create a separate output folder for each archive.
        archive_output = (
            self.output_dir / archive_path.stem
        )

        archive_output.mkdir(
            parents=True,
            exist_ok=True
        )

        pos = 0
        extracted = 0
        skipped = 0

        # Continue while enough bytes remain for an entry header.
        while pos + 30 <= len(data):

            # Check for the custom YPK entry signature.
            if data[pos:pos + 4] != SIGNATURE:
                break

            # Read the compression method and file metadata.
            method = struct.unpack_from(
                "<H", data, pos + 8
            )[0]

            crc = struct.unpack_from(
                "<I", data, pos + 14
            )[0]

            compressed = struct.unpack_from(
                "<I", data, pos + 18
            )[0]

            uncompressed = struct.unpack_from(
                "<I", data, pos + 22
            )[0]

            name_len = struct.unpack_from(
                "<H", data, pos + 26
            )[0]

            extra_len = struct.unpack_from(
                "<H", data, pos + 28
            )[0]

            # Calculate the filename and compressed payload offsets.
            start = pos + 30
            name_end = start + name_len
            payload_start = name_end + extra_len
            payload_end = payload_start + compressed

            # Prevent reading outside the archive boundary.
            if payload_end > len(data):
                raise RuntimeError(
                    "Archive entry extends beyond file boundary"
                )

            # Decode the filename and retrieve its compressed data.
            name = data[start:name_end].decode(
                "utf-8", "replace"
            )

            payload = data[payload_start:payload_end]

            # Advance to the next entry.
            pos = payload_end

            # ------------------------------------------------
            # PATH VALIDATION
            # ------------------------------------------------

            # Normalise Windows-style separators.
            normalized_name = name.replace("\\", "/")
            relative_path = PurePosixPath(normalized_name)

            # Reject absolute paths, parent traversal, and
            # Windows drive-style paths.
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or (
                    relative_path.parts
                    and relative_path.parts[0].endswith(":")
                )
            ):

                self.log(
                    f"Skipped unsafe path: {name}",
                    "warning"
                )

                skipped += 1
                continue

            # Directory entries do not contain file data.
            if name.endswith("/"):

                (archive_output / Path(*relative_path.parts)).mkdir(
                    parents=True,
                    exist_ok=True
                )

                continue

            # ------------------------------------------------
            # DECOMPRESSION
            # ------------------------------------------------

            try:

                # Method 0: Stored without compression.
                if method == 0:
                    result = payload

                # Method 8: Raw DEFLATE compression.
                elif method == 8:
                    result = zlib.decompress(
                        payload,
                        -15
                    )

                else:

                    self.log(
                        f"Unsupported compression ({method}): {name}",
                        "warning"
                    )

                    skipped += 1
                    continue

            except Exception as exc:

                self.log(
                    f"Decompression failed: {name} - {exc}",
                    "error"
                )

                skipped += 1
                continue

            # ------------------------------------------------
            # DATA VALIDATION
            # ------------------------------------------------

            # Calculate CRC32 to verify extracted file integrity.
            actual_crc = (
                binascii.crc32(result) & 0xFFFFFFFF
            )

            # Verify the extracted file size.
            if len(result) != uncompressed:

                self.log(
                    f"Size mismatch: {name}",
                    "warning"
                )

                skipped += 1
                continue

            # Verify the extracted file checksum.
            if actual_crc != crc:

                self.log(
                    f"CRC32 mismatch: {name}",
                    "warning"
                )

                skipped += 1
                continue

            # ------------------------------------------------
            # SAVE EXTRACTED FILE
            # ------------------------------------------------

            output_path = (
                archive_output / Path(*relative_path.parts)
            )

            # Create any required parent directories.
            output_path.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            # Write the verified file to disk.
            output_path.write_bytes(result)

            extracted += 1

            self.log(
                f"Extracted: {name} ({len(result):,} bytes)",
                "success"
            )

        # Report the final extraction results for this archive.
        self.log(
            f"Finished {archive_path.name}: "
            f"{extracted} extracted, {skipped} skipped",
            "success"
        )

    # ========================================================
    # EXTRACTION THREAD
    # ========================================================

    def start_extraction(self):

        """Validate settings and start background extraction."""

        # Prevent multiple extraction operations at once.
        if self.is_extracting:
            return

        if not self.archives:

            messagebox.showwarning(
                "Missing Archives",
                "Please select at least one YPK archive."
            )

            return

        if not self.output_dir:

            messagebox.showwarning(
                "Missing Output Folder",
                "Please choose an output folder."
            )

            return

        # Mark extraction as active.
        self.is_extracting = True

        self.extract_button.config(
            state="disabled",
            text="Extracting..."
        )

        self.progress["maximum"] = len(self.archives)
        self.progress["value"] = 0

        self.status_label.config(
            text="Extraction in progress..."
        )

        self.log("Starting extraction operation...", "success")

        # Start extraction without blocking the GUI.
        threading.Thread(
            target=self.run_extraction,
            daemon=True
        ).start()

    def run_extraction(self):

        """Process all selected archives in the background."""

        successful = 0
        failed = 0

        # Use a snapshot so the worker operates on consistent settings.
        archives = list(self.archives)

        for index, archive in enumerate(archives, 1):

            try:

                self.extract_archive(archive)
                successful += 1

            except Exception as exc:

                failed += 1

                self.log(
                    f"ERROR processing {archive.name}: {exc}",
                    "error"
                )

            # Update the progress bar on the main GUI thread.
            self.root.after(
                0,
                lambda value=index: self.progress.configure(
                    value=value
                )
            )

            self.root.after(
                0,
                lambda value=index, total=len(archives):
                    self.status_label.config(
                        text=f"Processed {value} / {total} archive(s)"
                    )
            )

        # Display the overall extraction summary.
        self.log("")
        self.log("Extraction operation complete.", "success")
        self.log(f"Successful archives: {successful}", "success")
        self.log(f"Failed archives: {failed}", "error" if failed else "normal")

        # Notify the main thread that extraction has finished.
        self.root.after(
            0,
            lambda: self.extraction_finished(
                successful,
                failed
            )
        )

    def extraction_finished(self, successful, failed):

        """Restore the interface and display the final result."""

        self.is_extracting = False

        self.extract_button.config(
            state="normal",
            text="▶   Extract Archives"
        )

        self.status_label.config(
            text=f"Completed | {successful} successful, {failed} failed"
        )

        messagebox.showinfo(
            "Extraction Complete",
            "Archive extraction has finished.\n\n"
            f"Successful archives: {successful}\n"
            f"Failed archives: {failed}\n\n"
            "Check the extraction log for details."
        )


# ============================================================
# APPLICATION ENTRY POINT
# ============================================================

if __name__ == "__main__":

    # Create the main Tkinter window.
    root = tk.Tk()

    # Initialise the extractor application.
    app = YPKExtractor(root)

    # Start the GUI event loop.
    root.mainloop()
