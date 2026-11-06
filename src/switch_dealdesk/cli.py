"""CLI entry point for Switch DealDesk demo and Switch-central proofs."""

from __future__ import annotations

import argparse
from pathlib import Path

from switch_dealdesk.workflow import (
    VENDOR_QUOTE_TEXT,
    prove_switch_central,
    provision_from_template,
    render_war_room_dashboard,
    run_finance_auditor,
    run_intake_concierge,
    run_legal_redliner_prepare,
    run_secops_reviewer,
    verify_and_dispatch_counter_offer,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Switch DealDesk multi-room workflow.")
    parser.add_argument(
        "--prove-switch-central",
        action="store_true",
        help="Run adversarial boundary tests proving Switch is load-bearing.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    template_path = root / "dealdesk.template.yaml"
    artifacts_dir = root / "artifacts"

    topo = provision_from_template(template_path, company="acme")
    try:
        print(f"[1/6] Provisioned Switch v2 template on {topo.server.base_url}")
        print(f"      Public room : {topo.vendor_intake_id} (channel_public)")
        print(f"      Private room: {topo.war_room_id} (channel_private)")

        topo.state.post_room_message(
            room_id=topo.vendor_intake_id,
            sender_id=topo.vendor_user_id,
            body=VENDOR_QUOTE_TEXT,
        )
        intake = run_intake_concierge(topo)
        print(
            f"[2/6] @acme-intake-concierge parsed quote: {intake['seats']} seats @ "
            f"${intake['unit_price']}/seat (${intake['acv']:,}/yr ACV)"
        )

        fin = run_finance_auditor(topo)
        print(
            f"[3/6] @acme-finance-auditor cross-read {topo.vendor_intake_id} & 'cfo-budget-mandate': "
            f"overage +${fin['overage']:,}/yr -> counter target ${fin['target_acv']:,}/yr"
        )

        sec = run_secops_reviewer(topo)
        print(
            f"[4/6] @acme-secops-reviewer assumed '{sec['role']}' role and flagged "
            f"{len(sec['findings'])} DPA clauses"
        )

        prep = run_legal_redliner_prepare(topo, artifacts_dir)
        print(
            f"[5/6] @acme-legal-redliner locked exclusive 'signatory-gate' role, created "
            f"'risk-and-redline-ledger', uploaded {prep['media_id']}, scheduled {prep['schedule_id']}"
        )

        cfo_msg = topo.state.post_room_message(
            room_id=topo.war_room_id,
            sender_id=topo.cfo_user_id,
            body="Approved. Send counter-offer.",
        )
        dispatched = verify_and_dispatch_counter_offer(topo, cfo_msg)
        print(
            f"[6/6] Verified human owner sign-off ({dispatched['verified_owner']}) -> "
            f"counter-offer v2 posted to {topo.vendor_intake_id}"
        )

        proofs = prove_switch_central(topo)
        if args.prove_switch_central:
            print("\n=== SWITCH-CENTRAL ADVERSARIAL PROOFS ===")
            for p in proofs:
                print(f"  * {p['boundary']}")
                print(f"    Attack : {p['attack']}")
                print(f"    Verdict: {p['outcome']}")

        dashboard = render_war_room_dashboard(topo, proofs, artifacts_dir)
        print(f"\nArtifacts written to:\n  - {prep['diff_path']}\n  - {dashboard}")
    finally:
        topo.server.stop()


if __name__ == "__main__":
    main()
