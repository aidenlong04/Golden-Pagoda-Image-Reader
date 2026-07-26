"""Fish-watch logic for the Golden Pagoda bot's ``/watch`` command.

Pure (Discord-free) helpers behind the fishing-screenshot watcher: the fish
reference data, the env-persisted watch state, the vision-model OCR prompt,
and the submission-evaluation rules. bot.py owns every Discord side effect
(the /watch panel, reactions, error replies, message deletion); this module
only decides *what* a submission is and whether it passes.

Fish names and weights/quality come from the
``aidenlong04/warframe-item-pull`` repo (``docs/items/fish.md``): Plains of
Eidolon fish carry a maximum weight in kg, while Orb Vallis servofish and
some Cambion Drift fish carry a maximum quality in points.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

from config import _csv_ids, _int_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fish reference data (source: aidenlong04/warframe-item-pull docs/items/fish.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fish:
    """One watchable fish: canonical name, planet, and max weight/quality."""

    name: str
    planet: str
    quality: str   # "40 kg" (Plains weight) or "8 points" (servofish quality)
    rarity: str


FISH: tuple[Fish, ...] = (
    # Earth — Plains of Eidolon (maximum weight)
    Fish("Mortus Lungfish", "Earth", "40 kg", "Uncommon"),
    Fish("Mawfish", "Earth", "30 kg", "Common"),
    Fish("Sharrac", "Earth", "40 kg", "Uncommon"),
    Fish("Norg", "Earth", "40 kg", "Rare"),
    # Venus — Orb Vallis servofish (maximum quality points)
    Fish("Scrubber", "Venus", "7 points", "Common"),
    Fish("Brickie", "Venus", "10 points", "Common"),
    Fish("Longwinder", "Venus", "16 points", "Rare"),
    Fish("Tromyzon", "Venus", "8 points", "Rare"),
    # Deimos — Cambion Drift (weight or quality points)
    Fish("Amniophysi", "Deimos", "16 kg", "Common"),
    Fish("Cryptosuctus", "Deimos", "16 kg", "Common"),
    Fish("Duroid", "Deimos", "8 points", "Uncommon"),
    Fish("Aquapulmo", "Deimos", "8 points", "Rare"),
)

FISH_BY_KEY: dict[str, Fish] = {f.name.casefold(): f for f in FISH}

# Planet display order for the /watch panel reference table.
PLANETS: tuple[str, ...] = ("Earth", "Venus", "Deimos")

# One compiled pattern per fish, matching either the plain name as whole
# words ("norg", "mortus lungfish") or the letter-spaced trophy header
# Warframe renders on a catch ("N o r g"). Requiring single spaces between
# every letter in the spaced variant keeps ordinary prose ("no rg…") from
# false-positive matching.
def _fish_pattern(name: str) -> re.Pattern[str]:
    plain = r"\s+".join(re.escape(part) for part in name.split())
    spaced = r"\s+".join(
        r" ".join(re.escape(ch) for ch in part) for part in name.split()
    )
    return re.compile(
        rf"(?<![A-Za-z])(?:{plain}|{spaced})(?![A-Za-z])",
        re.IGNORECASE,
    )


_FISH_PATTERNS: dict[str, re.Pattern[str]] = {
    f.name: _fish_pattern(f.name) for f in FISH
}


def find_fish(text: str) -> str | None:
    """Return the canonical name of the first known fish found in ``text``.

    Case-insensitive whole-word match; tolerates the letter-spaced trophy
    header Warframe renders (e.g. ``N o r g``). Returns None when no known
    fish appears.
    """
    if not text:
        return None
    best: tuple[int, str] | None = None
    for name, pattern in _FISH_PATTERNS.items():
        m = pattern.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Known session codewords
# ---------------------------------------------------------------------------

# The default roster of individual code words admins rotate between, used when
# no custom roster is configured via the /watch modal
# (``FISH_WATCH_CODEWORDS`` / WatchState.codewords). An admin typing one of the
# configured words in the watched channel sets it as the active session
# codeword, detected the same way fish declarations are (whole-word,
# case-insensitive, letter-spacing tolerant).
CODEWORDS: tuple[str, ...] = (
    "Pepperoni",
    "Shockalaka",
    "Dinglehopper",
    "DYWATTA",
    "Citrus",
    "Onion",
    "Capybara",
    "Pinocchio",
    "Skibbibidy",
)
_CANONICAL_CODEWORDS: dict[str, str] = {w.casefold(): w for w in CODEWORDS}

_CODEWORD_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _canonical_codeword(raw: str | None) -> str | None:
    word = normalize_codeword(raw)
    if not word:
        return None
    return _CANONICAL_CODEWORDS.get(word.casefold())


def _codeword_pattern(word: str) -> re.Pattern[str]:
    """Memoized fish-style matcher for one code word."""
    pattern = _CODEWORD_PATTERN_CACHE.get(word)
    if pattern is None:
        pattern = _fish_pattern(word)
        _CODEWORD_PATTERN_CACHE[word] = pattern
    return pattern


def find_codeword(
    text: str, codewords: "Iterable[str] | None" = None
) -> str | None:
    """Return the first configured code word found in ``text``.

    ``codewords`` is the roster of allowed words (the /watch modal roster,
    normally :attr:`WatchState.codewords`); it defaults to the built-in
    :data:`CODEWORDS` when not supplied. Same detection rules as
    :func:`find_fish`: case-insensitive whole-word match tolerating
    letter-spaced rendering. Returns None when no configured code word appears.
    """
    if not text:
        return None
    roster = CODEWORDS if codewords is None else codewords
    best: tuple[int, str] | None = None
    for word in roster:
        word = _canonical_codeword(word)
        if not word:
            continue
        m = _codeword_pattern(word).search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), word)
    return best[1] if best else None


# A watch admin can declare a codeword from chat by leading the message with a
# "codeword" keyword, e.g. "codeword: pepperoni", "the codeword is dywatta",
# "set codeword onion". The trailing text is scanned for the first configured
# code word.
# Requiring the keyword prefix keeps ordinary admin chatter from being mistaken
# for a codeword.
# The optional lead-in (the / today's / new / set...) is a single, non-
# overlapping group to avoid catastrophic regex backtracking.
_CODEWORD_DECL_RE = re.compile(
    r"^\s*(?:(?:the|today'?s|new|set(?:ting|s)?)\s+)?"
    r"code\s*words?\b\s*"
    r"(?:is|are|=|:|-|\u2014|\u2013)?\s*"
    r"(?P<phrase>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_codeword_declaration(
    text: str | None, codewords: "Iterable[str] | None" = None
) -> str | None:
    """Extract an explicitly declared code word from an admin message.

    Recognises a message that leads with a ``codeword`` keyword (optionally
    prefixed with ``the`` / ``today's`` / ``set`` / ``new`` and followed by
    ``is`` / ``are`` / ``:`` / ``=`` / a dash) and returns the first allowed
    code word from the trailing text. Returns ``None`` when the message isn't a
    codeword declaration, carries no content, or doesn't include a configured
    code word.
    """
    if not text:
        return None
    m = _CODEWORD_DECL_RE.match(text)
    if not m:
        return None
    content = normalize_codeword(m.group("phrase"))
    # Drop a leading separator that leaked past the optional matcher (e.g.
    # "codeword:" captures ":") and reject a phrase with no real content.
    content = content.lstrip(":=-\u2014\u2013 \t").strip()
    if not any(ch.isalnum() for ch in content):
        return None
    return find_codeword(content, codewords)


def canonical_fish(name: str) -> str | None:
    """Resolve a raw fish name (any case) to its canonical form, or None."""
    fish = FISH_BY_KEY.get((name or "").strip().casefold())
    return fish.name if fish else None


def fish_unit(fish_name: str) -> str:
    """The measurement unit for a fish: ``"kg"`` (weight) or ``"points"``."""
    fish = FISH_BY_KEY.get((fish_name or "").strip().casefold())
    if fish and fish.quality.endswith("points"):
        return "points"
    return "kg"


_WEIGHT_KG_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*kg(?![A-Za-z])", re.IGNORECASE
)
_POINTS_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:points?|pts?)(?![A-Za-z])", re.IGNORECASE
)


# Catch quality tiers as they render under the fish name on a catch:
# Small/Medium/Large for weighed fish, Basic/Adorned/Magnificent for
# servofish quality.
_QUALITY_RE = re.compile(
    r"(?<![A-Za-z])(Small|Medium|Large|Basic|Adorned|Magnificent)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)


def extract_quality(text: str) -> str | None:
    """Pull the catch quality tier (e.g. ``Large`` or ``Adorned``) out of
    an OCR transcript, or ``None`` when no tier word appears."""
    if not text:
        return None
    m = _QUALITY_RE.search(text)
    return m.group(1).capitalize() if m else None


def extract_weight(text: str, fish_name: str) -> float | None:
    """Pull the caught weight (kg) or quality (points) out of an OCR
    transcript, matched against the unit the species is measured in.

    Returns the first plausible number for the fish's unit, or ``None``
    when the transcript carries no readable measurement (submissions still
    pass — the measurement only feeds the /watch records leaderboard).
    """
    if not text:
        return None
    pattern = _POINTS_RE if fish_unit(fish_name) == "points" else _WEIGHT_KG_RE
    for m in pattern.finditer(text):
        try:
            value = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        # Reject junk OCR digits: zero/negative or absurdly large values.
        if 0 < value < 1000:
            return value
    return None


# ---------------------------------------------------------------------------
# Vision-model OCR prompt
# ---------------------------------------------------------------------------

def build_watch_prompt() -> str:
    """Prompt for the local Ollama vision model tuned to fishing screenshots.

    Built from the annotated training screenshot provided with the feature
    request: the caught fish's name renders letter-spaced at the top centre,
    its size (Small/Medium/Large) directly beneath, the weight in the
    right-hand description block, and the session codeword appears
    somewhere in the chat box at the bottom left. (The highlighting in
    the reference image only marked where to look — submissions don't
    need the codeword highlighted.)
    """
    names = ", ".join(f.name for f in FISH)
    return (
        "You are an OCR engine reading a Warframe fishing screenshot. "
        "Transcribe ALL text visible in the image exactly as it appears, "
        "line by line. Pay special attention to: (1) the fish name shown "
        "letter-spaced at the top centre of the screen — it is one of: "
        f"{names}; (2) the size word (Small, Medium or Large) directly "
        "under the fish name; (3) the weight in kg or quality in points "
        "from the description panel on the right; (4) every chat line in "
        "the chat box at the bottom left, including every word after "
        "each player name. Output only the raw transcribed "
        "text — no commentary, no markdown, no explanations."
    )


# ---------------------------------------------------------------------------
# Watch state (persisted to .env by bot.py via envstore)
# ---------------------------------------------------------------------------

ENV_ENABLED = "FISH_WATCH_ENABLED"
ENV_CHANNEL = "FISH_WATCH_CHANNEL_ID"
ENV_CODEWORD = "FISH_WATCH_CODEWORD"
ENV_CODEWORDS = "FISH_WATCH_CODEWORDS"
ENV_ADMIN_IDS = "FISH_WATCH_ADMIN_IDS"
ENV_FISH = "FISH_WATCH_FISH"


@dataclass
class WatchState:
    """Mutable in-memory watch config, mirrored to .env on every change."""

    enabled: bool = False
    channel_id: int = 0
    codeword: str = ""
    admin_ids: set[int] = field(default_factory=set)
    current_fish: str | None = None
    # The roster of allowed code words (set via the /watch modal). A
    # watch admin typing one in the watched channel promotes it to the active
    # ``codeword``. Defaults to the built-in CODEWORDS when none configured.
    codewords: list[str] = field(default_factory=lambda: list(CODEWORDS))

    @classmethod
    def from_env(cls) -> "WatchState":
        fish = canonical_fish(os.getenv(ENV_FISH, ""))
        raw_codeword = os.getenv(ENV_CODEWORD)
        normalized_codeword = normalize_codeword(raw_codeword)
        codeword = _canonical_codeword(raw_codeword) or ""
        if normalized_codeword and not codeword:
            logger.info(
                "watch: ignoring unknown configured codeword %r",
                normalized_codeword,
            )
        codewords = parse_codewords(os.getenv(ENV_CODEWORDS))
        # The active codeword must stay in the roster, otherwise a watch admin
        # re-typing it in the watched channel is never recognised (find_codeword
        # only scans the roster). This also covers a legacy single-codeword
        # install (FISH_WATCH_CODEWORD set, FISH_WATCH_CODEWORDS unset): seed the
        # roster from that codeword rather than the built-in placeholders.
        if not codewords:
            codewords = [codeword] if codeword else list(CODEWORDS)
        elif codeword and codeword.casefold() not in {
            c.casefold() for c in codewords
        }:
            codewords.insert(0, codeword)
        return cls(
            enabled=_int_env(ENV_ENABLED) == 1,
            channel_id=_int_env(ENV_CHANNEL),
            codeword=codeword,
            admin_ids=set(_csv_ids(ENV_ADMIN_IDS)),
            current_fish=fish,
            codewords=codewords,
        )

    def env_items(self) -> list[tuple[str, str]]:
        """``(env_key, value)`` pairs for the .env persister."""
        return [
            (ENV_ENABLED, "1" if self.enabled else "0"),
            (ENV_CHANNEL, str(self.channel_id) if self.channel_id else ""),
            (ENV_CODEWORD, self.codeword),
            (ENV_CODEWORDS, ",".join(self.codewords)),
            (ENV_ADMIN_IDS, ",".join(str(i) for i in sorted(self.admin_ids))),
            (ENV_FISH, self.current_fish or ""),
        ]


# ---------------------------------------------------------------------------
# Submission evaluation
# ---------------------------------------------------------------------------

# Problem keys, ordered by severity: an unreadable screenshot short-circuits
# the codeword/fish checks (we can't trust anything OCR'd from it).
PROBLEM_UNREADABLE = "unreadable"
PROBLEM_CODEWORD = "codeword"
PROBLEM_WRONG_FISH = "wrong_fish"


@dataclass(frozen=True)
class Verdict:
    """Outcome of evaluating one screenshot submission."""

    ok: bool
    problems: tuple[str, ...]
    fish: str | None
    weight: float | None = None
    unit: str | None = None
    quality: str | None = None


# Characters that can't appear in an OCR transcript and render invisibly (or
# as markdown noise) in the error message: markdown backticks/quotes pasted
# around the word, plus zero-width/invisible unicode Discord inputs can carry
# (U+200B/C/D zero-width space/non-joiner/joiner, U+2060 word joiner,
# U+FEFF zero-width no-break space/BOM). A codeword containing these is unpassable AND shows as "codeword ``" in the
# rejection reply — normalize them away at every entry point.
_CODEWORD_JUNK_RE = re.compile(r"[`\"'\u200b\u200c\u200d\u2060\ufeff]")


def normalize_codeword(raw: str | None) -> str:
    """Sanitize a configured codeword (modal input / .env value)."""
    return _CODEWORD_JUNK_RE.sub("", raw or "").strip()


# Codeword rosters are entered one-per-line in the /watch modal and persisted
# comma-joined to .env; accept both separators when parsing back.
_CODEWORD_SPLIT_RE = re.compile(r"[,\n\r]+")


def parse_codewords(raw: str | None) -> list[str]:
    """Parse a roster of codewords from modal / .env input.

    Splits on commas and newlines, sanitizes each item via
    :func:`normalize_codeword`, keeps only known built-in code words, and
    de-duplicates case-insensitively while preserving first-seen order.
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    rejected: set[str] = set()
    for chunk in _CODEWORD_SPLIT_RE.split(raw):
        candidate = normalize_codeword(chunk)
        if not candidate:
            continue
        word = _canonical_codeword(candidate)
        if not word:
            rejected.add(candidate)
            continue
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(word)
    if rejected:
        logger.info(
            "watch: ignored unknown codewords: %s",
            ", ".join(sorted(rejected)),
        )
    return out


def _contains_codeword(text: str, codeword: str) -> bool:
    codeword = _canonical_codeword(codeword) or ""
    if not codeword:
        return True
    # Same matcher the fish detector uses: whole-word, case-insensitive,
    # letter-spaced rendering tolerated.
    return bool(_fish_pattern(codeword).search(text))


def evaluate_submission(
    ocr_text: str, *, codeword: str, expected_fish: str | None
) -> Verdict:
    """Judge one screenshot's OCR transcript against the watch rules.

    - Bad quality (empty transcript / no recognisable fish name) →
      ``unreadable``: the member should retry with a clearer image.
    - Configured ``codeword`` missing from the transcript → ``codeword``.
    - A fish other than ``expected_fish`` (the one the admin declared) →
      ``wrong_fish``.
    """
    text = (ocr_text or "").strip()
    fish = find_fish(text)
    problems: list[str] = []
    if not text or fish is None:
        problems.append(PROBLEM_UNREADABLE)
        return Verdict(False, tuple(problems), None)
    if not _contains_codeword(text, codeword):
        problems.append(PROBLEM_CODEWORD)
    if expected_fish and fish.casefold() != expected_fish.casefold():
        problems.append(PROBLEM_WRONG_FISH)
    weight = extract_weight(text, fish)
    return Verdict(
        not problems, tuple(problems), fish,
        weight=weight, unit=fish_unit(fish) if weight is not None else None,
        quality=extract_quality(text),
    )


def problem_messages(
    verdict: Verdict, *, codeword: str, expected_fish: str | None
) -> list[str]:
    """Human-readable error lines for a failing verdict."""
    out: list[str] = []
    for problem in verdict.problems:
        if problem == PROBLEM_UNREADABLE:
            out.append(
                "Could not read your screenshot. "
                "Retry with a clearer image."
            )
        elif problem == PROBLEM_CODEWORD:
            codeword = normalize_codeword(codeword)
            if codeword:
                out.append(
                    f"The codeword `{codeword}` is not in your screenshot. "
                    "Type it in chat so it shows on screen, "
                    "then submit a new screenshot."
                )
            else:
                out.append("A codeword is required.")
        elif problem == PROBLEM_WRONG_FISH:
            out.append(
                f"You submitted `{verdict.fish}`, "
                f"but the current fish is `{expected_fish}`. "
                "Please submit the correct fish."
            )
    return out
