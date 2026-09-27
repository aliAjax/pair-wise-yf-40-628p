from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, ValidationError
from .rules import (
    RuleEngine,
    TERMINAL_STATUSES,
    chain_facilities,
    trace_downstream,
    validate_disinfection,
    validate_lab_report,
)


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
        if entity["kind"] == "consignment" and action == "lab_report":
            return self._lab_report(actor, entity, dict(data or {}), expected_version)
        if entity["kind"] == "consignment" and action == "disinfect":
            return self._disinfect(actor, entity, dict(data or {}), expected_version)
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

    @staticmethod
    def _check_version(entity, expected_version):
        if expected_version is not None and int(expected_version) != entity["version"]:
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, entity["version"])
            )

    @staticmethod
    def _hold(entity, cause, held_status):
        data = dict(entity["data"])
        holds = list(data.get("holds") or [])
        if cause not in holds:
            if not holds:
                data["status_before_hold"] = entity["status"]
            holds.append(cause)
            data["holds"] = holds
        return (held_status if holds else entity["status"]), data

    @staticmethod
    def _release(entity, cause):
        data = dict(entity["data"])
        holds = [item for item in (data.get("holds") or []) if item != cause]
        status = entity["status"]
        if holds:
            data["holds"] = holds
        else:
            data.pop("holds", None)
            status = data.pop("status_before_hold", entity["status"])
        return status, data

    def _lab_report(self, actor, entity, data, expected_version):
        self.rules.ensure_action_role(actor, "lab_report")
        validate_lab_report(data)
        if entity["status"] not in ("inspected", "quarantined"):
            raise InvalidTransition(
                "cannot lab_report from status %s" % entity["status"]
            )
        self._check_version(entity, expected_version)
        processed = list(entity["data"].get("processed_report_ids") or [])
        if data["report_id"] in processed:
            return entity
        conclusion = {
            "report_id": data["report_id"],
            "result": data["result"],
            "reported_at": data["reported_at"],
            "received_by": actor.user_id,
        }
        if data["result"] == "positive":
            return self._apply_positive_report(actor, entity, conclusion, processed)
        return self._apply_negative_report(actor, entity, conclusion, processed)

    def _apply_positive_report(self, actor, entity, conclusion, processed):
        origin = entity["id"]
        consignments = self.repository.list_entities(kind="consignment")
        links = [
            {"id": item["id"], "parent_id": item["data"].get("parent_id")}
            for item in consignments
        ]
        chain = trace_downstream(links, origin)
        by_id = {item["id"]: item for item in consignments}
        facilities = chain_facilities(
            self.repository.list_entities(kind="facility"), chain
        )
        updates = []
        audits = []
        affected = []
        for consignment_id in chain:
            current = by_id.get(consignment_id)
            if not current or current["status"] in TERMINAL_STATUSES:
                continue
            status, new_data = self._hold(current, origin, "quarantined")
            if consignment_id == origin:
                new_data["lab_conclusion"] = conclusion
                new_data["processed_report_ids"] = processed + [conclusion["report_id"]]
            elif status == current["status"] and new_data == current["data"]:
                continue
            updates.append((consignment_id, current["version"], status, new_data))
            audits.append((consignment_id, "hold", current["status"], status))
            affected.append(consignment_id)
        for facility in facilities:
            status, new_data = self._hold(facility, origin, "suspended")
            if status == facility["status"] and new_data == facility["data"]:
                continue
            updates.append((facility["id"], facility["version"], status, new_data))
            audits.append((facility["id"], "hold", facility["status"], status))
            affected.append(facility["id"])
        self.repository.update_entities(updates)
        for entity_id, action, from_status, to_status in audits:
            if entity_id == origin:
                action = "lab_report"
                detail = {"conclusion": conclusion, "affected": affected}
            else:
                detail = {"cause": origin, "report_id": conclusion["report_id"]}
            self.audit.record(entity_id, actor, action, from_status, to_status, detail)
        return self.get(origin)

    def _apply_negative_report(self, actor, entity, conclusion, processed):
        origin = entity["id"]
        updates = []
        audits = []
        released = []
        for kind in ("consignment", "facility"):
            for item in self.repository.list_entities(kind=kind):
                if origin not in (item["data"].get("holds") or []):
                    continue
                status, new_data = self._release(item, origin)
                if item["id"] == origin:
                    new_data["lab_conclusion"] = conclusion
                    new_data["processed_report_ids"] = processed + [conclusion["report_id"]]
                updates.append((item["id"], item["version"], status, new_data))
                audits.append((item["id"], "release_hold", item["status"], status))
                released.append(item["id"])
        if not any(item[0] == origin for item in updates):
            new_data = dict(entity["data"])
            new_data["lab_conclusion"] = conclusion
            new_data["processed_report_ids"] = processed + [conclusion["report_id"]]
            updates.append((origin, entity["version"], entity["status"], new_data))
            audits.append((origin, "lab_report", entity["status"], entity["status"]))
        self.repository.update_entities(updates)
        for entity_id, action, from_status, to_status in audits:
            if entity_id == origin:
                action = "lab_report"
                detail = {"conclusion": conclusion, "released": released}
            else:
                detail = {"cause": origin, "report_id": conclusion["report_id"]}
            self.audit.record(entity_id, actor, action, from_status, to_status, detail)
        return self.get(origin)

    def _disinfect(self, actor, entity, data, expected_version):
        self.rules.ensure_action_role(actor, "disinfect")
        conclusion = entity["data"].get("lab_conclusion") or {}
        if conclusion.get("result") != "positive":
            raise ValidationError("disinfection requires an active positive lab conclusion")
        self._check_version(entity, expected_version)
        origin = entity["id"]
        held_facilities = [
            item
            for item in self.repository.list_entities(kind="facility")
            if origin in (item["data"].get("holds") or [])
        ]
        validate_disinfection(
            data,
            conclusion.get("reported_at"),
            [item["id"] for item in held_facilities],
        )
        updates = []
        audits = []
        for facility in held_facilities:
            status, new_data = self._release(facility, origin)
            updates.append((facility["id"], facility["version"], status, new_data))
            audits.append((facility["id"], facility["status"], status))
        certificate = {
            "certificate_date": data["certificate_date"],
            "covered_facility_ids": list(data["covered_facility_ids"]),
            "recorded_by": actor.user_id,
        }
        origin_data = dict(entity["data"])
        origin_data["disinfection"] = certificate
        updates.append((origin, entity["version"], entity["status"], origin_data))
        self.repository.update_entities(updates)
        self.audit.record(
            origin,
            actor,
            "disinfect",
            entity["status"],
            entity["status"],
            {"certificate": certificate, "released": [item["id"] for item in held_facilities]},
        )
        for facility_id, from_status, to_status in audits:
            self.audit.record(
                facility_id,
                actor,
                "release_hold",
                from_status,
                to_status,
                {"cause": origin, "via": "disinfection"},
            )
        return self.get(origin)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
