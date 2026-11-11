

# SMILE UVI – Daily Sampler

A desktop GUI tool that downloads daily Ultra-Violet Imager (UVI) quick-look
PNG frames from the ESA SMILE mission archive, renders annotated frames,
and produces a short MP4 video for quick visual inspection.

## Features

- **Query** the ESA EMDs TAP service for UVI products on a given UTC day.
- **Download** one representative PNG per time slot (configurable count).
- **Render** annotated frames (timestamp, observation time, filename).
- **Create** an MP4 video from the rendered frames via FFmpeg.
- **Play** the resulting video in a built-in OpenCV-based player with
  seek, play/pause, and frame-skip controls.
- **Catalogue** all previously generated videos with bulk delete.

## Requirements

| Dependency | Version |
|---|---|
| Python | ≥ 3.9 |
| requests | ≥ 2.28 |
| Pillow | ≥ 9.0 |
| opencv-python | ≥ 4.5 |

## Installation

```bash
git clone https://github.com/davidglt/smile-uvi.git
cd smile-uvi
python -m venv .venv
source .venv/bin/activate
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
    └── smile_uvi_YYYYMMDD_Nsamples_Ffps.mp4
```

## License

This project is licensed under the GNU General Public License v3.0 or
later - see the LICENSE file for details.
