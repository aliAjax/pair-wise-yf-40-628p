from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_release(actor, entity, data, lookup):
    if data.get("pest_found"):
        raise ValidationError("pest-positive consignment cannot be released")
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    return {"released_by": actor.user_id}


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


TERMINAL_STATUSES = ("released", "destroyed")
HOLD_BYPASS_ACTIONS = ("destroy",)
LAB_RESULTS = ("positive", "negative")


def chain_facilities(facilities, consignment_ids):
    wanted = set(consignment_ids)
    return [
        facility
        for facility in facilities
        if wanted.intersection(facility.get("data", {}).get("consignment_ids") or [])
    ]


def _require_date(field, value):
    try:
        return _date_ordinal(value)
    except (TypeError, ValueError):
        raise ValidationError("invalid date for %s: %s" % (field, value))


def validate_lab_report(data):
    for field in ("report_id", "result", "reported_at"):
        if data.get(field) in (None, ""):
            raise ValidationError("missing required field: " + field)
    if data["result"] not in LAB_RESULTS:
        raise ValidationError("result must be one of: " + ", ".join(LAB_RESULTS))
    _require_date("reported_at", data["reported_at"])


def validate_disinfection(data, positive_reported_at, required_facility_ids):
    for field in ("certificate_date", "covered_facility_ids"):
        if data.get(field) in (None, "", []):
            raise ValidationError("missing required field: " + field)
    covered = data.get("covered_facility_ids")
    if not isinstance(covered, list):
        raise ValidationError("covered_facility_ids must be a list")
    if not positive_reported_at:
        raise ValidationError("no positive lab conclusion on record")
    certificate_day = _require_date("certificate_date", data["certificate_date"])
    if certificate_day <= _date_ordinal(positive_reported_at):
        raise ValidationError(
            "certificate_date must be later than the positive result date %s"
            % str(positive_reported_at)[:10]
        )
    missing = [item for item in required_facility_ids if item not in set(covered)]
    if missing:
        raise ValidationError(
            "certificate does not cover the full contact chain; missing facilities: "
            + ", ".join(missing)
        )


CUSTOM_CREATE = {'consignment': _validate_consignment}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected')}, 'facility': {'trace': (('registered',), 'traced')}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('facility', 'trace'): ('consignment_ids',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine'), 'lab_report': ('admin', 'lab'), 'disinfect': ('admin', 'quarantine')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    def ensure_action_role(self, actor, action, kind=None):
        allowed = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        holds = (entity.get("data") or {}).get("holds") or []
        if holds and action not in HOLD_BYPASS_ACTIONS:
            raise InvalidTransition(
                "cannot %s while holds are active from: %s"
                % (action, ", ".join(holds))
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
