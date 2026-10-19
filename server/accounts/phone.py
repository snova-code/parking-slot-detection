# server/accounts/phone.py
"""Phone number normalisation to E.164.

`phonenumbers.parse` alone is not enough: it accepts "12345" and returns
+97712345, which is well-formed enough to pass the database's E.164 CHECK.
`is_valid_number` checks against the actual numbering plan.
"""
from __future__ import annotations

import phonenumbers
from django.conf import settings


class InvalidPhoneNumber(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    try:
        number = phonenumbers.parse(raw or "", settings.PHONE_DEFAULT_REGION)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumber("not a phone number") from exc
    if not phonenumbers.is_valid_number(number):
        raise InvalidPhoneNumber("not a valid phone number")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)