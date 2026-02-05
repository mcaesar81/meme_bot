from __future__ import annotations

import json
import threading
import time
import webbrowser
from dataclasses import dataclass

import requests

STATUS_URL = "http://127.0.0.1:8000/api/status"
CONTROL_URL = "http://127.0.0.1:8000/api/control"
DASHBOARD_URL = "http://127.0.0.1:8000/"


@dataclass
class TrayStatus:
    mode: str = "SAFE"
    pause_reason: str = "-"
    trade_count_today: int = 0
    daily_pnl_usd: float = 0.0


class TrayController:
    def __init__(self):
        from PIL import Image, ImageDraw
        import pystray

        self._Image = Image
        self._ImageDraw = ImageDraw
        self._pystray = pystray
        self.status = TrayStatus()
        self.icon = pystray.Icon("meme_bot")
        self.stop_event = threading.Event()

    def _make_icon(self, mode: str):
        color_map = {"RUN": (34, 197, 94), "PAUSE": (234, 179, 8), "SAFE": (239, 68, 68)}
        color = color_map.get(mode, (107, 114, 128))
        img = self._Image.new("RGB", (64, 64), (20, 20, 20))
        draw = self._ImageDraw.Draw(img)
        draw.ellipse((10, 10, 54, 54), fill=color)
        return img

    def _tooltip(self) -> str:
        return (
            f"meme_bot {self.status.mode} | reason={self.status.pause_reason} | "
            f"trades={self.status.trade_count_today} | pnl={self.status.daily_pnl_usd:.2f}"
        )

    def _refresh_status(self) -> None:
        while not self.stop_event.is_set():
            try:
                resp = requests.get(STATUS_URL, timeout=2)
                resp.raise_for_status()
                body = resp.json()
                self.status = TrayStatus(
                    mode=body.get("mode", "SAFE"),
                    pause_reason=(body.get("pause_reason") or body.get("safe_reason") or "-"),
                    trade_count_today=int(body.get("trade_count_today", 0)),
                    daily_pnl_usd=float(body.get("daily_pnl_usd", 0.0)),
                )
            except Exception:
                self.status = TrayStatus(mode="SAFE", pause_reason="web_ui_unreachable")

            self.icon.icon = self._make_icon(self.status.mode)
            self.icon.title = self._tooltip()
            time.sleep(2)

    @staticmethod
    def _send_control(action: str, reason: str) -> None:
        payload = {"action": action, "reason": reason}
        requests.post(CONTROL_URL, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=3)

    def run(self) -> None:
        pystray = self._pystray

        def do_run(icon, _item):
            self._send_control("RUN", "tray_manual")

        def do_pause(icon, _item):
            self._send_control("PAUSE", "tray_manual")

        def do_safe(icon, _item):
            self._send_control("SAFE", "tray_manual")

        def do_open(_icon, _item):
            webbrowser.open(DASHBOARD_URL)

        def do_exit(icon, _item):
            self.stop_event.set()
            icon.stop()

        self.icon.icon = self._make_icon("SAFE")
        self.icon.title = self._tooltip()
        self.icon.menu = pystray.Menu(
            pystray.MenuItem("Run", do_run),
            pystray.MenuItem("Pause", do_pause),
            pystray.MenuItem("Safe", do_safe),
            pystray.MenuItem("Open Dashboard", do_open),
            pystray.MenuItem("Exit Tray", do_exit),
        )

        t = threading.Thread(target=self._refresh_status, daemon=True)
        t.start()
        self.icon.run()


def run_tray() -> None:
    try:
        controller = TrayController()
    except Exception as exc:
        raise RuntimeError(
            "Tray dependencies missing or unsupported environment. Install pystray/pillow on Windows."
        ) from exc
    controller.run()
