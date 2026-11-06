from __future__ import annotations

import time
from pathlib import Path

import pytest

from switch_dealdesk.protocol import SwitchProtocolError
from switch_dealdesk.workflow import (
    VENDOR_QUOTE_TEXT,
    prove_switch_central,
    provision_from_template,
    run_finance_auditor,
    run_intake_concierge,
    run_legal_redliner_prepare,
    run_secops_reviewer,
    verify_and_dispatch_counter_offer,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "dealdesk.template.yaml"


def test_end_to_end_dealdesk_workflow(tmp_path: Path) -> None:
    topo = provision_from_template(TEMPLATE, company="acme")
    try:
        topo.state.post_room_message(
            room_id=topo.vendor_intake_id,
            sender_id=topo.vendor_user_id,
            body=VENDOR_QUOTE_TEXT,
        )
        intake = run_intake_concierge(topo)
        assert intake["seats"] == 150
        assert intake["acv"] == 51000

        fin = run_finance_auditor(topo)
        assert fin["overage"] == 9000
        assert fin["target_acv"] == 39750

        sec = run_secops_reviewer(topo)
        assert len(sec["findings"]) == 3

        prep = run_legal_redliner_prepare(topo, tmp_path)
        assert prep["media_id"].startswith("mxc://")
        assert Path(prep["diff_path"]).exists()

        # Internal walk-away price must never leak into #acme-vendor-intake before or after approval
        intake_bodies = "\n".join(m["body"] for m in topo.state.rooms[topo.vendor_intake_id].messages)
        assert "42,000" not in intake_bodies
        assert "280" not in intake_bodies

        cfo_msg = topo.state.post_room_message(
            room_id=topo.war_room_id,
            sender_id=topo.cfo_user_id,
            body="Approved. Send counter-offer.",
        )
        res = verify_and_dispatch_counter_offer(topo, cfo_msg)
        assert res["verified_owner"] == topo.cfo_user_id
        assert "$39,750 ACV" in res["dispatched_message"]["body"]
    finally:
        topo.server.stop()


def test_switch_trust_boundaries_and_heartbeat_ttl() -> None:
    topo = provision_from_template(TEMPLATE, company="acme")
    try:
        proofs = prove_switch_central(topo)
        assert len(proofs) == 4

        # Verify 6s heartbeat TTL releases an exclusive role lease when a connection goes stale
        legal = topo.clients["legal_redliner"]
        legal.op(
            "assume_role",
            room_id=topo.war_room_id,
            role_name="signatory-gate",
            connection_id=legal.connection_id,
        )
        topo.state.expire_stale_connections(now=time.time() + 10.0)
        roles = topo.clients["intake_concierge"].op("list_roles", room_id=topo.war_room_id)["roles"]
        gate = next(r for r in roles if r["name"] == "signatory-gate")
        assert gate["holders"] == []
    finally:
        topo.server.stop()
