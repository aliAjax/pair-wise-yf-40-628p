import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LabReportTest(unittest.TestCase):
    """实验室报告驱动的停运/释放规则。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab-1", "lab")
        self.quarantine = Actor("q-1", "quarantine")
        self._build_chain()

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, actor, kind, data):
        return self.service.create(actor, kind, data)

    def _build_chain(self):
        # 批次：c1 -> c2 -> c3；c4 是独立批次，也流经共享场地
        self.c1 = self._create(self.admin, "consignment",
                               {"code": "C-1", "origin": "Port-A", "destination": "Nursery-A"})["id"]
        self.c2 = self._create(self.admin, "consignment",
                               {"code": "C-2", "origin": "Nursery-A", "destination": "Nursery-B",
                                "parent_id": self.c1})["id"]
        self.c3 = self._create(self.admin, "consignment",
                               {"code": "C-3", "origin": "Nursery-B", "destination": "Farm-C",
                                "parent_id": self.c2})["id"]
        self.c4 = self._create(self.admin, "consignment",
                               {"code": "C-4", "origin": "Port-A", "destination": "Shared-Hub"})["id"]
        # 场地：f1 只接触 c1；f2 接触 c2；f3 接触下游 c3；f4 是共享接触链
        self.f1 = self._create(self.quarantine, "facility",
                               {"name": "Nursery-A", "address": "County 1",
                                "consignment_id": self.c1})["id"]
        self.f2 = self._create(self.quarantine, "facility",
                               {"name": "Nursery-B", "address": "County 2",
                                "consignment_ids": [self.c2]})["id"]
        self.f3 = self._create(self.quarantine, "facility",
                               {"name": "Farm-C", "address": "County 3",
                                "consignment_ids": [self.c3]})["id"]
        self.f4 = self._create(self.quarantine, "facility",
                               {"name": "Shared-Hub", "address": "County 9",
                                "consignment_ids": [self.c3, self.c4]})["id"]
        # 批次进入已检验状态
        for cid in (self.c1, self.c2, self.c3, self.c4):
            self.service.transition(
                self.admin, cid, "inspect",
                {"inspector": "I-1", "inspection_result": "suspected"},
            )

    def _status(self, entity_id):
        return self.service.get(entity_id)["status"]

    def _stoppages(self, entity_id):
        return self.service.get(entity_id)["data"].get("stoppages", [])

    def _positive(self, cid, report_id, date):
        return self.service.transition(
            self.lab, cid, "lab_report",
            {"report_id": report_id, "result": "positive", "result_date": date},
        )

    # ------------------------------------------------------------------
    def test_positive_report_stops_batch_and_full_contact_chain(self):
        result = self._positive(self.c1, "R-1", "2026-09-01")
        # 阳性批次停运
        self.assertEqual(result["status"], "lab_positive")
        # 下游苗圃和整条接触链一起停运（含独立批次共享的场地）
        for fid in (self.f1, self.f2, self.f3, self.f4):
            self.assertEqual(self._status(fid), "stopped")
        # 接触链快照覆盖全部下游批次与场地
        chain = result["data"]["contact_chain"]
        self.assertEqual(chain["consignment_ids"], [self.c1, self.c2, self.c3])
        self.assertEqual(sorted(chain["facility_ids"]),
                         sorted([self.f1, self.f2, self.f3, self.f4]))
        # 停运归因和首次阳性结论
        self.assertEqual(result["data"]["lab_result"], "positive")
        self.assertEqual(result["data"]["lab_report_id"], "R-1")
        self.assertEqual(self._stoppages(self.f1)[0]["consignment_id"], self.c1)

    def test_duplicate_report_keeps_first_conclusion_and_has_no_effect(self):
        self._positive(self.c1, "R-1", "2026-09-01")
        version_before = self.service.get(self.c1)["version"]
        facility_version_before = self.service.get(self.f1)["version"]
        # 重复报告（即使结论不同）只保留首次结论
        result = self.service.transition(
            self.lab, self.c1, "lab_report",
            {"report_id": "R-1", "result": "negative", "result_date": "2026-09-05"},
        )
        self.assertEqual(result["status"], "lab_positive")
        self.assertEqual(result["version"], version_before)
        self.assertEqual(result["data"]["lab_result"], "positive")
        self.assertEqual(len(result["data"]["lab_reports"]), 1)
        # 级联对象不动
        self.assertEqual(self.service.get(self.f1)["version"], facility_version_before)
        self.assertEqual(self._status(self.f1), "stopped")

    def test_negative_recheck_releases_only_this_batch_stoppage(self):
        # c1 阳性 -> f1..f4 全部停运
        self._positive(self.c1, "R-1", "2026-09-01")
        # 另一条独立批次 c4 也阳性，它只接触共享场地 f4
        self._positive(self.c4, "R-4", "2026-09-02")
        self.assertEqual(
            sorted(item["consignment_id"] for item in self._stoppages(self.f4)),
            sorted([self.c1, self.c4]),
        )
        # c1 的阴性复核报告
        result = self.service.transition(
            self.lab, self.c1, "lab_report",
            {"report_id": "R-2", "result": "negative", "result_date": "2026-09-08"},
        )
        self.assertEqual(result["status"], "lab_negative")
        # 首次结论字段保持首次结论
        self.assertEqual(result["data"]["lab_result"], "positive")
        self.assertEqual(result["data"]["lab_recheck"]["result"], "negative")
        # 只释放 c1 造成的停运：f1/f2/f3 恢复
        self.assertEqual(self._status(self.f1), "registered")
        self.assertEqual(self._status(self.f2), "registered")
        self.assertEqual(self._status(self.f3), "registered")
        # f4 仍有 c4 的归因，保持停运
        self.assertEqual(self._status(self.f4), "stopped")
        self.assertEqual(
            [item["consignment_id"] for item in self._stoppages(self.f4)],
            [self.c4],
        )

    def test_disinfect_requires_certificate_after_positive(self):
        self._positive(self.c1, "R-1", "2026-09-01")
        # 证书日期不晚于阳性结果
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.quarantine, self.c1, "disinfect",
                {"certificate_id": "T-1", "certificate_date": "2026-09-01",
                 "covered_facilities": [self.f1, self.f2, self.f3, self.f4]},
            )
        self.assertIn("later than positive result date", str(ctx.exception))
        # 状态不变
        self.assertEqual(self._status(self.c1), "lab_positive")

    def test_disinfect_missing_site_keeps_stoppage_and_lists_gaps(self):
        self._positive(self.c1, "R-1", "2026-09-01")
        # 空覆盖：整条链都缺，同样保留停运并说明缺项
        with self.assertRaises(ValidationError) as ctx_empty:
            self.service.transition(
                self.quarantine, self.c1, "disinfect",
                {"certificate_id": "T-0", "certificate_date": "2026-09-03",
                 "covered_facilities": []},
            )
        self.assertIn(self.f1, str(ctx_empty.exception))
        # 缺 f4：保留停运并说明缺项
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.quarantine, self.c1, "disinfect",
                {"certificate_id": "T-1", "certificate_date": "2026-09-03",
                 "covered_facilities": [self.f1, self.f2, self.f3]},
            )
        message = str(ctx.exception)
        self.assertIn(self.f4, message)
        self.assertIn("Shared-Hub", message)
        # 全部场地保持停运
        for fid in (self.f1, self.f2, self.f3, self.f4):
            self.assertEqual(self._status(fid), "stopped")
        self.assertEqual(self._status(self.c1), "lab_positive")

    def test_full_coverage_disinfection_releases_chain(self):
        self._positive(self.c1, "R-1", "2026-09-01")
        result = self.service.transition(
            self.quarantine, self.c1, "disinfect",
            {"certificate_id": "T-1", "certificate_date": "2026-09-03",
             "covered_facilities": [self.f4, self.f2, self.f1, self.f3]},
        )
        self.assertEqual(result["status"], "released")
        self.assertEqual(result["data"]["disinfection"]["certificate_id"], "T-1")
        for fid in (self.f1, self.f2, self.f3, self.f4):
            self.assertEqual(self._status(fid), "registered")
            self.assertEqual(self._stoppages(fid), [])

    def test_lab_report_requires_lab_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.quarantine, self.c1, "lab_report",
                {"report_id": "R-1", "result": "positive", "result_date": "2026-09-01"},
            )

    def test_negative_first_report_does_not_release_wrongly(self):
        # 阴性报告只释放该批次造成的停运；本批次从未阳性时不得误放
        result = self.service.transition(
            self.lab, self.c1, "lab_report",
            {"report_id": "R-9", "result": "negative", "result_date": "2026-09-01"},
        )
        self.assertEqual(result["status"], "lab_negative")
        for fid in (self.f1, self.f2, self.f3, self.f4):
            self.assertEqual(self._status(fid), "registered")

    def test_cannot_disinfect_without_positive_report(self):
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.quarantine, self.c1, "disinfect",
                {"certificate_id": "T-1", "certificate_date": "2026-09-03",
                 "covered_facilities": [self.f1]},
            )


if __name__ == "__main__":
    unittest.main()
