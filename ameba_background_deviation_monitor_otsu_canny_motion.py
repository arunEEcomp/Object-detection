import sys

import time

import socket

import ipaddress

import subprocess

import threading

import queue

import json




import tkinter as tk

from tkinter import ttk, messagebox



import requests

import numpy as np

import cv2

from PIL import Image, ImageTk





# ---------------------------- SETTINGS ----------------------------

CAMERA_URL = "http://{ip}/"

CAMERA_DECODE_FPS = 25.0

GUI_FPS = 25.0

# Background-deviation detection settings.
# The user enters MIN_OBJECT_AREA in the GUI before scanning.
DEFAULT_MIN_OBJECT_AREA = 2500      # pixels^2
PIXEL_DIFF_THRESHOLD = 28           # retained GUI/internal compatibility threshold
BACKGROUND_INIT_FRAMES = 30         # automatic startup calibration frames
BACKGROUND_ALPHA = 0.025            # slow background adaptation when scene is clear
CLEAR_FRAMES_FOR_UPDATE = 12        # wait before adapting background
ALARM_CLEAR_FRAMES = 3              # debounce alarm-off
MORPH_KERNEL_SIZE = 5

# Robust foreground + HSV/Lab shadow rejection thresholds.
# IMPORTANT: HSV is used mainly to IDENTIFY shadows, not to require every object
# to have a different hue. This allows brown/gray/low-saturation objects to be
# detected against gray industrial floors.
HSV_MIN_SAT_FOR_HUE = 30
HSV_SHADOW_HUE_MAX = 10
HSV_SHADOW_SAT_MAX = 30
HSV_SHADOW_VALUE_RATIO_MIN = 0.45
HSV_SHADOW_VALUE_RATIO_MAX = 0.97

# General foreground evidence.
FG_VALUE_DIFF_MIN = 20              # significant brightness/lightness change
FG_CHROMA_DIFF_MIN = 10.0           # Lab a/b chromatic distance
FG_RGB_CHROMA_DIFF_MIN = 0.040      # normalized RGB chromaticity distance
FG_TEXTURE_DIFF_MIN = 22.0          # texture is SUPPORT evidence only, never standalone
FG_PHOTOMETRIC_GROW_KERNEL = 5        # allow texture around genuine colour/value change
FG_NOISE_COMPONENT_RATIO = 0.10       # clean mask: reject components below 10% of object threshold
FG_MIN_COMPONENT_AREA = 80            # absolute floor for mask speck removal
FG_MIN_LOCAL_THICKNESS = 3.0          # reject line-like components with no substantial thickness

# A pixel is suppressed as shadow only when ALL shadow cues agree: it is darker,
# chromatically similar, and its local texture still matches the background.
SHADOW_LAB_CHROMA_MAX = 8.0
SHADOW_RGB_CHROMA_MAX = 0.035
SHADOW_TEXTURE_DIFF_MAX = 14.0

# Region validation. A detected contour must contain a useful fraction of genuine
# non-shadow change. This replaces the old hue/saturation-only region test.
FG_VALID_PIXEL_RATIO_MIN = 0.12

# Camera-motion compensation. Detection remains ACTIVE while the camera moves.
# The original background is geometrically registered to every incoming frame
# before any foreground subtraction is performed.
REG_ORB_FEATURES = 900
REG_MIN_MATCHES = 24
REG_RATIO_TEST = 0.74
REG_RANSAC_REPROJ = 3.0
REG_MIN_INLIER_RATIO = 0.38
REG_BORDER_ERODE = 9
REG_MAX_PERSPECTIVE = 0.004

# Residual component validation after registration. Misregistration normally
# produces broken, ragged islands along floor joints/stains rather than one
# coherent filled object region.
FG_MIN_SOLIDITY = 0.34
FG_MIN_EXTENT = 0.20
FG_MIN_FILL_RATIO = 0.22
FG_MAX_FRAGMENT_HOLES = 10

# Adaptive residual segmentation: Otsu determines the foreground threshold from
# each registered frame. Canny is used only to verify NEW object boundaries;
# existing background edges are removed with a spatial tolerance so floor joints
# and scratches cannot become foreground just because of small registration error.
OTSU_THRESHOLD_MIN = 12
OTSU_THRESHOLD_MAX = 72
OTSU_COLOR_WEIGHT = 2.2
CANNY_LOW = 45
CANNY_HIGH = 125
CANNY_BG_EDGE_TOLERANCE = 7
CANNY_EDGE_SUPPORT_DILATE = 5
FG_CONSENSUS_CLOSE_KERNEL = 9
FG_CONSENSUS_OPEN_KERNEL = 3
FG_MIN_OTSU_OVERLAP = 0.30
FG_MIN_NEW_EDGE_RATIO = 0.008
FG_WIDE_ARTIFACT_RATIO = 0.82
FG_WIDE_ARTIFACT_MAX_HEIGHT_RATIO = 0.18


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





# ---------------------- BACKGROUND DETECTION ----------------------
class BackgroundDetector:
    """Detect substantial colour deviations while suppressing cast shadows.

    The reference is stored as a colour BGR image. Foreground evidence combines
    brightness, colour/chromaticity and local texture. HSV/Lab are used to reject
    cast shadows only when the changed pixels retain the background chromaticity
    and texture while becoming darker.
    """

    def __init__(self, camera, events):
        self.camera = camera
        self.events = events
        self.stop_event = threading.Event()
        self.enabled = threading.Event()
        self.settings_lock = threading.Lock()
        self.result_lock = threading.Lock()
        self.fps_lock = threading.Lock()

        self.min_area = float(DEFAULT_MIN_OBJECT_AREA)
        self.pixel_threshold = int(PIXEL_DIFF_THRESHOLD)

        # Float32 BGR running background.
        self.background = None
        self.init_accum = None
        self.init_count = 0
        self.clear_count = 0
        self.last_frame_id = -1

        # Camera-motion state. A moving camera invalidates pixel-wise background
        # subtraction, so alarms are inhibited until the view is stationary again.
        self.prev_motion_gray = None
        self.camera_motion_active = False
        self.motion_stable_count = 0

        self.boxes = []
        self.mask = None
        self.object_present = False
        self.largest_area = 0.0

        self.fps = 0.0
        self.fps_count = 0
        self.fps_t0 = time.perf_counter()

        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def configure(self, min_area, pixel_threshold=PIXEL_DIFF_THRESHOLD):
        with self.settings_lock:
            self.min_area = max(1.0, float(min_area))
            self.pixel_threshold = max(1, min(255, int(pixel_threshold)))

    def reset_background(self):
        with self.result_lock:
            self.background = None
            self.init_accum = None
            self.init_count = 0
            self.clear_count = 0
            self.prev_motion_gray = None
            self.camera_motion_active = False
            self.motion_stable_count = 0
            self.boxes = []
            self.mask = None
            self.object_present = False
            self.largest_area = 0.0
        self.events.put(("background_status", "Calibrating colour background automatically..."))

    def enable(self):
        self.reset_background()
        self.enabled.set()

    def disable(self):
        self.enabled.clear()
        with self.result_lock:
            self.boxes = []
            self.mask = None
            self.object_present = False
            self.largest_area = 0.0

    def stop(self):
        self.stop_event.set()
        self.enabled.set()
        if self.thread.is_alive():
            self.thread.join(timeout=0.7)

    def is_enabled(self):
        return self.enabled.is_set()

    def is_ready(self):
        with self.result_lock:
            return self.background is not None

    def get_result(self):
        with self.result_lock:
            mask = None if self.mask is None else self.mask.copy()
            return {
                "boxes": list(self.boxes),
                "mask": mask,
                "object_present": bool(self.object_present),
                "largest_area": float(self.largest_area),
                "ready": self.background is not None,
                "init_count": int(self.init_count),
            }

    def get_fps(self):
        with self.fps_lock:
            return self.fps

    def _tick_fps(self):
        with self.fps_lock:
            self.fps_count += 1
            now = time.perf_counter()
            elapsed = now - self.fps_t0
            if elapsed >= 1.0:
                self.fps = self.fps_count / elapsed
                self.fps_count = 0
                self.fps_t0 = now

    @staticmethod
    def _prepare_color(frame):
        # Mild blur suppresses MJPEG block noise without destroying object edges.
        return cv2.GaussianBlur(frame, (5, 5), 0)

    @staticmethod
    def _prepare_motion_gray(frame):
        """Prepare a low-noise grayscale frame for global camera-motion tracking."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        if w > CAMERA_MOTION_DOWNSCALE_WIDTH:
            scale = CAMERA_MOTION_DOWNSCALE_WIDTH / float(w)
            gray = cv2.resize(
                gray,
                (CAMERA_MOTION_DOWNSCALE_WIDTH, max(1, int(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
        return cv2.GaussianBlur(gray, (5, 5), 0)

    @staticmethod
    def _estimate_camera_motion(prev_gray, cur_gray):
        """Return (moving, translation_px, rotation_deg, inlier_ratio).

        Sparse Lucas-Kanade optical flow is fit with a RANSAC partial-affine
        transform. A moving object normally occupies only a minority of tracked
        points, whereas camera motion moves most static scene features coherently.
        """
        if prev_gray is None or cur_gray is None or prev_gray.shape != cur_gray.shape:
            return False, 0.0, 0.0, 0.0

        pts0 = cv2.goodFeaturesToTrack(
            prev_gray,
            maxCorners=CAMERA_MOTION_MAX_FEATURES,
            qualityLevel=0.015,
            minDistance=8,
            blockSize=7,
        )
        if pts0 is None or len(pts0) < CAMERA_MOTION_MIN_FEATURES:
            return False, 0.0, 0.0, 0.0

        pts1, status, _err = cv2.calcOpticalFlowPyrLK(
            prev_gray, cur_gray, pts0, None,
            winSize=(21, 21), maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 25, 0.01),
        )
        if pts1 is None or status is None:
            return False, 0.0, 0.0, 0.0

        ok = status.reshape(-1).astype(bool)
        p0 = pts0.reshape(-1, 2)[ok]
        p1 = pts1.reshape(-1, 2)[ok]
        if len(p0) < CAMERA_MOTION_MIN_FEATURES:
            return False, 0.0, 0.0, 0.0

        M, inliers = cv2.estimateAffinePartial2D(
            p0, p1, method=cv2.RANSAC, ransacReprojThreshold=2.0,
            maxIters=1000, confidence=0.99, refineIters=10,
        )
        if M is None:
            return False, 0.0, 0.0, 0.0

        if inliers is None:
            inlier_ratio = 0.0
        else:
            inlier_ratio = float(np.mean(inliers.reshape(-1) > 0))

        a, b = float(M[0, 0]), float(M[0, 1])
        dx, dy = float(M[0, 2]), float(M[1, 2])
        translation = float(np.hypot(dx, dy))
        rotation_deg = abs(float(np.degrees(np.arctan2(b, a))))

        coherent = inlier_ratio >= CAMERA_MOTION_INLIER_RATIO_MIN
        moving = coherent and (
            translation >= CAMERA_MOTION_TRANSLATION_PX
            or rotation_deg >= CAMERA_MOTION_ROTATION_DEG
        )
        return bool(moving), translation, rotation_deg, inlier_ratio

    @staticmethod
    def _align_background_to_current(background_bgr, current_bgr):
        """Register the stored background into the current camera viewpoint.

        ORB features are matched between the reference background and the current
        frame. A RANSAC homography absorbs camera translation, rotation and modest
        perspective change. The returned valid mask excludes warped borders where
        there is no genuine reference information.
        """
        h, w = current_bgr.shape[:2]
        bg_gray = cv2.cvtColor(background_bgr, cv2.COLOR_BGR2GRAY)
        cur_gray = cv2.cvtColor(current_bgr, cv2.COLOR_BGR2GRAY)

        orb = cv2.ORB_create(
            nfeatures=REG_ORB_FEATURES,
            scaleFactor=1.2,
            nlevels=8,
            edgeThreshold=19,
            patchSize=31,
            fastThreshold=12,
        )
        kp_bg, des_bg = orb.detectAndCompute(bg_gray, None)
        kp_cur, des_cur = orb.detectAndCompute(cur_gray, None)
        if des_bg is None or des_cur is None or len(kp_bg) < REG_MIN_MATCHES or len(kp_cur) < REG_MIN_MATCHES:
            valid = np.ones((h, w), dtype=np.uint8) * 255
            return background_bgr, valid, False, 0.0

        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        pairs = matcher.knnMatch(des_bg, des_cur, k=2)
        good = []
        for pair in pairs:
            if len(pair) != 2:
                continue
            m, n = pair
            if m.distance < REG_RATIO_TEST * n.distance:
                good.append(m)

        if len(good) < REG_MIN_MATCHES:
            valid = np.ones((h, w), dtype=np.uint8) * 255
            return background_bgr, valid, False, 0.0

        src = np.float32([kp_bg[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kp_cur[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        H, inliers = cv2.findHomography(
            src, dst, cv2.RANSAC, REG_RANSAC_REPROJ,
            maxIters=2000, confidence=0.995,
        )
        if H is None or inliers is None:
            valid = np.ones((h, w), dtype=np.uint8) * 255
            return background_bgr, valid, False, 0.0

        inlier_ratio = float(np.mean(inliers.reshape(-1) > 0))
        if inlier_ratio < REG_MIN_INLIER_RATIO:
            valid = np.ones((h, w), dtype=np.uint8) * 255
            return background_bgr, valid, False, inlier_ratio

        H = H / max(abs(float(H[2, 2])), 1e-8)
        # Reject numerically implausible transforms. This prevents a bad feature
        # match from warping the floor into a false foreground field.
        if abs(float(H[2, 0])) > REG_MAX_PERSPECTIVE or abs(float(H[2, 1])) > REG_MAX_PERSPECTIVE:
            valid = np.ones((h, w), dtype=np.uint8) * 255
            return background_bgr, valid, False, inlier_ratio

        aligned = cv2.warpPerspective(
            background_bgr, H, (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0),
        )
        ref_valid = np.ones(background_bgr.shape[:2], dtype=np.uint8) * 255
        valid = cv2.warpPerspective(
            ref_valid, H, (w, h),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        if REG_BORDER_ERODE > 0:
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (REG_BORDER_ERODE, REG_BORDER_ERODE))
            valid = cv2.erode(valid, k, iterations=1)
        return aligned, valid, True, inlier_ratio

    @staticmethod
    def _circular_hue_diff(h1, h2):
        """Circular hue distance for OpenCV H in the range [0, 179]."""
        d = cv2.absdiff(h1, h2).astype(np.int16)
        return np.minimum(d, 180 - d).astype(np.uint8)

    @staticmethod
    def _compensate_global_value(cur_hsv, bg_hsv):
        """Compensate only a frame-wide V shift; never alter H or S.

        This handles camera exposure drift while preserving the chromatic channels
        used to decide whether a region is a real object or just illumination.
        """
        out = cur_hsv.copy()
        cur_v = cur_hsv[:, :, 2].astype(np.int16)
        bg_v = bg_hsv[:, :, 2].astype(np.int16)
        delta = float(np.median(cur_v - bg_v))
        corrected_v = cur_v.astype(np.float32) - delta
        out[:, :, 2] = np.clip(corrected_v, 0, 255).astype(np.uint8)
        return out

    def _initialise_background(self, color):
        if self.init_accum is None:
            self.init_accum = color.astype(np.float32)
            self.init_count = 1
        else:
            self.init_accum += color.astype(np.float32)
            self.init_count += 1

        if self.init_count >= BACKGROUND_INIT_FRAMES:
            self.background = self.init_accum / float(self.init_count)
            self.init_accum = None
            self.clear_count = 0
            self.events.put(("background_ready", None))

    @staticmethod
    def _build_hsv_object_mask(current_bgr, background_bgr):
        """Return (object_mask, evidence_mask, shadow_mask).

        The detector first finds substantial scene change using brightness, Lab
        chroma, normalized RGB chromaticity, and local texture. HSV/Lab are then
        used to remove pixels that behave like a cast shadow.

        This is intentionally different from a colour-only detector: a real
        brown/gray object may have low saturation and therefore must not be rejected
        simply because H/S are close to the concrete/background colour.
        """
        cur = current_bgr.astype(np.float32)
        bg = background_bgr.astype(np.float32)

        cur_hsv_raw = cv2.cvtColor(current_bgr, cv2.COLOR_BGR2HSV)
        bg_hsv = cv2.cvtColor(background_bgr, cv2.COLOR_BGR2HSV)
        cur_hsv = BackgroundDetector._compensate_global_value(cur_hsv_raw, bg_hsv)

        ch, cs, cv = cv2.split(cur_hsv)
        bh, bs, bv = cv2.split(bg_hsv)
        hue_diff = BackgroundDetector._circular_hue_diff(ch, bh).astype(np.float32)
        sat_diff = cv2.absdiff(cs, bs).astype(np.float32)
        val_diff = cv2.absdiff(cv, bv).astype(np.float32)

        # ---- Lab chromatic distance: useful even when HSV saturation is modest.
        cur_lab = cv2.cvtColor(current_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
        bg_lab = cv2.cvtColor(background_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
        da = cur_lab[:, :, 1] - bg_lab[:, :, 1]
        db = cur_lab[:, :, 2] - bg_lab[:, :, 2]
        lab_chroma_diff = np.sqrt(da * da + db * db)

        # ---- Normalized RGB chromaticity. Multiplicative shadows largely preserve
        # these channel ratios, while a different surface/object generally does not.
        cur_sum = np.maximum(np.sum(cur, axis=2), 1.0)
        bg_sum = np.maximum(np.sum(bg, axis=2), 1.0)
        cur_norm = cur / cur_sum[:, :, None]
        bg_norm = bg / bg_sum[:, :, None]
        rgb_chroma_diff = np.sqrt(np.sum((cur_norm - bg_norm) ** 2, axis=2))

        # ---- Local texture/edge evidence. A cast shadow usually preserves the floor
        # texture, whereas an inserted solid object introduces/occludes edges/texture.
        cur_gray = cv2.cvtColor(current_bgr, cv2.COLOR_BGR2GRAY)
        bg_gray = cv2.cvtColor(background_bgr, cv2.COLOR_BGR2GRAY)
        cgx = cv2.Sobel(cur_gray, cv2.CV_32F, 1, 0, ksize=3)
        cgy = cv2.Sobel(cur_gray, cv2.CV_32F, 0, 1, ksize=3)
        bgx = cv2.Sobel(bg_gray, cv2.CV_32F, 1, 0, ksize=3)
        bgy = cv2.Sobel(bg_gray, cv2.CV_32F, 0, 1, ksize=3)
        cur_grad = cv2.magnitude(cgx, cgy)
        bg_grad = cv2.magnitude(bgx, bgy)
        texture_diff = cv2.absdiff(cur_grad, bg_grad)

        # Hue should only contribute where hue is meaningful.
        hue_reliable = np.maximum(cs, bs) >= HSV_MIN_SAT_FOR_HUE
        hue_changed = hue_reliable & (hue_diff >= 10.0)

        # General foreground evidence. Any of these can support an object.
        chromatic_change = (
            (lab_chroma_diff >= FG_CHROMA_DIFF_MIN)
            | (rgb_chroma_diff >= FG_RGB_CHROMA_DIFF_MIN)
            | hue_changed
            | (sat_diff >= 28.0)
        )
        structural_change = texture_diff >= FG_TEXTURE_DIFF_MIN
        brightness_change = val_diff >= FG_VALUE_DIFF_MIN

        # IMPORTANT: texture/edges are not allowed to create foreground on their own.
        # Floor joints, scratches and JPEG/camera registration jitter generate large
        # Sobel differences even though no object has appeared. Start from genuine
        # photometric change, then let nearby texture support the object's boundary.
        photometric_change = brightness_change | chromatic_change
        support_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (FG_PHOTOMETRIC_GROW_KERNEL, FG_PHOTOMETRIC_GROW_KERNEL),
        )
        nearby_photometric = cv2.dilate(
            np.where(photometric_change, 255, 0).astype(np.uint8),
            support_kernel,
            iterations=1,
        ) > 0
        supported_texture = structural_change & nearby_photometric
        candidate = photometric_change | supported_texture

        # True shadow test: darker AND same chromaticity AND same local texture.
        bv_safe = np.maximum(bv.astype(np.float32), 1.0)
        value_ratio = cv.astype(np.float32) / bv_safe
        darker = (
            (value_ratio >= HSV_SHADOW_VALUE_RATIO_MIN)
            & (value_ratio <= HSV_SHADOW_VALUE_RATIO_MAX)
        )

        hue_similar = (~hue_reliable) | (hue_diff <= HSV_SHADOW_HUE_MAX)
        hsv_similar = hue_similar & (sat_diff <= HSV_SHADOW_SAT_MAX)
        chroma_similar = (
            (lab_chroma_diff <= SHADOW_LAB_CHROMA_MAX)
            & (rgb_chroma_diff <= SHADOW_RGB_CHROMA_MAX)
        )
        texture_similar = texture_diff <= SHADOW_TEXTURE_DIFF_MAX

        strong_shadow = darker & hsv_similar & chroma_similar & texture_similar

        # Preserve object boundaries/texture even if the object's colour is close to
        # the background. Only pixels satisfying the full shadow model are removed.
        object_pixels = candidate & (~strong_shadow)

        # Remove extremely weak single-channel/compression changes.
        evidence = (
            chromatic_change
            | supported_texture
            | (val_diff >= (FG_VALUE_DIFF_MIN + 6))
        )
        object_pixels &= evidence

        mask = np.where(object_pixels, 255, 0).astype(np.uint8)
        evidence_mask = np.where(evidence & (~strong_shadow), 255, 0).astype(np.uint8)
        shadow_mask = np.where(strong_shadow, 255, 0).astype(np.uint8)
        return mask, evidence_mask, shadow_mask


    @staticmethod
    def _refine_with_otsu_canny(current_bgr, background_bgr, base_mask, valid_overlap):
        """Build a foreground mask from registered residual consensus.

        Otsu performs adaptive foreground/background separation on a residual
        image containing intensity and Lab-chroma change. Canny contributes only
        *novel* boundaries: current edges that do not correspond to a nearby edge
        in the aligned background. Therefore floor seams/scratches displaced by a
        few pixels are suppressed rather than promoted to objects.
        """
        cur_gray = cv2.cvtColor(current_bgr, cv2.COLOR_BGR2GRAY)
        bg_gray = cv2.cvtColor(background_bgr, cv2.COLOR_BGR2GRAY)
        cur_gray = cv2.GaussianBlur(cur_gray, (5, 5), 0)
        bg_gray = cv2.GaussianBlur(bg_gray, (5, 5), 0)

        # Photometric residual.
        gray_diff = cv2.absdiff(cur_gray, bg_gray).astype(np.float32)

        # Add chromatic residual so a similarly bright but differently coloured
        # object is still separable by Otsu.
        cur_lab = cv2.cvtColor(current_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
        bg_lab = cv2.cvtColor(background_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
        da = cur_lab[:, :, 1] - bg_lab[:, :, 1]
        db = cur_lab[:, :, 2] - bg_lab[:, :, 2]
        chroma = np.sqrt(da * da + db * db)
        residual = np.maximum(gray_diff, chroma * float(OTSU_COLOR_WEIGHT))
        residual = np.clip(residual, 0, 255).astype(np.uint8)
        residual = cv2.GaussianBlur(residual, (5, 5), 0)
        residual[valid_overlap == 0] = 0

        valid_pixels = residual[valid_overlap > 0]
        if valid_pixels.size < 64:
            return np.zeros_like(base_mask), np.zeros_like(base_mask), 0.0

        # Get the automatic Otsu threshold, then clamp it so tiny camera/JPEG
        # fluctuations cannot lower the threshold excessively.
        otsu_value, _ = cv2.threshold(
            valid_pixels.reshape(-1, 1), 0, 255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        otsu_value = float(np.clip(otsu_value, OTSU_THRESHOLD_MIN, OTSU_THRESHOLD_MAX))
        otsu_mask = np.where(residual >= otsu_value, 255, 0).astype(np.uint8)
        otsu_mask[valid_overlap == 0] = 0

        # Canny boundary consensus. Suppress any current edge having a nearby
        # counterpart in the ALIGNED background; those are background structure.
        cur_edges = cv2.Canny(cur_gray, CANNY_LOW, CANNY_HIGH, L2gradient=True)
        bg_edges = cv2.Canny(bg_gray, CANNY_LOW, CANNY_HIGH, L2gradient=True)
        tol_k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (2 * CANNY_BG_EDGE_TOLERANCE + 1, 2 * CANNY_BG_EDGE_TOLERANCE + 1),
        )
        bg_edge_neighbourhood = cv2.dilate(bg_edges, tol_k, iterations=1)
        novel_edges = cv2.bitwise_and(cur_edges, cv2.bitwise_not(bg_edge_neighbourhood))
        novel_edges[valid_overlap == 0] = 0

        support_k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (CANNY_EDGE_SUPPORT_DILATE, CANNY_EDGE_SUPPORT_DILATE),
        )
        edge_support = cv2.dilate(novel_edges, support_k, iterations=1)

        # Three-way consensus. Otsu is the adaptive foreground/background split;
        # HSV/Lab base_mask confirms that the residual is not a shadow; Canny can
        # recover a coherent object boundary but can NEVER create foreground alone.
        otsu_bool = otsu_mask > 0
        base_bool = base_mask > 0
        edge_bool = edge_support > 0
        consensus = otsu_bool & (base_bool | edge_bool)
        mask = np.where(consensus, 255, 0).astype(np.uint8)

        # Connect the interior of coherent objects, then remove isolated fragments.
        close_k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (FG_CONSENSUS_CLOSE_KERNEL, FG_CONSENSUS_CLOSE_KERNEL),
        )
        open_k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (FG_CONSENSUS_OPEN_KERNEL, FG_CONSENSUS_OPEN_KERNEL),
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k, iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_k, iterations=1)
        mask[valid_overlap == 0] = 0
        return mask, novel_edges, otsu_value

    def _worker(self):
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE)
        )

        while not self.stop_event.is_set():
            if not self.enabled.wait(0.1):
                continue
            if self.stop_event.is_set():
                break

            frame, frame_id = self.camera.get_frame()
            if frame is None or frame_id == self.last_frame_id:
                time.sleep(0.008)
                continue
            self.last_frame_id = frame_id

            try:
                color = self._prepare_color(frame)

                # -------- CAMERA-MOTION COMPENSATION --------
                # Detection remains active while the camera moves. We first warp
                # the stored reference background into the CURRENT viewpoint, then
                # calculate foreground only from residual scene change.
                with self.result_lock:
                    if self.background is None:
                        self._initialise_background(color)
                        self.mask = np.zeros(color.shape[:2], dtype=np.uint8)
                        self.boxes = []
                        self.object_present = False
                        self.largest_area = 0.0
                        self._tick_fps()
                        continue
                    bg_bgr = cv2.convertScaleAbs(self.background)

                aligned_bg, valid_overlap, registration_ok, reg_inlier_ratio = (
                    self._align_background_to_current(bg_bgr, color)
                )

                with self.settings_lock:
                    min_area = float(self.min_area)

                base_mask, evidence_mask, _shadow_mask = self._build_hsv_object_mask(
                    color, aligned_bg
                )

                # Never evaluate pixels that have no valid warped-background support.
                base_mask[valid_overlap == 0] = 0
                evidence_mask[valid_overlap == 0] = 0

                # Adaptive foreground/background segmentation. Otsu finds the
                # frame-specific residual threshold, while Canny verifies only NEW
                # boundaries after registered background edges have been removed.
                # This is the main rejection stage for arbitrary moving-camera patches.
                mask, novel_edges, otsu_threshold = self._refine_with_otsu_canny(
                    color, aligned_bg, base_mask, valid_overlap
                )

                # Component-level mask cleanup. This affects the displayed mask as
                # well as detection, so random white points and thin floor lines are
                # actually removed rather than merely ignored later.
                num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
                    mask, connectivity=8
                )
                cleaned_mask = np.zeros_like(mask)
                component_area_min = max(
                    float(FG_MIN_COMPONENT_AREA),
                    float(min_area) * float(FG_NOISE_COMPONENT_RATIO),
                )

                for label in range(1, num_labels):
                    area_cc = float(stats[label, cv2.CC_STAT_AREA])
                    if area_cc < component_area_min:
                        continue

                    xcc = int(stats[label, cv2.CC_STAT_LEFT])
                    ycc = int(stats[label, cv2.CC_STAT_TOP])
                    wcc = int(stats[label, cv2.CC_STAT_WIDTH])
                    hcc = int(stats[label, cv2.CC_STAT_HEIGHT])
                    if wcc <= 0 or hcc <= 0:
                        continue

                    component = (labels[ycc:ycc+hcc, xcc:xcc+wcc] == label).astype(np.uint8) * 255

                    # Distance transform measures the radius of the thickest part of
                    # the component. A scratch/grid line can be very long and still
                    # have a large area, but its maximum local thickness stays small.
                    dist = cv2.distanceTransform(component, cv2.DIST_L2, 5)
                    max_radius = float(dist.max()) if dist.size else 0.0
                    if max_radius < FG_MIN_LOCAL_THICKNESS:
                        continue

                    cleaned_mask[labels == label] = 255

                mask = cleaned_mask

                contours, _ = cv2.findContours(
                    mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )

                boxes = []
                largest_area = 0.0
                for contour in contours:
                    area = float(cv2.contourArea(contour))
                    if area < min_area:
                        continue

                    x, y, w, h = cv2.boundingRect(contour)
                    if w <= 0 or h <= 0:
                        continue

                    # Reject ragged residual patches caused by imperfect image
                    # registration. Real objects normally occupy a coherent filled
                    # region; camera-jitter residuals are sparse/branched islands.
                    hull = cv2.convexHull(contour)
                    hull_area = float(cv2.contourArea(hull))
                    bbox_area = float(w * h)
                    solidity = area / max(hull_area, 1.0)
                    extent = area / max(bbox_area, 1.0)
                    if solidity < FG_MIN_SOLIDITY or extent < FG_MIN_EXTENT:
                        continue

                    # Filled-pixel ratio uses the CLEANED binary component rather than
                    # only contour geometry. This specifically rejects arbitrary
                    # broken patches such as those seen while the camera is moving.
                    region_binary = mask[y:y+h, x:x+w] > 0
                    fill_ratio = float(np.count_nonzero(region_binary)) / max(bbox_area, 1.0)
                    if fill_ratio < FG_MIN_FILL_RATIO:
                        continue

                    # A very wide, shallow residual is characteristic of registration
                    # error along a floor seam/frame boundary, not a discrete object.
                    frame_h, frame_w = mask.shape[:2]
                    if (
                        (w / max(float(frame_w), 1.0)) >= FG_WIDE_ARTIFACT_RATIO
                        and (h / max(float(frame_h), 1.0)) <= FG_WIDE_ARTIFACT_MAX_HEIGHT_RATIO
                    ):
                        continue

                    # Require some genuinely NEW Canny boundary inside/around the
                    # component. Existing floor edges have already been cancelled
                    # against the aligned background with spatial tolerance.
                    edge_roi = novel_edges[y:y+h, x:x+w]
                    edge_ratio = float(np.count_nonzero(edge_roi)) / max(bbox_area, 1.0)
                    if edge_ratio < FG_MIN_NEW_EDGE_RATIO:
                        continue

                    # Second-stage region validation. Reject a contour only when
                    # too little of its area contains genuine non-shadow evidence.
                    # This no longer requires a strong hue/saturation difference.
                    contour_roi = np.zeros((h, w), dtype=np.uint8)
                    shifted = contour.copy()
                    shifted[:, 0, 0] -= x
                    shifted[:, 0, 1] -= y
                    cv2.drawContours(contour_roi, [shifted], -1, 255, thickness=-1)

                    evidence_roi = evidence_mask[y:y+h, x:x+w]
                    inside = contour_roi > 0
                    inside_count = int(np.count_nonzero(inside))
                    if inside_count <= 0:
                        continue
                    evidence_count = int(np.count_nonzero((evidence_roi > 0) & inside))
                    evidence_ratio = evidence_count / float(inside_count)
                    if evidence_ratio < FG_VALID_PIXEL_RATIO_MIN:
                        continue

                    boxes.append((x, y, x + w, y + h, area))
                    largest_area = max(largest_area, area)

                object_present = bool(boxes)

                with self.result_lock:
                    self.mask = mask
                    self.boxes = boxes
                    self.object_present = object_present
                    self.largest_area = largest_area

                    if object_present:
                        # Freeze the background while a valid object is present.
                        self.clear_count = 0
                    else:
                        self.clear_count += 1
                        if self.clear_count >= CLEAR_FRAMES_FOR_UPDATE:
                            # Keep the original reference coordinate system fixed.
                            # Updating it with a geometrically shifted live frame would
                            # itself create registration artifacts.
                            self.clear_count = CLEAR_FRAMES_FOR_UPDATE

                self._tick_fps()

            except Exception as exc:
                self.events.put(("detector_error", str(exc)))
                time.sleep(0.05)


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
        self.detector = BackgroundDetector(self.camera, self.events)

        self.selected_ip = None
        self.photo = None
        self.mask_photo = None
        self.last_frame_id = -1
        self.scanning = False
        self.last_alarm_state = False
        self.alarm_clear_count = 0
        self.validated_min_area = float(DEFAULT_MIN_OBJECT_AREA)

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

        left = tk.Frame(root, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)

        # Object-size threshold is deliberately placed before Scan Network.
        net_card = RoundedCard(left, height=186, fill=SURFACE, border=BORDER)
        net_card.grid(row=0, column=0, sticky="ew")
        net = net_card.inner
        net.grid_columnconfigure(0, weight=1)
        self._section_title(net, "Network & detection threshold").grid(row=0, column=0, sticky="w")
        self.network_label = self._label(net, interface_text(self.scan_targets),
                                         justify="left", wraplength=255)
        self.network_label.grid(row=1, column=0, sticky="ew", pady=(4, 6))

        threshold_row = tk.Frame(net, bg=SURFACE)
        threshold_row.grid(row=2, column=0, sticky="ew", pady=(0, 7))
        threshold_row.grid_columnconfigure(1, weight=1)
        tk.Label(threshold_row, text="Min object area (px²)", bg=SURFACE, fg=MUTED,
                 font=FONT_SMALL).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.min_area_var = tk.StringVar(value=str(DEFAULT_MIN_OBJECT_AREA))
        self.min_area_entry = tk.Entry(
            threshold_row, textvariable=self.min_area_var,
            bg=SURFACE_2, fg=TEXT, insertbackground=TEXT,
            relief="flat", justify="center", font=FONT_SMALL,
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.min_area_entry.grid(row=0, column=1, sticky="ew", ipady=4)

        self.scan_button = RoundedButton(net, "Scan network", self.start_scan,
                                         color=BLUE, hover=BLUE_HOVER, height=30)
        self.scan_button.grid(row=3, column=0, sticky="ew")
        self.progress = ttk.Progressbar(net, maximum=100,
                                        style="Vision.Horizontal.TProgressbar")
        self.progress.grid(row=4, column=0, sticky="ew", pady=(7, 0))

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

        detect_card = RoundedCard(left, height=132, fill=SURFACE, border=BORDER)
        detect_card.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        dbox = detect_card.inner
        dbox.grid_columnconfigure(0, weight=1)
        self._section_title(dbox, "Background deviation detection").grid(row=0, column=0, sticky="w")
        self.detector_state_var = tk.StringVar(value="Waiting for camera")
        tk.Label(dbox, textvariable=self.detector_state_var, bg=SURFACE, fg=MUTED,
                 font=FONT_SMALL, justify="left", wraplength=255).grid(
                     row=1, column=0, sticky="w", pady=(5, 4)
                 )
        tk.Label(
            dbox,
            text="Background is captured automatically and adapts only while the scene is clear.",
            bg=SURFACE, fg=MUTED, font=(FONT, 9), justify="left", wraplength=255,
        ).grid(row=2, column=0, sticky="w")

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

        views = tk.Frame(right, bg=BG)
        views.grid(row=1, column=0, sticky="nsew")
        views.grid_rowconfigure(0, weight=1)
        views.grid_columnconfigure(0, weight=3, minsize=480)
        views.grid_columnconfigure(1, weight=1, minsize=220)

        video_card = RoundedCard(views, radius=10, fill=VIDEO_BG, border=BORDER, padding=5)
        video_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        v = video_card.inner
        v.configure(bg=VIDEO_BG)
        v.grid_rowconfigure(0, weight=1)
        v.grid_columnconfigure(0, weight=1)
        self.video = tk.Label(v, text="No camera connected", bg=VIDEO_BG,
                              fg=MUTED, font=FONT_BODY)
        self.video.grid(row=0, column=0, sticky="nsew")

        mask_card = RoundedCard(views, radius=10, fill=VIDEO_BG, border=BORDER, padding=5)
        mask_card.grid(row=0, column=1, sticky="nsew")
        m = mask_card.inner
        m.configure(bg=VIDEO_BG)
        m.grid_rowconfigure(1, weight=1)
        m.grid_columnconfigure(0, weight=1)
        tk.Label(m, text="Deviation mask", bg=VIDEO_BG, fg=TEXT,
                 font=FONT_BODY_BOLD).grid(row=0, column=0, sticky="w", padx=5, pady=(3, 4))
        self.mask_view = tk.Label(m, text="Waiting for background", bg=VIDEO_BG,
                                  fg=MUTED, font=FONT_SMALL)
        self.mask_view.grid(row=1, column=0, sticky="nsew")

        # Large centre alarm prompt. It overlays the live-camera panel and is
        # hidden automatically as soon as the object clears.
        self.alarm_prompt = tk.Frame(
            v, bg="#B71C1C", highlightthickness=3, highlightbackground="#FF6B6B"
        )
        tk.Label(self.alarm_prompt, text="ALARM", bg="#B71C1C", fg="white",
                 font=(FONT, 34, "bold")).pack(padx=55, pady=(22, 2))
        tk.Label(self.alarm_prompt, text="OBJECT DETECTED", bg="#B71C1C", fg="white",
                 font=(FONT, 20, "bold")).pack(padx=55, pady=(0, 22))

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
        self.camera_dot.itemconfigure(
            self.camera_dot_id, fill=GREEN if connected else "#565B62"
        )

    def _set_detection_state(self, enabled):
        self.detect_dot.itemconfigure(
            self.detect_dot_id, fill=BLUE if enabled else "#565B62"
        )

    def _read_min_area(self, show_error=True):
        try:
            value = float(self.min_area_var.get().strip())
            if value <= 0:
                raise ValueError
            self.validated_min_area = value
            return value
        except Exception:
            if show_error:
                messagebox.showerror(
                    "Object size threshold",
                    "Enter a positive minimum object area in pixels² before scanning.\n\n"
                    "Example: 2500",
                )
            return None

    def start_scan(self):
        if self.scanning:
            return
        if self.camera.connected:
            messagebox.showinfo("Camera", "Disconnect the camera before scanning.")
            return
        min_area = self._read_min_area(show_error=True)
        if min_area is None:
            return
        self.detector.configure(min_area)
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
        min_area = self._read_min_area(show_error=True)
        if min_area is None:
            return
        self.detector.configure(min_area)
        self.detector.disable()
        self._set_detection_state(False)
        self._set_alarm(False)
        self.detector_state_var.set("Waiting for camera")
        self.photo = None
        self.mask_photo = None
        self.last_frame_id = -1
        self.video.configure(image="", text="Connecting...")
        self.mask_view.configure(image="", text="Waiting for background")
        self.camera.start(self.selected_ip)

    def disconnect_camera(self):
        self.detector.disable()
        self._set_detection_state(False)
        self._set_alarm(False)
        self.camera.stop()
        self._set_camera_state(False)
        self.photo = None
        self.mask_photo = None
        self.last_frame_id = -1
        self.video.configure(image="", text="No camera connected")
        self.mask_view.configure(image="", text="Waiting for background")
        self.detector_state_var.set("Waiting for camera")
        self.status_var.set("Camera disconnected")

    def _set_alarm(self, active):
        active = bool(active)
        if active != self.last_alarm_state:
            self.last_alarm_state = active
            self.alarm_led.set_active(active)
            if active:
                self.alarm_prompt.place(
                    relx=0.5, rely=0.5, anchor="center", relwidth=0.68
                )
                self.alarm_prompt.lift()
            else:
                self.alarm_prompt.place_forget()

    @staticmethod
    def draw_boxes(frame, boxes, raw_w, raw_h):
        h, w = frame.shape[:2]
        sx, sy = w / raw_w, h / raw_h
        for x1, y1, x2, y2, _area in boxes:
            p1 = (max(0, int(x1 * sx)), max(0, int(y1 * sy)))
            p2 = (min(w - 1, int(x2 * sx)), min(h - 1, int(y2 * sy)))
            if p2[0] <= p1[0] or p2[1] <= p1[1]:
                continue
            cv2.rectangle(frame, p1, p2, (0, 0, 255), 3)

    def _update_mask_view(self, mask):
        if mask is None:
            self.mask_view.configure(image="", text="Calibrating background...")
            return
        panel_w = max(self.mask_view.winfo_width(), 200)
        panel_h = max(self.mask_view.winfo_height(), 180)
        mh, mw = mask.shape[:2]
        scale = min(panel_w / mw, panel_h / mh)
        out_w = max(1, int(mw * scale))
        out_h = max(1, int(mh * scale))
        disp = cv2.resize(mask, (out_w, out_h), interpolation=cv2.INTER_NEAREST)
        rgb = cv2.cvtColor(disp, cv2.COLOR_GRAY2RGB)
        self.mask_photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.mask_view.configure(image=self.mask_photo, text="")

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

            result = self.detector.get_result() if self.detector.is_enabled() else {
                "boxes": [], "mask": None, "object_present": False,
                "largest_area": 0.0, "ready": False, "init_count": 0,
            }

            if result["ready"]:
                self.draw_boxes(display, result["boxes"], raw_w, raw_h)
                self._update_mask_view(result["mask"])
                if result["object_present"]:
                    self.alarm_clear_count = 0
                    self._set_alarm(True)
                else:
                    self.alarm_clear_count += 1
                    if self.alarm_clear_count >= ALARM_CLEAR_FRAMES:
                        self._set_alarm(False)
            else:
                self.alarm_clear_count = 0
                self._set_alarm(False)
                self._update_mask_view(result["mask"])

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
                    self.detector.configure(self.validated_min_area)
                    self.detector.enable()
                    self._set_detection_state(False)
                    self.detector_state_var.set(
                        f"Auto-calibrating background ({BACKGROUND_INIT_FRAMES} frames)..."
                    )
                    self.status_var.set(
                        "Camera connected - automatically capturing clear background..."
                    )
                elif event == "background_status":
                    self.detector_state_var.set(str(data))
                elif event == "background_ready":
                    self._set_detection_state(True)
                    self.detector_state_var.set(
                        f"Monitoring | minimum area {self.validated_min_area:.0f} px²"
                    )
                    self.status_var.set("Background ready - monitoring scene deviation")
                elif event == "camera_error":
                    self.detector.disable()
                    self._set_camera_state(False)
                    self._set_detection_state(False)
                    self._set_alarm(False)
                    self.status_var.set("Camera connection failed")
                    messagebox.showerror("Camera error", data)
                elif event == "camera_disconnected":
                    self.detector.disable()
                    self._set_camera_state(False)
                    self._set_detection_state(False)
                    self._set_alarm(False)
                    self.detector_state_var.set("Waiting for camera")
                    self.status_var.set("Camera disconnected")
                elif event == "detector_error":
                    self._set_alarm(False)
                    self.status_var.set("Background detector error")
                    messagebox.showerror("Detection error", data)
        except queue.Empty:
            pass
        self.after(40, self.process_events)

    def update_stats(self):
        result = self.detector.get_result() if self.detector.is_enabled() else None
        detector_fps = f"{self.detector.get_fps():.1f}" if self.detector.is_enabled() else "OFF"
        largest = result["largest_area"] if result else 0.0
        self.stats_var.set(
            f"Camera {self.camera.get_fps():.1f} FPS   |   Detection {detector_fps} FPS"
            f"   |   Largest change {largest:.0f} px²"
        )
        self.after(500, self.update_stats)

    def close_app(self):
        self.detector.stop()
        self.camera.stop()
        self.destroy()


if __name__ == "__main__":
    AmebaApp().mainloop()
