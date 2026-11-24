import sys
import time
import socket
import ipaddress
import subprocess
import threading
import queue
import json
import importlib

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import requests
import numpy as np
import cv2
from PIL import Image, ImageTk


# ---------------------------- SETTINGS ----------------------------
CAMERA_URL = "http://{ip}/"
CAMERA_DECODE_FPS = 25.0
GUI_FPS = 25.0
YOLO_MAX_FPS = 3.0
YOLO_IMGSZ = 640
YOLO_CONF = 0.50
# Ignore pathological detections whose box covers almost the entire camera image.
# Set to 1.0 to disable this guard.
YOLO_MAX_BOX_AREA_RATIO = 0.92

CONNECT_TIMEOUT = 2.0
# MJPEG cameras can pause for several seconds while starting or changing exposure.
# Keep this finite so a genuinely dead stream is recovered, but do not use a
# very short timeout because that creates false ReadTimeout errors.
READ_TIMEOUT = 8.0
FRAME_STALL_TIMEOUT = 10.0
STREAM_CHUNK_SIZE = 4096
MAX_MJPEG_BUFFER = 2 * 1024 * 1024
RECONNECT_DELAY = 0.75

ARP_SETTLE_SECONDS = 0.25
ARP_TRIGGER_PORT = 9
MAX_HOSTS_PER_INTERFACE = 254

# Small camera frames do not benefit from OpenCV creating many worker threads.
cv2.setNumThreads(1)


# ------------------------- NETWORK DISCOVERY -------------------------
def run_command(command, timeout=5):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=flags,
    )


def powershell_json(script, timeout=6):
    if not sys.platform.startswith("win"):
        return []
    try:
        r = run_command(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            timeout=timeout,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        data = json.loads(r.stdout.strip())
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def usable_private_ipv4(address):
    try:
        ip = ipaddress.ip_address(address)
        return (
            ip.version == 4
            and ip.is_private
            and not ip.is_loopback
            and not ip.is_link_local
            and not ip.is_multicast
            and not ip.is_unspecified
        )
    except ValueError:
        return False


def get_scan_interfaces():
    targets = []

    if sys.platform.startswith("win"):
        script = r"""
        Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object {
            $_.IPAddress -ne '127.0.0.1' -and
            $_.IPAddress -notlike '169.254.*'
        } |
        ForEach-Object {
            $i = Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
            if ($i -and $i.ConnectionState -eq 'Connected') {
                [PSCustomObject]@{
                    Name = $_.InterfaceAlias
                    Index = $_.InterfaceIndex
                    IP = $_.IPAddress
                    Prefix = $_.PrefixLength
                }
            }
        } | ConvertTo-Json -Compress
        """

        for item in powershell_json(script):
            try:
                name = str(item["Name"]).strip()
                local_ip = str(item["IP"]).strip()
                prefix = int(item["Prefix"])
                index = int(item["Index"])
            except Exception:
                continue

            if not usable_private_ipv4(local_ip):
                continue

            try:
                network = ipaddress.ip_network(f"{local_ip}/{prefix}", strict=False)
            except ValueError:
                continue

            if max(0, network.num_addresses - 2) > MAX_HOSTS_PER_INTERFACE:
                network = ipaddress.ip_network(f"{local_ip}/24", strict=False)

            lname = name.lower()
            hotspot_hint = any(
                token in lname
                for token in (
                    "local area connection",
                    "wi-fi direct",
                    "wifi direct",
                    "mobile hotspot",
                    "hosted network",
                )
            )

            targets.append(
                {
                    "name": name,
                    "index": index,
                    "local_ip": local_ip,
                    "network": network,
                    "hotspot_hint": hotspot_hint,
                }
            )

    if not targets:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        except OSError:
            local_ip = socket.gethostbyname(socket.gethostname())
        finally:
            s.close()

        if usable_private_ipv4(local_ip):
            targets.append(
                {
                    "name": "Default network",
                    "index": None,
                    "local_ip": local_ip,
                    "network": ipaddress.ip_network(f"{local_ip}/24", strict=False),
                    "hotspot_hint": False,
                }
            )

    unique = {}
    for t in targets:
        unique[(t["name"], t["local_ip"], str(t["network"]))] = t
    targets = list(unique.values())

    # If Windows clearly identifies a Mobile Hotspot/Wi-Fi Direct adapter,
    # scan only that adapter. This avoids wasting time on normal Wi-Fi/VPN LANs.
    hotspot_targets = [t for t in targets if t["hotspot_hint"]]
    return hotspot_targets or targets


def interface_text(targets):
    if not targets:
        return "No active private IPv4 network found."
    return "\n".join(
        f'{t["name"]}: {t["local_ip"]}  ({t["network"]})' for t in targets
    )


def trigger_arp(target, progress=None):
    hosts = [
        str(ip)
        for ip in target["network"].hosts()
        if str(ip) != target["local_ip"]
    ]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sock.bind((target["local_ip"], 0))
        total = max(1, len(hosts))
        for i, host in enumerate(hosts, 1):
            try:
                sock.sendto(b"\x00", (host, ARP_TRIGGER_PORT))
            except OSError:
                pass
            if progress and (i % 32 == 0 or i == len(hosts)):
                progress(i / total)
    finally:
        sock.close()


def get_neighbors():
    if not sys.platform.startswith("win"):
        return []

    script = r"""
    Get-NetNeighbor -AddressFamily IPv4 |
    Where-Object {
        $_.State -ne 'Unreachable' -and
        $_.State -ne 'Incomplete' -and
        $_.IPAddress -ne '255.255.255.255'
    } |
    Select-Object InterfaceIndex,IPAddress,LinkLayerAddress,State |
    ConvertTo-Json -Compress
    """

    out = []
    for item in powershell_json(script):
        try:
            ip = str(item["IPAddress"]).strip()
            mac = str(item["LinkLayerAddress"]).strip().replace("-", ":").lower()
            index = item.get("InterfaceIndex")
            index = int(index) if index is not None else None
            state = str(item.get("State", "Neighbor")).strip()
        except Exception:
            continue

        if not usable_private_ipv4(ip):
            continue
        if not mac or mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            continue

        out.append({"ip": ip, "mac": mac, "index": index, "state": state})
    return out


def discover_devices(targets, progress=None):
    if not targets:
        return []

    count = len(targets)
    for idx, target in enumerate(targets):
        def p(ratio, idx=idx):
            if progress:
                progress(((idx + ratio) / count) * 80.0)

        try:
            trigger_arp(target, p)
        except OSError:
            pass

    time.sleep(ARP_SETTLE_SECONDS)
    if progress:
        progress(85.0)

    devices = {}
    for n in get_neighbors():
        target = None

        for t in targets:
            if t["index"] is not None and n["index"] == t["index"]:
                target = t
                break

        if target is None:
            for t in targets:
                try:
                    if ipaddress.ip_address(n["ip"]) in t["network"]:
                        target = t
                        break
                except ValueError:
                    pass

        if target is None or n["ip"] == target["local_ip"]:
            continue

        devices[(n["ip"], target["name"])] = {
            "ip": n["ip"],
            "mac": n["mac"],
            "interface": target["name"],
            "state": n["state"],
        }

    result = list(devices.values())
    result.sort(key=lambda d: tuple(map(int, d["ip"].split("."))))
    if progress:
        progress(100.0)
    return result


# ---------------------------- CAMERA ----------------------------
class CameraReceiver:
    SOI = b"\xff\xd8"
    EOI = b"\xff\xd9"

    def __init__(self, events):
        self.events = events
        self.state_lock = threading.Lock()
        self.thread = None
        self.stop_event = None
        self.response = None
        self.session = None
        self.connected = False
        self.generation = 0

        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.frame_id = 0

        self.fps_lock = threading.Lock()
        self.fps = 0.0
        self.fps_count = 0
        self.fps_t0 = time.perf_counter()

    def _is_current(self, generation, stop_event):
        with self.state_lock:
            return (
                generation == self.generation
                and self.stop_event is stop_event
                and not stop_event.is_set()
            )

    def _emit(self, generation, stop_event, event, data=None):
        if self._is_current(generation, stop_event):
            self.events.put((event, data))

    def _reset_fps(self):
        with self.fps_lock:
            self.fps = 0.0
            self.fps_count = 0
            self.fps_t0 = time.perf_counter()

    def start(self, ip):
        self.stop()

        stop_event = threading.Event()
        with self.state_lock:
            self.generation += 1
            generation = self.generation
            self.stop_event = stop_event
            self.connected = False
            self.thread = threading.Thread(
                target=self._worker,
                args=(ip, generation, stop_event),
                daemon=True,
            )
            thread = self.thread

        self._reset_fps()
        thread.start()

    def stop(self):
        with self.state_lock:
            thread = self.thread
            stop_event = self.stop_event
            response = self.response
            session = self.session

            self.generation += 1
            self.thread = None
            self.stop_event = None
            self.response = None
            self.session = None
            self.connected = False

        if stop_event is not None:
            stop_event.set()

        if response is not None:
            try:
                response.close()
            except Exception:
                pass

        if session is not None:
            try:
                session.close()
            except Exception:
                pass

        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=0.8)

        with self.frame_lock:
            self.latest_frame = None
            self.frame_id += 1
        self._reset_fps()

    def get_frame(self):
        with self.frame_lock:
            return self.latest_frame, self.frame_id

    def get_fps(self):
        with self.fps_lock:
            return self.fps

    def _publish(self, frame, generation, stop_event):
        if not self._is_current(generation, stop_event):
            return

        with self.frame_lock:
            self.latest_frame = frame
            self.frame_id += 1

        with self.fps_lock:
            self.fps_count += 1
            now = time.perf_counter()
            elapsed = now - self.fps_t0
            if elapsed >= 1.0:
                self.fps = self.fps_count / elapsed
                self.fps_count = 0
                self.fps_t0 = now

    def _worker(self, ip, generation, stop_event):
        url = CAMERA_URL.format(ip=ip)
        ever_connected = False
        initial_failures = 0

        while self._is_current(generation, stop_event):
            session = None
            response = None
            buffer = bytearray()
            last_decode = 0.0
            decode_period = 1.0 / CAMERA_DECODE_FPS

            try:
                self._emit(
                    generation, stop_event, "status",
                    f"Connecting to {ip} ..."
                )

                session = requests.Session()
                session.trust_env = False
                response = session.get(
                    url,
                    stream=True,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                    headers={
                        "Accept": "multipart/x-mixed-replace,image/jpeg,*/*",
                        "Cache-Control": "no-cache, no-store",
                        "Pragma": "no-cache",
                        "Connection": "close",
                        "User-Agent": "AmebaCameraGUI/3.0",
                    },
                )
                response.raise_for_status()

                with self.state_lock:
                    if (
                        generation != self.generation
                        or self.stop_event is not stop_event
                        or stop_event.is_set()
                    ):
                        break
                    self.response = response
                    self.session = session
                    self.connected = True

                ever_connected = True
                initial_failures = 0
                self._emit(
                    generation, stop_event,
                    "camera_connected", url
                )

                # Use requests' iterator instead of response.raw.stream().
                # iter_content translates urllib3 ReadTimeoutError/ProtocolError
                # into requests exceptions handled by the reconnect path below.
                last_good_frame = time.perf_counter()

                for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                    if not self._is_current(generation, stop_event):
                        break
                    if not chunk:
                        continue

                    buffer.extend(chunk)

                    # If bytes are arriving but no valid JPEG is produced for a
                    # long time, force a clean reconnect instead of freezing.
                    now = time.perf_counter()
                    if now - last_good_frame > FRAME_STALL_TIMEOUT:
                        raise requests.ConnectionError(
                            "No decodable camera frame received"
                        )

                    if len(buffer) > MAX_MJPEG_BUFFER:
                        start = buffer.rfind(self.SOI)
                        if start >= 0:
                            del buffer[:start]
                        else:
                            buffer.clear()
                        continue

                    # Decode only the newest complete JPEG currently buffered.
                    # Older completed frames are deliberately discarded so the
                    # display cannot build up seconds of latency.
                    end = buffer.rfind(self.EOI)
                    if end < 0:
                        continue

                    start = buffer.rfind(self.SOI, 0, end)
                    if start < 0:
                        del buffer[: end + 2]
                        continue

                    end += 2
                    jpeg = bytes(buffer[start:end])
                    del buffer[:end]

                    now = time.perf_counter()
                    if now - last_decode < decode_period:
                        continue

                    frame = cv2.imdecode(
                        np.frombuffer(jpeg, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                    if frame is not None:
                        last_decode = now
                        last_good_frame = now
                        self._publish(frame, generation, stop_event)

                if self._is_current(generation, stop_event):
                    raise requests.ConnectionError("Camera stream ended")

            except (
                requests.ReadTimeout,
                requests.ConnectionError,
                requests.ConnectTimeout,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                if not self._is_current(generation, stop_event):
                    break

                if not ever_connected:
                    initial_failures += 1
                    if initial_failures >= 2:
                        self._emit(
                            generation,
                            stop_event,
                            "camera_error",
                            (
                                f"Camera at {ip} did not provide a usable "
                                "MJPEG stream after two attempts.\n\n"
                                "Check the camera IP, Wi-Fi connection, and "
                                "that no other viewer is holding the stream."
                            ),
                        )
                        break
                    self._emit(
                        generation, stop_event, "status",
                        "Camera response delayed - retrying ..."
                    )
                else:
                    self._emit(
                        generation, stop_event, "status",
                        "Camera stream stalled - reconnecting automatically ..."
                    )

            except requests.RequestException as exc:
                self._emit(
                    generation, stop_event, "camera_error",
                    f"HTTP camera error:\n\n{exc}"
                )
                break

            except Exception as exc:
                self._emit(
                    generation, stop_event, "camera_error",
                    f"Camera error:\n\n{exc}"
                )
                break

            finally:
                with self.state_lock:
                    if (
                        generation == self.generation
                        and self.stop_event is stop_event
                    ):
                        if self.response is response:
                            self.response = None
                        if self.session is session:
                            self.session = None
                        self.connected = False

                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass

            if stop_event.wait(RECONNECT_DELAY):
                break

        if self._is_current(generation, stop_event):
            self.events.put(("camera_disconnected", None))


# ----------------------------- YOLO -----------------------------
class YoloWorker:
    def __init__(self, camera, events):
        self.camera = camera
        self.events = events
        self.stop_event = threading.Event()
        self.enabled = threading.Event()
        self.settings_lock = threading.Lock()
        self.result_lock = threading.Lock()
        self.fps_lock = threading.Lock()

        self.model_path = ""
        self.conf = YOLO_CONF
        self.max_fps = YOLO_MAX_FPS
        self.model = None
        self.loaded_path = None
        self.detections = []
        self.fps = 0.0
        self.fps_count = 0
        self.fps_t0 = time.perf_counter()

        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def configure(self, path, conf, max_fps):
        with self.settings_lock:
            self.model_path = path
            self.conf = float(conf)
            self.max_fps = max(0.5, float(max_fps))

    def enable(self):
        self.enabled.set()

    def disable(self):
        self.enabled.clear()
        with self.result_lock:
            self.detections = []
        with self.fps_lock:
            self.fps = 0.0

    def stop(self):
        self.stop_event.set()
        self.enabled.set()
        if self.thread.is_alive():
            self.thread.join(timeout=0.5)

    def is_enabled(self):
        return self.enabled.is_set()

    def get_detections(self):
        with self.result_lock:
            return list(self.detections)

    def get_fps(self):
        with self.fps_lock:
            return self.fps

    def _load_model(self, path):
        if not path:
            raise RuntimeError("Select a YOLO .pt model first.")
        if self.model is not None and self.loaded_path == path:
            return

        try:
            ultralytics = importlib.import_module("ultralytics")
        except ImportError as exc:
            raise RuntimeError("Install YOLO with: py -3.13 -m pip install ultralytics") from exc

        self.events.put(("status", "Loading YOLO model ..."))
        self.model = ultralytics.YOLO(path)
        self.loaded_path = path

    def _tick_fps(self):
        with self.fps_lock:
            self.fps_count += 1
            now = time.perf_counter()
            elapsed = now - self.fps_t0
            if elapsed >= 1.0:
                self.fps = self.fps_count / elapsed
                self.fps_count = 0
                self.fps_t0 = now

    def _worker(self):
        last_frame_id = -1
        last_inference = 0.0

        while not self.stop_event.is_set():
            if not self.enabled.wait(0.1):
                continue
            if self.stop_event.is_set():
                break

            with self.settings_lock:
                path = self.model_path
                conf = self.conf
                max_fps = self.max_fps

            try:
                self._load_model(path)
            except Exception as exc:
                self.events.put(("yolo_error", str(exc)))
                self.disable()
                continue

            frame, frame_id = self.camera.get_frame()
            if frame is None or frame_id == last_frame_id:
                time.sleep(0.01)
                continue

            period = 1.0 / max_fps
            remaining = period - (time.perf_counter() - last_inference)
            if remaining > 0:
                time.sleep(min(remaining, 0.02))
                continue

            frame, frame_id = self.camera.get_frame()
            if frame is None or frame_id == last_frame_id:
                continue

            last_frame_id = frame_id
            last_inference = time.perf_counter()

            try:
                result = self.model.predict(
                    source=frame,
                    conf=conf,
                    imgsz=YOLO_IMGSZ,
                    verbose=False,
                )[0]

                detections = []
                if result.boxes is not None:
                    boxes = result.boxes.xyxy.detach().cpu().numpy()
                    scores = result.boxes.conf.detach().cpu().numpy()
                    classes = result.boxes.cls.detach().cpu().numpy().astype(int)
                    names = result.names

                    frame_h, frame_w = frame.shape[:2]
                    frame_area = max(1.0, float(frame_w * frame_h))

                    for box, score, cls_id in zip(boxes, scores, classes):
                        x1, y1, x2, y2 = map(float, box)
                        box_w = max(0.0, x2 - x1)
                        box_h = max(0.0, y2 - y1)
                        area_ratio = (box_w * box_h) / frame_area

                        # A pretrained detector can occasionally return a box that
                        # spans essentially the complete image.  Do not let such a
                        # pathological box trigger the display/alarm.
                        if area_ratio >= YOLO_MAX_BOX_AREA_RATIO:
                            continue

                        label = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else str(names[cls_id])
                        detections.append(
                            {
                                "box": (x1, y1, x2, y2),
                                "score": float(score),
                                "label": label,
                            }
                        )

                with self.result_lock:
                    self.detections = detections
                self._tick_fps()

            except Exception as exc:
                self.events.put(("yolo_error", str(exc)))
                self.disable()


# ------------------------------ GUI ------------------------------
# Professional dark HMI palette.  The layout follows a consistent 8 px spacing
# system, a compact control sidebar, and a video-first workspace.
BG = "#101214"
SURFACE = "#181B1F"
SURFACE_2 = "#20242A"
SURFACE_3 = "#282D34"
BORDER = "#343A42"
TEXT = "#F3F3F3"
MUTED = "#A7ADB5"
BLUE = "#2878D0"
BLUE_HOVER = "#3489E2"
GREEN = "#2E9D58"
GREEN_HOVER = "#38AD65"
RED = "#E53935"
RED_HOVER = "#F04B47"
VIDEO_BG = "#050607"
LED_OFF = "#421619"
LED_ON = "#FF3434"

FONT = "Times New Roman"
FONT_TITLE = (FONT, 17, "bold")
FONT_SECTION = (FONT, 13, "bold")
FONT_BODY = (FONT, 11)
FONT_BODY_BOLD = (FONT, 11, "bold")
FONT_SMALL = (FONT, 10)
FONT_STATUS = (FONT, 10)


def rounded_rectangle(canvas, x1, y1, x2, y2, radius=12, **kwargs):
    radius = max(1, min(radius, int((x2-x1)/2), int((y2-y1)/2)))
    points = [
        x1+radius,y1, x2-radius,y1, x2,y1, x2,y1+radius,
        x2,y2-radius, x2,y2, x2-radius,y2, x1+radius,y2,
        x1,y2, x1,y2-radius, x1,y1+radius, x1,y1
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class RoundedCard(tk.Canvas):
    def __init__(self, master, radius=12, outer_bg=BG, fill=SURFACE, border=BORDER,
                 padding=12, **kwargs):
        super().__init__(master, bg=outer_bg, highlightthickness=0, bd=0, **kwargs)
        self.radius = radius
        self.fill = fill
        self.border = border
        self.padding = padding
        self.inner = tk.Frame(self, bg=fill)
        self.window = self.create_window(padding, padding, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        self.delete("cardshape")
        rounded_rectangle(self, 1, 1, w-1, h-1, self.radius,
                          fill=self.fill, outline=self.border, width=1, tags="cardshape")
        self.tag_lower("cardshape")
        p = self.padding
        self.coords(self.window, p, p)
        self.itemconfigure(self.window, width=max(1, w-2*p), height=max(1, h-2*p))


class RoundedButton(tk.Canvas):
    def __init__(self, master, text, command, color=SURFACE_3, hover=None,
                 fg=TEXT, height=30, radius=8, font=FONT_BODY_BOLD, **kwargs):
        super().__init__(master, height=height, bg=master.cget("bg"), highlightthickness=0,
                         bd=0, cursor="hand2", **kwargs)
        self.text = text
        self.command = command
        self.color = color
        self.hover = hover or color
        self.fg = fg
        self.radius = radius
        self.font = font
        self.enabled = True
        self.bind("<Configure>", lambda e: self._draw(self.color))
        self.bind("<Enter>", lambda e: self._draw(self.hover if self.enabled else SURFACE_3))
        self.bind("<Leave>", lambda e: self._draw(self.color if self.enabled else SURFACE_3))
        self.bind("<Button-1>", self._click)

    def _draw(self, color):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        rounded_rectangle(self, 1, 1, max(2, w-1), max(2, h-1), self.radius,
                          fill=color, outline=color)
        self.create_text(w/2, h/2, text=self.text, fill=self.fg, font=self.font)

    def _click(self, _event):
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._draw(self.color if self.enabled else SURFACE_3)


class LedIndicator(tk.Canvas):
    def __init__(self, master, size=28):
        super().__init__(master, width=size, height=size, bg=master.cget("bg"),
                         highlightthickness=0, bd=0)
        self.size = size
        self.active = None
        self.set_active(False)

    def set_active(self, active):
        active = bool(active)
        if self.active == active:
            return
        self.active = active
        self.delete("all")
        s = self.size
        if active:
            self.create_oval(1, 1, s-1, s-1, fill="#381315", outline="")
            self.create_oval(4, 4, s-4, s-4, fill="#7E1C20", outline="")
            self.create_oval(7, 7, s-7, s-7, fill=LED_ON, outline="#FF7777", width=1)
            self.create_oval(10, 9, 14, 13, fill="#FFD4D4", outline="")
        else:
            self.create_oval(6, 6, s-6, s-6, fill=LED_OFF, outline="#713034", width=1)


class AmebaApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Ameba Vision Monitor")
        self.geometry("1360x800")
        self.minsize(1080, 650)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close_app)

        self.events = queue.Queue()
        self.scan_targets = get_scan_interfaces()
        self.camera = CameraReceiver(self.events)
        self.yolo = YoloWorker(self.camera, self.events)

        self.selected_ip = None
        self.photo = None
        self.last_frame_id = -1
        self.scanning = False
        self.last_alarm_state = False
        self.model_path = ""

        self._configure_ttk()
        self._build_gui()
        self.after(40, self.process_events)
        self.after(int(1000 / GUI_FPS), self.update_video)
        self.after(500, self.update_stats)

    def _configure_ttk(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Vision.Treeview", background=SURFACE, fieldbackground=SURFACE,
                        foreground=TEXT, rowheight=27, borderwidth=0, relief="flat",
                        font=FONT_SMALL)
        style.map("Vision.Treeview", background=[("selected", "#30465D")],
                  foreground=[("selected", TEXT)])
        style.configure("Vision.Treeview.Heading", background=SURFACE_2, foreground=TEXT,
                        relief="flat", borderwidth=0, font=(FONT, 10, "bold"), padding=(5, 6))
        style.map("Vision.Treeview.Heading", background=[("active", SURFACE_3)])
        style.configure("Vision.Horizontal.TProgressbar", troughcolor=SURFACE_3,
                        background=BLUE, bordercolor=SURFACE_3, lightcolor=BLUE,
                        darkcolor=BLUE, thickness=4)
        style.configure("Vision.Vertical.TScrollbar", background=SURFACE_3,
                        troughcolor=SURFACE, bordercolor=SURFACE, arrowcolor=MUTED)

    @staticmethod
    def _section_title(parent, text):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=TEXT,
                        font=FONT_SECTION, anchor="w")

    @staticmethod
    def _label(parent, text="", **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=MUTED,
                        font=FONT_SMALL, **kwargs)

    def _build_gui(self):
        shell = tk.Frame(self, bg=BG)
        shell.pack(fill="both", expand=True)

        # Compact desktop-HMI header: no borrowed branding or decorative logo.
        top = tk.Frame(shell, bg=SURFACE, height=54,
                       highlightthickness=1, highlightbackground=BORDER)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="Ameba Vision Monitor", bg=SURFACE, fg=TEXT,
                 font=FONT_TITLE).pack(side="left", padx=18)

        self.camera_dot = tk.Canvas(top, width=16, height=16, bg=SURFACE,
                                    highlightthickness=0)
        self.camera_dot.pack(side="right", padx=(6, 18))
        self.camera_dot_id = self.camera_dot.create_oval(4, 4, 12, 12,
                                                         fill="#565B62", outline="")
        tk.Label(top, text="Camera", bg=SURFACE, fg=MUTED,
                 font=FONT_SMALL).pack(side="right")

        self.detect_dot = tk.Canvas(top, width=16, height=16, bg=SURFACE,
                                    highlightthickness=0)
        self.detect_dot.pack(side="right", padx=(6, 18))
        self.detect_dot_id = self.detect_dot.create_oval(4, 4, 12, 12,
                                                         fill="#565B62", outline="")
        tk.Label(top, text="Detection", bg=SURFACE, fg=MUTED,
                 font=FONT_SMALL).pack(side="right")

        root = tk.Frame(shell, bg=BG)
        root.pack(fill="both", expand=True, padx=12, pady=12)
        root.grid_columnconfigure(0, weight=23, minsize=285)
        root.grid_columnconfigure(1, weight=77, minsize=650)
        root.grid_rowconfigure(0, weight=1)

        # ------------------------- LEFT CONTROL RAIL -------------------------
        left = tk.Frame(root, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)

        net_card = RoundedCard(left, height=130, fill=SURFACE, border=BORDER)
        net_card.grid(row=0, column=0, sticky="ew")
        net = net_card.inner
        net.grid_columnconfigure(0, weight=1)
        self._section_title(net, "Network").grid(row=0, column=0, sticky="w")
        self.network_label = self._label(net, interface_text(self.scan_targets),
                                         justify="left", wraplength=255)
        self.network_label.grid(row=1, column=0, sticky="ew", pady=(4, 7))
        self.scan_button = RoundedButton(net, "Scan network", self.start_scan,
                                         color=BLUE, hover=BLUE_HOVER, height=30)
        self.scan_button.grid(row=2, column=0, sticky="ew")
        self.progress = ttk.Progressbar(net, maximum=100,
                                        style="Vision.Horizontal.TProgressbar")
        self.progress.grid(row=3, column=0, sticky="ew", pady=(7, 0))

        devices_card = RoundedCard(left, fill=SURFACE, border=BORDER)
        devices_card.grid(row=1, column=0, sticky="nsew", pady=8)
        devices = devices_card.inner
        devices.grid_rowconfigure(1, weight=1)
        devices.grid_columnconfigure(0, weight=1)
        self._section_title(devices, "Devices").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.tree = ttk.Treeview(devices, columns=("ip", "mac", "interface"),
                                 show="headings", selectmode="browse", height=7,
                                 style="Vision.Treeview")
        for col, title, width, anchor in (
            ("ip", "IP address", 105, "center"),
            ("mac", "MAC", 115, "center"),
            ("interface", "Interface", 120, "w"),
        ):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=70, anchor=anchor, stretch=True)
        scroll = ttk.Scrollbar(devices, orient="vertical", command=self.tree.yview,
                               style="Vision.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns", padx=(3, 0))
        self.tree.bind("<<TreeviewSelect>>", self.device_selected)

        cam_card = RoundedCard(left, height=116, fill=SURFACE, border=BORDER)
        cam_card.grid(row=2, column=0, sticky="ew")
        cam = cam_card.inner
        cam.grid_columnconfigure((0, 1), weight=1)
        self._section_title(cam, "Camera").grid(row=0, column=0, columnspan=2, sticky="w")
        self.selected_var = tk.StringVar(value="Selected IP: None")
        tk.Label(cam, textvariable=self.selected_var, bg=SURFACE, fg=MUTED,
                 font=FONT_SMALL).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 7))
        RoundedButton(cam, "Connect", self.connect_camera, color=GREEN,
                      hover=GREEN_HOVER, height=30).grid(row=2, column=0, sticky="ew", padx=(0, 4))
        RoundedButton(cam, "Disconnect", self.disconnect_camera, color=SURFACE_3,
                      hover="#363D45", height=30).grid(row=2, column=1, sticky="ew", padx=(4, 0))

        yolo_card = RoundedCard(left, height=166, fill=SURFACE, border=BORDER)
        yolo_card.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ybox = yolo_card.inner
        ybox.grid_columnconfigure(0, weight=1)
        self._section_title(ybox, "Object detection").grid(row=0, column=0, sticky="w")

        self.yolo_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ybox, text="Enable YOLO", variable=self.yolo_var,
                       command=self.toggle_yolo, bg=SURFACE, fg=TEXT,
                       activebackground=SURFACE, activeforeground=TEXT,
                       selectcolor=SURFACE_2, font=FONT_SMALL,
                       highlightthickness=0, bd=0).grid(row=1, column=0, sticky="w", pady=(3, 5))

        model_row = tk.Frame(ybox, bg=SURFACE)
        model_row.grid(row=2, column=0, sticky="ew")
        model_row.grid_columnconfigure(0, weight=1)
        self.model_display_var = tk.StringVar(value="No model selected")
        self.model_entry = tk.Entry(model_row, textvariable=self.model_display_var,
                                    state="readonly", readonlybackground=SURFACE_2,
                                    fg=MUTED, relief="flat", font=FONT_SMALL,
                                    highlightthickness=1, highlightbackground=BORDER)
        self.model_entry.grid(row=0, column=0, sticky="ew", ipady=5)
        RoundedButton(model_row, "Browse", self.select_model, color=SURFACE_3,
                      hover="#363D45", width=68, height=29).grid(row=0, column=1, padx=(6, 0))

        opts = tk.Frame(ybox, bg=SURFACE)
        opts.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        opts.grid_columnconfigure((1, 3), weight=1)
        self.conf_var = tk.DoubleVar(value=YOLO_CONF)
        self.yolo_fps_var = tk.DoubleVar(value=YOLO_MAX_FPS)
        tk.Label(opts, text="Confidence", bg=SURFACE, fg=MUTED,
                 font=FONT_SMALL).grid(row=0, column=0, sticky="w")
        self.conf_spin = tk.Spinbox(opts, from_=0.05, to=0.95, increment=0.05,
                                    width=5, textvariable=self.conf_var,
                                    bg=SURFACE_2, fg=TEXT, insertbackground=TEXT,
                                    buttonbackground=SURFACE_3, relief="flat", font=FONT_SMALL)
        self.conf_spin.grid(row=0, column=1, sticky="w", padx=(5, 12), ipady=2)
        tk.Label(opts, text="FPS", bg=SURFACE, fg=MUTED,
                 font=FONT_SMALL).grid(row=0, column=2, sticky="w")
        self.fps_spin = tk.Spinbox(opts, from_=0.5, to=15, increment=0.5,
                                   width=5, textvariable=self.yolo_fps_var,
                                   bg=SURFACE_2, fg=TEXT, insertbackground=TEXT,
                                   buttonbackground=SURFACE_3, relief="flat", font=FONT_SMALL)
        self.fps_spin.grid(row=0, column=3, sticky="w", padx=(5, 0), ipady=2)

        # ------------------------- VIDEO WORKSPACE -------------------------
        right = tk.Frame(root, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        header = tk.Frame(right, bg=BG, height=34)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        header.grid_columnconfigure(0, weight=1)
        tk.Label(header, text="Live camera", bg=BG, fg=TEXT,
                 font=(FONT, 15, "bold")).grid(row=0, column=0, sticky="w")
        alarm_wrap = tk.Frame(header, bg=BG)
        alarm_wrap.grid(row=0, column=1, sticky="e")
        tk.Label(alarm_wrap, text="Alarm", bg=BG, fg=MUTED,
                 font=FONT_SMALL).pack(side="left", padx=(0, 5))
        self.alarm_led = LedIndicator(alarm_wrap, size=28)
        self.alarm_led.pack(side="left")

        video_card = RoundedCard(right, radius=10, fill=VIDEO_BG, border=BORDER,
                                 padding=5)
        video_card.grid(row=1, column=0, sticky="nsew")
        v = video_card.inner
        v.configure(bg=VIDEO_BG)
        v.grid_rowconfigure(0, weight=1)
        v.grid_columnconfigure(0, weight=1)
        self.video = tk.Label(v, text="No camera connected", bg=VIDEO_BG,
                              fg=MUTED, font=FONT_BODY)
        self.video.grid(row=0, column=0, sticky="nsew")

        info = tk.Frame(right, bg=BG, height=30)
        info.grid(row=2, column=0, sticky="ew", pady=(7, 0))
        info.grid_columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Ready")
        self.stats_var = tk.StringVar(value="")
        tk.Label(info, textvariable=self.status_var, bg=BG, fg=MUTED,
                 font=FONT_STATUS).grid(row=0, column=0, sticky="w")
        tk.Label(info, textvariable=self.stats_var, bg=BG, fg=MUTED,
                 font=FONT_STATUS).grid(row=0, column=1, sticky="e")

    def _set_camera_state(self, connected):
        self.camera_dot.itemconfigure(self.camera_dot_id,
                                      fill=GREEN if connected else "#565B62")

    def _set_detection_state(self, enabled):
        self.detect_dot.itemconfigure(self.detect_dot_id,
                                      fill=BLUE if enabled else "#565B62")

    def start_scan(self):
        if self.scanning:
            return
        if self.camera.connected:
            messagebox.showinfo("Camera", "Disconnect the camera before scanning.")
            return
        self.scan_targets = get_scan_interfaces()
        self.network_label.config(text=interface_text(self.scan_targets))
        self.tree.delete(*self.tree.get_children())
        self.scanning = True
        self.scan_button.set_enabled(False)
        self.progress["value"] = 0
        self.status_var.set("Scanning network...")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            devices = discover_devices(
                self.scan_targets,
                lambda value: self.events.put(("scan_progress", value)),
            )
            self.events.put(("scan_complete", devices))
        except Exception as exc:
            self.events.put(("scan_error", str(exc)))

    def device_selected(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        values = self.tree.item(selected[0], "values")
        self.selected_ip = str(values[0])
        self.selected_var.set(f"Selected IP: {self.selected_ip}")

    def connect_camera(self):
        if not self.selected_ip:
            messagebox.showwarning("Camera", "Scan and select the Ameba camera first.")
            return
        self.yolo_var.set(False)
        self.yolo.disable()
        self._set_detection_state(False)
        self._set_alarm(False)
        self.photo = None
        self.last_frame_id = -1
        self.video.configure(image="", text="Connecting...")
        self.camera.start(self.selected_ip)

    def disconnect_camera(self):
        self.yolo_var.set(False)
        self.yolo.disable()
        self._set_detection_state(False)
        self._set_alarm(False)
        self.camera.stop()
        self._set_camera_state(False)
        self.photo = None
        self.last_frame_id = -1
        self.video.configure(image="", text="No camera connected")
        self.status_var.set("Camera disconnected")

    def select_model(self):
        path = filedialog.askopenfilename(
            title="Select YOLO model",
            filetypes=[("YOLO model", "*.pt"), ("All files", "*.*")],
        )
        if path:
            import os
            self.model_path = path
            self.model_display_var.set(os.path.basename(path))

    def toggle_yolo(self):
        if not self.yolo_var.get():
            self.yolo.disable()
            self._set_detection_state(False)
            self._set_alarm(False)
            self.status_var.set("YOLO disabled")
            return
        if not self.camera.connected:
            self.yolo_var.set(False)
            messagebox.showwarning("YOLO", "Connect the camera first.")
            return
        path = self.model_path.strip()
        if not path:
            self.yolo_var.set(False)
            messagebox.showwarning("YOLO", "Select a .pt model first.")
            return
        try:
            conf = float(self.conf_var.get())
            max_fps = float(self.yolo_fps_var.get())
        except ValueError:
            self.yolo_var.set(False)
            messagebox.showerror("YOLO", "Invalid YOLO settings.")
            return
        self.yolo.configure(path, conf, max_fps)
        self.yolo.enable()
        self._set_detection_state(True)
        self.status_var.set("YOLO enabled")

    def _set_alarm(self, active):
        active = bool(active)
        if active != self.last_alarm_state:
            self.last_alarm_state = active
            self.alarm_led.set_active(active)

    @staticmethod
    def draw_detections(frame, detections, raw_w, raw_h):
        """Draw bounding boxes only; no class labels or confidence text."""
        h, w = frame.shape[:2]
        sx, sy = w / raw_w, h / raw_h
        for det in detections:
            x1, y1, x2, y2 = det["box"]
            p1 = (max(0, int(x1 * sx)), max(0, int(y1 * sy)))
            p2 = (min(w - 1, int(x2 * sx)), min(h - 1, int(y2 * sy)))
            if p2[0] <= p1[0] or p2[1] <= p1[1]:
                continue
            cv2.rectangle(frame, p1, p2, (0, 0, 255), 2)

    def update_video(self):
        try:
            frame, frame_id = self.camera.get_frame()
            if frame is None or frame_id == self.last_frame_id:
                return
            self.last_frame_id = frame_id
            raw_h, raw_w = frame.shape[:2]
            panel_w = max(self.video.winfo_width(), 320)
            panel_h = max(self.video.winfo_height(), 240)
            scale = min(panel_w / raw_w, panel_h / raw_h)
            out_w = max(1, int(raw_w * scale))
            out_h = max(1, int(raw_h * scale))
            display = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

            detections = []
            if self.yolo.is_enabled():
                detections = self.yolo.get_detections()
                self.draw_detections(display, detections, raw_w, raw_h)
            self._set_alarm(self.yolo.is_enabled() and bool(detections))

            rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
            self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))
            self.video.configure(image=self.photo, text="")
        finally:
            self.after(int(1000 / GUI_FPS), self.update_video)

    def process_events(self):
        try:
            while True:
                event, data = self.events.get_nowait()
                if event == "scan_progress":
                    self.progress["value"] = data
                elif event == "scan_complete":
                    self.scanning = False
                    self.scan_button.set_enabled(True)
                    self.progress["value"] = 100
                    self.tree.delete(*self.tree.get_children())
                    for d in data:
                        self.tree.insert("", "end",
                                         values=(d["ip"], d["mac"], d["interface"]))
                    self.status_var.set(f"Scan complete: {len(data)} device(s)")
                elif event == "scan_error":
                    self.scanning = False
                    self.scan_button.set_enabled(True)
                    messagebox.showerror("Scan error", data)
                elif event == "status":
                    self.status_var.set(data)
                elif event == "camera_connected":
                    self._set_camera_state(True)
                    self.status_var.set(f"Camera connected: {data}")
                elif event == "camera_error":
                    self._set_camera_state(False)
                    self._set_alarm(False)
                    self.status_var.set("Camera connection failed")
                    messagebox.showerror("Camera error", data)
                elif event == "camera_disconnected":
                    self._set_camera_state(False)
                    self._set_alarm(False)
                    self.status_var.set("Camera disconnected")
                elif event == "yolo_error":
                    self.yolo_var.set(False)
                    self.yolo.disable()
                    self._set_detection_state(False)
                    self._set_alarm(False)
                    messagebox.showerror("YOLO error", data)
        except queue.Empty:
            pass
        self.after(40, self.process_events)

    def update_stats(self):
        yolo_fps = f"{self.yolo.get_fps():.1f}" if self.yolo.is_enabled() else "OFF"
        self.stats_var.set(f"Camera {self.camera.get_fps():.1f} FPS   |   YOLO {yolo_fps}")
        self.after(500, self.update_stats)

    def close_app(self):
        self.yolo.stop()
        self.camera.stop()
        self.destroy()


if __name__ == "__main__":
    AmebaApp().mainloop()
