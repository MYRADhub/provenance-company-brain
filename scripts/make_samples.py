"""Write a realistic, messy deal folder to import: `samples/Northbeam Deals/`.

The folder looks like a PE fund's shared drive: one folder per deal, models saved as loose copies
("v2", "v2 - downside case (PS)", "v3 (DO comments)"), memos as Word files, call notes as text,
emails as .eml, a Slack export as Markdown and a QoE note as PDF. Document texts come from
app/seed_content.py, and every number a memo or deck quotes ties to a model file written here.

Also writes `samples/_live/Falcon/Models/Falcon LBO v4.xlsx`, the next save the demo drops into the folder
(relative paths under `_live` mirror those under `Northbeam Deals`).
"""

import json
import os
import shutil
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docx import Document  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Font, PatternFill  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet  # noqa: E402
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer  # noqa: E402

from app.seed_content import DEALS, DOCUMENTS, MEMBERSHIPS, USERS  # noqa: E402

SAMPLES = ROOT / "samples"
DEALS_ROOT = SAMPLES / "Northbeam Deals"
LIVE = SAMPLES / "_live"

YEARS = list(range(2025, 2031))
HEADER_FILL = PatternFill("solid", fgColor="1F2937")
INPUT_FONT = Font(color="1D4ED8")
BOLD = Font(bold=True)


# --------------------------------------------------------------------------- model math

def lbo(base_revenue, growth, margin, capex_pct, entry_ev, entry_equity, exit_multiple, exit_net_debt, hold=5):
    revenue = {y: round(base_revenue * (1 + growth) ** i, 2) for i, y in enumerate(YEARS)}
    ebitda = {y: round(revenue[y] * margin, 2) for y in YEARS}
    capex = {y: round(revenue[y] * capex_pct, 2) for y in YEARS}
    fcf = {y: round(ebitda[y] - capex[y], 2) for y in YEARS}
    exit_ev = round(ebitda[YEARS[-1]] * exit_multiple, 2)
    exit_equity = round(exit_ev - exit_net_debt, 2)
    moic = round(exit_equity / entry_equity, 2)
    irr = round((exit_equity / entry_equity) ** (1 / hold) - 1, 4)
    return {
        "inputs": {
            "base_revenue": base_revenue, "growth": growth, "margin": margin, "capex_pct": capex_pct,
            "entry_ev": entry_ev, "entry_equity": entry_equity, "exit_multiple": exit_multiple,
            "exit_net_debt": exit_net_debt, "hold": hold,
        },
        "revenue": revenue, "ebitda": ebitda, "capex": capex, "fcf": fcf,
        "exit_ev": exit_ev, "exit_equity": exit_equity, "moic": moic, "irr": irr,
    }


FALCON = dict(base_revenue=48.0, entry_ev=120.0, entry_equity=48.0, exit_net_debt=40.0)
HARBOR = dict(base_revenue=30.0, entry_ev=160.0, entry_equity=75.6, exit_net_debt=40.0)


def write_model(path: Path, *, company: str, codename: str, prepared_by: str, saved: str, m: dict,
                notes: list[str] | None = None) -> None:
    wb = Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover["B2"] = f"{codename} - LBO model"
    cover["B2"].font = Font(bold=True, size=14)
    cover["B4"], cover["C4"] = "Company", company
    cover["B5"], cover["C5"] = "Prepared by", prepared_by
    cover["B6"], cover["C6"] = "Status", "Draft - confidential"
    cover["B8"] = "Blue figures are inputs. All figures in $M unless stated."
    for i, note in enumerate(notes or []):
        cover[f"B{10 + i}"] = note
    cover.column_dimensions["B"].width = 28
    cover.column_dimensions["C"].width = 34

    a = wb.create_sheet("Assumptions")
    a["B2"] = "Key assumptions"
    a["B2"].font = BOLD
    rows = [
        ("FY25 revenue ($M)", m["inputs"]["base_revenue"], "0.00"),
        ("Revenue growth (annual)", m["inputs"]["growth"], "0.0%"),
        ("EBITDA margin", m["inputs"]["margin"], "0.0%"),
        ("Capex (% of revenue)", m["inputs"]["capex_pct"], "0.0%"),
        ("Entry enterprise value ($M)", m["inputs"]["entry_ev"], "0.00"),
        ("Equity cheque ($M)", m["inputs"]["entry_equity"], "0.00"),
        ("Exit multiple (x FY30 EBITDA)", m["inputs"]["exit_multiple"], '0.0"x"'),
        ("Net debt at exit ($M)", m["inputs"]["exit_net_debt"], "0.00"),
        ("Hold period (years)", m["inputs"]["hold"], "0"),
    ]
    for i, (label, value, fmt) in enumerate(rows, start=4):
        a[f"B{i}"] = label
        a[f"C{i}"] = value
        a[f"C{i}"].number_format = fmt
        a[f"C{i}"].font = INPUT_FONT
    a.column_dimensions["B"].width = 32

    op = wb.create_sheet("Operating Model")
    op["B2"] = "Operating model ($M)"
    op["B2"].font = BOLD
    op["B4"] = "($M)"
    for j, y in enumerate(YEARS):
        cell = op.cell(row=4, column=3 + j, value=f"FY{str(y)[2:]}")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
    lines = [
        ("Revenue", m["revenue"], "0.00"),
        ("Revenue growth", {y: (None if i == 0 else m["inputs"]["growth"]) for i, y in enumerate(YEARS)}, "0.0%"),
        ("EBITDA", m["ebitda"], "0.00"),
        ("EBITDA margin", {y: m["inputs"]["margin"] for y in YEARS}, "0.0%"),
        ("Capex", m["capex"], "0.00"),
        ("Free cash flow", m["fcf"], "0.00"),
    ]
    for i, (label, series, fmt) in enumerate(lines, start=5):
        op[f"B{i}"] = label
        for j, y in enumerate(YEARS):
            if series[y] is not None:
                c = op.cell(row=i, column=3 + j, value=series[y])
                c.number_format = fmt
    op.column_dimensions["B"].width = 22

    r = wb.create_sheet("Returns")
    r["B2"] = "Returns analysis"
    r["B2"].font = BOLD
    ret = [
        ("FY30 EBITDA ($M)", m["ebitda"][YEARS[-1]], "0.00"),
        ("Exit multiple", m["inputs"]["exit_multiple"], '0.0"x"'),
        ("Exit enterprise value ($M)", m["exit_ev"], "0.00"),
        ("Net debt at exit ($M)", m["inputs"]["exit_net_debt"], "0.00"),
        ("Exit equity value ($M)", m["exit_equity"], "0.00"),
        ("Equity invested ($M)", m["inputs"]["entry_equity"], "0.00"),
        ("MOIC", m["moic"], '0.00"x"'),
        ("Gross IRR", m["irr"], "0.0%"),
    ]
    for i, (label, value, fmt) in enumerate(ret, start=4):
        r[f"B{i}"] = label
        r[f"C{i}"] = value
        r[f"C{i}"].number_format = fmt
    r["B11"].font = BOLD
    r["B12"].font = BOLD
    r.column_dimensions["B"].width = 30

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    stamp(path, saved)


# --------------------------------------------------------------------------- file helpers

def stamp(path: Path, when: str) -> None:
    ts = datetime.fromisoformat(when).timestamp()
    os.utime(path, (ts, ts))


def doc_by_id(doc_id: str) -> dict:
    return next(d for d in DOCUMENTS if d["doc_id"] == doc_id)


def user_name(email: str) -> str:
    return next((u["name"] for u in USERS if u["email"] == email), email)


def write_docx(path: Path, doc: dict, when: str) -> None:
    d = Document()
    d.core_properties.author = user_name(doc["author_email"])
    d.core_properties.title = doc["title"]
    d.add_heading(doc["title"], level=1)
    for para in doc["body"].split("\n\n"):
        d.add_paragraph(para.strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)
    stamp(path, when)


def write_pdf(path: Path, doc: dict, when: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    story = [Paragraph(doc["title"], styles["Title"]), Spacer(1, 12)]
    for para in doc["body"].split("\n\n"):
        story.append(Paragraph(para.strip().replace("\n", "<br/>").replace("&", "&amp;"), styles["BodyText"]))
        story.append(Spacer(1, 8))
    SimpleDocTemplate(str(path), pagesize=letter, title=doc["title"],
                      author=user_name(doc["author_email"])).build(story)
    stamp(path, when)


def write_text(path: Path, text: str, when: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp(path, when)


def transcript_text(doc: dict) -> str:
    header = [doc["title"], f"Date: {doc['date']}"]
    if doc.get("participants"):
        header.append("Participants: " + ", ".join(doc["participants"]))
    return "\n".join(header) + "\n\n" + doc["body"].strip() + "\n"


def write_eml(path: Path, doc: dict, when: str) -> None:
    body = doc["body"]
    if body.startswith("From:"):
        body = body.split("\n\n", 1)[1]
    msg = EmailMessage()
    msg["From"] = f"{user_name(doc['from'])} <{doc['from']}>"
    msg["To"] = ", ".join(f"{user_name(e)} <{e}>" for e in doc.get("to", []))
    if doc.get("cc"):
        msg["Cc"] = ", ".join(f"{user_name(e)} <{e}>" for e in doc["cc"])
    msg["Subject"] = doc["title"].removeprefix("Email: ")
    msg["Date"] = format_datetime(datetime.fromisoformat(when).astimezone())
    msg.set_content(body.strip() + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(msg))
    stamp(path, when)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def deal_json(deal_id: str, lead: str) -> dict:
    deal = next(d for d in DEALS if d["id"] == deal_id)
    return {
        "codename": deal["codename"], "company": deal["company"], "sector": deal["sector"],
        "status": deal["status"], "summary": deal["summary"], "lead": lead,
        "team": [email for d, email in MEMBERSHIPS if d == deal_id],
    }


# --------------------------------------------------------------------------- corpus

FALCON_MODELS = [
    # file name, saved at, author, growth, margin, capex, exit multiple, cover notes
    ("Falcon LBO v1.xlsx", "2026-09-20T17:42:00", "david@northbeam.com", 0.15, 0.18, 0.03, 11.0, []),
    ("Falcon LBO v2.xlsx", "2026-10-02T21:15:00", "priya@northbeam.com", 0.12, 0.18, 0.03, 11.0, []),
    ("Falcon LBO v2 - downside case (PS).xlsx", "2026-10-04T16:20:00", "priya@northbeam.com",
     0.06, 0.16, 0.03, 11.0, ["Downside case: NorthStar and Pallas churn, margin pressure from pricing."]),
    ("Falcon LBO v3.xlsx", "2026-10-08T19:03:00", "priya@northbeam.com", 0.08, 0.18, 0.03, 11.0, []),
    ("Falcon LBO v3 (DO comments).xlsx", "2026-10-09T11:05:00", "david@northbeam.com", 0.08, 0.18, 0.035, 11.0,
     ["DO: capex nudged to 3.5% per QoE; check churn scenario before IC."]),
]
FALCON_LIVE = ("Falcon LBO v4.xlsx", "2026-10-12T15:30:00", "david@northbeam.com", 0.08, 0.18, 0.03, 9.5, [])
# Second scripted save for the debugging demo: a fat-fingered input (net debt at exit 40.0 typed as 4.0)
# that inflates IRR with no documented reason.
FALCON_LIVE_TYPO = ("Falcon LBO v5.xlsx", "2026-10-12T16:10:00", "priya@northbeam.com", 4.0)

HARBOR_MODELS = [
    ("Harbor operating model v1.xlsx", "2026-10-01T18:30:00", "marcus@northbeam.com", 0.28, 0.22, 0.04, 15.0),
    ("Harbor operating model v2.xlsx", "2026-10-07T17:10:00", "marcus@northbeam.com", 0.25, 0.22, 0.04, 14.0),
]


def build() -> None:
    if SAMPLES.exists():
        shutil.rmtree(SAMPLES)
    falcon = DEALS_ROOT / "Falcon"
    harbor = DEALS_ROOT / "Harbor"
    kestrel = DEALS_ROOT / "Kestrel"
    firm = DEALS_ROOT / "_firm"

    # ---------------------------------------------------------------- Falcon
    write_json(falcon / "deal.json", deal_json("falcon", "david@northbeam.com"))
    authors = {}
    for name, saved, author, g, mg, cx, xm, notes in FALCON_MODELS:
        m = lbo(growth=g, margin=mg, capex_pct=cx, exit_multiple=xm, **FALCON)
        write_model(falcon / "Models" / name, company="Acme Freight Systems", codename="Project Falcon",
                    prepared_by=user_name(author), saved=saved, m=m, notes=notes)
        authors[name] = author
    name, saved, author, g, mg, cx, xm, notes = FALCON_LIVE
    authors[name] = author
    authors.update({
        "Acme Freight - CIM summary.docx": "david@northbeam.com",
        "Project Falcon - IC memo DRAFT v0.3.docx": "david@northbeam.com",
        "Falcon IC deck - outline.md": "priya@northbeam.com",
        "Ardent QoE - preliminary findings.pdf": "priya@northbeam.com",
        "slack-export-deal-falcon-2026-10-09.md": "elena@northbeam.com",
    })
    write_json(falcon / "_authors.json", authors)
    write_model(LIVE / "Falcon" / "Models" / name, company="Acme Freight Systems", codename="Project Falcon",
                prepared_by=user_name(author), saved=saved, m=lbo(growth=g, margin=mg, capex_pct=cx,
                                                                  exit_multiple=xm, **FALCON), notes=notes)
    typo_name, typo_saved, typo_author, typo_debt = FALCON_LIVE_TYPO
    authors[typo_name] = typo_author
    write_json(falcon / "_authors.json", authors)
    write_model(LIVE / "Falcon" / "Models" / typo_name, company="Acme Freight Systems", codename="Project Falcon",
                prepared_by=user_name(typo_author), saved=typo_saved,
                m=lbo(growth=g, margin=mg, capex_pct=cx, exit_multiple=xm, **{**FALCON, "exit_net_debt": typo_debt}),
                notes=notes)

    write_docx(falcon / "Diligence" / "Acme Freight - CIM summary.docx", doc_by_id("falcon_cim_summary"),
               "2026-09-15T14:00:00")
    write_text(falcon / "Calls" / "2026-09-25 Ridgeway Logistics reference call.txt",
               transcript_text(doc_by_id("falcon_reference_call_ridgeway")), "2026-09-25T15:30:00")
    write_pdf(falcon / "Diligence" / "Ardent QoE - preliminary findings.pdf", doc_by_id("falcon_qoe_note"),
              "2026-09-29T10:00:00")
    write_docx(falcon / "Memos" / "Project Falcon - IC memo DRAFT v0.3.docx", doc_by_id("falcon_ic_memo_draft"),
               "2026-10-03T20:45:00")
    deck = doc_by_id("falcon_ic_deck")
    write_text(falcon / "Decks" / "Falcon IC deck - outline.md", f"# {deck['title']}\n\n{deck['body'].strip()}\n",
               "2026-10-04T18:00:00")
    write_text(falcon / "Calls" / "2026-10-06 Acme mgmt call - CFO (transcript).txt",
               transcript_text(doc_by_id("falcon_mgmt_call_2026_10_06")), "2026-10-06T12:30:00")
    write_text(falcon / "Calls" / "2026-10-07 expert call - freight software market.txt",
               transcript_text(doc_by_id("falcon_expert_call_2026_10_07")), "2026-10-07T16:00:00")
    write_eml(falcon / "Financing" / "Halyard Credit - Falcon unitranche indicative terms.eml",
              doc_by_id("falcon_lender_terms"), "2026-10-01T09:40:00")
    write_eml(falcon / "Email" / "RE Falcon model update - growth assumption.eml", doc_by_id("falcon_email_2026_10_08"),
              "2026-10-08T19:10:00")
    slack = doc_by_id("falcon_slack_2026_10_09")
    write_text(falcon / "Notes" / "slack-export-deal-falcon-2026-10-09.md", slack["body"].strip() + "\n",
               "2026-10-09T09:30:00")

    # ---------------------------------------------------------------- Harbor
    write_json(harbor / "deal.json", deal_json("harbor", "marcus@northbeam.com"))
    harbor_authors = {}
    last = None
    for name, saved, author, g, mg, cx, xm in HARBOR_MODELS:
        last = lbo(growth=g, margin=mg, capex_pct=cx, exit_multiple=xm, **HARBOR)
        write_model(harbor / "Models" / name, company="Brightline Health", codename="Project Harbor",
                    prepared_by=user_name(author), saved=saved, m=last)
        harbor_authors[name] = author
    harbor_authors["Harbor - model brief.docx"] = "marcus@northbeam.com"
    harbor_authors["Brightline - CIM summary.docx"] = "marcus@northbeam.com"
    write_json(harbor / "_authors.json", harbor_authors)
    write_docx(harbor / "Diligence" / "Brightline - CIM summary.docx", doc_by_id("harbor_cim_summary"),
               "2026-09-22T13:00:00")
    write_text(harbor / "Calls" / "2026-10-05 Brightline mgmt call.txt",
               transcript_text(doc_by_id("harbor_mgmt_call_2026_10_05")), "2026-10-05T17:00:00")
    brief = dict(doc_by_id("harbor_model_brief"))
    brief["body"] = (
        "Project Harbor - model brief. Marcus Lee, Oct 7 2026.\n\n"
        f"Base case (operating model v2): {last['inputs']['growth'] * 100:.1f}% annual revenue growth, "
        f"{last['inputs']['margin'] * 100:.1f}% EBITDA margin, FY26 revenue ${last['revenue'][2026]:.2f}M and "
        f"FY26 EBITDA ${last['ebitda'][2026]:.2f}M, exit at {last['inputs']['exit_multiple']:.1f}x FY30 EBITDA. "
        f"Gross IRR {last['irr'] * 100:.1f}%, MOIC {last['moic']:.2f}x.\n\n"
        "Downside: 18% growth, 12x exit, IRR 19%.\n\n"
        "Healthcare software multiples have held up better than logistics; no cap applied beyond the "
        "valuation policy's general rules."
    )
    write_docx(harbor / "Memos" / "Harbor - model brief.docx", brief, "2026-10-07T18:00:00")

    # ---------------------------------------------------------------- Kestrel
    write_json(kestrel / "deal.json", deal_json("kestrel", "david@northbeam.com"))
    write_docx(kestrel / "Kestrel - IC pass memo (Mar 2025).docx", doc_by_id("kestrel_ic_pass_memo"),
               "2025-03-12T17:00:00")

    # ---------------------------------------------------------------- firm-wide
    write_docx(firm / "Policies" / "Valuation and underwriting policy.docx", doc_by_id("policy_valuation"),
               "2026-01-15T09:00:00")
    write_docx(firm / "Policies" / "Information barrier policy.docx", doc_by_id("policy_information_barriers"),
               "2026-02-01T09:00:00")
    write_docx(firm / "Process" / "Investment committee process.docx", doc_by_id("ic_process"),
               "2026-01-15T09:30:00")
    write_text(firm / "Team expertise directory.md", "# Northbeam team expertise directory\n\n"
               + doc_by_id("expertise_directory")["body"].strip() + "\n", "2026-09-01T09:00:00")
    write_docx(firm / "Precedents" / "Precedent - logistics SaaS pass (2025, anonymised).docx",
               doc_by_id("precedent_logistics_saas_pass_2025"), "2025-04-01T09:00:00")
    write_json(firm / "_authors.json", {
        "Valuation and underwriting policy.docx": "elena@northbeam.com",
        "Information barrier policy.docx": "sam@northbeam.com",
        "Investment committee process.docx": "elena@northbeam.com",
        "Team expertise directory.md": "elena@northbeam.com",
        "Precedent - logistics SaaS pass (2025, anonymised).docx": "elena@northbeam.com",
    })



def main() -> None:
    build()
    files = sorted(p.relative_to(SAMPLES) for p in SAMPLES.rglob("*") if p.is_file())
    print(f"wrote {len(files)} files under {SAMPLES}")
    for f in files:
        print(" ", f)


if __name__ == "__main__":
    main()
