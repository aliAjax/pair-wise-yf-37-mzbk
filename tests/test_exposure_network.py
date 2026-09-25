import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def confirmed_case(service, person_id, onset_date, location="District-A", case_id=None):
    data = {
        "person_id": person_id,
        "onset_date": onset_date,
        "location": location,
        "symptoms": ["fever"],
    }
    if case_id:
        data["id"] = case_id
    case = service.create(Actor("admin", "admin"), "case", data)
    service.transition(Actor("admin", "admin"), case["id"], "triage", {"clinician": "C-1"})
    case = service.transition(
        Actor("lab-1", "lab"), case["id"], "lab_positive",
        {"lab_id": "L-1", "result": "positive"},
    )
    return case


class ExposureNetworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.investigator = Actor("inv-1", "investigator")

    def tearDown(self):
        self.tmp.cleanup()

    def _exposure(self, case_id, source_id, exposure_date, location="cafe", person_id=None, actor=None):
        payload = {
            "case_id": case_id,
            "source_case_id": source_id,
            "exposure_date": exposure_date,
            "location": location,
        }
        if person_id:
            payload["person_id"] = person_id
        return self.service.register_exposure(actor or self.investigator, payload)

    def test_chain_and_generations(self):
        root = confirmed_case(self.service, "P-1", "2026-03-01", case_id="case-1")
        second = confirmed_case(self.service, "P-2", "2026-03-05", case_id="case-2")
        third = confirmed_case(self.service, "P-3", "2026-03-09", case_id="case-3")
        self._exposure(second["id"], root["id"], "2026-03-03")
        self._exposure(third["id"], second["id"], "2026-03-07")

        network = self.service.network()
        self.assertEqual(len(network["chains"]), 1)
        chain = network["chains"][0]
        by_id = {member["case_id"]: member for member in chain["members"]}
        self.assertEqual(by_id[root["id"]]["generation"], 0)
        self.assertEqual(by_id[second["id"]]["generation"], 1)
        self.assertEqual(by_id[third["id"]]["generation"], 2)
        self.assertEqual(chain["generations"], 2)
        self.assertEqual(by_id[second["id"]]["upstream_case_ids"], [root["id"]])
        self.assertEqual(by_id[root["id"]]["downstream_case_ids"], [second["id"]])

    def test_repeated_exposure_same_source_keeps_latest(self):
        root = confirmed_case(self.service, "P-1", "2026-03-01", case_id="case-1")
        second = confirmed_case(self.service, "P-2", "2026-03-05", case_id="case-2")
        first = self._exposure(second["id"], root["id"], "2026-03-02", location="market", person_id="P-2")
        second_reg = self._exposure(second["id"], root["id"], "2026-03-04", location="clinic", person_id="P-2")
        self.assertEqual(first["id"], second_reg["id"])
        self.assertEqual(second_reg["data"]["exposure_date"], "2026-03-04")
        self.assertEqual(second_reg["data"]["location"], "clinic")
        self.assertEqual(second_reg["status"], "established")

        exposures = self.service.list("exposures")
        self.assertEqual(len(exposures), 1)

        # 更早的登记不会覆盖最近一次。
        older = self._exposure(second["id"], root["id"], "2026-02-28", location="old", person_id="P-2")
        self.assertEqual(older["id"], first["id"])
        self.assertEqual(older["data"]["exposure_date"], "2026-03-04")
        self.assertEqual(older["data"]["ignored_register"]["exposure_date"], "2026-02-28")
        network = self.service.network()
        self.assertEqual(len(network["chains"][0]["members"]), 2)

    def test_source_not_confirmed_relation_not_established(self):
        root = self.service.create(
            Actor("admin", "admin"), "case",
            {"id": "case-a", "person_id": "P-1", "onset_date": "2026-03-01", "location": "A", "symptoms": ["fever"]},
        )
        self.service.transition(Actor("admin", "admin"), root["id"], "triage", {"clinician": "C-1"})
        second = confirmed_case(self.service, "P-2", "2026-03-05", case_id="case-2")

        exposure = self._exposure(second["id"], root["id"], "2026-03-03", person_id="P-2")
        self.assertEqual(exposure["status"], "not_established")
        self.assertTrue(any("来源病例" in reason for reason in exposure["data"]["reasons"]))

        network = self.service.network()
        # 未建立的关系不产生链边；传播链只含确诊病例。
        self.assertEqual(len(network["chains"]), 1)
        chain_members = [member["case_id"] for member in network["chains"][0]["members"]]
        self.assertEqual(sorted(chain_members), sorted([second["id"]]))
        rejected = network["rejected_exposures"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["source_case_id"], root["id"])
        self.assertTrue(rejected[0]["reasons"])

    def test_exposure_after_onset_not_established(self):
        root = confirmed_case(self.service, "P-1", "2026-03-01", case_id="case-1")
        second = confirmed_case(self.service, "P-2", "2026-03-05", case_id="case-2")
        exposure = self._exposure(second["id"], root["id"], "2026-03-06", person_id="P-2")
        self.assertEqual(exposure["status"], "not_established")
        self.assertTrue(any("晚于" in reason for reason in exposure["data"]["reasons"]))
        network = self.service.network()
        self.assertEqual(len(network["chains"]), 2)
        self.assertTrue(network["rejected_exposures"])

    def test_released_contact_keeps_relations_and_hidden_from_pending(self):
        root = confirmed_case(self.service, "P-1", "2026-03-01", case_id="case-1")
        second = confirmed_case(self.service, "P-2", "2026-03-05", case_id="case-2")
        self._exposure(second["id"], root["id"], "2026-03-03")

        contact = self.service.create(
            Actor("admin", "admin"), "contact",
            {"case_id": root["id"], "person_id": "P-9", "exposure_start": "2026-03-02"},
        )
        network = self.service.network()
        self.assertEqual(len(network["pending_contacts"]), 1)

        released = self.service.transition(
            self.investigator, contact["id"], "release", {"outcome": "未感染"}
        )
        self.assertEqual(released["status"], "released")

        network = self.service.network()
        self.assertEqual(network["pending_contacts"], [])
        chain = network["chains"][0]
        root_member = next(m for m in chain["members"] if m["case_id"] == root["id"])
        self.assertEqual(len(root_member["released_contacts"]), 1)
        self.assertEqual(root_member["released_contacts"][0]["person_id"], "P-9")
        self.assertEqual(root_member["released_contacts"][0]["outcome"], "未感染")
        # 病例之间的传播链不受解除影响。
        self.assertEqual(len(chain["members"]), 2)

    def test_release_requires_investigator_role(self):
        root = confirmed_case(self.service, "P-1", "2026-03-01", case_id="case-1")
        contact = self.service.create(
            Actor("admin", "admin"), "contact",
            {"case_id": root["id"], "person_id": "P-9", "exposure_start": "2026-03-02"},
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer-1", "viewer"), contact["id"], "release", {"outcome": "未感染"}
            )

    def test_multiple_sources_keep_longest_generation(self):
        c1 = confirmed_case(self.service, "P-1", "2026-03-01", case_id="case-1")
        c2 = confirmed_case(self.service, "P-2", "2026-03-04", case_id="case-2")
        c3 = confirmed_case(self.service, "P-3", "2026-03-07", case_id="case-3")
        self._exposure(c2["id"], c1["id"], "2026-03-02")
        self._exposure(c3["id"], c1["id"], "2026-03-03")
        self._exposure(c3["id"], c2["id"], "2026-03-05")
        chain = self.service.network()["chains"][0]
        by_id = {member["case_id"]: member for member in chain["members"]}
        self.assertEqual(by_id[c3["id"]]["generation"], 2)
        self.assertEqual(sorted(by_id[c3["id"]]["upstream_case_ids"]), sorted([c1["id"], c2["id"]]))

    def test_register_exposure_requires_investigator(self):
        root = confirmed_case(self.service, "P-1", "2026-03-01", case_id="case-1")
        second = confirmed_case(self.service, "P-2", "2026-03-05", case_id="case-2")
        with self.assertRaises(PermissionDenied):
            self._exposure(second["id"], root["id"], "2026-03-03", actor=Actor("v", "viewer"))


if __name__ == "__main__":
    unittest.main()
