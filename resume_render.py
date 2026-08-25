"""Parse the master resume into a structured document and render it as a PDF.

The first tailoring slice bulleted every markdown line, so headings, the
contact line and raw ``**`` markers all reached the PDF as bullet points. The
fix is to stop treating the resume as a flat list of lines: parse it once into
name/sections/entries/bullets, tailor that structure, and render each part with
the style it actually deserves.

Layout is deliberately single column with standard fonts and no tables, since
that is what ATS parsers read reliably.
"""

from __future__ import annotations

import re
from io import BytesIO
from xml.sax.saxutils import escape


PAGE_LIMIT = 1
MIN_BULLETS_PER_ENTRY = 2
BODY_SIZES = (9.6, 9.2, 8.8, 8.4)


def _clean(text: str) -> str:
    """Strip markdown emphasis so no ``**`` or ``##`` can reach the PDF."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    return text.replace("`", "").strip()


LABEL_RE = re.compile(r"^([A-Z][^:—]{2,38})(:|\s—)")


def _bold(text: str, restore_label: bool = False) -> str:
    """Convert markdown bold to the inline markup reportlab understands.

    A rewritten bullet comes back from the model as plain text, so a skill
    group or project name would silently lose the emphasis the master resume
    gives it. The label is restored only for lines that were emphasised in the
    master, so tailoring never introduces bold where the original had none.
    """
    markup = escape(text.replace("`", ""))
    if "**" in markup:
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", markup)
    return LABEL_RE.sub(r"<b>\1</b>\2", markup) if restore_label else markup


def parse_resume(markdown: str) -> dict:
    """Turn the master markdown into the structure the renderer draws.

    Every bullet gets a stable ref so a tailoring model can cite exactly which
    source line it rewrote, and the validator can check that rewrite against
    the original text rather than against the resume as a whole.
    """
    document: dict = {"name": "", "headline": "", "contact": [], "summary": "", "sections": []}
    section: dict | None = None
    entry: dict | None = None
    counter = 0
    in_summary = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("# ") and not stripped.startswith("##"):
            document["name"] = _clean(stripped[2:])
            continue

        if stripped.startswith("## "):
            heading = _clean(stripped[3:]).upper()
            in_summary = heading == "SUMMARY"
            entry = None
            if in_summary:
                section = None
                continue
            section = {"heading": heading, "entries": [], "lines": []}
            document["sections"].append(section)
            continue

        if stripped.startswith("### "):
            title, _, meta = _clean(stripped[4:]).partition(" | ")
            entry = {"title": title.strip(), "meta": meta.strip(), "lead": "", "bullets": []}
            if section is not None:
                section["entries"].append(entry)
            continue

        if in_summary:
            document["summary"] = (document["summary"] + " " + _clean(stripped)).strip()
            continue

        if section is None:
            # Anything above the first heading that is not the name is contact
            # detail: the role line and the phone/email/link lines.
            if not document["headline"] and stripped.startswith("**"):
                document["headline"] = _clean(stripped)
            else:
                document["contact"].append(_clean(stripped))
            continue

        if stripped.startswith("- "):
            counter += 1
            # text keeps its emphasis for rendering; source is the plain
            # wording the evidence check compares a rewrite against.
            raw = stripped[2:].strip()
            item = {"ref": f"b{counter}", "text": raw, "source": _clean(raw),
                    "emphasis": "**" in raw}
            if entry is not None:
                entry["bullets"].append(item)
            else:
                section["lines"].append(item)
            continue

        # A plain line inside a section: an entry's one-line description, or
        # the standing intro a section like PROJECTS opens with.
        if entry is not None and not entry["bullets"]:
            entry["lead"] = _clean(stripped)
        else:
            section.setdefault("intro", "")
            section["intro"] = (section.get("intro", "") + " " + _clean(stripped)).strip()

    return document


def source_refs(document: dict) -> dict[str, str]:
    """Map every bullet ref to its original text, for evidence checking."""
    refs: dict[str, str] = {}
    for section in document["sections"]:
        for entry in section["entries"]:
            for bullet in entry["bullets"]:
                refs[bullet["ref"]] = bullet["source"]
        for line in section["lines"]:
            refs[line["ref"]] = line["source"]
    return refs


def _story(document: dict, size: float):
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, ListFlowable, ListItem, Paragraph, Spacer

    name_style = ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=size + 7.4,
                                leading=size + 9, alignment=TA_CENTER, spaceAfter=1)
    headline_style = ParagraphStyle("headline", fontName="Helvetica-Bold", fontSize=size + 0.8,
                                    leading=size + 3, alignment=TA_CENTER, spaceAfter=1)
    contact_style = ParagraphStyle("contact", fontName="Helvetica", fontSize=size - 1.2,
                                   leading=size + 1, alignment=TA_CENTER)
    heading_style = ParagraphStyle("heading", fontName="Helvetica-Bold", fontSize=size + 0.6,
                                   leading=size + 2.5, spaceBefore=6, spaceAfter=1)
    title_style = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=size,
                                 leading=size + 2, spaceBefore=3)
    meta_style = ParagraphStyle("meta", fontName="Helvetica-Oblique", fontSize=size - 1.2,
                                leading=size + 1, spaceAfter=1)
    body_style = ParagraphStyle("body", fontName="Helvetica", fontSize=size, leading=size + 2.6)
    bullet_style = ParagraphStyle("bullet", parent=body_style, spaceAfter=1.2)

    story = [Paragraph(escape(document["name"]), name_style)]
    if document["headline"]:
        story.append(Paragraph(escape(document["headline"]), headline_style))
    for line in document["contact"]:
        story.append(Paragraph(escape(line), contact_style))
    if document["summary"]:
        story.append(Spacer(1, 5))
        story.append(Paragraph(escape(document["summary"]), body_style))

    def bullet_list(items):
        return ListFlowable(
            [ListItem(Paragraph(_bold(item["text"], item.get("emphasis", False)), bullet_style),
                      leftIndent=9)
             for item in items],
            bulletType="bullet", bulletChar="•", bulletFontSize=size - 1.5,
            leftIndent=9, start=None, spaceBefore=1,
        )

    for section in document["sections"]:
        # A section can be nothing but its intro. LANGUAGES is exactly that --
        # "Arabic (native) . English (professional)" is a plain line, and
        # parse_resume files a plain line under "intro" because only a "- " line
        # becomes a bullet. Skipping a section on entries and lines alone
        # therefore dropped LANGUAGES out of every PDF this has ever rendered,
        # tailored or not, including on jobs that ask for the languages.
        if not section["entries"] and not section["lines"] and not section.get("intro"):
            continue
        story.append(Paragraph(escape(section["heading"]), heading_style))
        story.append(HRFlowable(width="100%", thickness=0.6, spaceAfter=3, spaceBefore=0))
        if section.get("intro"):
            story.append(Paragraph(escape(section["intro"]), body_style))
        for entry in section["entries"]:
            story.append(Paragraph(_bold(entry["title"]), title_style))
            if entry["meta"]:
                story.append(Paragraph(escape(entry["meta"]), meta_style))
            if entry["lead"]:
                story.append(Paragraph(escape(entry["lead"]), body_style))
            if entry["bullets"]:
                story.append(bullet_list(entry["bullets"]))
        if section["lines"]:
            story.append(bullet_list(section["lines"]))

    return story, mm


def _build(document: dict, size: float) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate

    story, mm = _story(document, size)
    output = BytesIO()
    SimpleDocTemplate(
        output, pagesize=A4,
        topMargin=12 * mm, bottomMargin=12 * mm,
        leftMargin=15 * mm, rightMargin=15 * mm,
        title=f"{document['name']} — Resume", author=document["name"],
    ).build(story)
    return output.getvalue()


def _page_count(pdf: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(BytesIO(pdf)).pages)


def _trim_once(document: dict) -> bool:
    """Remove the least valuable remaining content. False when nothing is left.

    Order matters: the oldest role's extra bullets go before anything recent,
    and a role is only dropped whole once it has nothing left to give.
    """
    entries = [entry for section in document["sections"] for entry in section["entries"]]
    for entry in reversed(entries):
        if len(entry["bullets"]) > MIN_BULLETS_PER_ENTRY:
            entry["bullets"].pop()
            return True
    # Late sections first. Iterating forward took a project away before a sixth
    # certificate, and both before a skills line the job actually asked for.
    # Within a section the last item goes, which after apply_plan's ordering is
    # the one the job description supported least. An intro-only section holds
    # neither entries nor lines, so nothing here can reach it.
    for section in reversed(document["sections"]):
        if len(section["lines"]) > MIN_BULLETS_PER_ENTRY:
            section["lines"].pop()
            return True
    for section in reversed(document["sections"]):
        if len(section["entries"]) > 1:
            section["entries"].pop()
            return True
    for entry in reversed(entries):
        if entry["bullets"]:
            entry["bullets"].pop()
            return True
    return False


def render_pdf(document: dict, page_limit: int = PAGE_LIMIT) -> bytes:
    """Render to PDF, shrinking then trimming until it fits the page limit."""
    for size in BODY_SIZES:
        pdf = _build(document, size)
        if _page_count(pdf) <= page_limit:
            return pdf
    while _trim_once(document):
        pdf = _build(document, BODY_SIZES[-1])
        if _page_count(pdf) <= page_limit:
            return pdf
    raise ValueError("The resume could not be reduced to the page limit.")
