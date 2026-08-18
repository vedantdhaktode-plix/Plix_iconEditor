from __future__ import annotations

import gc
import colorsys
import hmac
import io
import math
import os
import re
import threading
import uuid
from collections import Counter
from collections import defaultdict, deque
from functools import lru_cache, wraps
from pathlib import Path
from typing import BinaryIO, Iterable, Sequence

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, UnidentifiedImageError

from supabase_catalog import (
    SupabaseCatalog,
    SupabaseCatalogError,
    SupabaseSettings,
)


BASE_DIR = Path(__file__).resolve().parent
ICONS_DIR = BASE_DIR / "icons"

# Supports either of these layouts:
#   app.py + index.html
#   app.py + templates/index.html
STANDARD_TEMPLATE_DIR = BASE_DIR / "templates"
TEMPLATE_DIR = (
    BASE_DIR
    if (BASE_DIR / "index.html").is_file()
    else STANDARD_TEMPLATE_DIR
)

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024

MAX_OUTPUT_SIZE = 1024
MAX_ICON_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ICON_NAME_LENGTH = 100
MAX_MANUAL_TAGS = 25
MAX_TAG_LENGTH = 40
Image.MAX_IMAGE_PIXELS = 25_000_000
# Keep CPU and memory bounded on small hosting instances. The UI exports at
# up to 400 px, so a 384 px working mask keeps edges clean without analysing
# every pixel of a potentially very large source file.
PROCESSING_MIN_DIMENSION = 192
PROCESSING_MAX_DIMENSION = 384
PROCESSING_LOCK = threading.Lock()
HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
RESAMPLING_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS

# Legacy IDs and tags are retained only for local-development fallback and old
# bookmarked process URLs. With Supabase configured, public.icons is the catalog
# source of truth, including inactive records.
LOCAL_ICON_METADATA = {
    "cream.png": {
        "legacy_id": "1",
        "name": "Cream",
        "tags": ["cream", "finger", "hand", "lotion", "moisturizer"],
    },
    "lotion.png": {
        "legacy_id": "2",
        "name": "Lotion",
        "tags": ["c", "drop", "pink", "white", "serum", "lotion"],
    },
    "guava.png": {
        "legacy_id": "3",
        "name": "Guava",
        "tags": ["apple", "fruit", "red", "guava"],
    },
    "c_drop.png": {
        "legacy_id": "4",
        "name": "C Drop",
        "tags": ["c", "drop", "serum", "pink"],
    },
    "coffee_seed.png": {
        "legacy_id": "5",
        "name": "Coffee Seed",
        "tags": ["coffee", "seed", "bean", "brown"],
    },
    "drop.png": {
        "legacy_id": "6",
        "name": "Drop",
        "tags": ["drop", "water", "liquid", "blue"],
    },
    "hands.png": {
        "legacy_id": "7",
        "name": "Hands",
        "tags": ["hands", "care", "protection", "skin"],
    },
    "pink_star.png": {
        "legacy_id": "8",
        "name": "Pink Star",
        "tags": ["pink", "star", "badge", "sparkle"],
    },
    "purple_drop.png": {
        "legacy_id": "9",
        "name": "Purple Drop",
        "tags": ["purple", "drop", "liquid", "oil"],
    },
    "sponge_bar.png": {
        "legacy_id": "10",
        "name": "Sponge Bar",
        "tags": ["sponge", "bar", "clean", "wash"],
    },
    "star_bar.png": {
        "legacy_id": "11",
        "name": "Star Bar",
        "tags": ["star", "bar", "rating", "yellow"],
    },
}
COLOR_FAMILIES = (
    "red",
    "orange",
    "yellow",
    "green",
    "cyan",
    "blue",
    "purple",
    "pink",
    "brown",
    "black",
    "white",
    "gray",
)


def normalize_icon_name(value: object) -> str:
    """Validate and normalize a teammate-facing icon name."""
    name = " ".join(str(value or "").split())
    if not name:
        raise ValueError("Icon name is required")
    if len(name) > MAX_ICON_NAME_LENGTH:
        raise ValueError(
            f"Icon name must be {MAX_ICON_NAME_LENGTH} characters or fewer"
        )
    return name


def normalize_manual_tags(value: object) -> list[str]:
    """Accept comma/newline strings or JSON arrays and return clean tags."""
    if value is None:
        return []
    if isinstance(value, str):
        candidates = re.split(r"[,\n]", value)
    elif isinstance(value, list):
        candidates = value
    else:
        raise ValueError("Tags must be a comma-separated string or a list")

    tags: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        tag = " ".join(str(candidate).strip().lower().split())
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(
                f"Each tag must be {MAX_TAG_LENGTH} characters or fewer"
            )
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
        if len(tags) > MAX_MANUAL_TAGS:
            raise ValueError(f"Use no more than {MAX_MANUAL_TAGS} tags")
    return tags


def color_family(red: int, green: int, blue: int) -> str:
    """Map an RGB color to one of the supported searchable color families."""
    hue, saturation, value = colorsys.rgb_to_hsv(
        red / 255.0, green / 255.0, blue / 255.0
    )
    hue_degrees = hue * 360.0

    if value <= 0.15:
        return "black"
    if saturation <= 0.12 and value >= 0.88:
        return "white"
    if saturation <= 0.16:
        return "gray"
    if 15 <= hue_degrees < 50 and value <= 0.68:
        return "brown"
    if hue_degrees < 15 or hue_degrees >= 345:
        return "red"
    if hue_degrees < 45:
        return "orange"
    if hue_degrees < 70:
        return "yellow"
    if hue_degrees < 165:
        return "green"
    if hue_degrees < 200:
        return "cyan"
    if hue_degrees < 255:
        return "blue"
    if hue_degrees < 295:
        return "purple"
    return "pink"


def detect_dominant_color_families(image: Image.Image) -> list[str]:
    """Detect up to four significant color families from an icon."""
    sample = image.convert("RGBA")
    sample.thumbnail((160, 160), RESAMPLING_LANCZOS)
    counts: Counter[str] = Counter()
    total_weight = 0

    for red, green, blue, alpha in sample.getdata():
        if alpha < 32:
            continue
        weight = max(1, alpha // 32)
        counts[color_family(red, green, blue)] += weight
        total_weight += weight

    if not counts or total_weight == 0:
        return []

    minimum_weight = max(1, round(total_weight * 0.025))
    detected = [
        family
        for family, weight in counts.most_common()
        if weight >= minimum_weight
    ]
    return detected[:4] or [counts.most_common(1)[0][0]]


@lru_cache(maxsize=32)
def local_icon_colors(path_text: str, modified_ns: int) -> tuple[str, ...]:
    del modified_ns
    with Image.open(path_text) as image:
        return tuple(detect_dominant_color_families(image))


def validate_png_upload(upload: object) -> tuple[bytes, str, int, int, list[str]]:
    """Read and validate an uploaded PNG without writing it to local storage."""
    raw_filename = str(getattr(upload, "filename", "") or "")
    filename = re.split(r"[\\/]", raw_filename)[-1].strip()
    if not filename or Path(filename).suffix.lower() != ".png":
        raise ValueError("Choose a PNG file with a .png extension")

    stream = getattr(upload, "stream", None)
    if stream is None:
        raise ValueError("The uploaded PNG could not be read")
    png_bytes = stream.read(MAX_ICON_UPLOAD_BYTES + 1)
    if not png_bytes:
        raise ValueError("The uploaded PNG is empty")
    if len(png_bytes) > MAX_ICON_UPLOAD_BYTES:
        raise ValueError("PNG files must be 5 MB or smaller")

    try:
        with Image.open(io.BytesIO(png_bytes)) as candidate:
            if candidate.format != "PNG":
                raise ValueError("The uploaded file is not a PNG")
            width, height = candidate.size
            if width * height > int(Image.MAX_IMAGE_PIXELS or 0):
                raise ValueError("The PNG dimensions are too large to process safely")
            candidate.verify()
        with Image.open(io.BytesIO(png_bytes)) as candidate:
            candidate.load()
            colors = detect_dominant_color_families(candidate)
    except ValueError:
        raise
    except (Image.DecompressionBombError, OSError, SyntaxError, UnidentifiedImageError) as error:
        raise ValueError("The PNG is corrupt or unsafe to process") from error

    if width <= 0 or height <= 0:
        raise ValueError("The PNG has invalid dimensions")
    return png_bytes, filename, width, height, colors


def generated_icon_name(filename: str) -> str:
    return Path(filename).stem.replace("_", " ").replace("-", " ").title()


def local_fallback_icons() -> list[dict[str, object]]:
    """Scan bundled PNGs when Supabase is absent or temporarily unavailable."""
    records: list[dict[str, object]] = []
    for image_path in sorted(ICONS_DIR.glob("*.png")):
        metadata = LOCAL_ICON_METADATA.get(image_path.name, {})
        modified_ns = image_path.stat().st_mtime_ns
        records.append(
            {
                "id": str(metadata.get("legacy_id") or f"local:{image_path.name}"),
                "name": str(metadata.get("name") or generated_icon_name(image_path.name)),
                "source": "local",
                "local_filename": image_path.name,
                "storage_path": None,
                "original_filename": image_path.name,
                "tags": list(metadata.get("tags") or []),
                "colors": list(local_icon_colors(str(image_path), modified_ns)),
                "width": None,
                "height": None,
                "is_active": True,
                "updated_at": f"local:{modified_ns}",
            }
        )
    records.sort(key=lambda record: str(record["name"]).casefold())
    return records


def get_catalog_service() -> SupabaseCatalog:
    factory = app.config.get("SUPABASE_CATALOG_FACTORY")
    if callable(factory):
        return factory()
    return SupabaseCatalog(SupabaseSettings.from_environment())


def load_active_catalog() -> tuple[list[dict[str, object]], str]:
    service = get_catalog_service()
    if service.configured:
        try:
            return service.list_icons(active_only=True), "supabase"
        except SupabaseCatalogError:
            app.logger.warning(
                "Supabase catalog unavailable; serving bundled local icons",
                exc_info=True,
            )
            return local_fallback_icons(), "local-fallback"
    return local_fallback_icons(), "local"


def resolve_icon_record(icon_id: str) -> dict[str, object] | None:
    service = get_catalog_service()
    if service.configured:
        try:
            return service.get_icon(icon_id, active_only=True)
        except SupabaseCatalogError:
            local_record = next(
                (
                    record
                    for record in local_fallback_icons()
                    if record["id"] == icon_id
                ),
                None,
            )
            if local_record is not None:
                app.logger.warning(
                    "Supabase lookup unavailable; processing bundled local icon",
                    exc_info=True,
                )
                return local_record
            raise
    return next(
        (record for record in local_fallback_icons() if record["id"] == icon_id),
        None,
    )


def icon_filename(icon: dict[str, object]) -> str:
    return str(
        icon.get("original_filename")
        or icon.get("local_filename")
        or Path(str(icon.get("storage_path") or "icon.png")).name
    )


def searchable_icon_tags(icon: dict[str, object]) -> list[str]:
    candidates: list[object] = [
        icon_filename(icon),
        Path(icon_filename(icon)).stem.replace("_", " ").replace("-", " "),
        icon.get("name") or "",
        *(icon.get("tags") or []),
        *(icon.get("colors") or []),
    ]
    terms: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        term = " ".join(str(candidate).strip().lower().split())
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def public_icon_record(icon: dict[str, object]) -> dict[str, object]:
    icon_id = str(icon["id"])
    source = str(icon.get("source") or "local")
    filename = icon_filename(icon)
    local_path = ICONS_DIR / str(icon.get("local_filename") or "")
    available = bool(icon.get("storage_path")) if source == "storage" else local_path.is_file()
    return {
        "id": icon_id,
        "name": str(icon.get("name") or generated_icon_name(filename)),
        "src": filename,
        "filename": filename,
        "tags": list(icon.get("tags") or []),
        "colors": list(icon.get("colors") or []),
        "search_tags": searchable_icon_tags(icon),
        "source": source,
        "available": available,
        "thumbnail_url": f"/api/icons/{icon_id}",
    }


def icon_matches_query(icon: dict[str, object], query: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", query.casefold())
    if not tokens:
        return True
    haystack = " ".join(searchable_icon_tags(icon))
    return all(token in haystack for token in tokens)


def require_admin(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        configured_password = os.environ.get("ADMIN_PASSWORD", "")
        if not configured_password:
            return jsonify({"error": "Icon management is not configured"}), 503
        supplied_password = request.headers.get("X-Admin-Password", "")
        if not hmac.compare_digest(supplied_password, configured_password):
            response = jsonify({"error": "Incorrect admin password"})
            response.status_code = 401
            response.headers["Cache-Control"] = "no-store"
            return response
        return function(*args, **kwargs)

    return wrapped


def configured_catalog_for_admin() -> SupabaseCatalog:
    service = get_catalog_service()
    service.require_configured()
    return service


def normalize_hex_color(value: str | None) -> tuple[int, int, int] | None:
    """Validate a browser color value and return an RGB tuple."""
    if value is None or value == "":
        return None

    match = HEX_COLOR_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("bg_color must be a six-digit hexadecimal color")

    hex_value = match.group(1)
    return tuple(int(hex_value[index : index + 2], 16) for index in (0, 2, 4))


def clamp_dimension(value: int | None) -> int | None:
    """Keep requested output dimensions within a safe range."""
    if value is None:
        return None
    return max(16, min(MAX_OUTPUT_SIZE, int(value)))


def color_distance_squared(
    first: tuple[int, int, int], second: tuple[int, int, int]
) -> int:
    return sum((first[channel] - second[channel]) ** 2 for channel in range(3))


def border_indices(width: int, height: int) -> list[int]:
    """Return all unique pixel indexes around the outer border."""
    if width <= 0 or height <= 0:
        return []

    indexes: set[int] = set()
    for x in range(width):
        indexes.add(x)
        indexes.add((height - 1) * width + x)
    for y in range(height):
        indexes.add(y * width)
        indexes.add(y * width + (width - 1))
    return list(indexes)


def ring_indices(width: int, height: int) -> list[int]:
    """
    Sample several rings around the illustration.

    Flat icon backgrounds normally occupy these rings, while the subject is
    usually concentrated closer to the center.
    """
    if width < 3 or height < 3:
        return border_indices(width, height)

    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0
    shortest_side = min(width, height)
    samples_per_ring = max(72, min(240, int(shortest_side * 1.25)))

    indexes: set[int] = set()
    for radius_fraction in (0.28, 0.34, 0.40):
        radius = shortest_side * radius_fraction
        for sample_number in range(samples_per_ring):
            angle = (2.0 * math.pi * sample_number) / samples_per_ring
            x = int(round(center_x + math.cos(angle) * radius))
            y = int(round(center_y + math.sin(angle) * radius))
            if 0 <= x < width and 0 <= y < height:
                indexes.add(y * width + x)

    return list(indexes)


def dominant_color_candidates(
    pixels: Sequence[tuple[int, int, int, int]],
    indexes: Iterable[int],
    *,
    bin_size: int = 16,
    minimum_alpha: int = 24,
) -> tuple[list[tuple[tuple[int, int, int], int]], int]:
    """
    Return coarse RGB color clusters, ordered by frequency.

    Coarse bins make the detector tolerant of compression and anti-aliasing.
    """
    buckets: dict[tuple[int, int, int], list[int]] = defaultdict(
        lambda: [0, 0, 0, 0]
    )
    opaque_samples = 0

    for index in indexes:
        red, green, blue, alpha = pixels[index]
        if alpha < minimum_alpha:
            continue

        opaque_samples += 1
        key = (red // bin_size, green // bin_size, blue // bin_size)
        bucket = buckets[key]
        bucket[0] += 1
        bucket[1] += red
        bucket[2] += green
        bucket[3] += blue

    candidates: list[tuple[tuple[int, int, int], int]] = []
    for count, red_sum, green_sum, blue_sum in buckets.values():
        candidates.append(
            (
                (
                    round(red_sum / count),
                    round(green_sum / count),
                    round(blue_sum / count),
                ),
                count,
            )
        )

    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates, opaque_samples


def connected_color_mask(
    pixels: Sequence[tuple[int, int, int, int]],
    width: int,
    height: int,
    seeds: Iterable[int],
    target_color: tuple[int, int, int],
    *,
    core_tolerance: float,
    edge_tolerance: float,
) -> bytearray:
    """
    Build a soft mask for pixels connected to seed points and close to a color.

    Restricting the mask to connected regions prevents similarly colored,
    isolated foreground details from being removed accidentally.
    """
    pixel_count = width * height
    visited = bytearray(pixel_count)
    mask = bytearray(pixel_count)
    queue: deque[int] = deque()

    core_squared = core_tolerance * core_tolerance
    edge_squared = edge_tolerance * edge_tolerance
    feather_range = max(0.001, edge_tolerance - core_tolerance)

    def mask_strength(index: int) -> int:
        red, green, blue, alpha = pixels[index]
        if alpha <= 2:
            return 0

        distance_squared = color_distance_squared(
            (red, green, blue), target_color
        )
        if distance_squared > edge_squared:
            return 0
        if distance_squared <= core_squared:
            return 255

        distance = math.sqrt(distance_squared)
        strength = 255.0 * (edge_tolerance - distance) / feather_range
        return max(1, min(254, round(strength)))

    for seed in seeds:
        if seed < 0 or seed >= pixel_count or visited[seed]:
            continue
        visited[seed] = 1
        strength = mask_strength(seed)
        if strength:
            mask[seed] = strength
            queue.append(seed)

    while queue:
        index = queue.popleft()
        x = index % width
        y = index // width

        if x > 0:
            neighbour = index - 1
            if not visited[neighbour]:
                visited[neighbour] = 1
                strength = mask_strength(neighbour)
                if strength:
                    mask[neighbour] = strength
                    queue.append(neighbour)

        if x + 1 < width:
            neighbour = index + 1
            if not visited[neighbour]:
                visited[neighbour] = 1
                strength = mask_strength(neighbour)
                if strength:
                    mask[neighbour] = strength
                    queue.append(neighbour)

        if y > 0:
            neighbour = index - width
            if not visited[neighbour]:
                visited[neighbour] = 1
                strength = mask_strength(neighbour)
                if strength:
                    mask[neighbour] = strength
                    queue.append(neighbour)

        if y + 1 < height:
            neighbour = index + width
            if not visited[neighbour]:
                visited[neighbour] = 1
                strength = mask_strength(neighbour)
                if strength:
                    mask[neighbour] = strength
                    queue.append(neighbour)

    return mask


def mask_coverage(mask: bytearray, minimum_strength: int = 24) -> float:
    if not mask:
        return 0.0
    covered = sum(1 for value in mask if value >= minimum_strength)
    return covered / len(mask)


def combine_masks(*masks: bytearray) -> bytearray:
    usable_masks = [mask for mask in masks if mask]
    if not usable_masks:
        return bytearray()
    if len(usable_masks) == 1:
        return bytearray(usable_masks[0])
    if len(usable_masks) == 2:
        first, second = usable_masks
        return bytearray(
            left if left >= right else right
            for left, right in zip(first, second)
        )

    combined = bytearray(usable_masks[0])
    for mask in usable_masks[1:]:
        for index, value in enumerate(mask):
            if value > combined[index]:
                combined[index] = value
    return combined


def pillow_mask(
    mask: bytearray,
    size: tuple[int, int],
    *,
    expand: bool = False,
) -> Image.Image:
    image = Image.frombytes("L", size, bytes(mask))
    if expand and min(size) >= 3:
        # One-pixel expansion removes colored anti-aliasing halos left at the
        # outside edge of transparent backgrounds.
        image = image.filter(ImageFilter.MaxFilter(3))
    # A light blur keeps anti-aliased edges natural without swallowing detail.
    return image.filter(ImageFilter.GaussianBlur(radius=0.65))


def clear_with_mask(image: Image.Image, mask: Image.Image) -> Image.Image:
    result = image.copy()
    current_alpha = result.getchannel("A")
    remaining_alpha = ImageChops.multiply(current_alpha, ImageOps.invert(mask))
    result.putalpha(remaining_alpha)
    return result


def replace_with_color(
    image: Image.Image, mask: Image.Image, color: tuple[int, int, int]
) -> Image.Image:
    solid = Image.new("RGBA", image.size, (*color, 255))
    return Image.composite(solid, image, mask)


def add_color_behind_transparent_artwork(
    image: Image.Image, color: tuple[int, int, int]
) -> Image.Image:
    """Fallback for source files that are already transparent cut-outs."""
    background = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(background)
    draw.ellipse((0, 0, image.width - 1, image.height - 1), fill=(*color, 255))
    background.alpha_composite(image)
    return background


def process_icon_image(
    image_source: Path | BinaryIO,
    width: int | None = None,
    height: int | None = None,
    remove_background: bool = False,
    background_color: tuple[int, int, int] | None = None,
) -> io.BytesIO:
    """
    Remove or recolor a flat icon background with bounded CPU and memory use.

    Expensive analysis runs on a small working copy. The full-resolution source
    is not converted to RGBA before resizing, which keeps peak memory low for
    large PNG files on small hosting instances.
    """
    with Image.open(image_source) as source:
        source_width, source_height = source.size
        output_width = width or source_width
        output_height = height or source_height
        output_size = (output_width, output_height)

        # A plain resize needs no background analysis.
        if not remove_background and background_color is None:
            if source.size != output_size:
                resized = source.resize(output_size, RESAMPLING_LANCZOS)
            else:
                resized = source.copy()
            result = resized.convert("RGBA")
        else:
            requested_long_side = max(output_size)
            working_long_side = min(
                PROCESSING_MAX_DIMENSION,
                max(PROCESSING_MIN_DIMENSION, requested_long_side),
            )
            scale = working_long_side / requested_long_side
            working_size = (
                max(16, round(output_width * scale)),
                max(16, round(output_height * scale)),
            )

            if source.size != working_size:
                resized = source.resize(working_size, RESAMPLING_LANCZOS)
            else:
                resized = source.copy()
            image = resized.convert("RGBA")
            result = _process_working_image(
                image,
                remove_background=remove_background,
                background_color=background_color,
            )

    if result.size != output_size:
        result = result.resize(output_size, RESAMPLING_LANCZOS)

    output = io.BytesIO()
    result.save(output, format="PNG", compress_level=4)
    output.seek(0)
    return output


def _process_working_image(
    image: Image.Image,
    *,
    remove_background: bool,
    background_color: tuple[int, int, int] | None,
) -> Image.Image:
    """Run mask analysis on an already bounded RGBA working image."""
    # ImagingCore is indexable, so avoid materialising a large Python list of
    # per-pixel tuples. This substantially reduces memory use.
    pixel_data = image.getdata()
    image_width, image_height = image.size
    total_pixels = image_width * image_height

    outer_indexes = border_indices(image_width, image_height)
    outer_candidates, opaque_border_samples = dominant_color_candidates(
        pixel_data, outer_indexes
    )

    outer_color: tuple[int, int, int] | None = None
    minimum_opaque_border = max(8, round(len(outer_indexes) * 0.20))
    if outer_candidates and opaque_border_samples >= minimum_opaque_border:
        candidate_color, candidate_count = outer_candidates[0]
        if candidate_count >= max(4, round(opaque_border_samples * 0.12)):
            outer_color = candidate_color

    outer_mask = bytearray(total_pixels)
    if outer_color is not None:
        outer_seed_indexes = [
            index
            for index in outer_indexes
            if pixel_data[index][3] > 2
            and color_distance_squared(pixel_data[index][:3], outer_color)
            <= 72 * 72
        ]
        outer_mask = connected_color_mask(
            pixel_data,
            image_width,
            image_height,
            outer_seed_indexes,
            outer_color,
            core_tolerance=30,
            edge_tolerance=72,
        )
        if mask_coverage(outer_mask) < 0.002:
            outer_mask = bytearray(total_pixels)

    inner_indexes = ring_indices(image_width, image_height)
    inner_candidates, opaque_ring_samples = dominant_color_candidates(
        pixel_data, inner_indexes
    )

    inner_color: tuple[int, int, int] | None = None
    minimum_opaque_ring = max(12, round(len(inner_indexes) * 0.25))
    if inner_candidates and opaque_ring_samples >= minimum_opaque_ring:
        inner_color = inner_candidates[0][0]
        if outer_color is not None and color_distance_squared(
            inner_color, outer_color
        ) <= 44 * 44:
            for alternative_color, alternative_count in inner_candidates[1:]:
                if (
                    alternative_count
                    >= max(8, round(opaque_ring_samples * 0.08))
                    and color_distance_squared(alternative_color, outer_color)
                    > 44 * 44
                ):
                    inner_color = alternative_color
                    break

    inner_mask = bytearray(total_pixels)
    if inner_color is not None:
        inner_seed_indexes = [
            index
            for index in inner_indexes
            if pixel_data[index][3] > 2
            and color_distance_squared(pixel_data[index][:3], inner_color)
            <= 96 * 96
        ]
        inner_mask = connected_color_mask(
            pixel_data,
            image_width,
            image_height,
            inner_seed_indexes,
            inner_color,
            core_tolerance=38,
            edge_tolerance=96,
        )
        if mask_coverage(inner_mask) < 0.02:
            inner_mask = bytearray(total_pixels)
            inner_color = None

    outer_coverage = mask_coverage(outer_mask)
    inner_coverage = mask_coverage(inner_mask)
    result = image

    if remove_background:
        removal_mask_bytes = combine_masks(outer_mask, inner_mask)
        if removal_mask_bytes and mask_coverage(removal_mask_bytes) > 0.001:
            result = clear_with_mask(
                result,
                pillow_mask(removal_mask_bytes, result.size, expand=True),
            )

    elif background_color is not None:
        colors_are_distinct = (
            outer_color is not None
            and inner_color is not None
            and color_distance_squared(outer_color, inner_color) > 44 * 44
        )

        if inner_coverage > 0:
            if colors_are_distinct:
                result = replace_with_color(
                    result,
                    pillow_mask(inner_mask, result.size),
                    background_color,
                )
                if outer_coverage > 0:
                    result = clear_with_mask(
                        result,
                        pillow_mask(outer_mask, result.size, expand=True),
                    )
            else:
                replacement_mask_bytes = combine_masks(outer_mask, inner_mask)
                result = replace_with_color(
                    result,
                    pillow_mask(replacement_mask_bytes, result.size),
                    background_color,
                )
        elif outer_coverage > 0:
            result = replace_with_color(
                result,
                pillow_mask(outer_mask, result.size),
                background_color,
            )
        else:
            result = add_color_behind_transparent_artwork(
                result, background_color
            )

    return result


@lru_cache(maxsize=24)
def cached_processed_bytes(
    image_path_text: str,
    image_mtime_ns: int,
    width: int,
    height: int,
    remove_background: bool,
    background_color: tuple[int, int, int] | None,
) -> bytes:
    """Serialize expensive processing and cache recent previews."""
    del image_mtime_ns  # Included in the key so changed files invalidate cache.
    with PROCESSING_LOCK:
        stream = process_icon_image(
            image_source=Path(image_path_text),
            width=width or None,
            height=height or None,
            remove_background=remove_background,
            background_color=background_color,
        )
        data = stream.getvalue()
        stream.close()
        gc.collect()
        return data


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({"error": "PNG files must be 5 MB or smaller"}), 413


@app.get("/api/search")
def search_icons():
    query = (
        request.args.get("q") or request.args.get("tag") or ""
    ).strip()
    icons, catalog_source = load_active_catalog()
    if query:
        icons = [icon for icon in icons if icon_matches_query(icon, query)]

    response = jsonify([public_icon_record(icon) for icon in icons])
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Icon-Catalog"] = catalog_source
    return response


@app.get("/icons/<path:filename>")
def raw_icon(filename: str):
    """Preserve the original local-icon URL used by existing clients."""
    if (
        re.split(r"[\\/]", filename)[-1] != filename
        or Path(filename).suffix.lower() != ".png"
        or not (ICONS_DIR / filename).is_file()
    ):
        abort(404)
    return send_from_directory(str(ICONS_DIR), filename, max_age=0)


@app.get("/api/icons/<icon_id>")
def source_icon(icon_id: str):
    try:
        icon = resolve_icon_record(icon_id)
    except SupabaseCatalogError as error:
        app.logger.warning("Could not resolve icon source: %s", error)
        return jsonify({"error": str(error)}), 502
    if icon is None:
        return jsonify({"error": "Unknown icon id"}), 404

    if icon.get("source") == "storage":
        storage_path = str(icon.get("storage_path") or "")
        if not storage_path:
            return jsonify({"error": "Icon storage path is missing"}), 404
        try:
            png_bytes = get_catalog_service().download_png(storage_path)
        except SupabaseCatalogError as error:
            app.logger.warning("Could not download icon source: %s", error)
            return jsonify({"error": str(error)}), 502
        response = send_file(
            io.BytesIO(png_bytes),
            mimetype="image/png",
            download_name=icon_filename(icon),
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    filename = str(icon.get("local_filename") or "")
    if (
        not filename
        or re.split(r"[\\/]", filename)[-1] != filename
        or not (ICONS_DIR / filename).is_file()
    ):
        return jsonify({"error": "Local icon file was not found"}), 404
    return send_from_directory(str(ICONS_DIR), filename, max_age=0)


@app.post("/api/admin/check")
@require_admin
def check_admin_password():
    response = jsonify({"status": "ok"})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/admin/icons")
@require_admin
def admin_icons():
    try:
        icons = configured_catalog_for_admin().list_icons(active_only=True)
    except SupabaseCatalogError as error:
        app.logger.warning("Admin catalog request failed: %s", error)
        return jsonify({"error": str(error)}), 502
    response = jsonify([public_icon_record(icon) for icon in icons])
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/admin/icons")
@require_admin
def upload_icon():
    try:
        name = normalize_icon_name(request.form.get("name"))
        tags = normalize_manual_tags(request.form.get("tags", ""))
        upload = request.files.get("file")
        if upload is None:
            raise ValueError("Choose a PNG file to upload")
        png_bytes, filename, width, height, colors = validate_png_upload(upload)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    slug = re.sub(r"[^a-z0-9]+", "-", Path(filename).stem.casefold()).strip("-")
    storage_path = f"uploads/{uuid.uuid4().hex}-{slug or 'icon'}.png"
    try:
        service = configured_catalog_for_admin()
        service.upload_png(storage_path, png_bytes)
        try:
            icon = service.insert_icon(
                {
                    "name": name,
                    "source": "storage",
                    "local_filename": None,
                    "storage_path": storage_path,
                    "original_filename": filename,
                    "tags": tags,
                    "colors": colors,
                    "width": width,
                    "height": height,
                    "is_active": True,
                }
            )
        except SupabaseCatalogError:
            try:
                service.remove_png(storage_path)
            except SupabaseCatalogError:
                app.logger.exception(
                    "Could not clean up Storage after metadata insert failed"
                )
            raise
    except SupabaseCatalogError as error:
        app.logger.warning("Icon upload failed: %s", error)
        return jsonify({"error": str(error)}), 502

    response = jsonify(public_icon_record(icon))
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    return response


@app.patch("/api/admin/icons/<icon_id>")
@require_admin
def edit_icon(icon_id: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Send icon metadata as JSON"}), 400

    updates: dict[str, object] = {}
    try:
        if "name" in payload:
            updates["name"] = normalize_icon_name(payload["name"])
        if "tags" in payload:
            updates["tags"] = normalize_manual_tags(payload["tags"])
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if not updates:
        return jsonify({"error": "Provide a name or tags to update"}), 400

    try:
        icon = configured_catalog_for_admin().update_icon(icon_id, updates)
    except SupabaseCatalogError as error:
        app.logger.warning("Icon metadata update failed: %s", error)
        return jsonify({"error": str(error)}), 502
    if icon is None:
        return jsonify({"error": "Unknown icon id"}), 404

    response = jsonify(public_icon_record(icon))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.delete("/api/admin/icons/<icon_id>")
@require_admin
def remove_icon(icon_id: str):
    try:
        service = configured_catalog_for_admin()
        icon = service.get_icon(icon_id, active_only=True)
        if icon is None:
            return jsonify({"error": "Unknown icon id"}), 404

        if icon.get("source") == "storage" and not icon.get("storage_path"):
            return jsonify({"error": "Icon storage path is missing"}), 409

        deactivated = service.update_icon(icon_id, {"is_active": False})
        if deactivated is None:
            return jsonify({"error": "Unknown icon id"}), 404

        if icon.get("source") == "storage":
            try:
                service.remove_png(str(icon["storage_path"]))
            except SupabaseCatalogError:
                try:
                    service.update_icon(icon_id, {"is_active": True})
                except SupabaseCatalogError:
                    app.logger.exception(
                        "Could not reactivate icon after Storage removal failed"
                    )
                raise
    except SupabaseCatalogError as error:
        app.logger.warning("Icon removal failed: %s", error)
        return jsonify({"error": str(error)}), 502

    response = jsonify({"id": icon_id, "status": "removed"})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/process/<icon_id>")
def process_icon(icon_id: str):
    try:
        icon = resolve_icon_record(icon_id)
    except SupabaseCatalogError as error:
        app.logger.warning("Could not resolve icon for processing: %s", error)
        return jsonify({"error": str(error)}), 502
    if icon is None:
        return jsonify({"error": "Unknown icon id"}), 404

    requested_size = request.args.get("size", type=int)
    width = clamp_dimension(request.args.get("width", type=int) or requested_size)
    height = clamp_dimension(request.args.get("height", type=int) or requested_size)
    remove_background = (
        request.args.get("remove_bg", "false").strip().lower() == "true"
    )

    try:
        background_color = normalize_hex_color(request.args.get("bg_color"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    # Transparency takes precedence over a replacement color.
    if remove_background:
        background_color = None

    try:
        if icon.get("source") == "storage":
            storage_path = str(icon.get("storage_path") or "")
            if not storage_path:
                return jsonify({"error": "Icon storage path is missing"}), 404
            source_bytes = get_catalog_service().download_png(storage_path)
            if len(source_bytes) > MAX_ICON_UPLOAD_BYTES:
                return jsonify({"error": "Stored PNG exceeds the 5 MB limit"}), 422
            with PROCESSING_LOCK:
                processed_stream = process_icon_image(
                    image_source=io.BytesIO(source_bytes),
                    width=width,
                    height=height,
                    remove_background=remove_background,
                    background_color=background_color,
                )
                image_bytes = processed_stream.getvalue()
                processed_stream.close()
                gc.collect()
        else:
            filename = str(icon.get("local_filename") or "")
            if re.split(r"[\\/]", filename)[-1] != filename:
                return jsonify({"error": "Invalid local icon path"}), 404
            image_path = ICONS_DIR / filename
            if not image_path.is_file():
                return jsonify({"error": f"{filename} was not found"}), 404
            image_bytes = cached_processed_bytes(
                str(image_path),
                image_path.stat().st_mtime_ns,
                width or 0,
                height or 0,
                remove_background,
                background_color,
            )
        image_stream = io.BytesIO(image_bytes)
    except SupabaseCatalogError as error:
        app.logger.warning("Stored icon download failed: %s", error)
        return jsonify({"error": str(error)}), 502
    except (OSError, UnidentifiedImageError) as error:
        return jsonify({"error": f"Could not read image: {error}"}), 500
    except Exception as error:  # Keeps the image endpoint useful in production.
        app.logger.exception("Icon processing failed")
        return jsonify({"error": f"Image processing failed: {error}"}), 500

    as_download = (
        request.args.get("download", "false").strip().lower() == "true"
    )
    output_name = f"{Path(icon_filename(icon)).stem}_custom.png"

    response = send_file(
        image_stream,
        mimetype="image/png",
        as_attachment=as_download,
        download_name=output_name,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


if __name__ == "__main__":
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
