"""Five-stage float32 conditioning, content diagnostics, and native-pixel previews."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
from typing import Literal
import warnings

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageCms, ImageOps
from scipy import ndimage

Fit = Literal["contain", "cover", "pad_paper", "pad_slate"]
Grade = Literal["matte_natural", "raw"]
FloatImage = NDArray[np.float32]
PAPER = np.array([234, 228, 215], dtype=np.float32)
SLATE = np.array([34, 36, 40], dtype=np.float32)
WIDTH, HEIGHT = 3840, 2160


@dataclass(frozen=True)
class Conditioned:
    jpeg: bytes
    result: dict


def decode(data: bytes, fit: Fit) -> FloatImage:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.width * original.height > 120_000_000:
                    raise ValueError("image_dimensions_exceeded")
                if getattr(original, "n_frames", 1) > 1:
                    raise ValueError("animated_image_unsupported")
                img = ImageOps.exif_transpose(original)
                alpha = (
                    np.asarray(img.convert("RGBA"), dtype=np.float32)[..., 3:4] / 255.0
                    if "A" in img.getbands() or "transparency" in img.info
                    else None
                )
                profile = img.info.get("icc_profile")
                if profile:
                    try:
                        img = ImageCms.profileToProfile(
                            img,
                            ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                            ImageCms.createProfile("sRGB"),
                            outputMode="RGB",
                        )
                    except (ImageCms.PyCMSError, OSError) as exc:
                        raise ValueError("invalid_color_profile") from exc
                    rgb = np.array(img, dtype=np.float32)
                elif img.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
                    gray = np.array(img, dtype=np.float32)
                    gray = np.clip(
                        gray * (255.0 if img.mode == "F" else 1.0 / 257.0), 0, 255
                    )
                    rgb = np.repeat(gray[..., None], 3, axis=2)
                else:
                    rgb = np.array(img.convert("RGB"), dtype=np.float32)
                if alpha is not None and alpha.min() < 1.0:
                    foreground = rgb[(alpha[..., 0] > 32.0 / 255.0)]
                    lum = (
                        foreground
                        @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
                        / 255.0
                    )
                    median = float(np.median(lum)) if lum.size else 0.0
                    backing = (
                        SLATE
                        if fit == "pad_slate" or (fit == "contain" and median >= 0.55)
                        else PAPER
                    )
                    rgb = rgb * alpha + backing * (1.0 - alpha)
                if not np.isfinite(rgb).all():
                    raise ValueError("invalid_image_values")
                return rgb
    except Image.DecompressionBombError as exc:
        raise ValueError("image_dimensions_exceeded") from exc


def scale_content(rgb: FloatImage, fit: Fit) -> tuple[FloatImage, list[int]]:
    h, w = rgb.shape[:2]
    box = None
    if fit == "cover":
        if w * 9 > h * 16:
            crop_w = h * 16.0 / 9.0
            box = ((w - crop_w) / 2, 0, (w + crop_w) / 2, h)
        else:
            crop_h = w * 9.0 / 16.0
            box = (0, (h - crop_h) / 2, w, (h + crop_h) / 2)
        cw, ch = WIDTH, HEIGHT
    elif w * 9 == h * 16:
        cw, ch = WIDTH, HEIGHT
    else:
        factor = min(WIDTH / w, HEIGHT / h)
        cw, ch = max(1, round(w * factor)), max(1, round(h * factor))
    content = np.stack(
        [
            np.asarray(
                Image.fromarray(rgb[..., c]).resize(
                    (cw, ch), Image.Resampling.LANCZOS, box=box
                ),
                dtype=np.float32,
            )
            for c in range(3)
        ],
        axis=2,
    )
    np.clip(content, 0.0, 255.0, out=content)
    x, y = (WIDTH - cw) // 2, (HEIGHT - ch) // 2
    return content, [x, y, x + cw, y + ch]


def luminance(rgb: FloatImage) -> FloatImage:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def saturation(rgb: FloatImage) -> tuple[FloatImage, FloatImage]:
    value = rgb.max(axis=2)
    sat = np.divide(
        value - rgb.min(axis=2), value, out=np.zeros_like(value), where=value != 0
    )
    return sat, value


def tonal_map(rgb: FloatImage) -> FloatImage:
    """Apply the specification's quadratic C1 curves, then compress HSV saturation."""
    floor = np.array([22.0, 22.0, 21.0], dtype=np.float32)
    peak = np.array([243.0, 241.0, 236.0], dtype=np.float32)
    t = np.clip(rgb / 64.0, 0.0, 1.0)
    toe = floor * (1.0 - t) ** 2 + rgb
    u = np.clip((toe - 215.0) / 40.0, 0.0, 1.0)
    result = toe - (255.0 - peak) * u**2
    sat, value = saturation(result)
    compressed = np.where(sat > 0.88, 0.88 + 0.06 * np.tanh((sat - 0.88) / 0.06), sat)
    ratio = np.divide(compressed, sat, out=np.ones_like(sat), where=sat != 0)
    # Scaling distance from V preserves HSV hue without an RGB/HSV round-trip.
    return value[..., None] - (value[..., None] - result) * ratio[..., None]


def block_means(values: FloatImage, side: int) -> FloatImage:
    h, w = values.shape
    ys, xs = np.arange(0, h, side), np.arange(0, w, side)
    sums = np.add.reduceat(np.add.reduceat(values, ys, axis=0), xs, axis=1)
    counts = np.minimum(side, h - ys)[:, None] * np.minimum(side, w - xs)[None, :]
    return (sums / counts).astype(np.float32)


def metrics(rgb: FloatImage) -> dict[str, float]:
    lum = luminance(rgb)
    cells = block_means(lum, 8)
    labels, count = ndimage.label(cells < 22.0, structure=np.ones((3, 3)))
    largest = int(np.bincount(labels.ravel())[1:].max()) if count else 0
    sat, value = saturation(rgb)
    return {
        "max_dark_blob_pct": largest / cells.size * 100.0,
        "neon_gamut_pct": float(np.mean((sat > 0.90) & (value > 0.80 * 255.0)) * 100),
        "glare_pct": float(np.mean(lum > 245.0) * 100),
    }


def flatness(rgb: FloatImage) -> float:
    if min(rgb.shape[:2]) < 3:
        return 0.0
    lap = np.abs(ndimage.laplace(luminance(rgb), mode="nearest"))
    return float(np.mean(block_means(lap, 16) < 0.75) * 100.0)


def _window_scores(values: FloatImage, h: int, w: int) -> NDArray[np.float64]:
    integral = np.pad(values.astype(np.float64), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return integral[h:, w:] - integral[:-h, w:] - integral[h:, :-w] + integral[:-h, :-w]


def retinal_crops(content: NDArray[np.uint8], directory: Path, key: str) -> dict:
    h, w = content.shape[:2]
    if min(w, h) < 64:
        return {
            "detail_crop_path": None,
            "tonal_crop_path": None,
            "crop_skipped_reason": "content_dimension_below_64px",
        }
    ix, iy = (16 if w >= 544 else 0), (16 if h >= 544 else 0)
    cw, ch = min(512, w - 2 * ix), min(512, h - 2 * iy)
    inner = content[iy : h - iy, ix : w - ix]
    lum = luminance(inner.astype(np.float32))
    energy = ndimage.sobel(lum, axis=0) ** 2 + ndimage.sobel(lum, axis=1) ** 2
    detail = _window_scores(energy, ch, cw)
    dy, dx = np.unravel_index(np.argmax(detail), detail.shape)
    del detail, energy
    tonal = _window_scores(lum, ch, cw)
    ty, tx = np.unravel_index(np.argmin(tonal), tonal.shape)
    result = {}
    for name, y, x in (("detail", dy, dx), ("tonal", ty, tx)):
        path = directory / f"{key}_{name}_512.png"
        Image.fromarray(inner[y : y + ch, x : x + cw]).save(path)
        result[f"{name}_crop_path"] = str(path)
    return result


def prune_previews(directory: Path) -> None:
    files = sorted(
        (p for p in directory.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    for path in files[16:]:
        path.unlink(missing_ok=True)


def condition(
    image_path: str | Path,
    previews: Path,
    *,
    fit: Fit = "contain",
    optical_grade: Grade = "matte_natural",
    previous_path: str | None = None,
) -> Conditioned:
    if fit not in (
        "contain",
        "cover",
        "pad_paper",
        "pad_slate",
    ) or optical_grade not in ("matte_natural", "raw"):
        raise ValueError("invalid_optical_parameters")
    data = Path(image_path).read_bytes()
    rgb = decode(data, fit)
    h, w = rgb.shape[:2]
    content, bbox = scale_content(rgb, fit)
    del rgb
    pre = metrics(content)
    if optical_grade == "matte_natural":
        content = tonal_map(content)
    post = metrics(content)
    synthetic = flatness(content)
    ch, cw = content.shape[:2]
    border = np.zeros((ch, cw), dtype=bool)
    border[:8] = border[-8:] = True
    border[:, :8] = border[:, -8:] = True
    edge = content[border]
    edge_lum = (
        np.median(edge @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)) / 255.0
    )
    mat = (
        "none"
        if (cw, ch) == (WIDTH, HEIGHT)
        else (
            "paper"
            if fit == "pad_paper" or (fit == "contain" and edge_lum >= 0.42)
            else "slate"
        )
    )
    canvas = np.empty((HEIGHT, WIDTH, 3), dtype=np.float32)
    canvas[:] = PAPER if mat == "paper" else SLATE
    x0, y0, x1, y1 = bbox
    canvas[y0:y1, x0:x1] = content
    del content
    digest = hashlib.sha256(data).digest()
    rng = np.random.Generator(np.random.PCG64(int.from_bytes(digest[:8], "big")))
    dither = rng.random(canvas.shape, dtype=np.float32) - rng.random(
        canvas.shape, dtype=np.float32
    )
    if optical_grade == "raw":
        dither[y0:y1, x0:x1] = 0
    canvas += dither
    del dither
    final = np.clip(np.rint(canvas), 0, 255).astype(np.uint8)
    del canvas
    output = io.BytesIO()
    Image.fromarray(final).save(output, format="JPEG", quality=96, subsampling=0)
    jpeg = output.getvalue()
    previews.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Include transform parameters in cache identity; seed remains input-only.
    key = hashlib.sha256(data + f"{fit}:{optical_grade}".encode()).hexdigest()[:12]
    preview = previews / f"{key}_full.jpg"
    preview.write_bytes(jpeg)
    crops = retinal_crops(final[y0:y1, x0:x1], previews, key)
    transition = None
    if previous_path and Path(previous_path).is_file():
        with Image.open(previous_path) as previous:
            if previous.size == (WIDTH, HEIGHT):
                with Image.open(io.BytesIO(jpeg)) as encoded:
                    current_lum = luminance(np.asarray(encoded, dtype=np.float32))
                transition = float(
                    np.mean(
                        np.abs(
                            current_lum
                            - luminance(
                                np.asarray(previous.convert("RGB"), dtype=np.float32)
                            )
                        )
                    )
                    / 255.0
                    * 100
                )
    warning_list = []
    if post["max_dark_blob_pct"] > 3:
        warning_list.append("large_dark_region")
    if optical_grade == "matte_natural" and synthetic > 25:
        warning_list.append("synthetic_flatness")
    if post["glare_pct"] > 0:
        warning_list.append("peak_white_glare")
    if post["neon_gamut_pct"] > 0:
        warning_list.append("neon_gamut")
    diagnostics = {
        "input_resolution": [w, h],
        "input_aspect_ratio": w / h,
        "fit_applied": fit,
        "optical_grade_applied": optical_grade,
        "pre_lut": pre,
        "post_lut": post,
        "synthetic_flatness_pct": synthetic,
        "transition_delta_pct": transition,
        "warnings": warning_list,
        "retinal_crops": crops,
    }
    prune_previews(previews)
    return Conditioned(
        jpeg,
        {
            "preview_full_path": str(preview),
            "content_bbox": bbox,
            "mat_applied": mat,
            "optical_diagnostics": diagnostics,
        },
    )
