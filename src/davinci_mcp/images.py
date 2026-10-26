# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Images returned to the client alongside structured results.

A result model that has an ``images: list[ImageFile]`` field gets each file
attached as an MCP image content block (the structured result keeps the paths).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal, cast

from mcp.types import ImageContent
from pydantic import BaseModel, Field

ImageMime = Literal["image/png", "image/jpeg", "image/webp"]
MAX_IMAGE_BYTES = 4 * 1024 * 1024


class ImageFile(BaseModel):
    path: str
    mime: ImageMime = "image/jpeg"
    width: int | None = None
    height: int | None = None
    caption: str | None = Field(default=None, description="What the image shows.")


def image_blocks(result: BaseModel) -> list[ImageContent]:
    images: object = getattr(result, "images", None)
    if not isinstance(images, list):
        return []
    blocks: list[ImageContent] = []
    for image in cast("list[object]", images):
        if not isinstance(image, ImageFile):
            continue
        path = Path(image.path)
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            continue
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        blocks.append(ImageContent(type="image", data=data, mime_type=image.mime))
    return blocks
