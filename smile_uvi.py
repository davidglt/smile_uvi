#!/usr/bin/env python3
"""SMILE UVI GUI: download daily samples and play the MP4 video."""

import csv
import io
import subprocess
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import cv2
import requests
import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk
from requests.adapters import HTTPAdapter
from tkinter import ttk, messagebox
from urllib3.util.retry import Retry


BASE_URL = "https://emds.esac.esa.int/service"
TABLE = "smile.v_ql_data_file_latest"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DAY_SECONDS = 86400
MAX_RECORDS = 100000


def adql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def parse_timestamp(value):
    timestamp = datetime.fromisoformat(
        value.strip().replace("Z", "+00:00")
    )
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def make_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "smile-uvi-daily-sampler/1.0"
    return session


def valid_png(path):
    if not path.is_file():
        return False

    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                return False
            image.verify()
        return True
    except (OSError, SyntaxError, ValueError):
        return False


def get_font(size):
    for name in ("DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw_fitted_text(draw, position, text, font, max_width):
    if draw.textbbox((0, 0), text, font=font)[2] > max_width:
        while text and (
            draw.textbbox((0, 0), text + "...", font=font)[2]
            > max_width
        ):
            text = text[:-1]
        text += "..."

    draw.text(position, text, fill="white", font=font)


class SmileUviData:
    """Data layer: query, download, cache, render, and video creation."""

    def __init__(self, output_root: Path):
        self.output_root = output_root

    def day_dir(self, day: date) -> Path:
        """Return the directory for a given day (YYYYMMDD format)."""
        return self.output_root / day.strftime("%Y%m%d")

    def originals_dir(self, day: date) -> Path:
        """Return the originals sub-directory for a day."""
        return self.day_dir(day) / "originals"

    def frames_dir(self, day: date, samples: int) -> Path:
        """Return the frames sub-directory for a day and sample count."""
        return self.day_dir(day) / f"frames_{samples}samples"

    def manifest_path(self, day: date, samples: int) -> Path:
        """Return the manifest CSV path for a day and sample count."""
        return self.day_dir(day) / f"manifest_{samples}samples.csv"

    def video_path(self, day: date, samples: int, fps: int) -> Path:
        """Return the MP4 video path for a day, samples, and fps."""
        return self.day_dir(day) / (
            f"smile_uvi_{day.strftime('%Y%m%d')}_"
            f"{samples}samples_{fps}fps.mp4"
        )

    def query_products(self, session, start, end) -> list:
        """Query the TAP service for UVI products in a time range."""
        start_text = start.strftime("%Y-%m-%dT%H:%M:%S")
        end_text = end.strftime("%Y-%m-%dT%H:%M:%S")

        query = f"""
            SELECT filepath, filename, begin_time, item_version
            FROM {TABLE}
            WHERE instrument_name = 'UVI'
              AND filename LIKE '%.png'
              AND begin_time >= {adql_string(start_text)}
              AND begin_time < {adql_string(end_text)}
            ORDER BY begin_time ASC, filename ASC
        """

        response = session.get(
            f"{BASE_URL}/tap/sync",
            params={
                "REQUEST": "doQuery",
                "LANG": "ADQL",
                "FORMAT": "csv",
                "MAXREC": MAX_RECORDS,
                "QUERY": query,
            },
            timeout=(30, 180),
        )
        response.raise_for_status()

        reader = csv.DictReader(
            io.StringIO(response.text.lstrip("\ufeff"))
        )
        required = {
            "filepath", "filename", "begin_time", "item_version"
        }

        if not required.issubset(reader.fieldnames or []):
            raise RuntimeError(
                "Unexpected TAP response:\n" + response.text[:1500]
            )

        products = list(reader)
        if len(products) >= MAX_RECORDS:
            raise RuntimeError(
                "Possible TAP result truncation. Refine the query."
            )

        return products

    def select_samples(
        self, products, start, sample_count, prefix
    ) -> dict:
        """Select one product per time slot from the product list."""
        selected = {}

        for product in products:
            if prefix and not product["filename"].startswith(prefix):
                continue

            timestamp = parse_timestamp(product["begin_time"])
            elapsed = timestamp - start
            elapsed_us = (
                elapsed.days * DAY_SECONDS * 1_000_000
                + elapsed.seconds * 1_000_000
                + elapsed.microseconds
            )

            if not 0 <= elapsed_us < DAY_SECONDS * 1_000_000:
                continue

            slot = (
                elapsed_us * sample_count
                // (DAY_SECONDS * 1_000_000)
            )

            previous = selected.get(slot)
            if previous is None:
                selected[slot] = product
                continue

            previous_time = parse_timestamp(previous["begin_time"])
            if timestamp < previous_time:
                selected[slot] = product
            elif timestamp == previous_time:
                if (product["item_version"] or "") > (
                    previous["item_version"] or ""
                ):
                    selected[slot] = product

        return selected

    def download_product(
        self, session, product, originals_dir
    ) -> Path:
        """Download a single product, using cache if valid."""
        filename = product["filename"]
        if (
            not filename
            or "/" in filename
            or "\\" in filename
            or ":" in filename
            or filename in {".", ".."}
        ):
            raise RuntimeError(f"Unexpected filename: {filename!r}")

        destination = originals_dir / filename
        if valid_png(destination):
            return destination

        query = f"""
            SELECT filepath, filename
            FROM {TABLE}
            WHERE filepath = {adql_string(product["filepath"])}
              AND filename = {adql_string(filename)}
        """

        temporary = destination.with_name(destination.name + ".part")

        try:
            with session.get(
                f"{BASE_URL}/data",
                params={
                    "retrieval_type": "PRODUCT",
                    "QUERY": query,
                },
                stream=True,
                timeout=(30, 300),
            ) as response:
                response.raise_for_status()
                chunks = response.iter_content(
                    chunk_size=1024 * 1024
                )

                first_bytes = b""
                while len(first_bytes) < len(PNG_SIGNATURE):
                    chunk = next(chunks, b"")
                    if not chunk:
                        break
                    first_bytes += chunk

                if not first_bytes.startswith(PNG_SIGNATURE):
                    raise RuntimeError(
                        f"Expected a PNG for {filename}.\n"
                        f"Content-Type: "
                        f"{response.headers.get('Content-Type')}\n"
                        f"First bytes: {first_bytes[:120]!r}"
                    )

                with temporary.open("wb") as output:
                    output.write(first_bytes)
                    for chunk in chunks:
                        if chunk:
                            output.write(chunk)

            if not valid_png(temporary):
                raise RuntimeError(f"Invalid PNG: {filename}")

            temporary.replace(destination)
            return destination

        finally:
            temporary.unlink(missing_ok=True)

    def render_frame(
        self,
        source,
        destination,
        slot_start,
        slot_end,
        product,
        size,
    ) -> None:
        """Render a single frame with image and metadata overlay."""
        width, height = size
        footer_height = 110
        frame = Image.new("RGB", size, "black")

        if source is not None:
            with Image.open(source) as original:
                rgba = original.convert("RGBA")
                background = Image.new(
                    "RGBA", rgba.size, "black"
                )
                background.alpha_composite(rgba)

                fitted = ImageOps.contain(
                    background.convert("RGB"),
                    (width, height - footer_height),
                    Image.Resampling.LANCZOS,
                )

            frame.paste(
                fitted,
                (
                    (width - fitted.width) // 2,
                    (height - footer_height
                     - fitted.height) // 2,
                ),
            )

        draw = ImageDraw.Draw(frame)

        if source is None:
            draw.text(
                (width // 2, (height - footer_height) // 2),
                "NO DATA",
                fill="white",
                font=get_font(44),
                anchor="mm",
            )

        label = (
            f'{slot_start.strftime("%Y-%m-%d")} UTC | '
            f'{slot_start.strftime("%H:%M:%S")} - '
            f'{slot_end.strftime("%H:%M:%S")}'
        )
        draw_fitted_text(
            draw, (15, height - 100), label,
            get_font(24), width - 30,
        )

        observation = (
            f'Observation: {product["begin_time"]}'
            if product
            else "No observation available in this time slot"
        )
        draw_fitted_text(
            draw, (15, height - 65), observation,
            get_font(20), width - 30,
        )

        filename = product["filename"] if product else ""
        draw_fitted_text(
            draw, (15, height - 33), filename,
            get_font(18), width - 30,
        )

        temporary = destination.with_name(
            destination.name + ".part"
        )
        try:
            frame.save(
                temporary, format="PNG", compress_level=1
            )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def create_video(
        self, frames_dir, destination, sample_count, fps
    ) -> None:
        """Create an MP4 video from rendered frames using FFmpeg."""
        temporary = destination.with_name(
            destination.stem + ".part.mp4"
        )

        command = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-framerate", str(fps),
            "-start_number", "0",
            "-i", str(frames_dir / "frame_%06d.png"),
            "-frames:v", str(sample_count),
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(temporary),
        ]

        try:
            subprocess.run(command, check=True)

            if (
                not temporary.is_file()
                or temporary.stat().st_size == 0
            ):
                raise RuntimeError(
                    "FFmpeg did not create a valid output file."
                )

            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def get_cached_files(self, day: date, samples: int) -> list:
        """Return list of valid PNGs already in originals/ for this day."""
        odir = self.originals_dir(day)
        if not odir.is_dir():
            return []
        return [p for p in sorted(odir.glob("*.png")) if valid_png(p)]

    def get_telemetry(self, products: list) -> list:
        """Extract telemetry rows from the product list."""
        return [
            {
                "time": p["begin_time"],
                "file": p["filename"],
                "version": p.get("item_version", ""),
            }
            for p in products
        ]


class VideoPlayerWidget(ttk.Frame):
    """Plays an MP4 file using OpenCV on a tkinter Canvas."""

    def __init__(self, parent, width=640, height=480):
        super().__init__(parent)
        self.canvas = tk.Canvas(
            self, width=width, height=height,
            bg="black", highlightthickness=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._cap = None
        self._playing = False
        self._after_id = None
        self._width = width
        self._height = height
        self._photo = None
        self._video_path = None

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, pady=(4, 0))
        self.btn_play = ttk.Button(
            btn_frame, text="Play", command=self.toggle_play,
        )
        self.btn_play.pack(side=tk.LEFT, padx=4)
        self.lbl_status = ttk.Label(
            btn_frame, text="No video loaded"
        )
        self.lbl_status.pack(side=tk.LEFT, padx=8)

    def load_video(self, path: Path):
        """Load an MP4 file. Must be called from the main thread."""
        self.stop()
        if not path.is_file():
            self.lbl_status.config(
                text=f"Not found: {path.name}"
            )
            return
        self._cap = cv2.VideoCapture(str(path))
        self._video_path = path
        if not self._cap.isOpened():
            self.lbl_status.config(text="Failed to open video")
            self._cap = None
            return
        fps = self._cap.get(cv2.CAP_PROP_FPS)
        frames = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.lbl_status.config(
            text=f"{path.name}  ({frames}f @ {fps:.1f}fps)"
        )

    def toggle_play(self):
        """Toggle play/pause of the loaded video."""
        if self._video_path is None:
            return
        if self._playing:
            self.stop()
        else:
            if self._cap is None:
                self._cap = cv2.VideoCapture(
                    str(self._video_path)
                )
                if not self._cap.isOpened():
                    self.lbl_status.config(
                        text="Failed to reopen video"
                    )
                    self._cap = None
                    return
            self._playing = True
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self._tick()

    def stop(self):
        """Stop playback and release the video capture."""
        self._playing = False
        if self._after_id:
            self.after_cancel(self._after_id)
            self._after_id = None
        if self._cap:
            self._cap.release()
        self.canvas.delete("all")
        self._photo = None

    def _tick(self):
        """Advance one frame and schedule the next tick."""
        if not self._playing or self._cap is None:
            return
        ret, frame = self._cap.read()
        if not ret:
            self.stop()
            return
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frame = cv2.resize(
            frame, (self._width, self._height),
            interpolation=cv2.INTER_AREA,
        )
        img = Image.fromarray(frame)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(
            self._width // 2, self._height // 2,
            image=self._photo, anchor=tk.CENTER,
        )
        delay = int(
            1000 / max(self._cap.get(cv2.CAP_PROP_FPS), 1)
        )
        self._after_id = self.after(delay, self._tick)


class ControlPanel(ttk.Frame):
    """Right-side form: date, params, telemetry, action buttons."""

    def __init__(self, parent, app: "SmileUviApp"):
        super().__init__(parent, padding=10)
        self.app = app
        self._build()

    def _build(self):
        """Build all form widgets."""
        # --- Top section: generation options ---
        top = ttk.Frame(self)
        top.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(top, text="Date (YYYY-MM-DD):").pack(
            anchor=tk.W
        )
        self.var_date = tk.StringVar(
            value=date.today().isoformat()
        )
        ttk.Entry(
            top, textvariable=self.var_date, width=14
        ).pack(anchor=tk.W, pady=(2, 8))

        ttk.Label(top, text="Number of frames:").pack(
            anchor=tk.W
        )
        self.var_samples = tk.IntVar(value=24)
        ttk.Spinbox(
            top, from_=1, to=86400,
            textvariable=self.var_samples, width=10,
        ).pack(anchor=tk.W, pady=(2, 8))

        ttk.Label(top, text="Video FPS:").pack(anchor=tk.W)
        self.var_fps = tk.IntVar(value=1)
        ttk.Spinbox(
            top, from_=1, to=60,
            textvariable=self.var_fps, width=10,
        ).pack(anchor=tk.W, pady=(2, 8))

        ttk.Label(top, text="Filename prefix:").pack(
            anchor=tk.W
        )
        self.var_prefix = tk.StringVar(
            value="SMILE_UVI_QL_AURORA_"
        )
        ttk.Entry(
            top, textvariable=self.var_prefix, width=28,
        ).pack(anchor=tk.W, pady=(2, 8))

        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill=tk.X, pady=(8, 4))
        self.btn_download = ttk.Button(
            btn_frame, text="Download",
            command=self._on_download,
        )
        self.btn_download.pack(side=tk.LEFT, padx=4)
        self.btn_video = ttk.Button(
            btn_frame, text="Create Video",
            command=self._on_create_video,
        )
        self.btn_video.pack(side=tk.LEFT, padx=4)

        self.var_status = tk.StringVar(value="Ready")
        ttk.Label(
            top, textvariable=self.var_status,
            wraplength=280, justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(8, 4))

        # --- Bottom section: video catalog ---
        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        ttk.Label(bottom, text="Video Catalog:").pack(
            anchor=tk.W
        )

        cols = ("day", "frames", "fps")
        self.tree = ttk.Treeview(
            bottom, columns=cols, show="headings",
            height=10,
        )
        self.tree.heading("day", text="Day")
        self.tree.heading("frames", text="Frames")
        self.tree.heading("fps", text="FPS")
        self.tree.column("day", width=100, anchor=tk.CENTER)
        self.tree.column("frames", width=60, anchor=tk.CENTER)
        self.tree.column("fps", width=40, anchor=tk.CENTER)

        tree_scroll = ttk.Scrollbar(
            bottom, orient=tk.VERTICAL,
            command=self.tree.yview,
        )
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind(
            "<<TreeviewSelect>>", self._on_catalog_select
        )

        ttk.Button(
            bottom, text="Refresh",
            command=self.refresh_catalog,
        ).pack(anchor=tk.E, pady=(4, 0))

    def _on_download(self):
        """Trigger the download workflow."""
        self.app.start_download()

    def _on_create_video(self):
        """Trigger the video creation workflow."""
        self.app.start_video()

    def set_status(self, text: str):
        """Update the status label text."""
        self.var_status.set(text)

    def set_telemetry(self, rows: list):
        """No-op kept for backward compatibility."""
        pass

    def refresh_catalog(self):
        """Scan the data directory for generated MP4 videos."""
        self.tree.delete(*self.tree.get_children())
        data_root = self.app.data.output_root
        if not data_root.is_dir():
            return
        for mp4 in sorted(data_root.rglob("*.mp4")):
            parts = mp4.stem.split("_")
            if len(parts) < 5:
                continue
            day_str = parts[2]
            frames_str = parts[3].replace("samples", "")
            fps_str = parts[4].replace("fps", "")
            self.tree.insert(
                "", tk.END,
                values=(day_str, frames_str, fps_str),
                tags=(str(mp4),),
            )

    def _on_catalog_select(self, event):
        """Load the selected video into the player."""
        sel = self.tree.selection()
        if not sel:
            return
        item = self.tree.item(sel[0])
        video_path = Path(item["tags"][0])
        self.app.player.load_video(video_path)
        self.app.player.toggle_play()


class SmileUviApp:
    """Main application window wiring all components together."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("SMILE UVI - Daily Sampler")
        self.root.geometry("1100x700")
        self.root.minsize(900, 550)

        self.data = SmileUviData(output_root=Path("data"))

        main = ttk.PanedWindow(
            self.root, orient=tk.HORIZONTAL,
        )
        main.pack(fill=tk.BOTH, expand=True)

        self.player = VideoPlayerWidget(
            main, width=640, height=480
        )
        main.add(self.player, weight=3)

        self.panel = ControlPanel(main, self)
        main.add(self.panel, weight=2)

        self._busy = False
        self.panel.refresh_catalog()

    def run(self):
        """Start the tkinter main loop."""
        self.root.mainloop()

    def start_download(self):
        """Begin the download workflow in a background thread."""
        if self._busy:
            return
        self._busy = True
        self.panel.set_status("Downloading...")
        threading.Thread(
            target=self._worker_download, daemon=True,
        ).start()

    def start_video(self):
        """Begin the video creation workflow in a background thread."""
        if self._busy:
            return
        self._busy = True
        self.panel.set_status("Creating video...")
        threading.Thread(
            target=self._worker_video, daemon=True,
        ).start()

    def _worker_download(self):
        """Background worker: query, download, and render frames."""
        try:
            day = date.fromisoformat(
                self.panel.var_date.get()
            )
            samples = self.panel.var_samples.get()
            prefix = self.panel.var_prefix.get()

            start = datetime.combine(
                day, datetime.min.time(),
                tzinfo=timezone.utc,
            )
            end = start + timedelta(days=1)

            odir = self.data.originals_dir(day)
            odir.mkdir(parents=True, exist_ok=True)

            with make_session() as session:
                products = self.data.query_products(
                    session, start, end,
                )
                selected = self.data.select_samples(
                    products, start, samples, prefix,
                )

                cached = self.data.get_cached_files(
                    day, samples
                )
                self.root.after(
                    0,
                    lambda: self.panel.set_status(
                        f"Catalogue: {len(products)} rows, "
                        f"slots: {len(selected)}/{samples}, "
                        f"cached: {len(cached)}",
                    ),
                )

                for slot, product in sorted(
                    selected.items()
                ):
                    dest = odir / product["filename"]
                    if valid_png(dest):
                        continue
                    self.data.download_product(
                        session, product, odir,
                    )

                fdir = self.data.frames_dir(day, samples)
                fdir.mkdir(parents=True, exist_ok=True)
                size = (1280, 1080)
                for slot in range(samples):
                    slot_start = start + timedelta(
                        seconds=(
                            slot * DAY_SECONDS / samples
                        ),
                    )
                    slot_end = start + timedelta(
                        seconds=(
                            (slot + 1) * DAY_SECONDS / samples
                        ),
                    )
                    product = selected.get(slot)
                    source = None
                    if product:
                        src_path = (
                            odir / product["filename"]
                        )
                        if valid_png(src_path):
                            source = src_path
                    frame_path = (
                        fdir / f"frame_{slot:06d}.png"
                    )
                    self.data.render_frame(
                        source, frame_path,
                        slot_start, slot_end,
                        product, size,
                    )

            self.root.after(
                0,
                lambda: self.panel.set_status(
                    "Download complete."
                ),
            )
        except Exception as e:
            error_msg = f"ERROR: {e}"
            self.root.after(
                0,
                lambda: self.panel.set_status(error_msg),
            )
        finally:
            self._busy = False

    def _worker_video(self):
        """Background worker: create MP4 and load into player."""
        try:
            day = date.fromisoformat(
                self.panel.var_date.get()
            )
            samples = self.panel.var_samples.get()
            fps = self.panel.var_fps.get()

            fdir = self.data.frames_dir(day, samples)
            vpath = self.data.video_path(
                day, samples, fps
            )

            self.data.create_video(
                fdir, vpath, samples, fps
            )

            self.root.after(
                0,
                lambda: self.player.load_video(vpath),
            )
            self.root.after(
                0,
                lambda: self.panel.set_status(
                    f"Video ready: {vpath.name}"
                ),
            )
            self.root.after(0, self.panel.refresh_catalog)
        except Exception as e:
            error_msg = f"ERROR: {e}"
            self.root.after(
                0,
                lambda: self.panel.set_status(error_msg),
            )
        finally:
            self._busy = False


if __name__ == "__main__":
    app = SmileUviApp()
    app.run()
