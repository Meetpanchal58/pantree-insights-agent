"""Reconstruct a corrected request without treating old approvals as new authorization."""

from .interpret import is_confirmation

CORRECTION = "Correction to your reading of the question"


def reconstruction(prefix: list, edited: str) -> dict:
    prefix = [m for m in prefix if not (m.get("run") or {}).get("superseded_by_edit")]
    boundary = max(
        (i for i, m in enumerate(prefix) if m["role"] == "assistant" and (m.get("run") or {}).get("type") == "final"),
        default=-1,
    )
    segment = prefix[boundary + 1 :]
    # A question after a completed response starts a new request. Replies follow a clarification.
    if not segment or segment[-1]["role"] != "assistant" or (segment[-1].get("run") or {}).get("type") != "clarify":
        return {"question": edited, "answers": {}}
    original = next((m["content"] for m in segment if m["role"] == "user"), None)
    if not original:
        return {"question": edited, "answers": {}}
    answers = {}
    prompt = None
    for message in [*segment, {"role": "user", "content": edited}]:
        if message["role"] == "assistant":
            prompt = message.get("run") or {}
        elif prompt:
            value = message["content"]
            if is_confirmation(value):
                continue  # A regenerated plan always needs a new confirmation.
            questions = prompt.get("questions") or []
            if prompt.get("kind") == "confirm":
                if len(value.strip()) > 1:
                    answers[CORRECTION] = value
            elif len(questions) == 1:
                choices = questions[0].get("options") or []
                if value.strip().isdigit() and 1 <= int(value.strip()) <= len(choices):
                    value = choices[int(value.strip()) - 1]
                answers[questions[0]["question"]] = value
            else:
                answers[CORRECTION] = value
    return {"question": original, "answers": answers}
