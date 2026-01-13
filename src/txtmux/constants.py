"""Terminal emulator constants and default values."""

# VT100 standard terminal dimensions (80 columns x 24 rows)
# This has been the de facto standard since the DEC VT100 was introduced in 1978.
# Reference: https://en.wikipedia.org/wiki/VT100
DEFAULT_TERMINAL_WIDTH = 80
DEFAULT_TERMINAL_HEIGHT = 24

# Minimum dimensions for terminal operation
MIN_TERMINAL_WIDTH = 20
MIN_TERMINAL_HEIGHT = 10


def get_terminal_dimensions(width: int, height: int) -> tuple[int, int]:
    """
    Validate and return terminal dimensions, falling back to defaults if invalid.

    Args:
        width: Requested terminal width in columns
        height: Requested terminal height in rows

    Returns:
        Tuple of (width, height) using defaults if input is invalid
    """
    if width < MIN_TERMINAL_WIDTH:
        width = DEFAULT_TERMINAL_WIDTH
    if height < MIN_TERMINAL_HEIGHT:
        height = DEFAULT_TERMINAL_HEIGHT
    return (width, height)
