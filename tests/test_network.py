import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ContactNetworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id, onset_date, confirm=True):
        case = self.service.create(self.admin, "case", {
            "person_id": person_id,
            "onset_date": onset_date,
            "location": "District-A",
            "symptoms": ["fever"],
        })
        if confirm:
            self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
            self.service.transition(self.admin, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"})
            case = self.service.get(case["id"])
        return case

    def _register(self, case_id, source_id, exposure_date, location="Office"):
        return self.service.transition(self.admin, case_id, "register_exposure", {
            "source_case_id": source_id,
            "exposure_date": exposure_date,
            "exposure_location": location,
        })

    def _contact(self, case_id, person_id):
        return self.service.create(self.admin, "contact", {
            "case_id": case_id,
            "person_id": person_id,
            "exposure_start": "2026-03-02",
        })

    def test_chain_generations_and_lineage(self):
        case_a = self._case("P-A", "2026-03-01")
        case_b = self._case("P-B", "2026-03-05")
        case_c = self._case("P-C", "2026-03-09")
        self._register(case_b["id"], case_a["id"], "2026-03-02")
        self._register(case_c["id"], case_b["id"], "2026-03-06")

        chains = self.service.chains()
        self.assertEqual(len(chains), 1)
        chain = chains[0]
        self.assertEqual(chain["chain_id"], case_a["id"])
        generations = {member["case_id"]: member["generation"] for member in chain["members"]}
        self.assertEqual(generations, {case_a["id"]: 1, case_b["id"]: 2, case_c["id"]: 3})

        view = self.service.chain_for(case_b["id"])
        self.assertEqual([m["case_id"] for m in view["upstream"]], [case_a["id"]])
        self.assertEqual([m["case_id"] for m in view["downstream"]], [case_c["id"]])
        # 登记暴露不改变病例状态
        self.assertEqual(self.service.get(case_b["id"])["status"], "confirmed")

    def test_duplicate_exposure_keeps_most_recent(self):
        case_a = self._case("P-A", "2026-03-01")
        case_b = self._case("P-B", "2026-03-10")
        self._register(case_b["id"], case_a["id"], "2026-03-02")
        updated = self._register(case_b["id"], case_a["id"], "2026-03-05", location="Canteen")
        exposures = updated["data"]["exposures"]
        self.assertEqual(len(exposures), 1)
        self.assertEqual(exposures[0]["exposure_date"], "2026-03-05")
        self.assertEqual(exposures[0]["exposure_location"], "Canteen")
        # 更早的暴露登记不会覆盖已保留的最近一次
        updated = self._register(case_b["id"], case_a["id"], "2026-03-01")
        exposures = updated["data"]["exposures"]
        self.assertEqual(len(exposures), 1)
        self.assertEqual(exposures[0]["exposure_date"], "2026-03-05")

    def test_source_status_must_be_confirmed(self):
        source = self._case("P-S", "2026-03-01", confirm=False)
        case_b = self._case("P-B", "2026-03-05")
        with self.assertRaises(ValidationError) as ctx:
            self._register(case_b["id"], source["id"], "2026-03-02")
        self.assertIn("未建立", str(ctx.exception))

    def test_exposure_after_onset_rejected(self):
        case_a = self._case("P-A", "2026-03-01")
        case_b = self._case("P-B", "2026-03-05")
        with self.assertRaises(ValidationError) as ctx:
            self._register(case_b["id"], case_a["id"], "2026-03-06")
        self.assertIn("未建立", str(ctx.exception))

    def test_case_cannot_be_its_own_source(self):
        case_a = self._case("P-A", "2026-03-01")
        with self.assertRaises(ValidationError) as ctx:
            self._register(case_a["id"], case_a["id"], "2026-02-28")
        self.assertIn("未建立", str(ctx.exception))

    def test_register_requires_confirmed_case(self):
        case_a = self._case("P-A", "2026-03-01")
        reported = self._case("P-R", "2026-03-05", confirm=False)
        with self.assertRaises(InvalidTransition):
            self._register(reported["id"], case_a["id"], "2026-03-02")

    def test_release_contact_keeps_chain_visible(self):
        case_a = self._case("P-A", "2026-03-01")
        case_b = self._case("P-B", "2026-03-05")
        self._register(case_b["id"], case_a["id"], "2026-03-02")
        contact = self._contact(case_a["id"], "P-X")
        self.service.transition(self.admin, contact["id"], "begin_followup", {"followup_start": "2026-03-03", "due_at": "2026-03-17"})
        released = self.service.transition(self.admin, contact["id"], "release", {"outcome": "no symptoms"})
        self.assertEqual(released["status"], "released")

        chain = self.service.chains()[0]
        self.assertEqual(chain["size"], 2)
        entry = [c for c in chain["contacts"] if c["contact_id"] == contact["id"]][0]
        self.assertFalse(entry["pending"])
        self.assertEqual(chain["pending_contacts"], [])
        # 解除未感染的接触者后，上下游关系仍可查
        view = self.service.chain_for(case_b["id"])
        self.assertEqual([m["case_id"] for m in view["upstream"]], [case_a["id"]])

    def test_pending_contacts_listed(self):
        case_a = self._case("P-A", "2026-03-01")
        contact = self._contact(case_a["id"], "P-Y")
        chain = self.service.chains()[0]
        self.assertEqual([c["contact_id"] for c in chain["pending_contacts"]], [contact["id"]])

    def test_infected_contact_cannot_be_released(self):
        case_a = self._case("P-A", "2026-03-01")
        contact = self._contact(case_a["id"], "P-Z")
        self.service.transition(self.admin, contact["id"], "begin_followup", {"followup_start": "2026-03-03", "due_at": "2026-03-17"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, contact["id"], "release", {"outcome": "confirmed infected"})


if __name__ == "__main__":
    unittest.main()
