"""File processing for Drop-to-Action: OCR, compression and conversion.

Pure functions with no Qt dependency, so they can be tested without a
display and called from any worker thread.

Two rules run through all of it.

**Never destroy the user's file.** Every operation writes a new file beside
the original with a non-colliding name. Nothing is overwritten and nothing
is deleted, because a drop onto a floating overlay is an easy thing to do
by accident.

**Never make a file worse.** If a "compressed" output comes out larger than
the input, which happens with already-optimised images, the output is
discarded and the result says the file was already optimal. Writing a
bigger file and calling it compression is the kind of thing that makes
these tools untrustworthy.

Heavy imports (Pillow, pypdf, the ONNX runtime) happen on first use, never
at module import, so the island's idle memory does not pay for features
nobody triggered.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".gif",
}
PDF_SUFFIXES = {".pdf"}
TEXT_SUFFIXES = {".txt", ".md", ".json", ".csv", ".log", ".xml", ".yaml", ".yml"}

# Quality settings. 82 is the usual sweet spot where re-encoding is not
# visible at 100% zoom but the saving is still substantial.
JPEG_QUALITY = 82
WEBP_QUALITY = 85


class Kind(StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    TEXT = "text"
    OTHER = "other"


class Operation(StrEnum):
    EXTRACT_TEXT = "extract_text"
    COMPRESS = "compress"
    TO_WEBP = "to_webp"
    TO_PNG = "to_png"


@dataclass
class Result:
    """Outcome of one operation on one file."""

    source: Path
    operation: Operation
    ok: bool
    message: str
    output: Path | None = None
    text: str | None = None
    bytes_before: int = 0
    bytes_after: int = 0
    seconds: float = 0.0

    @property
    def saved_bytes(self) -> int:
        if not self.bytes_before or not self.bytes_after:
            return 0
        return max(0, self.bytes_before - self.bytes_after)

    @property
    def saved_percent(self) -> float:
        if not self.bytes_before:
            return 0.0
        return 100.0 * self.saved_bytes / self.bytes_before


def human_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(count)} {unit}"
            return f"{count:.1f} {unit}"
        count /= 1024.0
    return f"{count:.1f} GB"


def classify(path: Path) -> Kind:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return Kind.IMAGE
    if suffix in PDF_SUFFIXES:
        return Kind.PDF
    if suffix in TEXT_SUFFIXES:
        return Kind.TEXT
    return Kind.OTHER


def operations_for(paths: list[Path]) -> list[Operation]:
    """Operations that make sense for a whole dropped selection.

    Only offers an action when it applies to at least one file, so the
    island never shows a button that would do nothing.
    """
    kinds = {classify(p) for p in paths}
    out: list[Operation] = []
    if Kind.IMAGE in kinds or Kind.PDF in kinds:
        out.append(Operation.EXTRACT_TEXT)
        out.append(Operation.COMPRESS)
    if Kind.IMAGE in kinds:
        out.append(Operation.TO_WEBP)
    return out


def unique_output(source: Path, suffix_tag: str, extension: str) -> Path:
    """A writable path beside ``source`` that does not already exist."""
    directory = source.parent
    stem = source.stem
    candidate = directory / f"{stem}{suffix_tag}{extension}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}{suffix_tag}-{counter}{extension}"
        counter += 1
    return candidate


# ---- OCR -----------------------------------------------------------------

_ocr_engine = None
_ocr_failed = False


def ocr_available() -> bool:
    """True when the OCR dependency is importable, without loading models."""
    import importlib.util

    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


def _get_ocr_engine():
    """Load the OCR engine once. Returns None when unavailable.

    The first call pays for the ONNX runtime and the model files, which is
    why nothing here is imported at startup.
    """
    global _ocr_engine, _ocr_failed
    if _ocr_engine is not None or _ocr_failed:
        return _ocr_engine
    try:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_engine = RapidOCR()
    except Exception as exc:  # model download or runtime failure
        print(f"[media] OCR unavailable: {exc}")
        _ocr_failed = True
        return None
    return _ocr_engine


def _ocr_lines(raw) -> list[str]:
    """Normalise RapidOCR output across its versions.

    Older releases return ``[[box, text, score], ...]``; newer ones return
    an object exposing ``.txts``. Supporting both keeps the app working
    across a dependency bump instead of silently extracting nothing.
    """
    if raw is None:
        return []
    texts = getattr(raw, "txts", None)
    if texts:
        return [str(t) for t in texts if str(t).strip()]
    lines: list[str] = []
    try:
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = str(item[1]).strip()
                if text:
                    lines.append(text)
    except TypeError:
        return []
    return lines


def extract_text_from_image(path: Path) -> Result:
    started = time.perf_counter()
    engine = _get_ocr_engine()
    if engine is None:
        return Result(
            source=path,
            operation=Operation.EXTRACT_TEXT,
            ok=False,
            message="OCR is not installed (pip install -r requirements-ocr.txt)",
        )
    try:
        raw = engine(str(path))
        # RapidOCR returns (result, elapsed) on most versions.
        if isinstance(raw, tuple):
            raw = raw[0]
        lines = _ocr_lines(raw)
    except Exception as exc:
        return Result(
            source=path,
            operation=Operation.EXTRACT_TEXT,
            ok=False,
            message=f"OCR failed: {exc}",
            seconds=time.perf_counter() - started,
        )

    text = "\n".join(lines)
    elapsed = time.perf_counter() - started
    if not text:
        return Result(
            source=path,
            operation=Operation.EXTRACT_TEXT,
            ok=False,
            message="no text found in the image",
            seconds=elapsed,
        )
    word_count = len(text.split())
    return Result(
        source=path,
        operation=Operation.EXTRACT_TEXT,
        ok=True,
        message=f"{word_count} words in {elapsed:.1f}s",
        text=text,
        seconds=elapsed,
    )


def extract_text_from_pdf(path: Path) -> Result:
    started = time.perf_counter()
    try:
        from pypdf import PdfReader
    except ImportError:
        return Result(
            source=path,
            operation=Operation.EXTRACT_TEXT,
            ok=False,
            message="pypdf is not installed",
        )
    try:
        reader = PdfReader(str(path))
        chunks = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:
        return Result(
            source=path,
            operation=Operation.EXTRACT_TEXT,
            ok=False,
            message=f"could not read the PDF: {exc}",
        )
    text = "\n\n".join(c for c in chunks if c)
    elapsed = time.perf_counter() - started
    if not text:
        # A scanned PDF has no text layer. Say so plainly rather than
        # returning an empty success.
        return Result(
            source=path,
            operation=Operation.EXTRACT_TEXT,
            ok=False,
            message="no text layer (this looks like a scanned PDF)",
            seconds=elapsed,
        )
    return Result(
        source=path,
        operation=Operation.EXTRACT_TEXT,
        ok=True,
        message=f"{len(text.split())} words from {len(reader.pages)} pages",
        text=text,
        seconds=elapsed,
    )


def extract_text(path: Path) -> Result:
    kind = classify(path)
    if kind is Kind.PDF:
        return extract_text_from_pdf(path)
    if kind is Kind.IMAGE:
        return extract_text_from_image(path)
    if kind is Kind.TEXT:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return Result(path, Operation.EXTRACT_TEXT, False, str(exc))
        return Result(
            source=path,
            operation=Operation.EXTRACT_TEXT,
            ok=True,
            message=f"{len(text.split())} words",
            text=text,
        )
    return Result(
        source=path,
        operation=Operation.EXTRACT_TEXT,
        ok=False,
        message=f"cannot extract text from {path.suffix or 'this file'}",
    )


# ---- images --------------------------------------------------------------


def _open_image(path: Path):
    from PIL import Image

    return Image.open(path)


def compress_image(path: Path) -> Result:
    """Re-encode an image smaller, keeping its format."""
    started = time.perf_counter()
    try:
        from PIL import Image  # noqa: F401 - import check before work
    except ImportError:
        return Result(
            source=path,
            operation=Operation.COMPRESS,
            ok=False,
            message="Pillow is not installed",
        )

    before = path.stat().st_size
    extension = path.suffix.lower()
    destination = unique_output(path, ".min", extension)

    try:
        with _open_image(path) as image:
            params: dict[str, object] = {}
            icc = image.info.get("icc_profile")
            exif = image.info.get("exif")
            if icc:
                params["icc_profile"] = icc
            if exif:
                params["exif"] = exif

            if extension in (".jpg", ".jpeg"):
                converted = image.convert("RGB") if image.mode != "RGB" else image
                converted.save(
                    destination,
                    quality=JPEG_QUALITY,
                    optimize=True,
                    progressive=True,
                    **params,
                )
            elif extension == ".png":
                image.save(destination, optimize=True, compress_level=9, **params)
            elif extension == ".webp":
                image.save(
                    destination, quality=WEBP_QUALITY, method=6, **params
                )
            else:
                image.save(destination, optimize=True, **params)
    except Exception as exc:
        _discard(destination)
        return Result(
            source=path,
            operation=Operation.COMPRESS,
            ok=False,
            message=f"could not compress: {exc}",
        )

    after = destination.stat().st_size
    elapsed = time.perf_counter() - started

    if after >= before:
        # Already optimal. Writing a larger file and calling it a saving
        # would be worse than doing nothing.
        _discard(destination)
        return Result(
            source=path,
            operation=Operation.COMPRESS,
            ok=False,
            message=f"already optimal at {human_bytes(before)}",
            bytes_before=before,
            bytes_after=before,
            seconds=elapsed,
        )

    result = Result(
        source=path,
        operation=Operation.COMPRESS,
        ok=True,
        message="",
        output=destination,
        bytes_before=before,
        bytes_after=after,
        seconds=elapsed,
    )
    result.message = (
        f"{human_bytes(before)} to {human_bytes(after)}, "
        f"{result.saved_percent:.0f}% smaller"
    )
    return result


def convert_image(path: Path, target: str = "webp") -> Result:
    """Convert an image to another format beside the original."""
    started = time.perf_counter()
    operation = Operation.TO_WEBP if target == "webp" else Operation.TO_PNG
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return Result(path, operation, False, "Pillow is not installed")

    before = path.stat().st_size
    destination = unique_output(path, "", f".{target}")
    try:
        with _open_image(path) as image:
            params: dict[str, object] = {}
            icc = image.info.get("icc_profile")
            if icc:
                params["icc_profile"] = icc
            if target == "webp":
                # WebP keeps alpha, so RGBA does not need flattening.
                source = image
                if image.mode not in ("RGB", "RGBA"):
                    source = image.convert(
                        "RGBA" if "A" in image.getbands() else "RGB"
                    )
                source.save(
                    destination, quality=WEBP_QUALITY, method=6, **params
                )
            else:
                image.save(destination, optimize=True, **params)
    except Exception as exc:
        _discard(destination)
        return Result(path, operation, False, f"could not convert: {exc}")

    after = destination.stat().st_size
    result = Result(
        source=path,
        operation=operation,
        ok=True,
        message="",
        output=destination,
        bytes_before=before,
        bytes_after=after,
        seconds=time.perf_counter() - started,
    )
    if after < before:
        result.message = (
            f"{destination.suffix[1:]} at {human_bytes(after)}, "
            f"{result.saved_percent:.0f}% smaller"
        )
    else:
        result.message = (
            f"{destination.suffix[1:]} at {human_bytes(after)} "
            f"(was {human_bytes(before)})"
        )
    return result


# ---- pdf -----------------------------------------------------------------


def compress_pdf(path: Path) -> Result:
    started = time.perf_counter()
    try:
        from pypdf import PdfWriter
    except ImportError:
        return Result(path, Operation.COMPRESS, False, "pypdf is not installed")

    before = path.stat().st_size
    destination = unique_output(path, ".min", ".pdf")
    try:
        # The pages must already belong to the writer before their content
        # streams can be compressed; pypdf 7 raises "Page must be part of a
        # PdfWriter" if they are compressed while still owned by a reader.
        writer = PdfWriter(clone_from=str(path))
        for page in writer.pages:
            page.compress_content_streams()
        writer.compress_identical_objects()
        with destination.open("wb") as handle:
            writer.write(handle)
    except Exception as exc:
        _discard(destination)
        return Result(path, Operation.COMPRESS, False, f"could not compress: {exc}")

    after = destination.stat().st_size
    elapsed = time.perf_counter() - started
    if after >= before:
        _discard(destination)
        return Result(
            source=path,
            operation=Operation.COMPRESS,
            ok=False,
            message=f"already optimal at {human_bytes(before)}",
            bytes_before=before,
            bytes_after=before,
            seconds=elapsed,
        )
    result = Result(
        source=path,
        operation=Operation.COMPRESS,
        ok=True,
        message="",
        output=destination,
        bytes_before=before,
        bytes_after=after,
        seconds=elapsed,
    )
    result.message = (
        f"{human_bytes(before)} to {human_bytes(after)}, "
        f"{result.saved_percent:.0f}% smaller"
    )
    return result


def compress(path: Path) -> Result:
    kind = classify(path)
    if kind is Kind.IMAGE:
        return compress_image(path)
    if kind is Kind.PDF:
        return compress_pdf(path)
    return Result(
        source=path,
        operation=Operation.COMPRESS,
        ok=False,
        message=f"cannot compress {path.suffix or 'this file'}",
    )


def _discard(path: Path) -> None:
    """Remove a partial or pointless output. Never touches the input."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def apply(operation: Operation, path: Path) -> Result:
    """Dispatch one operation onto one file."""
    if operation is Operation.EXTRACT_TEXT:
        return extract_text(path)
    if operation is Operation.COMPRESS:
        return compress(path)
    if operation is Operation.TO_WEBP:
        if classify(path) is not Kind.IMAGE:
            return Result(path, operation, False, "not an image")
        return convert_image(path, "webp")
    if operation is Operation.TO_PNG:
        if classify(path) is not Kind.IMAGE:
            return Result(path, operation, False, "not an image")
        return convert_image(path, "png")
    return Result(path, operation, False, f"unknown operation {operation}")


LABELS = {
    Operation.EXTRACT_TEXT: "Extract text",
    Operation.COMPRESS: "Compress",
    Operation.TO_WEBP: "To WebP",
    Operation.TO_PNG: "To PNG",
}

GLYPHS = {
    Operation.EXTRACT_TEXT: "text",
    Operation.COMPRESS: "cube",
    Operation.TO_WEBP: "sparkle",
    Operation.TO_PNG: "sparkle",
}
