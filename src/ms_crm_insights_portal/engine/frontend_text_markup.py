"""Markdown-style emphasis for BI frontends.

- **Title:** stored plain — no ``*`` characters (the UI already styles the title).
- **Body / summaries:** Unicode ``•`` bullets; strip stray ``*`` after bullets and malformed
  ``**``; re-wrap bare numeric tokens; optionally wrap **dimension** phrases from row context.
"""

from __future__ import annotations

import re

# BOM / zero-width chars (often from JSON or LLM output) break dimension matching:
# ``find("market type")`` can start at 1 after U+FEFF, producing ``**arket Type**``.
_ZW_AND_BOM = re.compile(r"[\ufeff\u200b\u200c\u200d\u2060]")


def strip_zero_width_and_bom(text: str | None) -> str:
    """Remove BOM and zero-width characters that confuse ``**`` / phrase matching."""
    if text is None:
        return ""
    return _ZW_AND_BOM.sub("", str(text))


# Non-greedy span between paired double asterisks (no ``**`` nested inside).
_ALREADY_BOLD = re.compile(r"\*\*[^*]+?\*\*", re.DOTALL)
_BOLD_PAIR_CAPTURE = re.compile(r"\*\*([^*]+?)\*\*", re.DOTALL)

# Model glitch: ``**M**arket`` — paired ``**`` around a single letter before the rest
# of the word. ``_ALREADY_BOLD`` then treats ``**M**`` as one span and leaves ``arket``.
_SINGLE_LETTER_BOLD_PREFIX = re.compile(r"\*\*([A-Za-z])\*\*(?=[a-z])")


def repair_broken_bold_fragments(text: str | None) -> str:
    """Fix ``**M**arket``-style splits and stray backslash-asterisk noise from models/UI."""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n")
    if not s:
        return s
    while True:
        n = _SINGLE_LETTER_BOLD_PREFIX.sub(r"\1", s)
        if n == s:
            break
        s = n
    s = re.sub(r"\\+\*{1,2}", "", s)
    return s


def strip_title_markup(text: str | None) -> str:
    """Remove every asterisk from the title — frontend applies its own emphasis."""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n")
    s = s.replace("*", "")
    return re.sub(r"\s{2,}", " ", s).strip()


def _unwrap_or_strip_double_stars(text: str) -> str:
    """Remove ``**…**`` pairs and any leftover ``**`` (fixes `word**`, trailing `**`)."""
    s = text
    prev: str | None = None
    while prev != s:
        prev = s
        s = _BOLD_PAIR_CAPTURE.sub(r"\1", s)
    return s.replace("**", "")


def normalize_body_bullets_and_markdown(text: str | None) -> str:
    """Use ``•`` for list lines; strip ``• *`` artifacts; strip broken ``**`` for a clean bold pass."""
    if text is None:
        return ""
    s = strip_zero_width_and_bom(str(text)).replace("\r\n", "\n")
    if not s.strip():
        return s
    # Line-start ``*`` (not ``**``) → bullet
    s = re.sub(r"(?m)^\*(?!\*)\s*", "• ", s)
    # ``•`` followed by stray ``*`` (not ``**``) — common LLM mistake: "• *Average..."
    s = re.sub(r"•\s*\*(?!\*)", "• ", s)
    s = re.sub(r"(?m)^(•\s*)\*(?!\*)", r"\1", s)
    return _unwrap_or_strip_double_stars(s)


def apply_frontend_bold_markup(text: str | None) -> str:
    """Wrap bare numeric/date/currency tokens in ``**…**``; preserve existing pairs."""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\u2212", "-")
    if not s.strip():
        return s
    s = repair_broken_bold_fragments(s)
    s = re.sub(r"(?m)^\*(?!\*)\s*", "• ", s)
    s = re.sub(r"•\s*\*(?!\*)", "• ", s)

    def _bold_plain(seg: str) -> str:
        if not seg:
            return seg
        seg = re.sub(
            r"(?<!\*)(\b\d{4}-\d{2}-\d{2}\b)(?!\*)",
            r"**\1**",
            seg,
        )
        # Percentage points (e.g. "22 pp", "11.5 pp") before generic % pass
        seg = re.sub(
            r"(?<!\*)(?<![\d.])([+-]?\d+(?:\.\d+)?)\s*(pp)\b",
            r"**\1 \2**",
            seg,
            flags=re.IGNORECASE,
        )
        # Percentages: allow leading + / - (e.g. "+80%", "-20%"). No (?<![\d.]): keep
        # "0.15%"-style tokens matching the full token, not a suffix after another dot.
        seg = re.sub(
            r"(?<!\*)([+-]?\d+(?:,\d{3})*(?:\.\d+)?%)(?!\*)",
            r"**\1**",
            seg,
        )
        seg = re.sub(
            r"(?<!\*)(\$\s*[+-]?\d+(?:,\d{3})*(?:\.\d+)?)(?!\*)",
            r"**\1**",
            seg,
        )
        seg = re.sub(
            r"(?<!\*)(?<![\d.])([+-]?\d{1,3}(?:,\d{3})+)(?!\*)",
            r"**\1**",
            seg,
        )
        seg = re.sub(
            r"(?<!\*)(?<![\d.])([+-]?\d+\.\d+)(?![\d%*])(?!\*)",
            r"**\1**",
            seg,
        )
        seg = re.sub(r"\*\*\s*\*\*", "", seg)
        return seg

    out: list[str] = []
    pos = 0
    for m in _ALREADY_BOLD.finditer(s):
        out.append(_bold_plain(s[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_bold_plain(s[pos:]))
    return "".join(out)


def _humanize_dim_name(name: str) -> str:
    return (name or "").replace("_", " ").strip()


def _split_dimension_values(raw: str) -> list[str]:
    """Split ``dimension_value`` on list separators; keep ``City, ST``-style values intact."""
    s = (raw or "").strip()
    if not s:
        return []
    for sep in (r"\s*;\s*", r"\s*\|\s*", r"\s+/\s+"):
        if re.search(sep, s):
            return [p.strip() for p in re.split(sep, s) if p.strip()]
    if "," in s:
        chunks = [p.strip() for p in re.split(r",\s*", s) if p.strip()]
        if (
            len(chunks) == 2
            and len(chunks[1]) <= 3
            and chunks[1].replace(".", "").isalpha()
        ):
            return [s]
        return chunks
    return [s]


def _dimension_value_variants(value: str) -> list[str]:
    """Add hyphen vs space forms so DB ``Self-Generated`` matches prose ``Self Generated``."""
    v = (value or "").strip()
    if len(v) < 2:
        return []
    out: list[str] = [v]
    if "-" in v:
        out.append(v.replace("-", " "))
        out.append(v.replace("-", " – "))
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        n = re.sub(r"\s+", " ", x).strip()
        if len(n) < 2 or n.lower() in seen:
            continue
        seen.add(n.lower())
        uniq.append(n)
    return uniq


def _collect_dimension_phrases(
    dimension_name: str | None,
    dimension_value: str | None,
) -> list[str]:
    phrases: list[str] = []
    dvn = _humanize_dim_name(dimension_name or "")
    raw_dn = (dimension_name or "").strip()
    dvv_raw = (dimension_value or "").strip()

    parts: list[str] = []
    for p in _split_dimension_values(dvv_raw):
        parts.extend(_dimension_value_variants(p))
    # De-duplicate part list preserving order
    part_seen: set[str] = set()
    uniq_parts: list[str] = []
    for p in parts:
        k = p.lower()
        if k in part_seen:
            continue
        part_seen.add(k)
        uniq_parts.append(p)
    parts = uniq_parts

    for p in parts:
        if len(p) >= 2:
            phrases.append(p)
    if dvn and len(dvn) >= 2 and dvn.lower() not in {"n/a", "na", "—", "-"}:
        phrases.append(dvn)
    if raw_dn and raw_dn.lower() != dvn.lower() and len(raw_dn) >= 2:
        phrases.append(raw_dn)

    if dvn and parts:
        seps = [" — ", " – ", " - ", ": ", ":  ", " / ", " "]
        for p in parts:
            for sep in seps:
                if sep == " " and (len(dvn) < 4 or len(p) < 4):
                    continue
                combo = f"{dvn}{sep}{p}"
                phrases.append(combo)
    # Longest first at wrap time so "Self-Generated" wins over "Self"; do not drop
    # shorter phrases just because they are substrings of a longer combo — that combo
    # often does not appear verbatim in the narrative.
    uniq = sorted({p for p in phrases if len(p) >= 2}, key=len, reverse=True)
    return uniq[:120]


def _alnum_boundary_ok(seg: str, j: int, L: int) -> bool:
    if j > 0 and seg[j - 1].isalnum():
        return False
    end = j + L
    if end < len(seg) and seg[end].isalnum():
        return False
    return True


def _index_inside_paired_double_bold(seg: str, j: int) -> bool:
    """True if ``j`` lies inside a ``**…**`` span (odd number of ``**`` markers before ``j``)."""
    return seg[:j].count("**") % 2 == 1


def _wrap_case_insensitive_phrase(seg: str, phrase: str) -> str:
    if len(phrase) < 2 or not seg:
        return seg
    low_seg = seg.lower()
    low_ph = phrase.lower()
    L = len(phrase)
    out: list[str] = []
    i = 0
    search = 0
    while True:
        j = low_seg.find(low_ph, search)
        if j == -1:
            out.append(seg[i:])
            break
        if not _alnum_boundary_ok(seg, j, L):
            search = j + 1
            continue
        if _index_inside_paired_double_bold(seg, j):
            search = j + 1
            continue
        # Single ``*`` (not part of ``**``) before match — copy through, skip wrapping here.
        if j > 0 and seg[j - 1] == "*" and not (
            j >= 2 and seg[j - 2 : j] == "**"
        ):
            out.append(seg[i:j])
            i = j
            search = j + 1
            continue
        end = j + L
        out.append(seg[i:j])
        out.append(f"**{seg[j:end]}**")
        i = end
        search = end
    return "".join(out)


def apply_dimension_highlights(
    text: str | None,
    *,
    dimension_name: str | None = None,
    dimension_value: str | None = None,
) -> str:
    """Wrap dimension name/value phrases (outside existing ``**…**`` spans) for UI emphasis."""
    if not text or not str(text).strip():
        return text or ""
    text = repair_broken_bold_fragments(strip_zero_width_and_bom(str(text)))
    phrases = _collect_dimension_phrases(dimension_name, dimension_value)
    if not phrases:
        return str(text)
    chunks = re.split(r"(\*\*[^*]+?\*\*)", str(text), flags=re.DOTALL)
    out: list[str] = []
    for i, ch in enumerate(chunks):
        if i % 2 == 1:
            out.append(ch)
            continue
        seg = ch
        for phrase in phrases:
            seg = _wrap_case_insensitive_phrase(seg, phrase)
        out.append(seg)
    return repair_broken_bold_fragments("".join(out))


def reformat_body_markup_only(
    text: str | None,
    *,
    dimension_name: str | None = None,
    dimension_value: str | None = None,
) -> str:
    """Formatting-only pass for stored narratives (no route/jargon rewrites)."""
    out = apply_dimension_highlights(
        apply_frontend_bold_markup(normalize_body_bullets_and_markdown(text)),
        dimension_name=dimension_name,
        dimension_value=dimension_value,
    )
    return repair_broken_bold_fragments(out)


def reformat_title_markup_only(text: str | None) -> str:
    """Strip all asterisks from title."""
    return strip_title_markup(text)


def normalize_markup_plain_text(text: str | None) -> str:
    """Unwrap ``**…**`` and collapse whitespace — for verifying an LLM did not change prose."""
    if not text:
        return ""
    s = str(text).replace("\r\n", "\n")
    # Why-field subsection wrappers must not break markup-only comparisons.
    prev_tags: str | None = None
    while prev_tags != s:
        prev_tags = s
        # Portal uses `<sub>…<sub>` as open/close; also unwrap legacy `</sub>`.
        s = re.sub(r"(?is)<sub>\s*(.*?)\s*<sub>", r"\1", s, flags=re.DOTALL)
        s = re.sub(r"(?is)<sub>\s*(.*?)\s*</sub>", r"\1", s, flags=re.DOTALL)
    prev: str | None = None
    while prev != s:
        prev = s
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.DOTALL)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Front-end delimiter: same tag opens and closes the subsection header line.
_WHY_LINE_SUB_FRONTEND = re.compile(
    r"<sub>\s*(\*\*.+?\*\*)\s*<sub>\s*",
    re.IGNORECASE | re.DOTALL,
)
_WHY_LINE_SUB_LEGACY = re.compile(
    r"<sub>\s*(\*\*.+?\*\*)\s*</sub>\s*",
    re.IGNORECASE | re.DOTALL,
)

# Subsection titles in prose often omit **…**; they read ``Spend Mix — Observation…``.
_WHY_TITLE_EMDASH = " — "
_WHY_TITLE_ENDASH = " – "


def _why_strip_leading_bold_prefix(text: str) -> str:
    """Drop a leading ``**…**`` span so we can test observation-start heuristics."""
    t = text.lstrip()
    if not t.startswith("**"):
        return t
    end = t.find("**", 2)
    if end == -1:
        return t
    return t[end + 2 :].lstrip()


def _why_right_observation_start_ok(right: str) -> bool:
    """True if the observation (after ``—``) reads like a subsection gloss, not mid-sentence."""
    t = _why_strip_leading_bold_prefix(right)
    if not t:
        return False
    ch = t[0]
    return ch.isupper() or ch.isdigit()


def _split_why_title_observation(s: str) -> tuple[str, str] | None:
    if _WHY_TITLE_EMDASH in s:
        left, right = s.split(_WHY_TITLE_EMDASH, 1)
        return left.strip(), right.strip()
    if _WHY_TITLE_ENDASH in s:
        left, right = s.split(_WHY_TITLE_ENDASH, 1)
        return left.strip(), right.strip()
    return None


def _plain_why_subsection_title_line(s: str) -> str | None:
    """If ``s`` is a bare ``Dimension — Observation`` title line, return ``s``; else None."""
    if s.startswith("•") or s.startswith("\u2022"):
        return None
    parts = _split_why_title_observation(s)
    if not parts:
        return None
    left, right = parts
    if "*" in left:
        return None
    if len(left) < 2 or len(left) > 55 or len(right) < 12:
        return None
    if "," in left:
        return None
    if len(left.split()) > 6:
        return None
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 &./'\-]{0,54}$", left):
        return None
    if not _why_right_observation_start_ok(right):
        return None
    return s


def _classify_why_structure_line(line: str) -> tuple[str, str] | None:
    """Return (kind, payload) for a single line, or None if blank."""
    raw = line.rstrip()
    s = raw.strip()
    if not s:
        return None
    if s.startswith("•") or s.startswith("\u2022"):
        return ("b", s)
    if re.match(r"^[\-\*]\s+\S", s):
        return ("b", re.sub(r"^[\-\*]\s+", "• ", s, count=1))
    m_fe = _WHY_LINE_SUB_FRONTEND.fullmatch(s)
    if m_fe:
        return ("h", m_fe.group(1).strip())
    m_legacy = _WHY_LINE_SUB_LEGACY.fullmatch(s)
    if m_legacy:
        return ("h", m_legacy.group(1).strip())
    if re.fullmatch(r"\*\*.+\*\*", s):
        return ("h", s)
    plain_title = _plain_why_subsection_title_line(s)
    if plain_title:
        return ("h", f"**{plain_title}**")
    return ("p", raw)


def normalize_why_subsection_structure(text: str | None) -> str:
    """Normalize WHY text for subsection headers and newlines (portal parsing).

    When the text contains standalone ``**…**`` subsection headers (full line) or
    equivalent ``<sub>`` wrappers, rebuilds spacing:

    - Intro paragraphs, each ``<sub>**header**<sub>`` line, and each bullet group
      are separated by ``\\n\\n``.
    - Bullets under a subsection use a single ``\\n`` between lines.

    If there are no subsection headers, returns the input unchanged (aside from
    ``\\r\\n`` normalization and trailing newline).
    """
    if text is None:
        return ""
    s0 = repair_broken_bold_fragments(strip_zero_width_and_bom(str(text))).replace(
        "\r\n", "\n"
    )
    if not s0.strip():
        return ""
    lines = s0.split("\n")
    elements: list[tuple[str, str]] = []
    for line in lines:
        row = _classify_why_structure_line(line)
        if row:
            elements.append(row)
    if not any(k == "h" for k, _ in elements):
        return s0.rstrip() + "\n"

    parts: list[str] = []
    i = 0
    paras: list[str] = []
    while i < len(elements) and elements[i][0] == "p":
        paras.append(elements[i][1].strip())
        i += 1
    if paras:
        parts.append("\n\n".join(p for p in paras if p))

    while i < len(elements):
        kind = elements[i][0]
        if kind == "h":
            hdr = elements[i][1]
            i += 1
            bullets: list[str] = []
            while i < len(elements) and elements[i][0] == "b":
                bullets.append(elements[i][1])
                i += 1
            parts.append(f"<sub>{hdr}<sub>")
            if bullets:
                parts.append("\n".join(bullets))
        elif kind == "b":
            bullets = []
            while i < len(elements) and elements[i][0] == "b":
                bullets.append(elements[i][1])
                i += 1
            parts.append("\n".join(bullets))
        elif kind == "p":
            paras = []
            while i < len(elements) and elements[i][0] == "p":
                paras.append(elements[i][1].strip())
                i += 1
            chunk = "\n\n".join(p for p in paras if p)
            if chunk:
                parts.append(chunk)
        else:
            i += 1

    out = "\n\n".join(parts).rstrip()
    return (out + "\n") if out else ""
