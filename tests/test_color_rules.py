from run_v560_storage import _processing_gate, _reader_rating_color


def _doc(*tags):
    return {"readwise_tags": {str(i): {"name": tag} for i, tag in enumerate(tags)}}


def test_color_processing_matrix():
    expected = {
        "white": (False, False),
        "red": (False, False),
        "orange": (True, False),
        "yellow": (True, False),
        "green": (True, False),
        "blue": (True, True),
        "navy": (True, True),
        "purple": (True, True),
        "gold": (True, True),
    }
    for color, (exportable, rateable) in expected.items():
        actual_color, actual_exportable, actual_rateable = _processing_gate(_doc(color))
        assert actual_color == color
        assert (actual_exportable, actual_rateable) == (exportable, rateable)


def test_orange_is_translated_but_not_rated():
    color, exportable, rateable = _processing_gate(_doc("orange"))
    assert color == "orange"
    assert exportable is True
    assert rateable is False


def test_white_and_red_are_neither_translated_nor_rated():
    for color in ("white", "red"):
        assert _processing_gate(_doc(color))[1:] == (False, False)


def test_conflicting_colors_are_quarantined():
    color, exportable, rateable = _processing_gate(_doc("blue", "gold"))
    assert color is None
    assert (exportable, rateable) == (False, False)
