# Launch Gate: Dataflow and Architecture (v2)

Companion to `Launch_Gate_Mini_PRD.md` (v2). **If the two ever disagree: the PRD wins on scope, this file wins on structure.**

**Source tags used in both files**
- **[B]** = Fernlight brief
- **[N]** = your handwritten notes
- **[D#]** = a decision recorded in the PRD, section 9 (decision log)

Every box below carries one of these tags in the mapping table at the end. Nothing is in a diagram unless it has a tag.

---

## 1. Dataflow diagram

Solid arrow = main flow. Dotted arrow = supporting flow. Nothing goes live without a person's approval. [B R6]

```mermaid
flowchart TD
  U["Buyer uploads messy launch sheet\nCSV or XLSX, 40-200 rows"] --> N1
  DP["Fernlight data pack\nbrand books, claims registers,\nnaming doc, platform limits"] -.-> KB[("Per-client rules store\nfiltered by client_id")]

  N1["N1 Ingest and Normalize\ncolumn names, platform spelling,\nblank cells, row hash,\nbad row isolated, duplicate sheet adds nothing"]
  N1 --> DB[("PostgreSQL\nads, findings, approvals,\naudit, costs")]
  N1 --> Q{{"Redis job queue\none job per ad"}}

  Q --> N2["N2 Mechanical checks (code, no LLM)\nUTM codes and spelling,\ntext and headline limits,\ncreative size and ratio\nSuggest fix, never silent change"]
  Q --> N3["N3 Page check (headless browser, read-only)\npage loads, UTM survives redirect,\noffer, price, dates match ad,\nnever submits forms or buys"]
  Q --> N4["N4 Wording check\nretrieval on THIS client's rules only,\ncite exact entry or section,\nunknown case goes to a person"]
  KB --> N4

  N2 --> N5
  N3 --> N5
  N4 --> N5
  N5["N5 Verdict engine (bounded)\nready / fix needed / blocked / needs a person\nstep cap and spend cap per row"]
  N5 --> DB

  N5 --> RV["Review screen\nbatch summary + each ad beside its findings"]
  RV -->|"Reject or Edit + reason"| FIX["Edit and reason saved"]
  FIX -->|"re-check changed ad (bounded loop)"| Q
  RV -->|"Approve"| AP["Approval record\nwho, when, content hash"]
  EV["Reviewer attaches client approval evidence\nnote or screenshot"] -.-> AP
  AP --> DB

  AP --> SND["Sender worker\nidempotency key\nstate: approved - sending - sent\nretry with backoff, dead-letter list"]
  SND --> SP["Stand-in ad platform (DRY RUN)\nReal platform calls: designed, switched OFF"]
  SP --> RES["Result: accepted or rejected\n+ reason"]
  RES --> DB
  RES --> MET["Metrics and traces\nproblems by type, ads blocked,\nreviewer time, false alarms,\ncost per batch,\nrejection rate with vs without gate"]
  DB --> MET

  RES -.->|"rejection or uncertain state"| ALERT["Notify a person\n(no automatic relaunch)"]
  ALERT -.-> RV

  N2 -.->|"full output"| FS[("File store\nraw page text, screenshots,\nfull rule text, full findings")]
  N3 -.->|"full output"| FS
  N4 -.->|"full output"| FS
  FS -.->|"short summary + file ref only"| ST["Shared state (thin)\nad_id, client_id, status,\nfindings summary, file refs"]
  ST -.->|"summaries; opens file only if needed"| N5
  ST --> CK[("Checkpointer (PostgreSQL)\nthread_id = batch_id:ad_id\nsaved after every step")]
  CK -.->|"after crash or restart:\nresume from last saved step"| Q
```

---

## 2. Architecture diagram

```mermaid
flowchart LR
  subgraph UI["Frontend - Next.js"]
    UP["Upload page"]
    REV["Review screen\napprove / edit / reject"]
    MS["Metrics screen"]
    TR["Verdict trace view"]
  end

  subgraph API["Backend - FastAPI"]
    AUTH["Auth + role-based access\nbuyer sees only assigned clients"]
    ING["Ingest API\nidempotent by sheet hash"]
    APR["Approval API\nstate machine OUTSIDE the model"]
    LIVE["Live status (SSE)"]
  end

  subgraph INFRA["State"]
    RD[("Redis\njob queue + cache")]
    PG[("PostgreSQL + pgvector\nclient_id on every row")]
    FS[("File store\nDocker volume\nraw page text, screenshots,\nfull rule text")]
    CKP[("LangGraph checkpointer\nPostgreSQL, thread_id")]
  end

  subgraph WK["Worker - one fixed LangGraph flow"]
    RULES["Rules engine\nUTM, limits, ratios"]
    PW["Playwright in sandbox container\nread-only, timeouts, forms blocked"]
    RAG["Retriever\nper-client filter, entry citations"]
    GUARD["Input and output guards\ntext is data, no authority"]
    VER["Verdict + budget guard\nmax steps, max spend per row"]
    CTX["Context builder\nper step: select tools,\nresources and summary"]
  end

  subgraph EXT["External"]
    LLM["One hosted model provider\none key, spend cap in code"]
    STAND["Stand-in ad platform\ndry run"]
    REAL["Real platform calls\ndocumented, OFF"]
  end

  subgraph OPS["Quality"]
    OBS["Tracing (Langfuse)\nspan per step: ad_id, batch_id, cost"]
    EVAL["Eval replay: 30 labeled sheets\nCI release gate (GitHub Actions)"]
  end

  UP --> ING --> RD --> RULES
  RD --> PW
  RD --> RAG
  RAG --> PG
  RAG --> GUARD --> LLM
  RULES --> VER
  PW --> VER
  GUARD --> VER
  VER --> PG
  PG --> REV
  REV --> APR --> PG
  APR --> SENDER["Sender worker\nidempotency key, retry, dead-letter"]
  SENDER --> STAND
  SENDER -.-> REAL
  STAND --> PG
  AUTH --- API
  WK --> OBS
  PG --> MS
  OBS --> TR
  EVAL -.->|"blocks merge if detection falls over 3 points\nor false alarms exceed 5%"| WK
  LIVE --> REV
  RULES -.->|"full output"| FS
  PW -.->|"full output"| FS
  RAG -.->|"full chunks"| FS
  FS -.->|"summary + file ref"| CTX
  CTX --> GUARD
  VER -->|"state saved after each step"| CKP
  CKP -.->|"resume by thread_id"| RD
```

---

## 3. Send exactly once: state machine

```mermaid
stateDiagram-v2
  [*] --> pending_review
  pending_review --> approved: reviewer approves (hash stored)
  pending_review --> rejected: reject + reason
  approved --> sending: lock row by idempotency key
  sending --> sent: stand-in confirms
  sending --> approved: timeout, retry with backoff
  sending --> dead_letter: retries exhausted
  sent --> [*]
```

A second click on Approve, or a retry after a crash, finds the row already `sending` or `sent` and does nothing. The checkpointer resumes checks; only the idempotency key protects sending.

---

## 4. Context engineering: shared state, files, summaries

Big data goes to a file; state keeps a short summary and a reference. Each step gets only what it needs.

```mermaid
flowchart LR
  IN["Big raw outputs\npage text, screenshots,\nrule chunks, findings"] --> OFF["Offload to file store\nkeep path + hash"]
  OFF --> SUM["Summarize to a few lines\nonly what the next step needs"]
  SUM --> ST["Shared state (thin)\nids, status, summary, file refs"]
  ST --> SEL["Context builder (per step)"]
  SEL --> T["Select tools\nonly those this step needs"]
  SEL --> R["Select resources\ntop-k rule chunks,\nTHIS client only"]
  SEL --> P["Prompt = task + summary\n+ selected chunks"]
  SEL -.->|"detail needed: read file by ref"| OFF
  T --> P
  R --> P
  P --> LLM["Model call"]
  LLM --> OUT["Structured output\nverdict + cited entry"]
  OUT --> OFF
```

---

## 5. Checkpointer: resume after a stop

```mermaid
sequenceDiagram
  participant W as Worker (LangGraph)
  participant C as Checkpointer (PostgreSQL)
  participant S as Stand-in platform
  W->>C: save state after N2 (thread_id = batch_id:ad_id)
  W->>C: save state after N3
  Note over W: crash, restart or deploy
  W->>C: load latest checkpoint for thread_id
  C-->>W: state as of after N3
  W->>W: continue from N4 (N2 and N3 not repeated)
  W->>C: save state after N4
  W->>S: send with idempotency key
  Note over W,S: a resume cannot duplicate a send
```

---

## 6. Mapping: every box to its source

| Box | PRD section | Source |
|---|---|---|
| N1 Ingest, dedup, bad-row isolation, Redis queue | 4.1 | [B R1], [N] Redis |
| CSV and XLSX upload | 4.1 | [D5], [N] "excel sheet" |
| N2 Mechanical checks | 4.2 | [B R2] |
| N3 Page check, Playwright, read-only | 4.3 | [B R3] |
| N4 Wording check, per-client retrieval, citations | 4.4 | [B R4], [N] RAG |
| pgvector in PostgreSQL | 7 | [D6] |
| N5 Verdict engine, step and spend caps | 4.5 | [B R5] |
| Review screen, edits and reasons | 4.6 | [B R6] |
| Approval record with content hash | 4.6 | [D13], [B R6] |
| Client approval evidence (note or screenshot) | 4.6 | [D3], [B section 4 Tobias] |
| Sender, idempotency key, dead-letter | 4.6 | [B R6] |
| Stand-in platform; real calls OFF | 4.6 | [B R6] |
| Auth and role-based access | 4.8 | [B R8] |
| Guards: text is data | 4.8 | [B R8], [N] guardrails |
| Metrics screen | 4.7 | [B R7] |
| Tracing (Langfuse) | 4.9 | [B R9], [D8] |
| Eval replay and CI gate (GitHub Actions) | 4.7 | [B R7], [D9] |
| File store (Docker volume) | 5 | [N] context engineering, [D7] |
| Shared state, context builder | 5 | [N] context engineering |
| Checkpointer, `thread_id` | 5 | [N] checkpointer, [D18] |
| Live status (SSE) | 7 | [D12] |
| Next.js, FastAPI, Redis, PostgreSQL | 7 | [N], [B section 8] |
| One hosted model provider, spend cap in code | 7 | [B R5, section 8], [D11] |

**Removed from v1 diagrams** (compared with v1 of this file): client approval link, MCP publishing, auto-correct and relaunch, post-launch monitoring nodes 10 to 12. They are listed under "NOT in scope" in the PRD.
