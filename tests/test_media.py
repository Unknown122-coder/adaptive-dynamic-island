"""Drop-to-Action file handling.

The two rules worth protecting with tests are that an input file is never
modified and that an output is never larger than its input. Everything
else about this feature can be wrong and merely annoying; those two being
wrong loses someone's file or quietly wastes their disk.

OCR itself is not exercised here: loading the model takes seconds, which
does not belong in a unit suite. scripts/check_media.py covers it against
real generated files.
"""

from __future__ import annotations

import pytest

from workers import media

PIL = pytest.importorskip("PIL", reason="Pillow is an optional feature pack")


def make_png(path, size=(400, 300), noisy=False):
    """A PNG on disk. Noise makes it resist compression."""
    import random

    from PIL import Image

    image = Image.new("RGB", size, (240, 240, 245))
    if noisy:
        rng = random.Random(3)
        pixels = image.load()
        for x in range(size[0]):
            for y in range(size[1]):
                pixels[x, y] = (
                    rng.randrange(256),
                    rng.randrange(256),
                    rng.randrange(256),
                )
    image.save(path)
    return path


def make_jpeg(path, size=(600, 400)):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, (30, 90, 170))
    draw = ImageDraw.Draw(image)
    for i in range(0, size[0], 7):
        draw.line([(i, 0), (size[0] - i, size[1])], fill=(220, 140 + i % 100, 60))
    image.save(path, "JPEG", quality=98)
    return path


# ---- classification ------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("a.png", media.Kind.IMAGE),
        ("a.JPG", media.Kind.IMAGE),
        ("a.webp", media.Kind.IMAGE),
        ("a.pdf", media.Kind.PDF),
        ("a.md", media.Kind.TEXT),
        ("a.zip", media.Kind.OTHER),
        ("a", media.Kind.OTHER),
    ],
)
def test_classify(tmp_path, name, expected):
    assert media.classify(tmp_path / name) is expected


def test_operations_offered_for_images(tmp_path):
    offered = media.operations_for([tmp_path / "a.png"])
    assert media.Operation.EXTRACT_TEXT in offered
    assert media.Operation.COMPRESS in offered
    assert media.Operation.TO_WEBP in offered


def test_pdf_is_not_offered_webp_conversion(tmp_path):
    offered = media.operations_for([tmp_path / "a.pdf"])
    assert media.Operation.TO_WEBP not in offered


def test_nothing_offered_for_unsupported(tmp_path):
    assert media.operations_for([tmp_path / "a.zip"]) == []


# ---- output naming -------------------------------------------------------


def test_output_goes_beside_the_source(tmp_path):
    source = tmp_path / "photo.jpg"
    source.touch()
    out = media.unique_output(source, ".min", ".jpg")
    assert out.parent == tmp_path
    assert out.name == "photo.min.jpg"


def test_output_never_collides(tmp_path):
    source = tmp_path / "photo.jpg"
    source.touch()
    (tmp_path / "photo.min.jpg").touch()
    (tmp_path / "photo.min-1.jpg").touch()
    assert media.unique_output(source, ".min", ".jpg").name == "photo.min-2.jpg"


def test_output_is_never_the_source(tmp_path):
    source = tmp_path / "photo.webp"
    source.touch()
    assert media.unique_output(source, "", ".webp") != source


# ---- compression ---------------------------------------------------------


def test_compressing_a_jpeg_shrinks_it(tmp_path):
    source = make_jpeg(tmp_path / "photo.jpg")
    before = source.stat().st_size
    result = media.compress(source)
    assert result.ok, result.message
    assert result.output is not None and result.output.exists()
    assert result.bytes_after < before
    assert 0 < result.saved_percent < 100


def test_compression_leaves_the_original_untouched(tmp_path):
    source = make_jpeg(tmp_path / "photo.jpg")
    before = source.read_bytes()
    media.compress(source)
    assert source.read_bytes() == before


def test_incompressible_image_is_reported_not_inflated(tmp_path):
    # Random noise cannot be compressed further; the naive implementation
    # writes a larger file and claims success.
    source = make_png(tmp_path / "noise.png", size=(200, 200), noisy=True)
    before = source.stat().st_size
    result = media.compress(source)
    if not result.ok:
        assert "optimal" in result.message
        assert list(tmp_path.glob("noise.min*.png")) == []
    else:
        assert result.bytes_after < before


def test_compress_refuses_unknown_types(tmp_path):
    junk = tmp_path / "archive.zip"
    junk.write_bytes(b"PK\x03\x04")
    result = media.compress(junk)
    assert not result.ok
    assert "cannot compress" in result.message


# ---- conversion ----------------------------------------------------------


def test_convert_to_webp(tmp_path):
    source = make_jpeg(tmp_path / "photo.jpg")
    result = media.convert_image(source, "webp")
    assert result.ok, result.message
    assert result.output.suffix == ".webp"
    assert result.output.exists()
    assert source.exists()


def test_repeated_conversion_does_not_overwrite(tmp_path):
    source = make_jpeg(tmp_path / "photo.jpg")
    first = media.convert_image(source, "webp").output
    second = media.convert_image(source, "webp").output
    assert first != second
    assert first.exists() and second.exists()


def test_convert_refuses_non_images(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert not media.apply(media.Operation.TO_WEBP, pdf).ok


# ---- text ----------------------------------------------------------------


def test_extract_text_from_a_text_file(tmp_path):
    source = tmp_path / "notes.md"
    source.write_text("hello island world", encoding="utf-8")
    result = media.extract_text(source)
    assert result.ok
    assert result.text == "hello island world"


def test_extract_text_refuses_unknown_types(tmp_path):
    junk = tmp_path / "a.zip"
    junk.write_bytes(b"PK")
    assert not media.extract_text(junk).ok


# ---- reporting -----------------------------------------------------------


@pytest.mark.parametrize(
    "count,expected",
    [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")],
)
def test_human_bytes(count, expected):
    assert media.human_bytes(count) == expected


def test_saved_percent_is_zero_without_measurements(tmp_path):
    result = media.Result(tmp_path / "x", media.Operation.COMPRESS, False, "")
    assert result.saved_percent == 0.0
    assert result.saved_bytes == 0
