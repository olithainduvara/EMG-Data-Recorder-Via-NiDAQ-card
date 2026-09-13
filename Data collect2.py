"""Three-channel NI USB-6009 EMG recorder and real-time viewer.

Performance notes
-----------------
The GUI thread never touches raw sample arrays whose length grows with the
display window.  Every per-frame operation is O(new samples) instead of
O(window length):

* acquisition runs in its own thread;
* CSV writing runs in a *second* thread fed by its own queue, so recording
  never blocks painting and no block is dropped while the UI is busy;
* device presence is polled in a *third* thread, so NI-DAQmx enumeration
  never stalls the interface;
* the display keeps a peak-decimating ring buffer (min/max per bucket), so a
  curve is always ~1200 buckets no matter whether the window holds 2,500 or
  480,000 samples;
* RMS is tracked with a running sum of squares over its own short ring, so it
  reacts quickly while the trace still shows a long window.

Styling note: every stylesheet rule here is scoped with an object name.  A
selector-less rule such as "background:transparent" is inherited by *all*
descendants and will silently repaint the input fields.

Run: python "Data collect2.py"
"""
import sys, math, queue, threading, time
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

try:
    # Broad except on purpose: on a machine without the NI-DAQmx *driver* this
    # can fail while loading nicaiu.dll, which is not an ImportError. The app
    # must still start (simulation mode) and say what is missing.
    import nidaqmx
    from nidaqmx.system import System as NiSystem
    from nidaqmx.constants import AcquisitionType, TerminalConfiguration
    NIDAQMX_AVAILABLE = True
    NIDAQMX_ERROR = None
except Exception as _exc:
    # Keep the reason. "Driver missing" and "the package failed to import"
    # look identical to the user otherwise, and the second one is a packaging
    # bug worth seeing rather than blaming on the driver.
    NIDAQMX_AVAILABLE = False
    NIDAQMX_ERROR = f"{type(_exc).__name__}: {_exc}"


def _terminal_config(mode):
    """Look up a TerminalConfiguration member across nidaqmx releases.

    nidaqmx 1.x renamed DIFFERENTIAL -> DIFF (RSE was kept). Trying a short
    list of known spellings means a future rename degrades to a clear
    RuntimeError instead of an AttributeError mid-acquisition.
    """
    names = {"Differential": ("DIFFERENTIAL", "DIFF"), "RSE": ("RSE",)}[mode]
    for name in names:
        member = getattr(TerminalConfiguration, name, None)
        if member is not None:
            return member
    raise RuntimeError(
        f"nidaqmx.constants.TerminalConfiguration has none of {names}; "
        f"this nidaqmx version ({getattr(nidaqmx, '__version__', '?')}) "
        f"is not supported for '{mode}' mode."
    )

APP_TITLE = "EMG Recorder — NI USB-6009"
COLORS = [(0, 196, 180), (255, 166, 43), (205, 100, 255)]

FROZEN = getattr(sys, "frozen", False)


def resource_path(name):
    """Locate a bundled file, running either as a script or as a PyInstaller exe.

    PyInstaller unpacks data files to a temporary folder named by sys._MEIPASS;
    next to the .py otherwise.
    """
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / name if base else Path(__file__).resolve().with_name(name)


def application_icon():
    """The window/taskbar icon.

    PyInstaller's --icon only sets the icon Explorer shows for the .exe file.
    The icon Windows shows in the taskbar and title bar comes from Qt, so it
    has to be set explicitly or the app gets a generic default.

    The .ico is preferred over the .png: it carries 16-48 px variants, and
    Windows picks the small ones for the title bar and Alt+Tab. A single large
    PNG would be downscaled and look soft there.
    """
    for name in ("app_icon.ico", "icon.png"):
        path = resource_path(name)
        if path.exists():
            icon = QtGui.QIcon(str(path))
            if not icon.isNull():
                return icon
    return QtGui.QIcon()


def claim_taskbar_identity(app_id="Peradeniya.FYP.EMGRecorder.USB6009"):
    """Give Windows an explicit AppUserModelID.

    Without one, the taskbar groups the window under the host process and
    shows that process's icon instead of the window icon. Must run before the
    first window is shown. No-op off Windows.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass          # cosmetic only - never block startup over the icon


def default_save_dir():
    """Somewhere always writable — an exe may sit in Program Files or on a stick."""
    if FROZEN:
        documents = Path.home() / "Documents"
        root = documents if documents.is_dir() else Path.home()
        return root / "EMG Recordings"
    return Path.cwd() / "recordings"


PANEL_BACKDROP = resource_path("cover4.jpg")
GRAPH_BACKDROP = resource_path("cover5.jpg")
PANEL_FOCUS = 0.33             # 0..1 horizontal crop anchor (the hand/sphere)
GRAPH_FOCUS = 0.50

DISPLAY_POINTS = 1200          # buckets drawn per channel, whatever the window
RMS_INTERVAL_MS = 100          # how often the RMS labels are repainted
DEFAULT_RMS_WINDOW = 0.25      # seconds averaged for the RMS readout
DEFAULT_V_MIN, DEFAULT_V_MAX = -5.0, 5.0
DEFAULT_NOTCH_HZ = 50.0        # mains frequency (50 Hz here, 60 Hz in the Americas)
DEFAULT_NOTCH_Q = 30.0         # higher Q = narrower notch = less signal removed
DEFAULT_NOTCH_HARMONICS = 3    # 50, 100, 150 Hz
DEVICE_POLL_S = 1.5            # how often the DAQ presence is re-checked

# Field widths — the inputs are sized, not stretched to the panel edge.
FIELD_W, SPIN_W, COMBO_W, LABEL_W = 146, 132, 168, 118
TERMINALS = ["DAQmx default", "Differential", "RSE"]

# Glassmorphism lives on the controls: translucent fills with a light rim.
# The backdrop photos are left sharp — `blur_panel`/`blur_graph` are kept at 0
# (set a radius to frost them instead). `backdrop_graph` is the photo strength
# behind the traces: raise it for more cover5, lower it to fade it back.
THEMES = {
    "dark": {
        "app_bg": "#0a0e13", "panel": "#161f29", "graph_bg": "#0e131a",
        "text": "#e3ebf3", "muted": "#93a6b8", "heading": "#f2f7fc",
        "field_bg": "rgba(255,255,255,0.08)", "field_fg": "#eaf2fa",
        "field_border": "rgba(255,255,255,0.20)",
        "field_focus": "rgba(0,196,180,0.95)",
        "field_hover": "rgba(255,255,255,0.13)",
        "axis": "#c7d3de", "grid_alpha": 0.18,
        "ok": "#8ff0c4", "err": "#ff9aa0",
        "glass": "rgba(255,255,255,0.10)", "glass_hi": "rgba(255,255,255,0.16)",
        "glass_rim": "rgba(255,255,255,0.22)", "glass_fg": "#e3ebf3",
        "scroll_handle": "rgba(255,255,255,0.22)", "menu_bg": "#1b2531",
        "backdrop_panel": 0.16, "backdrop_graph": 0.09,
        "blur_panel": 0, "blur_graph": 0,
    },
    "light": {
        "app_bg": "#d9e2ea", "panel": "#eef3f8", "graph_bg": "#fbfdff",
        "text": "#16202b", "muted": "#5b6b7c", "heading": "#0d151d",
        "field_bg": "rgba(255,255,255,0.82)", "field_fg": "#16202b",
        "field_border": "rgba(0,0,0,0.14)",
        "field_focus": "rgba(0,150,138,0.95)",
        "field_hover": "rgba(255,255,255,0.95)",
        "axis": "#3a4754", "grid_alpha": 0.26,
        "ok": "#0f5f38", "err": "#a81f18",
        "glass": "rgba(255,255,255,0.52)", "glass_hi": "rgba(255,255,255,0.70)",
        "glass_rim": "rgba(255,255,255,0.85)", "glass_fg": "#16202b",
        "scroll_handle": "rgba(0,0,0,0.20)", "menu_bg": "#ffffff",
        "backdrop_panel": 0.14, "backdrop_graph": 0.07,
        "blur_panel": 0, "blur_graph": 0,
    },
}


_ARROW_DIR = None          # QTemporaryDir, kept alive for the app's lifetime
_ARROW_CACHE = {}


def arrow_image(direction, colour):
    """Draw a small chevron and return a file URL a stylesheet can use.

    Qt cannot tint the built-in spin-box arrows, and the default framed
    steppers render as bright slabs on a dark field, so the arrows are drawn
    in the theme's own text colour and cached on disk.
    """
    global _ARROW_DIR
    key = (direction, colour)
    if key in _ARROW_CACHE:
        return _ARROW_CACHE[key]
    if _ARROW_DIR is None:
        _ARROW_DIR = QtCore.QTemporaryDir()
    pixmap = QtGui.QPixmap(18, 18)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    pen = QtGui.QPen(QtGui.QColor(colour))
    pen.setWidthF(1.9)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    chevron = QtGui.QPainterPath()
    if direction == "up":
        chevron.moveTo(5, 11.5); chevron.lineTo(9, 7); chevron.lineTo(13, 11.5)
    else:
        chevron.moveTo(5, 7); chevron.lineTo(9, 11.5); chevron.lineTo(13, 7)
    painter.drawPath(chevron)
    painter.end()
    safe = "".join(ch for ch in colour if ch.isalnum())
    target = f"{_ARROW_DIR.path()}/{direction}_{safe}.png"
    pixmap.save(target, "PNG")
    url = target.replace("\\", "/")
    _ARROW_CACHE[key] = url
    return url


def friendly_daq_error(message):
    """Turn a raw NI-DAQmx exception into something a user can act on."""
    text = str(message)
    low = text.lower()
    if any(code in text for code in ("-88709", "-200587", "-88705")) or \
            "no longer present" in low or "device removed" in low or "not present" in low:
        return "DAQ disconnected — reconnect the USB cable, then press Start again."
    if "-201003" in text or "could not be found" in low or "does not exist" in low:
        return "Device not found. Check the device name (see NI MAX)."
    if "-50103" in text or "reserved" in low or "already in use" in low:
        return "Device is busy — close NI MAX test panels or other software using it."
    if "-200077" in text or "requested value is not a supported value" in low:
        return "Unsupported setting for this device (check channel names and input mode)."
    if "-200279" in text or "overwritten before" in low:
        return "Samples were overwritten — lower the sample rate."
    return text


# --------------------------------------------------------------------------- #
#  Buffers
# --------------------------------------------------------------------------- #
class PeakRing:
    """Ring buffer of per-bucket (min, max) pairs - a peak-decimated trace.

    Appending costs O(new samples); reading costs O(number of buckets), which
    is fixed at ~DISPLAY_POINTS regardless of sample rate or window length.
    """

    def __init__(self):
        self.configure(1, 2)

    def configure(self, decimation, capacity):
        self.dec = max(1, int(decimation))
        self.cap = max(2, int(capacity))
        self.mins = np.zeros(self.cap)
        self.maxs = np.zeros(self.cap)
        self.idx = 0
        self.count = 0
        self.carry = np.empty(0)

    def clear(self):
        self.idx = 0
        self.count = 0
        self.carry = np.empty(0)

    def append(self, y):
        if self.carry.size:
            y = np.concatenate((self.carry, y))
        d = self.dec
        usable = (y.size // d) * d
        if usable:
            block = y[:usable].reshape(-1, d)
            self._push(block.min(axis=1), block.max(axis=1))
        self.carry = y[usable:].copy()

    def _push(self, mn, mx):
        k = mn.size
        if k > self.cap:
            mn, mx, k = mn[-self.cap:], mx[-self.cap:], self.cap
        end = self.idx + k
        if end <= self.cap:
            self.mins[self.idx:end] = mn
            self.maxs[self.idx:end] = mx
        else:
            split = self.cap - self.idx
            self.mins[self.idx:] = mn[:split]
            self.maxs[self.idx:] = mx[:split]
            self.mins[:end - self.cap] = mn[split:]
            self.maxs[:end - self.cap] = mx[split:]
        self.idx = end % self.cap
        self.count = min(self.count + k, self.cap)

    def ordered(self):
        """Oldest-to-newest (mins, maxs); at most `cap` elements copied."""
        if self.count < self.cap:
            return self.mins[:self.count], self.maxs[:self.count]
        return (np.concatenate((self.mins[self.idx:], self.mins[:self.idx])),
                np.concatenate((self.maxs[self.idx:], self.maxs[:self.idx])))


class RmsRing:
    """Sliding-window RMS kept with a running sum of squares."""

    def __init__(self):
        self.configure(2)

    def configure(self, capacity):
        self.cap = max(1, int(capacity))
        self.sq = np.zeros(self.cap)
        self.idx = 0
        self.count = 0
        self.total = 0.0
        self.since_rebuild = 0

    def clear(self):
        self.sq[:] = 0.0
        self.idx = self.count = self.since_rebuild = 0
        self.total = 0.0

    def append(self, y):
        if y.size > self.cap:
            y = y[-self.cap:]
        sq = y * y
        k = sq.size
        end = self.idx + k
        if end <= self.cap:
            self.total += sq.sum() - self.sq[self.idx:end].sum()
            self.sq[self.idx:end] = sq
        else:
            split = self.cap - self.idx
            self.total += (sq.sum() - self.sq[self.idx:].sum()
                           - self.sq[:end - self.cap].sum())
            self.sq[self.idx:] = sq[:split]
            self.sq[:end - self.cap] = sq[split:]
        self.idx = end % self.cap
        self.count = min(self.count + k, self.cap)
        # Re-sum once per window to stop floating-point drift accumulating.
        self.since_rebuild += k
        if self.since_rebuild >= self.cap:
            self.total = float(self.sq[:self.count].sum())
            self.since_rebuild = 0

    def value(self):
        if not self.count:
            return 0.0
        return math.sqrt(max(self.total, 0.0) / self.count)


# --------------------------------------------------------------------------- #
#  Filtering
# --------------------------------------------------------------------------- #
class NotchFilter:
    """Cascade of second-order IIR notches, one per mains harmonic.

    Uses the RBJ cookbook biquad, which is the same design scipy.signal.iirnotch
    produces. It is written out here rather than imported so the application
    keeps no scipy dependency - scipy would roughly double the executable for
    five lines of algebra.

    State is carried across blocks (Direct Form II transposed), so a continuous
    stream filters exactly as if it had been one long array. Changing any
    parameter builds a new filter, which restarts from zero state.
    """

    def __init__(self, fs, freq, q, harmonics, channels=3):
        self.fs, self.freq, self.q = float(fs), float(freq), float(q)
        self.channels = int(channels)
        self.sections = []
        nyquist = self.fs / 2.0
        for k in range(1, max(1, int(harmonics)) + 1):
            f0 = self.freq * k
            if f0 >= nyquist:
                break                      # a notch at or above Nyquist is meaningless
            self.sections.append(self._biquad(f0))
        self.reset()

    def _biquad(self, f0):
        """One notch section, identical to scipy.signal.iirnotch(f0, Q, fs).

        Note this is *not* the RBJ cookbook form (alpha = sin(w0)/2Q): that is
        an approximation whose -3 dB bandwidth drifts from w0/Q as f0 rises.
        Using tan(bw/2) makes Q mean exactly what the textbooks say it does, so
        results match anything analysed with scipy offline.
        """
        w0 = 2.0 * math.pi * f0 / self.fs
        cos_w0 = math.cos(w0)
        beta = math.tan(w0 / self.q / 2.0)      # gb = 1/sqrt(2) folds to a factor of 1
        gain = 1.0 / (1.0 + beta)
        b = (gain, -2.0 * gain * cos_w0, gain)
        a = (1.0, -2.0 * gain * cos_w0, 2.0 * gain - 1.0)
        return b, a

    def reset(self):
        self.z1 = [[0.0] * self.channels for _ in self.sections]
        self.z2 = [[0.0] * self.channels for _ in self.sections]

    @property
    def active(self):
        return bool(self.sections)

    def notch_frequencies(self):
        return [self.freq * k for k in range(1, len(self.sections) + 1)]

    def describe(self):
        if not self.sections:
            return f"no harmonic of {self.freq:g} Hz falls below Nyquist"
        freqs = ", ".join(f"{f:g}" for f in self.notch_frequencies())
        return f"{freqs} Hz  ·  Q {self.q:g}"

    def process(self, block):
        """Filter a (channels, n) block and return a new array.

        The recursion is inherently sequential, so this runs a scalar loop over
        a Python list: for the few-hundred-sample blocks used here that beats
        per-sample numpy indexing, which pays ~1 us of overhead per operation.
        """
        out = np.array(block, dtype=float, copy=True)
        rows = out.shape[0]
        for s, (b, a) in enumerate(self.sections):
            b0, b1, b2 = b
            a1, a2 = a[1], a[2]
            z1_row, z2_row = self.z1[s], self.z2[s]
            for c in range(rows):
                z1, z2 = z1_row[c], z2_row[c]
                data = out[c].tolist()
                for i, x in enumerate(data):
                    y = b0 * x + z1
                    z1 = b1 * x - a1 * y + z2
                    z2 = b2 * x - a2 * y
                    data[i] = y
                out[c] = data
                z1_row[c], z2_row[c] = z1, z2
        return out


# --------------------------------------------------------------------------- #
#  Threads
# --------------------------------------------------------------------------- #
class DeviceMonitor(QtCore.QObject):
    """Polls NI-DAQmx for attached devices without ever blocking the GUI.

    Emits `changed` only when the attached set actually changes, so plugging
    or unplugging the USB-6009 is reported once, not every poll.
    """

    changed = QtCore.Signal(object)     # list[dict] | {"error": str} | None

    def __init__(self, interval=DEVICE_POLL_S, parent=None):
        super().__init__(parent)
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    @staticmethod
    def _scan():
        if not NIDAQMX_AVAILABLE:
            return None
        try:
            found = []
            for dev in NiSystem.local().devices:
                try:
                    serial = dev.serial_num
                except AttributeError:
                    serial = getattr(dev, "dev_serial_num", 0)
                except Exception:
                    serial = 0
                try:
                    product = dev.product_type
                except Exception:
                    product = "unknown"
                found.append({"name": dev.name, "product": product, "serial": serial})
            return found
        except Exception as exc:
            return {"error": str(exc)}

    def _run(self):
        previous = "<unset>"
        while not self._stop.is_set():
            snapshot = self._scan()
            if snapshot != previous:
                previous = snapshot
                self.changed.emit(snapshot)
            self._stop.wait(self.interval)


class AcquisitionWorker(threading.Thread):
    """Acquires blocks in a background thread; never accesses Qt widgets."""

    def __init__(self, device, channels, fs, terminal, block_size, out_queue, simulate=False):
        super().__init__(daemon=True)
        self.device, self.channels, self.fs = device, channels, fs
        self.terminal, self.block_size, self.out_queue, self.simulate = terminal, block_size, out_queue, simulate
        self.stop_event = threading.Event()
        self.error = None
        self.record_queue = None     # set by the GUI while recording
        self.notch = None            # optional NotchFilter, set by the GUI
        self.dropped = 0             # display blocks skipped (never recorded ones)
        self.total = 0               # samples per channel produced so far

    def stop(self):
        self.stop_event.set()

    def put(self, values):
        notch = self.notch                    # atomic read of the attribute
        if notch is not None:
            # Filter here, before the block is split between the display and
            # the recorder, so what is plotted always matches what is saved.
            values = notch.process(values)
        start = self.total
        self.total += values.shape[1]
        recorder = self.record_queue          # atomic read of the attribute
        if recorder is not None:
            try:
                recorder.put_nowait((start, values))
            except queue.Full:
                pass
        # The display queue may drop: stale frames are worthless anyway.
        try:
            self.out_queue.put_nowait((start, values))
        except queue.Full:
            try:
                self.out_queue.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.out_queue.put_nowait((start, values))
            except queue.Full:
                pass

    def run(self):
        if self.simulate:
            phase = 0.0
            period = self.block_size / self.fs
            next_due = time.monotonic()
            while not self.stop_event.is_set():
                t = np.arange(self.block_size) / self.fs + phase
                phase = t[-1] + 1 / self.fs
                # EMG-like simulated signal, in volts
                noise = np.random.normal(0, .008, (3, self.block_size))
                envelope = .015 + .040 * (np.sin(2 * np.pi * .35 * t) + 1) / 2
                self.put(noise + envelope * np.random.normal(0, 1, (3, self.block_size)))
                next_due += period
                delay = next_due - time.monotonic()
                if delay > 0:
                    self.stop_event.wait(delay)
                else:
                    next_due = time.monotonic()
            return
        if not NIDAQMX_AVAILABLE:
            self.error = "nidaqmx is not installed. Run: pip install nidaqmx"
            return
        try:
            # "DAQmx default" deliberately makes the same channel call as the
            # small test script supplied by the user. It lets NI-DAQmx choose the
            # USB-6009 terminal configuration rather than forcing Differential/RSE.
            with nidaqmx.Task() as task:
                for channel in self.channels:
                    physical_channel = f"{self.device}/{channel}"
                    if self.terminal == "DAQmx default":
                        task.ai_channels.add_ai_voltage_chan(physical_channel)
                    else:
                        term = (_terminal_config("Differential") if self.terminal == "Differential"
                                else _terminal_config("RSE"))
                        task.ai_channels.add_ai_voltage_chan(physical_channel,
                            terminal_config=term, min_val=-10.0, max_val=10.0)
                task.timing.cfg_samp_clk_timing(rate=self.fs,
                                                sample_mode=AcquisitionType.CONTINUOUS,
                                                samps_per_chan=max(self.fs, self.block_size * 10))
                task.start()
                while not self.stop_event.is_set():
                    data = task.read(number_of_samples_per_channel=self.block_size, timeout=2.0)
                    self.put(np.asarray(data, dtype=float))
        except Exception as exc:
            self.error = friendly_daq_error(exc)


class CsvRecorder(threading.Thread):
    """Formats and writes CSV rows off the GUI thread."""

    def __init__(self, path, fs, in_queue):
        super().__init__(daemon=True)
        self.path, self.fs, self.in_queue = path, fs, in_queue
        self.rows = 0
        self.error = None
        self._fmt_cache = {}

    def _fmt(self, n):
        fmt = self._fmt_cache.get(n)
        if fmt is None:
            # One C-level %-format for the whole block beats per-row csv writes.
            fmt = "%.6f,%.6f,%.6f,%.6f\n" * n
            self._fmt_cache = {n: fmt}
        return fmt

    def run(self):
        try:
            with open(self.path, "w", newline="", encoding="utf-8") as fh:
                fh.write("time_s,channel_1_V,channel_2_V,channel_3_V\n")
                last_flush = time.monotonic()
                while True:
                    item = self.in_queue.get()
                    if item is None:
                        break
                    start, block = item
                    n = block.shape[1]
                    rows = np.empty((n, 4))
                    rows[:, 0] = np.arange(start, start + n) / self.fs
                    rows[:, 1:] = block.T
                    fh.write(self._fmt(n) % tuple(rows.ravel()))
                    self.rows += n
                    now = time.monotonic()
                    if now - last_flush >= 1.0:
                        fh.flush()
                        last_flush = now
        except Exception as exc:
            self.error = str(exc)


# --------------------------------------------------------------------------- #
#  Widgets
# --------------------------------------------------------------------------- #
class BackdropFrame(QtWidgets.QFrame):
    """Panel that paints a faint, cropped photo behind its children.

    The plots on top are transparent, so this widget is repainted beneath them
    on *every* frame.  Blending the photo and clipping a rounded path per paint
    costs more than the acquisition itself, so the background colour, the
    faded photo and the rounded corners are composed once per resize into a
    single pixmap and each paint is one straight blit.
    """

    def __init__(self, image_path, opacity, radius=12, focus_x=0.5, bg="#18212b",
                 blur=0, parent=None):
        super().__init__(parent)
        self._source = QtGui.QPixmap(str(image_path))
        self._opacity = opacity
        self._radius = radius
        self._focus_x = min(max(focus_x, 0.0), 1.0)
        self._bg = bg
        self._blur = blur
        self._composite = None       # fully composed pixmap, rebuilt on resize

    def set_appearance(self, bg, opacity, blur=None):
        blur = self._blur if blur is None else blur
        if bg == self._bg and opacity == self._opacity and blur == self._blur:
            return
        self._bg, self._opacity, self._blur = bg, opacity, blur
        self._composite = None
        self.update()

    @staticmethod
    def _blurred(pixmap, radius):
        """Gaussian-blur a pixmap once, for the frosted-glass backdrop."""
        if radius <= 0:
            return pixmap
        scene = QtWidgets.QGraphicsScene()
        item = scene.addPixmap(pixmap)
        effect = QtWidgets.QGraphicsBlurEffect()
        effect.setBlurRadius(radius)
        item.setGraphicsEffect(effect)
        out = QtGui.QImage(pixmap.size(), QtGui.QImage.Format_ARGB32_Premultiplied)
        out.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(out)
        scene.render(painter, QtCore.QRectF(out.rect()),
                     QtCore.QRectF(pixmap.rect()))
        painter.end()
        return QtGui.QPixmap.fromImage(out)

    def resizeEvent(self, event):
        self._composite = None
        super().resizeEvent(event)

    def _compose(self):
        ratio = self.devicePixelRatioF()
        width, height = self.width(), self.height()
        pixmap = QtGui.QPixmap(max(1, int(width * ratio)), max(1, int(height * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(QtCore.QRectF(0, 0, width, height), self._radius, self._radius)
        painter.setClipPath(clip)
        painter.fillRect(0, 0, width, height, QtGui.QColor(self._bg))
        if not self._source.isNull():
            scaled = self._source.scaled(self.size(), QtCore.Qt.KeepAspectRatioByExpanding,
                                         QtCore.Qt.SmoothTransformation)
            scaled = self._blurred(scaled, self._blur)
            painter.setOpacity(self._opacity)
            # A wide banner in a tall panel is cropped, so anchor the visible
            # slice on the part of the photo worth showing, not its centre.
            overflow = scaled.width() - width
            painter.drawPixmap(-int(overflow * self._focus_x),
                               (height - scaled.height()) // 2, scaled)
        painter.end()
        self._composite = pixmap

    def paintEvent(self, event):
        if self.width() <= 0 or self.height() <= 0:
            return
        if self._composite is None:
            self._compose()
        painter = QtGui.QPainter(self)
        painter.drawPixmap(0, 0, self._composite)
        painter.end()


class AlertOverlay(QtWidgets.QWidget):
    """Pulsing banner floated over the plots for a state the user must not miss.

    It only animates while it is visible, and it is only visible once
    acquisition has already stopped, so the repaints cost nothing that matters.
    """

    def __init__(self, parent):
        super().__init__(parent)
        # Never swallow clicks meant for the plots underneath.
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self._title = ""
        self._detail = ""
        self._phase = 0.0
        self._pulse = QtCore.QVariantAnimation(self)
        self._pulse.setStartValue(0.0)
        self._pulse.setEndValue(1.0)
        self._pulse.setDuration(900)
        self._pulse.setLoopCount(-1)
        self._pulse.setEasingCurve(QtCore.QEasingCurve.InOutSine)
        self._pulse.valueChanged.connect(self._advance)
        parent.installEventFilter(self)
        self.hide()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Resize and watched is self.parent():
            self.reposition()
        return False

    def _advance(self, value):
        self._phase = float(value)
        self.update()

    def reposition(self):
        parent = self.parent()
        if parent is None:
            return
        width = max(280, min(parent.width() - 48, 560))
        height = 96
        self.setGeometry((parent.width() - width) // 2,
                         (parent.height() - height) // 2, width, height)

    def show_alert(self, title, detail):
        self._title, self._detail = title, detail
        self.reposition()
        self.show()
        self.raise_()
        if self._pulse.state() != QtCore.QAbstractAnimation.Running:
            self._pulse.start()
        self.update()

    def hide_alert(self):
        self._pulse.stop()
        self.hide()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        radius = 14.0

        # Body: a deep red glass slab.
        body = QtGui.QLinearGradient(rect.topLeft(), rect.bottomLeft())
        body.setColorAt(0.0, QtGui.QColor(186, 46, 54, 240))
        body.setColorAt(1.0, QtGui.QColor(132, 24, 32, 245))
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(body)
        painter.drawRoundedRect(rect, radius, radius)

        # Rim brightens and dims on the pulse.
        alpha = int(110 + 130 * self._phase)
        pen = QtGui.QPen(QtGui.QColor(255, 150, 156, alpha))
        pen.setWidthF(2.0)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

        # Warning triangle, drawn rather than typed: no font dependency.
        side = 34.0
        cx, cy = rect.left() + 34, rect.center().y()
        triangle = QtGui.QPainterPath()
        triangle.moveTo(cx, cy - side / 2)
        triangle.lineTo(cx + side / 2, cy + side / 2)
        triangle.lineTo(cx - side / 2, cy + side / 2)
        triangle.closeSubpath()
        glyph = QtGui.QPen(QtGui.QColor(255, 226, 228, 235))
        glyph.setWidthF(2.2)
        glyph.setJoinStyle(QtCore.Qt.RoundJoin)
        painter.setPen(glyph)
        painter.drawPath(triangle)
        painter.drawLine(QtCore.QPointF(cx, cy - 6), QtCore.QPointF(cx, cy + 4))
        painter.drawPoint(QtCore.QPointF(cx, cy + 9))

        # Text block.
        left = rect.left() + 64
        text_rect = QtCore.QRectF(left, rect.top() + 16, rect.right() - left - 16, 26)
        font = painter.font()
        font.setBold(True)
        font.setPointSizeF(max(11.0, font.pointSizeF() + 2))
        painter.setFont(font)
        painter.setPen(QtGui.QColor(255, 255, 255))
        painter.drawText(text_rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, self._title)

        font.setBold(False)
        font.setPointSizeF(max(8.5, font.pointSizeF() - 3.5))
        painter.setFont(font)
        painter.setPen(QtGui.QColor(255, 224, 226, 225))
        detail_rect = QtCore.QRectF(left, rect.top() + 44, rect.right() - left - 16, 38)
        metrics = QtGui.QFontMetrics(font)
        elided = metrics.elidedText(self._detail, QtCore.Qt.ElideRight,
                                    int(detail_rect.width()) * 2)
        painter.drawText(detail_rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop |
                         QtCore.Qt.TextWordWrap, elided)
        painter.end()


class DeviceCard(QtWidgets.QFrame):
    """Compact connection indicator: a status lamp plus device identity."""

    # state -> (accent, dark lamp/text, light lamp/text, headline)
    STATES = {
        "ok":       ("#1f9d63", "#7ef0b6", "#0f6b41", "DAQ CONNECTED"),
        "wait":     ("#b08828", "#ffd479", "#7a5c0d", "SEARCHING…"),
        "error":    ("#c0444d", "#ff9ea4", "#9e1f27", "DAQ DISCONNECTED"),
        "sim":      ("#3d76c4", "#9ec5ff", "#1c4a8a", "SIMULATION MODE"),
        "nodriver": ("#6a757f", "#c3ccd6", "#4a545f", "NI-DAQmx UNAVAILABLE"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("deviceCard")
        self._theme_name = "dark"
        self._theme = THEMES["dark"]
        self._state = "wait"
        self._detail = "Looking for NI hardware…"
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        header = QtWidgets.QHBoxLayout(); header.setSpacing(7)
        self.lamp = QtWidgets.QLabel("●"); self.lamp.setObjectName("cardLamp")
        self.lamp.setFixedWidth(12)
        self.headline = QtWidgets.QLabel(); self.headline.setObjectName("cardHeadline")
        header.addWidget(self.lamp); header.addWidget(self.headline); header.addStretch()
        layout.addLayout(header)
        self.detail = QtWidgets.QLabel(); self.detail.setObjectName("cardDetail")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(QtCore.Qt.RichText)
        self.detail.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Minimum)
        self.detail.setMinimumWidth(60)
        layout.addWidget(self.detail)
        self._render()

    def set_theme(self, name):
        self._theme_name = name if name in THEMES else "dark"
        self._theme = THEMES[self._theme_name]
        self._render()

    def set_state(self, state, detail):
        self._state = state if state in self.STATES else "wait"
        self._detail = detail
        self._render()

    def _render(self):
        accent, dark_ink, light_ink, title = self.STATES[self._state]
        ink = dark_ink if self._theme_name == "dark" else light_ink
        if self._state == "error":
            # A fault should not look like every other state: fill it red.
            top, bottom = "rgba(186,46,54,0.92)", "rgba(132,24,32,0.94)"
            rim, ink = "rgba(255,150,156,0.75)", "#ffe6e8"
        else:
            top, bottom = self._theme["glass_hi"], self._theme["glass"]
            rim = self._theme["glass_rim"]
        self._ink = ink
        self.setStyleSheet(
            f"QFrame#deviceCard{{border:1px solid {rim};"
            f"border-left:3px solid {accent};border-radius:10px;"
            f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 {top}, stop:1 {bottom})}}")
        self.lamp.setStyleSheet(f"QLabel#cardLamp{{color:{ink};font-size:13px;background:transparent}}")
        self.headline.setText(title)
        self.headline.setStyleSheet(
            f"QLabel#cardHeadline{{color:{ink};font-weight:bold;font-size:10px;"
            f"letter-spacing:1px;background:transparent}}")
        self.detail.setText(self._detail)
        detail_ink = "#ffe6e8" if self._state == "error" else self._theme["text"]
        self.detail.setStyleSheet(
            f"QLabel#cardDetail{{color:{detail_ink};font-size:11px;background:transparent}}")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(900, 600)   # usable floor if the user un-maximizes
        self.resize(1300, 840)          # fallback size before the first show
        self.setWindowIcon(application_icon())
        self.settings = QtCore.QSettings("FYP", "EMGRecorder")
        self.worker = None; self.recorder = None; self.recording = False
        self.record_queue = None; self.record_path = None
        self.fs = 2000; self.samples = 0; self._busy = False
        self._device_edited = False; self._devices = []; self._scan_error = None
        self.notch = None            # active NotchFilter, or None when off
        self._status_text = "Ready"; self._status_error = False
        self.data_queue = queue.Queue(maxsize=25)
        self.rings = [PeakRing() for _ in range(3)]
        self.rms_rings = [RmsRing() for _ in range(3)]
        self._x_full = np.zeros(0); self._x_rep = np.zeros(0)
        saved_dir = self.settings.value("save_dir", "")
        self.save_dir = Path(saved_dir) if saved_dir else default_save_dir()
        self.theme_name = self.settings.value("theme", "dark")
        if self.theme_name not in THEMES:
            self.theme_name = "dark"
        self._build_ui()
        self.apply_theme(self.theme_name)
        self._rebuild_display_buffers()
        self._rebuild_rms_buffers()
        self._last_rms = 0.0
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.CoarseTimer)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.consume_data)
        self.timer.start()
        self.monitor = DeviceMonitor()
        self.monitor.changed.connect(self.on_devices_changed)
        self.monitor.start()

    # -- UI ----------------------------------------------------------------- #
    def _build_ui(self):
        # Antialiasing a live trace is pure cost; the peak decimation below is
        # what actually keeps the curve readable.
        pg.setConfigOptions(antialias=False)
        central = QtWidgets.QWidget(); central.setObjectName("centralHost")
        self.central = central
        self.setCentralWidget(central)
        layout = QtWidgets.QHBoxLayout(central); layout.setContentsMargins(10, 10, 10, 10); layout.setSpacing(10)

        # ---- left: controls over a faint backdrop, scrollable when cramped --
        self.controls = BackdropFrame(PANEL_BACKDROP, THEMES[self.theme_name]["backdrop_panel"],
                                      focus_x=PANEL_FOCUS, bg=THEMES[self.theme_name]["panel"],
                                      blur=THEMES[self.theme_name]["blur_panel"])
        self.controls.setObjectName("controlPanel")
        self.controls.setFixedWidth(340)
        outer = QtWidgets.QVBoxLayout(self.controls); outer.setContentsMargins(8, 8, 4, 8); outer.setSpacing(8)
        self.scroll = QtWidgets.QScrollArea(); self.scroll.setObjectName("panelScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setObjectName("panelViewport")
        self.scroll.viewport().setAutoFillBackground(False)
        inner = QtWidgets.QWidget(); inner.setObjectName("panelInner")
        inner.setAutoFillBackground(False)
        formbox = QtWidgets.QVBoxLayout(inner); formbox.setSpacing(9); formbox.setContentsMargins(2, 2, 6, 2)
        self.scroll.setWidget(inner); outer.addWidget(self.scroll, 1)

        # header: title + theme toggle
        header = QtWidgets.QHBoxLayout(); header.setSpacing(6)
        self.title_label = QtWidgets.QLabel(); self.title_label.setObjectName("panelTitle")
        self.title_label.setWordWrap(True)
        self.theme_btn = QtWidgets.QPushButton(); self.theme_btn.setObjectName("themeButton")
        self.theme_btn.setFixedSize(74, 26)
        self.theme_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.theme_btn.clicked.connect(self.toggle_theme)
        header.addWidget(self.title_label, 1)
        header.addWidget(self.theme_btn, 0, QtCore.Qt.AlignTop)
        formbox.addLayout(header)

        form = QtWidgets.QFormLayout(); form.setSpacing(7)
        form.setLabelAlignment(QtCore.Qt.AlignLeft)
        # In a narrow panel, let a row stack its field under the label rather
        # than squeezing both onto one line.
        # Every row stays on one line. WrapLongRows would wrap only the rows
        # whose label happens to be wide, giving a ragged mix of stacked and
        # inline rows; the fields are fixed-width, so inline always fits.
        form.setRowWrapPolicy(QtWidgets.QFormLayout.DontWrapRows)
        # Sized fields rather than edge-to-edge ones: the panel reads as a form,
        # not a stack of full-width bars.
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldsStayAtSizeHint)
        form.setFormAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self.device = QtWidgets.QLineEdit("Dev1")
        self.device.textEdited.connect(lambda *_: setattr(self, "_device_edited", True))
        self.channel_edits = [QtWidgets.QLineEdit(f"ai{i}") for i in range(3)]
        self.rate = QtWidgets.QSpinBox(); self.rate.setRange(10, 16000); self.rate.setValue(2000); self.rate.setSuffix(" Hz/ch")
        self.window_s = QtWidgets.QDoubleSpinBox(); self.window_s.setRange(.25, 30); self.window_s.setValue(5); self.window_s.setSingleStep(.5); self.window_s.setSuffix(" s")
        self.rms_window = QtWidgets.QDoubleSpinBox(); self.rms_window.setRange(.02, 10.0); self.rms_window.setDecimals(2)
        self.rms_window.setValue(DEFAULT_RMS_WINDOW); self.rms_window.setSingleStep(.05); self.rms_window.setSuffix(" s")
        self.rms_window.setToolTip("Averaging time for the RMS readout — independent of the plotted window.")
        self.y_min = QtWidgets.QDoubleSpinBox(); self.y_min.setRange(-10.0, 9.99); self.y_min.setDecimals(4); self.y_min.setSingleStep(0.1); self.y_min.setSuffix(" V")
        self.y_max = QtWidgets.QDoubleSpinBox(); self.y_max.setRange(-9.99, 10.0); self.y_max.setDecimals(4); self.y_max.setSingleStep(0.1); self.y_max.setSuffix(" V")
        # Set before connecting, so the handler cannot fire while the plots
        # and the status label do not exist yet.
        self.y_min.setValue(DEFAULT_V_MIN); self.y_max.setValue(DEFAULT_V_MAX)
        self.notch_hz = QtWidgets.QDoubleSpinBox(); self.notch_hz.setRange(1.0, 5000.0); self.notch_hz.setDecimals(1)
        self.notch_hz.setValue(DEFAULT_NOTCH_HZ); self.notch_hz.setSingleStep(10.0); self.notch_hz.setSuffix(" Hz")
        self.notch_hz.setToolTip("Mains frequency to remove. 50 Hz in Sri Lanka/Europe/Asia, 60 Hz in the Americas.")
        self.notch_q = QtWidgets.QDoubleSpinBox(); self.notch_q.setRange(1.0, 200.0); self.notch_q.setDecimals(1)
        self.notch_q.setValue(DEFAULT_NOTCH_Q); self.notch_q.setSingleStep(5.0)
        self.notch_q.setToolTip("Quality factor: higher is a narrower notch, so less EMG is removed "
                                "along with the hum. The -3 dB bandwidth is frequency / Q.")
        self.notch_harmonics = QtWidgets.QSpinBox(); self.notch_harmonics.setRange(1, 10)
        self.notch_harmonics.setValue(DEFAULT_NOTCH_HARMONICS)
        self.notch_harmonics.setToolTip("How many multiples of the mains frequency to notch. "
                                        "Harmonics at or above Nyquist are skipped automatically.")
        for _w in (self.notch_hz, self.notch_q, self.notch_harmonics):
            _w.valueChanged.connect(self.apply_notch_settings)
        self.terminal = QtWidgets.QComboBox(); self.terminal.addItems(TERMINALS)
        self.terminal.setToolTip("DAQmx default is recommended: it lets NI-DAQmx pick "
                                 "the USB-6009 terminal configuration.")
        # Otherwise the longest item dictates a ~350px minimum for the panel.
        self.terminal.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.terminal.setMinimumContentsLength(10)
        for _w in (self.device, *self.channel_edits):
            _w.setFixedWidth(FIELD_W)
        for _w in (self.rate, self.window_s, self.rms_window, self.y_min, self.y_max,
                   self.notch_hz, self.notch_q, self.notch_harmonics):
            _w.setFixedWidth(SPIN_W)
        self.terminal.setFixedWidth(COMBO_W)
        self.y_min.valueChanged.connect(self.apply_voltage_scale)
        self.y_max.valueChanged.connect(self.apply_voltage_scale)
        self.window_s.valueChanged.connect(self._rebuild_display_buffers)
        self.rms_window.valueChanged.connect(self._rebuild_rms_buffers)
        def add_row(text, widget):
            # A fixed, wrapping label column bounds the row's minimum width:
            # a wide font wraps the label instead of forcing the panel wider
            # (or clipping the text).
            label = QtWidgets.QLabel(text)
            label.setFixedWidth(LABEL_W)
            label.setWordWrap(True)
            form.addRow(label, widget)

        add_row("Device", self.device)
        for i, e in enumerate(self.channel_edits): add_row(f"Channel {i+1}", e)
        add_row("Sample rate", self.rate)
        add_row("Display window", self.window_s)
        add_row("RMS window", self.rms_window)
        add_row("Voltage min", self.y_min)
        add_row("Voltage max", self.y_max)
        add_row("Input mode", self.terminal)
        add_row("Notch frequency", self.notch_hz)
        add_row("Notch Q", self.notch_q)
        add_row("Notch harmonics", self.notch_harmonics)
        formbox.addLayout(form)

        # ---- save location ------------------------------------------------ #
        self.save_head = QtWidgets.QLabel("Save recordings to"); self.save_head.setObjectName("sectionLabel")
        formbox.addWidget(self.save_head)
        save_row = QtWidgets.QHBoxLayout(); save_row.setSpacing(6)
        self.save_path_label = QtWidgets.QLabel(); self.save_path_label.setObjectName("savePath")
        self.save_path_label.setMinimumHeight(28)
        # Ignored width: the label takes whatever space is left and elides into
        # it. Without this a deep folder path widens the whole panel.
        self.save_path_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.save_path_label.setMinimumWidth(60)
        self.browse_btn = QtWidgets.QPushButton("Browse…"); self.browse_btn.setObjectName("neutralButton")
        self.browse_btn.setFixedWidth(78)
        self.browse_btn.clicked.connect(self.choose_save_dir)
        save_row.addWidget(self.save_path_label, 1); save_row.addWidget(self.browse_btn)
        formbox.addLayout(save_row)
        self._update_save_label()

        self.start_btn = QtWidgets.QPushButton("▶  Start acquisition"); self.start_btn.setObjectName("startButton"); self.start_btn.clicked.connect(self.start)
        self.record_btn = QtWidgets.QPushButton("●  Start recording"); self.record_btn.setObjectName("recordButton"); self.record_btn.setEnabled(False); self.record_btn.clicked.connect(self.toggle_record)
        self.clear_btn = QtWidgets.QPushButton("Clear display"); self.clear_btn.setObjectName("neutralButton"); self.clear_btn.clicked.connect(self.clear)
        self.notch_btn = QtWidgets.QPushButton(); self.notch_btn.setObjectName("notchButton")
        self.notch_btn.setCheckable(True)
        self.notch_btn.setToolTip("Apply a digital IIR notch filter to the live traces and to "
                                  "everything written to CSV.")
        self.notch_btn.toggled.connect(self.toggle_notch)
        self.simulate = QtWidgets.QCheckBox("Simulation mode (no DAQ)"); self.simulate.setObjectName("simCheck")
        self.simulate.toggled.connect(self.refresh_device_card)
        formbox.addWidget(self.start_btn); formbox.addWidget(self.record_btn)
        formbox.addWidget(self.notch_btn)
        formbox.addWidget(self.clear_btn); formbox.addWidget(self.simulate)
        self.status = QtWidgets.QLabel(); self.status.setObjectName("statusLabel"); self.status.setWordWrap(True)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Minimum)
        self.status.setMinimumWidth(60)
        formbox.addWidget(self.status)
        formbox.addStretch()
        self.note = QtWidgets.QLabel("USB-6009 supports up to 48 kS/s aggregate. 3 × 2,000 Hz = 6 kS/s.")
        self.note.setObjectName("noteLabel"); self.note.setWordWrap(True)
        self.note.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Minimum)
        self.note.setMinimumWidth(60)
        formbox.addWidget(self.note)

        # Pinned outside the scroll area, so the connection state is always
        # visible at the bottom of the panel however far the form is scrolled.
        self.device_card = DeviceCard()
        outer.addWidget(self.device_card, 0)
        layout.addWidget(self.controls)

        # ---- right: plots over their own faint backdrop -------------------- #
        self.graphs = BackdropFrame(GRAPH_BACKDROP, THEMES[self.theme_name]["backdrop_graph"],
                                    focus_x=GRAPH_FOCUS, bg=THEMES[self.theme_name]["graph_bg"],
                                    blur=THEMES[self.theme_name]["blur_graph"])
        self.graphs.setObjectName("graphPanel")
        right = QtWidgets.QVBoxLayout(self.graphs); right.setContentsMargins(8, 8, 8, 8); right.setSpacing(8)
        layout.addWidget(self.graphs, 1)
        self.alert = AlertOverlay(self.graphs)
        self.plots=[]; self.curves=[]; self.rms_labels=[]
        for i in range(3):
            row=QtWidgets.QHBoxLayout(); row.setSpacing(8)
            plot=pg.PlotWidget()
            # Transparent plots let the backdrop show through behind the traces.
            # setBackground(None) only drops pyqtgraph's own fill; without the
            # stylesheet the viewport still paints the palette's (light) base.
            # Both object names are scoped so the rule cannot leak further.
            plot.setObjectName(f"plot{i}")
            plot.viewport().setObjectName(f"plotViewport{i}")
            plot.setBackground(None)
            plot.viewport().setAutoFillBackground(False)
            plot.setStyleSheet(f"QWidget#plot{i},QWidget#plotViewport{i}{{background:transparent}}")
            plot.setMouseEnabled(x=False, y=True)
            plot.setMenuEnabled(False); plot.hideButtons(); plot.setClipToView(True)
            curve=plot.plot(pen=pg.mkPen(COLORS[i], width=1.2)); row.addWidget(plot, 1)
            rms=QtWidgets.QLabel("RMS\n— mV"); rms.setFixedWidth(76); rms.setAlignment(QtCore.Qt.AlignCenter)
            r, g, b = COLORS[i]
            rms.setObjectName(f"rmsChip{i}")
            rms.setStyleSheet(
                f"QLabel#rmsChip{i}{{color:#0d141c;font-weight:bold;border-radius:10px;"
                f"border:1px solid rgba(255,255,255,0.35);"
                f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                f"stop:0 rgba({r},{g},{b},0.95), stop:1 rgba({r},{g},{b},0.72))}}")
            row.addWidget(rms); right.addLayout(row)
            self.plots.append(plot); self.curves.append(curve); self.rms_labels.append(rms)

    # -- theming -------------------------------------------------------------- #
    def toggle_theme(self):
        self.apply_theme("light" if self.theme_name == "dark" else "dark")

    def apply_theme(self, name):
        self.theme_name = name if name in THEMES else "dark"
        th = THEMES[self.theme_name]
        self.theme = th
        self.settings.setValue("theme", self.theme_name)
        self.theme_btn.setText("☀  Light" if self.theme_name == "dark" else "🌙  Dark")
        self.theme_btn.setToolTip("Switch to the light theme" if self.theme_name == "dark"
                                  else "Switch to the dark theme")
        self.central.setStyleSheet(f"QWidget#centralHost{{background:{th['app_bg']}}}")
        ARROW_UP = arrow_image("up", th["field_fg"])
        ARROW_DOWN = arrow_image("down", th["field_fg"])
        self.title_label.setText(
            f"<h2 style='margin-bottom:0;color:{th['heading']}'>EMG Recorder</h2>"
            f"<p style='margin-top:2px;color:{th['muted']}'>NI USB-6009 · 3 channels</p>")
        # Every rule is object-name scoped: a bare "background:transparent"
        # cascades into the input fields and repaints them.
        self.controls.setStyleSheet(f"""
            QFrame#controlPanel{{background:transparent}}
            QScrollArea#panelScroll{{background:transparent;border:none}}
            QWidget#panelViewport{{background:transparent}}
            QWidget#panelInner{{background:transparent}}
            QLabel{{background:transparent;color:{th['text']}}}
            QLabel#sectionLabel{{color:{th['text']};font-weight:bold}}
            QLabel#noteLabel{{color:{th['muted']};font-size:11px}}
            QLabel#savePath{{background:{th['glass']};color:{th['text']};
                             border:1px solid {th['glass_rim']};border-radius:8px;
                             padding:6px;font-size:11px}}
            QCheckBox#simCheck{{background:transparent;color:{th['text']};spacing:7px}}
            QCheckBox#simCheck::indicator{{width:15px;height:15px;border-radius:5px;
                border:1px solid {th['glass_rim']};background:{th['glass']}}}
            QCheckBox#simCheck::indicator:checked{{background:rgba(0,196,180,0.90);
                border:1px solid rgba(0,196,180,0.95)}}
            /* Inputs are glass in the same key as the panel, not white slabs:
               translucent fill, light rim, theme-coloured text. */
            QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox{{padding:5px 9px;
                background:{th['field_bg']};color:{th['field_fg']};
                border:1px solid {th['field_border']};border-radius:8px;
                selection-background-color:rgba(0,196,180,0.45);
                selection-color:{th['field_fg']}}}
            QLineEdit:hover,QComboBox:hover,QSpinBox:hover,QDoubleSpinBox:hover{{
                background:{th['field_hover']}}}
            QLineEdit:focus,QComboBox:focus,QSpinBox:focus,QDoubleSpinBox:focus{{
                border:1px solid {th['field_focus']};background:{th['field_hover']}}}
            QLineEdit:disabled,QComboBox:disabled,QSpinBox:disabled,QDoubleSpinBox:disabled{{
                background:{th['glass']};color:{th['muted']};
                border:1px solid {th['glass_rim']}}}
            /* Flat, rimless steppers — the default framed buttons render as
               bright slabs on a dark field. */
            QSpinBox::up-button,QDoubleSpinBox::up-button{{subcontrol-origin:border;
                subcontrol-position:top right;width:17px;margin:1px 1px 0 0;
                border:none;background:transparent;border-top-right-radius:7px}}
            QSpinBox::down-button,QDoubleSpinBox::down-button{{subcontrol-origin:border;
                subcontrol-position:bottom right;width:17px;margin:0 1px 1px 0;
                border:none;background:transparent;border-bottom-right-radius:7px}}
            QSpinBox::up-button:hover,QDoubleSpinBox::up-button:hover,
            QSpinBox::down-button:hover,QDoubleSpinBox::down-button:hover{{
                background:{th['glass_hi']}}}
            QSpinBox::up-arrow,QDoubleSpinBox::up-arrow{{
                image:url("{ARROW_UP}");width:9px;height:9px}}
            QSpinBox::down-arrow,QDoubleSpinBox::down-arrow{{
                image:url("{ARROW_DOWN}");width:9px;height:9px}}
            QComboBox::drop-down{{border:none;width:20px}}
            QComboBox::down-arrow{{image:url("{ARROW_DOWN}");width:9px;height:9px}}
            QComboBox QAbstractItemView{{background:{th['menu_bg']};
                color:{th['text']};border:1px solid {th['glass_rim']};
                border-radius:8px;padding:4px;outline:none;
                selection-background-color:rgba(0,196,180,0.35);
                selection-color:{th['text']}}}
            QPushButton{{padding:9px;font-weight:bold;border-radius:9px;
                border:1px solid {th['glass_rim']}}}
            QPushButton#startButton{{color:#ffffff}}
            QPushButton#recordButton{{color:#ffffff;
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 rgba(224,94,104,0.92), stop:1 rgba(196,62,72,0.92));
                border:1px solid rgba(255,255,255,0.28)}}
            QPushButton#recordButton:disabled{{background:{th['glass']};
                color:{th['muted']};border:1px solid {th['glass_rim']}}}
            QPushButton#neutralButton{{padding:7px;color:{th['glass_fg']};
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 {th['glass_hi']}, stop:1 {th['glass']})}}
            QPushButton#neutralButton:hover{{background:{th['glass_hi']}}}
            QPushButton#themeButton{{padding:2px;font-weight:bold;font-size:11px;
                border-radius:13px;color:{th['glass_fg']};
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 {th['glass_hi']}, stop:1 {th['glass']})}}
            QPushButton#themeButton:hover{{background:{th['glass_hi']}}}
            QScrollBar:vertical{{background:transparent;width:8px;margin:0}}
            QScrollBar::handle:vertical{{background:{th['scroll_handle']};border-radius:4px;min-height:30px}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0}}
            QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent}}
        """)
        self.graphs.setStyleSheet("QFrame#graphPanel{background:transparent}")
        self.controls.set_appearance(th["panel"], th["backdrop_panel"], th["blur_panel"])
        self.graphs.set_appearance(th["graph_bg"], th["backdrop_graph"], th["blur_graph"])
        for i, plot in enumerate(self.plots):
            self._style_plot(plot, i, th)
        self.device_card.set_theme(self.theme_name)
        self._apply_start_button_style()
        self._style_notch_button()
        self.set_status(self._status_text, self._status_error)

    def _style_plot(self, plot, index, th):
        ink = th["axis"]
        pen = pg.mkPen(ink)
        for axis_name in ("left", "bottom"):
            axis = plot.getAxis(axis_name)
            axis.setPen(pen)
            axis.setTextPen(pen)
        plot.setTitle(f"Channel {index+1}  ·  raw EMG voltage", color=ink, size="10pt")
        plot.setLabel("left", "Voltage", units="V", color=ink)
        plot.setLabel("bottom", "Time", units="s", color=ink)
        plot.showGrid(x=True, y=True, alpha=th["grid_alpha"])

    def _apply_start_button_style(self):
        """The Start button doubles as Stop, so it carries an inline colour."""
        running = self.worker is not None
        top, bottom = (("rgba(122,134,148,0.92)", "rgba(92,104,118,0.92)") if running
                       else ("rgba(31,190,133,0.92)", "rgba(18,148,102,0.92)"))
        self.start_btn.setStyleSheet(
            f"QPushButton#startButton{{color:#ffffff;border:1px solid rgba(255,255,255,0.28);"
            f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 {top}, stop:1 {bottom})}}")

    # -- notch filter --------------------------------------------------------- #
    def build_notch(self):
        """Construct a filter for the current rate and settings, or None if off."""
        if not self.notch_btn.isChecked():
            return None
        return NotchFilter(self.fs, self.notch_hz.value(), self.notch_q.value(),
                           self.notch_harmonics.value(), channels=3)

    def apply_notch_settings(self):
        """Rebuild the filter and hand it to the worker.

        A new filter starts from zero state, so changing settings mid-stream
        produces a brief transient - the alternative is silently filtering with
        stale coefficients, which is worse.
        """
        self.notch = self.build_notch()
        if self.worker:
            self.worker.notch = self.notch
        self._style_notch_button()
        if self.notch is None:
            return
        if not self.notch.active:
            self.set_status(
                f"Notch off: {self.notch.describe()} at {self.fs:g} Hz sampling.", True)
            return
        self.set_status(f"Notch filter on — {self.notch.describe()}")

    def toggle_notch(self, checked):
        self.apply_notch_settings()
        if not checked:
            self.set_status("Notch filter off — recording raw signal")

    def _style_notch_button(self):
        on = self.notch_btn.isChecked()
        active = on and self.notch is not None and self.notch.active
        # Kept short deliberately: a QPushButton cannot elide, so a long label
        # sets a minimum width and would widen the whole panel. The frequency
        # is already on the spin box directly above and in the status line.
        self.notch_btn.setText("◉  Notch filter ON" if on else "◎  Notch filter OFF")
        # Amber rather than green: filtering is a deviation from the raw signal
        # and should read as a state worth noticing, not a default.
        if on:
            top, bottom = "rgba(226,150,45,0.92)", "rgba(196,120,24,0.92)"
            fg, rim = "#ffffff", "rgba(255,255,255,0.30)"
        else:
            th = THEMES[self.theme_name]
            top, bottom = th["glass_hi"], th["glass"]
            fg, rim = th["glass_fg"], th["glass_rim"]
        self.notch_btn.setStyleSheet(
            f"QPushButton#notchButton{{color:{fg};border:1px solid {rim};"
            f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 {top}, stop:1 {bottom})}}")

    def _set_notch_controls_enabled(self, enabled):
        for widget in (self.notch_btn, self.notch_hz, self.notch_q, self.notch_harmonics):
            widget.setEnabled(enabled)

    # -- alerts --------------------------------------------------------------- #
    def raise_alert(self, title, detail):
        """Make a fault impossible to miss: banner, title bar and taskbar."""
        self.alert.show_alert(title, detail)
        self.setWindowTitle(f"⚠  {title} — {APP_TITLE}")
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.alert(self, 3000)          # flash the taskbar entry

    def clear_alert(self):
        self.alert.hide_alert()
        self.setWindowTitle(APP_TITLE)

    # -- device presence ----------------------------------------------------- #
    @QtCore.Slot(object)
    def on_devices_changed(self, snapshot):
        """Called on the GUI thread whenever the attached device set changes."""
        previous = self._devices
        if snapshot is None:
            self._devices = []
            self.refresh_device_card()
            return
        if isinstance(snapshot, dict):           # enumeration itself failed
            self._devices = []
            self._scan_error = snapshot.get("error", "")
            self.refresh_device_card()
            return
        self._scan_error = None
        self._devices = snapshot
        if snapshot and not self._device_edited:
            self.device.setText(snapshot[0]["name"])
        if snapshot and not previous:
            first = snapshot[0]
            self.clear_alert()
            self.set_status(f"{first['product']} detected on {first['name']}")
        elif previous and not snapshot:
            message = "DAQ disconnected — reconnect the USB cable."
            if self.worker and not self.worker.simulate:
                rows = self.recorder.rows if self.recorder else 0
                saved = self.record_path.name if self.recording else None
                self.stop()                      # writes its own status line
                if saved:
                    message += f"  ·  recording saved: {rows:,} samples → {saved}"
            self.set_status(message, True)
            self.raise_alert("DAQ DISCONNECTED",
                             "Reconnect the USB cable. Acquisition has stopped; "
                             "any recording was saved.")
        self.refresh_device_card()

    def refresh_device_card(self):
        if self.simulate.isChecked():
            self.clear_alert()
            self.device_card.set_state("sim", "Generating synthetic EMG — no hardware required.")
            return
        if not NIDAQMX_AVAILABLE:
            muted = THEMES[self.theme_name]["muted"]
            detail = ("Install the NI-DAQmx driver from ni.com, then restart. "
                      "Simulation mode works without it.")
            if NIDAQMX_ERROR:
                reason = NIDAQMX_ERROR
                if len(reason) > 160:
                    reason = reason[:157] + "…"
                detail += f"<br><span style='color:{muted}'>{reason}</span>"
            self.device_card.set_state("nodriver", detail)
            return
        if self._scan_error:
            self.device_card.set_state("error", friendly_daq_error(self._scan_error))
            return
        if not self._devices:
            self.device_card.set_state("error", "No NI device attached. Plug in the USB-6009.")
            return
        muted = THEMES[self.theme_name]["muted"]
        lines = []
        for d in self._devices:
            serial = d.get("serial") or 0
            lines.append(f"<b>{d['product']}</b> &nbsp;·&nbsp; {d['name']}<br>"
                         f"<span style='color:{muted}'>Serial {serial:08X}</span>")
        self.device_card.set_state("ok", "<br>".join(lines))

    # -- save location -------------------------------------------------------- #
    def _update_save_label(self):
        text = str(self.save_dir)
        metrics = self.save_path_label.fontMetrics()
        width = max(80, self.save_path_label.width() - 12)
        self.save_path_label.setText(metrics.elidedText(text, QtCore.Qt.ElideMiddle, width))
        self.save_path_label.setToolTip(text)

    def choose_save_dir(self):
        start = str(self.save_dir if self.save_dir.exists() else Path.cwd())
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose a folder for CSV recordings", start)
        if not chosen:
            return
        self.save_dir = Path(chosen)
        self.settings.setValue("save_dir", str(self.save_dir))
        self._update_save_label()
        self.set_status(f"Recordings will be saved to {self.save_dir}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_save_label()

    # -- buffer sizing ------------------------------------------------------- #
    def _rebuild_display_buffers(self):
        """Resize the decimating rings for the current rate and plotted window."""
        window = float(self.window_s.value())
        max_n = max(2, int(round(window * self.fs)))
        dec = max(1, int(math.ceil(max_n / DISPLAY_POINTS)))
        cap = max(2, int(math.ceil(max_n / dec)))
        for ring in self.rings:
            ring.configure(dec, cap)
        step = dec / self.fs
        self._x_full = (np.arange(cap) - cap + 1) * step
        self._x_rep = np.repeat(self._x_full, 2)
        for plot in self.plots:
            plot.setXRange(-window, 0, padding=0)
        for curve in self.curves:
            curve.clear()

    def _rebuild_rms_buffers(self):
        """RMS averages over its own window, so it can react far faster."""
        span = max(2, int(round(float(self.rms_window.value()) * self.fs)))
        for ring in self.rms_rings:
            ring.configure(span)

    # -- acquisition --------------------------------------------------------- #
    def _set_inputs_enabled(self, enabled):
        for widget in (self.device, self.rate, self.terminal, self.simulate, *self.channel_edits):
            widget.setEnabled(enabled)

    def start(self):
        if self.worker: self.stop(); return
        fs=int(self.rate.value()); channels=[x.text().strip() for x in self.channel_edits]
        if not self.simulate.isChecked():
            if not self.device.text().strip() or any(not c for c in channels):
                self.set_status("Enter device and all 3 channel names.", True); return
            if NIDAQMX_AVAILABLE and self._devices:
                names = [d["name"] for d in self._devices]
                if self.device.text().strip() not in names:
                    self.set_status(f"'{self.device.text().strip()}' is not attached. Found: {', '.join(names)}", True)
                    return
            elif NIDAQMX_AVAILABLE and not self._devices:
                self.set_status("No DAQ attached — plug in the USB-6009 or tick Simulation mode.", True); return
        self.fs = fs
        self._rebuild_display_buffers(); self._rebuild_rms_buffers(); self.clear(); self.samples = 0
        while True:
            try: self.data_queue.get_nowait()
            except queue.Empty: break
        self.worker=AcquisitionWorker(self.device.text().strip(),channels,fs,self.terminal.currentText(),max(20,fs//20),self.data_queue,self.simulate.isChecked())
        self.notch = self.build_notch()          # fs may have changed; rebuild
        self.worker.notch = self.notch
        self.worker.start()
        self.clear_alert()
        self._set_inputs_enabled(False)
        self._style_notch_button()
        self.start_btn.setText("■  Stop acquisition"); self._apply_start_button_style()
        self.record_btn.setEnabled(True); self.set_status("Acquiring live data")

    def stop(self):
        if self.recording: self.toggle_record()
        if self.worker: self.worker.stop(); self.worker.join(timeout=2); self.worker=None
        self._set_inputs_enabled(True)
        self.start_btn.setText("▶  Start acquisition"); self._apply_start_button_style()
        self.record_btn.setEnabled(False)

    # -- recording ----------------------------------------------------------- #
    def toggle_record(self):
        if not self.recording:
            try:
                self.save_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                self.set_status(f"Cannot create {self.save_dir}: {exc}", True); return
            stamp = datetime.now().strftime("EMG_%Y%m%d_%H%M%S")
            # Put the processing in the filename: a CSV of filtered samples that
            # looks identical to a raw one is a trap months later.
            if self.notch is not None and self.notch.active:
                stamp += "_notch%gHz" % self.notch.freq
            self.record_path = self.save_dir / (stamp + ".csv")
            # Big queue: a slow disk buffers here instead of stalling the UI.
            self.record_queue = queue.Queue(maxsize=4000)
            self.recorder = CsvRecorder(self.record_path, self.fs, self.record_queue)
            self.recorder.start()
            if self.worker: self.worker.record_queue = self.record_queue
            self.recording=True; self.record_btn.setText("■  Stop recording")
            # Frozen while recording: a file whose samples change processing
            # halfway through cannot be analysed, and its name would lie.
            self._set_notch_controls_enabled(False)
            self.set_status(f"RECORDING → {self.record_path}")
        else:
            if self.worker: self.worker.record_queue = None
            if self.recorder:
                self.record_queue.put(None)
                self.recorder.join(timeout=10)
                rows, err = self.recorder.rows, self.recorder.error
            else:
                rows, err = 0, None
            self.recorder=None; self.record_queue=None; self.recording=False
            self.record_btn.setText("●  Start recording")
            self._set_notch_controls_enabled(True)
            if err: self.set_status(f"Recording error: {err}", True)
            else: self.set_status(f"Saved {rows:,} samples → {self.record_path}")

    # -- per-frame update ---------------------------------------------------- #
    def consume_data(self):
        if self._busy:
            return
        if self.worker and self.worker.error:
            # stop() closes the recording and writes its own status line, so
            # report the failure *after* it — otherwise "Saved N samples"
            # hides the reason acquisition ended.
            message = self.worker.error
            rows = self.recorder.rows if self.recorder else 0
            saved = self.record_path.name if self.recording else None
            self.stop()
            if saved:
                message += f"  ·  recording saved: {rows:,} samples → {saved}"
            self.set_status(message, True)
            self.device_card.set_state("error", message)
            self.raise_alert("ACQUISITION STOPPED", message)
            return
        if self.recorder and self.recorder.error:
            self.set_status("Recording error: " + self.recorder.error, True); self.toggle_record(); return
        self._busy = True
        try:
            got = False
            while True:
                try: _, block = self.data_queue.get_nowait()
                except queue.Empty: break
                got = True
                self.samples += block.shape[1]
                for i in range(3):
                    self.rings[i].append(block[i])
                    self.rms_rings[i].append(block[i])
            if not got:
                return
            for i in range(3):
                mins, maxs = self.rings[i].ordered()
                count = mins.size
                if not count:
                    continue
                # pyqtgraph keeps a *reference* to whatever is handed to
                # setData, so every curve needs its own array: a shared scratch
                # buffer would leave all three plots showing the last channel,
                # and a view into the live ring would be overwritten in place.
                if self.rings[i].dec == 1:
                    self.curves[i].setData(self._x_full[-count:], maxs.copy())
                else:
                    y = np.empty(2 * count)
                    y[0::2] = mins; y[1::2] = maxs
                    self.curves[i].setData(self._x_rep[-2 * count:], y)
            now = time.monotonic()
            if now - self._last_rms >= RMS_INTERVAL_MS / 1000.0:
                self._last_rms = now
                for i in range(3):
                    self.rms_labels[i].setText(f"RMS\n{self.rms_rings[i].value()*1000:.2f} mV")
                if self.recording and self.recorder:
                    self.set_status(f"RECORDING → {self.record_path.name}  ·  {self.recorder.rows:,} samples")
        finally:
            self._busy = False

    def apply_voltage_scale(self):
        """Apply the user-entered vertical display limits in volts to all channels."""
        low, high = self.y_min.value(), self.y_max.value()
        if low >= high:
            self.set_status("Voltage minimum must be lower than voltage maximum.", True)
            return
        for plot in self.plots:
            plot.setYRange(low, high, padding=0)
        self.set_status(f"Voltage scale set to {low:g} V to {high:g} V")

    def clear(self):
        self.samples = 0
        for ring, rms in zip(self.rings, self.rms_rings):
            ring.clear(); rms.clear()
        for curve, lab in zip(self.curves, self.rms_labels):
            curve.clear(); lab.setText("RMS\n— mV")

    def set_status(self, text, error=False):
        self._status_text, self._status_error = text, error
        colour = THEMES[self.theme_name]["err" if error else "ok"]
        self.status.setText("● " + text.lstrip("● "))
        self.status.setStyleSheet(f"QLabel#statusLabel{{background:transparent;color:{colour}}}")

    def closeEvent(self, event):
        self.timer.stop(); self.monitor.stop(); self.stop(); event.accept()


if __name__ == "__main__":
    claim_taskbar_identity()          # must precede the first window
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("EMG Recorder")
    app.setWindowIcon(application_icon())
    window = MainWindow()
    window.showMaximized()   # fill the screen instead of a fixed 1300x840
    sys.exit(app.exec())
