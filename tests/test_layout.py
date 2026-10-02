from argus.geometry import Rect
from argus.layout import arrange


def labels(rects, primary=0):
    order, labs = arrange(rects, primary)
    return [labs[i] for i in order]


def test_single_monitor_has_no_position():
    assert arrange([Rect(0, 0, 1920, 1080)]) == ([0], [""])


def test_laptop_plus_one_external_side_by_side():
    rects = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 2560, 1440)]
    assert labels(rects) == ["left", "right"]


def test_three_externals_with_laptop_below_center():
    # Primary is the laptop, enumerated first; externals sit in a row above it.
    rects = [Rect(2560, 1440, 1920, 1080), Rect(0, 0, 2560, 1440), Rect(2560, 0, 2560, 1440), Rect(5120, 0, 2560, 1440)]
    order, labs = arrange(rects, primary=0)
    assert order == [1, 2, 3, 0]  # main row left to right, then the laptop
    assert [labs[i] for i in order] == ["left", "center", "right", "bottom"]


def test_monitor_left_of_primary_has_negative_coordinates():
    rects = [Rect(0, 0, 1920, 1080), Rect(-2560, -200, 2560, 1440)]
    order, labs = arrange(rects, primary=0)
    assert order == [1, 0]
    assert labs[1] == "left" and labs[0] == "right"


def test_portrait_monitor_stays_in_the_row():
    rects = [Rect(0, 0, 2560, 1440), Rect(2560, -300, 1440, 2560), Rect(-1920, 200, 1920, 1080)]
    assert labels(rects) == ["left", "center", "right"]


def test_two_by_two_grid_gets_row_prefixes():
    rects = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080), Rect(0, 1080, 1920, 1080), Rect(1920, 1080, 1920, 1080)]
    assert sorted(labels(rects)) == ["bottom-left", "bottom-right", "top-left", "top-right"]


def test_external_above_laptop():
    rects = [Rect(0, 0, 1920, 1080), Rect(-320, -1440, 2560, 1440)]
    order, labs = arrange(rects, primary=0)
    assert {labs[0], labs[1]} == {"top", "bottom"}
    assert labs[1] == "top"


def test_four_in_a_row():
    rects = [Rect(i * 1920, 0, 1920, 1080) for i in range(4)]
    assert labels(rects) == ["left", "center-left", "center-right", "right"]


def test_five_in_a_row_uses_ordinals():
    rects = [Rect(i * 1920, 0, 1920, 1080) for i in range(5)]
    assert labels(rects) == ["left", "2nd from left", "3rd from left", "4th from left", "right"]
