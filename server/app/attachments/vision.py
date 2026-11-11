"""可选视觉理解：把图片附件转成 OpenAI 兼容的 image_url content parts。"""

import base64
import io
import pathlib

from app.attachments.context import image_records
from app.core.exceptions import AttachmentStorageError


def enabled(settings) -> bool:
    return bool(getattr(settings, "attachment_vision", False))


def _image_path(settings, record: dict) -> pathlib.Path:
    root = pathlib.Path(settings.attachment_dir).resolve()
    path = (root / record["session_id"] / record["stored_name"]).resolve()
    if not path.is_relative_to(root):
        raise AttachmentStorageError("附件路径越界")
    return path


def build_parts(records: list[dict], settings) -> list[dict]:
    """最多返回 N 张图片的 content parts；失败的单张直接跳过。"""
    if not enabled(settings):
        return []
    limit = int(getattr(settings, "attachment_vision_max_images", 3))
    parts: list[dict] = []
    for record in image_records(records)[:limit]:
        if record.get("mime") == "image/svg+xml":
            continue
        try:
            from PIL import Image

            with Image.open(_image_path(settings, record)) as image:
                if image.mode not in {"RGB", "L"}:
                    background = Image.new("RGB", image.size, (255, 255, 255))
                    if "A" in image.getbands():
                        background.paste(image, mask=image.getchannel("A"))
                    else:
                        background.paste(image)
                    image = background
                image.thumbnail((1568, 1568))
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=85)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                }
            )
        except Exception:  # noqa: BLE001, S112 单张图片失败不影响文本回答
            continue
    return parts
