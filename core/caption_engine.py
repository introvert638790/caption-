"""
Caption transformation engine.

Pure logic, no Telegram API calls. Operates on caption text + entities
(as returned by the Bot API) and produces a new (text, entities) pair.

Critical detail: Telegram entity offsets are UTF-16 code units, not Python
string indices. All offset math here is done in UTF-16 space to avoid
corrupting entities when captions contain emoji or other non-BMP characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Entity types that represent a hyperlink on visible text.
LINK_ENTITY_TYPES = {"text_link", "url"}


@dataclass(frozen=True)
class CaptionEntity:
    """Mirrors Telegram's MessageEntity, restricted to fields we use."""

    type: str
    offset: int  # UTF-16 code units
    length: int  # UTF-16 code units
    url: str | None = None


@dataclass(frozen=True)
class TransformResult:
    text: str
    entities: list[CaptionEntity]
    links_removed: int
    words_replaced: int
    urls_removed: int = 0
    injected: bool = False
    quotes_removed: int = 0

    @property
    def changed(self) -> bool:
        return (
            self.links_removed > 0
            or self.words_replaced > 0
            or self.urls_removed > 0
            or self.injected
            or self.quotes_removed > 0
        )


def _to_utf16(text: str) -> list[bytes]:
    """Encode text as UTF-16 code units, returned as a list of 2-byte chunks."""
    raw = text.encode("utf-16-le")
    return [raw[i : i + 2] for i in range(0, len(raw), 2)]  # noqa: E203


def _utf16_units_to_str(units: list[bytes]) -> str:
    return b"".join(units).decode("utf-16-le")


def _boundary_pattern(word: str) -> re.Pattern[str]:
    """
    Builds a match pattern for `word` with boundary rules that adapt to the
    word's first/last character:

    - If a side's boundary character is a word character (\\w: letters,
      digits, underscore), use standard \\b there. This preserves whole-word
      behavior for normal words -- "cat" won't match inside "concatenate".
    - If a side's boundary character is NOT a word character (e.g. "@" in
      "@username", an emoji, "#" in a hashtag, or a symbol like "★"), \\b
      can never be satisfied there, so no boundary constraint is applied on
      that side -- the literal search text is matched as-is.
    """
    escaped = re.escape(word)
    first_char = word[0]
    last_char = word[-1]

    left = r"\b" if re.match(r"\w", first_char, re.UNICODE) else ""
    right = r"\b" if re.match(r"\w", last_char, re.UNICODE) else ""

    return re.compile(left + escaped + right, re.IGNORECASE)


def _find_whole_word_matches_utf16(units: list[bytes], word: str) -> list[tuple[int, int]]:
    """
    Find whole-word (or whole-token, for symbol/emoji/@-led words),
    case-insensitive matches of `word` in the UTF-16 unit sequence. Returns
    list of (offset, length) in UTF-16 units.
    """
    text = _utf16_units_to_str(units)
    pattern = _boundary_pattern(word)

    matches: list[tuple[int, int]] = []
    for m in pattern.finditer(text):
        prefix_units = len(text[: m.start()].encode("utf-16-le")) // 2
        match_units = len(m.group(0).encode("utf-16-le")) // 2
        matches.append((prefix_units, match_units))
    return matches


def _entity_overlaps(entity: CaptionEntity, start: int, end: int) -> bool:
    """True if [start, end) overlaps entity's [offset, offset+length)."""
    e_start, e_end = entity.offset, entity.offset + entity.length
    return e_start < end and start < e_end


# Matches plain-text URLs of the supported forms only (scoped intentionally
# narrow to avoid false positives on ordinary text containing dots, e.g.
# "Node.js" or "file.txt"): http://, https://, www., t.me/, telegram.me/
# (the last two also matched bare, without a leading scheme). Generic bare
# domains (example.com, google.in) are explicitly out of scope.
URL_PATTERN = re.compile(
    r"(?:https?://\S+)"
    r"|(?:www\.\S+)"
    r"|(?:(?<![\w./])t\.me/\S+)"
    r"|(?:(?<![\w./])telegram\.me/\S+)",
    re.IGNORECASE,
)


def _find_url_matches_utf16(units: list[bytes]) -> list[tuple[int, int]]:
    """
    Find all direct-URL matches in the UTF-16 unit sequence. Returns list
    of (offset, length) in UTF-16 units, same convention as
    `_find_whole_word_matches_utf16`.

    Trailing punctuation commonly attached to a URL in prose (., ,, ), !,
    ?) is trimmed from the match so removing the URL doesn't eat the
    sentence's closing punctuation.
    """
    text = _utf16_units_to_str(units)
    matches: list[tuple[int, int]] = []
    for m in URL_PATTERN.finditer(text):
        match_text = m.group(0)
        end = m.end()
        while match_text and match_text[-1] in ".,)!?":
            match_text = match_text[:-1]
            end -= 1
        if not match_text:
            continue
        prefix_units = len(text[: m.start()].encode("utf-16-le")) // 2
        match_units = len(text[m.start() : end].encode("utf-16-le")) // 2  # noqa: E203
        matches.append((prefix_units, match_units))
    return matches


def remove_direct_urls(
    text: str, entities: list[CaptionEntity]
) -> tuple[str, list[CaptionEntity], int]:
    """
    Remove plain-text URLs (http://, https://, www., t.me/, telegram.me/)
    from visible caption text. Any Telegram entity overlapping a removed
    URL span is also dropped, so the resulting caption + entities remain
    valid for editMessageCaption (an entity pointing past deleted text
    would otherwise cause a Telegram API "can't parse entities" error) --
    one generic overlap-based rule, same mechanism already used by
    replace_word for link entities overlapping a replaced word.

    Whitespace immediately surrounding a removed URL is collapsed to a
    single space (or removed entirely at line start/end) to avoid leaving
    stray double-spaces -- localized strictly to the removal spot, so it
    cannot alter whitespace anywhere else in the caption.
    """
    units = _to_utf16(text)
    matches = _find_url_matches_utf16(units)
    if not matches:
        return text, entities, 0

    matches_sorted = sorted(matches, key=lambda m: m[0], reverse=True)

    working_units = list(units)
    working_entities = list(entities)
    removed_count = 0

    for match_start, match_len in matches_sorted:
        match_end = match_start + match_len

        survivors: list[CaptionEntity] = []
        for ent in working_entities:
            if _entity_overlaps(ent, match_start, match_end):
                continue
            survivors.append(ent)
        working_entities = survivors

        del working_units[match_start:match_end]

        # Collapse whitespace strictly at the spot the URL was removed from
        # (not the whole caption): if a space/tab now sits directly on both
        # sides of the deletion point, collapse that adjacent run down to a
        # single space; if the deletion point is now at the start/end of its
        # line, trim the adjacent run instead of leaving a leading/trailing
        # space. This is local to match_start only -- it cannot touch
        # whitespace anywhere else in the caption (e.g. inside a Find &
        # Replace block elsewhere in the text).
        collapse_start = match_start
        while collapse_start > 0 and working_units[collapse_start - 1] in (b" ", b"\t"):
            collapse_start -= 1
        collapse_end = match_start
        while collapse_end < len(working_units) and working_units[collapse_end] in (b" ", b"\t"):
            collapse_end += 1

        at_line_start = collapse_start == 0 or working_units[collapse_start - 1] == b"\n"
        at_line_end = collapse_end == len(working_units) or working_units[collapse_end] == b"\n"

        if collapse_end > collapse_start:
            if at_line_start or at_line_end:
                replacement_ws: list[bytes] = []
            else:
                replacement_ws = [" ".encode("utf-16-le")]
            removed_ws_len = collapse_end - collapse_start
            working_units[collapse_start:collapse_end] = replacement_ws
            ws_delta = len(replacement_ws) - removed_ws_len

            reshifted: list[CaptionEntity] = []
            for ent in working_entities:
                if ent.offset >= collapse_end:
                    reshifted.append(
                        CaptionEntity(
                            type=ent.type,
                            offset=ent.offset + ws_delta,
                            length=ent.length,
                            url=ent.url,
                        )
                    )
                else:
                    reshifted.append(ent)
            working_entities = reshifted

            length_delta = -match_len + ws_delta
        else:
            length_delta = -match_len

        shifted_entities: list[CaptionEntity] = []
        for ent in working_entities:
            if ent.offset >= match_end:
                shifted_entities.append(
                    CaptionEntity(
                        type=ent.type,
                        offset=ent.offset + length_delta,
                        length=ent.length,
                        url=ent.url,
                    )
                )
            else:
                shifted_entities.append(ent)
        working_entities = shifted_entities

        removed_count += 1

    new_text = _utf16_units_to_str(working_units)

    return new_text, working_entities, removed_count


def remove_all_links(
    text: str, entities: list[CaptionEntity]
) -> tuple[list[CaptionEntity], int]:
    """
    Strip link entities (text_link / url) while keeping visible text unchanged.
    """
    kept: list[CaptionEntity] = []
    removed = 0
    for ent in entities:
        if ent.type in LINK_ENTITY_TYPES:
            removed += 1
            continue
        kept.append(ent)
    return kept, removed


QUOTE_ENTITY_TYPES = {"blockquote", "expandable_blockquote"}


def remove_quotes(
    text: str, entities: list[CaptionEntity]
) -> tuple[str, list[CaptionEntity], int]:
    """
    Remove Telegram quote FORMATTING (both `blockquote` and
    `expandable_blockquote` entity types -- covering both a normal/expanded
    quote and a collapsed/expandable quote) while preserving the
    underlying caption text exactly as-is.

    This strips only the quote entity itself -- text, spacing, line
    breaks, and every other entity (including any nested inside the quote,
    e.g. a mention/bold/text_link) are left completely untouched. Since no
    text is removed, no offset shifting is needed for anything else.
    """
    removed_count = 0
    kept: list[CaptionEntity] = []
    for ent in entities:
        if ent.type in QUOTE_ENTITY_TYPES:
            removed_count += 1
            continue
        kept.append(ent)
    return text, kept, removed_count




def replace_word(
    text: str,
    entities: list[CaptionEntity],
    find_word: str,
    replace_word_with: str,
    replace_word_entities: list[CaptionEntity] | None = None,
) -> tuple[str, list[CaptionEntity], int]:
    """
    Whole-word (or whole-token), case-insensitive replace of every
    occurrence of `find_word` with `replace_word_with`.

    Every entity (of any type -- mention, text_link, url, bold, italic,
    blockquote, etc.) is classified against each match: fully before the
    match is left unchanged; fully inside the match is dropped (its
    underlying text is gone); fully after the match is offset-shifted by
    the replacement's length delta; and an entity that partially overlaps
    the match boundary is clipped to its surviving portion(s) outside the
    match, with offsets recalculated accordingly. This keeps every
    resulting entity's offset/length valid against the post-replacement
    text, regardless of entity type.

    If `replace_word_entities` is provided, those entities (offsets
    relative to the start of `replace_word_with` itself) are re-anchored to
    each match position and inserted on top of the above. This preserves
    formatting -- most commonly a hyperlink -- that the user applied to
    only part of the replacement text (e.g. only "ALEX" linked in "ALEX is
    King", or only "JOIN ME" linked in "JOIN ME on insta").
    """
    units = _to_utf16(text)
    matches = _find_whole_word_matches_utf16(units, find_word)
    if not matches:
        return text, entities, 0

    replace_units = _to_utf16(replace_word_with)
    replace_word_entities = replace_word_entities or []

    matches_sorted = sorted(matches, key=lambda m: m[0], reverse=True)

    working_units = list(units)
    working_entities = list(entities)
    replaced_count = 0

    for match_start, match_len in matches_sorted:
        match_end = match_start + match_len
        length_delta = len(replace_units) - match_len

        # Entity-aware classification against this match, applied uniformly
        # to every entity type (no type-based skip) -- fully-before entities
        # are untouched, fully-contained entities are dropped, fully-after
        # entities are shifted by length_delta, and entities that partially
        # overlap the match boundary are clipped to their surviving portion
        # (with offsets recalculated) rather than being dropped outright or
        # left stale. This keeps every entity's offset/length valid against
        # the post-replacement text.
        next_entities: list[CaptionEntity] = []
        for ent in working_entities:
            ent_start = ent.offset
            ent_end = ent.offset + ent.length

            if ent_end <= match_start:
                # Fully-before: unaffected by this match.
                next_entities.append(ent)
                continue

            if ent_start >= match_end:
                # Fully-after: shift by the length delta introduced by this
                # match's replacement.
                next_entities.append(
                    CaptionEntity(
                        type=ent.type,
                        offset=ent_start + length_delta,
                        length=ent.length,
                        url=ent.url,
                    )
                )
                continue

            if ent_start >= match_start and ent_end <= match_end:
                # Fully-contained inside the matched span: the span itself
                # is being replaced, so this entity's underlying text is
                # gone -- drop it.
                continue

            # Partial-overlap: entity crosses the match boundary on one or
            # both sides. Clip it to whichever portion(s) fall outside the
            # matched span, then account for the replacement's length delta
            # like a fully-after entity would need if any surviving portion
            # is on the right side.
            left_part_len = max(0, match_start - ent_start)
            right_part_start = max(ent_start, match_end)
            right_part_len = max(0, ent_end - right_part_start)

            if left_part_len > 0:
                # Surviving portion is the slice before match_start; offset
                # unchanged since it's entirely before the match.
                next_entities.append(
                    CaptionEntity(
                        type=ent.type,
                        offset=ent_start,
                        length=left_part_len,
                        url=ent.url,
                    )
                )
            if right_part_len > 0:
                # Surviving portion is the slice after match_end; shift by
                # length_delta same as a fully-after entity.
                next_entities.append(
                    CaptionEntity(
                        type=ent.type,
                        offset=right_part_start + length_delta,
                        length=right_part_len,
                        url=ent.url,
                    )
                )
            # If both parts are zero-length (shouldn't happen given the
            # containment check above already handled full containment),
            # the entity is effectively dropped.

        working_entities = next_entities

        working_units[match_start:match_end] = list(replace_units)

        # Re-anchor any entities carried on the replacement text itself
        # (e.g. a hyperlink on just part of it) to this match's position.
        for rep_ent in replace_word_entities:
            working_entities.append(
                CaptionEntity(
                    type=rep_ent.type,
                    offset=match_start + rep_ent.offset,
                    length=rep_ent.length,
                    url=rep_ent.url,
                )
            )

        replaced_count += 1

    new_text = _utf16_units_to_str(working_units)
    return new_text, working_entities, replaced_count


def inject_text(
    text: str,
    entities: list[CaptionEntity],
    inject_text_value: str,
    inject_text_entities: list[CaptionEntity] | None = None,
) -> tuple[str, list[CaptionEntity]]:
    """
    Append `inject_text_value` to the bottom of `text`. Existing entity
    offsets are unaffected since text is appended after them -- no shift
    needed. Always the final step of the transform pipeline.

    `inject_text_entities` (offsets relative to the start of
    inject_text_value itself) are re-anchored to the injected text's actual
    position in the final caption and appended alongside the existing
    entities, preserving any formatting -- most commonly a hyperlink on
    only part of the injected text -- that the user applied when the text
    was set.
    """
    if not inject_text_value:
        return text, entities

    separator = "\n\n"
    prefix_units = len(_to_utf16(text + separator))
    new_text = text + separator + inject_text_value

    new_entities = list(entities)
    for ent in inject_text_entities or []:
        new_entities.append(
            CaptionEntity(
                type=ent.type,
                offset=prefix_units + ent.offset,
                length=ent.length,
                url=ent.url,
            )
        )

    return new_text, new_entities


def transform_caption(
    text: str,
    entities: list[CaptionEntity],
    find_word: str,
    replace_word_with: str,
    remove_links: bool,
    inject_text_value: str | None = None,
    remove_urls: bool = False,
    replace_word_entities: list[CaptionEntity] | None = None,
    inject_text_entities: list[CaptionEntity] | None = None,
    remove_quotes_enabled: bool = False,
) -> TransformResult:
    """
    Apply the full configured transformation pipeline to a single caption,
    in order:
    1. Remove Direct URLs (if enabled) -- strips plain-text URLs and any
       overlapping entity.
    2. Remove Hyperlinks (if enabled) -- strips all link entities globally.
    3. Quote Removal (if enabled) -- strips blockquote/
       expandable_blockquote FORMATTING only; the underlying text is
       always preserved unchanged.
    4. Find & Replace (if enabled) -- whole-word replace; any remaining
       link entity overlapping a replaced word is stripped too (safe
       regardless of whether earlier steps already ran). If
       `replace_word_entities` is provided, those entities (e.g. a
       hyperlink on part of the replacement text) are preserved instead of
       being dropped.
    5. Caption Injector (if inject_text_value provided) -- appended at the
       bottom. Off by default (None/empty leaves existing behavior
       completely unchanged). If `inject_text_entities` is provided, that
       formatting is preserved.
    """
    working_text = text
    working_entities = entities
    words_replaced = 0
    links_removed = 0
    urls_removed = 0
    quotes_removed = 0

    if remove_urls:
        working_text, working_entities, urls_removed = remove_direct_urls(working_text, working_entities)

    if remove_links:
        working_entities, links_removed = remove_all_links(working_text, working_entities)

    if remove_quotes_enabled:
        working_text, working_entities, quotes_removed = remove_quotes(working_text, working_entities)

    if find_word:
        working_text, working_entities, words_replaced = replace_word(
            working_text, working_entities, find_word, replace_word_with, replace_word_entities
        )

    injected = False
    if inject_text_value:
        working_text, working_entities = inject_text(
            working_text, working_entities, inject_text_value, inject_text_entities
        )
        injected = True

    return TransformResult(
        text=working_text,
        entities=working_entities,
        links_removed=links_removed,
        words_replaced=words_replaced,
        urls_removed=urls_removed,
        injected=injected,
        quotes_removed=quotes_removed,
    )


def is_skippable(text: str | None) -> bool:
    """
    Cheap pre-check: True if caption is empty/missing.
    """
    return not text
