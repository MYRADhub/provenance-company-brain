"""The fictional world of Northbeam Capital, as plain Python data.

Structured facts (users, deals, memberships, model versions, references) go to SQLite.
DOCUMENTS go to HydraDB. The reason behind each model change lives only in DOCUMENTS.
"""

DOMAIN = "northbeam.com"
FIRM_ACL = [f"domain:{DOMAIN}"]


def deal_acl(deal_id: str) -> list[str]:
    return [f"group:northbeam:{deal_id}"]


USERS = [
    {"email": "elena@northbeam.com", "name": "Elena Ruiz", "title": "Managing Partner", "role": "managing_partner"},
    {"email": "david@northbeam.com", "name": "David Okafor", "title": "Principal", "role": "principal"},
    {"email": "priya@northbeam.com", "name": "Priya Shah", "title": "Associate", "role": "associate"},
    {"email": "marcus@northbeam.com", "name": "Marcus Lee", "title": "Associate", "role": "associate"},
    {"email": "sam@northbeam.com", "name": "Sam Patel", "title": "Chief Compliance Officer", "role": "compliance"},
]

DEALS = [
    {
        "id": "falcon",
        "codename": "Project Falcon",
        "company": "Acme Freight Systems",
        "sector": "Logistics software",
        "status": "diligence",
        "summary": "Buyout of a transportation management SaaS vendor for mid-market shippers and 3PLs. IC meeting Oct 15 2026.",
    },
    {
        "id": "harbor",
        "codename": "Project Harbor",
        "company": "Brightline Health",
        "sector": "Healthcare software",
        "status": "LOI",
        "summary": "Growth buyout of a patient scheduling platform for outpatient clinics. LOI signed, confirmatory diligence under way.",
    },
    {
        "id": "kestrel",
        "codename": "Project Kestrel",
        "company": "Swiftroute",
        "sector": "Logistics software",
        "status": "passed",
        "summary": "Route optimisation SaaS. Passed at IC in March 2025 on customer concentration and churn.",
    },
]

MEMBERSHIPS = [
    ("falcon", "elena@northbeam.com"),
    ("falcon", "david@northbeam.com"),
    ("falcon", "priya@northbeam.com"),
    ("harbor", "elena@northbeam.com"),
    ("harbor", "marcus@northbeam.com"),
    ("kestrel", "elena@northbeam.com"),
    ("kestrel", "david@northbeam.com"),
]

# ---------------------------------------------------------------------------
# Falcon operating model. A simple five-year-hold buyout: entry at a fixed EV,
# exit at FY30 EBITDA times the exit multiple, less the net debt still owed.
# ---------------------------------------------------------------------------

BASE_FY25_REVENUE = 48.0
ENTRY_EV = 120.0
ENTRY_EQUITY = 48.0
EXIT_NET_DEBT = 40.0
HOLD_YEARS = 5


def compute_model(growth: float, margin: float, exit_multiple: float) -> dict:
    revenue = {}
    ebitda = {}
    for i, year in enumerate(range(2025, 2031)):
        rev = BASE_FY25_REVENUE * (1 + growth) ** i
        revenue[year] = round(rev, 2)
        ebitda[year] = round(rev * margin, 2)
    exit_ev = round(ebitda[2030] * exit_multiple, 2)
    exit_equity = round(exit_ev - EXIT_NET_DEBT, 2)
    moic = round(exit_equity / ENTRY_EQUITY, 2)
    irr = round((exit_equity / ENTRY_EQUITY) ** (1 / HOLD_YEARS) - 1, 4)
    return {
        "revenue": revenue,
        "ebitda": ebitda,
        "exit_ev": exit_ev,
        "exit_equity": exit_equity,
        "metrics": {
            "revenue_growth": growth,
            "ebitda_margin": margin,
            "fy26_revenue": revenue[2026],
            "fy26_ebitda": ebitda[2026],
            "exit_multiple": exit_multiple,
            "irr": irr,
            "moic": moic,
        },
    }


# Notes are what Excel records on save: who and when. Never why.
MODEL_VERSIONS = [
    {"deal_id": "falcon", "version": 1, "author_email": "david@northbeam.com", "saved_at": "2026-09-20T17:42:00",
     "note": "Initial model from CIM", "growth": 0.15, "margin": 0.18, "exit_multiple": 11.0},
    {"deal_id": "falcon", "version": 2, "author_email": "priya@northbeam.com", "saved_at": "2026-10-02T21:15:00",
     "note": "Saved from Excel", "growth": 0.12, "margin": 0.18, "exit_multiple": 11.0},
    {"deal_id": "falcon", "version": 3, "author_email": "priya@northbeam.com", "saved_at": "2026-10-08T19:03:00",
     "note": "Saved from Excel", "growth": 0.08, "margin": 0.18, "exit_multiple": 11.0},
    {"deal_id": "falcon", "version": 4, "author_email": "david@northbeam.com", "saved_at": "2026-10-12T15:30:00",
     "note": "Saved from Excel", "growth": 0.08, "margin": 0.18, "exit_multiple": 9.5},
]

SEEDED_VERSIONS = (1, 2, 3)


def version_spec(deal_id: str, version: int) -> dict:
    for spec in MODEL_VERSIONS:
        if spec["deal_id"] == deal_id and spec["version"] == version:
            return spec
    raise KeyError(f"no model version {deal_id} v{version}")


def _v(version: int) -> dict:
    spec = version_spec("falcon", version)
    return compute_model(spec["growth"], spec["margin"], spec["exit_multiple"])


V1 = _v(1)
V2 = _v(2)
V3 = _v(3)
V2M = V2["metrics"]
V3M = V3["metrics"]


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def money(x: float) -> str:
    return f"${x:.2f}M"


# ---------------------------------------------------------------------------
# Documents. kind is the HydraDB app class; doc_kind is our own label.
# ---------------------------------------------------------------------------

DOCUMENTS = [
    # ------------------------------------------------------------------ Falcon
    {
        "doc_id": "falcon_cim_summary",
        "title": "Project Falcon - CIM summary (Acme Freight Systems)",
        "deal_id": "falcon", "kind": "knowledge_base", "doc_kind": "cim",
        "date": "2026-09-15", "author_email": "david@northbeam.com", "acl": deal_acl("falcon"),
        "body": f"""Project Falcon - Confidential Information Memorandum summary
Target: Acme Freight Systems ("Acme"). Sell-side adviser: Halvorsen & Co. Prepared by David Okafor, Sep 15 2026.

Business. Acme sells a cloud transportation management system (TMS) to mid-market shippers and third-party logistics providers (3PLs). The product covers load tendering, carrier rate management, freight audit and dock scheduling. Roughly 85% of revenue is recurring subscription; the rest is implementation and professional services.

Financials (management figures). FY25 revenue was {money(BASE_FY25_REVENUE)}, up 22% on FY24. ARR at fiscal year end was $46.0M. FY25 adjusted EBITDA margin was 18%. Net revenue retention was 112% and gross logo retention 93%.

Management case. The CIM projects 18-20% annual revenue growth through FY30 with margin expanding to 25%. We do not underwrite the margin expansion and hold margin flat at 18% in our model.

Customers. 410 customers. The top 10 account for 31% of ARR. The two largest enterprise accounts are NorthStar Grocers (about 5% of ARR, customer since 2019) and Pallas Retail (about 4% of ARR, customer since 2021). Both are on three-year contracts renewing in FY26.

Management. CEO Linda Marsh (founder). CFO Tom Becker (joined 2023, previously at a public freight brokerage).

Process. Halvorsen is guiding to an enterprise value around $120M. First-round bids were due Sep 26; we submitted at $115-125M. Final bids are due Oct 20, after our IC on Oct 15.

Initial view. Attractive product with real switching costs. Key diligence questions: quality of FY25 growth, concentration in the enterprise segment, and sensitivity to the freight cycle.""",
    },
    {
        "doc_id": "falcon_reference_call_ridgeway",
        "title": "Project Falcon - customer reference call, Ridgeway Foods",
        "deal_id": "falcon", "kind": "meeting", "doc_kind": "transcript",
        "date": "2026-09-25", "author_email": "priya@northbeam.com", "acl": deal_acl("falcon"),
        "participants": ["Priya Shah", "Dana Whitfield (Ridgeway Foods, Director of Logistics)"],
        "body": """Customer reference call - Ridgeway Foods. Sep 25 2026. Notes by Priya Shah.

Ridgeway is a regional food distributor, about 40 trucks plus brokered loads, Acme customer for four years.

Priya: Why did you choose Acme over the larger TMS vendors?
Dana Whitfield: Implementation took ten weeks, not nine months. The carrier rate management module paid for itself in the first year; we cut spot freight spend by about 7%.

Priya: Any plans to switch?
Dana: No. Our dispatchers live in it. Switching would mean retraining 30 people and re-integrating our WMS.

Priya: Pricing?
Dana: We took a 6% increase last renewal without much argument. If they pushed 15% we would look around.

Priya: Weak spots?
Dana: Reporting is clunky, and support slowed down last winter during peak season.

Takeaway: strong stickiness in the mid-market core. Mid-market customers like Ridgeway are not the concentration risk; the enterprise accounts are.""",
    },
    {
        "doc_id": "falcon_qoe_note",
        "title": "Project Falcon - quality of earnings findings (Ardent Advisory)",
        "deal_id": "falcon", "kind": "knowledge_base", "doc_kind": "diligence",
        "date": "2026-09-29", "author_email": "priya@northbeam.com", "acl": deal_acl("falcon"),
        "body": """Project Falcon - Quality of earnings (QoE) findings. Ardent Advisory draft report, summarised by Priya Shah, Sep 29 2026.

EBITDA. Ardent confirms FY25 adjusted EBITDA within 2% of management's figure. Add-backs are reasonable apart from $0.3M of recurring "one-off" recruiting costs, which we treat as ongoing. Margin of 18% holds.

Revenue quality. FY25 reported growth of 22% is flattered by two items:
1. $1.9M of one-time implementation revenue from a large onboarding in Q4 FY25.
2. Roughly $0.8M of multi-year prepaid subscriptions recognised early after a billing change.
Adjusting for both, organic recurring growth is closer to 13-14%.

Bookings. Q2 and Q3 FY26 new bookings were pulled forward into late FY25 by year-end discounting. Ardent flags this as a risk to FY26 growth.

Recommendation. Ardent suggests haircutting the management growth case to roughly 12% for underwriting.

Action. Priya to update the operating model to the QoE-adjusted growth rate.""",
    },
    {
        "doc_id": "falcon_ic_memo_draft",
        "title": "Project Falcon - IC memo draft",
        "deal_id": "falcon", "kind": "knowledge_base", "doc_kind": "ic_memo",
        "date": "2026-10-03", "author_email": "david@northbeam.com", "acl": deal_acl("falcon"),
        "body": f"""Project Falcon - Investment Committee memo (DRAFT). Prepared by David Okafor and Priya Shah, Oct 3 2026. IC meeting: Oct 15 2026.

Recommendation. Approve a final bid for 100% of Acme Freight Systems at an enterprise value of {money(ENTRY_EV)} (about 13.9x FY25 EBITDA), funded with {money(ENTRY_EQUITY)} of equity and {money(ENTRY_EV - ENTRY_EQUITY)} of unitranche debt.

Investment thesis.
1. Sticky mid-market TMS franchise with 93% gross logo retention and 112% net revenue retention.
2. Underwritten growth of {pct(V2M['revenue_growth'])} a year, a haircut to the CIM case following the quality of earnings work.
3. Pricing power: reference customers accept 5-7% annual increases.

Base case returns (operating model v2).
- Revenue growth: {pct(V2M['revenue_growth'])} a year, FY26-FY30
- FY26 revenue: {money(V2M['fy26_revenue'])}; FY26 EBITDA: {money(V2M['fy26_ebitda'])}
- EBITDA margin held flat at {pct(V2M['ebitda_margin'])}
- Exit multiple: {V2M['exit_multiple']:.1f}x FY30 EBITDA
- Gross IRR: {pct(V2M['irr'])}; MOIC: {V2M['moic']:.2f}x
This clears the fund's 20% gross IRR hurdle.

Key risks.
- Customer concentration: top 10 customers are 31% of ARR; the two largest enterprise accounts renew in FY26.
- Freight cycle: shipper budgets are under pressure in a soft freight market.
- Exit multiple: logistics software multiples have been volatile.

Next steps. Management call with the CFO on Oct 6, expert call on freight market conditions, final model by Oct 10.""",
    },
    {
        "doc_id": "falcon_ic_deck",
        "title": "Project Falcon - IC deck outline",
        "deal_id": "falcon", "kind": "knowledge_base", "doc_kind": "deck",
        "date": "2026-10-04", "author_email": "priya@northbeam.com", "acl": deal_acl("falcon"),
        "body": f"""Project Falcon - IC presentation outline. Owner: Priya Shah. Oct 4 2026.

Slide 1 - Transaction overview: {money(ENTRY_EV)} EV, {money(ENTRY_EQUITY)} equity cheque, 5-year hold.
Slide 2 - Business overview: TMS for mid-market shippers and 3PLs, 410 customers, 85% recurring.
Slide 3 - Why we win: ten-week implementations, carrier rate management ROI, 93% gross logo retention.
Slide 4 - Financial summary: FY26 revenue {money(V2M['fy26_revenue'])}, FY26 EBITDA {money(V2M['fy26_ebitda'])}, growth {pct(V2M['revenue_growth'])} a year.
Slide 5 - Returns: {pct(V2M['irr'])} gross IRR, {V2M['moic']:.2f}x MOIC at a {V2M['exit_multiple']:.1f}x exit.
Slide 6 - Sensitivities: IRR by growth (8-16%) and exit multiple (9-12x).
Slide 7 - Risks and mitigants: concentration, freight cycle, exit multiple.
Slide 8 - Recommendation: approve final bid.""",
    },
    {
        "doc_id": "falcon_mgmt_call_2026_10_06",
        "title": "Project Falcon - management call with CFO Tom Becker",
        "deal_id": "falcon", "kind": "meeting", "doc_kind": "transcript",
        "date": "2026-10-06", "author_email": "priya@northbeam.com", "acl": deal_acl("falcon"),
        "participants": ["David Okafor", "Priya Shah", "Tom Becker (Acme CFO)", "Linda Marsh (Acme CEO)"],
        "body": """Management call transcript - Acme Freight Systems. Oct 6 2026, 10:00 PT. Recorded and transcribed; edited for length.
Attendees: David Okafor, Priya Shah (Northbeam); Tom Becker, CFO; Linda Marsh, CEO (joined for the second half).

David: Thanks for making time, Tom. We want to walk through Q3 and the FY26 renewal book.

Tom Becker: Sure. Q3 was softer than we planned. New bookings came in at $2.1M against a $3.0M plan. Some of that is the pull-forward into Q4 last year that your QoE team already found, and some is longer enterprise sales cycles. Shippers are delaying software decisions while freight volumes are weak.

Priya: How does the renewal book look for FY26?

Tom Becker: Mid-market renewals look normal, about 93% logo retention. I want to be upfront on two enterprise accounts. NorthStar Grocers issued an RFP last month, and we hear they are evaluating bringing transportation planning in-house on their ERP. Pallas Retail closed its merger with Corvid Stores in August, and their new procurement team told us they are consolidating onto Corvid's incumbent TMS. They have signalled they do not intend to renew when the contract ends in Q1.

David: What is the combined exposure?

Tom Becker: Together about 9% of ARR, roughly $4.1M. NorthStar is about $2.3M and Pallas about $1.8M.

Priya: What odds do you put on keeping them?

Tom Becker: Pallas I would treat as gone. NorthStar maybe fifty-fifty; we are offering a multi-year discount to stay.

Linda Marsh: I want to add context. Our mid-market pipeline is the strongest it has been, up 30% on last year. But I would not pretend the enterprise segment is not under pressure right now.

David: Has this changed your FY26 outlook?

Tom Becker: Our internal FY26 plan now has recurring revenue growth in the high single digits, not the mid-teens in the CIM. We would rather tell you now than have you find it in confirmatory diligence.

David: Appreciated. We will reflect it in our model.

Action items: Priya to rerun the model with the enterprise churn scenario. David to brief Elena before IC.""",
    },
    {
        "doc_id": "falcon_expert_call_2026_10_07",
        "title": "Project Falcon - expert call on freight software market",
        "deal_id": "falcon", "kind": "meeting", "doc_kind": "transcript",
        "date": "2026-10-07", "author_email": "priya@northbeam.com", "acl": deal_acl("falcon"),
        "participants": ["Priya Shah", "Former VP Sales, a competing TMS vendor (via Insight Bridge expert network)"],
        "body": """Expert network call - freight software market. Oct 7 2026. Expert: former VP of Sales at a competing TMS vendor, sourced through Insight Bridge. Notes by Priya Shah.

Market. Freight volumes have been down for six straight quarters. Shippers are cutting software spend, and enterprise shippers in particular are consolidating vendors after a wave of retail mergers.

Pricing. Renewal pricing pressure is real: the expert sees 5-8% discounts being demanded at enterprise renewals, against list price increases of 5-7% in the mid-market.

Enterprise versus mid-market. Enterprise customers increasingly use ERP-native transportation modules or the TMS of whichever company acquired them. Mid-market shippers rarely switch because implementation is painful.

Acme specifically. The expert views Acme as the best product in the mid-market but "over-indexed on a handful of enterprise logos." He would expect Acme's enterprise segment to shrink in FY26.

Multiples. Public and private logistics software valuations have compressed. Deals that printed at 12-13x EBITDA in 2023 are now trading at 8-10x.

Takeaway for the model: the CFO's high-single-digit growth guide looks credible; mid-teens growth does not.""",
    },
    {
        "doc_id": "falcon_email_2026_10_08",
        "title": "Email: Falcon model update - growth assumption",
        "deal_id": "falcon", "kind": "email", "doc_kind": "email",
        "date": "2026-10-08", "author_email": "priya@northbeam.com", "acl": deal_acl("falcon"),
        "from": "priya@northbeam.com", "to": ["david@northbeam.com"], "cc": ["elena@northbeam.com"],
        "body": f"""From: Priya Shah
To: David Okafor
Cc: Elena Ruiz
Date: Oct 8 2026, 7:10 PM
Subject: Falcon model update - growth assumption

David,

I have saved a new version of the Falcon model. The main change is annual revenue growth, cut from {pct(V2M['revenue_growth'])} to {pct(V3M['revenue_growth'])}.

Reasoning:
1. On Monday's call Tom Becker told us NorthStar Grocers and Pallas Retail are both likely to leave. Together that is about 9% of ARR ($4.1M). I have assumed Pallas churns in Q1 and NorthStar is a coin flip.
2. Q3 bookings were $2.1M against a $3.0M plan.
3. The expert call yesterday supports high-single-digit growth for FY26 and flags 5-8% renewal discounts in enterprise.
4. Tom himself said their internal plan is now high single digits.

Effect: FY26 revenue goes from {money(V2M['fy26_revenue'])} to {money(V3M['fy26_revenue'])}, FY26 EBITDA from {money(V2M['fy26_ebitda'])} to {money(V3M['fy26_ebitda'])}, and gross IRR from {pct(V2M['irr'])} to {pct(V3M['irr'])}. That is below our 20% hurdle at the current price.

Margin and exit multiple are unchanged for now. Happy to walk through it tomorrow.

Priya""",
    },
    {
        "doc_id": "falcon_slack_2026_10_09",
        "title": "#deal-falcon: exit multiple house view",
        "deal_id": "falcon", "kind": "message", "doc_kind": "chat",
        "date": "2026-10-09", "author_email": "elena@northbeam.com", "acl": deal_acl("falcon"),
        "body": """#deal-falcon, Oct 9 2026

Elena Ruiz (9:12 AM): Read Priya's email, the growth cut is right. Separate point before IC: we are not underwriting any logistics software exit above 10x. We learned this on Kestrel, where the comps went from 12x to 8x in eighteen months while we were still in diligence. Please take Falcon's exit multiple down to 9.5x before the memo goes to IC.

David Okafor (9:20 AM): Understood. I'll make the change and save a new version today or Monday. Returns will drop further; we should discuss whether we can still get there on price.

Elena Ruiz (9:22 AM): Yes. If it does not clear the hurdle at 9.5x, we bid lower or walk.

Priya Shah (9:25 AM): Noted. I'll hold off on sending the IC materials until the new version is in.""",
    },
    # ----------------------------------------------------------------- Kestrel
    {
        "doc_id": "kestrel_ic_pass_memo",
        "title": "Project Kestrel - IC decision: pass (Swiftroute)",
        "deal_id": "kestrel", "kind": "knowledge_base", "doc_kind": "ic_memo",
        "date": "2025-03-12", "author_email": "david@northbeam.com", "acl": deal_acl("kestrel"),
        "body": """Project Kestrel - Investment Committee decision record. Mar 12 2025. Prepared by David Okafor.

Target: Swiftroute, route optimisation SaaS for regional carriers. FY24 revenue $62M, 24% growth, 20% EBITDA margin.

Decision: PASS.

Reasons.
1. Customer concentration. The top two customers were 22% of revenue. During confirmatory diligence the second-largest customer (11% of revenue) gave notice after being acquired; its acquirer already used a competing platform.
2. Growth quality. Once that churn was reflected, forward growth fell from 24% to about 9%, and our base case IRR fell from 27% to 14%.
3. Exit multiple. We had underwritten a 12x exit. Over the eighteen months of our coverage, comparable logistics software transactions compressed from about 12x to 8x EBITDA. At 9x the deal did not work at any price the seller would accept.

Lessons recorded for future logistics software deals.
- Haircut growth for any customer above 5% of revenue that is renewing during the hold, especially after an M&A event at the customer.
- Do not underwrite logistics software exits above 10x in the current cycle (Managing Partner house view, Elena Ruiz).
- Run a customer-level churn scenario before the IC memo, not after.""",
    },
    {
        "doc_id": "precedent_logistics_saas_pass_2025",
        "title": "Precedent library: logistics SaaS pass (2025), anonymised",
        "deal_id": None, "kind": "knowledge_base", "doc_kind": "precedent",
        "date": "2025-04-01", "author_email": "elena@northbeam.com", "acl": FIRM_ACL,
        "body": """Precedent library entry - anonymised for firm-wide use. Logistics software buyout, passed at IC in Q1 2025.

What happened. A large customer was acquired during diligence and gave notice. Forward growth roughly halved, and comparable exit multiples compressed by about a third during the process. Base case returns fell below the hurdle.

Firm lessons.
1. Treat customers above 5% of revenue with a renewal during the hold as at risk, particularly after an acquisition at the customer.
2. House view (Managing Partner): no logistics software exit multiple above 10x in the current cycle.
3. Run customer-level churn scenarios before the IC memo is drafted.

Details of the company and deal team are restricted under the information barrier policy. Contact Compliance (Sam Patel) for access requests.""",
    },
    # ------------------------------------------------------------------ Harbor
    {
        "doc_id": "harbor_cim_summary",
        "title": "Project Harbor - CIM summary (Brightline Health)",
        "deal_id": "harbor", "kind": "knowledge_base", "doc_kind": "cim",
        "date": "2026-09-22", "author_email": "marcus@northbeam.com", "acl": deal_acl("harbor"),
        "body": """Project Harbor - CIM summary. Target: Brightline Health. Prepared by Marcus Lee, Sep 22 2026.

Business. Patient scheduling and intake software for outpatient clinics: online booking, waitlist automation, insurance eligibility checks. About 1,900 clinic customers across physical therapy, dermatology and imaging.

Financials. FY25 revenue $31.0M, up 28%. EBITDA margin 22%. Net revenue retention 118%. No customer above 2% of revenue.

Process. LOI signed Sep 30 at $210M enterprise value with 60 days of exclusivity.

Key diligence areas: Epic and Athena integration roadmap, HIPAA compliance and security posture, sales efficiency in new specialties.""",
    },
    {
        "doc_id": "harbor_mgmt_call_2026_10_05",
        "title": "Project Harbor - management call with CEO Raj Mehta",
        "deal_id": "harbor", "kind": "meeting", "doc_kind": "transcript",
        "date": "2026-10-05", "author_email": "marcus@northbeam.com", "acl": deal_acl("harbor"),
        "participants": ["Elena Ruiz", "Marcus Lee", "Raj Mehta (Brightline CEO)"],
        "body": """Management call - Brightline Health. Oct 5 2026. Notes by Marcus Lee.

Raj Mehta (CEO) on growth: dermatology launched in Q2 and already has 140 clinics. Imaging is the next vertical.

Integrations: the Epic App Orchard integration is certified; Athena is in beta and due in Q1.

Churn: logo churn is about 6% a year, mostly very small clinics closing. No enterprise concentration.

Security: SOC 2 Type II clean; a HIPAA risk assessment is scheduled for November.

Elena asked about pricing: per-provider pricing with an 8% list increase this year, accepted by most customers.

Action: Marcus to build the base case at 25% growth and test the exit multiple range.""",
    },
    {
        "doc_id": "harbor_model_brief",
        "title": "Project Harbor - operating model brief",
        "deal_id": "harbor", "kind": "knowledge_base", "doc_kind": "model_brief",
        "date": "2026-10-07", "author_email": "marcus@northbeam.com", "acl": deal_acl("harbor"),
        "body": """Project Harbor - model brief. Marcus Lee, Oct 7 2026.

Base case: 25% revenue growth tapering to 18% by FY30, margin expanding from 22% to 27%, exit at 14x FY30 EBITDA. Gross IRR 26%, MOIC 3.2x.

Downside: 18% growth, 12x exit, IRR 19%.

Healthcare software multiples have held up better than logistics; no cap applied beyond the valuation policy's general rules.""",
    },
    # --------------------------------------------------------------- Firm-wide
    {
        "doc_id": "policy_valuation",
        "title": "Northbeam valuation and underwriting policy",
        "deal_id": None, "kind": "knowledge_base", "doc_kind": "policy",
        "date": "2026-01-15", "author_email": "elena@northbeam.com", "acl": FIRM_ACL,
        "body": """Northbeam Capital - Valuation and underwriting policy. Effective Jan 15 2026. Owner: Elena Ruiz, Managing Partner.

1. Hurdle. Base case gross IRR of at least 20% and MOIC of at least 2.5x over a five-year hold.
2. Growth. Underwrite growth from quality of earnings adjusted figures, never from the management case alone. Customers above 5% of revenue with renewals during the hold require an explicit churn scenario.
3. Exit multiples. The base case exit multiple may not exceed the entry multiple without IC approval. The Managing Partner may set sector caps (house views), which bind all deal teams.
4. Model versioning. Every deal model is saved as a numbered version. Any document presented to IC must quote the latest model version. If the model changes after the IC memo is drafted, the memo and deck must be refreshed before IC.
5. Sign-off. The deal lead certifies that IC materials tie to the latest model.""",
    },
    {
        "doc_id": "policy_information_barriers",
        "title": "Northbeam information barrier (ethical wall) policy",
        "deal_id": None, "kind": "knowledge_base", "doc_kind": "policy",
        "date": "2026-02-01", "author_email": "sam@northbeam.com", "acl": FIRM_ACL,
        "body": """Northbeam Capital - Information barrier policy. Effective Feb 1 2026. Owner: Sam Patel, Chief Compliance Officer (sam@northbeam.com).

Purpose. Deal teams receive confidential and sometimes material non-public information (MNPI) from targets under NDA. Information about a deal must stay inside its deal team.

Rules.
1. Access to deal materials (models, memos, transcripts, data rooms) is limited to members of that deal team.
2. Staff must not seek, request or discuss information about deals they are not staffed on.
3. Systems, including the firm's AI assistant, enforce these walls: they do not retrieve or summarise restricted material for people outside the deal team, and they do not confirm whether restricted material exists.
4. Access requests and wall-crossing go through Compliance. Contact Sam Patel.
5. Anonymised lessons may be shared firm-wide through the precedent library once Compliance has approved them.""",
    },
    {
        "doc_id": "ic_process",
        "title": "Northbeam investment committee process",
        "deal_id": None, "kind": "knowledge_base", "doc_kind": "process",
        "date": "2026-01-15", "author_email": "elena@northbeam.com", "acl": FIRM_ACL,
        "body": """Northbeam Capital - Investment committee (IC) process.

Stages: screening, first-round bid, diligence, IC approval, final bid, signing.

IC materials. The IC memo and IC deck are due to committee members three business days before the meeting. All figures must tie to the latest saved version of the operating model (see the valuation policy, section 4).

Required sections: thesis, base case returns, sensitivities, risks, customer concentration analysis, and a statement on any deviation from house views.

Roles. The deal lead (principal or above) owns the memo; the associate owns the model and ties out the numbers; the Managing Partner chairs IC.""",
    },
    {
        "doc_id": "expertise_directory",
        "title": "Northbeam team expertise directory",
        "deal_id": None, "kind": "knowledge_base", "doc_kind": "directory",
        "date": "2026-09-01", "author_email": "elena@northbeam.com", "acl": FIRM_ACL,
        "body": """Northbeam Capital - who to ask.

Elena Ruiz, Managing Partner: logistics, industrials, transportation. Sets sector house views. Chairs IC.
David Okafor, Principal: logistics software and supply chain technology; led two logistics software processes. Go-to for freight market questions.
Priya Shah, Associate: SaaS metrics, quality of earnings, operating models. Owns model version hygiene for her deals.
Marcus Lee, Associate: healthcare IT; previously in strategy at a hospital system. Go-to for HIPAA and EHR integration questions.
Sam Patel, Chief Compliance Officer: information barriers, access requests, MNPI questions.

External advisers we use: Ardent Advisory (quality of earnings), Insight Bridge (expert network), Halvorsen & Co (sell-side contact for logistics deals).""",
    },
]

# Numbers that downstream documents quote from a model version. In production these
# would be extracted at ingest; here they are recorded alongside the documents.
DOC_REFERENCES = [
    (doc_id, title, "falcon", metric, V2M[metric], 2, owner)
    for doc_id, title, owner in [
        ("falcon_ic_memo_draft", "Project Falcon - IC memo draft", "david@northbeam.com"),
        ("falcon_ic_deck", "Project Falcon - IC deck outline", "priya@northbeam.com"),
    ]
    for metric in ("revenue_growth", "fy26_revenue", "fy26_ebitda", "exit_multiple", "irr", "moic")
]
