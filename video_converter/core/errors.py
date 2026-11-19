"""The two exceptions an export raises."""

from __future__ import annotations

class ConversionError(Exception):
    pass


class ConversionCancelled(Exception):
    pass
