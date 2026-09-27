from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


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
        payload = dict(data or {})

        def load_all(kind):
            return self.repository.list_entities(kind=self.rules.normalize_kind(kind))

        plan = self.rules.plan_transition(actor, entity, action, payload, load_all)
        if plan is None:
            expected = int(expected_version) if expected_version is not None else entity["version"]
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, payload, self._lookup
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

        # 重复报告：首次结论保持不变，不产生任何停运/释放效果
        if plan.get("duplicate"):
            self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                entity["status"],
                {"duplicate": True, "report_id": payload.get("report_id")},
            )
            return entity

        expected = int(expected_version) if expected_version is not None else entity["version"]
        primary = plan["primary"]
        effects = plan["effects"]

        # 级联对象以计划加载后为基准，合并 patch；乐观锁由原子更新统一兜底
        effect_entities = {item["id"]: self.repository.get_entity(item["id"]) for item in effects}
        merged = dict(entity["data"])
        merged.update(primary["patch"])
        updates = [(entity_id, expected, primary["status"], merged)]
        for effect in effects:
            target = effect_entities[effect["id"]]
            target_data = dict(target["data"])
            target_data.update(effect["patch"])
            updates.append((
                effect["id"],
                target["version"],
                effect["to_status"],
                target_data,
            ))

        updated_entities = self.repository.update_entities_atomic(updates)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            primary["status"],
            plan["detail"],
        )
        for effect in effects:
            self.audit.record(
                effect["id"],
                actor,
                effect["action"],
                effect["from_status"],
                effect["to_status"],
                {
                    "consignment_id": entity_id,
                    "trigger_action": action,
                    "type": effect["type"],
                },
            )
        return updated_entities[0]

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
