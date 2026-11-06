# What I Learned Shipping 5 Core PRs to Switch and Building Multi-Room Agent Workflows

Most multi-agent demos cheat in the exact same way: they put three prompts inside one Python loop, print their output to a terminal, and call it "collaboration." The moment you put real humans, external vendors, and multiple coding agents into the same Slack or Mattermost workspace, that illusion falls apart.

Over the four-day Flip the Switch Hackathon (Nov 4-7, 2026), I worked on both sides of the stack. First, I contributed five pull requests to Switch's core (`sandbox-quantum/switch` issues `#771`, `#799`, `#800`, `#802`, and `#803`). Then I used Switch's `version: 2` template engine and Agent Protocol to build two multi-room workflows: **Switch DealDesk** (a public/private vendor procurement and contract redlining war room) and **Switch Studio** (a fact-checked creative production room).

Here is how Switch actually works under the hood, what we changed in core, and how to design workflows where the room topology does the heavy lifting.

---

## 1. How a Switch Room Works Under the Hood

Switch is not a wrapper around a chat webhook. Every message in a Switch room is an `INSERT` into PostgreSQL (`core/switch_core/db/stores/message_store.py`) guarded by a per-room advisory lock that assigns a strictly monotonic `seq`.

In the same database transaction as the `INSERT`:

1. `MessageRouter` (`core/switch_core/delivery/router.py`) evaluates every agent in the room against that agent's addressing policy (`anyone`, `owner`, `owner-agents`, or `{ anyone_in: [...] }`).
2. For each agent that should receive the message, the router writes an `agent_events` row in the same transaction.
3. On commit, the `messages_notify` trigger fires `pg_notify('switch_message', ...)` with the room ID, `seq`, and row ID.
4. A single `MessageListener` connection wakes up the subscribers for that room, and each client reads rows after its own cursor, 200 rows at a time.

Because routing happens inside the write transaction, addressing rules are not prompt instructions that an LLM can ignore. If an agent is configured with `addressing: owner-agents`, and an external guest in a shared channel `@mentions` that agent, `MessageRouter` never creates an `agent_events` row for it. The agent process never even receives the payload over its SSE stream.

---

## 2. The 5 Core PRs We Shipped to `sandbox-quantum/switch`

While running agents in real channels, five day-to-day operational gaps stood out. Each became an assigned Track 03 pull request:

### PR #1214 (Issue #802): Live Subagents on the Activity Card
When a Claude Code or Codex session spawns background subagents (`Task` / child sessions), the channel activity card previously looked frozen until the parent turn finished. We extended the session-v1 wire contract (`console/packages/shared/src/session-v1/contract.ts` and `core/switch_core/sessions/contract.py`) with a `'subagent'` item kind, mapped provider child-session events in `ChatProjector`, added an Alembic migration (`a3d9c5e1b7f2`) widening the `session_activity_items` check constraint, and rendered live subagent rows (`▸` running, `✓` completed, `✗` failed) across Console, Slack Block Kit, and neutral Markdown renderers.

### PR #1215 (Issue #803): Safe, Opt-In Tool Input & Output in the Channel
By default, Switch keeps raw tool arguments and outputs on the execution host so secrets never leak into Slack or Mattermost. When debugging a failing command with teammates, though, seeing only the tool title is not enough. We added an per-agent `tool_details` opt-in column (`c3f8a1d27e54`), built a bounded `ToolDetailBuffer` on the session host that runs every byte of tool input and output through `Redactions` (scrubbing service tokens and holding back partial token prefixes during streaming), and rendered expandable `<details>` / Slack code blocks in the room.

### PR #1216 (Issue #799): Bring-Your-Own Messaging App (Generic Webhook Platform)
Switch supported Slack, Mattermost, Discord, Teams, and Telegram, but teams with internal chat portals or custom web apps had no drop-in bridge. We built `WebhookAdapter` (`core/switch_core/bridges/collaboration/webhook/`), an HMAC-SHA256 signed inbound/outbound collaboration bridge with timestamp replay protection, SSRF validation on outbound webhook URLs, and full thread/attachment mapping.

### PR #1592 (Issue #800): `!cancel` a Queued Message Before the Agent Picks It Up
If an agent is busy on Turn 1 and a teammate sends a follow-up with a typo, `!interrupt` would kill Turn 1 instead of pulling the queued Turn 2 out of the inbox. We added `turn.cancel` and the `'cancelled'` turn status across the TypeScript and Python session-v1 contracts, implemented `HostedSession.cancelQueued` to splice the queued command out of the host queue (verifying `origin.actorId` and `origin.roomId` so only the sender can cancel their own message), added a 422 fallback to `'interrupted'` for older servers, and printed `!cancel @<agent> <8-char-turn-prefix>` directly on every queued card.

### PR #1593 (Issue #771): Scheduled Messages to Agents in a Room
Rooms needed a native way to wake agents on a cron schedule or a one-off timestamp without an external cron daemon. We added the `room_schedules` table (`c5a8e2f7d913`) with tenant RLS policies, `FOR UPDATE SKIP LOCKED` due-claiming, stdlib `zoneinfo` 5-field cron evaluation, a 24-hour late-run grace window (coalescing missed runs while the server was down into a single fire), automatic handling when an agent leaves a room (`partial` vs `no_agents` auto-pause), four new MCP tools (`list_schedules`, `create_schedule`, `update_schedule`, `delete_schedule`), and a full Schedules tab in Switch Console using `CronPicker`.

---

## 3. Designing Multi-Room Workflows Where Switch Is Central

When we moved from core PRs to building **Switch DealDesk** (`TejasLamba2006/switch-dealdesk`) and **Switch Studio** (`TejasLamba2006/switch-studio`), we wanted every step of the workflow to depend on Switch's room primitives rather than prompt engineering:

1. **Public vs. Private Linked Rooms (`channel_public` + `channel_private`)**: In DealDesk, the vendor rep posts their $51,000/yr quote in `#acme-vendor-intake`. Our internal agents (`@acme-finance-auditor`, `@acme-secops-reviewer`, `@acme-legal-redliner`) live in `#acme-internal-deal-desk` and belong to `#acme-vendor-intake` with `addressing: owner-agents`. They call `read_context(room_id=vendor_intake_id)` to inspect the vendor's MSA while keeping our $42,000 CFO budget cap and $39,750 counter-offer math sealed inside the private room.
2. **Single-Writer Room Documents (`created_by`)**: The CFO's budget mandate is provisioned as a room document owned by `{$creator}`. Agents can read it via `load_internal_documents`, but if an agent attempts `update_room_document` on `cfo-budget-mandate`, Switch refuses the write with `SINGLE_WRITER_VIOLATION`. Conversely, `@acme-legal-redliner` creates `risk-and-redline-ledger` at runtime via `create_room_document`, making the legal agent its single writer.
3. **Assumable Roles with Heartbeat Leases (`exclusive: true`)**: Before drafting a counter-offer (DealDesk) or triggering a final render (Studio), the lead agent must call `assume_role` on an exclusive room role (`signatory-gate` or `on-air-director`). That lease is bound to the agent's 2-second `/connection/beat` heartbeat with a 6-second TTL.
4. **Verified Human Approval via `list_participants`**: SSE message payloads carry an opaque transport sender ID. Before dispatching a counter-offer or publishing a cut, our agents call `list_participants` and verify that the sender of `Approved.` is both `type == "human"` and `is_owner == True`.

---

## 4. Try It Yourself

Both workflow repositories include a one-command `bash demo.sh` that provisions the `version: 2` `.template.yaml`, runs the full multi-room workflow over the Switch Agent Protocol, and runs `--prove-switch-central` to verify that every single trust boundary fails closed when attacked:

- **Switch DealDesk (Track 01)**: `https://github.com/TejasLamba2006/switch-dealdesk`
- **Switch Studio (Track 02)**: `https://github.com/TejasLamba2006/switch-studio`
- **Core PRs (Track 03)**: `#1214`, `#1215`, `#1216`, `#1592`, `#1593` on `sandbox-quantum/switch`
