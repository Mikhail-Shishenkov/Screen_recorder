"""Pure geometry helpers for screen-region selection."""


def normalize_selection_rect(start_x, start_y, end_x, end_y):
    """Return a non-empty even-sized ``(x, y, width, height)`` rectangle."""
    x = min(start_x, end_x)
    y = min(start_y, end_y)
    width = abs(end_x - start_x)
    height = abs(end_y - start_y)

    if width % 2:
        width -= 1
    if height % 2:
        height -= 1

    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def rect_to_capture_bbox(rect):
    """Convert ``(x, y, width, height)`` into ``(left, top, right, bottom)``."""
    x, y, width, height = rect
    return x, y, x + width, y + height


def rect_to_capture_region(rect):
    """Return the region tuple expected by backends that use width/height."""
    return rect
