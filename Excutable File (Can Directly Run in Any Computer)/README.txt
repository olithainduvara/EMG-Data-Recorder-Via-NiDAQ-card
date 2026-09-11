EMG Recorder - NI USB-6009
==========================

RUNNING IT
----------
Copy "EMG Recorder.exe" to any 64-bit Windows 10/11 PC and double-click it.
Nothing to install: Python, PySide6, numpy and pyqtgraph are all inside the
file. No admin rights needed.

First launch takes ~5-10 seconds (the app unpacks itself to a temp folder).
Later launches are quicker.

Windows may show "Windows protected your PC" because the file is not code-
signed. Click "More info" -> "Run anyway". This appears for any unsigned
program; buying a code-signing certificate is the only way to remove it.


TALKING TO THE USB-6009
-----------------------
Reading real signals needs the NI-DAQmx driver from National Instruments on
that PC. That driver is a Windows system component and CANNOT be packed into
this .exe - install it separately:

    https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html

Without the driver the app still starts and the panel says
"NI-DAQmx UNAVAILABLE", with the exact reason underneath. Tick
"Simulation mode (no DAQ)" to use the whole interface with synthetic EMG -
useful for demos and for testing the layout.

After installing the driver, restart the app: the check runs once at startup.
The panel should then show "DAQ CONNECTED" with the model and serial number.


WHERE RECORDINGS GO
-------------------
By default:  Documents\EMG Recordings\EMG_<date>_<time>.csv
Change it with the "Browse..." button; the choice is remembered.

CSV columns: time_s, channel_1_V, channel_2_V, channel_3_V


OTHER OPERATING SYSTEMS
-----------------------
This .exe is Windows-only. macOS and Linux need a build made on that OS
(same command, see below). NI-DAQmx support on those platforms is limited.


REBUILDING AFTER EDITING THE PYTHON FILE
----------------------------------------
The source "Data collect2.py" is unchanged and still runs directly with
    python "Data collect2.py"

To rebuild the .exe:
    pip install pyinstaller
    pyinstaller "EMG Recorder.spec" --noconfirm

The result lands in dist\. Build intermediates go to build\ and can be
deleted. If this folder is inside OneDrive, consider building to a local
path instead so OneDrive does not sync ~70 MB on every build:
    pyinstaller "EMG Recorder.spec" --noconfirm --workpath %TEMP%\emgbuild
