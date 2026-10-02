from PIL import Image, ImageDraw

from argus.geometry import Rect
from argus.testing import diff


def base():
    img = Image.new("RGB", (800, 600), (250, 250, 250))
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, 300, 120], fill=(30, 30, 30))
    return img


def test_identical_images_have_no_change():
    result = diff(base(), base())
    assert result.changed == 0 and result.regions == []


def test_one_changed_area_is_boxed_tightly():
    after = base()
    ImageDraw.Draw(after).rectangle([500, 300, 619, 379], fill=(220, 0, 0))
    result = diff(base(), after)
    assert len(result.regions) == 1
    r = result.regions[0]
    assert r.x <= 500 and r.y <= 300 and r.right >= 620 and r.bottom >= 380
    assert r.x >= 492 and r.y >= 292 and r.right <= 628 and r.bottom <= 388  # within one 8px block
    assert 0.019 < result.changed < 0.021


def test_separate_changes_are_separate_regions_biggest_first():
    after = base()
    d = ImageDraw.Draw(after)
    d.rectangle([600, 20, 700, 60], fill=(0, 0, 255))
    d.rectangle([20, 500, 400, 580], fill=(0, 160, 0))
    result = diff(base(), after)
    assert len(result.regions) == 2
    assert result.regions[0].area > result.regions[1].area
    assert result.regions[0].y >= 490


def test_small_noise_under_threshold_is_ignored():
    after = base().point(lambda p: min(255, p + 10))
    assert diff(base(), after).changed == 0


def test_ignore_regions():
    after = base()
    ImageDraw.Draw(after).rectangle([700, 560, 790, 590], fill=(0, 0, 0))  # e.g. a clock
    assert diff(base(), after, ignore=[Rect(690, 550, 110, 50)]).changed == 0


def test_size_mismatch_is_noted_and_compared():
    after = base().resize((400, 300))
    result = diff(base(), after)
    assert result.note and "Sizes differ" in result.note


def test_words_close_together_merge_into_one_region():
    after = base()
    d = ImageDraw.Draw(after)
    for x in range(100, 400, 30):  # a "line of text": blobs 10px apart
        d.rectangle([x, 300, x + 20, 312], fill=(0, 0, 0))
    assert len(diff(base(), after).regions) == 1
