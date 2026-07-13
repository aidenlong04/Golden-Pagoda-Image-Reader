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

import os
import re
from dataclasses import dataclass, field

from config import _csv_ids, _int_env

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


def canonical_fish(name: str) -> str | None:
    """Resolve a raw fish name (any case) to its canonical form, or None."""
    fish = FISH_BY_KEY.get((name or "").strip().casefold())
    return fish.name if fish else None


# ---------------------------------------------------------------------------
# Vision-model OCR prompt
# ---------------------------------------------------------------------------

def build_watch_prompt() -> str:
    """Prompt for the local Ollama vision model tuned to fishing screenshots.

    Built from the annotated training screenshot provided with the feature
    request: the caught fish's name renders letter-spaced at the top centre,
    its size (Small/Medium/Large) directly beneath, the weight in the
    right-hand description block, and the session codeword is the
    green-highlighted word in the chat box at the bottom left.
    """
    names = ", ".join(f.name for f in FISH)
    return (
        "You are an OCR engine reading a Warframe fishing screenshot. "
        "Transcribe ALL text visible in the image exactly as it appears, "
        "line by line. Pay special attention to: (1) the fish name shown "
        "letter-spaced at the top centre of the screen — it is one of: "
        f"{names}; (2) the size word (Small, Medium or Large) directly "
        "under the fish name; (3) the weight in kg from the description "
        "panel on the right; (4) every chat line in the chat box at the "
        "bottom left, including any word highlighted in green after the "
        "player name. Output only the raw transcribed text — no "
        "commentary, no markdown, no explanations."
    )


# ---------------------------------------------------------------------------
# Watch state (persisted to .env by bot.py via envstore)
# ---------------------------------------------------------------------------

ENV_ENABLED = "FISH_WATCH_ENABLED"
ENV_CHANNEL = "FISH_WATCH_CHANNEL_ID"
ENV_CODEWORD = "FISH_WATCH_CODEWORD"
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

    @classmethod
    def from_env(cls) -> "WatchState":
        fish = canonical_fish(os.getenv(ENV_FISH, ""))
        return cls(
            enabled=_int_env(ENV_ENABLED) == 1,
            channel_id=_int_env(ENV_CHANNEL),
            codeword=(os.getenv(ENV_CODEWORD) or "").strip(),
            admin_ids=set(_csv_ids(ENV_ADMIN_IDS)),
            current_fish=fish,
        )

    def env_items(self) -> list[tuple[str, str]]:
        """``(env_key, value)`` pairs for the .env persister."""
        return [
            (ENV_ENABLED, "1" if self.enabled else "0"),
            (ENV_CHANNEL, str(self.channel_id) if self.channel_id else ""),
            (ENV_CODEWORD, self.codeword),
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


def _contains_codeword(text: str, codeword: str) -> bool:
    if not codeword:
        return True
    pattern = re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(codeword) + r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


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
    return Verdict(not problems, tuple(problems), fish)


def problem_messages(
    verdict: Verdict, *, codeword_set: bool, expected_fish: str | None
) -> list[str]:
    """Human-readable error lines for a failing verdict."""
    out: list[str] = []
    for problem in verdict.problems:
        if problem == PROBLEM_UNREADABLE:
            out.append(
                "The screenshot is too low quality to read — the text is "
                "not visible or is illegible. Please retry with a new, "
                "clearer image."
            )
        elif problem == PROBLEM_CODEWORD:
            out.append(
                "The codeword is required. Make sure the current codeword "
                "is visible (highlighted in green in your chat box) and "
                "submit a new screenshot."
                if codeword_set
                else "The codeword is required."
            )
        elif problem == PROBLEM_WRONG_FISH:
            out.append(
                f"That's a **{verdict.fish}** — the fish being watched is "
                f"**{expected_fish}**. Submit a {expected_fish} screenshot."
            )
    return out
