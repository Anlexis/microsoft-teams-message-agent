"""Input sanitizer helpers for CMN-C2-234 (outer backbone pre_process).

Pure, stateless domain helpers (NOT framework gate methods). Strips HTML
markup and caps length before the raw caller text is JSON-serialized and
handed to the inner Teams workflow graph, defines the bounded shape a
caller-supplied Teams id must match before it is accepted, and owns the
template's instruction-override (prompt-injection) screen.
"""

from __future__ import annotations

import re

from typing import Any

_HTML_TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_MAX_LENGTH = 4000

# The bounded shape of a Microsoft Teams target id: a GUID (team) or a
# thread-style id such as 19:...@thread.tacv2 (channel / chat) — alphanumeric
# plus :@._- , no spaces, at most 80 characters. Caller-supplied target hints
# must match this shape exactly; anything else is refused (fail closed).
TEAMS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:@._-]{0,79}$")

# ── Template-owned instruction-override screen ────────────────────────────────
# HTML stripping is markup hygiene, not injection refusal: text that tries to
# re-instruct the MODEL ("Ignore all previous instructions ...") must FAIL the
# request, not ride into the inner workflow lightly dusted. The framework's
# input gate is no substitute — it exists only on 1.0.1+ hosts, covers only
# user_input/validated_input, and rejects only HIGH-confidence findings — so a
# template whose only defence is the platform's fails OPEN wherever that gate
# is absent. This screen makes the refusal the template's own guarantee.
#
# Deliberately NARROW, because in this agent the message text IS the product:
# ordinary workplace messages use every verb an injection uses ("please
# disregard my earlier message", "acting as the on-call this week", "forget
# the earlier draft", "you are now an owner of the Design team"). Every
# alternative therefore anchors a WHOLE directive phrase — the override verb
# plus a prompt-specific noun or a model role — or a chat-template control
# token (<|im_start|>, [INST], <<SYS>>), which has no legitimate reading in a
# workplace message. The screen refuses instruction-override aimed AT the
# agent, never people coordinating work with each other.
INSTRUCTION_OVERRIDE_RE = re.compile(
    # Chat-template control tokens as a CLASS: <|im_start|>, <|system|>,
    # [INST]...[/INST], <<SYS>>...<</SYS>>.
    r"<\|[a-z_]{2,32}\|>"
    r"|\[/?INST\]"
    r"|<</?SYS>>"
    # ignore/disregard/forget + qualifier + instruction-noun. "message" is
    # deliberately NOT an instruction-noun: "please disregard my earlier
    # message" is the product, not an attack.
    r"|\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+|my\s+)*"
    r"(?:previous|prior|above|earlier|preceding|original|system)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules)\b"
    # The bare form with no temporal qualifier: "ignore all instructions".
    r"|\b(?:ignore|disregard)\s+all\s+(?:instructions|rules)\b"
    # Exfiltration of the system/initial/hidden prompt. "the system prompt for
    # the demo is on the wiki" carries no reveal-verb and passes.
    r"|\b(?:reveal|show|print|repeat|output|disclose|display|dump)\s+(?:me\s+)?"
    r"(?:your\s+(?:system\s+|initial\s+|hidden\s+)?(?:prompt|prompts|instructions)"
    r"|the\s+(?:system|initial|hidden)\s+(?:prompt|prompts|instructions|message))\b"
    # Role-reassignment to a MODEL role. "you are now an owner of the Design
    # team" reassigns a PERSON and passes.
    r"|\byou\s+are\s+now\s+(?:a\s+|an\s+)?(?:different\s+|unrestricted\s+|new\s+|jailbroken\s+)?"
    r"(?:assistant|ai|chatbot|language\s+model|llm|dan)\b"
    # Privileged-mode role-play: requires developer/admin/root/jailbroken +
    # "mode" ("acting as the on-call this week" passes).
    r"|\bact\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:a\s+|an\s+)?"
    r"(?:developer|admin|administrator|root|jailbroken|unrestricted)\s+mode\b"
    # Rule-override: requires an instruction-noun ("the badge override is
    # approved" passes).
    r"|\boverride\s+(?:your|the)\s+"
    r"(?:instruction|instructions|rule|rules|safety|guardrail|guardrails|restriction|restrictions)\b"
    # "new system prompt:" header form.
    r"|\b(?:new|updated)\s+system\s+(?:prompt|instructions)\s*[:=]",
    re.IGNORECASE,
)

# Placeholder echoed in place of a caller-controlled field name. A hostile
# field NAME is data too — it is masked in error paths, never repeated.
MASKED_FIELD = "<unrecognised-field>"


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip HTML tags (injection/markup guard) and cap length."""
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]


def find_instruction_override(
    value: Any,
    known_fields: frozenset[str] = frozenset(),
    root: str = "input",
) -> str | None:
    """Masked path of the first string carrying an instruction-override directive.

    Walks the DECODED structure — keys and values, at any depth — never the
    raw JSON text, so backslash-u escaping cannot smuggle a phrase past the
    screen. The walk is iterative: payload depth is bounded only by what
    json.loads accepted, and this must not be the piece that falls over first.
    Path components outside ``known_fields`` are replaced with MASKED_FIELD,
    so the returned path is always safe to name in an error message. Returns
    None when the structure is clean.
    """
    stack: list[tuple[Any, str]] = [(value, root)]
    while stack:
        item, path = stack.pop()
        if isinstance(item, str):
            if INSTRUCTION_OVERRIDE_RE.search(item):
                return path
        elif isinstance(item, dict):
            for key, sub in item.items():
                if isinstance(key, str):
                    safe_key = key if key in known_fields else MASKED_FIELD
                    key_path = f"{path}.{safe_key}"
                    if INSTRUCTION_OVERRIDE_RE.search(key):
                        return key_path
                else:
                    key_path = f"{path}.{MASKED_FIELD}"
                stack.append((sub, key_path))
        elif isinstance(item, (list, tuple)):
            stack.extend((sub, f"{path}[{index}]") for index, sub in enumerate(item))
    return None
