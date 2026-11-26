"""Small, deterministic speech-length heuristics shared by podcast gates.

The result is a listening-load estimate in Chinese-character-equivalent units,
not a syllable count or a claim about how any speech engine will pronounce the
text.  Chinese and non-English input keeps the existing non-whitespace
character count.  A normal English word is one word at 280 Chinese chars per
minute versus 150 English words per minute; all-uppercase abbreviations with
at least two letters are counted letter by letter instead.
"""

from __future__ import annotations

import re


ENGLISH_WORD_UNITS = 280 / 150
_ENGLISH_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)*")


def spoken_sentence_units(text: str) -> float:
    """Estimate spoken sentence load without discarding punctuation.

    This deliberately remains a heuristic: it does not estimate syllables,
    pronunciation, or actual listening time.  Whitespace is ignored as in the
    legacy ``len(nonwhitespace)`` gate.  English letters in a normal word are
    replaced by :data:`ENGLISH_WORD_UNITS`; apostrophes inside words, digits,
    punctuation, and every other non-whitespace character retain one unit.
    ``ML4`` therefore contributes two abbreviation letters plus one digit,
    while ``GLM-5.3`` retains its hyphen, digits, and decimal point.
    """

    units = 0.0
    cursor = 0

    def count_unmatched(value: str) -> float:
        return float(sum(not character.isspace() for character in value))

    for match in _ENGLISH_WORD.finditer(text):
        units += count_unmatched(text[cursor:match.start()])
        token = match.group(0)
        letters = token.replace("'", "")
        if len(letters) >= 2 and letters.isupper():
            units += len(letters)
        else:
            units += ENGLISH_WORD_UNITS
        # The regex groups don't split this into two words, but the apostrophe is still a
        # spoken punctuation character and must not disappear from the count.
        units += token.count("'")
        cursor = match.end()

    units += count_unmatched(text[cursor:])
    return units
