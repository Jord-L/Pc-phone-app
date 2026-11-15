"""The PC-side window: on/off switch, phone address + PIN, settings, tray icon.

This is what double-clicking PhoneDesk.exe opens. The actual server runs in a
background thread (service.py); this file only starts/stops it and edits the
saved settings.

THREADING RULE: tkinter may only be touched from the main thread. The tray icon
(pystray) calls us from its own thread, so its menu handlers do nothing except
put a command on a queue; `_tick` (main thread, 4x per second) executes them.
"""

from __future__ import annotations

import logging
import queue
import sys
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Optional

from . import settings as settings_mod
from .main import _lan_ip, _setup_logging
from .platform import acquire_single_instance, list_monitors, prepare_process
from .service import FAILED, RUNNING, STARTING, STOPPED, STOPPING, ServiceController

log = logging.getLogger("server.gui")

WIDTHS = ["480", "640", "800", "960", "1280", "1600", "1920"]
POLL_MS = 250

GREEN, GREY, RED, AMBER = "#1a8f3c", "#6b6b6b", "#c0262d", "#b26a00"


def _tray_image(on: bool):
    """A small monitor glyph with a green (on) or grey (off) status dot."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 10, 58, 42], radius=5, fill=(40, 90, 200, 255))
    d.rectangle([11, 15, 53, 37], fill=(190, 215, 255, 255))
    d.rectangle([28, 42, 36, 50], fill=(70, 70, 70, 255))
    d.rectangle([20, 50, 44, 54], fill=(70, 70, 70, 255))
    d.ellipse([40, 36, 62, 58], fill=(26, 143, 60, 255) if on else (130, 130, 130, 255), outline=(255, 255, 255, 255), width=2)
    return img


class App:
    def __init__(self) -> None:
        self.s = settings_mod.load()
        self.ctl = ServiceController()
        self._cmds: "queue.Queue[str]" = queue.Queue()
        self._restart_pending = False
        self._tray = None
        self._tray_on: Optional[bool] = None
        self._tip_shown = False

        self.root = tk.Tk()
        self.root.title("PhoneDesk")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_window_icon()

        self._monitors = self._load_monitors()
        self._build()
        self._tray = self._make_tray()

        if self.s.autostart_service:
            self.ctl.start(self.s)
        self._tick()

    # ------------------------------------------------------------------ setup

    def _set_window_icon(self) -> None:
        try:
            from PIL import ImageTk

            self._icon_ref = ImageTk.PhotoImage(_tray_image(True))  # keep a reference or Tk drops it
            self.root.iconphoto(True, self._icon_ref)
        except Exception:
            pass  # default Tk icon is fine

    def _load_monitors(self) -> list[tuple[int, str]]:
        choices = [(-1, "Primary monitor")]
        try:
            for m in list_monitors(self.s.backend):
                choices.append((m.index, f"Monitor {m.index + 1}: {m.width}x{m.height}"))
        except Exception as exc:
            log.warning("Could not list monitors: %s", exc)
        return choices

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        outer = ttk.Frame(self.root, padding=6)
        outer.grid()

        # ---- status + switch
        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew", **pad)
        self.status = tk.Label(top, text="", font=("Segoe UI", 15, "bold"), anchor="w")
        self.status.grid(row=0, column=0, sticky="w")
        self.detail = ttk.Label(top, text="")
        self.detail.grid(row=1, column=0, sticky="w")
        buttons = ttk.Frame(top)
        buttons.grid(row=0, column=1, rowspan=2, padx=(24, 0))
        self.toggle_btn = ttk.Button(buttons, text="Turn on", width=14, command=self._toggle)
        self.toggle_btn.pack()
        # Restart = stop + start with the saved settings. Useful after a
        # network change or when a phone is stuck, without re-clicking twice.
        self.restart_btn = ttk.Button(buttons, text="Restart", width=14, command=self._restart_clicked)
        self.restart_btn.pack(pady=(4, 0))
        top.columnconfigure(0, weight=1)

        # ---- how to connect
        conn = ttk.LabelFrame(outer, text="Connect your phone", padding=10)
        conn.grid(row=1, column=0, sticky="ew", **pad)
        self.addr_var = tk.StringVar()
        self.pin_var = tk.StringVar()
        ttk.Label(conn, text="PC address").grid(row=0, column=0, sticky="w")
        ttk.Label(conn, textvariable=self.addr_var, font=("Consolas", 12, "bold")).grid(row=0, column=1, sticky="w", padx=10)
        ttk.Button(conn, text="Copy", width=8, command=lambda: self._copy(_lan_ip())).grid(row=0, column=2)
        ttk.Label(conn, text="PIN").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(conn, textvariable=self.pin_var, font=("Consolas", 12, "bold")).grid(row=1, column=1, sticky="w", padx=10, pady=(6, 0))
        ttk.Button(conn, text="New PIN", width=8, command=self._new_pin).grid(row=1, column=2, pady=(6, 0))
        ttk.Label(
            conn,
            text="Enter the address and PIN in the phone app. Phone and PC must be on the same Wi-Fi.",
            foreground=GREY,
            wraplength=360,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # ---- settings
        st = ttk.LabelFrame(outer, text="Settings", padding=10)
        st.grid(row=2, column=0, sticky="ew", **pad)

        self.v_monitor = tk.StringVar(value=self._monitor_label(self.s.monitor))
        self.v_width = tk.StringVar(value=str(self.s.max_width))
        self.v_quality = tk.IntVar(value=self.s.jpeg_quality)
        self.v_fps = tk.IntVar(value=self.s.fps)
        self.v_port = tk.IntVar(value=self.s.port)
        self.v_lan = tk.BooleanVar(value=self.s.lan)
        self.v_input = tk.BooleanVar(value=self.s.allow_input)
        self.v_auto = tk.BooleanVar(value=self.s.autostart_service)

        rows = [
            ("Screen to share", ttk.Combobox(st, textvariable=self.v_monitor, state="readonly", width=22,
                                             values=[label for _, label in self._monitors])),
            ("Picture width (px)", ttk.Combobox(st, textvariable=self.v_width, width=8, values=WIDTHS)),
            ("Picture quality", ttk.Spinbox(st, textvariable=self.v_quality, from_=10, to=90, increment=5, width=8)),
            ("Frames per second", ttk.Spinbox(st, textvariable=self.v_fps, from_=5, to=30, width=8)),
            ("Port", ttk.Spinbox(st, textvariable=self.v_port, from_=1024, to=65535, width=8)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(st, text=label).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="w", padx=10, pady=2)

        r = len(rows)
        ttk.Checkbutton(st, text="Allow phones on my network (off = this PC only)", variable=self.v_lan).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(st, text="Let the phone control mouse and keyboard (off = view only)", variable=self.v_input).grid(
            row=r + 1, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(st, text="Turn sharing on when PhoneDesk opens", variable=self.v_auto).grid(
            row=r + 2, column=0, columnspan=2, sticky="w")

        self.msg = ttk.Label(st, text="", foreground=RED, wraplength=360)
        self.msg.grid(row=r + 3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(st, text="Apply", width=10, command=self._apply).grid(row=r + 4, column=0, sticky="w", pady=(6, 0))
        ttk.Label(st, text="If sharing is on, Apply restarts it.", foreground=GREY).grid(
            row=r + 4, column=1, sticky="w", padx=10, pady=(6, 0))

        # ---- footer
        foot = ttk.Frame(outer)
        foot.grid(row=3, column=0, sticky="ew", **pad)
        ttk.Button(foot, text="Open test page", command=lambda: webbrowser.open(f"http://127.0.0.1:{self.s.port}/")).pack(side="left")
        ttk.Button(foot, text="Quit PhoneDesk", command=self._quit).pack(side="right")
        ttk.Button(foot, text="Hide to tray" if self._tray_possible() else "Close", command=self._on_close).pack(side="right", padx=8)

        self._refresh_static()

    def _monitor_label(self, index: int) -> str:
        for i, label in self._monitors:
            if i == index:
                return label
        return self._monitors[0][1]  # saved monitor no longer exists -> primary

    # ------------------------------------------------------------------ actions

    def _refresh_static(self) -> None:
        self.addr_var.set(f"{_lan_ip()}:{self.s.port}")
        self.pin_var.set(self.s.pin if self.s.lan else "(not needed: this PC only)")

    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _toggle(self) -> None:
        if self.ctl.active:
            self._restart_pending = False
            self.ctl.stop()
        else:
            self.ctl.start(self.s)

    def _restart_clicked(self) -> None:
        if self.ctl.active:
            self._restart()
        else:
            self.ctl.start(self.s)  # from "Could not start": try again

    def _new_pin(self) -> None:
        self.s.pin = settings_mod.new_pin()
        settings_mod.save(self.s)
        self._refresh_static()
        # A running server only reads the PIN at start. Restarting also drops
        # every connected phone, which is what "new PIN" is usually for.
        if self.ctl.active:
            self._restart()

    def _apply(self) -> None:
        try:
            width = int(self.v_width.get())
            quality = int(self.v_quality.get())
            fps = int(self.v_fps.get())
            port = int(self.v_port.get())
        except (ValueError, tk.TclError):
            self.msg.config(text="Width, quality, fps and port must be whole numbers.")
            return
        if not (160 <= width <= 3840 and 1 <= quality <= 95 and 1 <= fps <= 60 and 1024 <= port <= 65535):
            self.msg.config(text="Out of range: width 160-3840, quality 1-95, fps 1-60, port 1024-65535.")
            return
        self.msg.config(text="")

        monitor = next((i for i, label in self._monitors if label == self.v_monitor.get()), -1)
        s = self.s
        s.monitor, s.max_width, s.jpeg_quality, s.fps, s.port = monitor, width, quality, fps, port
        s.lan, s.allow_input, s.autostart_service = self.v_lan.get(), self.v_input.get(), self.v_auto.get()
        settings_mod.save(s)
        self._refresh_static()
        if self.ctl.active:
            self._restart()

    def _restart(self) -> None:
        self._restart_pending = True
        self.ctl.stop()  # _tick starts it again once the old thread has finished

    # --------------------------------------------------------------------- tray

    def _tray_possible(self) -> bool:
        try:
            import pystray  # noqa: F401

            return True
        except Exception:
            return False

    def _make_tray(self):
        if not self._tray_possible():
            return None
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Open PhoneDesk", lambda: self._cmds.put("show"), default=True),
            pystray.MenuItem(
                lambda item: "Turn sharing off" if self.ctl.active else "Turn sharing on",
                lambda: self._cmds.put("toggle"),
            ),
            pystray.MenuItem("Restart sharing", lambda: self._cmds.put("restart")),
            pystray.MenuItem("Quit", lambda: self._cmds.put("quit")),
        )
        try:
            icon = pystray.Icon("PhoneDesk", _tray_image(False), "PhoneDesk", menu)
            icon.run_detached()  # its own thread; our window keeps the main one
            return icon
        except Exception as exc:
            log.warning("No tray icon: %s", exc)
            return None

    def _update_tray(self, on: bool, tip: str) -> None:
        if self._tray is None:
            return
        try:
            if on != self._tray_on:
                self._tray.icon = _tray_image(on)
                self._tray_on = on
            self._tray.title = tip
        except Exception:
            pass

    # ------------------------------------------------------------------- window

    def _show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_close(self) -> None:
        if self._tray is not None:
            self.root.withdraw()  # keep running in the tray
            if not self._tip_shown:
                self._tip_shown = True
                try:
                    self._tray.notify("Still running here. Click the icon to open it again.", "PhoneDesk")
                except Exception:
                    pass
        else:
            self._quit()

    def _quit(self) -> None:
        self.ctl.stop()
        self.ctl.join(5.0)
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # --------------------------------------------------------------------- poll

    def _tick(self) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                break
            if cmd == "show":
                self._show()
            elif cmd == "toggle":
                self._toggle()
            elif cmd == "restart":
                self._restart_clicked()
            elif cmd == "quit":
                self._quit()
                return

        if self._restart_pending and not self.ctl.active:
            self._restart_pending = False
            self.ctl.start(self.s)

        state = self.ctl.state
        n = self.ctl.client_count()
        if state == RUNNING:
            self.status.config(text="Sharing is ON", fg=GREEN)
            self.detail.config(text=f"{n} phone{'s' if n != 1 else ''} connected" + ("" if self.s.allow_input else "  (view only)"))
            self.toggle_btn.config(text="Turn off", state="normal")
        elif state == STARTING:
            self.status.config(text="Starting...", fg=AMBER)
            self.detail.config(text="")
            self.toggle_btn.config(text="Turn off", state="normal")
        elif state == STOPPING:
            self.status.config(text="Stopping...", fg=AMBER)
            self.detail.config(text="")
            self.toggle_btn.config(text="Turn on", state="disabled")
        elif state == FAILED:
            self.status.config(text="Could not start", fg=RED)
            self.detail.config(text=self.ctl.error or "unknown error")
            self.toggle_btn.config(text="Try again", state="normal")
        else:
            self.status.config(text="Sharing is OFF", fg=GREY)
            self.detail.config(text="Phones cannot connect")
            self.toggle_btn.config(text="Turn on", state="normal")

        # Restart only makes sense once there is something to restart.
        self.restart_btn.config(state="normal" if state in (RUNNING, FAILED) else "disabled")

        self._update_tray(state == RUNNING, f"PhoneDesk: {self.status.cget('text')}")
        self.root.after(POLL_MS, self._tick)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    # Must run before the first window exists, or Windows keeps the process
    # DPI-unaware and screen capture / cursor coordinates come out scaled.
    prepare_process()
    _setup_logging(False)

    if not acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "PhoneDesk",
            "PhoneDesk is already running.\n\nLook for its icon in the system tray "
            "(bottom right, possibly behind the ^ arrow) and click it.",
        )
        root.destroy()
        sys.exit(0)

    try:
        App().run()
    except Exception:
        log.exception("GUI crashed")
        raise
