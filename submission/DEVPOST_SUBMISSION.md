# Project overview

## Project name (60 max)
Provenance: the company brain for finance teams

## Elevator pitch (200 max)
Git for Excel plus a company brain for PE and banking. Every number gets a history: what changed, why it changed and where it's used, with ethical walls enforced in retrieval.

---

# Project details

## About the project

## Inspiration
In private equity and banking, the most expensive questions are about numbers. *Why did EBITDA drop since the IC memo? Which slides still show last week's IRR? Is this 19% IRR real or a typo?* Version-control tools for Excel can tell you that a cell changed from 12% to 8%. None of them can tell you **why**. The reason lives in a CFO call transcript, an associate's email or a partner's chat message, which is exactly where a database can't reach.

A finance brain also can't be a single shared index. An associate on one deal must never see confidential material from another. Those information barriers have to be enforced when content is retrieved, not filtered out of the answer afterwards.

**The database knows what changed. The brain knows why.**

## What it does
Point Provenance at a firm's existing shared drive: deal folders full of Excel models named "v3", "v3 (PS edits)" and "FINAL (2)", plus memos, decks, call transcripts and emails. It rebuilds all of that into a brain.

- **Git for Excel, rebuilt from a messy folder.** Every saved state of a model becomes a commit with a parent, an author and a one-line message written by the LLM, e.g. *"Cut revenue growth 12% to 8%; IRR 21.6% to 15.7%"*. Each file is a branch, and a copied file ("downside case") forks from the commit it most resembles. You can compare any two versions cell by cell.
- **Live.** Save a model in Excel and a new version appears within seconds. The agent then sends a proactive alert covering what changed, why, which documents now quote old numbers, and any conflict with firm policy.
- **Debugging.** When IRR suddenly jumps, the agent finds the exact input that changed (net debt at exit typed as $4M instead of $40M) and who saved it. It checks the value against the lender's term sheet, calls out the likely typo, and says which version to restore.
- **Answers with sources.** Every claim links to the file it came from and to the exact model version. Links are verified on the server, so the agent can't invent a citation.
- **Documents pinned to versions.** IC memos and decks are linked to the version whose numbers they quote, so the brain knows when they go stale.
- **Governance.** Each deal folder is an ethical wall. The same question from two people gets two different answers, and the agent never confirms or denies that a walled deal exists. Compliance manages who can see what from an access grid, approves access requests, and sees an audit trail of every question and blocked attempt. Access changes apply to the person's next question, with nothing re-indexed.

## How we built it
- **SQLite for the facts:** users, deals and deal teams, models, commits, branches, cell-level changes, key metrics, which documents quote which numbers, and the audit trail.
- **HydraDB for the brain:** memos, transcripts, emails, policies, precedents and version change logs. Every document carries an access tag for its deal team, and every query carries the asking user's identity, worked out on the server from deal staffing. Because staffing lives in SQLite and documents only carry a deal tag, access changes take effect immediately.
- **Version history:** openpyxl reads values, formulas and row and column labels. Each commit is identified by a hash of its contents, and forks are detected by similarity between workbooks. The LLM maps the key metric cells once per model and writes the commit messages.
- **Ingestion and watcher:** the deal is taken from the folder name. PDF, DOCX, EML, TXT and MD files are parsed into text, and the folder is polled every 3 seconds for new saves.
- **OpenAI GPT-5.5 as the agent,** with database tools (model history, version diffs, stale references) and a brain search tool. It decides when it needs facts and when it needs reasons, then combines them.
- **FastAPI backend** with a single-page UI styled with the app-starter design system.

## Challenges we ran into
- Deciding what belongs in a relational database and what belongs in the brain. Our rule: if you can write the query before anyone asks the question, it goes in SQL. If answering means reading text, inferring links or combining sources, it goes in the brain.
- Rebuilding a version tree from real-world chaos: files duplicated, renamed, forked, and saved over again.
- Enforcing walls in retrieval rather than in the prompt, so no prompt trick can leak content that was never retrieved. HydraDB deletes run in the background, so SQLite acts as the registry of which documents exist.

## Accomplishments that we're proud of
- An agent that catches a fat-fingered model input on its own and backs it up with the lender's own term sheet.
- Ethical walls that are invisible to the people they protect, and manageable by Compliance without re-indexing anything.
- 49 automated tests covering the walls, versioning, ingestion, citations and governance.

## What we learned
Finance teams don't need another search box. They need the reason behind every number, from a source they trust, without seeing anything they shouldn't.

## What's next for Provenance
An Excel add-in for "why did this cell change?" right inside the workbook, SharePoint and Google Drive connectors that carry their own permissions, and approval workflows for IC materials.

---

## Built with
python, fastapi, sqlite, hydradb, openai, gpt-5.5, openpyxl, pypdf, python-docx, javascript, html, css, app-starter

## "Try it out" links
https://github.com/MYRADhub/provenance-company-brain

## Video demo link
(YouTube URL once uploaded)
