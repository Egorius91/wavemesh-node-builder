#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
sys.path.insert(0, str(AGENT_DIR))

import node_recovery as recovery  # noqa: E402

CREDENTIAL_ID = "credential_12345678"
CERTIFICATE = "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n"
CHAIN = "-----BEGIN CERTIFICATE-----\nCHAIN\n-----END CERTIFICATE-----\n"


class NodeRecoveryCertificateExpiredReasonTests(unittest.TestCase):
    def client(self) -> recovery.RecoveryClient:
        return object.__new__(recovery.RecoveryClient)

    def delivery(self, reason: str | None) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        return {
            "credential_id": CREDENTIAL_ID,
            "certificate": CERTIFICATE,
            "chain": CHAIN,
            "lifecycle_status": "PENDING_ACKNOWLEDGEMENT",
            "recovery_reason": reason,
            "not_before": recovery.format_timestamp(now - timedelta(minutes=1)),
            "expires_at": recovery.format_timestamp(now + timedelta(hours=24)),
            "delivery_expires_at": recovery.format_timestamp(now + timedelta(minutes=10)),
            "previous_valid_until": None,
            "already_processed": False,
        }

    def test_certificate_expired_reason_is_accepted_without_changing_delivery_shape(self) -> None:
        validated = self.client()._validate_delivery(
            self.delivery("CERTIFICATE_EXPIRED")
        )

        self.assertEqual(validated["credential_id"], CREDENTIAL_ID)
        self.assertEqual(validated["certificate"], CERTIFICATE)
        self.assertEqual(validated["chain"], CHAIN)
        self.assertNotIn("recovery_reason", validated)

    def test_existing_recovery_reasons_remain_accepted(self) -> None:
        for reason in ("LOST_KEY", "COMPROMISED_KEY"):
            with self.subTest(reason=reason):
                validated = self.client()._validate_delivery(self.delivery(reason))
                self.assertEqual(validated["credential_id"], CREDENTIAL_ID)

    def test_unknown_recovery_reason_remains_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "Recovery reason metadata is invalid",
        ):
            self.client()._validate_delivery(self.delivery("UNRECOGNIZED_REASON"))

    def test_absent_recovery_reason_remains_backward_compatible(self) -> None:
        delivery = self.delivery(None)
        delivery.pop("recovery_reason")
        validated = self.client()._validate_delivery(delivery)
        self.assertEqual(validated["credential_id"], CREDENTIAL_ID)


if __name__ == "__main__":
    unittest.main()
