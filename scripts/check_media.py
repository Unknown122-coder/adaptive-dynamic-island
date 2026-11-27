"""Exercise the Drop-to-Action pipeline against real generated files.

Creates its own fixtures rather than carrying binaries in the repository:
a PNG with rendered text, a photographic-looking JPEG, and a PDF with a
real text layer written through QPdfWriter. Then it runs every operation
and checks the output, including that nothing overwrote an input.

Run::

    .venv/Scripts/python.exe scripts/check_media.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QMarginsF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
)
from PySide6.QtWidgets import QApplication

from workers import media

SAMPLE_TEXT = "The quick brown fox jumps over the lazy dog"
PDF_TEXT = "Adaptive Dynamic Island drop to action test page"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  [{'ok  ' if condition else 'FAIL'}] {message}")
    if not condition:
        failures.append(message)


def make_text_png(path: Path) -> Path:
    """A clean, high-contrast image: what a screenshot of text looks like."""
    image = QImage(900, 260, QImage.Format.Format_RGB32)
    image.fill(QColor(255, 255, 255))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setPen(QColor(10, 10, 10))
    font = QFont("Arial", 34)
    font.setWeight(QFont.Weight.Medium)
    painter.setFont(font)
    painter.drawText(
        QRectF(30, 30, 840, 200),
        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        | int(Qt.TextFlag.TextWordWrap),
        SAMPLE_TEXT,
    )
    painter.end()
    image.save(str(path))
    return path


def make_photo_jpeg(path: Path) -> Path:
    """A gradient with noise, which compresses like a photograph does."""
    import random

    width, height = 1400, 900
    image = QImage(width, height, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, width, height)
    gradient.setColorAt(0.0, QColor(30, 90, 170))
    gradient.setColorAt(0.5, QColor(220, 140, 60))
    gradient.setColorAt(1.0, QColor(40, 160, 110))
    painter.fillRect(0, 0, width, height, gradient)
    rng = random.Random(7)
    for _ in range(14000):
        painter.setPen(
            QColor(rng.randrange(256), rng.randrange(256), rng.randrange(256), 70)
        )
        painter.drawPoint(rng.randrange(width), rng.randrange(height))
    painter.end()
    # Save at a high quality so there is real headroom to compress.
    image.save(str(path), "JPEG", 97)
    return path


def make_pdf(path: Path) -> Path:
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter)
    writer.setResolution(300)
    painter = QPainter(writer)
    font = QFont("Arial", 16)
    painter.setFont(font)
    for page in range(2):
        if page:
            writer.newPage()
        painter.drawText(
            QRectF(0, 0, writer.width(), 1200),
            int(Qt.TextFlag.TextWordWrap),
            f"{PDF_TEXT} (page {page + 1})",
        )
    painter.end()
    return path


def main() -> int:
    app = QApplication(sys.argv)
    _ = app

    with tempfile.TemporaryDirectory(prefix="island-media-") as raw_dir:
        work = Path(raw_dir)

        png = make_text_png(work / "screenshot.png")
        jpeg = make_photo_jpeg(work / "photo.jpg")
        pdf = make_pdf(work / "document.pdf")
        originals = {p: p.stat().st_size for p in (png, jpeg, pdf)}
        print("fixtures")
        for path, size in originals.items():
            print(f"  {path.name:<18}{media.human_bytes(size)}")

        print("\nclassification")
        check(media.classify(png) is media.Kind.IMAGE, "png is an image")
        check(media.classify(pdf) is media.Kind.PDF, "pdf is a pdf")
        check(
            media.classify(work / "x.zip") is media.Kind.OTHER,
            "zip is unsupported",
        )
        offered = media.operations_for([png, pdf])
        check(
            media.Operation.EXTRACT_TEXT in offered
            and media.Operation.TO_WEBP in offered,
            f"offers the right actions for a mixed drop ({len(offered)} actions)",
        )
        check(
            media.operations_for([work / "x.zip"]) == [],
            "offers nothing for an unsupported file",
        )

        print("\nOCR on a text image")
        result = media.extract_text(png)
        print(f"  -> {result.message}")
        if result.text:
            print(f"  -> {result.text.strip()[:90]!r}")
        check(result.ok, "OCR succeeded")
        if result.ok and result.text:
            words = SAMPLE_TEXT.lower().split()
            got = result.text.lower()
            hits = sum(1 for w in words if w in got)
            check(
                hits >= len(words) - 2,
                f"recognised {hits} of {len(words)} words",
            )

        print("\nPDF text extraction")
        pdf_result = media.extract_text(pdf)
        print(f"  -> {pdf_result.message}")
        check(pdf_result.ok, "PDF text extracted")
        if pdf_result.text:
            check(
                "adaptive" in pdf_result.text.lower(),
                "extracted text matches the page content",
            )
            check("page 2" in pdf_result.text.lower(), "both pages extracted")

        print("\nJPEG compression")
        compressed = media.compress(jpeg)
        print(f"  -> {compressed.message}")
        check(compressed.ok, "JPEG compressed")
        if compressed.ok and compressed.output:
            check(
                compressed.output.exists() and compressed.output != jpeg,
                "wrote a new file beside the original",
            )
            check(
                compressed.bytes_after < compressed.bytes_before,
                "output is smaller than the input",
            )

        print("\nWebP conversion")
        webp = media.convert_image(png, "webp")
        print(f"  -> {webp.message}")
        check(webp.ok, "converted to WebP")
        if webp.output:
            check(webp.output.suffix == ".webp", "output has a .webp extension")

        print("\nPDF compression")
        pdf_min = media.compress(pdf)
        print(f"  -> {pdf_min.message}")
        check(
            pdf_min.ok or "optimal" in pdf_min.message,
            "PDF compression either shrank the file or reported it optimal",
        )
        if not pdf_min.ok:
            check(
                not (work / "document.min.pdf").exists(),
                "a pointless compressed PDF was discarded, not left behind",
            )

        print("\nsafety")
        for path, size in originals.items():
            check(
                path.exists() and path.stat().st_size == size,
                f"{path.name} is untouched",
            )
        # Re-running must not clobber the first output.
        again = media.convert_image(png, "webp")
        check(
            again.output is not None and again.output != webp.output,
            "a second conversion picks a fresh filename",
        )

        print("\nunsupported input")
        junk = work / "archive.zip"
        junk.write_bytes(b"PK\x03\x04not really a zip")
        check(not media.compress(junk).ok, "refuses to compress an unknown type")
        check(not media.extract_text(junk).ok, "refuses to OCR an unknown type")

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nOK: drop-to-action pipeline verified on real files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
