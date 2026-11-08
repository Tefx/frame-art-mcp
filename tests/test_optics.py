import io

import numpy as np
from PIL import Image, ImageCms
import pytest

from frame_art_mcp import optics


def image_bytes(image, format="PNG", **kwargs):
    stream = io.BytesIO()
    image.save(stream, format=format, **kwargs)
    return stream.getvalue()


@pytest.mark.parametrize(
    "fit,foreground,backing",
    [
        ("contain", (0, 0, 0), (234, 228, 215)),
        ("contain", (255, 255, 255), (34, 36, 40)),
        ("pad_slate", (0, 0, 0), (34, 36, 40)),
        ("pad_paper", (255, 255, 255), (234, 228, 215)),
    ],
)
def test_alpha_backing_and_foreground_luminance(fit, foreground, backing):
    image = Image.new("RGBA", (4, 4), (*foreground, 0))
    image.putpixel((1, 1), (*foreground, 255))
    rgb = optics.decode(image_bytes(image), fit)
    np.testing.assert_array_equal(rgb[0, 0], backing)
    np.testing.assert_array_equal(rgb[1, 1], foreground)
    assert rgb.dtype == np.float32


def test_palette_transparency_and_la_are_composited():
    palette = Image.new("P", (2, 2))
    palette.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    palette.putpixel((1, 1), 1)
    rgb = optics.decode(image_bytes(palette, transparency=0), "contain")
    np.testing.assert_array_equal(rgb[0, 0], [34, 36, 40])
    la = Image.new("LA", (2, 2), (0, 0))
    np.testing.assert_array_equal(
        optics.decode(image_bytes(la), "contain")[0, 0], [234, 228, 215]
    )


def test_exif_orientation_icc_cmyk_and_16bit_conversion():
    exif = Image.Exif()
    exif[274] = 6
    oriented = optics.decode(
        image_bytes(Image.new("RGB", (4, 2), "red"), exif=exif), "contain"
    )
    assert oriented.shape == (4, 2, 3)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    rgb = optics.decode(
        image_bytes(Image.new("RGB", (2, 2), (120, 90, 40)), icc_profile=profile),
        "contain",
    )
    np.testing.assert_allclose(rgb[0, 0], [120, 90, 40], atol=1)
    cmyk = optics.decode(
        image_bytes(Image.new("CMYK", (2, 2), (0, 255, 255, 0)), "TIFF"), "contain"
    )
    np.testing.assert_array_equal(cmyk[0, 0], [255, 0, 0])
    gray = Image.fromarray(np.array([[0, 32768, 65535]], dtype=np.uint16))
    converted = optics.decode(image_bytes(gray, "TIFF"), "contain")
    np.testing.assert_allclose(converted[0, :, 0], [0, 127.5019, 255], atol=0.01)


def test_multiframe_rejected_and_dimensions_checked_before_decode(monkeypatch):
    frames = [Image.new("RGB", (2, 2), color) for color in ("red", "blue")]
    stream = io.BytesIO()
    frames[0].save(stream, format="GIF", save_all=True, append_images=frames[1:])
    with pytest.raises(ValueError, match="animated_image_unsupported"):
        optics.decode(stream.getvalue(), "contain")

    class Huge:
        width, height = 120_000_001, 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(Image, "open", lambda *args: Huge())
    with pytest.raises(ValueError, match="image_dimensions_exceeded"):
        optics.decode(b"header", "contain")


@pytest.mark.parametrize(
    "shape,bbox",
    [
        ((100, 100), [840, 0, 3000, 2160]),
        ((160, 90), [0, 0, 3840, 2160]),
        ((4000, 1), [0, 1079, 3840, 1080]),
        ((1, 4000), [1919, 0, 1920, 2160]),
    ],
)
def test_contain_geometry_exact(shape, bbox):
    w, h = shape
    rgb = np.full((h, w, 3), 80, dtype=np.float32)
    scaled, result = optics.scale_content(rgb, "contain")
    assert result == bbox
    assert scaled.shape == (bbox[3] - bbox[1], bbox[2] - bbox[0], 3)
    np.testing.assert_allclose(scaled[0, 0], [80, 80, 80])


def test_cover_center_crop():
    image = np.zeros((100, 200, 3), dtype=np.float32)
    image[:, 25:175] = 100
    content, bbox = optics.scale_content(image, "cover")
    assert bbox == [0, 0, 3840, 2160]
    np.testing.assert_allclose(content[1000, 1920], [100, 100, 100], atol=0.01)
    assert content[1000, 0, 0] == 0


def test_matte_quadratic_curve_values_and_c1_joins():
    inputs = np.repeat(
        np.array([0, 32, 64, 128, 215, 235, 255], dtype=np.float32)[None, :, None],
        3,
        axis=2,
    )
    output = optics.tonal_map(inputs)
    expected = [
        [22, 22, 21],
        [37.5, 37.5, 37.25],
        [64, 64, 64],
        [128, 128, 128],
        [215, 215, 215],
        [232, 231.5, 230.25],
        [243, 241, 236],
    ]
    np.testing.assert_allclose(output[0], expected, atol=0.0001)
    assert output.dtype == np.float32
    eps = 0.02
    for join in (64.0, 215.0):
        sample = np.repeat(
            np.array([join - eps, join, join + eps], dtype=np.float32)[None, :, None],
            3,
            axis=2,
        )
        mapped = optics.tonal_map(sample)[0]
        np.testing.assert_allclose((mapped[1] - mapped[0]) / eps, [1, 1, 1], atol=0.002)
        np.testing.assert_allclose((mapped[2] - mapped[1]) / eps, [1, 1, 1], atol=0.002)
    ramp = np.repeat(
        np.linspace(0, 255, 4096, dtype=np.float32)[None, :, None], 3, axis=2
    )
    assert np.all(np.diff(optics.tonal_map(ramp)[0], axis=0) >= 0)


def test_matte_saturation_compression_preserves_hue_and_value():
    import colorsys

    source = np.array([[[200, 0, 0], [100, 120, 150], [0, 0, 0]]], dtype=np.float32)
    mapped = optics.tonal_map(source)
    # The independently derived pre-compression red pixel is (200, 22, 21).
    h, s, v = colorsys.rgb_to_hsv(200.0, 22.0, 21.0)
    expected_s = 0.88 + 0.06 * np.tanh((s - 0.88) / 0.06)
    np.testing.assert_allclose(
        mapped[0, 0], colorsys.hsv_to_rgb(h, expected_s, v), atol=0.0001
    )
    np.testing.assert_array_equal(mapped[0, 1], [100, 120, 150])
    assert np.isfinite(mapped).all()


def test_matte_full_pipeline_default_and_pre_dither_diagnostics(tmp_path):
    source = tmp_path / "paper.png"
    Image.new("RGB", (160, 90), "white").save(source)
    result = optics.condition(source, tmp_path / "previews")
    diagnostics = result.result["optical_diagnostics"]
    assert result.result["mat_applied"] == "none"
    assert diagnostics["pre_lut"]["glare_pct"] == 100
    assert diagnostics["post_lut"]["glare_pct"] == 0
    assert diagnostics["synthetic_flatness_pct"] == 100
    assert diagnostics["optical_grade_applied"] == "matte_natural"
    with Image.open(diagnostics["retinal_crops"]["tonal_crop_path"]) as crop:
        pixels = np.asarray(crop)
        assert crop.size == (512, 512)
        np.testing.assert_allclose(pixels.mean(axis=(0, 1)), [243, 241, 236], atol=0.02)
        assert pixels.min() >= 235 and pixels.max() <= 244
    again = optics.condition(source, tmp_path / "previews")
    assert again.jpeg == result.jpeg


def test_dark_components_use_partial_cells_and_eight_connectivity():
    rgb = np.full((17, 17, 3), 100, dtype=np.float32)
    rgb[:8, :8] = 0
    rgb[8:16, 8:16] = 0
    rgb[16:, 16:] = 0
    result = optics.metrics(rgb)
    assert result["max_dark_blob_pct"] == pytest.approx(100 / 3)
    rgb[8:16, 8:16] = 100
    assert optics.metrics(rgb)["max_dark_blob_pct"] == pytest.approx(100 / 9)


def test_flatness_is_pre_dither_and_detects_linear_gradient():
    gradient = np.broadcast_to(
        np.arange(128, dtype=np.float32)[None, :, None], (128, 128, 3)
    ).copy()
    assert optics.flatness(gradient) == 100
    checker = (np.indices((128, 128)).sum(0) % 2 * 255).astype(np.float32)
    assert optics.flatness(np.repeat(checker[..., None], 3, axis=2)) == 0
    assert optics.flatness(np.zeros((1, 40, 3), dtype=np.float32)) == 0


def test_neon_glare_are_content_scoped():
    content = np.array(
        [[[255, 255, 255], [255, 0, 0]], [[10, 10, 10], [120, 120, 120]]],
        dtype=np.float32,
    )
    result = optics.metrics(content)
    assert result["glare_pct"] == 25
    assert result["neon_gamut_pct"] == 25


def test_retinal_crops_are_native_inset_and_discriminating(tmp_path):
    content = np.full((600, 1400, 3), 100, dtype=np.uint8)
    content[:, :560] = 30
    checker = ((np.indices((568, 568)) // 4).sum(0) % 2 * 100 + 100).astype(np.uint8)
    content[16:584, 816:1384] = checker[..., None]
    result = optics.retinal_crops(content, tmp_path, "test")
    with (
        Image.open(result["tonal_crop_path"]) as tonal,
        Image.open(result["detail_crop_path"]) as detail,
    ):
        assert tonal.size == detail.size == (512, 512)
        assert np.asarray(tonal).max() == 30
        assert np.asarray(detail).min() == 100 and np.asarray(detail).max() == 200
    small = optics.retinal_crops(content[:100, :90], tmp_path, "small")
    with Image.open(small["detail_crop_path"]) as detail:
        assert detail.size == (90, 100)
    skipped = optics.retinal_crops(content[:63], tmp_path, "skip")
    assert skipped == {
        "detail_crop_path": None,
        "tonal_crop_path": None,
        "crop_skipped_reason": "content_dimension_below_64px",
    }


def test_raw_pipeline_preserves_content_and_mats_do_not_skew_metrics(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (4000, 1), (0, 0, 0)).save(source)
    result = optics.condition(
        source, tmp_path / "previews", optical_grade="raw", fit="pad_paper"
    )
    assert result.result["mat_applied"] == "paper"
    assert result.result["content_bbox"] == [0, 1079, 3840, 1080]
    diagnostics = result.result["optical_diagnostics"]
    assert diagnostics["pre_lut"]["max_dark_blob_pct"] == 100
    assert diagnostics["post_lut"]["max_dark_blob_pct"] == 100
    assert diagnostics["synthetic_flatness_pct"] == 0
    with Image.open(io.BytesIO(result.jpeg)) as final:
        assert final.size == (3840, 2160)
        assert final.layer[0][1:3] == (1, 1)
        assert max(final.getpixel((1000, 1079))) < 10
    again = optics.condition(
        source, tmp_path / "previews", optical_grade="raw", fit="pad_paper"
    )
    assert result.jpeg == again.jpeg


def test_transition_delta_compares_encoded_frames(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (4000, 1), (123, 85, 45)).save(source)
    original = optics.condition(source, tmp_path / "previews", optical_grade="raw")
    previous = tmp_path / "previous.jpg"
    previous.write_bytes(original.jpeg)
    same = optics.condition(
        source, tmp_path / "previews", optical_grade="raw", previous_path=str(previous)
    )
    assert same.result["optical_diagnostics"]["transition_delta_pct"] == 0.0
    Image.new("RGB", (4000, 1), (230, 220, 210)).save(source)
    changed = optics.condition(
        source, tmp_path / "previews", optical_grade="raw", previous_path=str(previous)
    )
    assert changed.result["optical_diagnostics"]["transition_delta_pct"] > 50.0


def test_nonfinite_float_input_rejected():
    source = Image.fromarray(np.array([[0, np.nan]], dtype=np.float32))
    with pytest.raises(ValueError, match="invalid_image_values"):
        optics.decode(image_bytes(source, "TIFF"), "contain")


def test_preview_cache_is_bounded_by_mtime(tmp_path):
    import os

    for i in range(24):
        path = tmp_path / f"{i}.png"
        path.write_bytes(b"cache")
        os.utime(path, ns=(i + 1, i + 1))
    optics.prune_previews(tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {f"{i}.png" for i in range(8, 24)}
