from __future__ import annotations

import re
import unicodedata

PRINTED_IDENTIFIER = re.compile(
    r'^\s*(?P<prefix>[A-Za-z]|[IVXivx]{1,4}|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ])?\s*'
    r'(?P<number>[0-9]{1,4})\s*(?:\(\s*(?P<sub>[0-9A-Za-zㄱ-ㆎ가-힣]+)\s*\))?\s*$'
)


def normalize_parent_number(value: str | int) -> str:
    text = re.sub(r'\s+', '', str(value).strip()).casefold()
    text = re.sub(r'^(?:문항|문제)', '', text)
    text = re.sub(r'번$', '', text).strip('()[]{}.（）［］｛｝．')
    if not text:
        raise ValueError('printed number is empty')
    digits: list[int] = []
    for character in text:
        name = unicodedata.name(character, '')
        numeric = unicodedata.numeric(character, None)
        if (numeric is None or not numeric.is_integer()
                or not character.isdecimal() and not any(
                    marker in name for marker in ('CIRCLED', 'PARENTHESIZED', 'FULL STOP'))):
            break
        digits.append(int(numeric))
    else:
        if len(digits) == 1:
            return str(digits[0])
        if digits and all(digit < 10 for digit in digits):
            return str(int(''.join(str(digit) for digit in digits)))
    numeric = unicodedata.numeric(text, None) if len(text) == 1 else None
    if len(text) == 1 and ((numeric is not None and not numeric.is_integer())
                         or numeric is None and unicodedata.category(text) == 'So'):
        return text
    return unicodedata.normalize('NFKC', text).casefold()
