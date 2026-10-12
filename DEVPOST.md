# Provenance

**The database knows what changed. The brain knows why.**

A company brain for finance teams that tracks where every number came from, why it changed, and where it is used -- with ethical walls enforced inside the retrieval layer.

## Inspiration

In private equity and banking, the most expensive questions are about numbers: "Why did EBITDA drop since the IC memo?", "Which slides still show last week's IRR?", "What multiple did we use on the last logistics deal?". Version-control tools for Excel can tell you that cell C7 changed from 12% to 8%. None of them can tell you that it changed because the CFO said on Tuesday's call that two enterprise customers are not renewing. That reason lives in transcripts, emails and chat notes -- exactly where a database cannot reach.

At the same time, a finance brain cannot be a single shared index. An associate on one deal must not see material non-public information from another. Information barriers have to be enforced at retrieval, not filtered after the fact.

## What it does

Provenance is the brain of a fictional PE fund, Northbeam Capital.

- **Explains changes with sources.** Ask "Why did FY26 EBITDA drop?" and the agent pulls the cell-level diff between model versions from the database, then finds the reason in the brain (a management call transcript, an associate's email, a partner's note) and cites it.
- **Proactive alerts.** When a new model version is saved, the agent automatically assesses the impact: what changed, why, which IC memos and decks now quote stale numbers and who owns them, and whether it conflicts with firm policy.
- **Pre-meeting briefs and precedent lookup** across deals, people, transcripts and past investment decisions.
- **Ethical walls.** The same question from two users gets two different answers. Content behind a wall is never retrieved for someone outside it, and the agent neither confirms nor denies that walled deals exist -- it routes the user to Compliance.

## How we built it

- **SQLite** for hard facts: users, deals, deal-team membership, model versions, cell-level diffs, key metrics, and which documents quote which numbers.
- **HydraDB** for the unstructured brain: memos, transcripts, emails, notes, policies, precedents and change logs, with its context graph linking entities (companies, people, customers, metrics) across documents. Every document carries an ACL (`group:northbeam:<deal>` or firm-wide), and every query sends the caller's principals, which are derived server-side from deal-team membership.
- **openpyxl** cell-level diffing of real `.xlsx` model versions.
- **OpenAI GPT-5.5** as the agent, with database tools (model history, diffs, stale references) and a brain search tool. It decides when it needs facts versus reasons and joins them.
- **FastAPI** backend and a single-page UI with a user switcher to demonstrate the walls.

## Challenges

- Deciding what belongs in a relational database versus the brain. Our rule: if you can write the query before anyone asks the question, it goes in SQL; if answering requires reading text, inferring links or combining sources, it goes in the brain.
- Enforcing walls in retrieval rather than in the prompt, so a jailbreak cannot leak content that was never retrieved.

## What's next

Provenance is built to plug into Tessel, a desktop spreadsheet app for finance professionals, so every save becomes a version, every number gets a history, and the brain sits one click away from the cell.

## Built with

HydraDB, OpenAI, Python, FastAPI, SQLite, openpyxl
