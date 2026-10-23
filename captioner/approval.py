"""Parse the owner's replies and render review questions. Pure functions, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass

_OK = re.compile(r"^\s*(ok|okay)\s*[.!]?\s*$", re.IGNORECASE)
# Near-misses such as "okk", "k", "oki" must not become an approved one-word caption.
_OK_TYPO = re.compile(r"^\s*(o+k+[a-z]{0,3}|k+|0k)\s*[.!]*\s*$", re.IGNORECASE)
_CHANGE_SINGLE = re.compile(r"^\s*change\s*:\s*(?P<instr>.*\S)\s*$", re.IGNORECASE | re.DOTALL)
_CHANGE_ITEM = re.compile(r"^\s*change\s+(?P<n>\d+)\s*:\s*(?P<instr>.*\S)\s*$", re.IGNORECASE | re.DOTALL)
_REPLACE_ITEM = re.compile(r"^\s*(?P<n>\d+)\s*:[ \t]?(?P<text>.*)$", re.DOTALL)
SHORT_REPLY_WORDS = 2


@dataclass(frozen=True)
class Approve:
    pass


@dataclass(frozen=True)
class Rewrite:
    item: int
    instruction: str


@dataclass(frozen=True)
class Replace:
    item: int
    text: str


@dataclass(frozen=True)
class Unclear:
    reason: str


Command = Approve | Rewrite | Replace | Unclear


def parse_reply(text: str, n_items: int) -> Command:
    """Single item: `ok`, `change: ...`, or any other text = exact replacement.
    Album: `ok`, `change N: ...`, `N: exact caption`; anything else is unclear (nothing changes)."""
    if _OK.match(text):
        return Approve()
    if _OK_TYPO.match(text):
        return Unclear("Did you mean ok? Reply exactly ok to approve.")
    item_change = _CHANGE_ITEM.match(text)
    if item_change:
        n = int(item_change["n"])
        if not 1 <= n <= n_items:
            return Unclear(f"There is no item {n}; this review has {n_items}.")
        return Rewrite(n, item_change["instr"])
    if n_items == 1:
        single_change = _CHANGE_SINGLE.match(text)
        if single_change:
            return Rewrite(1, single_change["instr"])
        if re.match(r"^\s*change\s*:?\s*$", text, re.IGNORECASE):
            return Unclear("Say what to change, for example: change: make it shorter")
        if not text.strip():
            return Unclear("The reply was empty.")
        explicit = re.match(r"^\s*1\s*:[ \t]?(?P<text>.*\S.*)$", text, re.DOTALL)
        if explicit:
            return Replace(1, explicit["text"])
        if "\n" not in text.strip() and len(text.split()) <= SHORT_REPLY_WORDS:
            # "yes", "thanks", "👍" are far more likely chat than a caption: ask before approving.
            return Unclear(f"To use \"{text.strip()}\" as the caption, reply: 1: {text.strip()}")
        return Replace(1, text)
    replace = _REPLACE_ITEM.match(text)
    if replace:
        n = int(replace["n"])
        if not 1 <= n <= n_items:
            return Unclear(f"There is no item {n}; this review has {n_items}.")
        if not replace["text"].strip():
            return Unclear(f"The caption for item {n} was empty.")
        return Replace(n, replace["text"])
    return Unclear("I could not tell which item you mean.")


@dataclass(frozen=True)
class ReviewItem:
    number: int
    job_id: int
    kind: str
    file_name: str
    message_id: int
    posted: str
    caption: str | None
    maybe_compressed: bool
    rewrites_left: int


def render_question(items: list[ReviewItem]) -> str:
    album = len(items) > 1
    head = f"Review: album of {len(items)} items" if album else "Review: 1 item"
    blocks = [head]
    for it in items:
        lines = [f"#{it.number} {it.kind} | {it.file_name} | message {it.message_id} | {it.posted} | job {it.job_id}"]
        if it.maybe_compressed:
            lines.append("Note: this looks Telegram-compressed. Send as file to keep full quality.")
        if it.caption:
            lines.append("Proposed caption:")
            lines.append(it.caption)
        else:
            lines.append("No caption yet. Please reply with the caption you want.")
        if it.rewrites_left == 0:
            lines.append("AI rewrite limit reached for this item: reply with the exact caption instead.")
        blocks.append("\n".join(lines))
    if album:
        blocks.append("Reply to THIS message with:\n"
                      "ok  (approve all captions shown)\n"
                      "change 2: <what to change>  (AI rewrites item 2)\n"
                      "2: <exact caption>  (use your words for item 2)")
    else:
        blocks.append("Reply to THIS message with:\n"
                      "ok  (approve)\n"
                      "change: <what to change>  (AI rewrites it)\n"
                      "or type the caption you want (used exactly as written; for one or two words, "
                      "start with 1:)")
    return "\n\n".join(blocks)


MAX_DESCRIPTION_BYTES = 5000


def youtube_description(caption: str) -> str:
    """YouTube rejects '<' and '>' in descriptions and limits them to 5000 bytes. The approved caption
    itself (sidecar, approval record) keeps every character; only the uploaded copy is adjusted."""
    text = caption.replace("<", "").replace(">", "")
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_DESCRIPTION_BYTES:
        return text
    return encoded[:MAX_DESCRIPTION_BYTES].decode("utf-8", "ignore")


def youtube_title(caption: str, fallback_date: str) -> str:
    """Deterministic title: first non-empty caption line, trimmed to 100 characters."""
    for line in caption.splitlines():
        cleaned = line.strip().replace("<", "").replace(">", "")
        if cleaned:
            return cleaned if len(cleaned) <= 100 else cleaned[:99].rstrip() + "…"
    return f"Memory {fallback_date}"
