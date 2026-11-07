"""Contact list loader. Reads a CSV and keeps only dialable numbers.

Expected headers (case-insensitive): name, phone, organization, status, notes.
Only `phone` is required. See sample_contacts.csv.
"""

import csv
import re

EXT_RE = re.compile(r"\b(?:ext(?:n|ension)?|x)\.?\s*\d+\s*$", re.I)


def normalize_phone(raw):
    """Best-effort US E.164. Returns None if it doesn't look dialable.

    Extensions are dropped rather than rejected -- a main line with an
    extension is still dialable, and throwing the whole row away over the
    suffix silently loses a call.
    """
    if not raw:
        return None
    raw = EXT_RE.sub("", str(raw).strip())
    digits = re.sub(r"[^\d+]", "", str(raw))
    if digits.startswith("+"):
        return digits if len(digits) >= 11 else None
    digits = re.sub(r"\D", "", digits)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def load_csv(path, limit=None):
    leads = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            lower = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
            phone = normalize_phone(lower.get("phone") or lower.get("phone number"))
            if not phone:
                continue
            leads.append(
                {
                    "id": f"csv-{i}",
                    "phone": phone,
                    "name": lower.get("name") or "Unknown",
                    "organization": lower.get("organization") or lower.get("company", ""),
                    "status": lower.get("status", ""),
                    "notes": lower.get("notes", "")[:400],
                    "source": "csv",
                }
            )
            if limit and len(leads) >= limit:
                break
    return leads
