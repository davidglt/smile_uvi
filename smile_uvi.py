#!/usr/bin/env python3
"""Download daily SMILE UVI samples and optionally create an MP4 video."""

import argparse
import csv
import io
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps
from requests.adapters import HTTPAdapter
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


def query_products(session, start, end):
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
    required = {"filepath", "filename", "begin_time", "item_version"}

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


def select_samples(products, start, sample_count, prefix):
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


def download_product(session, product, originals_dir):
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
        print(f"  Cached: {filename}", flush=True)
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
            chunks = response.iter_content(chunk_size=1024 * 1024)

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
        print(f"  Downloaded: {filename}", flush=True)
        return destination

    finally:
        temporary.unlink(missing_ok=True)


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


def render_frame(
    source,
    destination,
    slot_start,
    slot_end,
    product,
    size,
):
    width, height = size
    footer_height = 110
    frame = Image.new("RGB", size, "black")

    if source is not None:
        with Image.open(source) as original:
            rgba = original.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "black")
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
                (height - footer_height - fitted.height) // 2,
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

    temporary = destination.with_name(destination.name + ".part")
    try:
        frame.save(temporary, format="PNG", compress_level=1)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def create_video(frames_dir, destination, sample_count, fps):
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

        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not create a valid output file.")

        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Download equally spaced daily SMILE UVI samples "
            "and optionally create an MP4 video."
        )
    )
    parser.add_argument(
        "day", type=date.fromisoformat,
        help="UTC date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--samples", type=int, default=24,
        help="Number of daily time slots (1 to 86400). Default: 24.",
    )
    parser.add_argument(
        "--fps", type=int, default=1,
        help="Video frames per second. Default: 1.",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data"),
        help="Output root directory. Default: ./data.",
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--prefix",
        default="SMILE_UVI_QL_AURORA_",
        help="Filename prefix filter. Default: SMILE_UVI_QL_AURORA_.",
    )
    parser.add_argument(
        "--no-video", action="store_true",
        help="Download originals and create frames without an MP4.",
    )

    args = parser.parse_args()

    if not 1 <= args.samples <= 86400:
        parser.error("--samples must be between 1 and 86400")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.width < 320 or args.height < 240:
        parser.error("Minimum output size is 320x240")
    if args.width % 2 or args.height % 2:
        parser.error("Width and height must be even")
    if not args.no_video and shutil.which("ffmpeg") is None:
        parser.error(
            "FFmpeg is not available in PATH. "
            "Install FFmpeg or use --no-video."
        )

    return args


def main():
    args = parse_arguments()

    start = datetime.combine(
        args.day,
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    end = start + timedelta(days=1)

    day_dir = args.output / args.day.isoformat()
    originals_dir = day_dir / "originals"
    frames_dir = day_dir / f"frames_{args.samples}samples"
    manifest_path = day_dir / f"manifest_{args.samples}samples.csv"
    video_path = day_dir / (
        f"smile_uvi_{args.day.isoformat()}_"
        f"{args.samples}samples_{args.fps}fps.mp4"
    )

    originals_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    print(f"UTC date: {args.day}")
    print(f"Daily time slots: {args.samples}")
    print(f"Slot duration: {DAY_SECONDS / args.samples:.6f} seconds")
    print(f"Output directory: {day_dir.resolve()}")

    with make_session() as session:
        products = query_products(session, start, end)
        selected = select_samples(
            products, start, args.samples, args.prefix
        )

        print(f"Catalogue rows: {len(products)}")
        print(f"Available slots: {len(selected)}/{args.samples}")
        print(f"Missing slots: {args.samples - len(selected)}")

        if not selected:
            raise RuntimeError(
                "No matching products found. "
                "Check the date, prefix and access permissions."
            )

        with manifest_path.open(
            "w", newline="", encoding="utf-8"
        ) as output:
            writer = csv.writer(output)
            writer.writerow([
                "slot_index",
                "slot_start_utc",
                "slot_end_utc",
                "status",
                "observation_time",
                "filename",
                "local_file",
                "frame_file",
            ])

            for slot in range(args.samples):
                slot_start = start + timedelta(
                    seconds=slot * DAY_SECONDS / args.samples
                )
                slot_end = start + timedelta(
                    seconds=(slot + 1) * DAY_SECONDS / args.samples
                )
                product = selected.get(slot)
                source = None
                frame_path = frames_dir / f"frame_{slot:06d}.png"

                print(
                    f"[{slot + 1}/{args.samples}] "
                    f"{slot_start.isoformat()}",
                    flush=True,
                )

                try:
                    if product is not None:
                        source = download_product(
                            session, product, originals_dir
                        )
                    else:
                        print("  No observation in this slot.", flush=True)

                    render_frame(
                        source,
                        frame_path,
                        slot_start,
                        slot_end,
                        product,
                        (args.width, args.height),
                    )

                except Exception:
                    writer.writerow([
                        slot,
                        slot_start.isoformat(),
                        slot_end.isoformat(),
                        "error",
                        product["begin_time"] if product else "",
                        product["filename"] if product else "",
                        str(source) if source else "",
                        "",
                    ])
                    output.flush()
                    raise

                writer.writerow([
                    slot,
                    slot_start.isoformat(),
                    slot_end.isoformat(),
                    "available" if source else "missing",
                    product["begin_time"] if product else "",
                    product["filename"] if product else "",
                    str(source) if source else "",
                    str(frame_path),
                ])
                output.flush()

    print(f"Manifest: {manifest_path}")
    print(f"Original images: {originals_dir}")
    print(f"Frames: {frames_dir}")

    if args.no_video:
        print("Video generation skipped.")
        return

    create_video(
        frames_dir, video_path, args.samples, args.fps
    )
    print(f"Video created: {video_path}")
    print(f"Video duration: {args.samples / args.fps:.2f} seconds")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("\nInterrupted by user.")
    except Exception as error:
        raise SystemExit(f"\nERROR: {error}")