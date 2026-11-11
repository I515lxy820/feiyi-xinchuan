import base64
import io

from PIL import Image, ImageOps


Image.MAX_IMAGE_PIXELS = 40_000_000


class ImageValidationError(ValueError):
    pass


def prepare_image(data: bytes) -> str:
    if not data:
        raise ImageValidationError("图片为空")
    if len(data) > 5 * 1024 * 1024:
        raise ImageValidationError("图片不能超过 5 MB")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            if image.mode == "L":
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageValidationError("图片像素过大") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImageValidationError("无法识别图片，请使用 JPEG、PNG 或 WebP") from exc
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
