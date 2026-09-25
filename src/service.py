from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import EXPOSURE_KIND, RuleEngine, build_network


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def register_exposure(self, actor, payload, idempotency_key=None):
        """病例确认时登记一次来源暴露。

        关系建立失败也会落库（status=not_established 并附原因）；同一暴露人对同一
        来源病例的多次暴露只保留最近一次。
        """
        data = dict(payload or {})
        if idempotency_key:
            existing_id = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing_id:
                existing = self.repository.get_entity(existing_id)
                if existing:
                    return existing

        target = self.repository.get_entity(str(data.get("case_id") or ""))
        source = self.repository.get_entity(str(data.get("source_case_id") or ""))
        status, reasons = self.rules.validate_exposure(actor, data, target, source)

        person_id = data.get("person_id")
        if not person_id and target is not None:
            person_id = target["data"].get("person_id")
        exposure_payload = {
            "case_id": data.get("case_id"),
            "source_case_id": data.get("source_case_id"),
            "person_id": person_id,
            "exposure_date": data.get("exposure_date"),
            "location": data.get("location"),
        }
        if reasons:
            exposure_payload["reasons"] = reasons

        previous = self._find_latest_exposure(person_id, data.get("source_case_id"))
        if previous is not None:
            try:
                from .rules import _date_ordinal

                previous_date = _date_ordinal(previous["data"].get("exposure_date"))
                new_date = _date_ordinal(data.get("exposure_date"))
            except (TypeError, ValueError):
                raise ValidationError("exposure_date must be an ISO date")
            if new_date < previous_date:
                # 已有更近的暴露登记，保留最近一次，本次不产生新关系。
                previous["data"]["ignored_register"] = {
                    "exposure_date": data.get("exposure_date"),
                    "location": data.get("location"),
                    "reasons": reasons,
                }
                updated = self.repository.update_entity(
                    previous["id"], previous["version"], previous["status"], previous["data"]
                )
                self.audit.record(
                    previous["id"], actor, "exposure_ignored",
                    previous["status"], previous["status"],
                    {"kept": previous["data"].get("exposure_date"), "ignored": data.get("exposure_date")},
                )
                if idempotency_key:
                    self.repository.save_idempotency(actor.user_id, idempotency_key, previous["id"])
                return updated
            exposure_id = previous["id"]
            updated = self.repository.update_entity(exposure_id, previous["version"], status, exposure_payload)
            self.audit.record(
                exposure_id, actor, "exposure_update", previous["status"], status,
                {"reasons": reasons},
            )
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, exposure_id)
            return updated

        exposure_id = str(data.pop("id", "") or uuid4())
        if self.repository.get_entity(exposure_id):
            raise ConflictError("entity already exists: " + exposure_id)
        entity = self.repository.create_entity(
            exposure_id, EXPOSURE_KIND, status, exposure_payload, actor.user_id
        )
        self.audit.record(
            exposure_id, actor, "exposure_register", None, status,
            {"kind": EXPOSURE_KIND, "reasons": reasons},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, exposure_id)
        return entity

    def _find_latest_exposure(self, person_id, source_case_id):
        if not person_id or not source_case_id:
            return None
        candidates = [
            entity
            for entity in self.repository.list_entities(kind=EXPOSURE_KIND)
            if entity["data"].get("person_id") == person_id
            and entity["data"].get("source_case_id") == source_case_id
        ]
        if not candidates:
            return None
        from .rules import _date_ordinal

        def order(entity):
            try:
                return _date_ordinal(entity["data"].get("exposure_date"))
            except (TypeError, ValueError):
                return float("-inf")

        return max(candidates, key=order)

    def network(self):
        cases = self.repository.list_entities(kind="case")
        contacts = self.repository.list_entities(kind="contact")
        exposures = self.repository.list_entities(kind=EXPOSURE_KIND)
        return build_network(cases, contacts, exposures)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
