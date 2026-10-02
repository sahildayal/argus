import io

import pytest
from PIL import Image

from argus.imaging import Grid, column_index, column_name, fit, render


def test_fit_keeps_1080p_at_full_size_and_shrinks_4k():
    assert fit(1920, 1080, 2000) == (1920, 1080, 1.0)
    w, h, s = fit(3840, 2160, 2000)
    assert (w, h) == (2000, 1125) and s == pytest.approx(2000 / 3840)
    assert fit(1080, 1920, 2000)[:2] == (1080, 1920)  # portrait


def test_column_names_round_trip():
    for i in (0, 1, 25, 26, 27, 51, 52, 700):
        assert column_index(column_name(i)) == i
    assert [column_name(i) for i in (0, 25, 26)] == ["A", "Z", "AA"]


def test_grid_cells_and_ranges():
    g = Grid(cell=100.0, cols=12, rows=7)
    assert g.cells("A1") == (0, 0, 100, 100)
    assert g.cells("d7") == (300, 600, 400, 700)
    assert g.cells("C3:E5") == (200, 200, 500, 500)
    assert g.cells("E5:C3") == (200, 200, 500, 500)
    assert g.cell_at(250, 610) == "C7"
    with pytest.raises(ValueError, match="outside the grid"):
        g.cells("M1")
    with pytest.raises(ValueError):
        g.cells("7D")


def test_render_downscales_draws_grid_and_encodes_png():
    img = Image.new("RGB", (3840, 2160), "white")
    view, drawn = render(img, max_side=2000, grid=True, cursor=(1920, 1080))
    assert (view.width, view.height) == (2000, 1125)
    assert view.grid is not None and view.grid.cols * view.grid.cell >= 2000
    assert view.mime == "png"
    assert Image.open(io.BytesIO(view.data)).size == (2000, 1125)
    # the mouse ring is magenta-ish at the scaled position
    r, g, b = drawn.getpixel((round(1920 * view.scale) + 14, round(1080 * view.scale)))
    assert r > 200 and b > 150 and g < 100


def test_render_enlarges_small_crops():
    view, _ = render(Image.new("RGB", (200, 100)), max_side=2000, upscale_to=1200)
    assert view.scale == pytest.approx(3.0) and (view.width, view.height) == (600, 300)


def test_noisy_images_fall_back_to_jpeg():
    import random

    rnd = random.Random(1)
    img = Image.frombytes("RGB", (1200, 900), bytes(rnd.getrandbits(8) for _ in range(1200 * 900 * 3)))
    view, _ = render(img, max_side=2000)
    assert view.mime == "jpeg"
