"""
Namespace-aware XBRL instance parser for the repaired Transco ten-year pack.

Replaces the v1 regex parser (fetch_transco_10y.py::parse_instance), whose two
material defects were confirmed against source before this rewrite:

  * its fact regex matched ANY `ferc:`-prefixed open/close element pair, so the
    typed-dimension domain values inside <context> elements were emitted as
    26,170 spurious "fact" rows, while self-closing / xsi:nil facts were missed;
  * its dimension regex captured the first text node after the member opening
    tag, which for <xbrldi:typedMember> is whitespace before the child element,
    so 71,071 fact rows lost their typed-member value.

Definition used here (task section 5A): an XBRL element is a reported fact
if and only if it carries a `contextRef` attribute. Typed-dimension domain
values never carry contextRef and are stored as context metadata instead.

Both instance dialects in the window parse identically because ElementTree
resolves default namespaces: native instances (2021 Q3 onward) prefix the
structural elements with `xbrli:`; FERC-migrated instances (pre-2021) make the
XBRL instance namespace the DEFAULT namespace.

Stdlib only (xml.etree.ElementTree; lxml is not installed in this environment).
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"

STRUCTURAL_TAGS = {
    f"{{{XBRLI}}}context", f"{{{XBRLI}}}unit", f"{{{XBRLI}}}entity",
    f"{{{XBRLI}}}period", f"{{{XBRLI}}}segment", f"{{{XBRLI}}}scenario",
}


def _split(tag: str) -> tuple[str, str]:
    """'{ns}local' -> (ns, local)."""
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def _qname(tag: str, nsmap: dict[str, str]) -> str:
    ns, local = _split(tag)
    prefix = nsmap.get(ns)
    return f"{prefix}:{local}" if prefix else local


def _build_nsmap(path: str) -> dict[str, str]:
    """URI -> preferred prefix, from the document's own declarations."""
    nsmap: dict[str, str] = {}
    for event, (prefix, uri) in ET.iterparse(path, events=["start-ns"]):
        if uri not in nsmap and prefix:
            nsmap[uri] = prefix
    # sensible defaults where the document used a default namespace
    nsmap.setdefault(XBRLI, "xbrli")
    nsmap.setdefault(XBRLDI, "xbrldi")
    return nsmap


def parse_instance(path: str) -> dict:
    """
    Parse one XBRL instance file.

    Returns {
      "contexts":  {context_id: {...}},          # 5B
      "units":     {unit_id: {...}},             # 5E
      "facts":     [ {...}, ... ],               # 5A, document order
      "schema_ref": str,
      "taxonomy_ns": str,                        # ferc core namespace URI
      "nsmap": {uri: prefix},
    }
    """
    nsmap = _build_nsmap(path)
    root = ET.parse(path).getroot()

    schema_ref = ""
    for sr in root.iter(f"{{{LINK}}}schemaRef"):
        schema_ref = sr.get(f"{{{XLINK}}}href") or ""
        break
    taxonomy_ns = next((uri for uri in nsmap
                        if uri.startswith("http://ferc.gov/form/") and uri.endswith("/ferc")), "")

    contexts: dict[str, dict] = {}
    for ctx in root.iter(f"{{{XBRLI}}}context"):
        cid = ctx.get("id")
        entity_id, scheme = "", ""
        ent = ctx.find(f"{{{XBRLI}}}entity")
        if ent is not None:
            ident = ent.find(f"{{{XBRLI}}}identifier")
            if ident is not None:
                entity_id = (ident.text or "").strip()
                scheme = ident.get("scheme") or ""
        per = ctx.find(f"{{{XBRLI}}}period")
        instant = start = end = ""
        if per is not None:
            for tag, attr in ((f"{{{XBRLI}}}instant", "instant"),
                              (f"{{{XBRLI}}}startDate", "start"),
                              (f"{{{XBRLI}}}endDate", "end")):
                el = per.find(tag)
                if el is not None:
                    if attr == "instant":
                        instant = (el.text or "").strip()
                    elif attr == "start":
                        start = (el.text or "").strip()
                    else:
                        end = (el.text or "").strip()

        explicit: list[dict] = []
        typed: list[dict] = []
        containers = set()
        for container_tag in ("segment", "scenario"):
            for container in ctx.iter(f"{{{XBRLI}}}{container_tag}"):
                for em in container.iter(f"{{{XBRLDI}}}explicitMember"):
                    axis = em.get("dimension") or ""
                    member = (em.text or "").strip()
                    explicit.append({
                        "container": container_tag,
                        "axis_qname": axis,
                        "axis_local": axis.split(":")[-1],
                        "member_qname": member,
                        "member_local": member.split(":")[-1],
                        "member_ns": nsmap_reverse(nsmap, member),
                    })
                    containers.add(container_tag)
                for tm in container.iter(f"{{{XBRLDI}}}typedMember"):
                    axis = tm.get("dimension") or ""
                    for child in tm:
                        cns, clocal = _split(child.tag)
                        typed.append({
                            "container": container_tag,
                            "axis_qname": axis,
                            "axis_local": axis.split(":")[-1],
                            "domain_qname": _qname(child.tag, nsmap),
                            "domain_local": clocal,
                            "domain_ns": cns,
                            "value": (child.text or "").strip(),
                            "serialized": ET.tostring(child, encoding="unicode").strip(),
                        })
                        containers.add(container_tag)
        contexts[cid] = {
            "entity_identifier": entity_id, "scheme": scheme,
            "instant": instant, "start": start, "end": end,
            "containers": sorted(containers),
            "explicit": explicit, "typed": typed,
        }

    units: dict[str, dict] = {}
    for unit in root.iter(f"{{{XBRLI}}}unit"):
        uid = unit.get("id")
        divide = unit.find(f"{{{XBRLI}}}divide")
        if divide is not None:
            num = [(m.text or "").strip() for m in
                   divide.find(f"{{{XBRLI}}}unitNumerator").iter(f"{{{XBRLI}}}measure")]
            den = [(m.text or "").strip() for m in
                   divide.find(f"{{{XBRLI}}}unitDenominator").iter(f"{{{XBRLI}}}measure")]
        else:
            num = [(m.text or "").strip() for m in unit.iter(f"{{{XBRLI}}}measure")]
            den = []
        text = "*".join(num) + (("/" + "*".join(den)) if den else "")
        units[uid] = {
            "numerators": num, "denominators": den, "text": text,
            "raw_xml": ET.tostring(unit, encoding="unicode").strip(),
        }

    facts: list[dict] = []
    for el in root.iter():
        context_ref = el.get("contextRef")
        if context_ref is None:
            continue
        ns, local = _split(el.tag)
        nil = el.get(f"{{{XSI}}}nil") == "true"
        facts.append({
            "qname": _qname(el.tag, nsmap),
            "ns": ns,
            "local": local,
            "context_ref": context_ref,
            "unit_ref": el.get("unitRef") or "",
            "decimals": el.get("decimals") or "",
            "precision": el.get("precision") or "",
            "value": "" if el.text is None else el.text,   # exactly as filed; "" for self-closing
            "nil": nil,
        })

    return {"contexts": contexts, "units": units, "facts": facts,
            "schema_ref": schema_ref, "taxonomy_ns": taxonomy_ns, "nsmap": nsmap}


def nsmap_reverse(nsmap: dict[str, str], qname: str) -> str:
    """Best-effort namespace URI for a prefixed QName like 'ferc:GasUtilityMember'."""
    if ":" not in qname:
        return ""
    prefix = qname.split(":", 1)[0]
    for uri, p in nsmap.items():
        if p == prefix:
            return uri
    return ""


def classify_period(ctx: dict, filing_year: int) -> tuple[str, str, int | str]:
    """
    (period_classification, CY/PY, duration_days) from actual dates.

    instant / monthly / quarter / ytd / annual, with the CY/PY flag carrying
    the prior-year distinction (task 5B: prior-year quarter == quarter + PY).
    Never derived from context-ID naming conventions.
    """
    if ctx.get("instant"):
        return "instant", ("CY" if ctx["instant"][:4] == str(filing_year) else "PY"), ""
    s, e = ctx.get("start"), ctx.get("end")
    if not s or not e:
        return "other_ambiguous", "", ""
    try:
        days = (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days + 1
    except ValueError:
        return "other_ambiguous", "", ""
    cy = "CY" if e[:4] == str(filing_year) else "PY"
    if 28 <= days <= 31:
        return "monthly", cy, days
    if 85 <= days <= 95:
        return "quarter", cy, days
    if days >= 350:
        return "annual", cy, days
    if days >= 175:
        return "ytd", cy, days
    return f"other_{days}d", cy, days
