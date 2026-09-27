import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LabReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab-1", "lab")

    def tearDown(self):
        self.tmp.cleanup()

    def _consignment(self, code, parent_id=None):
        data = {"code": code, "origin": "A-" + code, "destination": "B-" + code}
        if parent_id:
            data["parent_id"] = parent_id
        entity = self.service.create(self.admin, "consignment", data)
        self.service.transition(
            self.admin,
            entity["id"],
            "inspect",
            {"inspector": "I-1", "inspection_result": "suspected"},
        )
        return entity["id"]

    def _facility(self, name, consignment_ids=None):
        entity = self.service.create(
            self.admin, "facility", {"name": name, "address": "addr"}
        )
        if consignment_ids:
            self.service.transition(
                self.admin, entity["id"], "trace", {"consignment_ids": consignment_ids}
            )
        return entity["id"]

    def _report(self, consignment_id, report_id, result, reported_at):
        return self.service.transition(
            self.lab,
            consignment_id,
            "lab_report",
            {"report_id": report_id, "result": result, "reported_at": reported_at},
        )

    def test_positive_report_holds_whole_chain(self):
        parent = self._consignment("C-1")
        child = self._consignment("C-2", parent_id=parent)
        grandchild = self._consignment("C-3", parent_id=child)
        nursery = self._facility("Nursery-1", [child])
        shared = self._facility("Nursery-2", [grandchild])
        unrelated = self._facility("Nursery-3")

        updated = self._report(parent, "R-1", "positive", "2026-09-20")

        self.assertEqual(updated["status"], "quarantined")
        self.assertEqual(updated["data"]["lab_conclusion"]["result"], "positive")
        for entity_id, status in (
            (child, "quarantined"),
            (grandchild, "quarantined"),
            (nursery, "suspended"),
            (shared, "suspended"),
        ):
            entity = self.service.get(entity_id)
            self.assertEqual(entity["status"], status)
            self.assertIn(parent, entity["data"]["holds"])
        self.assertEqual(self.service.get(unrelated)["status"], "registered")

    def test_duplicate_report_keeps_first_conclusion(self):
        parent = self._consignment("C-1")
        first = self._report(parent, "R-1", "positive", "2026-09-20")

        again = self._report(parent, "R-1", "negative", "2026-09-21")

        self.assertEqual(again["version"], first["version"])
        self.assertEqual(again["status"], "quarantined")
        self.assertEqual(again["data"]["lab_conclusion"]["result"], "positive")
        self.assertEqual(again["data"]["processed_report_ids"], ["R-1"])

    def test_negative_report_releases_only_own_holds(self):
        first = self._consignment("C-1")
        second = self._consignment("C-2")
        shared = self._facility("Nursery", [first, second])
        self._report(first, "R-1", "positive", "2026-09-20")
        self._report(second, "R-2", "positive", "2026-09-21")
        self.assertEqual(
            sorted(self.service.get(shared)["data"]["holds"]), sorted([first, second])
        )

        updated = self._report(first, "R-3", "negative", "2026-09-22")

        self.assertEqual(updated["status"], "inspected")
        facility = self.service.get(shared)
        self.assertEqual(facility["status"], "suspended")
        self.assertEqual(facility["data"]["holds"], [second])

        self._report(second, "R-4", "negative", "2026-09-23")

        facility = self.service.get(shared)
        self.assertEqual(facility["status"], "traced")
        self.assertNotIn("holds", facility["data"])

    def test_disinfection_requires_later_date_and_full_coverage(self):
        parent = self._consignment("C-1")
        child = self._consignment("C-2", parent_id=parent)
        site_a = self._facility("Site-A", [parent])
        site_b = self._facility("Site-B", [child])
        self._report(parent, "R-1", "positive", "2026-09-20")

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                parent,
                "disinfect",
                {"certificate_date": "2026-09-20", "covered_facility_ids": [site_a, site_b]},
            )

        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.admin,
                parent,
                "disinfect",
                {"certificate_date": "2026-09-25", "covered_facility_ids": [site_a]},
            )
        self.assertIn(site_b, str(ctx.exception))
        self.assertEqual(self.service.get(site_a)["status"], "suspended")
        self.assertEqual(self.service.get(site_b)["status"], "suspended")

        updated = self.service.transition(
            self.admin,
            parent,
            "disinfect",
            {"certificate_date": "2026-09-25", "covered_facility_ids": [site_a, site_b]},
        )

        self.assertEqual(updated["status"], "quarantined")
        self.assertEqual(self.service.get(site_a)["status"], "traced")
        self.assertEqual(self.service.get(site_b)["status"], "traced")
        self.assertEqual(self.service.get(child)["status"], "quarantined")

    def test_held_consignment_blocks_recheck_and_report_permissions(self):
        parent = self._consignment("C-1")
        self._report(parent, "R-1", "positive", "2026-09-20")

        with self.assertRaises(InvalidTransition):
            self.service.transition(self.admin, parent, "recheck", {"sample_id": "S-9"})
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                parent,
                "lab_report",
                {"report_id": "R-2", "result": "negative", "reported_at": "2026-09-21"},
            )


if __name__ == "__main__":
    unittest.main()
