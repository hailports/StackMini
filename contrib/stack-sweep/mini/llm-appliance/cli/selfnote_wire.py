"""Self-note body shared by the sprint mailer and the work-graph note.

One finding, one next step, the open button, and links that already exist
on the record. Technical prep stays on disk. A blocked paragraph is dropped
and retried. The prep stub is never sent in its place.
"""
from __future__ import annotations

import html
import json
import re

import handoff_mail


def mail_links(tid: str, rec: dict, ado_template: str, related_ids=None) -> list:
    """ADO first, then URLs already present on the record. Nothing is guessed."""
    links = [(f"ADO {tid}", ado_template.format(tid))]
    raw = (rec or {}).get("links") or []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                links.append((str(item[0]), str(item[1])))
            elif isinstance(item, dict) and item.get("url"):
                links.append((str(item.get("label") or "Link"), str(item["url"])))
            elif isinstance(item, str):
                links.extend(handoff_mail.extract_work_links(item, tid))
    parts = []
    for key in ("ado", "found", "ado_live", "work_brain", "packet_draft", "first_draft"):
        val = (rec or {}).get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, (list, dict)):
            try:
                parts.append(json.dumps(val)[:24000])
            except Exception:
                pass
    for key in ("findings", "steps", "remaining"):
        val = (rec or {}).get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, list):
            parts.append("\n".join(str(x) for x in val))
    blob = "\n".join(parts)
    links.extend(handoff_mail.extract_work_links(blob, tid))
    if related_ids is not None:
        try:
            for wid in related_ids(blob, tid):
                links.append((f"ADO {wid}", ado_template.format(wid)))
        except Exception:
            pass
    for wid in re.findall(r"\b(?:work item|ticket)\s+#?(\d{5,7})\b", blob, re.I):
        if wid != str(tid):
            links.append((f"ADO {wid}", ado_template.format(wid)))
    return links


def next_from_rec(rec: dict) -> str:
    steps = (rec or {}).get("steps") or []
    if isinstance(steps, str):
        steps = [steps]
    remaining = (rec or {}).get("remaining") or ""
    if isinstance(remaining, list):
        remaining = " ".join(str(x) for x in remaining[:2])
    cands = []
    if isinstance(steps, list) and steps:
        cands.append(steps[0])
    if str(remaining or "").strip():
        cands.append(remaining)
    if isinstance(steps, list):
        cands.extend(list(steps)[1:4])
    for cand in cands:
        cleaned = handoff_mail.sanitize_outlook(str(cand or ""))
        if cleaned:
            return cleaned
    return ""


def note_for_record(title: str, rec: dict) -> tuple[str, list[str]]:
    """Plain finding and one next step. The record is used, not discarded."""
    findings = (rec or {}).get("findings") or []
    step = next_from_rec(rec or {})
    paras = handoff_mail.note_paragraphs(handoff_mail.about_line(title), findings, step)
    return " ".join(paras), paras


def retry_gate(plain: str, paras: list[str], subject: str, gate) -> tuple[str, list[str], str]:
    """Drop or resanitize a blocked paragraph. Never fall back to the prep stub."""

    def blocked(text: str) -> str:
        if not str(text or "").strip():
            return "empty"
        try:
            return gate(text, subject) or ""
        except Exception as exc:
            return f"gate:{type(exc).__name__}"

    block = blocked(plain)
    tries = 0
    while block and tries < 6 and paras:
        tries += 1
        if "lowercase_start" in str(block):
            paras = [((p[0].upper() + p[1:]) if p[:1].islower() else p) for p in paras if p]
            plain = " ".join(paras)
            block = blocked(plain)
            if not block:
                break
        if str(block).startswith("banned:") and ":" in str(block)[7:]:
            bit = str(block).split(":", 1)[1].strip()
            if bit and bit not in ("lowercase_start", "slash", "truncated", "underscore_token"):
                paras = [re.sub(re.escape(bit), " ", p, flags=re.I) for p in paras]
                paras = [handoff_mail.sanitize_outlook(p) for p in paras]
                paras = [p for p in paras if p]
                plain = " ".join(paras)
                block = blocked(plain)
                if not block:
                    break
        kept = [p for p in paras if p and not blocked(p)]
        if kept and kept != paras:
            paras = kept
            plain = " ".join(paras)
            block = blocked(plain)
            continue
        word = ""
        mword = re.search(r"(?i)(?:bare|token|raw_tech|tech_word|filler|stack_talk|hedge):([^;]+)", str(block))
        if mword:
            word = mword.group(1).strip()
        if word:
            new_paras = []
            for p in paras:
                cleaned = handoff_mail.sanitize_outlook(re.sub(re.escape(word), " ", p, flags=re.I))
                if cleaned:
                    new_paras.append(cleaned)
            if new_paras and new_paras != paras:
                paras = new_paras
                plain = " ".join(paras)
                block = blocked(plain)
                continue
        if len(paras) > 1:
            paras = paras[:-1]
            plain = " ".join(paras)
            block = blocked(plain)
            continue
        break
    return plain, paras, str(block or "")


def render_html(paras: list[str], button_url: str, links) -> str:
    body = "\n".join(f"<p>{html.escape(p)}</p>" for p in paras)
    body += "\n" + handoff_mail.primary_button(button_url)
    body += "\n" + handoff_mail.links_html(links)
    return body


def work_graph_links(ticket_id: str, blob: str, existing) -> list:
    """Keep ADO and related work items. Drop class-file paths. Add real PR and record URLs."""
    links = []
    for label, url in existing or []:
        if str(label).endswith(".cls") or ".cls" in str(url).lower() or "/blob/" in str(url):
            continue
        links.append((label, url))
    links.extend(handoff_mail.extract_work_links(blob, str(ticket_id)))
    return links


def work_graph_body(title: str, ticket_id: str, findings, steps, decision, button_url: str, links) -> tuple[str, str]:
    """When a next-step URL exists, the body is the finding and one step, not the stub."""
    nxt = ""
    for cand in list(steps or []) + ([decision] if decision else []):
        cleaned = handoff_mail.sanitize_outlook(str(cand or ""))
        if cleaned:
            nxt = cleaned
            break
    paras = handoff_mail.note_paragraphs(handoff_mail.about_line(title), findings, nxt)
    if not paras:
        return "", ""
    return " ".join(paras), render_html(paras, button_url, links)
