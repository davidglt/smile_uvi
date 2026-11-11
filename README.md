

# SMILE UVI – Daily Sampler

A desktop GUI tool that downloads daily Ultra-Violet Imager (UVI) quick-look
PNG frames from the ESA SMILE mission archive, renders annotated frames,
and produces a short MP4 video for quick visual inspection.

**Version:** 1.1

## Features

- **Query** the ESA EMDs TAP service for UVI products on a given UTC day.
- **Filter** results by an optional filename prefix before slot selection.
- **Download** one representative PNG per time slot (configurable count),
  with automatic HTTP retry and PNG integrity validation (magic-byte check
  plus Pillow `verify()`).
- **Render** annotated frames (slot time range, observation timestamp,
  filename).
- **Create** an MP4 video from the rendered frames via OpenCV
  (`cv2.VideoWriter`, `mp4v` codec) — no external codec binary required.
- **Play** the resulting video in a built-in OpenCV-based player
  (`cv2.VideoCapture`) with seek bar, Play/Pause, Stop, and frame-skip
  (±5) controls.
- **Catalogue** all previously generated videos with bulk delete.
- **About** dialog showing version, author, license, and a button to open
  the LICENSE file.

## Requirements

| Dependency | Version |
|---|---|
| Python | ≥ 3.9 |
| requests | ≥ 2.28 |
| Pillow | ≥ 9.0 |
| opencv-python | ≥ 4.5 |

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/davidglt/smile-uvi.git
cd smile-uvi
```

### 2. Create and activate a virtual environment

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell or CMD)**

```bat
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

## Usage

```bash
python smile_uvi.py
```

1. Set the **Date**, **Number of frames**, **Video FPS**, and optional
   **Filename prefix** in the right-hand panel.
2. Click **Download** to query the archive, fetch the PNGs, and render
   the annotated frames.
3. Click **Create Video** to encode the frames into an MP4.
4. The video loads automatically into the player on the left.
   Use the seek bar, Play/Pause, and frame-skip buttons to navigate.
5. The **Video Catalog** lists all previously generated MP4s.
   Check boxes and click **Delete** to remove them.

All data is stored under a local `data/` directory:

```
data/
└── YYYYMMDD/
    ├── originals/          # downloaded PNGs
    ├── frames_Nsamples/    # rendered annotated frames
    ├── manifest_Nsamples.csv
    └── smile_uvi_YYYYMMDD_Nsamples_Ffps.mp4
```

## Architecture

| Module | Role |
|---|---|
| `smile_uvi.py` | GUI (tkinter), HTTP session, download, render, video, player |
| `smile_sampling.py` | Timestamp parsing and per-slot sample selection |

Video encoding and playback are handled entirely by **OpenCV**; no external
codec binary (e.g. ffmpeg) is required at runtime.

## License

This project is licensed under the GNU General Public License v3.0 or
later - see the [LICENSE](LICENSE) file for details.
