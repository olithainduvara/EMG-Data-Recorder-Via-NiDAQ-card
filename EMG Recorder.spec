# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the EMG Recorder.

Build:   pyinstaller "EMG Recorder.spec" --noconfirm
Output:  dist/EMG Recorder.exe  (single file, no Python needed on the target)

Note: the NI-DAQmx *driver* cannot be bundled — it is a system component from
National Instruments. Without it the app still runs and simulation mode works,
but talking to a USB-6009 requires the driver on that machine.
"""

from PyInstaller.utils.hooks import copy_metadata

block_cipher = None

# nitypes (a nidaqmx dependency) calls importlib.metadata.version() at import
# time, and nidaqmx does the same. PyInstaller bundles the CODE but not the
# .dist-info metadata, so without this the whole nidaqmx import raises
# PackageNotFoundError and the app reports "NI-DAQmx unavailable" on machines
# that do have the driver installed.
METADATA = []
for _pkg in ('nidaqmx', 'nitypes', 'hightime', 'deprecation', 'tzlocal', 'packaging'):
    try:
        METADATA += copy_metadata(_pkg)
    except Exception:
        pass

# Qt ships far more than this app uses; dropping the unused modules roughly
# halves the executable.
EXCLUDES = [
    'PyQt5', 'PyQt6', 'PySide2', 'tkinter', 'matplotlib', 'scipy', 'pandas',
    'IPython', 'pytest', 'setuptools', 'pip', 'PIL',
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
    'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQuickWidgets', 'PySide6.QtQml',
    'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtSpatialAudio',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput', 'PySide6.Qt3DLogic',
    'PySide6.Qt3DAnimation', 'PySide6.Qt3DExtras',
    'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtGraphs',
    'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning',
    'PySide6.QtLocation', 'PySide6.QtSerialPort', 'PySide6.QtSerialBus',
    'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtDesigner', 'PySide6.QtHelp',
    'PySide6.QtUiTools', 'PySide6.QtNetworkAuth', 'PySide6.QtRemoteObjects',
    'PySide6.QtScxml', 'PySide6.QtSensors', 'PySide6.QtStateMachine',
    'PySide6.QtTextToSpeech', 'PySide6.QtWebChannel', 'PySide6.QtWebSockets',
    'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtDBus',
]

a = Analysis(
    ['Data collect2.py'],
    pathex=[],
    binaries=[],
    # app_icon.ico is bundled as DATA too, not just passed to icon= below:
    # icon= sets the .exe file icon, the bundled copy is what Qt loads for
    # the taskbar and title bar at runtime.
    datas=[('cover4.jpg', '.'), ('cover5.jpg', '.'),
           ('app_icon.ico', '.'), ('docs/icon.png', '.')] + METADATA,
    hiddenimports=['pyqtgraph', 'nidaqmx'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='EMG Recorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # windowed app: no console flashes on launch
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app_icon.ico',
)
