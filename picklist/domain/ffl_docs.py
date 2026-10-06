"""Tier-2 readiness: read the FFL / EZ Check attached to an order and compare it
with the ship-to address.

This is the class of Teams thread where Shipping opens the PDF, squints at the
licensee name and premise, and asks Sales "is Juneau AK right? the FFL says AL".
The module does that read once per document (cached by file identity), parses
the licensee / trade names and premise address with the same regexes the
sql-toolbox audit uses, and returns findings that ``readiness.evaluate_readiness``
turns into ``ship_to_vs_ffl_name_mismatch`` / ``ship_to_vs_ffl_premise_mismatch``
holds. Both strings travel in the hold detail so a human can judge quickly.

Pure Python plus optional extractors: ``pypdf`` for text PDFs, ``pdf2image`` +
``pytesseract`` for scans, ``Pillow`` + ``pytesseract`` for images. Missing
dependencies never raise; they surface as an ``error`` string in the cache row.
"""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable, Optional

from picklist.domain.readiness import ffl_numbers_match, parse_expiration

REASON_NAME = "ship_to_vs_ffl_name_mismatch"
REASON_PREMISE = "ship_to_vs_ffl_premise_mismatch"
REASON_RECORD = "ffl_record_differs_from_doc"

DEFAULT_NAME_THRESHOLD = 0.60
DEFAULT_ADDR_THRESHOLD = 0.75
DEFAULT_OCR_DPI = 200
DEFAULT_OCR_PAGES = 2
DEFAULT_MAX_DOCS_PER_RUN = 20
SPARSE_TEXT_CHARS = 40
MAX_TEXT_CHARS = 40_000

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".jfif", ".tiff", ".tif", ".bmp"}
FFL_KINDS = ("ffl_ez_check", "ffl_master")

_cfg: dict[str, Any] = {
    "path_map": [],            # list[(windows_prefix, container_prefix)]
    "roots": [],               # list[Path]
    "ocr_enabled": False,
    "ocr_dpi": DEFAULT_OCR_DPI,
    "ocr_pages": DEFAULT_OCR_PAGES,
    "name_threshold": DEFAULT_NAME_THRESHOLD,
    "addr_threshold": DEFAULT_ADDR_THRESHOLD,
    "max_docs_per_run": DEFAULT_MAX_DOCS_PER_RUN,
    "logger": None,
}


def configure(**options: Any) -> None:
    for key, value in options.items():
        if key not in _cfg:
            raise KeyError(f"Unknown ffl_docs option: {key}")
        if key == "path_map" and isinstance(value, str):
            value = parse_path_map(value)
        if key == "roots" and isinstance(value, str):
            value = parse_roots(value)
        _cfg[key] = value


def _log() -> logging.Logger:
    return _cfg.get("logger") or logging.getLogger(__name__)


# --------------------------------------------------------------------------- paths


def _split_semicolon(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [piece.strip() for piece in str(raw).split(";") if piece.strip()]


def parse_path_map(raw: Optional[str]) -> list[tuple[str, str]]:
    """``V:\\=/mnt/carms;\\\\2CARMS\\CARMS$=/mnt/carms`` -> [(host, container), ...]."""
    pairs: list[tuple[str, str]] = []
    for piece in _split_semicolon(raw):
        if "=" not in piece:
            continue
        host, container = piece.split("=", 1)
        if host.strip() and container.strip():
            pairs.append((host.strip(), container.strip()))
    return pairs


def parse_roots(raw: Optional[str]) -> list[Path]:
    roots: list[Path] = []
    for piece in _split_semicolon(raw):
        try:
            roots.append(Path(piece).resolve())
        except OSError:
            continue
    return roots


def _normalize_windows(prefix: str) -> str:
    return prefix.replace("/", "\\").lower().rstrip("\\")


def map_path(raw: str, path_map: Optional[Iterable[tuple[str, str]]] = None) -> str:
    """Rewrite a Windows DOC_FILE_PATH prefix to its container mount."""
    pairs = list(path_map if path_map is not None else _cfg["path_map"])
    if not raw or not pairs:
        return raw
    raw_norm = raw.replace("/", "\\")
    raw_lower = raw_norm.lower()
    for host, container in pairs:
        host_norm = _normalize_windows(host)
        if not host_norm:
            continue
        if raw_lower == host_norm or raw_lower.startswith(host_norm + "\\"):
            remainder = raw_norm[len(host_norm):].lstrip("\\")
            container_clean = container.rstrip("/\\")
            if remainder:
                return f"{container_clean}/{remainder.replace(chr(92), '/')}"
            return container_clean
    return raw


def join_doc_path(doc_file_path: Optional[str], document_id: Optional[str]) -> str:
    """VISUAL stores the directory in DOC_FILE_PATH and the filename in DOCUMENT.ID."""
    base = (doc_file_path or "").rstrip("\\/").strip()
    name = (document_id or "").strip()
    if not base:
        return name
    if not name:
        return base
    sep = "\\" if ("\\" in base or (len(base) >= 2 and base[1] == ":")) else "/"
    return f"{base}{sep}{name}"


def resolve_document_path(
    doc_file_path: Optional[str],
    document_id: Optional[str],
    *,
    path_map: Optional[Iterable[tuple[str, str]]] = None,
    roots: Optional[Iterable[Path]] = None,
) -> tuple[Optional[Path], str, Optional[str]]:
    """Return (path, status, error). status: ok | missing_path | unsupported |
    no_roots | out_of_roots | not_found | invalid."""
    raw = join_doc_path(doc_file_path, document_id)
    if not raw:
        return None, "missing_path", "no DOC_FILE_PATH or DOCUMENT_ID"
    ext = Path(raw).suffix.lower()
    if ext not in PDF_EXTS | IMAGE_EXTS:
        return None, "unsupported", f"extension {ext or '(none)'} is not a PDF or image"
    mapped = map_path(raw, path_map)
    try:
        if mapped.startswith("\\\\") or (len(mapped) >= 2 and mapped[1] == ":"):
            candidate = Path(PureWindowsPath(mapped).as_posix()).resolve()
        else:
            candidate = Path(mapped).resolve()
    except OSError as exc:
        return None, "invalid", str(exc)
    allowed = list(roots if roots is not None else _cfg["roots"])
    if not allowed:
        return None, "no_roots", "DOCUMENT_ROOTS is not configured"
    inside = False
    for root in allowed:
        try:
            if candidate.is_relative_to(root):
                inside = True
                break
        except ValueError:
            continue
    if not inside:
        return None, "out_of_roots", f"{mapped} is outside DOCUMENT_ROOTS"
    if not candidate.is_file():
        return None, "not_found", f"document not found: {mapped}"
    return candidate, "ok", None


def doc_key(document_id: str, path: Path) -> str:
    """Cache key that changes whenever the file is replaced."""
    try:
        stat = path.stat()
        return f"{document_id}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        return f"{document_id}|?|?"


# --------------------------------------------------------------------------- classification


def classify_kind(filename: Optional[str], path: Optional[str] = None) -> str:
    """ffl_ez_check | ffl_master | other, from the filename / folder."""
    name_upper = (filename or "").strip().upper()
    path_upper = (path or "").replace("\\", "/").upper()
    ext = Path(name_upper).suffix.lower() if name_upper else ""
    if "FFL EZ CHECK" in name_upper or "FFL EZCHECK" in name_upper or "EZ CHECK" in name_upper or "EZCHECK" in name_upper:
        return "ffl_ez_check"
    if ext in PDF_EXTS | IMAGE_EXTS and ("/FFLS/" in path_upper or "FFL" in name_upper):
        return "ffl_master"
    return "other"


# --------------------------------------------------------------------------- extraction


def _cap(text: str) -> str:
    return text[:MAX_TEXT_CHARS]


def _ocr_pdf(path: Path, pages: int, dpi: int) -> tuple[str, Optional[str]]:
    try:
        from pdf2image import convert_from_path  # type: ignore
    except ImportError as exc:
        return "", f"pdf2image is not installed ({exc}); install poppler-utils + pdf2image"
    try:
        import pytesseract  # type: ignore
    except ImportError as exc:
        return "", f"pytesseract is not installed ({exc}); install tesseract-ocr + pytesseract"
    try:
        images = convert_from_path(str(path), dpi=dpi, first_page=1, last_page=max(1, pages))
    except Exception as exc:  # noqa: BLE001 - poppler errors surface here
        return "", f"pdf2image failed: {exc}"
    chunks: list[str] = []
    for index, image in enumerate(images, start=1):
        try:
            chunks.append(pytesseract.image_to_string(image) or "")
        except Exception as exc:  # noqa: BLE001
            chunks.append(f"[tesseract failed on page {index}: {exc}]")
    return "\f".join(chunks), None


def _ocr_image(path: Path) -> tuple[str, Optional[str]]:
    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:
        return "", f"Pillow is not installed ({exc})"
    try:
        import pytesseract  # type: ignore
    except ImportError as exc:
        return "", f"pytesseract is not installed ({exc})"
    try:
        with Image.open(path) as img:
            return pytesseract.image_to_string(img) or "", None
    except Exception as exc:  # noqa: BLE001
        return "", f"tesseract failed: {exc}"


def extract_text(
    path: Path,
    *,
    ocr_enabled: Optional[bool] = None,
    dpi: Optional[int] = None,
    pages: Optional[int] = None,
) -> tuple[str, str, Optional[str]]:
    """Return (text, method, error). method: pypdf | ocr | image_ocr | none. Never raises."""
    use_ocr = _cfg["ocr_enabled"] if ocr_enabled is None else bool(ocr_enabled)
    dpi = int(dpi or _cfg["ocr_dpi"])
    pages = int(pages or _cfg["ocr_pages"])
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        if not use_ocr:
            return "", "none", "image attachment needs OCR (READINESS_OCR_ENABLED=false)"
        text, error = _ocr_image(path)
        return _cap(text), ("image_ocr" if text.strip() else "none"), error
    if ext not in PDF_EXTS:
        return "", "none", f"unsupported extension {ext}"

    text = ""
    error: Optional[str] = None
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:
        error = f"pypdf is not installed ({exc})"
    else:
        try:
            reader = PdfReader(str(path))
            chunks = []
            for index in range(min(len(reader.pages), pages)):
                try:
                    chunks.append(reader.pages[index].extract_text() or "")
                except Exception as exc:  # noqa: BLE001
                    chunks.append("")
                    error = f"pypdf failed on page {index + 1}: {exc}"
            text = "\f".join(chunks)
        except Exception as exc:  # noqa: BLE001
            error = f"pypdf could not open the file: {exc}"
    if len(text.strip()) >= SPARSE_TEXT_CHARS:
        return _cap(text), "pypdf", None
    if not use_ocr:
        return _cap(text), ("pypdf" if text.strip() else "none"), error or "scanned PDF needs OCR (READINESS_OCR_ENABLED=false)"
    ocr_text, ocr_error = _ocr_pdf(path, pages, dpi)
    if ocr_text.strip():
        return _cap(ocr_text), "ocr", None
    return _cap(text), ("pypdf" if text.strip() else "none"), ocr_error or error


# --------------------------------------------------------------------------- parsing (ported from sql-toolbox diagnostics)

_CORP_SUFFIXES = {"LLC", "INC", "LTD", "CO", "CORP", "LP", "LLP", "COMPANY", "INCORPORATED", "THE"}
_USPS_ABBREVIATIONS = {
    "RD": "ROAD", "ST": "STREET", "AVE": "AVENUE", "BLVD": "BOULEVARD", "DR": "DRIVE", "LN": "LANE",
    "HWY": "HIGHWAY", "CIR": "CIRCLE", "CT": "COURT", "PKWY": "PARKWAY", "N": "NORTH", "S": "SOUTH",
    "E": "EAST", "W": "WEST", "NE": "NORTHEAST", "NW": "NORTHWEST", "SE": "SOUTHEAST", "SW": "SOUTHWEST",
    "IH": "I", "INTERSTATE": "I",
}
_FFL_LABELS_RE = re.compile(
    r"(?im)^\s*(License Name|Trade Name|Premise Address|Mailing Address|License Number|Expiration Date|Name)\s*:?\s*(.*?)\s*$"
)
_FFL_FOOTER_RE = re.compile(
    r"(?m)^\s*([^:\r\n]{3,}):([^:\r\n]{3,}):(\d{5}(?:-\d{4})?):"
    r"([0-9]-[0-9]{2}-[0-9]{3}-[0-9]{2}-[0-9A-Z]{2}-[0-9]{5}):"  # 5th segment carries a letter (9D)
)
# ATF EZ Check prints ZIP+4 without the hyphen ("TN - 376040000"), so the +4 is
# optional with or without it; a bare \d{5}\b would miss the nine-digit form.
_ZIP_RE = re.compile(r"\b(\d{5})(?:-?\d{4})?\b")
_STREET_NUMBER_RE = re.compile(r"^\s*(\d+)\b")
_ATTN_OR_UNIT_RE = re.compile(r"\b(?:ATTN|ATTENTION|SUITE|STE|UNIT|#)\b.*", re.IGNORECASE)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_name(value: Any) -> str:
    text = str(value or "").upper().replace("&", " AND ")
    tokens = re.sub(r"[^A-Z0-9]+", " ", text).split()
    return " ".join(token for token in tokens if token not in _CORP_SUFFIXES)


def _normalize_addr(value: Any) -> str:
    text = _ATTN_OR_UNIT_RE.sub("", str(value or "").upper())
    tokens = re.sub(r"[^A-Z0-9]+", " ", text).split()
    return " ".join(_USPS_ABBREVIATIONS.get(token, token) for token in tokens)


def similarity(left: Any, right: Any, *, address: bool = False) -> float:
    normalize = _normalize_addr if address else _normalize_name
    a = normalize(left)
    b = normalize(right)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))).ratio()
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if a_tokens and b_tokens and (a_tokens <= b_tokens or b_tokens <= a_tokens):
        return max(ratio, 0.95)
    return round(ratio, 4)


def _split_trade_names(raw: str) -> list[str]:
    if not raw:
        return []
    pieces = re.split(r"\s*(?:,|&|\band\b)\s*", raw, flags=re.IGNORECASE)
    return [_clean_text(piece) for piece in pieces if _clean_text(piece)]


def _zip5(value: Any) -> Optional[str]:
    match = _ZIP_RE.search(str(value or ""))
    return match.group(1) if match else None


def _street_number(value: Any) -> Optional[str]:
    match = _STREET_NUMBER_RE.match(str(value or ""))
    return match.group(1) if match else None


def _line_after(text_lines: list[str], index: int) -> str:
    parts: list[str] = []
    for line in text_lines[index + 1: index + 4]:
        cleaned = _clean_text(line)
        if not cleaned:
            continue
        if _FFL_LABELS_RE.match(cleaned):
            break
        parts.append(cleaned)
        if _zip5(cleaned):
            break
    return " ".join(parts)


def parse_ffl_doc(text: str) -> dict[str, Any]:
    """Best-effort parse of EZ Check / FFL text: legal names, trade names, premise."""
    parsed: dict[str, Any] = {"legal_names": [], "trade_names": [], "premise": None, "license_number": None, "expiration": None}
    if not text:
        return parsed
    lines = text.splitlines()
    footer = _FFL_FOOTER_RE.search(text)
    if footer:
        parsed["legal_names"].append(_clean_text(footer.group(1)))
        parsed["premise"] = _clean_text(f"{footer.group(2)} {footer.group(3)}")
        parsed["license_number"] = footer.group(4)
    for index, line in enumerate(lines):
        match = _FFL_LABELS_RE.match(line)
        if not match:
            continue
        label = match.group(1).lower()
        value = _clean_text(match.group(2))
        if not value:
            value = _line_after(lines, index)
        if not value:
            continue
        if label in {"license name", "name"}:
            parsed["legal_names"].append(value)
        elif label == "trade name":
            parsed["trade_names"].extend(_split_trade_names(value))
        elif label == "premise address":
            following = _line_after(lines, index)
            if following and following not in value:
                value = _clean_text(f"{value} {following}")
            parsed["premise"] = value
        elif label == "license number" and not parsed["license_number"]:
            parsed["license_number"] = value
        elif label == "expiration date" and not parsed["expiration"]:
            parsed["expiration"] = value
    parsed["legal_names"] = list(dict.fromkeys(parsed["legal_names"]))
    parsed["trade_names"] = list(dict.fromkeys(parsed["trade_names"]))
    return parsed


def merge_parsed(parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"legal_names": [], "trade_names": [], "premise": None, "license_number": None, "expiration": None}
    for parsed in parts:
        out["legal_names"].extend(parsed.get("legal_names") or [])
        out["trade_names"].extend(parsed.get("trade_names") or [])
        for key in ("premise", "license_number", "expiration"):
            if not out[key] and parsed.get(key):
                out[key] = parsed[key]
    out["legal_names"] = list(dict.fromkeys(out["legal_names"]))
    out["trade_names"] = list(dict.fromkeys(out["trade_names"]))
    return out


# --------------------------------------------------------------------------- comparison


def _ship_to_street(ship_to: dict[str, Any]) -> str:
    candidates = [ship_to.get("addr_1"), ship_to.get("addr_2"), ship_to.get("addr_3")]
    cleaned = [_clean_text(value) for value in candidates if _clean_text(value)]
    for line in cleaned:
        if _STREET_NUMBER_RE.match(line):
            return line
    return cleaned[0] if cleaned else ""


def compare(
    ship_to: dict[str, Any],
    parsed: dict[str, Any],
    *,
    name_threshold: Optional[float] = None,
    addr_threshold: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Compare the ship-to with the parsed FFL. Returns two findings, each
    ``{"reason_code", "passed", "detail"}``. Unparseable documents pass
    (with ``detail.unparsed``) so OCR noise never blocks an order."""
    name_threshold = float(name_threshold if name_threshold is not None else _cfg["name_threshold"])
    addr_threshold = float(addr_threshold if addr_threshold is not None else _cfg["addr_threshold"])
    ship_name = _clean_text(ship_to.get("name"))
    legal = list(parsed.get("legal_names") or [])
    trade = list(parsed.get("trade_names") or [])
    findings: list[dict[str, Any]] = []

    if not ship_name or not (legal or trade):
        findings.append({"reason_code": REASON_NAME, "passed": True, "detail": {
            "unparsed": True, "shipto_name": ship_name, "ffl_legal_name": legal[0] if legal else None,
        }})
    else:
        scored = [("legal", name, similarity(ship_name, name)) for name in legal]
        scored += [("trade", name, similarity(ship_name, name)) for name in trade]
        matched_on, matched_value, score = max(scored, key=lambda row: row[2])
        passed = score >= name_threshold
        findings.append({"reason_code": REASON_NAME, "passed": passed, "detail": {
            "shipto_name": ship_name,
            "ffl_legal_name": legal[0] if legal else None,
            "ffl_trade_names": ", ".join(trade) if trade else None,
            "best_match": f"{matched_value} ({matched_on})" if passed else None,
            "score": round(score, 2),
            "threshold": name_threshold,
        }})

    street = _ship_to_street(ship_to)
    premise = _clean_text(parsed.get("premise"))
    if not street or not premise:
        findings.append({"reason_code": REASON_PREMISE, "passed": True, "detail": {
            "unparsed": True, "shipto_addr": street or None, "ffl_premise": premise or None,
        }})
    else:
        ship_zip = _zip5(ship_to.get("zip"))
        premise_zip = _zip5(premise)
        zip_match = bool(ship_zip and premise_zip and ship_zip == premise_zip)
        score = similarity(street, premise, address=True)
        number_match = bool(_street_number(street) and _street_number(street) == _street_number(premise))
        # Same ZIP and same street number with a plausible street is the same
        # premise even when the road is written two ways ("490 I-35" vs
        # "490 IH 35 South"). A different ZIP is always a mismatch.
        passed = zip_match and (score >= addr_threshold or (number_match and score >= 0.5))
        if not zip_match:
            why = f"ZIP {ship_zip or '(missing)'} on the ship-to vs {premise_zip or '(missing)'} on the FFL"
        elif not passed:
            why = f"street similarity {score:.2f} below {addr_threshold:.2f}"
        else:
            why = None
        findings.append({"reason_code": REASON_PREMISE, "passed": passed, "detail": {
            "shipto_addr": f"{street} {_clean_text(ship_to.get('city'))} {_clean_text(ship_to.get('state'))} {ship_zip or ''}".strip(),
            "ffl_premise": premise,
            "zip_match": zip_match,
            "score": round(score, 2),
            "why": why,
        }})

    # The attached license is what ATF looks at; the VISUAL ship-to record is
    # what the tier-1 rules use. When they disagree, Sales fixes the record.
    doc_number = _clean_text(parsed.get("license_number")) or None
    doc_expiry_raw = _clean_text(parsed.get("expiration")) or None
    doc_expiry = parse_expiration(doc_expiry_raw) if doc_expiry_raw else None
    record_number = _clean_text(ship_to.get("ffl_number")) or None
    record_expiry_raw = _clean_text(ship_to.get("ffl_expiry_raw")) or None
    record_expiry = parse_expiration(record_expiry_raw) if record_expiry_raw else None
    problems: list[str] = []
    if doc_expiry and record_expiry and doc_expiry != record_expiry:
        problems.append(f"expiry {record_expiry_raw} on the ship-to vs {doc_expiry_raw} on the license")
    elif doc_expiry and not record_expiry:
        problems.append(f"no readable expiry on the ship-to; license says {doc_expiry_raw}")
    if doc_number and record_number and not ffl_numbers_match(doc_number, record_number):
        problems.append(f"number {record_number} on the ship-to vs {doc_number} on the license")
    elif doc_number and not record_number:
        problems.append(f"no FFL number on the ship-to; license is {doc_number}")
    findings.append({"reason_code": REASON_RECORD, "passed": not problems, "detail": {
        "unparsed": not (doc_number or doc_expiry_raw),
        "shipto_ffl_number": record_number,
        "shipto_ffl_expiry": record_expiry_raw,
        "doc_license_number": doc_number,
        "doc_expiration": doc_expiry_raw,
        "why": "; ".join(problems) if problems else None,
    }})
    return findings


# --------------------------------------------------------------------------- orchestration


def _is_candidate(order: dict[str, Any]) -> bool:
    if not order.get("firearms") or order.get("closed"):
        return False
    docs = order.get("docs") or {}
    if int(docs.get("ffl_ez_check") or 0) + int(docs.get("ffl_master") or 0) <= 0:
        return False
    flags = order.get("flags") or {}
    if flags.get("is_rma") or flags.get("is_international") or flags.get("is_employee") or flags.get("excluded_customer"):
        return False
    for hold in order.get("holds") or []:
        if hold.get("blocking") and hold.get("reason_code") not in (REASON_NAME, REASON_PREMISE, REASON_RECORD):
            return False
    return True


def findings_for_orders(
    orders: Iterable[dict[str, Any]],
    *,
    fetch_documents: Callable[[str], Iterable[dict[str, Any]]],
    cache_get: Callable[[str], Optional[dict[str, Any]]],
    cache_put: Callable[..., None],
    max_docs: Optional[int] = None,
    ocr_enabled: Optional[bool] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Tier-2 findings for every eligible order. Reads at most ``max_docs`` new
    documents per run; cached documents are free. Never raises per order."""
    budget = int(max_docs if max_docs is not None else _cfg["max_docs_per_run"])
    use_ocr = _cfg["ocr_enabled"] if ocr_enabled is None else bool(ocr_enabled)
    log = _log()
    results: dict[str, list[dict[str, Any]]] = {}
    candidates = sorted(
        (o for o in orders if _is_candidate(o)),
        key=lambda o: (o.get("due") or "9999-12-31", o.get("order_id") or ""),
    )
    stats = {"orders": 0, "docs_read": 0, "docs_cached": 0, "docs_skipped": 0}
    for order in candidates:
        order_id = str(order.get("order_id") or "").upper()
        if not order_id:
            continue
        try:
            rows = list(fetch_documents(order_id) or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("FFL docs: could not list documents for %s: %s", order_id, exc)
            continue
        parsed_parts: list[dict[str, Any]] = []
        docs: list[tuple[str, str, Optional[str]]] = []
        for row in rows:
            document_id = str(row.get("DOCUMENT_ID") or row.get("document_id") or "").strip()
            doc_dir = str(row.get("DOC_FILE_PATH") or row.get("doc_file_path") or "").strip()
            kind = classify_kind(document_id, doc_dir)
            if kind in FFL_KINDS:
                docs.append((kind, document_id, doc_dir))
        docs.sort(key=lambda item: 0 if item[0] == "ffl_ez_check" else 1)
        for kind, document_id, doc_dir in docs:
            path, status, error = resolve_document_path(doc_dir, document_id)
            if path is None:
                stats["docs_skipped"] += 1
                log.info("FFL docs: %s %s skipped (%s: %s)", order_id, document_id, status, error)
                continue
            key = doc_key(document_id, path)
            cached = cache_get(key)
            if cached is not None and cached.get("parsed") is not None:
                parsed_parts.append(cached["parsed"])
                stats["docs_cached"] += 1
                continue
            if budget <= 0:
                stats["docs_skipped"] += 1
                continue
            budget -= 1
            stats["docs_read"] += 1
            text, method, error = extract_text(path, ocr_enabled=use_ocr)
            parsed = parse_ffl_doc(text) if text else {"legal_names": [], "trade_names": [], "premise": None}
            try:
                cache_put(key, document_id=document_id, doc_path=str(path), method=method,
                          text=text[:MAX_TEXT_CHARS] if text else None, parsed=parsed, error=error)
            except Exception as exc:  # noqa: BLE001
                log.warning("FFL docs: cache write failed for %s: %s", document_id, exc)
            parsed_parts.append(parsed)
        if not parsed_parts:
            continue
        merged = merge_parsed(parsed_parts)
        results[order_id] = compare(order.get("ship_to") or {}, merged)
        stats["orders"] += 1
    log.info(
        "FFL docs: %d orders compared, %d documents read, %d from cache, %d skipped",
        stats["orders"], stats["docs_read"], stats["docs_cached"], stats["docs_skipped"],
    )
    return results
