from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


CUSTOM_CREATE = {'case': _validate_case}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable}


EXPOSURE_KIND = "exposure"
EXPOSURE_REQUIRED = ("case_id", "source_case_id", "exposure_date", "location")
# 只有已确诊（含确诊后转归）的病例才能作为传播关系的两端。
CONFIRMED_CASE_STATUSES = frozenset(("confirmed", "recovered", "closed"))
PENDING_CONTACT_STATUSES = frozenset(("identified", "following"))


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact', 'exposures': 'exposure'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed'), 'release': (('identified', 'following'), 'released')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',), ('contact', 'release'): ('outcome',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator'), ('contact', 'release'): ('admin', 'investigator')}

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

    def validate_exposure(self, actor, payload, target_case, source_case):
        """登记一次暴露，返回关系状态与未建立原因。

        来源病例不是确诊状态，或暴露时间晚于下游病例发病日期时，关系不建立。
        """
        self._ensure_role(actor, ("admin", "investigator"))
        self._require(payload, EXPOSURE_REQUIRED)
        try:
            exposure_ord = _date_ordinal(payload.get("exposure_date"))
        except (TypeError, ValueError):
            raise ValidationError("exposure_date must be an ISO date")

        reasons = []
        if target_case is None:
            reasons.append("下游病例不存在")
        elif target_case["kind"] != "case":
            reasons.append("下游记录不是病例")
        elif target_case["status"] not in CONFIRMED_CASE_STATUSES:
            reasons.append("下游病例尚未确诊，关系暂不建立")
        if source_case is None:
            reasons.append("来源病例不存在")
        elif source_case["kind"] != "case":
            reasons.append("来源记录不是病例")
        elif source_case["status"] not in CONFIRMED_CASE_STATUSES:
            reasons.append("来源病例状态为%s，不是确诊病例" % source_case["status"])
        if target_case and source_case and target_case["id"] == source_case["id"]:
            reasons.append("来源病例与下游病例不能是同一病例")
        if target_case and target_case["kind"] == "case":
            try:
                onset_ord = _date_ordinal(target_case["data"].get("onset_date"))
            except (TypeError, ValueError):
                onset_ord = None
            if onset_ord is not None and exposure_ord > onset_ord:
                reasons.append("暴露时间晚于下游病例发病日期，关系不成立")

        if reasons:
            return "not_established", reasons
        return "established", []


def build_network(cases, contacts, exposures):
    """由已建立的暴露关系串出传播链并计算代际。

    一个下游病例可经多个来源暴露，代际取最长传播路径（根来源为第0代）。
    解除后的接触者不出现在待处理列表中，但其上下游关系仍保留。
    """
    case_index = {
        case["id"]: case for case in cases if case["status"] in CONFIRMED_CASE_STATUSES
    }

    # 同一(暴露人, 来源病例)的多条登记只保留最近一次暴露。
    latest = {}
    for exposure in exposures:
        data = exposure["data"]
        key = (data.get("person_id"), data.get("source_case_id"))
        try:
            order = _date_ordinal(data.get("exposure_date"))
        except (TypeError, ValueError):
            continue
        previous = latest.get(key)
        if previous is None or order >= previous[0]:
            latest[key] = (order, exposure)

    edges = {}
    rejected = []
    for order, exposure in latest.values():
        data = exposure["data"]
        if exposure["status"] != "established":
            rejected.append({
                "id": exposure["id"],
                "person_id": data.get("person_id"),
                "case_id": data.get("case_id"),
                "source_case_id": data.get("source_case_id"),
                "exposure_date": data.get("exposure_date"),
                "location": data.get("location"),
                "reasons": list(data.get("reasons", [])),
            })
            continue
        case_id = data.get("case_id")
        source_id = data.get("source_case_id")
        if case_id not in case_index or source_id not in case_index:
            continue
        edges.setdefault(case_id, set()).add(source_id)

    # 最长路径代际，带环保护避免异常数据导致死循环。
    generation = {}

    def resolve(node, stack):
        if node in generation:
            return generation[node]
        if node in stack:
            return 0
        stack.add(node)
        parents = [parent for parent in edges.get(node, ()) if parent != node]
        value = 0 if not parents else 1 + max(resolve(parent, stack) for parent in parents)
        stack.discard(node)
        generation[node] = value
        return value

    for case_id in case_index:
        resolve(case_id, set())

    # 无向连通分量 = 同一条传播链。
    neighbors = {case_id: set() for case_id in case_index}
    for child, parents in edges.items():
        for parent in parents:
            neighbors[child].add(parent)
            neighbors[parent].add(child)

    seen = set()
    chains = []
    chain_id = 1
    for case_id in sorted(case_index, key=lambda item: (generation.get(item, 0), str(case_index[item]["data"].get("onset_date", "")), item)):
        if case_id in seen:
            continue
        stack = [case_id]
        component = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.append(current)
            stack.extend(neighbors.get(current, ()) - seen)
        members = [
            _network_member(case_index[member_id], generation.get(member_id, 0), edges, contacts)
            for member_id in sorted(
                component,
                key=lambda item: (generation.get(item, 0), str(case_index[item]["data"].get("onset_date", "")), item),
            )
        ]
        chains.append({"chain_id": chain_id, "generations": max((member["generation"] for member in members), default=0), "members": members})
        chain_id += 1

    rejected.sort(key=lambda item: (str(item.get("exposure_date") or ""), item["id"]))
    pending_contacts = [
        _contact_view(contact)
        for contact in contacts
        if contact["status"] in PENDING_CONTACT_STATUSES
    ]
    return {"chains": chains, "pending_contacts": pending_contacts, "rejected_exposures": rejected}


def _network_member(case, gen, edges, contacts):
    upstream = sorted(edges.get(case["id"], ()))
    downstream = sorted(child for child, parents in edges.items() if case["id"] in parents)
    linked_contacts = [_contact_view(contact) for contact in contacts if contact["data"].get("case_id") == case["id"]]
    return {
        "case_id": case["id"],
        "status": case["status"],
        "generation": gen,
        "person_id": case["data"].get("person_id"),
        "onset_date": case["data"].get("onset_date"),
        "location": case["data"].get("location"),
        "upstream_case_ids": upstream,
        "downstream_case_ids": downstream,
        "pending_contacts": [item for item in linked_contacts if item["status"] in PENDING_CONTACT_STATUSES],
        "released_contacts": [item for item in linked_contacts if item["status"] == "released"],
        "other_contacts": [
            item for item in linked_contacts
            if item["status"] not in PENDING_CONTACT_STATUSES and item["status"] != "released"
        ],
    }


def _contact_view(contact):
    data = contact["data"]
    return {
        "id": contact["id"],
        "status": contact["status"],
        "case_id": data.get("case_id"),
        "person_id": data.get("person_id"),
        "exposure_start": data.get("exposure_start"),
        "outcome": data.get("outcome"),
    }


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
