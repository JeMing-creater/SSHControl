from __future__ import annotations

import re


SUPPORTED_BASE_IMAGE_PATTERN = re.compile(
    r"^pytorch:\d+(?:\.\d+){1,2}(?:[._-][A-Za-z0-9]+)*"
    r"-cuda\d+(?:\.\d+){1,2}"
    r"-cudnn\d+(?:[A-Za-z0-9._-]*)?$"
)


def is_supported_base_image(image_ref: str | None) -> bool:
    image = (image_ref or "").strip()
    return bool(SUPPORTED_BASE_IMAGE_PATTERN.fullmatch(image))


def filter_supported_base_images(image_refs: list[str]) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()
    for image_ref in image_refs:
        image = str(image_ref or "").strip()
        if not image or image in seen or not is_supported_base_image(image):
            continue
        seen.add(image)
        images.append(image)
    return images
