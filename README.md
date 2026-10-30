# Texas Bar Attorney Enrichment & Entity Resolution Pipeline

An end-to-end data pipeline that enriches attorney rosters against the State Bar of Texas directory with high-confidence entity resolution, polite scraping, and human-in-the-loop review.

---

## Key Features

- **Resilient & Polite Scraper**: Adaptive rate limiting (crawl-delay with jitter), retry mechanisms with exponential backoff, and robust HTML parsing via `selectolax`.
- **Composite Scoring Engine (0–100)**: Multi-signal identity resolution evaluating:
  - Name similarity (handling nicknames, initials, compound surnames)
  - Law firm matching & alias resolution
  - Email domain & handle corroboration
  - Bar admission year vs. JD year
  - Geographic/metro area validation
- **Three-Tier Confidence Policy**:
  - `auto_approved` ($\ge 85$): Directly promoted to golden master records.
  - `needs_review` ($55 - 84$ or detected lateral firm move): Sent to the review queue.
  - `quarantined` ($< 55$ or not licensed): Excluded from production deliverables.
- **Human-in-the-Loop Web Console**: Lightweight FastAPI web dashboard to inspect ambiguous matches, compare side-by-side evidence, and confirm/reject candidates with 1 click.
- **Dual Storage Backend**: Supports Google BigQuery + Cloud Storage for production cloud runs, as well as local CSV storage for offline development.
- **Cloud-Ready**: Packaged with multi-stage Dockerfile and full Terraform IaC for deployment on Google Cloud Run + BigQuery + Cloud Scheduler.

---

## System Architecture & Diagrams

### 1. Executive Pipeline & Confidence Semaphore
Visualizes the end-to-end flow from input roster to enriched delivery, highlighting the 3-tier confidence policy, the executive review console, and the self-accelerating State Bar ID feedback loop.

```mermaid
flowchart LR
    subgraph Input["1. Input Attorney Roster"]
        SRC["Roster Source Table<br/>BigQuery attorneys_source<br/>attorney_uuid + target roster"]
    end

    subgraph Engine["2. Resilient Scraper & Entity Resolution"]
        SRC --> SCR["State Bar Directory Scraper<br/>Resolves Initials, Nicknames & Mergers"]
        SCR --> SCORE["100-Point Scoring Engine<br/>Cross-validates Name, Email, Firm, Year, Geo"]
    end

    subgraph Decisions["3. Confidence Semaphore Policy"]
        SCORE -->|"High Confidence: Score >= 85%"| AUTO["✅ AUTO-APPROVED<br/>Promoted to Golden Master & CSV"]
        SCORE -->|"Medium Confidence: 55% - 84%<br/>or Lateral Firm Move"| REV["⚠️ HUMAN REVIEW QUEUE<br/>Side-by-side verification console"]
        SCORE -->|"Low Confidence: Score < 55%<br/>or Not Licensed"| REJ["❌ QUARANTINED / REJECTED<br/>Protects production warehouse"]
    end

    subgraph Output["4. Business Deliverables & Feedback"]
        AUTO --> UI["💻 Executive Web Console<br/>1-Click CSV Export + Live Search"]
        REV --> UI
        UI -->|"1-Click Confirm"| CACHE["⚡ Cache Verified State Bar ID<br/>Subsequent runs skip search stage"]
    end
```

---

### 2. Internal Engine & Extensible Design Patterns
Illustrates how the codebase decouples state directory HTTP clients, declarative domain knowledge, and multi-signal scoring algorithms to scale from 1 state to 50+ state bar associations.

```mermaid
flowchart TD
    subgraph DataDriven["1. Declarative Domain Knowledge (data/rules/ + rules.py)"]
        R1["nicknames.json<br/>Legal nickname equivalence clusters"] --> KB["KnowledgeBase Service<br/>Singleton In-Memory Provider"]
        R2["firm_aliases.json<br/>Law firm mergers & historical aliases"] --> KB
        R3["metro_areas.json<br/>Metropolitan statistical area clusters"] --> KB
        R4["firm_domains.json<br/>Corporate email domain maps"] --> KB
        CFG["ScoringConfig (Pydantic)<br/>Signal weights & confidence thresholds"] --> ENG["MatchEngine<br/>Identity Resolution Coordinator"]
    end

    subgraph ProviderPattern["2. Provider Adapter Pattern (provider.py + client.py)"]
        TXP["TexasBarClient<br/>httpx + ColdFusion + hCard"] -.->|"implements"| DP["DirectoryProvider Protocol<br/>Abstract Interface"]
        CAP["CaliforniaBarClient<br/>Future: curl_cffi / Playwright"] -.->|"implements"| DP
        NYP["NewYorkBarClient<br/>Future: API / Headless"] -.->|"implements"| DP
    end

    subgraph CompositeScoring["3. Composite Scoring Strategy (src/txbar/signals.py)"]
        ENG --> S1["NameMatchSignal<br/>Given name, surname & email hints"]
        ENG --> S2["FirmAffiliationSignal<br/>Canonical firm, aliases & corporate domains"]
        ENG --> S3["TemporalCareerSignal<br/>Graduation year & admission license timeline"]
        ENG --> S4["GeographicSignal<br/>Exact municipality & metro area clustering"]
        KB -.-> S1
        KB -.-> S2
        KB -.-> S4
    end

    DP -->|"Normalized Candidate Profiles"| ENG
    ENG --> OUT["MatchDecision + Audit Explanation (0-100 pts)"]
```

---

### 3. 4-Stage Waterfall Search Algorithm
Details how the scraper queries state bar directories efficiently without wasting HTTP requests or getting tripped up by initials, suffixes, or firm relocations.

```mermaid
flowchart LR
    IN["Input Attorney Record<br/>Name, Firm, City, JD Year, Email"] --> S0{"Stage 0:<br/>Cached State Bar ID<br/>from prior verified run?"}
    
    S0 -->|"YES: Subsequent monthly run"| DIRECT["Direct Profile GET Request<br/>1 HTTP req - Zero search overhead"]
    
    S0 -->|"NO: First-time lookup"| S1["Stage 1: Targeted Precision Search<br/>Surname + Canonical Firm Name<br/>Resolves ~90% in ~500ms"]
    
    S1 -->|"Plausible hit found: Score >= 70"| EVAL["Fetch Full Profile (hCard)<br/>+ DOM Canary Verification"]
    
    S1 -->|"0 hits or low score"| S2["Stage 2: Metro / City Search<br/>Full Name + City<br/>Catches lateral moves & blank firm fields"]
    
    S2 -->|"Plausible hit found: Score >= 68"| EVAL
    
    S2 -->|"0 hits in city"| S3["Stage 3: Statewide Relaxation<br/>Surname variants across entire state<br/>Catches suburban offices & state transfers"]
    
    S3 --> EVAL
    DIRECT --> SCORE["Composite Scoring Engine (0-100 pts)"]
    EVAL --> SCORE
```

---

### 4. Cloud-Native Architecture (GCP & BigQuery)
Shows the automated serverless deployment on Google Cloud Platform, the 3-tier Data Quality gate, and the Medallion lakehouse pattern in BigQuery.

```mermaid
flowchart TD
    subgraph Trigger["1. Automated Serverless Orchestration ($0 Idle Cost)"]
        CRON["Cloud Scheduler (Cron)<br/>e.g., Weekly / Monthly at 06:00 UTC"] -->|"HTTP Trigger"| JOB["Cloud Run Job (Docker Container)<br/>CLI Command: txbar run"]
        BQS[("BigQuery Source Table:<br/>attorneys_source<br/>attorney_uuid + target roster + cached IDs")] -->|"1. SELECT batch to process"| JOB
    end

    subgraph Execution["2. Resilient Execution & Quality Canaries"]
        JOB -->|"2. Polite HTTP with randomized jitter"| WEB["State Bar Directory<br/>e.g., texasbar.com ColdFusion"]
        WEB -->|"HTML with hCard Microformats"| PARSE["Parser + DOM Canary Checks<br/>Halts on layout drift or anti-bot challenge"]
        PARSE --> DQ{"3-Tier Data Quality Gate<br/>Schema integrity, E.164 phone,<br/>batch anomaly rate threshold"}
    end

    subgraph Medallion["3. Medallion Storage Layer in GCS & BigQuery"]
        DQ -->|"Bronze Layer: Raw Replay Audit"| GCS[("Cloud Storage Bucket (GCS)<br/>Dated NDJSON snapshots")]
        DQ -->|"Silver Layer: NDJSON Load Job"| BQR[("BigQuery Staging Tables:<br/>match_results & match_candidates")]
        DQ -->|"Run Health & DQ Telemetry"| BQP[("BigQuery Runs Table:<br/>pipeline_runs")]
        
        BQR -->|"If DQ Gate PASSED & High Confidence<br/>Atomic SQL MERGE ON attorney_uuid"| BQM[("Gold Master Table - BigQuery:<br/>attorneys_master<br/>Verified Golden Records")]
        BQR -->|"Feedback loop: Cache verified State Bar ID"| BQS
        BQR -->|"Ambiguous Score or Lateral Move"| BQV[("BigQuery Filtered View:<br/>v_review_queue")]
    end

    subgraph HITL["4. Human-in-the-Loop Review Console"]
        BQV --> UI["Cloud Run Service (Web UI)<br/>txbar-review-console"]
        UI -->|"PATCH Confirm or Reject Decision"| BQR
        UI -->|"Audit Event Trail"| BQE[("BigQuery Audit Table:<br/>review_events")]
    end
```

---

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or standard `pip`

### 1. Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/zebok/attorney-scrapper.git
cd attorney-scrapper
uv sync
```

Or using `pip`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Configuration

Copy the example environment file and adjust your settings as needed:

```bash
cp .env.example .env
```

To run purely locally without Google Cloud:
```env
STORAGE_BACKEND=csv
```

---

## Usage (CLI)

The package installs the `txbar` CLI command:

### Check Health & Connectivity
Verify Texas Bar directory access and DOM structure:
```bash
txbar health
```

### Run Enrichment Pipeline
Run batch enrichment on the input attorney roster:
```bash
# Run on full roster
txbar run

# Or test with a limit
txbar run --limit 5
```

### Launch Review Console
Start the interactive review dashboard:
```bash
txbar ui --port 8080
```
Open `http://localhost:8080` in your browser to inspect matches, review ambiguous candidates, and export reports.

### Export Results
Export enriched records directly to CSV:
```bash
txbar export-csv --out data/output/enriched_tx_lawyers.csv
```

---

## Running Tests

Execute the unit test suite:

```bash
uv run pytest
```

---

## Project Structure

```text
├── data/
│   ├── input/             # Source attorney rosters
│   ├── output/            # Enriched deliverables and run reports
│   └── rules/             # Declarative firm aliases, metro areas, nicknames
├── docs/
│   └── DIAGRAMS_EN.md     # Architecture and sequence diagrams
├── infra/
│   └── terraform/         # GCP Cloud Run, BigQuery, GCS infrastructure
├── schemas/               # BigQuery JSON table schemas
├── src/txbar/
│   ├── client.py          # Polite HTTP client for Texas Bar directory
│   ├── parser.py          # HTML extraction and data normalization
│   ├── matching.py        # Entity resolution scoring engine
│   ├── pipeline.py        # Orchestration and data quality checks
│   ├── storage.py         # BigQuery and CSV persistence layer
│   └── web/               # FastAPI review dashboard and UI
├── tests/                 # Unit tests and HTML fixtures
├── Dockerfile             # Multi-stage production container
└── pyproject.toml         # Project metadata and dependencies
```

---

## License

MIT
