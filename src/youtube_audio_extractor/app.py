from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import DownloadSettings, MEDIA_FORMAT_CHOICES, QUALITY_CHOICES, default_output_dir, download_media


class AudioExtractorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Snagger")
        self.geometry("760x500")
        self.minsize(660, 430)

        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.output_path: Path | None = None

        self.url_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(default_output_dir()))
        self.format_var = tk.StringVar(value=MEDIA_FORMAT_CHOICES["mp3"])
        self.quality_var = tk.StringVar(value="Maximum VBR quality")
        self.keep_source_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready.")

        self._build_styles()
        self._build_ui()
        self.after(100, self._drain_events)

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("Muted.TLabel", foreground="#555555")
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=22)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)

        ttk.Label(root, text="Snagger", style="Title.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Label(
            root,
            text="Save permitted YouTube URLs as MP3 audio or MP4 video.",
            style="Muted.TLabel",
            wraplength=620,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(4, 18))

        form = ttk.Frame(root)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="YouTube URL").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        url_entry = ttk.Entry(form, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, sticky="ew", pady=6)
        url_entry.focus()
        ttk.Button(form, text="Paste", command=self._paste_url).grid(row=0, column=2, padx=(8, 0), pady=6)

        ttk.Label(form, text="Output folder").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        ttk.Entry(form, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(form, text="Browse", command=self._choose_output_dir).grid(
            row=1,
            column=2,
            padx=(8, 0),
            pady=6,
        )

        ttk.Label(form, text="Output").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=6)
        media_format = ttk.Combobox(
            form,
            textvariable=self.format_var,
            values=list(MEDIA_FORMAT_CHOICES.values()),
            state="readonly",
        )
        media_format.grid(row=2, column=1, sticky="w", pady=6)

        ttk.Label(form, text="MP3 quality").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=6)
        quality = ttk.Combobox(
            form,
            textvariable=self.quality_var,
            values=list(QUALITY_CHOICES),
            state="readonly",
        )
        quality.grid(row=3, column=1, sticky="w", pady=6)

        ttk.Checkbutton(
            form,
            text="Keep original source audio too",
            variable=self.keep_source_var,
        ).grid(row=4, column=1, sticky="w", pady=(6, 0))

        actions = ttk.Frame(root)
        actions.grid(row=3, column=0, sticky="ew", pady=(18, 10))
        actions.columnconfigure(2, weight=1)

        self.download_button = ttk.Button(
            actions,
            text="Snag",
            command=self._start_download,
            style="Primary.TButton",
        )
        self.download_button.grid(row=0, column=0, padx=(0, 8))

        self.open_button = ttk.Button(actions, text="Open Folder", command=self._open_output_dir)
        self.open_button.grid(row=0, column=1)

        ttk.Label(actions, textvariable=self.status_var, style="Muted.TLabel").grid(
            row=0,
            column=2,
            sticky="e",
        )

        self.progress = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress.grid(row=4, column=0, sticky="ew", pady=(0, 12))

        self.log = tk.Text(
            root,
            height=10,
            wrap="word",
            borderwidth=1,
            relief="solid",
            font=("Consolas", 9),
        )
        self.log.grid(row=5, column=0, sticky="nsew")
        self.log.configure(state="disabled")

        note = ttk.Label(
            root,
            text="Note: YouTube source audio is usually Opus or AAC; converting to MP3 cannot be mathematically lossless.",
            style="Muted.TLabel",
            wraplength=620,
            justify="left",
        )
        note.grid(row=6, column=0, sticky="w", pady=(12, 0))

    def _paste_url(self) -> None:
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            messagebox.showinfo("Clipboard", "No text URL was found on the clipboard.")

    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_var.get() or str(default_output_dir()))
        if selected:
            self.output_var.set(selected)

    def _start_download(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        url = self.url_var.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showerror("Missing URL", "Enter a valid YouTube URL.")
            return

        quality_label = self.quality_var.get()
        output_format = next(
            value for value, label in MEDIA_FORMAT_CHOICES.items() if label == self.format_var.get()
        )
        settings = DownloadSettings(
            url=url,
            output_dir=Path(self.output_var.get()).expanduser(),
            quality_label=quality_label,
            quality_value=QUALITY_CHOICES[quality_label],
            keep_source_audio=output_format == "mp3" and self.keep_source_var.get(),
            output_format=output_format,
        )

        self.output_path = None
        self._clear_log()
        self.progress["value"] = 0
        self.status_var.set("Starting...")
        self.download_button.configure(state="disabled")

        self.worker = threading.Thread(
            target=download_media,
            args=(settings, self.events),
            daemon=True,
        )
        self.worker.start()

    def _open_output_dir(self) -> None:
        target = self.output_path.parent if self.output_path else Path(self.output_var.get()).expanduser()
        if not target.exists():
            messagebox.showerror("Missing folder", "The output folder does not exist yet.")
            return

        try:
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(target)], check=False)
            else:
                subprocess.run(["xdg-open", str(target)], check=False)
        except Exception as exc:
            messagebox.showerror("Open Folder", f"Could not open the folder:\n{exc}")

    def _drain_events(self) -> None:
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "status":
                    self.status_var.set(str(value))
                elif event == "progress":
                    self.progress["value"] = float(value)
                elif event == "log":
                    self._append_log(str(value))
                elif event == "done":
                    self.output_path = value if isinstance(value, Path) else None
                    self.download_button.configure(state="normal")
                    if self.output_path:
                        self._append_log(f"Saved file: {self.output_path}")
                    else:
                        self._append_log("Saved file.")
                elif event == "error":
                    self.progress["value"] = 0
                    self.download_button.configure(state="normal")
                    self.status_var.set("Failed.")
                    self._append_log(str(value))
                    messagebox.showerror("Conversion failed", str(value))
        except queue.Empty:
            pass

        self.after(100, self._drain_events)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, message + "\n")
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", tk.END)
        self.log.configure(state="disabled")


def main() -> None:
    app = AudioExtractorApp()
    app.mainloop()
