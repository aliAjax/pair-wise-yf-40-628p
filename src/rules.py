from datetime import datetime

from .domain import (
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


CUSTOM_CREATE = {'consignment': _validate_consignment}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined', 'lab_positive'), 'destroyed'), 'recheck': (('quarantined', 'lab_positive'), 'inspected')}, 'facility': {'trace': (('registered',), 'traced')}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('consignment', 'lab_report'): ('report_id', 'result', 'result_date'), ('consignment', 'disinfect'): ('certificate_id', 'certificate_date'), ('facility', 'trace'): ('consignment_ids',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine'), 'lab_report': ('admin', 'lab'), 'disinfect': ('admin', 'quarantine')}

    # 实验室报告与消毒是级联动作，由 plan_transition 单独编排
    CASCADE_ACTIONS = {'lab_report', 'disinfect'}
    REPORT_FROM_STATUSES = ('inspected', 'quarantined', 'lab_positive', 'lab_negative')
    DISINFECT_FROM_STATUSES = ('lab_positive',)

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

    # ------------------------------------------------------------------
    # 级联编排：实验室报告 / 消毒
    # ------------------------------------------------------------------
    def plan_transition(self, actor, entity, action, data, load_all):
        """返回级联执行计划。

        - 普通动作返回 None，由 service 回退到 validate_transition
        - 重复实验室报告返回 {"duplicate": True}
        - 其余返回 {"primary": {...}, "effects": [...], "detail": {...}}
        """
        kind = self.normalize_kind(entity["kind"])
        if kind != "consignment" or action not in self.CASCADE_ACTIONS:
            return None
        self._ensure_role(actor, self.ROLE_ACTIONS.get(action, ("admin",)))
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        if action == "lab_report":
            return self._plan_lab_report(actor, entity, data, load_all)
        return self._plan_disinfect(actor, entity, data, load_all)

    def _plan_lab_report(self, actor, entity, data, load_all):
        report_id = str(data["report_id"])
        result = str(data["result"]).lower()
        if result not in ("positive", "negative"):
            raise ValidationError("lab result must be 'positive' or 'negative'")
        result_date = _parse_date(data["result_date"])
        if entity["status"] not in self.REPORT_FROM_STATUSES:
            raise InvalidTransition(
                "cannot lab_report from status %s" % entity["status"]
            )

        reports = list(entity["data"].get("lab_reports", []))
        # 重复报告只保留首次结论，不产生任何停运/释放效果
        if any(str(item.get("report_id")) == report_id for item in reports):
            return {"duplicate": True}

        reports.append({
            "report_id": report_id,
            "result": result,
            "result_date": result_date.isoformat(),
        })
        patch = {"lab_reports": reports}
        first = reports[0]
        if len(reports) == 1:
            patch.update({
                "lab_result": first["result"],
                "lab_report_id": first["report_id"],
                "lab_result_date": first["result_date"],
            })
            detail_conclusion = first["result"]
        else:
            # 首次结论字段保持不变；复核结论单独记录
            patch["lab_recheck"] = {
                "report_id": report_id,
                "result": result,
                "result_date": result_date.isoformat(),
            }
            detail_conclusion = result

        chain = self.contact_chain(entity["id"], load_all)
        effects = []
        if result == "positive":
            next_status = "lab_positive"
            patch["contact_chain"] = {
                "consignment_ids": list(chain["consignment_ids"]),
                "facility_ids": list(chain["facility_ids"]),
            }
            patch["quarantined_by"] = actor.user_id
            for facility in chain["facilities"]:
                effect = _stop_effect(facility, entity["id"], report_id, result_date)
                if effect:
                    effects.append(effect)
        else:
            next_status = "lab_negative"
            patch["released_by"] = actor.user_id
            # 阴性报告只释放该批次造成的停运，不碰其他批次
            for facility in chain["facilities"]:
                effect = _resume_effect(facility, entity["id"])
                if effect:
                    effects.append(effect)

        return {
            "primary": {"status": next_status, "patch": patch},
            "effects": effects,
            "detail": {
                "report_id": report_id,
                "result": result,
                "conclusion": detail_conclusion,
                "result_date": result_date.isoformat(),
                "affected_facilities": [item["id"] for item in effects],
            },
        }

    def _plan_disinfect(self, actor, entity, data, load_all):
        if entity["status"] not in self.DISINFECT_FROM_STATUSES:
            raise InvalidTransition(
                "cannot disinfect from status %s" % entity["status"]
            )
        positive = _latest_positive(entity["data"].get("lab_reports", []))
        if not positive:
            raise ValidationError("disinfection requires a positive lab report")
        if not isinstance(data.get("covered_facilities"), list):
            raise ValidationError("missing required field: covered_facilities")
        certificate_date = _parse_date(data["certificate_date"])
        positive_date = _parse_date(positive["result_date"])
        # 证明日期必须晚于阳性结果日期
        if certificate_date <= positive_date:
            raise ValidationError(
                "certificate date must be later than positive result date %s"
                % positive_date.isoformat()
            )

        covered = [str(item) for item in data["covered_facilities"]]
        chain = self.contact_chain(entity["id"], load_all)
        facilities = {item["id"]: item for item in chain["facilities"]}
        unknown = [item for item in covered if item not in facilities]
        if unknown:
            raise ValidationError("unknown facility in coverage: " + ", ".join(unknown))

        # 快照中的接触链场地，加上当前仍由该批次停运的场地（防止快照过期）
        required = set(chain["facility_ids"])
        required.update(_facilities_stopped_by(load_all("facility"), entity["id"]))
        missing = sorted(required - set(covered))
        # 缺一个场地就保留停运，并说明缺项
        if missing:
            names = [
                "%s(%s)" % (item, facilities.get(item, {}).get("data", {}).get("name", "?"))
                if item in facilities else item
                for item in missing
            ]
            raise ValidationError(
                "disinfection coverage missing contact-chain sites: " + ", ".join(names)
            )

        patch = {
            "disinfection": {
                "certificate_id": str(data["certificate_id"]),
                "certificate_date": certificate_date.isoformat(),
                "covered_facilities": covered,
                "certified_by": actor.user_id,
            },
            "released_by": actor.user_id,
        }
        effects = []
        for facility in chain["facilities"]:
            effect = _resume_effect(facility, entity["id"])
            if effect:
                effects.append(effect)

        return {
            "primary": {"status": "released", "patch": patch},
            "effects": effects,
            "detail": {
                "certificate_id": str(data["certificate_id"]),
                "certificate_date": certificate_date.isoformat(),
                "covered_facilities": covered,
                "released_facilities": [item["id"] for item in effects],
            },
        }

    def contact_chain(self, consignment_id, load_all):
        """整条接触链：阳性批次自身、全部下游批次，以及它们关联的全部场地。"""
        consignments = load_all("consignment")
        consignment_map = {item["id"]: item for item in consignments}
        links = [
            {"id": item["id"], "parent_id": item["data"].get("parent_id")}
            for item in consignments
        ]
        chain_ids = trace_downstream(links, consignment_id)
        facilities = load_all("facility")
        linked = []
        seen = set()
        for facility in facilities:
            refs = _facility_consignment_refs(facility["data"])
            if refs & set(chain_ids) and facility["id"] not in seen:
                seen.add(facility["id"])
                linked.append(facility)
        return {
            "consignment_ids": chain_ids,
            "consignments": [consignment_map[item] for item in chain_ids if item in consignment_map],
            "facility_ids": [item["id"] for item in linked],
            "facilities": linked,
        }


def _facility_consignment_refs(data):
    refs = set()
    single = data.get("consignment_id")
    if single:
        refs.add(str(single))
    for value in data.get("consignment_ids", []) or []:
        refs.add(str(value))
    return refs


def _facilities_stopped_by(facilities, consignment_id):
    result = []
    for facility in facilities:
        if facility["status"] != "stopped":
            continue
        if any(item.get("consignment_id") == consignment_id
               for item in facility["data"].get("stoppages", [])):
            result.append(facility["id"])
    return result


def _stop_effect(facility, consignment_id, report_id, result_date):
    """对一个场地叠加一次停运归因；已停运且已归因则返回 None。"""
    data = facility["data"]
    stoppages = list(data.get("stoppages", []))
    if any(item.get("consignment_id") == consignment_id for item in stoppages):
        return None
    from_status = facility["status"] if facility["status"] != "stopped" \
        else data.get("status_before_stop")
    patch = {
        "stoppages": stoppages + [{
            "consignment_id": consignment_id,
            "report_id": report_id,
            "result_date": result_date.isoformat(),
        }],
    }
    if facility["status"] != "stopped":
        patch["status_before_stop"] = facility["status"]
    return {
        "type": "facility_stop",
        "id": facility["id"],
        "from_status": facility["status"],
        "to_status": "stopped",
        "patch": patch,
        "action": "stop",
    }


def _resume_effect(facility, consignment_id):
    """移除某批次对场地的停运归因；仍有其他批次归因时保持停运。"""
    data = facility["data"]
    stoppages = data.get("stoppages", [])
    remaining = [item for item in stoppages
                 if item.get("consignment_id") != consignment_id]
    if len(remaining) == len(stoppages):
        return None
    if remaining:
        return {
            "type": "facility_stay_stopped",
            "id": facility["id"],
            "from_status": "stopped",
            "to_status": "stopped",
            "patch": {"stoppages": remaining},
            "action": "partial_release",
        }
    # 没有其他批次归因：恢复到停运前状态
    return {
        "type": "facility_resume",
        "id": facility["id"],
        "from_status": "stopped",
        "to_status": data.get("status_before_stop", "registered"),
        "patch": {"stoppages": [], "status_before_stop": None},
        "action": "resume",
    }


def _latest_positive(reports):
    positive = [item for item in reports if item.get("result") == "positive"]
    return positive[-1] if positive else None


def _parse_date(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        raise ValidationError("invalid date: " + str(value))


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
