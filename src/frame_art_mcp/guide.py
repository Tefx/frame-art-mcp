"""Self-contained optical laws and executable material recipes."""

OPTICS_GUIDE = """# Matte Display Optical Laws

Use `matte_natural` for ambient art and `raw` for color-exact screenshots,
charts, and diagrams. Start with `dry_run=True`; inspect the returned native
retinal crops before projection. `contain` preserves any aspect ratio;
`cover` deliberately crops. Never draw a fake outer frame, bevel, deep shadowbox,
or large black void. LCD black can glow gray. Favor shallow tactile materials
at 0.5–3 mm scale, grazing light, soft penumbrae, and 1:1 museum macro details.
Keep text below 4% of the canvas as a physical label, seal, or plate legend.
Use temporary TTLs for general display; projection requires an already-awake
TV in Art Mode. TTLs and sequences run only while the MCP process is alive;
expired intent converges on the next authorized write after restart.

Each recipe is 15 lines, independent, and produces a local image. The relief
recipe expects `source.jpg`; the IIIF example fetches a native-pixel region,
not an upscaled whole-painting thumbnail. Replace the public identifier as needed.

## Frequency-Separated Relief Transfer
```python
from pathlib import Path
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter
source = Image.open("source.jpg").convert("RGB")
rgb = np.asarray(source, dtype=np.float32) / 255.0
lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
base = gaussian_filter(lum, 18.0)
relief = np.clip(lum - base, -0.18, 0.18)
pigment = np.array([0.72, 0.61, 0.45], dtype=np.float32)
wash = np.clip(0.94 + (base - base.mean()) * 0.22, 0.7, 1.1)
paint = pigment * wash[..., None] + relief[..., None] * 0.75
paint = np.clip(paint, 0.10, 0.94)
output = Image.fromarray(np.rint(paint * 255).astype(np.uint8))
output.save(Path("relief.png"))
```

## Optical Penumbra Shadow Projection
```python
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, shift
h, w = 2160, 3840
y, x = np.mgrid[:h, :w].astype(np.float32)
mask = (((x - w / 2) / 650)**2 + ((y - h / 2) / 400)**2 < 1)
height = gaussian_filter(mask.astype(np.float32), 2.0)
cast = shift(height, (14, 22), order=1, mode="constant")
penumbra = gaussian_filter(cast, 10.0) * (1 - height)
base = np.broadcast_to(np.array([234., 228., 215.]), (h, w, 3)).copy()
base -= penumbra[..., None] * np.array([24., 25., 26.])
light = -np.gradient(height, axis=1) * 5.0
base += light[..., None]
base = np.clip(base, 22, 243).astype(np.uint8)
Image.fromarray(base).save("penumbra.png")
```

## Deckle-Edge Torn Paper / Intaglio Plate Mask
```python
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, distance_transform_edt
h, w = 2160, 3840
rng = np.random.Generator(np.random.PCG64(42))
y, x = np.mgrid[:h, :w]
noise = gaussian_filter(rng.normal(size=(h, w)), 3.0) * 18
inside = (x > 240 + noise) & (x < w - 240 + noise)
inside &= (y > 180 + noise) & (y < h - 180 + noise)
depth = distance_transform_edt(inside)
fiber = rng.normal(0, 0.7, (h, w))
plate = np.clip(depth / 2, 0, 1)
rgb = np.array([34, 36, 40]) + plate[..., None] * np.array([200, 192, 175])
rgb += fiber[..., None] * plate[..., None]
Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save("deckle.png")
```

## IIIF Museum 4K 1:1 Crop Fetcher
```python
import io
import httpx
from PIL import Image
service = "https://ids.lib.harvard.edu/ids/iiif/47174896"
with httpx.Client(timeout=30, follow_redirects=True) as client:
    info_response = client.get(service + "/info.json")
    info_response.raise_for_status()
    info = info_response.json()
    assert info["width"] >= 3840 and info["height"] >= 2160
    x, y = (info["width"] - 3840) // 2, (info["height"] - 2160) // 2
    response = client.get(f"{service}/{x},{y},3840,2160/full/0/default.jpg")
    response.raise_for_status()
image = Image.open(io.BytesIO(response.content)).convert("RGB")
assert image.size == (3840, 2160)
image.save("museum-native.png")
```
"""
