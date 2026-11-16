# Serverless ETL & Entity Resolution Pipeline

[![CI](https://github.com/zebok/attorney-scrapper/actions/workflows/ci.yml/badge.svg)](https://github.com/zebok/attorney-scrapper/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Architecture: Serverless GCP](https://img.shields.io/badge/GCP-Cloud%20Run%20%7C%20BigQuery-4285F4.svg)](https://cloud.google.com/)

A production-grade data enrichment framework featuring resilient web ingestion, multi-signal identity reconciliation, and human-in-the-loop review on Google Cloud Platform and BigQuery.

*Reference Implementation: State Bar Legal Directory & Professional Roster Verification.*

---

## Architecture & ETL Overview

Cross-referencing internal rosters against public registries presents fundamental data engineering challenges: stateful web sessions, strict rate limits, unnormalized naming formats, and homonym collisions. 

This repository provides an enterprise-ready, serverless ETL framework designed to reconcile messy input data against state-maintained directories with sub-millisecond scoring and verifiable audit trails.

- **Extract (Resilient Web Ingestion)**: Adaptive rate limiting with randomized crawl-delay jitter, ColdFusion session bootstrapping, automatic exponential backoff retries, and layout drift canaries (`DomDriftError`).
- **Transform & Reconcile (Multi-Signal Scoring Engine)**: Decoupled 0-100 scoring engine cross-validating name similarity, corporate firm affiliations, historical merger aliases, career timeline plausibility, and metropolitan area statistical clustering.
- **Load (Staging-to-Gold Lakehouse)**: Staging ingestion via BigQuery Load Jobs (NDJSON) followed by atomic SQL `MERGE` into golden master tables upon passing automated 3-tier Data Quality gates.
- **Operational Review (Human-in-the-Loop Console)**: Single-page executive web console built with FastAPI, enabling side-by-side candidate comparison, one-click confirmation, and verified ID feedback caching to accelerate subsequent runs.

---

## Key Features

- **Resilient & Polite Ingestion**: Adaptive crawl delays with randomized jitter, session bootstrapping, backoff retries, and high-performance HTML parsing via `selectolax` (Lexbor engine).
- **Composite Multi-Signal Scoring (0–100 pts)**:
  - **Name Match (35 pts)**: Nicknames, middle initials, suffixes, preferred names, and corporate email handle corroboration.
  - **Firm Affiliation (25 pts)**: Canonical firm normalization, historical merger aliases, and corporate email domain mapping.
  - **Temporal Consistency (25 pts)**: Graduation year and state directory admission date alignment with multi-year tolerance.
  - **Geographic Alignment (15 pts)**: Exact municipality matching and Metropolitan Statistical Area (MSA) clustering.
- **Three-Tier Confidence Semaphore Policy**:
  - `auto_approved` (>= 85 pts): Automatically promoted to golden master records with extracted E.164 phone numbers.
  - `needs_review` (55 - 84 pts or detected lateral firm move): Routed to the review console for side-by-side verification.
  - `quarantined` (< 55 pts or unlicensed): Excluded from production deliverables to protect warehouse integrity.
- **Human-in-the-Loop Web Console**: Lightweight, responsive dashboard with side-by-side evidence inspection, candidate selection, search filtering, and 1-click CSV download.
- **Dual Storage Backend**: Supports local CSV storage (default for offline/local development) and Google BigQuery for production lakehouse runs.
- **Automated Data Quality Gate**: 3-tier validation (field-level regex, cross-attribute consistency, and batch-level anomaly thresholds) that halts master promotion if error rates spike.
- **Serverless Cloud Deployment**: Multi-stage Dockerfile and full Terraform Infrastructure-as-Code for zero-idle-cost execution on Cloud Run Jobs, BigQuery, Cloud Scheduler, and Secret Manager.

---

## Quick Start (60 Seconds)

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or standard `pip`

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/zebok/attorney-scrapper.git
cd attorney-scrapper

# Install dependencies with uv
uv sync
```

*Or using standard `pip`:*

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Configuration

Copy the example environment configuration:

```bash
cp .env.example .env
```

*By default, `STORAGE_BACKEND=csv` is enabled, allowing the pipeline and review console to run locally without cloud credentials.*

### 3. Verify Directory Health & Connectivity

```bash
txbar health
```

### 4. Run Batch Enrichment

```bash
# Test run with first 5 records
txbar run --limit 5

# Or run the full roster
txbar run
```

### 5. Launch the Review Console

```bash
txbar ui --port 8080
```

Open `http://localhost:8080` in your browser to inspect matches, review ambiguous candidates, and export reports.

---

## System Architecture

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
        SCORE -->|"High Confidence: Score >= 85%"| AUTO["[AUTO-APPROVED]<br/>Promoted to Golden Master & CSV"]
        SCORE -->|"Medium Confidence: 55% - 84%<br/>or Lateral Firm Move"| REV["[HUMAN REVIEW QUEUE]<br/>Side-by-side verification console"]
        SCORE -->|"Low Confidence: Score < 55%<br/>or Not Licensed"| REJ["[QUARANTINED / REJECTED]<br/>Protects production warehouse"]
    end

    subgraph Output["4. Business Deliverables & Feedback"]
        AUTO --> UI["Web Review Console<br/>1-Click CSV Export + Live Search"]
        REV --> UI
        UI -->|"1-Click Confirm"| CACHE["Cache Verified State Bar ID<br/>Subsequent runs skip search stage"]
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
Shows the automated serverless deployment on Google Cloud Platform, the 3-tier Data Quality gate, and the staging-to-gold lakehouse pattern in BigQuery.

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

    subgraph Storage["3. BigQuery Storage Layer"]
        DQ -->|"Staging Load Job (NDJSON)"| BQR[("BigQuery Staging Tables:<br/>match_results & match_candidates")]
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

## CLI Reference

The package installs the `txbar` command-line interface:

| Command | Arguments / Options | Description |
|:---|:---|:---|
| `txbar health` | | Validates Texas Bar directory connectivity, session bootstrapping, and DOM landmark canaries. |
| `txbar run` | `--limit N` | Executes the batch enrichment pipeline over the source roster. |
| `txbar ui` | `--host HOST`, `--port PORT` | Launches the interactive review console and REST API. |
| `txbar export-csv` | `--out PATH` | Exports enriched golden records from active storage to clean delivery CSV. |
| `txbar seed` | `--csv PATH` | Normalizes and seeds an input CSV into the active `attorneys_source` table. |

---

## Configuration Reference

Configuration is managed via Pydantic Settings and loaded from environment variables or `.env`:

| Variable | Default | Description |
|:---|:---|:---|
| `STORAGE_BACKEND` | `csv` | Persistence engine: `csv` (local directory) or `bigquery` (GCP). |
| `LOCAL_WAREHOUSE_DIR` | `data/warehouse` | Directory storing local CSV tables mirroring BigQuery 1:1. |
| `DEFAULT_INPUT_CSV` | `data/input/tx_lawyers.csv` | Default source roster CSV path. |
| `RULES_DIR` | `data/rules` | Location of declarative JSON domain rules. |
| `SCHEMAS_DIR` | `schemas` | JSON table schemas shared between Polars frames and Terraform. |
| `GCP_PROJECT_ID` | `None` | Google Cloud project ID (required when `STORAGE_BACKEND=bigquery`). |
| `BQ_DATASET` | `txbar` | BigQuery dataset name. |
| `GCP_REGION` | `us-central1` | Google Cloud region for BigQuery dataset and Cloud Run. |
| `REVIEWER_TOKEN` | `""` | Secret required in `X-Reviewer-Token` header for review decisions (empty disables auth). |
| `WEIGHT_NAME` | `35.0` | Max score points for name similarity. |
| `WEIGHT_FIRM` | `25.0` | Max score points for law firm affiliation and merger aliases. |
| `WEIGHT_TEMPORAL` | `25.0` | Max score points for graduation year vs license date alignment. |
| `WEIGHT_GEO` | `15.0` | Max score points for municipality and metropolitan cluster. |
| `MATCH_AUTO_APPROVE`| `85.0` | Minimum score for automatic golden master promotion. |
| `MATCH_REVIEW_FLOOR`| `55.0` | Minimum score to enter human review queue (below is quarantined). |
| `MATCH_AMBIGUITY_DELTA` | `8.0` | Score delta between top candidate and runner-up that triggers review. |

---

## Extensibility: Adding Other State Bars

The system decouples state directory implementations via the `DirectoryProvider` protocol (`src/txbar/provider.py`). Adding a new state (e.g. California or New York) requires only implementing this interface:

```python
from txbar.provider import DirectoryHit, DirectoryProvider, Profile, ProviderHealth


class CaliforniaBarClient(DirectoryProvider):
    provider_id = "cabar"
    provider_name = "State Bar of California"

    def search_by_affiliation(self, last_name: str, firm_core: str) -> list[DirectoryHit]: ...
    def search_by_location(self, name: str, city: str) -> list[DirectoryHit]: ...
    def search_statewide(self, last_name: str) -> list[DirectoryHit]: ...
    def get_profile(self, contact_id: str) -> Profile: ...
    def health_check(self) -> ProviderHealth: ...
    def profile_url(self, contact_id: str) -> str: ...
```

The composite scoring engine, waterfall resolution, data quality canaries, and BigQuery schemas remain completely unchanged.

---

## Testing & Quality Assurance

The test suite covers parsing, signals, homonym disambiguation, storage backends, and API endpoints without making external network calls:

```bash
# Run unit tests with pytest
uv run pytest -v

# Run linter
uv run ruff check .

# Check code formatting
uv run ruff format --check .
```

---

## Project Structure

```text
├── .github/
│   └── workflows/ci.yml       # Automated GitHub Actions test & lint suite
├── data/
│   ├── input/                 # Source attorney rosters
│   ├── output/                # Enriched deliverables and run reports
│   └── rules/                 # Declarative firm aliases, metro areas, nicknames
├── docs/
│   └── DIAGRAMS_EN.md         # Full architecture and sequence diagrams
├── infra/
│   └── terraform/             # GCP Cloud Run, BigQuery, Cloud Scheduler IaC
├── schemas/                   # Single-source-of-truth JSON table schemas
├── src/txbar/
│   ├── client.py              # Polite HTTP client for Texas Bar directory
│   ├── config.py              # Runtime configuration via Pydantic Settings
│   ├── matching.py            # Waterfall search coordinator and decision semaphore
│   ├── normalize.py           # Robust name, suffix, firm, and phone normalizers
│   ├── parser.py              # HTML extraction via Selectolax & DOM canary checks
│   ├── pipeline.py            # Batch orchestration and deliverable formatting
│   ├── provider.py            # DirectoryProvider protocol & unified domain models
│   ├── quality.py             # 3-tier Data Quality gate and anomaly detectors
│   ├── rules.py               # Declarative KnowledgeBase rule provider
│   ├── schemas.py             # Polars schema enforcement matching BigQuery 1:1
│   ├── signals.py             # Decoupled Name, Firm, Temporal, and Geo signals
│   ├── storage.py             # Interchangeable CSV & BigQuery storage backends
│   └── web/                   # Interactive review dashboard & FastAPI REST API
├── tests/                     # Unit test suite & sanitized HTML fixtures
├── Dockerfile                 # Multi-stage production container
├── pyproject.toml             # Project metadata, dependencies, and tools configuration
├── CONTRIBUTING.md            # Guidelines for open-source contributors
└── LICENSE                    # MIT License
```

---

## Contributing

Contributions, issues, and feature requests are welcome! Please review [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

---

## License

This project is licensed under the [MIT License](LICENSE).
