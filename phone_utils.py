"""Phone number normalization helpers."""

from __future__ import annotations

import re
from typing import Optional

DEFAULT_COUNTRY_CODE = "91"


def normalize_e164_phone(phone: str, country_code: str = DEFAULT_COUNTRY_CODE) -> Optional[str]:
    """Normalize Indian mobile numbers to E.164 (+91XXXXXXXXXX).

    Accepts: 9876543210, 919876543210, +919876543210, 09876543210, +91 98765 43210
    """
    if not phone:
        return None
    raw = str(phone).strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None

    while digits.startswith("0") and len(digits) > 10:
        digits = digits[1:]

    if raw.startswith("+"):
        return "+" + digits if len(digits) >= 12 else None

    if len(digits) == 10:
        digits = country_code + digits

    if len(digits) == 12 and digits.startswith(country_code):
        return "+" + digits

    return None
