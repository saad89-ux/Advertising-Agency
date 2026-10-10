# Mini PRD: The Launch Gate (v2)

**Company:** Fernlight Media, LLC (performance marketing agency, Denver)
**Stakeholder:** Elena Marsh, Head of Growth Operations | **Economic buyer:** Gareth Pell, founder
**Scope window:** 4 days, about 30 to 36 hours
**Companion file:** `Launch_Gate_Dataflow_Architecture.md` (v2). If the two disagree: **this PRD wins on scope, the diagram file wins on structure.**

**Source tags:** **[B]** Fernlight brief | **[N]** your handwritten notes | **[D#]** a decision in section 9. Every statement below has a tag or is plain arithmetic from tagged numbers. Nothing is added without one.

---

## 1. What are we building?

A **pre-launch gate** that every batch of ads passes through before it goes live. A buyer uploads a messy launch sheet. The gate turns it into clean records, checks every ad, gives each ad a verdict, and shows a reviewer the ad beside its findings. **Only ads a person approves are sent**, and only to a stand-in (dry-run) ad platform. [B R1 to R6]

**One-line pitch:** *Stop finding out about mistakes from the client.* [B section 4]

### The problem (Fernlight's numbers, all estimates) [B sections 4, 5]

| Fact | Value | Confidence |
|---|---|---|
| Batches per month | about 46 | Medium (re-sent batches counted twice) |
| Ads per month | about 1,750 (about 38 per batch) | Medium |
| Manual checking time | about 40 min per launch, about 30 h/month | Low (9 timed launches, people behaved differently when watched) |
| Post-launch incidents last quarter | 23 | Low to medium (only written-down ones) |
| Incident mix | 9 tracking-code errors, 6 wrong page or expired offer, 5 platform rejections, 3 wording the client would not approve | Low to medium |
| Waste and credits last quarter | about $19,000 | Low (Elena would not trust it within a third, so roughly $13k to $25k) |

**Core pain:** the 41-line checklist is partly skipped (claims and the page behind the ad are checked "when I remember"), and nothing connects the spreadsheets, project board, ad accounts and shared drive. [B section 4]

---

## 2. For whom? [B sections 3, 4, 5.3]

| Role | Who | What they need | Tension |
|---|---|---|---|
| **Economic buyer** | Gareth Pell | Fewer credits and refunds; no added day on launches | Will not accept anything that slows launch |
| **Sponsor** | Elena Marsh | Count of mistakes caught before they cost money; new hires launching as carefully as the best buyer | Wants numbers, not opinions |
| **Daily user** | Buyers (e.g. Rin) | Fast check, no extra steps | "Do not add steps" |
| **Daily user** | Account managers (e.g. Tobias) | Approval evidence attached; approved version equals launched version | Wants to show the client the check |
| **Indirect** | Designers (e.g. Mei) | Not responsible for wording decisions | Will not police wording |
| **Indirect** | Clients and their lawyers | Their own rules applied, never another client's | Need confidence nothing goes out unapproved |

**Position:** Gareth pays; buyers and account managers use it daily. They collide on speed versus thoroughness, so **v1 runs in the background of the existing flow and returns one screen** (median under 60 s per 100-row sheet), not a chat the buyer must drive. [B R5, 5.3]

---

## 3. Goals and success measures [B R1 to R9]

| Goal | Target |
|---|---|
| Ingest | 95% of sample rows read correctly; 100 rows ready in under 90 s without blocking others; second upload adds nothing; one bad row never loses the others |
| Mechanical checks | at least 98% of planted errors found; false alarms at most 3%; 100 rows checked in under 5 s |
| Page checks | at least 90% found; false alarms at most 5%; at most 10 s per page; zero form submissions or purchases |
| Wording checks | at least 90% found; correct rule cited in at least 90%; **zero invented rule numbers**; zero cross-client rules |
| Verdicts | at most 5% of "ready" rows contain a planted error; "needs a person" at most 20% of rows, each naming what is missing |
| Speed | median under 60 s per 100-row sheet; reviewer under 10 min per sheet |
| Safety | zero ads sent without approval; zero duplicates under 500 repeat clicks and retries; zero actions outside policy on 12 adversarial rows; zero cross-client leaks |
| Cost | average model cost under $0.25 per 100 ads; hard step and spend stop per row; assessment key capped at $15 |
| Proof | metrics reproducible from replay of 30 labeled sheets; merge blocked if detection falls more than 3 points or false alarms exceed 5%; any verdict reconstructed in under 2 min; weekly accuracy and cost trend |

---

## 4. In scope (v1)

### 4.1 Ingest [B R1, D5]
Accepts **CSV and XLSX** [D5]. Normalises column names and platform spellings, handles blank cells, makes one clean record per ad, hashes each row and the sheet. A repeated sheet adds nothing; a malformed row is isolated; concurrent uploads go through a Redis job queue (one job per ad).

### 4.2 Mechanical checks [B R2]
UTM tracking codes present and spelled in the agreed naming; text and headline limits per platform; creative size and ratio. Wrong-but-recognisable values ("Paid Social", "fb") are **suggested**, never silently changed. Each finding names row, field and proposed fix. Platform limits entries older than a configured date are flagged for a person to confirm [D15]. Where a client's own naming preference differs from the 2023 naming document, the client's preference applies and is recorded [D14].

### 4.3 Page check [B R3]
Headless browser (Playwright) opens the page as a shopper would: page loads, UTM codes survive redirects, offer, price and dates match the ad. Screenshot kept as evidence. **Read-only: never submits forms or buys.**

### 4.4 Wording check [B R4, N]
Retrieval over **that client's** brand book and claims register only. Every finding cites a real entry or section. Unknown situations go to a person.

### 4.5 Verdict [B R5]
Per ad: `ready`, `fix needed`, `blocked`, `needs a person`, plus a one-screen batch summary. Hard cap on steps and spend per row, enforced in code.

### 4.6 Review, approval, send [B R6, D3, D13, D17]
- Reviewer sees each ad beside its findings and can approve, edit or reject. Edits and reasons are kept. An edited ad is re-checked in a bounded loop [D17].
- Approval records who, when and a **content hash**; only the approved version can be sent [D13].
- Client approval given by email, text or call is recorded by the reviewer attaching a note or screenshot linked to the content hash [D3]. v1 does not read email, text or calls.
- Sender uses an idempotency key and states `approved`, `sending`, `sent`; bounded retries with timeouts; dead-letter list [B R6].
- Sends only to the stand-in platform. Real platform calls are designed and documented, **switched off** [B R6].
- A rejection or uncertain state alerts a person. There is no automatic relaunch.

### 4.7 Metrics and release gate [B R7]
Per batch and over time: problems by type, ads blocked, reviewer time, false alarms (reviewer dismissed), cost per batch, simulated rejection rate with and without the gate. Replay of 30 labeled sheets; CI blocks a change that worsens results [D9].

### 4.8 Security [B R8, N]
Text from ads, sheets and pages is data with no authority. Each tool has least privilege. The approval gate sits outside the model. Each client's rules and data are separate (`client_id` on every row). A buyer cannot approve an ad they were not given.

### 4.9 Explainability [B R9, D8]
Each ad is a trace with a span per step, tagged with ad and batch IDs (Langfuse, open source). A reviewer can open what was checked, which rule applied, what the page showed and what it cost.

---

## 5. Context engineering and resume [N]

- **Thin shared state.** Large outputs (page text, screenshots, rule chunks, full findings) go to a **file store** [D7]. State keeps ids, status, a short summary and file references.
- **Context builder.** For each step it selects only the tools, the client's top-k rule chunks and the summary that step needs, and reads a file on demand. Aim: fewer tokens, lower cost, sharper reasoning.
- **Checkpointer.** LangGraph saves state to PostgreSQL after every step with `thread_id = batch_id:ad_id` [D18]. After a crash, restart or deploy, an ad resumes from its last saved step.
- **Exactly-once sending is separate.** The checkpointer resumes checks; only the sender's idempotency key prevents duplicate sends.

---

## 6. Never-break rules

1. No ad is sent without a person's approval.
2. Each ad is sent exactly once.
3. One client's rules are never applied to another client.
4. The page check never submits a form or buys anything.
5. A cited rule number must come from a retrieved entry; none are invented.
6. Step and spend caps are never exceeded.
7. Real platform calls stay switched off.
8. Secrets are never committed to the repository.

[B R2 to R8; rule 8 is standard practice and part of the one-command, CI packaging in B section 8]

---

## 7. Stack [B section 8, N, D]

| Layer | Choice | Source |
|---|---|---|
| Language | Python 3.12+ | [B] |
| Backend | FastAPI | [B] |
| Frontend | Next.js | [N] |
| Database | PostgreSQL | [B] |
| Vector retrieval | pgvector inside PostgreSQL | [D6] |
| Queue and cache | Redis | [B], [N] |
| Orchestration | LangGraph, one fixed bounded flow | [B] |
| Page check | Playwright in a sandbox container | [B R3] |
| Model | one hosted provider, one key via environment variable, spend cap in code; provider not named | [B], [D11] |
| File store | Docker volume (local disk) | [D7] |
| Live status | Server-Sent Events | [D12] |
| Tracing | Langfuse | [D8] |
| CI | GitHub Actions: checks plus replay | [D9] |
| Start | single `docker compose up` | [B] |
| Cloud deploy | any Docker-capable host; host not named | [D10] |
| Ad platform | stand-in (dry run) from the data pack | [B] |

---

## 8. NOT in scope (v1)

| Not included | Why |
|---|---|
| Publishing to real ad platforms, including MCP servers | No real accounts; brief says stand-in only and "MCP not needed". Designed and documented, switched off. [B] |
| Auto-correct and auto-relaunch after a rejection | Violates "nothing goes live without a person". [B R6] |
| Continuous post-launch monitoring, root-cause node, monthly mistake report (your Nodes 9 to 12) | Needs real platforms; cut for a 4-day window. [B section 1, N] |
| Client-facing approval link, client portal, client chasing by email, text or call | Out of scope [D4]. v1 uses content hash plus manual evidence [D3]. |
| Fine-tuning or self-hosting models | "Not needed" [B R2] |
| Multi-agent design, free-form supervisor with dynamic tools, LLM gateway | "Not needed" [B R5] |
| Multimodal image RAG | Picture shape is a deterministic size and ratio check. [B R2] |
| Chat as the main interface | Adds steps; buyers upload and get a summary. [B 5.3] |
| Learned reviewer habits, agentic memory | "Not needed" [B R9]. (Checkpointer state is workflow resume, not learned memory.) |
| Lawyer-grade legal judgement | Gate cites the client's own register; unknown goes to a person. [B R4] |
| Report generation, sales attribution, channel-name clean-up in reporting | Different product. [B section 4] |
| Crawling at scale, anti-bot evasion, proxies | Skip list [B R3] |
| Enterprise SSO, compliance paperwork, multi-region | Skip lists [B R6, R8] |

---

## 9. Decision log (replaces "open questions"; nothing is left open)

These were not given by Fernlight. They are decisions for v1. If Fernlight says otherwise, change that one line; no other part is blocked.

| ID | Decision | Why |
|---|---|---|
| D1 | Naming document owner and approver of changes: **Elena Marsh** | She leads Growth Operations and wrote the brief. [B] |
| D2 | Pilot clients: the **three clients whose brand books and registers are in the data pack**. Reviewer: the client's assigned buyer; backup: that client's account manager | Only these have rules we can check. [B section 8] |
| D3 | Client approval by email, text or call: reviewer attaches a **note or screenshot** linked to the ad's content hash. v1 does not read those channels | Matches Tobias's screenshot habit. [B section 4] |
| D4 | Client approval link and portal: **out of scope** | Keeps v1 inside four days; diagram and PRD agree. |
| D5 | Upload formats: **CSV and XLSX** | Sheets are spreadsheets [B 5.2]; your notes mention an Excel sheet [N]. |
| D6 | Vector retrieval: **pgvector in PostgreSQL** | One database; small scale (rules per client). Retriever can be swapped later. |
| D7 | File store: **Docker volume** | Simplest with `docker compose up`. |
| D8 | Tracing: **Langfuse** | Brief asks for an open-source tracing tool. [B R9] |
| D9 | CI: **GitHub Actions** | Brief asks for a CI file that runs checks and replay. [B section 8] |
| D10 | Cloud deploy: any **Docker-capable host** | Brief says "a cloud host" without naming one. [B R7] |
| D11 | Model: **one hosted provider**, key from an environment variable, spend cap in code | [B R5, section 8] |
| D12 | Live status: **Server-Sent Events** (one-way updates are enough) | Simpler than WebSocket; no client approvals in v1. |
| D13 | Approval is bound to a **content hash**; only that version can be sent | Fixes "approved one, launched the other". [B section 4] |
| D14 | If a client's own preference and the 2023 naming document differ, **the client's preference wins and is recorded** | Brief says clients have their own preferences. [B 5.4] |
| D15 | Platform limits entries older than a **configured date** are flagged for a person to confirm | Limits file is partly stale. [B 5.4] |
| D16 | Cost framing (below) | Arithmetic from brief numbers only. |
| D17 | After an edit, the ad is re-checked in a **bounded loop** (cap in configuration) | Brief: re-examine, "never endlessly". [B R5] |
| D18 | `thread_id = batch_id:ad_id` | One ad failing does not block the others. |

### D16: cost arithmetic
- Target: under $0.25 per 100 ads [B R5]. A 38-ad batch: 0.25 × 0.38 ≈ **$0.095**.
- Today's checking: about 40 minutes per launch [B], low confidence. An hourly rate is not given in the brief, so none is assumed.
- Break-even: $0.095 ÷ (40 ÷ 60 h) ≈ **$0.14 per hour**. The gate's model cost is below the value of the checking time it replaces unless that time is worth under about 14 cents an hour, even if the 40 minutes were cut to a few.
- Where it breaks: if "needs a person" rises toward the 20% ceiling and reviewers re-check every ad anyway, no time is saved. That is measured in the pilot.

---

## 10. Working rules for v1 (stated assumptions)

- The sample sheet in the brief is representative of real messy sheets. [B 5.2]
- Three client brand books and registers stand in for all clients. [B section 8]
- The stand-in platform's failures are representative of real ones. [B section 8]
- Everything in sections 1, 5.1 and 5.4 of the brief is an estimate; none is treated as fact.

---

## 11. Risks and early signals [B sections 4, 5]

| Risk (ranked by damage) | Early signal | Mitigation |
|---|---|---|
| A bad ad is marked `ready` | False-ready rate above 5% in shadow mode | More "needs a person"; conservative thresholds |
| Wrong or invented rule cited | Citation check fails in eval | Cite only retrieved entries; code verifies the entry exists |
| Hostile text (row 33) steers the gate | Any adversarial row causes an out-of-policy action | Text is data; approval outside the model; least privilege |
| Buyers ignore a slow or noisy gate | Dismissed findings above 5%; sheet time above 10 min | Tune false alarms; keep median under 60 s |
| Stale limits or changed storefront pages | Weekly accuracy trend drops | Dated limits file [D15]; replay on every change; CI gate |

---

## 12. Milestones [B suggested checkpoints]

| Day | Outcome |
|---|---|
| 1 | Messy sheet in, clean records out; mechanical checks measured on labeled sheets; analysis written and committed |
| 2 | Page check; wording check with citations; first full batch verdict |
| 3 | Reviewer approve, edit, reject; exactly-once send to stand-in; adversarial rows handled; costs and limits enforced |
| 4 | Metrics screen and replay; CI release gate; one-command start and cloud deploy; README, failure cases, decision log |

**Honest cuts if time runs out, in order:** weekly trend view, cloud deploy polish, page-check edge cases.
