"""
Annual peak-day capacity report adapter.

CORRECTION TO THE PROJECT TEMPLATE, established live on 7 September 2026: this
report is **NOT filed as Form 549B**. The Form 549B instruction manual covers the
Index of Customers only. The annual capacity report is a separate submittal:

  * eLibrary Class/Type  `Report/Form` / `Peak Day Capacity Report`, library `G`
  * authority cited on the filings themselves: **18 CFR 284.13(d)(2)**,
    Docket **RM85-1-000**
  * description pattern `Annual Peak Day Capacity Report of <NAME> for <YYYY>.`
  * the eLibrary description year is retained unless exact value-period wording
    in the filing controls, or a reviewed correction is bound to both the
    accession and the exact PDF hash

The template document's attribution to Form 549B is wrong and is not reproduced
here; the same correction is written up in
`discovery/ioc_ferclib_requests.md`. The registry metric ids still read
`cap_*` and the regime string is still the registry's `Form 549B Capacity`,
because the registry is not this adapter's to change -- but every observation
carries the real authority in its evidence.

Extraction is COORDINATE-AWARE. Most attachments are born-digital PDFs with a
live text layer, but two reviewed filings are image-only and use the separately
declared Poppler + macOS Vision OCR path. For text-layer filings, two things
defeat a naive extractor:

  * TGP's attachment page uses an embedded subset font whose built-in encoding is
    **ASCII offset by +29** (code + 29 = ASCII, so `7UDQVSRUW` is `Transport`).
    Parts of Transco's cover letter use the same trick. Where the font carries a
    /ToUnicode CMap that map is used verbatim; where it does not, the +29 offset
    is applied only when it produces real English words.
  * without x-coordinates, adjacent table cells concatenate. This module tracks
    the text and line matrices, per-glyph advance widths from the font's /W or
    /Widths array, and TJ kerning, so every cell keeps its own x range and
    columns are assigned from geometry rather than guessed.

That resolves the one thing `discovery/ioc_discovery.md` recorded as unresolved.
Read positionally, TGP's first transport row is

    "Stations 245 & 321"  [x 119-191] | "3,303" [x 365-386] | "1,034" [x 457-478]

so the label is *Stations 245 & 321* and the Zn1->Zn6 figure is 3,303 MDth/d.
The earlier non-positional reading ("Stations 245 & 32" | "13,303") was a
mis-split and is corrected in the notes file. Where a row still cannot be split
cleanly -- glyphs emitted out of reading order, no unit resolvable, no column
label -- the figure is published with the ambiguity named in its confidence and
its `value_num` withheld, never with a guessed split.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import traceback
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from adapters import ioc as ioc_adapter                                     # noqa: E402
from ferclib import coverage, periods                                       # noqa: E402
from ferclib.http import FetchError, redact                                 # noqa: E402
from ferclib.image_ocr import ImageOCRFailure, extract_image_pdf             # noqa: E402
from ferclib.registry import BY_ADAPTER, BY_ID, REGISTRY_VERSION            # noqa: E402
from ferclib.staging import observation_id                                  # noqa: E402
from ferclib.status import (Availability, Method, Origin, Validation,       # noqa: E402
                            VersionStatus)

ADAPTER = "capacity"
SOURCE_SYSTEM = ioc_adapter.SOURCE_SYSTEM
REGIME = "Form 549B Capacity"
FORM = REGIME
CLASS_TYPE = ("Report/Form", "Peak Day Capacity Report")
SEARCH_PHRASE = "Annual Peak Day Capacity Report of"
AUTHORITY = "18 CFR 284.13(d)(2), Docket RM85-1-000"
APPLICABILITY_BASIS = (f"{AUTHORITY}; eLibrary Class/Type "
                       f"'{CLASS_TYPE[0]}' / '{CLASS_TYPE[1]}'. NOT Form 549B: the 549B "
                       f"instruction manual covers the Index of Customers only")
CYCLES = 2

# C0 values other than ordinary tab/newline/carriage return are binary glyph
# codes, not consumer text.  Some FERC PDFs omit a usable /ToUnicode map and
# therefore expose those codes through their content stream.  The raw PDF stays
# immutable in source_cache; this adapter records the affected codes and makes
# only its derived text portable.
_BINARY_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _binary_text_control_codes(text: str) -> list[str]:
    return sorted({f"U+{ord(match.group()):04X}"
                   for match in _BINARY_TEXT_CONTROL.finditer(text or "")})


def _portable_coordinate_text(text: str) -> str:
    """Return portable derived text without changing digits such as ``0``."""
    return re.sub(r"\s+", " ", _BINARY_TEXT_CONTROL.sub(" ", text or "")).strip()


def _control_problem(codes: list[str]) -> str:
    return (
        "the source PDF font has no usable character map for part of this "
        f"definition ({', '.join(codes)} occurred as binary glyph codes before "
        "portable-text normalisation), so the affected label/scope cannot be "
        "treated as semantically resolved"
    )

#: why an entity ended a retrieval with nothing -- our failure or FERC's silence
_RETRIEVAL_STATE: dict[str, tuple[str, str]] = {}

#: accession -> why an image-only source could not be published, so the record is
#: reported as parser/manual-review pending and never as a FERC source gap
_IMAGE_ONLY: dict[str, dict] = {}


def _description_names_filer(legal_name: str, description: str) -> bool:
    """Require the capacity-report description to name this exact filer.

    eLibrary text search is not a filer-key lookup. In particular, a query for
    ``MountainWest Pipeline`` can return ``MountainWest Overthrust Pipeline``.
    The capacity-report descriptions are machine generated as
    ``Annual Peak Day Capacity Report of <legal name> for <year>``; compare the
    complete non-suffix legal-name phrase at that boundary rather than accepting
    a bag of shared words.
    """
    words = lambda value: re.sub(r"[^a-z0-9]+", " ", value.lower()).split()
    name = words(legal_name or "")
    suffix = {"company", "corporation", "corp", "co", "inc", "llc", "lp",
              "limited", "l", "p", "c"}
    while name and name[-1] in suffix:
        name.pop()
    if not name:
        return False
    marker = words(SEARCH_PHRASE)
    described = words(description or "")
    width = len(marker) + len(name)
    target = marker + name
    return any(described[i:i + width] == target
               for i in range(0, len(described) - width + 1))


class ImageOnlySource(ValueError):
    """The bytes are a valid, public, legible FERC PDF with no text layer.

    Deliberately its own exception: the delivered adapter raised a bare
    ValueError here, the caller filed it as a `source` blocker, and a
    parser-capability gap of ours was thereby recorded as a FERC source failure.
    """

#: unit vocabulary seen in these reports. Read, never assumed.
UNIT_RE = re.compile(r"\b(MMBtu\s*/\s*D|MDth\s*/\s*d|MDth|Dth\s*/\s*d|Dth|MMscf\s*/\s*day|"
                     r"MMcf\s*/\s*d(?:ay)?|Bcf|Mcf|MMBtu)\b", re.I)
NUMBER_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")
PROSE_NUM_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(MMscf|MMcf|MDth|MMBtu|Bcf|Tcf|Mcf|Dth)"
    r"(\s*/\s*d(?:ay)?|\s+per\s+day|\s*/\s*D\b)?", re.I)
# Some born-digital reports emit a table value and its declared unit as one PDF
# text cell (``1,388,000 Dth``).  It is still a table cell, but NUMBER_RE alone
# cannot recognise it.  Keep this deliberately narrower than the prose matcher:
# the complete cell must be exactly one number followed by one known unit.
_INLINE_NUMBER_UNIT_RE = re.compile(
    rf"^\s*(?P<value>{NUMBER_RE.pattern[1:-1]})\s*"
    rf"(?P<unit>{UNIT_RE.pattern})\s*$", re.I)
AS_OF_RE = re.compile(r"as of\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})", re.I)
STORAGE_WORDS = re.compile(r"storage|LNG|peaking|LSS|GSS|SS-\d|S-\d\b|\bFS\b", re.I)

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


# ============================================================ PDF text layer

def _objects(raw: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", raw):
        end = raw.find(b"endobj", m.end())
        out[int(m.group(1))] = raw[m.end():end if end > 0 else len(raw)]
    for num, body in list(out.items()):                       # PDF 1.5 object streams
        if b"/ObjStm" not in body:
            continue
        data = _stream_data(body)
        n, first = _int_key(body, b"/N"), _int_key(body, b"/First")
        if not data or n is None or first is None:
            continue
        head = data[:first].split()
        for i in range(0, min(len(head) - 1, 2 * n), 2):
            try:
                onum, off = int(head[i]), int(head[i + 1])
            except ValueError:
                continue
            nxt = first + int(head[i + 3]) if i + 3 < len(head) else len(data)
            out.setdefault(onum, data[first + off:nxt])
    return out


def _int_key(body: bytes, key: bytes):
    m = re.search(key + rb"\s+(\d+)", body)
    return int(m.group(1)) if m else None


def _stream_data(body: bytes) -> bytes:
    m = re.search(rb"stream\r?\n", body)
    if not m:
        return b""
    end = body.find(b"endstream", m.end())
    raw = body[m.end():end if end > 0 else len(body)]
    if b"/FlateDecode" in body[:m.start()]:
        try:
            return zlib.decompressobj().decompress(raw)
        except zlib.error:
            return b""
    return raw


def _refs(body: bytes, key: bytes) -> list[int]:
    m = re.search(key + rb"\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", body)
    return [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1))] if m else []


def _resolve(objs, blob: bytes) -> bytes:
    m = re.fullmatch(rb"\s*(\d+)\s+\d+\s+R\s*", blob or b"")
    return objs.get(int(m.group(1)), b"") if m else (blob or b"")


def _dict_entries(body: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for m in re.finditer(
            rb"/([A-Za-z0-9#+._-]+)\s*(<<.*?>>|\[[^\]]*\]|\d+\s+\d+\s+R|/[^\s/\[\]<>()]+|[-\d.]+)",
            body or b"", re.S):
        out.setdefault(m.group(1).decode("latin-1"), m.group(2))
    return out


def _hex_to_str(h: bytes) -> str:
    s = h.decode("latin-1")
    return "".join(chr(int(s[i:i + 4], 16)) for i in range(0, len(s) - 3, 4))


def _tounicode(obj: bytes) -> dict[int, str]:
    """The authoritative decoding when the producer supplies it."""
    data = _stream_data(obj)
    if not data:
        return {}
    cmap: dict[int, str] = {}
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            cmap[int(src, 16)] = _hex_to_str(dst)
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            base = int(dst, 16)
            for i in range(int(lo, 16), min(int(hi, 16), int(lo, 16) + 65535) + 1):
                cmap[i] = chr(base + i - int(lo, 16))
    return cmap


def _balanced_array(body: bytes, key: bytes) -> bytes:
    r"""The value of `key` when it is an array that may nest, e.g.
    `/W[3[278]5[355]10[191 333]...]`. A non-nesting `\[[^\]]*\]` scan stops at
    the first inner `]` and silently returns one width for the whole font, which
    then drifts every x position on the page."""
    m = re.search(key + rb"\s*(\[|\d+\s+\d+\s+R)", body or b"")
    if not m:
        return b""
    if m.group(1) != b"[":
        return m.group(1)
    depth, i = 0, m.end(1) - 1
    while i < len(body):
        if body[i] == 0x5B:
            depth += 1
        elif body[i] == 0x5D:
            depth -= 1
            if depth == 0:
                return body[m.end(1) - 1:i + 1]
        i += 1
    return body[m.end(1) - 1:]


def _widths(objs, fd: dict, descendant: bytes | None) -> tuple[dict[int, float], float]:
    if descendant:
        dd = _dict_entries(descendant)
        w = _resolve(objs, _balanced_array(descendant, b"/W") or dd.get("W", b""))
        dw = dd.get("DW")
        default = float(dw) if dw and re.fullmatch(rb"[-\d.]+", dw) else 1000.0
        toks = re.findall(rb"\[|\]|[-\d.]+", w)
        table: dict[int, float] = {}
        i = 0
        while i < len(toks):
            if toks[i] in (b"[", b"]"):
                i += 1
                continue
            c = int(float(toks[i]))
            if i + 1 < len(toks) and toks[i + 1] == b"[":
                j, k = i + 2, 0
                while j < len(toks) and toks[j] != b"]":
                    table[c + k] = float(toks[j]); k += 1; j += 1
                i = j + 1
            elif i + 2 < len(toks):
                c2, val = int(float(toks[i + 1])), float(toks[i + 2])
                for cc in range(c, min(c2, c + 65535) + 1):
                    table[cc] = val
                i += 3
            else:
                break
        return table, default
    arr = _resolve(objs, _balanced_array(fd.get("_body", b""), b"/Widths")
                   or fd.get("Widths", b""))
    first = fd.get("FirstChar")
    first = int(float(first)) if first and re.fullmatch(rb"[-\d.]+", first) else 0
    vals = [float(x) for x in re.findall(rb"[-\d.]+", arr)]
    return {first + i: v for i, v in enumerate(vals)}, 500.0


def _fonts(objs, resources: bytes) -> dict[str, dict]:
    fdict = _resolve(objs, _dict_entries(_resolve(objs, resources)).get("Font", b""))
    out = {}
    for m in re.finditer(rb"/([A-Za-z0-9#+._-]+)\s+(\d+)\s+\d+\s+R", fdict):
        body = objs.get(int(m.group(2)), b"")
        fd = _dict_entries(body)
        fd["_body"] = body
        desc = None
        dm = re.search(rb"/DescendantFonts\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", body, re.S)
        if dm:
            desc = dm.group(1)
            for _ in range(3):
                if b"/BaseFont" in desc or b"/CIDFont" in desc:
                    break
                nxt = re.search(rb"(\d+)\s+\d+\s+R", desc)
                if not nxt:
                    break
                desc = objs.get(int(nxt.group(1)), b"")
        widths, default = _widths(objs, fd, desc)
        out["/" + m.group(1).decode("latin-1")] = {
            "two_byte": b"/Identity-H" in body or b"/Type0" in body,
            "cmap": _tounicode(_resolve(objs, fd.get("ToUnicode", b""))) if "ToUnicode" in fd
                    else {},
            "widths": widths, "default_width": default}
    return out


def pdf_pages(raw: bytes) -> list[dict]:
    objs = _objects(raw)
    order: list[int] = []
    root = next((n for n, b in objs.items() if b"/Catalog" in b), None)

    def walk(num, seen):
        if num in seen:
            return
        seen.add(num)
        body = objs.get(num, b"")
        if re.search(rb"/Type\s*/Pages", body):
            for k in _refs(body, b"/Kids"):
                walk(k, seen)
        elif re.search(rb"/Type\s*/Page[^s]", body):
            order.append(num)

    for t in (_refs(objs.get(root, b""), b"/Pages") if root is not None else []):
        walk(t, set())
    if not order:
        order = sorted(n for n, b in objs.items() if re.search(rb"/Type\s*/Page[^s]", b))
    out = []
    for i, numb in enumerate(order, 1):
        body = objs.get(numb, b"")
        content = b"".join(_stream_data(objs.get(c, b"")) + b"\n"
                           for c in _refs(body, b"/Contents"))
        rm = re.search(rb"/Resources\s*(<<.*?>>|\d+\s+\d+\s+R)", body, re.S)
        mb = re.search(rb"/MediaBox\s*\[([-\d.\s]+)\]", body)
        box = [float(x) for x in mb.group(1).split()] if mb else [0, 0, 612, 792]
        out.append({"page": i, "content": content, "height": (box[3] - box[1]) or 792.0,
                    "fonts": _fonts(objs, rm.group(1) if rm else b"")})
    return out


_TOKEN = re.compile(rb"""
    (?P<str>\((?:\\.|[^()\\]|\((?:\\.|[^()\\])*\))*\))
  | (?P<hex><[0-9A-Fa-f\s]*>)
  | (?P<arr>\[)
  | (?P<arrend>\])
  | (?P<num>[-+]?[\d.]+)
  | (?P<name>/[^\s/\[\]<>(){}]+)
  | (?P<op>[A-Za-z'"*][A-Za-z0-9'"*]*)
""", re.VERBOSE)

#: glyph fixups for shifted subset fonts that carry no /ToUnicode map
_FIXUP = {"\xd3": "’", "\xd0": "“", "\xd1": "”", "\xa3": "§",
          "\xd5": "’", "=": " "}
_WORDS = {"the", "and", "capacity", "report", "peak", "storage", "zone", "station",
          "total", "pipeline", "transport", "january", "february", "december",
          "company", "rate", "schedule", "annual", "design", "winter",
          "deliverability", "commission", "federal", "energy", "compliance"}


def _unescape(s: bytes) -> bytes:
    out, body, i = bytearray(), s[1:-1], 0
    while i < len(body):
        c = body[i]
        if c == 0x5C and i + 1 < len(body):
            nxt = body[i + 1]
            mapping = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
            if nxt in mapping:
                out.append(mapping[nxt]); i += 2
            elif 0x30 <= nxt <= 0x37:
                oct_ = body[i + 1:i + 4]
                k = 0
                while k < 3 and k < len(oct_) and 0x30 <= oct_[k] <= 0x37:
                    k += 1
                out.append(int(oct_[:k], 8) & 0xFF); i += 1 + k
            elif nxt in (10, 13):
                i += 2
            else:
                out.append(nxt); i += 2
        else:
            out.append(c); i += 1
    return bytes(out)


def _codes(raw: bytes, two_byte: bool) -> list[int]:
    if two_byte:
        if len(raw) % 2:
            raw += b"\x00"
        return [raw[i] << 8 | raw[i + 1] for i in range(0, len(raw), 2)]
    return list(raw)


def _show_operand(show, operand) -> None:
    """A text operand is a literal `(...)` string or a `<...>` hex string. Both
    carry glyphs; dropping the hex form silently loses whole sentences."""
    if not isinstance(operand, bytes):
        return
    if operand[:1] == b"(":
        show(_unescape(operand))
    elif operand[:1] == b"<":
        hx = re.sub(rb"[^0-9A-Fa-f]", b"", operand)
        if hx and len(hx) % 2 == 0:
            show(bytes.fromhex(hx.decode()))


def glyphs(page: dict) -> list[dict]:
    """One record per text-showing element, with its page position and advance."""
    content, fontmap = page["content"], page["fonts"]
    out: list[dict] = []
    ctm, stack = [1.0, 0, 0, 1.0, 0, 0], []
    tm = tlm = [1.0, 0, 0, 1.0, 0, 0]
    leading = tc = tw = 0.0
    th = 1.0
    font, size = "", 1.0
    operands: list = []
    arr: list | None = None

    def mul(a, b):
        return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
                a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
                a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]

    def fnum(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def show(raw: bytes):
        nonlocal tm
        f = fontmap.get(font, {})
        two = bool(f.get("two_byte"))
        widths, dw = f.get("widths", {}), f.get("default_width", 500.0)
        cmap = f.get("cmap") or {}
        start = mul(tm, ctm)
        buf = []
        for c in _codes(raw, two):
            ch = cmap.get(c)
            if ch is None:
                ch = chr(c & 0xFF) if not two else chr(c)
            buf.append(ch)
            adv = (widths.get(c, dw) / 1000.0 * size + tc +
                   (tw if ch == " " else 0.0)) * th
            tm = mul([1, 0, 0, 1, adv, 0], tm)
        if buf:
            out.append({"x": round(start[4], 2), "y": round(start[5], 2),
                        "size": abs(size * (tm[3] or 1)), "font": font,
                        "text": "".join(buf), "raw": raw, "seq": len(out),
                        "codes": _codes(raw, two),
                        "x_end": round(mul(tm, ctm)[4], 2)})

    for t in _TOKEN.finditer(content):
        kind, val = t.lastgroup, t.group()
        if kind == "arr":
            arr = []
            continue
        if kind == "arrend":
            operands.append(arr if arr is not None else [])
            arr = None
            continue
        if kind in ("str", "hex", "num", "name"):
            (arr if arr is not None else operands).append(val)
            continue
        op = val.decode("latin-1")
        try:
            if op == "q":
                stack.append(list(ctm))
            elif op == "Q":
                ctm = stack.pop() if stack else [1.0, 0, 0, 1.0, 0, 0]
            elif op == "cm" and len(operands) >= 6:
                ctm = mul([fnum(x) for x in operands[-6:]], ctm)
            elif op == "BT":
                tm = tlm = [1.0, 0, 0, 1.0, 0, 0]
            elif op == "Tf" and len(operands) >= 2:
                font, size = operands[-2].decode("latin-1"), fnum(operands[-1])
            elif op == "TL" and operands:
                leading = fnum(operands[-1])
            elif op == "Tc" and operands:
                tc = fnum(operands[-1])
            elif op == "Tw" and operands:
                tw = fnum(operands[-1])
            elif op == "Tz" and operands:
                th = fnum(operands[-1]) / 100.0
            elif op == "Tm" and len(operands) >= 6:
                tm = tlm = [fnum(x) for x in operands[-6:]]
            elif op in ("Td", "TD") and len(operands) >= 2:
                if op == "TD":
                    leading = -fnum(operands[-1])
                tlm = mul([1, 0, 0, 1, fnum(operands[-2]), fnum(operands[-1])], tlm)
                tm = list(tlm)
            elif op == "T*":
                tlm = mul([1, 0, 0, 1, 0, -leading], tlm)
                tm = list(tlm)
            elif op == "Tj" and operands:
                _show_operand(show, operands[-1])
            elif op in ("'", '"'):
                tlm = mul([1, 0, 0, 1, 0, -leading], tlm)
                tm = list(tlm)
                if operands:
                    _show_operand(show, operands[-1])
            elif op == "TJ" and operands and isinstance(operands[-1], list):
                for item in operands[-1]:
                    if isinstance(item, bytes) and item[:1] in (b"(", b"<"):
                        _show_operand(show, item)
                    else:
                        tm = mul([1, 0, 0, 1, -fnum(item) / 1000.0 * size * th, 0], tm)
        finally:
            operands = []
    return out


def repair_encoding(gl: list[dict], fontmap: dict) -> tuple[list[dict], dict[str, int]]:
    """Apply the +29 subset-font offset per font, and ONLY where the font carries
    no /ToUnicode map and the shift demonstrably produces English words."""
    by_font: dict[str, list[dict]] = {}
    for g in gl:
        by_font.setdefault(g["font"], []).append(g)
    shifts: dict[str, int] = {}
    for f, group in by_font.items():
        if (fontmap.get(f) or {}).get("cmap"):
            shifts[f] = 0                       # the producer supplied the true mapping
            continue
        codes = [c & 0xFF for g in group for c in g["codes"]]
        plain = re.sub(r"\s+", "", "".join(chr(c) for c in codes)).lower()
        shifted = re.sub(r"\s+", "",
                         "".join(chr((c + 29) & 0xFF) for c in codes)).lower()
        shifts[f] = 29 if (sum(shifted.count(w) for w in _WORDS)
                           > sum(plain.count(w) for w in _WORDS)) else 0
    for g in gl:
        s = shifts.get(g["font"], 0)
        g["shift"] = s
        if s:
            g["text"] = "".join(_FIXUP.get(ch, ch) for ch in
                                (chr(((c & 0xFF) + s) & 0xFF) for c in g["codes"]))
    return gl, shifts


def rows_of(gl: list[dict], ytol: float = 2.5) -> list[dict]:
    """Visual rows of cells. A cell break is a horizontal gap over 0.55 em."""
    rows: list[dict] = []
    for g in sorted(gl, key=lambda g: (-g["y"], g["seq"])):
        if not g["text"]:
            continue
        hit = next((r for r in rows if abs(r["y"] - g["y"]) <= ytol), None)
        if hit is None:
            rows.append({"y": g["y"], "_g": [g]})
        else:
            hit["_g"].append(g)
    rows.sort(key=lambda r: -r["y"])
    out = []
    for row in rows:
        # emission order IS reading order in every capacity report seen; only when
        # it is not left-to-right monotone do we fall back to sorting by x, and
        # the row is flagged so any figure taken from it is marked lower confidence
        seq_order = sorted(row["_g"], key=lambda g: g["seq"])
        monotone = all(b["x"] >= a["x"] - 6.0 for a, b in zip(seq_order, seq_order[1:]))
        gs = seq_order if monotone else sorted(row["_g"], key=lambda g: g["x"])
        row_control_codes = sorted({code for g in gs
                                    for code in _binary_text_control_codes(g["text"])})
        cells: list[dict] = []
        for g in gs:
            gap = g["x"] - cells[-1]["x_end"] if cells else 0.0
            if cells and gap < 0.55 * max(g["size"], 1.0):
                cells[-1]["text"] += g["text"]
                cells[-1]["x_end"] = max(cells[-1]["x_end"], g["x_end"])
                cells[-1]["binary_control_codes"] = sorted(set(
                    cells[-1]["binary_control_codes"]
                    + _binary_text_control_codes(g["text"])))
            else:
                cells.append({"x": g["x"], "x_end": g["x_end"], "text": g["text"],
                              "size": g["size"],
                              "binary_control_codes":
                                  _binary_text_control_codes(g["text"])})
        for c in cells:
            c["text"] = _portable_coordinate_text(c["text"])
        cells = [c for c in cells if c["text"]]
        if cells:
            out.append({"y": round(row["y"], 2), "cells": cells,
                        "reordered": not monotone,
                        "binary_control_codes": row_control_codes,
                        "text": _portable_coordinate_text(
                            " ".join(c["text"] for c in cells))})
    return out


def _normalise_capacity_doc_rows(pages: list[dict]) -> list[dict]:
    """Apply the portable-text contract to every capacity extraction route.

    Born-digital coordinate rows normally arrive here already normalised by
    :func:`rows_of`.  Image-only OCR and hash-bound reviewed rows have a separate
    producer and therefore require this common boundary as well.  Diagnostic
    ``ocr_raw_text`` remains untouched; when persisted inside JSON, ``json.dumps``
    escapes any control code instead of placing it in a SQLite/CSV text cell.
    """
    for page in pages:
        for row in page.get("rows") or []:
            codes = set(row.get("binary_control_codes") or [])
            codes.update(_binary_text_control_codes(str(row.get("text") or "")))
            cleaned_cells = []
            for cell in row.get("cells") or []:
                raw_text = str(cell.get("text") or "")
                cell_codes = set(cell.get("binary_control_codes") or [])
                cell_codes.update(_binary_text_control_codes(raw_text))
                codes.update(cell_codes)
                cell["text"] = _portable_coordinate_text(raw_text)
                cell["binary_control_codes"] = sorted(cell_codes)
                if cell["text"]:
                    cleaned_cells.append(cell)
            row["cells"] = cleaned_cells
            row_text = _portable_coordinate_text(str(row.get("text") or ""))
            row["text"] = row_text or _portable_coordinate_text(
                " ".join(cell["text"] for cell in cleaned_cells))
            row["binary_control_codes"] = sorted(codes)
    return pages


def extract_pdf(raw: bytes) -> list[dict]:
    out = []
    for p in pdf_pages(raw):
        gl, shifts = repair_encoding(glyphs(p), p["fonts"])
        out.append({"page": p["page"], "rows": rows_of(gl), "height": p["height"],
                    "shifted_fonts": sorted(f for f, s in shifts.items() if s)})
    return _normalise_capacity_doc_rows(out)


# ================================================================== A17
# IMAGE-ONLY SOURCES: A REVIEWED EXTRACTION PATH, NOT A SOURCE FAILURE.
#
# Two capacity reports in this universe -- Cadeville Gas Storage 20240223-5073
# and Monroe Gas Storage 20240223-5075 -- are valid, public, single-page FERC
# PDFs with NO text layer on any page. The delivered adapter raised, and the
# blocker it opened was recorded with kind="source". That is the wrong
# classification: FERC published the data, we could not read it. It is a
# PARSER-CAPABILITY GAP of ours.
#
# The path added here is deliberately bounded and deliberately not OCR:
#
#   1. The page is RENDERED to an image and READ BY A REVIEWER.
#   2. What the reviewer read is recorded verbatim below, keyed by the SHA-256
#      OF THE SOURCE BYTES. If FERC replaces the document, the hash stops
#      matching and the review expires rather than silently attaching to
#      different content.
#   3. The transcript is then fed through the SAME `read_figures` machinery as a
#      text-layer page. No number is typed in by hand: the value, the unit, the
#      qualifier and the sentence all come out of the normal reader, so the
#      reviewed path cannot assert something the transcript does not say.
#   4. Anything with no reviewed record stays explicitly parser/manual-review
#      pending. One reviewed sample never upgrades any other document.
#
# No OCR engine is used. If one is ever added, its output must be reviewed the
# same way and marked as OCR-derived; an unchecked OCR read is not evidence.

REVIEWED_IMAGE_METHOD = "reviewed_page_image_transcription"
IMAGE_OCR_METHOD = "macos_vision_ocr_reviewed_against_manual_transcription"
VISION_OCR_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "tools" / "vision_ocr.swift"

#: content_hash -> reviewed transcription of an image-only source.
#: `pages[i]["lines"]` is what the reviewer read on that page, in reading order.
REVIEWED_PAGE_IMAGES: dict[str, dict] = {
    "31f9219d3da5bc260392b6e2351cde0313ab38978ade4ee714f70308d7036d94": {
        "accession": "20240223-5073",
        "file_name": "2024.02.23 - CGS Section 284.13 Capacity Report.pdf",
        "byte_size": 337391,
        "page_count": 1,
        "render": "pdftoppm -r 150 -png <cached bytes>  (poppler 25.x)",
        "reviewed_by": "w5-documents, visual review of the rendered page",
        "reviewed_at": "2026-09-08",
        "ocr_used": False,
        "review_note": (
            "Rendered at 150 dpi and read on screen. The letterhead reads 'Cadeville Gas "
            "Storage'; the filer names itself 'Cadeville Gas Storage LLC (\"CGS\")'. Both "
            "figures are stated in ONE sentence and are of two different kinds: a DAILY "
            "DELIVERY RATE in MMcf/day and a STORAGE VOLUME in Bcf. The two qualifying "
            "sentences that follow are transcribed with them because they limit what the "
            "storage figure covers."),
        "pages": [{
            "page": 1,
            "lines": [
                "Cadeville Gas Storage",
                "February 23, 2024",
                "Via Electronic Filing",
                "Debbie-Ann Reese, Acting Secretary",
                "Federal Energy Regulatory Commission",
                "888 First Street, NE",
                "Washington, DC 20426",
                "RE: Cadeville Gas Storage LLC",
                "Section 284.13 Capacity Report",
                "Dear Ms. Reese:",
                "Section 284.13(d)(2) of the Federal Energy Regulatory Commission's "
                "regulations provides that an interstate pipeline must make an annual "
                "filing by March 1 of each year showing the estimated peak day capacity "
                "of the pipeline's system, and the estimated storage capacity and maximum "
                "daily delivery capability of storage facilities under reasonably "
                "representative operating assumptions and the respective assignments of "
                "that capacity to the various firm services provided by the pipeline.",
                "Accordingly, Cadeville Gas Storage LLC (“CGS”) hereby submits its "
                "annual filing by March 1, 2024. The estimated peak day delivery capacity "
                "of CGS’s facilities is approximately 420 MMcf/day, and its estimated "
                "total storage capacity is 23.7 Bcf. All of CGS’s capacity, except for "
                "capacity associated with base gas requirements, is assigned to, or made "
                "available for the provision of firm services. The estimated storage "
                "capacity total does not include any off-system capacity.",
                "Respectfully submitted,",
                "Justin Joyce",
            ]}],
    },
    "8e2069eeb07fdfdca7167b323b25cf91f0c34bbf91fd82a8bd82caf9826c0040": {
        "accession": "20240223-5075",
        "file_name": "2024.02.23 - MGS Section 284.13 Capacity Report.pdf",
        "byte_size": 339731,
        "page_count": 1,
        "render": "pdftoppm -r 150 -png <cached bytes>  (poppler 25.x)",
        "reviewed_by": "w5-documents, visual review of the rendered page",
        "reviewed_at": "2026-09-08",
        "ocr_used": False,
        "review_note": (
            "Same form of letter as the Cadeville filing and the same two kinds of figure. "
            "Note the wording differs slightly from its sibling -- Monroe writes 'off "
            "system capacity' without the hyphen and 'assigned to or made available' "
            "without the comma -- which is why each letter is transcribed separately rather "
            "than one being copied onto the other."),
        "pages": [{
            "page": 1,
            "lines": [
                "Monroe Gas Storage",
                "February 23, 2024",
                "Via Electronic Filing",
                "Debbie-Ann Reese, Acting Secretary",
                "Federal Energy Regulatory Commission",
                "888 First Street, NE",
                "Washington, DC 20426",
                "RE: Monroe Gas Storage Company, LLC",
                "Section 284.13 Capacity Report",
                "Dear Ms. Reese:",
                "Section 284.13(d)(2) of the Federal Energy Regulatory Commission's "
                "regulations provides that an interstate pipeline must make an annual "
                "filing by March 1 of each year showing the estimated peak day capacity "
                "of the pipeline's system, and the estimated storage capacity and maximum "
                "daily delivery capability of storage facilities under reasonably "
                "representative operating assumptions and the respective assignments of "
                "that capacity to the various firm services provided by the pipeline.",
                "Accordingly, Monroe Gas Storage Company, LLC (“MGS”) hereby submits "
                "its annual filing by March 1, 2024. The estimated peak day delivery "
                "capacity of MGS’s facilities is approximately 465 MMcf/day, and its "
                "estimated total storage capacity is 11.96 Bcf. All of MGS’s capacity, "
                "except for capacity associated with base gas requirements, is assigned to "
                "or made available for the provision of firm services. The estimated "
                "storage capacity total does not include any off system capacity.",
                "Respectfully submitted,",
                "Justin Joyce",
            ]}],
    },
}


def reviewed_pages(content_hash: str) -> list[dict] | None:
    """The reviewed transcript of an image-only source, in `extract_pdf` shape.

    Returns None when this exact byte content has not been reviewed -- which is
    the answer for every image-only document except the two above, and is what
    keeps a single successful review from upgrading anything else.
    """
    record = REVIEWED_PAGE_IMAGES.get(content_hash)
    if not record:
        return None
    out = []
    for page in record["pages"]:
        rows, y = [], 700.0
        for line in page["lines"]:
            text = re.sub(r"\s+", " ", line).strip()
            if not text:
                continue
            rows.append({"y": round(y, 2),
                         "cells": [{"x": 90.0, "x_end": 90.0 + 5.4 * len(text),
                                    "text": text, "size": 11.0}],
                         "reordered": False, "text": text})
            y -= 12.0
        out.append({"page": page["page"], "rows": rows, "height": 792.0,
                    "shifted_fonts": []})
    return out


def _review_signature(figures: list[dict]) -> list[tuple]:
    """Semantic signature used only to compare OCR with independent review.

    Values are still selected by ``read_figures`` from the OCR text. This is a
    fail-closed review check, not a source of replacement values.
    """
    out = []
    for figure in figures:
        caveats = " ".join(figure.get("caveats") or [])
        out.append((
            figure.get("value_text"),
            figure.get("unit"),
            figure.get("qualifier"),
            bool(re.search(r"BASE GAS", caveats, re.I)),
            bool(re.search(r"OFF-SYSTEM", caveats, re.I)),
            bool(re.search(r"OPERATING ASSUMPTIONS", caveats, re.I)),
        ))
    return sorted(out, key=lambda row: (str(row[0]), str(row[1])))


# ============================================================ figure reading

def _is_number(text: str) -> bool:
    return bool(NUMBER_RE.match(text.strip()))


def _num(text: str):
    t = text.strip().replace(",", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _unit_in(*texts: str) -> str:
    for t in texts:
        m = UNIT_RE.search(t or "")
        if m:
            return re.sub(r"\s+", "", m.group(1))
    return ""


def _numeric_cell(cell: dict) -> dict | None:
    """Return a numeric view of one cell without altering its filed text row."""
    text = str(cell.get("text") or "").strip()
    if _is_number(text):
        return cell
    match = _INLINE_NUMBER_UNIT_RE.fullmatch(text)
    if match is None:
        return None
    value = dict(cell)
    value["text"] = match.group("value")
    value["inline_unit"] = _unit_in(match.group("unit"))
    return value


#: A parenthesised token in a column or section header that is plainly meant as a
#: unit: "(MMDth)", "(Dth/d)", "(MDth/d)". Used to detect that a column HAS
#: declared its unit even when this reader's vocabulary does not contain it.
_UNITISH = re.compile(r"\(\s*([A-Za-z][A-Za-z0-9]{0,8}(?:\s*/\s*[A-Za-z]{1,4})?)\s*\)")


def _declared_unit_token(*texts: str) -> str:
    """The unit this column declares for itself, recognised or not."""
    for t in texts:
        for m in _UNITISH.finditer(t or ""):
            token = re.sub(r"\s+", "", m.group(1))
            if re.search(r"th|btu|cf|bbl|mcf", token, re.I):
                return token
    return ""


def _overlap(a: dict, b: dict) -> bool:
    return not (a["x_end"] < b["x"] - 1.0 or b["x_end"] < a["x"] - 1.0)


def _as_of(pages: list[dict], report_year: int | None) -> tuple[str, str]:
    """(iso date, how it was established). Never invented."""
    for p in pages:
        for row in p["rows"]:
            m = AS_OF_RE.search(row["text"])
            if m:
                iso = _parse_long_date(m.group(1))
                if iso:
                    return iso, f"stated on page {p['page']}: {m.group(0)!r}"
    if report_year:
        return f"{report_year:04d}-12-31", (
            f"no 'as of' date is stated in the document; the end of the report year "
            f"{report_year} is used and labelled as such")
    return "", "no as-of date could be established from the document"


_REPORT_YEAR_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b(?:annual\s+)?(?:estimated\s+)?peak[\s-]*day\s+capacity\s+report\b"
    r"[^0-9\r\n]{0,40}\b((?:19|20)\d{2})\b",
    r"\breport\s+for\s+(?:the\s+)?(?:calendar\s+)?year\s+((?:19|20)\d{2})\b",
    r"\b((?:19|20)\d{2})\s+(?:annual\s+)?(?:system\s+capacity\s+report|"
    r"(?:report\s+of\s+)?(?:estimated\s+)?peak[\s-]*day\s+capacity(?:\s+report)?|"
    r"annual\s+report\s+of\s+capacity)\b",
    r"\bannual\s+(?:system\s+capacity\s+report|report\s+of\s+(?:estimated\s+)?"
    r"peak[\s-]*day\s+capacity|report\s+of\s+capacity)\b[^0-9\r\n]{0,40}"
    r"\b((?:19|20)\d{2})\b",
))

_FILENAME_REPORT_YEAR_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"(?:^|[^A-Za-z0-9])Y(?:E)?[ _-]*((?:19|20)\d{2})(?:[^0-9]|$)",
    r"\b(?:capacity|PDC)[^0-9]{0,24}((?:19|20)\d{2})\b",
    r"\b((?:19|20)\d{2})\s+annual\b",
    r"\breport\s*\(\s*((?:19|20)\d{2})\s*\)",
))

_CAPACITY_PERIOD_YEAR_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b(?:capacity|capabilities|facility)\b.{0,180}\bfor\s+"
    r"(?:the\s+)?(?:calendar\s+year\s+)?((?:19|20)\d{2})\s+"
    r"(?:was|were|is|are)\b",
))


# A report heading and its attachment filename are commonly copied from the
# same filer-authored label, so their agreement is not independent evidence.
# Corrections for proven source anomalies are therefore deliberately byte-bound:
# a replacement PDF cannot inherit a decision made about earlier bytes.
_REVIEWED_REPORT_YEAR_CORRECTIONS = {
    (
        "20260113-5139",
        "05bd59b0837447be42bba68b4f764e8192063c76c7539072d88c2ff90f1ad00a",
    ): {
        "report_year": 2026,
        "basis": (
            "reviewed correction bound to accession 20260113-5139 and exact "
            "PDF SHA-256 05bd59b0837447be42bba68b4f764e8192063c76c7539072d88c2ff90f1ad00a"
        ),
    },
    (
        "20260226-5099",
        "b24a4437cf4a94dcd9ffbeee05a6f54555bca0b60a9afe54b18e69fb9ccb31d2",
    ): {
        "report_year": 2026,
        "basis": (
            "reviewed correction bound to accession 20260226-5099 and exact "
            "PDF SHA-256 b24a4437cf4a94dcd9ffbeee05a6f54555bca0b60a9afe54b18e69fb9ccb31d2"
        ),
    },
}


def _report_year_from_document(pages: list[dict], candidate: dict, *,
                               content_hash: str = ""
                               ) -> tuple[int, str, list[str]]:
    """Resolve a cycle without treating duplicated labels as corroboration.

    Exact prose tying a year to a reported capacity value controls generically.
    Otherwise the eLibrary cycle remains authoritative unless an anomaly has a
    reviewed accession-and-hash-bound correction.  Headings and filenames are
    retained as QA signals, but never vote one another into a different cycle.
    No year is inferred from the filing date or an as-of date.
    """
    rows = [str(row.get("text") or "")
            for page in pages for row in page.get("rows") or []]
    nearby = rows + [f"{rows[index]} {rows[index + 1]}"
                     for index in range(len(rows) - 1)]
    period_years = {
        int(match.group(1))
        for text in nearby
        for pattern in _CAPACITY_PERIOD_YEAR_PATTERNS
        for match in pattern.finditer(text)
    }
    if len(period_years) > 1:
        raise ValueError(
            "capacity report contains conflicting years tied to reported capacity "
            f"values: {sorted(period_years)}")
    heading_years = {
        int(match.group(1))
        for text in rows
        for pattern in _REPORT_YEAR_PATTERNS
        for match in pattern.finditer(text)
    }
    if len(heading_years) > 1:
        raise ValueError(
            "capacity report contains conflicting reporting years in its headings: "
            f"{sorted(heading_years)}")

    accession = str(candidate.get("accession") or "")
    normalised_hash = str(content_hash or "").lower()
    correction = _REVIEWED_REPORT_YEAR_CORRECTIONS.get(
        (accession, normalised_hash))
    reviewed_hashes = {
        reviewed_hash
        for reviewed_accession, reviewed_hash in _REVIEWED_REPORT_YEAR_CORRECTIONS
        if reviewed_accession == accession
    }
    if reviewed_hashes and correction is None:
        raise ValueError(
            f"capacity report {accession} has a reviewed report-year correction, "
            "but the PDF SHA-256 does not match the reviewed bytes")

    description_year = candidate.get("report_year")
    filename_years = {
        int(match.group(1))
        for item in candidate.get("pdfs") or []
        for pattern in _FILENAME_REPORT_YEAR_PATTERNS
        for match in pattern.finditer(str(item.get("fileName") or ""))
    }
    if len(filename_years) > 1:
        raise ValueError(
            "capacity filing attachment names identify conflicting report years: "
            f"{sorted(filename_years)}")
    filename_year = next(iter(filename_years), None)

    signals = {}
    if period_years:
        # This is not a cover-sheet label: it is a year grammatically attached
        # to the capacity being reported, so it controls conflicting metadata.
        year = next(iter(period_years))
        basis = "year tied to the reported capacity value in the filed document"
        signals["filed document value period"] = year
        if correction is not None and year != correction["report_year"]:
            raise ValueError(
                f"capacity report {accession} exact value-period year {year} "
                "conflicts with its reviewed accession/hash-bound correction "
                f"{correction['report_year']}")
    elif correction is not None:
        year = int(correction["report_year"])
        basis = str(correction["basis"])
        signals["reviewed accession/hash correction"] = year
    elif description_year is not None:
        year = int(description_year)
        basis = "eLibrary description"
        signals["eLibrary description"] = year
    elif heading_years:
        year = next(iter(heading_years))
        if filename_year is not None and filename_year != year:
            raise ValueError(
                "capacity filing report-year evidence disagrees: "
                f"filed report heading={year}, attachment filename={filename_year}")
        basis = "unambiguous filed report heading"
        signals["filed report heading"] = year
    else:
        if filename_year is not None:
            raise ValueError(
                "capacity filing has only an attachment-filename report year; "
                "filename labels do not establish the reporting cycle")
        raise ValueError(
            "capacity filing has no report year in exact value-period language, "
            "its description, or an unambiguous filed report heading")

    if heading_years:
        signals["filed report heading"] = next(iter(heading_years))
    if description_year is not None:
        signals["eLibrary description"] = int(description_year)
    if filename_year is not None:
        signals["report-specific attachment filename"] = filename_year
    disagreements = [
        f"{source} says {value}, but resolved report year is {year}"
        for source, value in sorted(signals.items()) if value != year
    ]
    return year, basis, disagreements


def _reviewed_image_report_year(ocr_pages: list[dict], reviewed_pages_: list[dict],
                                candidate: dict, *, content_hash: str
                                ) -> tuple[int, str, list[str]]:
    """Require OCR and the independent hash-bound transcript to resolve alike."""
    try:
        ocr_resolution = _report_year_from_document(
            ocr_pages, candidate, content_hash=content_hash)
        reviewed_resolution = _report_year_from_document(
            reviewed_pages_, candidate, content_hash=content_hash)
    except ValueError as exc:
        raise ImageOnlySource(
            "image-only capacity report year could not be established by both OCR "
            f"and the independent hash-bound review: {exc}. No value is published."
        ) from exc
    if ocr_resolution[0] != reviewed_resolution[0]:
        raise ImageOnlySource(
            "image-only capacity report year disagrees between OCR and the "
            f"independent hash-bound review: OCR={ocr_resolution[0]}, "
            f"review={reviewed_resolution[0]}. No value is published.")
    return ocr_resolution


def _parse_long_date(text: str) -> str:
    m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text.strip())
    if not m or m.group(1).lower() not in MONTHS:
        return ""
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


#: A single-cell row immediately above a table IS its section header -- and it is
#: the commonest form of one. The pre-repair reader took the section only from a
#: header row with two or more cells, so every lone title line was discarded and
#: whole blocks of a report collapsed onto one "unsectioned" scope. That is what
#: put Northwest Pipeline's Plymouth LNG "Working Gas 2,388,000" and its Jackson
#: Prairie "Working Gas 8,528,000" on the same grain, and Florida Gas's Winter
#: and Summer "Total" rows on another. These guards keep prose, footnote markers
#: and spanning unit banners out of the section slot.
_SECTION_MAX_CHARS = 70
_FOOTNOTE_MARKER = re.compile(r"^[\d\W_]+$")
#: A superscript footnote reference sitting on its own line between a table's
#: header and its data: "1", "3", "1,2". It is not a section, not a column header
#: and not the start of a new table. Treating one as a label row reset
#: MountainWest's column headers away from the row beneath it, so a storage
#: VOLUME in MMDth and a daily delivery RATE in Dth/d both arrived with no column
#: at all.
_LONE_FOOTNOTE = re.compile(r"^\d{1,2}(?:\s*,\s*\d{1,2})*$")
#: A lone header cell may label a COLUMN only if it is narrow enough to be one.
#: A centred page title overlaps every value column on the page; a unit banner
#: such as "(Dth/d)" spans about 35pt.
_COLUMN_HEADER_MAX_WIDTH = 120.0


def _is_unit_definition_footnote(row: dict) -> bool:
    """True only for a numbered footnote that defines a reported unit.

    A definition such as ``1) The term Dth ... is the quantity ...`` contains a
    numeric marker, prose and a unit, which otherwise looks exactly like a
    labelled capacity row.  Requiring both a marker-only first cell and explicit
    definition language keeps numbered route/service rows in the table.
    """
    cells = row.get("cells") or []
    if len(cells) < 2:
        return False
    marker = str(cells[0].get("text") or "").strip()
    explanation = " ".join(
        str(cell.get("text") or "") for cell in cells[1:])
    return bool(
        re.search(r"\d", marker)
        and _FOOTNOTE_MARKER.fullmatch(marker)
        and re.match(r"^\s*the\s+term\b", explanation, re.I)
        and UNIT_RE.search(explanation)
        and re.search(
            r"\b(?:means?|refers\s+to|is\s+(?:the|a|an)\b)",
            explanation,
            re.I,
        )
    )


_EXPLICIT_SYSTEM_CAPACITY_LABEL = re.compile(
    r"^(?:system\s+)?peak[\s-]+day\s+capacity$", re.I)


def _is_system_capacity_total(row_label: str) -> bool:
    """Whether a table label itself identifies the system-capacity total.

    Most filers use a row called ``Total`` or ``Total Estimated Peak Day
    Capacity``.  Gulfstream instead puts ``PEAK DAY CAPACITY`` on a spanning
    label line immediately above its value; its separate ``TOTAL ESTIMATED FIRM
    PEAK DAY OBLIGATION`` is the assigned firm obligation, not capacity.  Keep
    this distinction structural and label-bound rather than selecting by value
    equality or page order.
    """
    label = " ".join(str(row_label or "").split())
    if _EXPLICIT_SYSTEM_CAPACITY_LABEL.fullmatch(label):
        return True
    return bool(
        re.search(r"\btotal\b", label, re.I)
        and not re.search(r"\bobligation\b", label, re.I)
    )


def _section_candidate(row: dict, page_unit_note: str) -> str:
    """A header row's text when it can serve as a section title, else ''.

    A trailing colon is a STRONGER signal of a heading, not a weaker one --
    Discovery's report is built entirely out of them ("Peak day capacity:",
    "Firm Transportation Services:", "FT-1:") -- so it is stripped, not rejected.
    A trailing full stop means a sentence and is rejected.
    """
    text = row["cells"][0]["text"].strip() if row["cells"] else ""
    if len(row["cells"]) > 1:
        # a column-header row: its first cell is the row-label column's title
        text = row["cells"][0]["text"].strip()
    if not text or len(text) > _SECTION_MAX_CHARS:
        return ""                       # a paragraph of narrative is not a title
    if text.endswith("."):
        return ""                       # a sentence is not a title
    text = text.rstrip(":").strip()
    if not text or _FOOTNOTE_MARKER.match(text):
        return ""                       # "1", "2", "3" -- footnote markers
    if not re.search(r"[A-Za-z]", text):
        return ""
    if text == page_unit_note.strip().rstrip(":"):
        return ""                       # the spanning unit banner is not a section
    if UNIT_RE.fullmatch(re.sub(r"[()_\s]", "", text) or "x"):
        return ""                       # a bare unit, e.g. "(Dth/d)"
    return text


def _push_outline(outline: list[tuple[float, str, tuple[str, ...]]], row: dict,
                  page_unit_note: str) -> None:
    """Maintain the document's OUTLINE as an indent stack.

    A capacity report is a nested list, and its nesting is expressed by
    indentation. Discovery's onshore-mainline figure appears three times -- once
    as design capacity and once under each of two rate schedules -- and only the
    outline tells them apart:

        Peak day capacity:                 x=126
          Mainline Facilities (Onshore)    x=144   300,000
        Firm Transportation Services:      x=126
          FT-1:                            x=144
            Mainline Facilities (Onshore)  x=162   188,572
          FT-2:                            x=144
            Mainline Facilities (Onshore)  x=162    17,583

    The stack is kept SEPARATELY from `header_stack`, which exists to resolve
    COLUMN headers and must reset at every table boundary. Resetting the outline
    there too is what lost "Firm Transportation Services" for the FT-2 block.
    """
    title = _section_candidate(row, page_unit_note)
    if not title:
        return
    x = row["cells"][0]["x"]
    while outline and outline[-1][0] >= x - 1.0:
        outline.pop()                   # a sibling or an outer level closes it
    outline.append((x, title, tuple(row.get("binary_control_codes") or ())))


def _outline_path(outline: list[tuple[float, str, tuple[str, ...]]],
                  label_x: float | None) -> str:
    """The titles that GOVERN a data row: those indented left of its own label."""
    if label_x is None:
        return " / ".join(t for _x, t, _codes in outline)
    return " / ".join(t for x, t, _codes in outline if x < label_x - 1.0)


def _outline_control_codes(
        outline: list[tuple[float, str, tuple[str, ...]]],
        label_x: float | None) -> list[str]:
    members = outline if label_x is None else [
        item for item in outline if item[0] < label_x - 1.0]
    return sorted({code for _x, _text, codes in members for code in codes})


def read_figures(pages: list[dict], report_year: int | None) -> list[dict]:
    """Every numeric figure in the report, with its own definition, direction,
    unit, page evidence and confidence.

    Column assignment is geometric: a value cell takes the labels of the cells it
    overlaps in the header rows above it. Where no label or no unit can be found,
    or the row's glyphs were not emitted in reading order, the figure is kept with
    the ambiguity named and its numeric value withheld.

    Section assignment is hierarchical: the nearest header row above the data
    that can serve as a title, whether it carries two cells ("Storage Capacity |
    (MDth) | (MDth/d)") or one ("Jackson Prairie Storage"). The section is part
    of the figure's identity, because "Working Gas" means a different quantity
    under each of two storage facilities.
    """
    figures: list[dict] = []
    for p in pages:
        page_unit_note = ""
        section = ""
        header_stack: list[dict] = []
        outline: list[tuple[float, str, tuple[str, ...]]] = []
        section_control_codes: list[str] = []
        page_unit_control_codes: list[str] = []
        after_data = False
        table_index = 0
        prose_block: list[dict] = []

        def flush_prose(block=prose_block):
            out = _prose_figures(p, block)
            block.clear()
            return out

        height = p.get("height") or 792.0
        # A08: every figure must be traceable to the persisted source-fact row it
        # came from, so the row's index (which is what `_fact_id` keys on) travels
        # with the figure rather than being recomputed from a y coordinate later.
        for idx, row in enumerate(p["rows"]):
            row.setdefault("row_index", idx)
        for row in p["rows"]:
            # running headers and footers carry page numbers and filing dates, not
            # capacity figures. The top and bottom 8% of the page are excluded and
            # the exclusion is stated here rather than left as a silent filter.
            if row["y"] > 0.92 * height or row["y"] < 0.06 * height:
                continue
            if _is_unit_definition_footnote(row):
                continue
            classified = [(cell, _numeric_cell(cell)) for cell in row["cells"]]
            value_cells = [numeric for _cell, numeric in classified
                           if numeric is not None]
            label_cells = [cell for cell, numeric in classified if numeric is None]
            # A data row must carry a label of its own, or at least two values
            # sitting under a header. A lone number on a line is a page number or
            # a footnote marker, not a capacity figure.
            is_data = bool(value_cells) and (
                bool(label_cells) or len(value_cells) >= 2
                # A number with its own filed unit cannot be a bare page number
                # or superscript marker.  This admits a value split onto the line
                # immediately below its label while leaving lone numbers gated.
                or any(cell.get("inline_unit") for cell in value_cells)
            )
            if (not is_data and len(row["cells"]) == 1
                    and _LONE_FOOTNOTE.match(row["text"].strip())):
                continue        # a footnote marker: not a header, not a table break
            if not is_data:
                # a label row: a section header, a column header, a spanning unit
                # banner such as "Winter Day Design (MDth/d)", or prose. A label row
                # arriving AFTER a data row starts a new table, so the header stack
                # is reset there and only there -- otherwise every row of a table
                # after the first would lose its column headers and its unit.
                if after_data:
                    header_stack.clear()
                    section = ""
                    section_control_codes = []
                    after_data = False
                    table_index += 1
                if len(row["cells"]) == 1 and UNIT_RE.search(row["text"]):
                    page_unit_note = row["text"]
                    page_unit_control_codes = list(row.get("binary_control_codes") or [])
                _push_outline(outline, row, page_unit_note)
                header_stack.append(row)
                header_stack[:] = header_stack[-4:]
                prose_block.append(row)
                continue

            figures.extend(flush_prose())
            after_data = True
            row_label = label_cells[0]["text"] if label_cells else ""
            label_x = label_cells[0]["x"] if label_cells else None
            # The section is named by the header row NEAREST the data. A header
            # with a row-label column plus value columns wins ("Storage Capacity |
            # (MDth) | (MDth/d)", not the "Space | Deliverability" banner above
            # it); failing that, the nearest lone title line does -- "Jackson
            # Prairie Storage", "Summer Season Firm Service Assignment". Only
            # when neither exists is the block genuinely unsectioned.
            sec_rows = [h for h in header_stack if len(h["cells"]) >= 2]
            if sec_rows:
                section = sec_rows[-1]["cells"][0]["text"]
                section_basis = "column header row"
                section_control_codes = list(
                    sec_rows[-1].get("binary_control_codes") or [])
            else:
                path = _outline_path(outline, label_x)
                if path:
                    section, section_basis = path, "outline of title lines above the block"
                    section_control_codes = _outline_control_codes(outline, label_x)
                elif section:
                    section_basis = "carried from the preceding block"
                else:
                    section_basis = ""
            row_label = row_label or section
            label_from_section = not label_cells
            for ordinal, c in enumerate(sorted(value_cells, key=lambda v: v["x"]), 1):
                cols = []
                column_control_codes: set[str] = set()
                # The header BLOCK above this table, not a fixed pair of rows.
                # MountainWest's storage table has a three-row header --
                # "Storage Service | Estimated Storage | Maximum Daily" /
                # "Rate Schedule | Capacity (MMDth) | Delivery Capability" /
                # "(Dth/d)" -- and a two-row window dropped the row that says
                # which of the two values is a volume and which is a rate.
                for hrow in header_stack[-3:]:
                    lone = len(hrow["cells"]) == 1
                    for hc in hrow["cells"]:
                        # a column header is short; a paragraph that happens to sit
                        # above the table is not a column label
                        if len(hc["text"]) > 45 or _is_number(hc["text"]):
                            continue
                        # a LONE header cell wide enough to span the page is a
                        # title, not a column label
                        if lone and hc["x_end"] - hc["x"] > _COLUMN_HEADER_MAX_WIDTH:
                            continue
                        if _overlap(hc, c) and hc["text"] not in cols:
                            cols.append(hc["text"])
                            column_control_codes.update(
                                hrow.get("binary_control_codes") or [])
                column = " / ".join(cols[:3])
                # A spanning unit banner belongs to the table it sits over. Where
                # a column DECLARES its own unit, the banner is never substituted
                # for it -- even when this reader cannot parse the declared one.
                # MountainWest states its storage capacity in "(MMDth)", a volume;
                # borrowing the transport table's "(Dth/d)" banner would turn a
                # storage volume into a daily rate.
                unit = c.get("inline_unit") or _unit_in(column, section)
                declared = _declared_unit_token(column, section)
                problems = []
                source_control_codes = set(row.get("binary_control_codes") or [])
                source_control_codes.update(section_control_codes)
                source_control_codes.update(column_control_codes)
                if not unit and declared:
                    problems.append(
                        f"the column declares its unit as '{declared}', which this reader's "
                        f"unit vocabulary does not contain. The spanning banner "
                        f"{page_unit_note.strip()!r} is NOT applied to it, because a banner "
                        f"belongs to the table it sits over and this column has stated a "
                        f"different unit")
                elif not unit:
                    unit = _unit_in(page_unit_note)
                    if unit:
                        source_control_codes.update(page_unit_control_codes)
                if source_control_codes:
                    problems.append(_control_problem(sorted(source_control_codes)))
                if row["reordered"]:
                    problems.append("glyphs on this row were not emitted in reading order, "
                                    "so the cell split is not certain")
                if not unit:
                    problems.append("no unit could be resolved from the column header, the "
                                    "section header or a spanning unit banner")
                if not column and not row_label:
                    problems.append("no column or row label overlaps this value")
                if not column and len(value_cells) > 1:
                    # MountainWest's "FSS 55.75 810,000": one row, two values, no
                    # column header over either. A spanning unit banner would be
                    # applied to both, but 55.75 and 810,000 are plainly not the
                    # same kind of quantity, and which is which cannot be
                    # established. The value is retained; the number is withheld.
                    problems.append(
                        f"this row carries {len(value_cells)} values and no column header "
                        f"overlaps this one, so which quantity it is -- and whether the "
                        f"spanning unit applies to it -- cannot be established from the "
                        f"document")
                figures.append({
                    "kind": "table",
                    "page": p["page"], "y": row["y"],
                    "row_index": row.get("row_index", 0),
                    "table_index": table_index, "column_ordinal": ordinal,
                    "value_cell_count": len(value_cells),
                    "section": section, "section_basis": section_basis,
                    "row_label": row_label,
                    "column": column, "unit": unit,
                    "value_text": c["text"],
                    "value_num": _num(c["text"]) if not problems else None,
                    "x": c["x"], "x_end": c["x_end"],
                    "verbatim": row["text"],
                    "char_start": row["text"].find(c["text"]),
                    "char_end": row["text"].find(c["text"]) + len(c["text"]),
                    "qualifier": ("estimated" if re.search(r"estimat", row["text"] + section,
                                                           re.I) else ""),
                    "is_total": _is_system_capacity_total(row_label),
                    "label_from_section": label_from_section,
                    "unit_note": "" if c.get("inline_unit") else page_unit_note,
                    "binary_control_codes": sorted(source_control_codes),
                    "discriminator": "",
                    "problems": problems})
        figures.extend(flush_prose())
    _resolve_identical_definitions(figures)
    return figures


def _resolve_identical_definitions(figures: list[dict]) -> None:
    """Guarantee that no two figures in one document share a published identity.

    The observation grain is (entity, metric, regime, basis, instant, scope,
    unit, method). If two figures reduce to the same scope and unit with
    DIFFERENT values, the grain cannot tell them apart and the writer would keep
    whichever arrived first -- choosing between two materially different capacity
    figures on iteration order. Northwest Pipeline's two "Working Gas" rows are
    2,388,000 and 8,528,000, a factor of 3.6 apart.

    Section recovery removes almost all of these, because the rows really do have
    different definitions in the document. Anything still colliding afterwards is
    a definition this reader could not separate, and it is handled by saying so:

      * the figures are made DISTINCT by their structural position, so both
        survive and neither is deleted;
      * every one of them is marked ambiguous, so `value_num` is withheld and the
        consumer sees an unresolved figure rather than a confident wrong one.

    The position is a discriminator, never a tiebreak: no figure is preferred
    over another, and every member of a colliding group is flagged.
    """
    groups: dict[tuple, list[dict]] = {}
    for fig in figures:
        # `kind` is part of the identity: a narrative sentence and a table cell
        # are different assertions even when they carry the same words, and the
        # module never merges them.
        groups.setdefault((fig["kind"], fig["section"], fig["row_label"],
                           fig["column"], fig["unit"] or ""), []).append(fig)
    for key, group in groups.items():
        if len(group) < 2 or len({g["value_text"] for g in group}) < 2:
            continue
        others = sorted({g["value_text"] for g in group})
        for fig in group:
            where = (f"page {fig['page']}, "
                     + (f"table {fig.get('table_index', 0) + 1}, "
                        f"row {fig.get('row_index', 0)}, "
                        f"column {fig.get('column_ordinal', 1)}"
                        if fig.get("table_index", -1) >= 0 else
                        f"narrative, row {fig.get('row_index', 0)}, "
                        f"character {fig.get('char_offset', 0)}"))
            fig["discriminator"] = f"DEFINITION NOT SEPARATED: {where}"
            fig["problems"].append(
                f"{len(group)} figures in this document reduce to the same definition "
                f"({' :: '.join(k for k in key[1:4] if k) or 'no label at all'}) and the same "
                f"unit but carry different values ({', '.join(others)}). No section, column "
                f"or unit in the document separates them, so this reader cannot say which "
                f"quantity this is. All of them are retained with their page positions and "
                f"NONE is published as the answer")
            fig["value_num"] = None


#: boundaries between two assertions inside one sentence. Cadeville states both
#: of its figures in a single sentence -- "the estimated peak day delivery
#: capacity ... is approximately 420 MMcf/day, and its estimated total storage
#: capacity is 23.7 Bcf" -- so a sentence-wide qualifier scan would attach
#: "approximately" to the storage figure, which the filer did not hedge.
_CLAUSE_BOUNDARY = re.compile(r"[.;:]|,\s*and\b|\band\b|\bwhile\b")
#: below this, a leading clause ("Includes") names nothing and the sentence is
#: the figure's real definition
_PROSE_DEFINITION_MIN = 25


def _clause_before(text: str, at: int) -> str:
    """The clause that introduces the number at `at` -- its own definition."""
    start = 0
    for m in _CLAUSE_BOUNDARY.finditer(text, 0, at):
        start = m.end()
    return text[start:at].strip()


def _local_qualifier(clause: str) -> str:
    """The hedge the filer applied TO THIS FIGURE, not to its neighbour."""
    if re.search(r"approximate", clause, re.I):
        return "approximately"
    if re.search(r"\bup\s+to\b", clause, re.I):
        return "up to"
    if re.search(r"estimat", clause, re.I):
        return "estimated"
    return ""


#: Limits the filer states IN THE SAME PASSAGE. A storage total that excludes
#: off-system capacity, or that includes gas held as base-gas inventory, is not
#: the same quantity as working-gas capacity, and a reader who is not told cannot
#: know. These are captured as stated -- never rewritten into a derived figure.
_CAVEAT_PATTERNS = (
    (r"except\s+for\s+capacity\s+associated\s+with\s+base\s+gas[^.]{0,160}\.",
     "BASE GAS: the filer states that capacity associated with base gas requirements is "
     "NOT assigned to or available for firm service. The stated total is therefore not a "
     "working-gas figure and must not be presented as one"),
    (r"(?:does\s+not\s+include|excludes?)\s+any\s+off[-\s]?system\s+capacity[^.]{0,80}\.",
     "OFF-SYSTEM: the filer states that the storage total excludes off-system capacity"),
    (r"under\s+reasonably\s+representative\s+operating\s+assumptions",
     "OPERATING ASSUMPTIONS: 18 CFR 284.13(d)(2) asks for the figure under reasonably "
     "representative operating assumptions -- a design estimate, not an observed maximum"),
)


def _stated_caveats(text: str) -> list[str]:
    out = []
    for rx, note in _CAVEAT_PATTERNS:
        m = re.search(rx, text, re.I)
        if m:
            out.append(f"{note}. As filed: {re.sub(chr(32) + '+', ' ', m.group(0)).strip()}")
    return out


def _prose_unit(m) -> str:
    """The unit exactly as the filer wrote it, normalised only in whitespace:
    'MMscf per day' and 'MMscf/day' are the same unit, 'Bcf' is not per-day."""
    base = m.group(2)
    per = (m.group(3) or "").strip()
    return f"{base}/day" if per else base


def _prose_source_row(block: list[dict], value_text: str, unit_text: str) -> dict:
    """Return the persisted OCR/text row that directly supports a figure.

    A prose paragraph can span many extracted rows.  Pointing every figure at
    the paragraph's first row made image-only observations resolve to the page
    heading instead of the sentence containing the value.  Prefer the exact
    as-filed number; where a PDF split that number across rows, retain the row
    carrying its reported unit.  Falling back to the first row is intentionally
    last and keeps an unusually fragmented passage visible rather than
    inventing a row identity.
    """
    for probe in (value_text, unit_text, unit_text.split("/")[0]):
        needle = (probe or "").strip()
        if not needle:
            continue
        for row in block:
            if needle.lower() in str(row.get("text") or "").lower():
                return row
    return block[0]


def _prose_figures(page: dict, block: list[dict]) -> list[dict]:
    """Narrative capacity statements ("approximately 2,900 MMscf/day", "112 Bcf").

    Matched over a whole paragraph of consecutive non-table rows, because a
    sentence wraps: Transco's "approximately 2,9" and its unit "00 MMscf/day"
    sit on two different text rows and a per-row match would drop the figure.

    These are real filed disclosures and are kept with their exact sentence, but
    they are separate assertions from the table figures and are never merged with
    them, added to them, or converted.
    """
    if not block:
        return []
    text = re.sub(r"\s+", " ", " ".join(r["text"] for r in block)).strip()
    source_control_codes = sorted({code for row in block
                                   for code in row.get("binary_control_codes") or []})
    if not re.search(r"capacit|deliverab|storage", text, re.I):
        return []
    out = []
    for m in PROSE_NUM_RE.finditer(text):
        value = _num(m.group(1))
        if value is None:
            continue
        unit = _prose_unit(m)
        source_row = _prose_source_row(block, m.group(1), unit)
        dot = text.rfind(". ", 0, m.start(1))
        sentence_start = dot + 2 if dot >= 0 else 0
        sentence_end = text.find(". ", m.end(2))
        sentence = text[sentence_start: sentence_end + 1 if sentence_end > 0 else len(text)]
        clause = _clause_before(text, m.start(1))
        # The definition is the filer's own words for WHAT is being measured; the
        # whole sentence stays in `verbatim` so nothing is lost. Where the clause
        # is too generic to identify the figure -- Northwest Pipeline's footnotes
        # both begin "Includes", one about posted available capacity and one
        # about subordinated firm rights -- the SENTENCE is the identity, because
        # that is what actually defines a narrative assertion.
        clause = re.sub(r"^\W+", "", clause)[-140:]
        one_line = re.sub(r"\s+", " ", sentence).strip()
        definition = clause if len(clause) >= _PROSE_DEFINITION_MIN else one_line[:110]
        out.append({
            "kind": "prose", "page": page["page"], "y": source_row["y"],
            "row_index": source_row.get("row_index", 0),
            # a narrative figure's position within the block, so two figures in
            # one paragraph are never indistinguishable
            "char_offset": m.start(1),
            # a narrative statement has no table or column of its own; the keys
            # exist so every figure carries the same shape and the identity
            # resolver never has to guess what a figure is
            "table_index": -1, "column_ordinal": 1, "value_cell_count": 1,
            "section_basis": "narrative statement",
            "section": "narrative statement",
            "row_label": definition,
            "column": "", "unit": unit,
            "value_text": m.group(1),
            "value_num": None if source_control_codes else value,
            "x": source_row["cells"][0]["x"],
            "x_end": source_row["cells"][-1]["x_end"],
            "verbatim": text, "char_start": m.start(1),
            "char_end": m.end(3) if m.group(3) else m.end(2),
            "qualifier": _local_qualifier(clause) or _local_qualifier(sentence),
            "sentence": re.sub(r"\s+", " ", sentence).strip(),
            "caveats": _stated_caveats(text),
            "is_total": False, "label_from_section": False,
            "unit_note": "", "binary_control_codes": source_control_codes,
            "discriminator": "",
            "problems": ([_control_problem(source_control_codes)]
                         if source_control_codes else [])})
    return out


# ============================================================ retrieval


def retrieve(ctx, entity, *, year_from: int, year_to: int) -> list[dict]:
    """The latest two annual capacity-report cycles for one entity."""
    _RETRIEVAL_STATE[entity["entity_key"]] = ("ok", "")
    # A20: the capture date is DECLARED, never taken from the wall clock. This
    # value is a QUERY BOUND -- an offline replay run on a later day would
    # otherwise ask the cached capture a question it cannot answer and shift the
    # search window without saying so.
    today = ctx.today()
    start = f"{year_from:04d}-01-01"
    end = min(dt.date(year_to + 1, 12, 31), today).isoformat()
    try:
        hits = ioc_adapter.search(ctx, entity["legal_name"], SEARCH_PHRASE,
                                  start=start, end=end, document_type=CLASS_TYPE[1])
    except FetchError as exc:
        ctx.staging.open_blocker(ADAPTER, "access",
                                 f"{entity['entity_key']}: capacity-report search failed",
                                 scope=entity["entity_key"], attempts=str(exc.attempts),
                                 exact_error=exc.detail)
        ctx.log("error", f"{entity['entity_key']}: capacity search failed: {exc.detail}",
                adapter=ADAPTER, entity_cid=entity["entity_key"])
        _RETRIEVAL_STATE[entity["entity_key"]] = (
            "search_failed", f"the eLibrary search itself failed: {exc.detail}")
        return []
    ioc_adapter.resolve_blockers(ctx, ADAPTER, entity["entity_key"])
    if not hits:
        ctx.log("info", f"{entity['entity_key']} {entity['legal_name'][:40]}: no "
                        f"'{CLASS_TYPE[1]}' filings in eLibrary {start}..{end}",
                adapter=ADAPTER, entity_cid=entity["entity_key"])
        return []

    cands = []
    rejected_names = []
    for h in hits:
        acc = h.get("acesssionNumber")
        if not acc:
            continue
        desc = h.get("description") or ""
        if not _description_names_filer(entity["legal_name"], desc):
            rejected_names.append(acc)
            continue
        m = re.search(r"\bfor\s+(\d{4})\b", desc)
        filed = ioc_adapter.iso_date(h.get("filedDate", ""))
        report_year = int(m.group(1)) if m else None
        pdfs = [t for t in (h.get("transmittals") or [])
                if (t.get("fileType") or "").upper() == "PDF"
                or (t.get("fileName") or "").lower().endswith(".pdf")]
        cands.append({"accession": acc, "description": desc, "filed_date": filed,
                      "posted_date": ioc_adapter.iso_date(h.get("postedDate", "")),
                      "issued_date": ioc_adapter.iso_date(h.get("issuedDate", "")),
                      "avail_code": (h.get("availCode") or "").upper(),
                      "report_year": report_year, "pdfs": pdfs, "hit": h})

    if rejected_names:
        ctx.log(
            "info",
            f"{entity['entity_key']}: rejected {len(rejected_names)} capacity-search "
            "hit(s) whose machine-generated description names another filer",
            adapter=ADAPTER, entity_cid=entity["entity_key"])
    public = [c for c in cands if c["avail_code"] == "P" and c["pdfs"]]
    for c in cands:
        if c["avail_code"] != "P":
            ctx.staging.open_blocker(
                ADAPTER, "access",
                f"{entity['entity_key']} {c['accession']}: availCode "
                f"{c['avail_code'] or 'blank'} -- capacity report enumerated but not public",
                scope=f"{entity['entity_key']}:{c['accession']}",
                exact_error=f"availCode={c['avail_code']}; {c['description'][:160]}")
    public.sort(key=lambda c: (c["filed_date"], c["accession"]), reverse=True)
    # Select occurrences by filed date here.  Their true cycles are resolved
    # from the downloaded documents below; grouping on eLibrary's description
    # year would recreate the exact Florida Gas defect this adapter guards.
    chosen = public[:CYCLES]

    filings = []
    for c in sorted(chosen, key=lambda c: c["filed_date"]):
        scope_key = f"capacity:{c['accession']}"
        ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key, "in_progress")
        try:
            filings.append(_fetch_and_persist(ctx, entity, c))
        except FetchError as exc:
            ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key, "failed",
                                   error=exc.detail)
            ctx.staging.open_blocker(ADAPTER, "source",
                                     f"{entity['entity_key']} {c['accession']}: capacity "
                                     f"report not retrieved", scope=scope_key,
                                     attempts=str(exc.attempts), exact_error=exc.detail)
            continue
        except ImageOnlySource as exc:
            # A17: OUR gap, not FERC's. The blocker kind is `parser`, the record
            # is kept as manual-review pending, and nothing anywhere is allowed
            # to say FERC has no operating data for this filing.
            _IMAGE_ONLY[c["accession"]] = {
                "accession": c["accession"], "entity_key": entity["entity_key"],
                "report_year": c["report_year"], "filed_date": c["filed_date"],
                "detail": str(exc)}
            ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key,
                                   "manual_review_pending", error=str(exc)[:400])
            ctx.staging.open_blocker(
                ADAPTER, "parser",
                f"{c['accession']}: image-only capacity PDF awaiting rendered-page review",
                scope=scope_key, exact_error=str(exc)[:400])
            ctx.log("warn", f"{c['accession']}: image-only PDF, no reviewed transcription "
                            f"on file; held for rendered-page review",
                    adapter=ADAPTER, entity_cid=entity["entity_key"])
            continue
        except Exception as exc:                                          # noqa: BLE001
            # An unexpected exception in this adapter is OUR defect. Filing it as
            # kind="source" is the same mislabelling A17 records: it reads as a
            # FERC failure in every downstream count. It is a `parser` blocker,
            # and the traceback is kept so it is diagnosable rather than merely
            # counted. (Found the hard way: a missing dict key in the identity
            # resolver silently became "Northwest Pipeline: capacity PDF parse
            # failed" and removed the filer's figures entirely.)
            detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key, "failed",
                                   error=f"adapter error: {exc}")
            ctx.staging.open_blocker(
                ADAPTER, "parser",
                f"{c['accession']}: ADAPTER ERROR while reading a retrieved capacity "
                f"report -- our defect, not a FERC source failure",
                scope=scope_key, exact_error=detail[:1200])
            ctx.log("error", f"{c['accession']}: ADAPTER ERROR (our defect, not a source "
                             f"gap): {type(exc).__name__}: {exc}",
                    adapter=ADAPTER, entity_cid=entity["entity_key"])
            continue
        ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key, "done")
        ioc_adapter.resolve_blockers(ctx, ADAPTER, scope_key)

    image_only = [a for a in _IMAGE_ONLY
                  if _IMAGE_ONLY[a]["entity_key"] == entity["entity_key"]]
    if chosen and not filings and image_only:
        _RETRIEVAL_STATE[entity["entity_key"]] = (
            "image_only",
            f"{len(image_only)} public capacity report(s) ({', '.join(sorted(image_only))}) "
            "were retrieved successfully and carry no text layer. FERC published the data; "
            "this parser cannot read an image-only page and no reviewed transcription of "
            "these exact bytes is on file. This is a PARSER-CAPABILITY GAP of ours awaiting "
            "rendered-page review -- it is NOT evidence that FERC lacks operating data for "
            "these records, and no system capacity is inferred")
    elif chosen and not filings:
        _RETRIEVAL_STATE[entity["entity_key"]] = (
            "download_failed",
            f"{len(chosen)} public capacity reports were located but none could be "
            f"retrieved or parsed; see the open blockers for the exact errors")
    ctx.log("info", f"{entity['entity_key']} {entity['legal_name'][:34]}: "
                    f"{len(filings)} capacity cycles of {len(public)} public "
                    f"({len(cands)} hits)",
            adapter=ADAPTER, entity_cid=entity["entity_key"])
    return filings


def _fetch_and_persist(ctx, entity, c: dict) -> dict:
    acc = c["accession"]
    listing = ioc_adapter.file_list(ctx, acc)
    ids = [d["ID"] for d in listing if d.get("ID")]
    if not ids:
        raise FetchError(f"{ioc_adapter.ELIB}/File/GetFileListFromP8/{acc}",
                         "no attachments listed")
    blob, entry = ioc_adapter.download(ctx, acc, ids)
    parts = ioc_adapter.members(blob, acc, c["pdfs"][0]["fileName"])
    name, body = next(((n, b) for n, b in parts.items() if b[:5] == b"%PDF-"),
                      (None, None))
    if body is None:
        raise ValueError(f"no PDF member in the download (members: {sorted(parts)})")

    content_hash = ioc_adapter.sha256(body)
    doc = extract_pdf(body)
    text_layer, extraction = "yes", "pdf_text_span_coordinate_aware"
    review = None
    manual_pages_for_check = None
    ocr_evidence = None
    if not any(p["rows"] for p in doc):
        # A17: read the actual page pixels. A hash-bound manual review remains a
        # required independent cross-check; it never supplies the selector's
        # value. An unseen image cannot be published by analogy to these two.
        manual_review = REVIEWED_PAGE_IMAGES.get(content_hash)
        manual_pages = reviewed_pages(content_hash)
        if manual_review is None or manual_pages is None:
            raise ImageOnlySource(
                f"{acc}: the PDF carries no extractable text layer on any page "
                f"({len(doc)} page(s), sha256 {content_hash[:16]}...). This is a "
                "REVIEW GATE, not a FERC source gap: the document is public and legible, "
                "but these exact bytes have no independent page-image review against "
                "which OCR output can be checked. No figure is published by analogy.")
        try:
            ocr_pages, ocr_evidence = extract_image_pdf(
                body, vision_script=VISION_OCR_SCRIPT, expected_pages=len(doc))
        except ImageOCRFailure as exc:
            raise ImageOnlySource(
                f"{acc}: image-only PDF OCR failed locally ({exc}). FERC published the "
                "document; this is an OCR runtime/capability failure of ours.") from exc
        ocr_pages = _normalise_capacity_doc_rows(ocr_pages)
        manual_pages_for_check = _normalise_capacity_doc_rows(manual_pages)
        review = dict(manual_review)
        review.update({
            "automatic_ocr_used": True,
            "ocr_engine": "macOS Vision VNRecognizeTextRequest accurate",
            "ocr_script_sha256": ocr_evidence["vision_script_sha256"],
            "ocr_checked_against_manual_review": True,
            "ocr_corrections": ocr_evidence["corrections"],
        })
        doc = ocr_pages
        text_layer, extraction = "no", IMAGE_OCR_METHOD
        ctx.log("info", f"{acc}: image-only PDF; values extracted from actual page "
                        f"pixels by local Vision OCR and checked against the independent "
                        f"review of {review['reviewed_at']} ({review['reviewed_by']})",
                adapter=ADAPTER, entity_cid=entity["entity_key"])
    # A common final boundary protects any future capacity extraction route that
    # does not happen to use ``rows_of``.  It is idempotent for born-digital rows.
    doc = _normalise_capacity_doc_rows(doc)
    if manual_pages_for_check is not None:
        report_year, report_year_basis, report_year_disagreements = \
            _reviewed_image_report_year(
                doc, manual_pages_for_check, c, content_hash=content_hash)
    else:
        report_year, report_year_basis, report_year_disagreements = \
            _report_year_from_document(doc, c, content_hash=content_hash)
    if report_year_disagreements:
        ctx.log(
            "warn",
            f"{acc}: " + "; ".join(report_year_disagreements),
            adapter=ADAPTER,
            entity_cid=entity["entity_key"],
        )
    if manual_pages_for_check is not None:
        ocr_figures = read_figures(doc, report_year)
        manual_figures = read_figures(manual_pages_for_check, report_year)
        if _review_signature(ocr_figures) != _review_signature(manual_figures):
            raise ImageOnlySource(
                f"{acc}: OCR output disagrees with the independent hash-bound review. "
                f"ocr={_review_signature(ocr_figures)!r}; "
                f"review={_review_signature(manual_figures)!r}. No value is published.")
    as_of, as_of_basis = _as_of(doc, report_year)
    version_status, supersedes = ctx.staging.classify_version(
        SOURCE_SYSTEM, entity["entity_key"], FORM, report_year, "annual",
        acc, content_hash)

    doc_id = f"{SOURCE_SYSTEM}|{acc}|{c['pdfs'][0].get('fileId') or name}"
    filing = {
        "source_system": SOURCE_SYSTEM, "filing_id": acc, "entity_key": entity["entity_key"],
        "form": FORM, "accession_number": acc,
        "reporting_year": report_year, "reporting_period": "annual",
        "period_start": None, "period_end": None,
        "filed_date": c["filed_date"], "posted_date": c["posted_date"],
        "issued_date": c["issued_date"], "effective_date": as_of,
        "submitted_on": c["filed_date"], "snapshot_date": as_of,
        "acceptance_status": f"availCode={c['avail_code']}",
        "taxonomy_version": "not_a_taxonomy_form",
        "schema_ref": AUTHORITY, "content_hash": content_hash,
        "is_canonical": 1,
        "canonical_reason": "one annual capacity report per cycle; latest filing retained",
        "version_status": version_status, "supersedes_filing_id": supersedes,
        "data_origin": "document", "retrieved_at": entry["last_seen_at"],
        "first_seen_at": entry["first_seen_at"],
        "source_url": redact(ioc_adapter.DOCINFO.format(acc))}
    document = {
        "document_id": doc_id, "source_system": SOURCE_SYSTEM, "filing_id": acc,
        "accession_number": acc,
        "attachment_id": str(c["pdfs"][0].get("fileId") or ""), "title": name,
        "class_type": " / ".join(CLASS_TYPE), "media_type": "application/pdf",
        "byte_size": len(body), "content_hash": content_hash,
        "cache_path": entry.get("cache_path"), "text_layer": text_layer,
        "availability": "retrieved", "retrieved_at": entry["last_seen_at"],
        "source_url": redact(ioc_adapter.FILELIST.format(acc))}

    facts = []
    for p in doc:
        for i, row in enumerate(p["rows"]):
            facts.append({
                "source_system": SOURCE_SYSTEM, "filing_id": acc,
                "source_fact_id": _fact_id(p["page"], i, text_layer),
                "document_order": p["page"] * 10000 + i,
                "concept_qname": "capacityreport:row",
                "concept_local": ("capacity_report_row" if text_layer == "yes" else
                                  "ocr_page_image_row" if extraction == IMAGE_OCR_METHOD
                                  else "reviewed_page_image_row"),
                "context_id": f"page{p['page']:02d}", "unit_id": "", "unit_text": "",
                "decimals": "", "precision": "", "value_as_filed": row["text"], "is_nil": 0,
                "period_class": periods.AS_OF, "instant": as_of,
                "period_start": None, "period_end": None, "duration_days": "",
                "current_or_prior": "current", "explicit_dims_json": "",
                "typed_dims_json": json.dumps(
                    {"page": p["page"], "y": row["y"], "reordered": row["reordered"],
                     "shifted_fonts": p["shifted_fonts"],
                     "binary_control_codes": row.get("binary_control_codes") or [],
                     "cells": [{"x": cc["x"], "x_end": cc["x_end"], "text": cc["text"]}
                               for cc in row["cells"]],
                     "ocr_raw_text": row.get("ocr_raw_text"),
                     "ocr_confidence": row.get("ocr_confidence")}, sort_keys=True),
                "taxonomy_version": "not_a_taxonomy_form"})
    ctx.staging.write_filing_bundle(filing, facts=facts, documents=[document])

    filing["_doc"] = doc
    filing["_document_id"] = doc_id
    filing["_as_of"] = as_of
    filing["_as_of_basis"] = as_of_basis
    filing["_report_year_basis"] = report_year_basis
    filing["_report_year_disagreements"] = report_year_disagreements
    filing["_figures"] = read_figures(doc, report_year)
    filing["_pdf_name"] = name
    filing["_shifted"] = sorted({f for p in doc for f in p["shifted_fonts"]})
    filing["_text_layer"] = text_layer
    filing["_extraction"] = extraction
    filing["_review"] = review
    filing["_ocr_evidence"] = ocr_evidence
    return filing


def _fact_id(page: int, row_index: int, text_layer: str) -> str:
    """The source-fact key for one extracted row. A08: a document-derived
    observation must point at a real input fact, so every row the reader used is
    persisted with an id an edge can resolve. Reviewed image rows are keyed
    distinctly, so nothing can mistake a transcribed line for a text-layer one."""
    prefix = "P" if text_layer == "yes" else "IMG"
    return f"{prefix}{page:02d}R{row_index:04d}"


# ============================================================ expected

def freeze_expected(ctx, entity, filings: list[dict], assets: list[dict]) -> list[dict]:
    template = assets[0]["template"] if assets else entity.get("template", "interstate_gas")
    asset_id = assets[0]["asset_id"] if assets else ""
    metrics = [m for m in BY_ADAPTER[ADAPTER] if template in m.templates]
    # One slot per reporting CYCLE. The task asks for the latest two cycles, and
    # each is its own requested observation with its own as-of date; collapsing
    # them onto one slot would report a denominator of one for two filings.
    cycles = sorted({(f.get("_as_of") or f.get("snapshot_date") or "",
                      f.get("reporting_year") or ctx.args.year_to)
                     for f in filings if f.get("is_canonical", 1)}) or \
             [("", ctx.args.year_to)]
    out, seen = [], set()
    for as_of, year in cycles:
        for m in metrics:
            if not filings:
                requirement = coverage.UNKNOWN
                evidence = (f"no '{CLASS_TYPE[1]}' filing was found in eLibrary under this "
                            f"legal name in the requested window; {AUTHORITY} applies to "
                            f"interstate pipelines and whether this filer carries that "
                            f"obligation was not established from a FERC source")
            elif m.id == "cap_reported_capacity":
                requirement = coverage.REQUIRED
                evidence = (f"{AUTHORITY}: an annual peak-day capacity report is filed by "
                            f"1 March for the annual cycle identified in the filed report")
            else:
                requirement = coverage.CONDITIONAL
                evidence = m.gate_reason or m.quality_gate
            slot = coverage.build_expected(entity["entity_key"], asset_id, template, m,
                                           REGIME, periods.AS_OF, year, "Q4", requirement,
                                           evidence, APPLICABILITY_BASIS,
                                           ctx.staging.run_id, as_of=as_of)
            if slot["slot_id"] not in seen:
                seen.add(slot["slot_id"])
                out.append(slot)
    return out


# ============================================================ observations

def _obs(entity_key, m, *, instant, year, scope, unit, value_text, value_num,
         availability, method=Method.DOCUMENT_EXTRACTED, validation=Validation.PASS,
         version_status=VersionStatus.ORIGINAL, qa_flags="", missing_reason="",
         filing_id="", source_fact_id=None, document_id=None, accession=None,
         selector="", derivation="", notes="", applicability_evidence="") -> dict:
    return {
        "observation_id": observation_id(entity_key, m.id, REGIME, periods.AS_OF,
                                         "", "", instant or "", scope, unit or "", method),
        "entity_key": entity_key, "metric_id": m.id, "source_regime": REGIME,
        "period_basis": periods.AS_OF, "period_start": None, "period_end": None,
        "instant_date": instant or None, "reporting_year": year, "reporting_period": "Q4",
        "period_label": f"as of {instant}" if instant else f"FY{year}",
        "scope": scope, "unit": unit, "value_text": value_text, "value_num": value_num,
        "normalized_iso": instant or None,
        "availability": availability, "origin": Origin.ELIBRARY_DOCUMENT, "method": method,
        "version_status": version_status, "validation": validation,
        "source_system": SOURCE_SYSTEM, "filing_id": filing_id or None,
        "source_fact_id": source_fact_id, "source_context_id": None,
        "document_id": document_id, "accession_number": accession,
        "candidate_count": None, "selector": selector or m.selector, "derivation": derivation,
        "concept_local": None, "concept_qname": None,
        "taxonomy_version": "not_a_taxonomy_form", "registry_version": REGISTRY_VERSION,
        "applicability_version": APPLICABILITY_BASIS, "schedule_page": m.schedule,
        "taxonomy_label": None, "qa_flags": qa_flags, "review_status": "",
        "missing_reason": missing_reason, "applicability_evidence": applicability_evidence,
        "notes": notes,
    }


def _report_year_qa(filing: dict) -> list[str]:
    rows = []
    if filing.get("_report_year_basis"):
        rows.append(
            f"report year {filing.get('reporting_year')} resolved from "
            f"{filing['_report_year_basis']}")
    rows.extend(str(value) for value in
                filing.get("_report_year_disagreements") or [])
    return rows


def canonicalise(ctx, entity, filings: list[dict], expected: list[dict]) -> tuple[list, list]:
    entity_key = entity["entity_key"]
    template = entity.get("template", "interstate_gas")
    metrics = {m.id: m for m in BY_ADAPTER[ADAPTER] if template in m.templates}
    observations: list[dict] = []
    edges: list[dict] = []

    for f in filings:
        observations.extend(_capacity_observations(ctx, entity, f, metrics))

    obs, ed, populations = _peak_day_gate(ctx, entity, filings, metrics, observations)
    observations.extend(obs)
    edges.extend(ed)
    observations.extend(_account_for_remainder(entity, metrics, expected, observations,
                                               filings))
    # A08: set-based lineage travels with the observation it belongs to, so the
    # single writer commits the population, the observation and the edge that
    # references it inside ONE unit of work. Writing it here directly would fail
    # the foreign key, because the observation does not exist yet.
    deduped = _dedupe(ctx, observations)
    if populations:
        by_obs = {o["observation_id"]: o for o in deduped}
        for pop in populations:
            owner = by_obs.get(pop["observation_id"])
            if owner is not None:
                owner.setdefault("_populations", []).append(pop)
    return deduped, edges


def _dedupe(ctx, observations):
    """Last-resort guard on the observation grain.

    `_resolve_identical_definitions` should make this unreachable for conflicting
    values: figures whose definitions the document does not separate are already
    distinguished by position and already flagged. If a conflict still arrives,
    the previous behaviour -- keep whichever came first, log an error -- would
    publish one of two materially different capacity figures on iteration order
    and leave no trace of the other in the data. It is the same failure as
    choosing an owner by sort order.

    So a surviving conflict collapses to ONE row that publishes NO value and
    names every candidate with its position. An unresolved figure is a legitimate
    bounded outcome; a confident wrong one is not.
    """
    seen: dict[str, dict] = {}
    conflicts: dict[str, list[dict]] = {}
    for o in observations:
        prior = seen.get(o["observation_id"])
        if prior is None:
            seen[o["observation_id"]] = o
        elif prior.get("value_text") != o.get("value_text"):
            conflicts.setdefault(o["observation_id"], [prior]).append(o)
    for oid, group in conflicts.items():
        candidates = "; ".join(
            f"{g.get('value_text')!r} {g.get('unit') or ''}"
            f" ({(g.get('notes') or '')[:90]})" for g in group)
        ctx.log("error",
                f"observation grain collision on {group[0]['metric_id']} "
                f"scope={group[0]['scope']!r}: {len(group)} different values "
                f"({', '.join(repr(g.get('value_text')) for g in group)}). NONE is "
                f"published; the row is gated as an unresolved figure",
                adapter=ADAPTER, entity_cid=group[0]["entity_key"])
        o = dict(group[0])
        o["value_text"] = f"{len(group)} conflicting values, none published"
        o["value_num"] = None
        o["availability"] = Availability.UNVERIFIED_AVAILABILITY
        o["validation"] = Validation.BLOCKED_AMBIGUITY
        o["missing_reason"] = (
            f"UNRESOLVED FIGURE. {len(group)} figures in this document reduce to the "
            f"identical grain -- same entity, metric, period, scope, unit and method -- "
            f"but carry different values: {candidates}. The reader could not establish "
            f"what distinguishes them, so no value is published. Choosing one would be "
            f"choosing on iteration order. All candidates are retained as document facts "
            f"with their page positions.")
        o["qa_flags"] = ((o.get("qa_flags") or "")
                         + "; GRAIN COLLISION: every candidate retained as a document "
                           "fact, none published as the answer")
        # keep the first candidate's document fact and mark it for review; the
        # others' facts are still written from their own observations upstream
        if o.get("_document_fact"):
            fact = dict(o["_document_fact"])
            fact["confidence"] = "review_required:grain_collision"
            fact["review_state"] = "queued"
            fact["value_num"] = None
            o["_document_fact"] = fact
        seen[oid] = o
    return list(seen.values())


def _capacity_observations(ctx, entity, f, metrics) -> list[dict]:
    m = metrics.get("cap_reported_capacity")
    if m is None:
        return []
    entity_key = entity["entity_key"]
    acc = f["filing_id"]
    as_of = f["_as_of"]
    year = f["reporting_year"]
    figures = f["_figures"]
    out = []

    if not figures:
        return [_obs(entity_key, m, instant=as_of, year=year, scope=m.scope, unit=None,
                     value_text=None, value_num=None,
                     availability=Availability.PARSE_FAILED,
                     validation=Validation.NOT_YET_VALIDATED,
                     missing_reason=("the PDF text layer was read but no labelled numeric "
                                     "figure could be located in it; this is an extraction "
                                     "gap of ours, not a missing FERC disclosure"),
                     filing_id=acc, accession=acc, document_id=f.get("_document_id"))]

    shifted = f.get("_shifted") or []

    # The requested slot asks what this cycle reports. One observation at the
    # registry scope answers it -- the system total where the filer states one,
    # otherwise a census of what the document DOES provide, which is the honest
    # answer for a report that gives only route and rate-schedule figures. Every
    # individual figure is emitted below with its own definition and unit; none
    # of them is ever summed into a system figure.
    totals = [g for g in figures if g["is_total"] and g["kind"] == "table"]
    source_text_problems = sorted({
        problem for figure in figures for problem in figure.get("problems") or []
        if "binary glyph codes before portable-text normalisation" in problem
    })
    if totals:
        t = totals[0]
        head_text, head_num, head_unit = t["value_text"], t["value_num"], t["unit"]
        head_note = (f"the filer's own stated system total: "
                     f"{t['section']} / {t['row_label']}")
    else:
        head_text, head_num, head_unit = (
            f"{len(figures)} reported figures, no system total stated", None, None)
        head_note = ("this report states NO system-wide total -- it gives route, storage "
                     "and rate-schedule figures only. No total is computed from them, "
                     "because summing figures of different scope and direction would "
                     "invent a number the filer did not report")
    head_validation = (Validation.BLOCKED_AMBIGUITY if source_text_problems
                       else Validation.PASS)
    # The report and candidate figure population are both known to exist.  If
    # their extracted definition is undecodable, the unresolved question is
    # meaning—not whether the FERC source exists or applies.  Keep that state
    # distinct so coverage cannot misreport a semantic gate as applicability
    # unknown.
    head_availability = (Availability.INTERPRETATION_BLOCKED
                         if source_text_problems and head_num is None
                         else Availability.PRESENT)
    head_missing_reason = "; ".join(source_text_problems)
    # The metric asks for the capacity figures this report gives, plural. A filer
    # that reports routes and rate schedules but no single system number HAS
    # answered it -- so the slot is PRESENT with the census as its value, and the
    # absence of a system total is stated rather than treated as inapplicability.
    out.append(_obs(
        entity_key, m, instant=as_of, year=year, scope=m.scope, unit=head_unit,
        value_text=head_text, value_num=head_num,
        availability=head_availability, validation=head_validation,
        version_status=f.get("version_status") or VersionStatus.ORIGINAL,
        missing_reason=head_missing_reason,
        qa_flags="; ".join([
            f"{AUTHORITY}; filed {f['filed_date']} for report year {year}",
            *_report_year_qa(f),
            f"as-of date: {as_of} ({f['_as_of_basis']})",
            head_note,
            *(source_text_problems or []),
            f"{len(figures)} figures extracted from this document, each published as its "
            f"own observation with its own definition, direction, unit and page evidence",
            "figures are shown exactly as reported; no unit conversion is applied and "
            "figures from different sections are never added together"]),
        filing_id=acc, accession=acc, document_id=f.get("_document_id"),
        selector="document_span",
        notes="; ".join(f"{g['row_label'][:40]}={g['value_text']}{g['unit'] or ''}"
                        for g in figures[:8])))

    for i, fig in enumerate(figures):
        scope = _figure_scope(fig)
        confidence, validation, problems = _confidence(fig)
        qa = [
            f"{AUTHORITY}; filed {f['filed_date']} for report year {year}",
            *_report_year_qa(f),
            f"as-of date: {as_of} ({f['_as_of_basis']})",
            f"page {fig['page']}, text row at y={fig['y']}, cell x={fig['x']:.0f}"
            f"-{fig['x_end']:.0f}",
            f"definition as reported: {fig['section'] or '(no section header)'} / "
            f"{fig['row_label'] or '(no row label)'}"
            + (f" / {fig['column']}" if fig["column"] else ""),
            f"unit as reported: {fig['unit'] or 'NOT RESOLVED'}"
            + (f" (from the banner {fig['unit_note']!r})" if fig["unit_note"] else ""),
            "figures are shown exactly as reported; no unit conversion is applied and "
            "figures from different sections are never added together",
        ]
        if fig["kind"] == "prose":
            qa.append("narrative statement in the filer's own words, not a table cell; a "
                      "separate assertion from the tabulated figures")
            if fig.get("sentence"):
                qa.append(f"sentence as filed: {fig['sentence'][:300]}")
            for caveat in fig.get("caveats") or []:
                qa.append(caveat)
        if f.get("_text_layer") == "no":
            review = f.get("_review") or {}
            if f.get("_extraction") == IMAGE_OCR_METHOD:
                qa.append(
                    "IMAGE-ONLY SOURCE, OCR EXTRACTED AND REVIEW-CHECKED. This exact FERC "
                    "PDF carries no text layer. Poppler rendered the actual page and local "
                    "macOS Vision read its pixels; the ordinary capacity reader selected "
                    "the values, units, subjects and qualifiers from that OCR text. Its "
                    "semantic figure/caveat signature was then required to equal the "
                    "independent hash-bound manual review. Raw OCR rows, confidences and "
                    "bounded corrections are retained in source-fact metadata. A mismatch "
                    "publishes nothing. This is not evidence of absent FERC data")
            else:
                qa.append(
                    "IMAGE-ONLY SOURCE, READ BY REVIEW. The page-image transcription is "
                    "manual and hash-bound; no automatic extraction is claimed")
            qa.append(f"review: {review.get('reviewed_by', '')} on "
                      f"{review.get('reviewed_at', '')}; render {review.get('render', '')}")
            if f.get("_ocr_evidence"):
                qa.append(
                    f"OCR: {review.get('ocr_engine', '')}; script "
                    f"sha256={review.get('ocr_script_sha256', '')}; "
                    f"corrections={json.dumps(review.get('ocr_corrections') or [], sort_keys=True)}")
            if review.get("review_note"):
                qa.append(f"reviewer note: {review['review_note']}")
        if fig["label_from_section"]:
            qa.append("the row carries no label of its own; the section header supplies it, "
                      "and the column labels come from the header cells this value overlaps")
        if shifted:
            qa.append(f"font encoding repaired on {', '.join(shifted)} (+29 subset-font "
                      f"offset, applied only because it produced English words and the "
                      f"font carries no /ToUnicode map)")
        if problems:
            qa.append("CONFIDENCE: " + "; ".join(problems))
        out.append(_ob_with_fact(entity_key, m, f, fig, i, scope, qa, confidence,
                                 validation))
    return out


def _figure_scope(fig: dict) -> str:
    bits = [fig["section"] or "unsectioned", fig["row_label"] or "unlabelled row"]
    if fig["column"]:
        bits.append(fig["column"])
    # The structural position joins the identity ONLY where the document's own
    # labels failed to separate two figures. It is what lets both survive; it is
    # never used to pick a winner between them.
    if fig.get("discriminator"):
        bits.append(fig["discriminator"])
    scope = " :: ".join(b.strip() for b in bits if b)
    return f"as reported: {scope}"[:400]


def _confidence(fig: dict) -> tuple[str, str, list[str]]:
    problems = list(fig["problems"])
    if problems:
        return ("review_required:" + ",".join(
            "definition_not_separated" if "reduce to the same definition" in p
            else "column_unresolved_on_multi_value_row" if "and no column header" in p
            else "ambiguous_column_boundary" if "reading order" in p
            else "source_text_undecodable" if "binary glyph codes" in p
            else "unit_unresolved" if "unit" in p else "label_unresolved"
            for p in problems), Validation.BLOCKED_AMBIGUITY, problems)
    return "verified_span", Validation.PASS, problems


def _ob_with_fact(entity_key, m, f, fig, i, scope, qa, confidence, validation) -> dict:
    acc = f["filing_id"]
    reviewed = f.get("_text_layer") == "no"
    image_ocr = f.get("_extraction") == IMAGE_OCR_METHOD
    review = f.get("_review") or {}
    # A08: the persisted source-fact row this figure was read out of, so a lineage
    # edge can point at a real input instead of at nothing.
    fact_id = _fact_id(fig["page"], fig.get("row_index", 0), f.get("_text_layer", "yes"))
    o = _obs(entity_key, m, instant=f["_as_of"], year=f["reporting_year"], scope=scope,
             unit=fig["unit"] or None,
             value_text=fig["value_text"], value_num=fig["value_num"],
             availability=(Availability.PRESENT if fig["value_num"] is not None
                           else Availability.INTERPRETATION_BLOCKED
                           if validation == Validation.BLOCKED_AMBIGUITY
                           else Availability.UNVERIFIED_AVAILABILITY),
             method=(Method.DOCUMENT_EXTRACTED if image_ocr or not reviewed
                     else Method.MANUALLY_CURATED),
             validation=validation,
             version_status=f.get("version_status") or VersionStatus.ORIGINAL,
             qa_flags="; ".join(qa),
             missing_reason=("; ".join(fig["problems"]) if fig["value_num"] is None else ""),
             filing_id=acc, accession=acc, document_id=f.get("_document_id"),
             source_fact_id=fact_id, selector="document_span",
             notes=fig["verbatim"][:400])
    o["_document_fact"] = {
        "document_fact_id": "dfact-" + hashlib.sha256(
            f"{acc}|{fig['page']}|{fig['y']}|{fig['x']}|{i}".encode()).hexdigest()[:24],
        "document_id": f.get("_document_id"), "source_system": SOURCE_SYSTEM,
        "filing_id": acc, "source_fact_id": fact_id, "entity_key": entity_key,
        "assertion_type": "reported_capacity", "metric_id": m.id,
        "value_text": fig["value_text"], "value_num": fig["value_num"],
        "unit": fig["unit"], "qualifier": fig["qualifier"],
        "scope_note": scope, "page": str(fig["page"]),
        "paragraph": (f"OCR page image row {fig.get('row_index', 0)}"
                      if image_ocr else
                      f"reviewed page image, transcribed line {fig.get('row_index', 0)}"
                      if reviewed else
                      f"text row y={fig['y']}, cell x={fig['x']:.0f}-{fig['x_end']:.0f}"),
        "char_start": fig["char_start"], "char_end": fig["char_end"],
        "verbatim_span": fig["verbatim"],
        "extraction_method": f.get("_extraction") or "pdf_text_span_coordinate_aware",
        "content_hash": f["content_hash"],
        # the span IS verified -- the value appears in it verbatim -- and the
        # suffix records how the page was read, so nothing has to infer it
        "confidence": ("verified_span:ocr_reviewed_page_image" if image_ocr
                       and confidence == "verified_span" else
                       "verified_span:reviewed_page_image" if reviewed
                       and confidence == "verified_span" else confidence),
        "review_state": ("reviewed" if reviewed and confidence == "verified_span"
                         else "queued" if confidence != "verified_span" else ""),
        "reviewer_note": (f"{review.get('reviewed_by', '')} on "
                          f"{review.get('reviewed_at', '')}; render: "
                          f"{review.get('render', '')}; OCR used: "
                          f"{bool(review.get('automatic_ocr_used') or review.get('ocr_used'))}. "
                          f"OCR engine: {review.get('ocr_engine', '')}; script: "
                          f"{review.get('ocr_script_sha256', '')}. "
                          f"{review.get('review_note', '')}"
                          if reviewed else ""),
        "first_seen_at": ioc_adapter.utcnow()}
    return o


# ---------------------------------------------------------------- gated ratio

def _peak_day_gate(ctx, entity, filings, metrics, produced) -> tuple[list, list]:
    """cap_peak_day_ratio is GATED and is attempted only when system scope,
    season/date, storage treatment and units all align between the capacity
    figure and the Form 2 p.518 peak. It is never given a universal denominator
    and it is never called annual utilisation.

    The four dimensions are tested against the registry's own definition of
    `single_day_peak_deliveries`, so the gate is evaluable even when no p.518
    observation exists for this filer.
    """
    m = metrics.get("cap_peak_day_ratio")
    if m is None:
        return [], [], []
    entity_key = entity["entity_key"]
    peak = BY_ID["single_day_peak_deliveries"]
    out, edges, populations = [], [], []

    peak_rows = ctx.staging.query(
        "SELECT * FROM observations WHERE entity_key=? AND metric_id=? "
        "AND availability=? ORDER BY instant_date DESC",
        (entity_key, "single_day_peak_deliveries", Availability.PRESENT))

    if not filings:
        return [], [], []

    for f in filings:
        # ONLY an explicit system total is a candidate denominator. Using a route
        # or rate-schedule figure as if it were the system would invent a
        # denominator, which this metric must never do.
        # ONLY an unambiguous, explicitly stated system total is a candidate
        # denominator. A figure the reader could not pin down is not one, and
        # neither is a report that states SEVERAL different totals: Florida Gas
        # states a Winter Season total and a Summer Season total, and picking
        # `figures[0]` of those would choose a denominator by page order.
        totals = [fig for fig in f["_figures"] if fig["is_total"] and fig["kind"] == "table"]
        figures = [fig for fig in totals if not fig["problems"]]
        distinct = {fig["value_text"] for fig in figures}
        if len(distinct) > 1:
            out.append(_obs(
                entity_key, m, instant=f["_as_of"], year=f["reporting_year"],
                scope=m.scope, unit=None, value_text=None, value_num=None,
                availability=Availability.NOT_APPLICABLE, method=Method.DERIVED,
                validation=Validation.SCOPE_INCOMPATIBLE,
                missing_reason=(
                    f"this report states {len(distinct)} DIFFERENT total figures "
                    f"({', '.join(sorted(distinct))}) under different section headers "
                    f"({', '.join(sorted({fig['section'] or 'unsectioned' for fig in figures}))}). "
                    "They are different quantities -- a seasonal or per-facility total is not "
                    "a system total -- and no rule in this adapter says which, if any, is the "
                    "system denominator. Choosing one would choose it by page order, so the "
                    "ratio is refused. Every total is published separately as its own "
                    "observation with its own section."),
                qa_flags="; ".join([
                    "SYSTEM SCOPE: not evaluable, because the report states several totals "
                    "and does not say which is the system",
                    "STORAGE TREATMENT / SEASON / DATE: not evaluable for the same reason",
                    "this ratio is a PEAK-DAY comparison only and is never described as "
                    "annual utilisation"]),
                filing_id=f["filing_id"], accession=f["filing_id"],
                document_id=f.get("_document_id"), selector="derived_ratio",
                applicability_evidence=m.gate_reason))
            continue
        if not figures:
            refused = _obs(entity_key, m, instant=f["_as_of"], year=f["reporting_year"],
                            scope=m.scope, unit=None,
                            value_text=None, value_num=None,
                            availability=Availability.NOT_APPLICABLE, method=Method.DERIVED,
                            validation=Validation.SCOPE_INCOMPATIBLE,
                            missing_reason=(
                                ("no system-total capacity figure is reported in this "
                                 "document -- it gives route, storage and rate-schedule "
                                 "figures only -- so there is no candidate denominator and "
                                 "none is invented from a route or a rate schedule")
                                if not totals else
                                (f"this document states {len(totals)} figure(s) labelled a "
                                 "total, but the reader could not resolve their definition "
                                 "well enough to publish a number for any of them, so none "
                                 "is a candidate denominator. An unresolved figure is not a "
                                 "system total and no denominator is invented from one")),
                            qa_flags="; ".join([
                                f"candidate numerator would be {peak.display} -- "
                                f"{peak.meaning} (scope: {peak.scope}; unit rule: "
                                f"{peak.unit_rule}; schedule {peak.schedule})",
                                "SYSTEM SCOPE: not evaluable, because the report states no "
                                "system-wide figure",
                                "STORAGE TREATMENT / SEASON / DATE: not evaluable for the "
                                "same reason",
                                "this ratio is a PEAK-DAY comparison only and is never "
                                "described as annual utilisation",
                                f"figures that ARE published from this document: "
                                f"{len(f['_figures'])} route, storage and rate-schedule "
                                f"values, each with its own definition and unit -- a valid "
                                f"filer-specific value may be displayed even when this peer "
                                f"comparison is invalid"]),
                            filing_id=f["filing_id"], accession=f["filing_id"],
                            document_id=f.get("_document_id"), selector="derived_ratio",
                            applicability_evidence=m.gate_reason)
            out.append(refused)
            # A08: a REFUSED derivation still owes an account of what it looked at.
            # "No candidate denominator" is a claim about a population, and the
            # population is persisted so a reviewer can confirm that no row in the
            # report was in fact labelled a total.
            pop = _total_population(entity_key, f, refused["observation_id"])
            populations.append(pop)
            edges.append(_population_edge(refused["observation_id"], 1, pop))
            continue
        cand = figures[0]
        reasons = _gate_reasons(f, cand, peak)
        peak_obs = dict(peak_rows[0]) if peak_rows else None

        note = [
            f"candidate denominator: {_figure_scope(cand)} = {cand['value_text']} "
            f"{cand['unit'] or '(unit unresolved)'} as of {f['_as_of']} "
            f"(page {cand['page']}, accession {f['filing_id']})",
            f"candidate numerator: {peak.display} -- {peak.meaning} (scope: {peak.scope}; "
            f"unit rule: {peak.unit_rule}; schedule {peak.schedule})",
            (f"a POPULATED p.518 observation exists for this filer: "
             f"{peak_obs['value_text']} {peak_obs['unit']} at {peak_obs['instant_date']}"
             if peak_obs else
             "no POPULATED single_day_peak_deliveries observation exists for this filer in "
             "the staging database (slots for it may exist unpopulated); the gate is decided "
             "on the two definitions alone and does not depend on the numerator's value"),
            "this ratio is a PEAK-DAY comparison only and is never described as annual "
            "utilisation",
        ]
        o = _obs(entity_key, m, instant=f["_as_of"], year=f["reporting_year"],
                 scope=m.scope, unit=None, value_text=None, value_num=None,
                 availability=Availability.NOT_APPLICABLE, method=Method.DERIVED,
                 validation=Validation.SCOPE_INCOMPATIBLE,
                 missing_reason=("the gate is not met: " + " | ".join(reasons)),
                 qa_flags="; ".join(note), filing_id=f["filing_id"],
                 accession=f["filing_id"], document_id=f.get("_document_id"),
                 selector="derived_ratio",
                 derivation="ratio(single_day_peak_deliveries, cap_reported_capacity) "
                            "-- REFUSED",
                 applicability_evidence=m.gate_reason)
        out.append(o)
        # A08: this edge used to carry neither an input fact nor an input
        # observation -- an empty pointer that no reviewer could traverse. The
        # denominator comes out of a specific persisted row of a specific filing
        # OCCURRENCE, and both are recorded: byte-identical content under another
        # accession must never satisfy this edge.
        edges.append({
            "observation_id": o["observation_id"], "input_order": 1,
            "input_role": "denominator", "operator_sign": "/", "coefficient": 1.0,
            "input_source_system": SOURCE_SYSTEM, "input_filing_id": f["filing_id"],
            "input_source_fact_id": _fact_id(cand["page"], cand.get("row_index", 0),
                                             f.get("_text_layer", "yes")),
            "input_observation_id": None,
            "input_context_id": f"page{cand['page']:02d}",
            "input_concept": "cap_reported_capacity",
            "input_period": f["_as_of"], "input_value": cand["value_text"],
            "input_unit": cand["unit"], "input_version_status": f.get("version_status"),
            "input_population_id": None})
        if peak_obs:
            edges.append({
                "observation_id": o["observation_id"], "input_order": 2,
                "input_role": "numerator", "operator_sign": "", "coefficient": 1.0,
                "input_source_system": peak_obs["source_system"],
                # the occurrence the input observation itself came from, never an
                # id that merely happens to resolve under another occurrence
                "input_filing_id": peak_obs["filing_id"],
                "input_source_fact_id": peak_obs["source_fact_id"],
                "input_observation_id": peak_obs["observation_id"],
                "input_context_id": peak_obs["source_context_id"],
                "input_concept": "single_day_peak_deliveries",
                "input_period": peak_obs["instant_date"],
                "input_value": peak_obs["value_text"], "input_unit": peak_obs["unit"],
                "input_version_status": peak_obs["version_status"],
                "input_population_id": None})
    return out, edges, populations


def _total_population(entity_key, f, observation_id_value) -> dict:
    """The set of figures considered as a candidate system-total denominator.

    Row count is legitimately 0 for TGP and Transco, whose reports state route,
    storage and rate-schedule figures and no system-wide number. Under A08 a zero
    must carry a supported population and a reason, because "the report states no
    total" and "we did not read the report" are different facts.
    """
    figures = f.get("_figures") or []
    members = [_fact_id(g["page"], g.get("row_index", 0), f.get("_text_layer", "yes"))
               for g in figures if g["kind"] == "table"]
    matched = [_fact_id(g["page"], g.get("row_index", 0), f.get("_text_layer", "yes"))
               for g in figures if g["kind"] == "table" and g["is_total"]]
    digest = hashlib.sha256("|".join(sorted(matched)).encode()).hexdigest()
    return {
        # A population belongs to exactly one derived observation. Empty
        # populations share SHA256(empty), so filing+digest alone allowed a
        # foreign/duplicate occurrence to overwrite another observation's
        # owner. Bind the durable ID to entity and observation as well.
        "population_id": "pop-" + hashlib.sha256(
            f"capacity|total|{entity_key}|{observation_id_value}|"
            f"{f['filing_id']}|{digest}".encode()).hexdigest()[:24],
        "observation_id": observation_id_value,
        "source_system": SOURCE_SYSTEM, "source_table": "source_facts",
        "filing_ids": json.dumps([f["filing_id"]]),
        "inclusion_rule": (
            "rows of this capacity report whose own row label states a total "
            "capacity (a 'total' row other than an obligation subtotal, or the "
            "explicit label 'Peak Day Capacity'), read positionally from the page"),
        "exclusion_rule": ("narrative (prose) figures are excluded -- a sentence is a "
                           "different assertion from a table total; route, directional and "
                           "rate-schedule rows are excluded because summing them would "
                           "invent a system figure the filer did not report"),
        "row_count": len(matched), "candidate_count": len(members),
        "excluded_count": len(members) - len(matched),
        "member_key": "source_fact_id", "member_digest": digest,
        "members_sample": json.dumps(sorted(matched)[:20]),
        "aggregate_value": None, "aggregate_unit": None,
        "empty_reason": (None if matched else
                         f"the report was retrieved and read ({len(members)} table rows "
                         f"extracted from {len(f.get('_doc') or [])} page(s)); NONE of them "
                         "carries a 'total' row label, or explicitly states 'Peak Day "
                         "Capacity', that qualifies as a system-capacity total. The filer "
                         "states no system-wide "
                         "figure, so no denominator exists and none is manufactured from a "
                         "route or a rate schedule. This is a property of the document, not "
                         "a retrieval or parsing failure."),
        "note": (f"{f['filing_id']} report year {f.get('reporting_year')}: candidate "
                 "denominators for cap_peak_day_ratio"),
        "created_at": ioc_adapter.utcnow()}


def _population_edge(observation_id_value, order, pop) -> dict:
    return {
        "observation_id": observation_id_value, "input_order": order,
        "input_role": "population", "operator_sign": "", "coefficient": 1.0,
        "input_source_system": pop["source_system"], "input_filing_id": None,
        "input_source_fact_id": None, "input_observation_id": None,
        "input_context_id": None, "input_concept": pop["source_table"],
        "input_period": None, "input_value": pop.get("aggregate_value"),
        "input_unit": pop.get("aggregate_unit"), "input_version_status": None,
        "input_population_id": pop["population_id"]}


def _gate_reasons(f, cand, peak) -> list[str]:
    """Which of the four gate dimensions fail, and exactly why."""
    reasons = []
    text = " ".join(p["text"] for page in f["_doc"] for p in page["rows"])

    storage_in_report = bool(STORAGE_WORDS.search(text))
    if storage_in_report:
        reasons.append(
            "STORAGE TREATMENT: the capacity report's total includes storage, LNG and "
            "peaking services (rate schedules such as GSS/LSS/LNG/FS appear in the same "
            "table), while the p.518 peak explicitly EXCLUDES deliveries to storage; the "
            "two figures do not cover the same facilities")
    else:
        reasons.append("STORAGE TREATMENT: not established -- the report does not state "
                       "whether storage deliverability is inside or outside this figure")

    if re.search(r"estimat|design|winter day", " ".join(
            [cand["section"], cand["row_label"], text[:4000]]), re.I):
        reasons.append(
            f"SEASON / DATE: the capacity figure is an ESTIMATED design-day or winter-day "
            f"capability stated as of {f['_as_of']}, whereas the p.518 peak is the OBSERVED "
            f"maximum delivery on an actual day in the reporting year; an estimate as of a "
            f"date and an observation on a day are not the same measurement")
    else:
        reasons.append("SEASON / DATE: the report does not state the season or design "
                       "condition of this figure")

    unit = (cand["unit"] or "").lower()
    if not unit:
        reasons.append("UNITS: no unit could be resolved for the capacity figure")
    elif unit.startswith("mdth"):
        reasons.append(f"UNITS: the capacity figure is in {cand['unit']} (thousand "
                       f"dekatherms) while p.518 is filed in {peak.unit_rule}; a 1,000x "
                       f"factor would have to be applied and no such conversion is applied "
                       f"silently")
    elif unit.startswith("mmbtu"):
        reasons.append(f"UNITS: {cand['unit']} and the p.518 dekatherm are dimensionally "
                       f"equal (1 Dth = 1 MMBtu by definition), so units alone would not "
                       f"block the ratio")
    else:
        reasons.append(f"UNITS: {cand['unit']} is not commensurate with {peak.unit_rule} "
                       f"without a stated conversion")

    reasons.append(
        "SYSTEM SCOPE: the capacity report describes the filer's whole system as designed, "
        "including facilities the p.518 peak-delivery schedule excludes; the two scopes "
        "were not shown to be identical, and a ratio across two scopes is refused")
    return reasons


# ---------------------------------------------------------------- remainder

def _account_for_remainder(entity, metrics, expected, produced, filings) -> list[dict]:
    entity_key = entity["entity_key"]
    # per (metric, as-of date), matching the per-cycle slot grain: a cycle that
    # legitimately has no value for one metric gets its OWN answer and is never
    # left to be satisfied by another cycle's value
    have = {(o["metric_id"], o["instant_date"] or "") for o in produced}
    out = []
    for slot in expected:
        if (slot["metric_id"], slot.get("instant_date") or "") in have:
            continue
        m = metrics.get(slot["metric_id"])
        if m is None:
            continue
        if not filings:
            state, detail = _RETRIEVAL_STATE.get(entity_key, ("no_hits", ""))
            if state == "image_only":
                # NOT_IMPLEMENTED is the honest availability: our parser, our gap,
                # counted as unfinished work rather than as a source condition.
                avail, reason = Availability.NOT_IMPLEMENTED, detail
            elif state in ("search_failed", "download_failed"):
                avail, reason = Availability.RETRIEVAL_FAILED, detail
            else:
                avail = Availability.UNVERIFIED_AVAILABILITY
                reason = (f"no '{CLASS_TYPE[1]}' filing was located in eLibrary for this "
                          f"filer in the requested window, so whether the {AUTHORITY} "
                          f"obligation applies could not be established; this is an "
                          f"unresolved applicability, not a retrieval failure")
        else:
            avail = Availability.NOT_IMPLEMENTED
            reason = (f"no observation was produced for this metric at the "
                      f"{slot.get('instant_date') or 'undated'} as-of grain")
        out.append(_obs(entity_key, m,
                        instant=slot.get("instant_date") or "",
                        year=slot["reporting_year"], scope=m.scope, unit=None,
                        value_text=None, value_num=None, availability=avail,
                        method=Method.DERIVED if m.dependencies else Method.DOCUMENT_EXTRACTED,
                        validation=Validation.NOT_YET_VALIDATED, missing_reason=reason,
                        applicability_evidence=slot["requirement_evidence"]))
    return out
