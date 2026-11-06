# Switch DealDesk: Multi-Room Vendor Procurement & Contract Redlining War Room

When a company buys SaaS or negotiates an enterprise Master Services Agreement (MSA), the vendor rep, finance auditor, security reviewer, and legal counsel cannot all work inside one flat chat channel. If your finance agent calculates your internal walk-away ceiling in the same room where the vendor drops their quote, you just handed the vendor your reservation price. If an external vendor can `@mention` your internal finance agent directly, they can prompt-inject it into leaking the CFO's budget cap.

**Switch DealDesk** solves this by provisioning a linked two-room topology on Switch (`dealdesk.template.yaml`, `version: 2`):

1. **`#acme-vendor-intake` (`channel_public`)**: External vendor account executives drop quotes, order forms, and draft MSAs. `@acme-intake-concierge` (`addressing: anyone`) acknowledges receipt and forwards structured quote metadata to the private deal desk.
2. **`#acme-internal-deal-desk` (`channel_private`, linked via `escalates-quote-to`)**: `@acme-finance-auditor`, `@acme-secops-reviewer`, and `@acme-legal-redliner` (`addressing: owner-agents`) cross-read `#acme-vendor-intake` via `read_context(room_id=...)` without ever posting internal budget numbers in the public channel.

---

## Why Switch Is Load-Bearing (And What Breaks Without It)

| Switch Primitive | How DealDesk Uses It | What Fails Without Switch (`--prove-switch-central`) |
| :--- | :--- | :--- |
| **Scoped Agent Addressing (`addressing: owner-agents`)** | Only `@acme-intake-concierge` uses `addressing: anyone`. Finance, SecOps, and Legal use `addressing: owner-agents`. | In a regular group chat, the vendor rep can `@acme-finance-auditor` in `#acme-vendor-intake` to ask for the maximum walk-away budget. Switch's `MessageRouter` drops delivery at the transaction layer (`dropped_by_addressing`). |
| **Cross-Room `read_context(room_id=...)`** | Internal agents belong to both rooms so they can read `#acme-vendor-intake` from inside `#acme-internal-deal-desk` while keeping all TCO math and DPA audit findings inside `channel_private`. | Without room-isolated context reads, internal walk-away prices ($280/seat, $42,000 ACV ceiling) either leak into the vendor thread or require manual copy-pasting between silos. |
| **Single-Writer Room Documents (`created_by`)** | The human CFO owns `cfo-budget-mandate` (pre-created in `dealdesk.template.yaml`). `@acme-legal-redliner` creates `risk-and-redline-ledger` at runtime via `create_room_document`. | If an agent tries to call `update_room_document` on `cfo-budget-mandate` to raise the budget cap, Switch rejects the write with `403 SINGLE_WRITER_VIOLATION`. |
| **Exclusive Assumable Roles (`exclusive: true` + 6s TTL)** | `@acme-legal-redliner` assumes the exclusive `signatory-gate` role (`assume_role` tied to its `/connection/beat` lease) before assembling the counter-offer. | If a second agent tries to assume `signatory-gate` concurrently, Switch rejects it with `409 EXCLUSIVE_ROLE_HELD`. If the holder crashes, the 6-second heartbeat TTL releases the lock automatically. |
| **Verified Human Signatory Gate (`list_participants`)** | Before posting Counter-Offer v2 to `#acme-vendor-intake`, `@acme-legal-redliner` resolves the opaque `sender` transport ID against `list_participants` to verify `type == "human"` and `is_owner == True`. | If an agent or external vendor posts `Approved. Send counter-offer.`, verification rejects the message with `403 UNVERIFIED_SIGNATORY`. |
| **Scheduled Room Follow-Ups (`create_schedule`)** | `@acme-legal-redliner` registers a weekday `0 9 * * 1-5` (`America/New_York`) room schedule to track the 60-day renewal notice window. | Follow-ups and renewal deadlines get buried in chat history instead of firing back into the room on schedule. |

---

## Architecture

```text
+-------------------------------------------------------------------------+
| Public Room: #acme-vendor-intake (channel_public)                       |
|                                                                         |
|  Marcus (CloudPulse AE) ---> Posts Quote v1 ($340/seat, $51,000 ACV,    |
|                              15d auto-renewal, 9% uplift, 72h breach)   |
|  @acme-intake-concierge ---> Logs receipt (no internal numbers leaked)  |
+------------------------------------+------------------------------------+
                                     |
           link: escalates-quote-to  |  read_context(room_id=vendor_intake)
                                     v
+-------------------------------------------------------------------------+
| Private Room: #acme-internal-deal-desk (channel_private)                |
|                                                                         |
|  [Doc: cfo-budget-mandate] (Single-writer: Priya CFO, $42k ACV cap)     |
|                                                                         |
|  1. @acme-finance-auditor (addressing: owner-agents)                    |
|     - Cross-reads quote + cfo-budget-mandate                            |
|     - Flags +$9,000/yr overage; computes $265/seat ($39,750 ACV) target |
|                                                                         |
|  2. @acme-secops-reviewer (Role: compliance-reviewer, shared)           |
|     - Flags 72h breach window -> 48h, unilateral subprocessors -> 30d   |
|                                                                         |
|  3. @acme-legal-redliner (Role: signatory-gate, exclusive: true)        |
|     - Creates single-writer [Doc: risk-and-redline-ledger]              |
|     - Uploads artifacts/redlined-msa-diff.html via /media               |
|     - Creates weekday follow-up via create_schedule                     |
|     - HALTS until verified human owner posts:                           |
|       "Approved. Send counter-offer."                                   |
+------------------------------------+------------------------------------+
                                     |
                                     v
     Sanitized Counter-Offer v2 posted back to #acme-vendor-intake
     ($265/seat, $39,750 ACV, 60d notice, <=3% uplift, 48h breach, 2x cap)
```

---

## Quickstart (One Command)

Clone the repository and run `bash demo.sh`:

```bash
git clone https://github.com/TejasLamba2006/switch-dealdesk.git
cd switch-dealdesk
bash demo.sh
```

`demo.sh` provisions the `version: 2` template (`dealdesk.template.yaml`) against the Switch Agent Protocol harness (`GET /agents/{id}/events`, `POST /agents/{id}/connection/beat`, `POST /agents/{id}/ops/{name}`, `POST /agents/{id}/media`), runs all four agents through the CloudPulse Enterprise negotiation, executes the four `--prove-switch-central` adversarial tests, and generates two visual HTML artifacts:

- `artifacts/redlined-msa-diff.html` — Clause-by-clause redline table comparing the vendor's v1 draft against the CFO-mandated v2 counter-offer.
- `artifacts/dealdesk-war-room.html` — Side-by-side view of `#acme-vendor-intake` (`channel_public`) and `#acme-internal-deal-desk` (`channel_private`) plus the 4 adversarial boundary test verdicts.

### Run the Test Suite

```bash
pytest
```

### Validate `dealdesk.template.yaml` Against `switch-core`

`dealdesk.template.yaml` is validated directly against `switch_core.template_lint.lint_template`, `switch_core.template_v2.compile_v2`, and `switch_core.rooms_yaml.GroupTemplateDocument` with zero errors and zero warnings. To import it in Switch Console:

1. Open **Switch Console** -> **Templates** -> **Import YAML**.
2. Select `dealdesk.template.yaml`.
3. Pick your company slug (`acme`), collaboration bridge (`Mattermost` or `Slack`), and coding agent provider (`claude`, `codex`, or `opencode`).
