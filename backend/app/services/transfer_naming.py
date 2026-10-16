from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable


def collision_key(name: str) -> str:
    """Return the conservative key used when comparing destination names."""
    return unicodedata.normalize("NFC", name).casefold()


def allocate_available_name(
    original_name: str,
    occupied_names: Iterable[str],
    max_component_bytes: int | None = 255,
) -> str:
    """Keep an existing destination name and find the first numbered free name."""
    if (
        not isinstance(original_name, str)
        or not original_name
        or original_name in {".", ".."}
        or any(character in original_name for character in "/\\\x00")
    ):
        raise ValueError("Invalid destination component name")
    if max_component_bytes is not None and max_component_bytes < 1:
        raise ValueError("Destination component byte limit must be positive")

    occupied = {collision_key(name) for name in occupied_names}
    stem, extension = os.path.splitext(original_name)
    if not stem:
        stem = original_name
        extension = ""

    base_name = original_name
    if max_component_bytes is not None and len(original_name.encode("utf-8")) > max_component_bytes:
        base_name = _fit_component(stem, "", extension, max_component_bytes)
    if collision_key(base_name) not in occupied:
        return base_name

    number = 1
    while True:
        suffix = f"_{number:02d}"
        candidate = _fit_component(stem, suffix, extension, max_component_bytes)
        if collision_key(candidate) not in occupied:
            return candidate
        number += 1


def _fit_component(
    stem: str,
    suffix: str,
    extension: str,
    max_component_bytes: int | None,
) -> str:
    if max_component_bytes is None:
        return f"{stem}{suffix}{extension}"

    remaining = max_component_bytes - len(suffix.encode("utf-8")) - len(extension.encode("utf-8"))
    if remaining < 1:
        raise ValueError("Destination component limit cannot preserve the extension and suffix")

    shortened: list[str] = []
    used_bytes = 0
    for character in stem:
        character_bytes = len(character.encode("utf-8"))
        if used_bytes + character_bytes > remaining:
            break
        shortened.append(character)
        used_bytes += character_bytes
    if not shortened:
        raise ValueError("Destination component limit cannot preserve the file name")
    return f"{''.join(shortened)}{suffix}{extension}"
