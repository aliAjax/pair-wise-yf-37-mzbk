from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _parse_date(value, field):
    try:
        return _date_ordinal(value)
    except (TypeError, ValueError):
        raise ValidationError("invalid date for %s: %s" % (field, value))


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


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


# 来源病例必须是已经确诊（或疑似确诊）过的病例
SOURCE_CASE_STATUSES = ("confirmed", "probable", "recovered", "closed")
# 仍处于随访流程中、等待处理的接触者状态
PENDING_CONTACT_STATUSES = ("identified", "following")
# 接触者结局中出现这些词时视为已感染，不允许解除
INFECTED_OUTCOME_TOKENS = ("infected", "confirmed", "positive", "detected", "确诊", "感染", "阳性")


def _validate_register_exposure(actor, entity, data, lookup):
    source_id = data.get("source_case_id")
    if source_id == entity["id"]:
        raise ValidationError("暴露关系未建立：病例不能以自己为来源病例")
    source = _find_one(lookup, "case", "id", source_id)
    if source is None:
        raise ValidationError("暴露关系未建立：来源病例不存在 (%s)" % source_id)
    if source["status"] not in SOURCE_CASE_STATUSES:
        raise ValidationError(
            "暴露关系未建立：来源病例状态为 %s，需为 %s"
            % (source["status"], "/".join(SOURCE_CASE_STATUSES))
        )
    exposure_date = data.get("exposure_date")
    exposure_ordinal = _parse_date(exposure_date, "exposure_date")
    onset_date = entity["data"].get("onset_date")
    if onset_date and exposure_ordinal > _parse_date(onset_date, "onset_date"):
        raise ValidationError(
            "暴露关系未建立：暴露时间 %s 晚于发病日期 %s" % (exposure_date, onset_date)
        )
    existing = list(entity["data"].get("exposures", []))
    previous = [item for item in existing if item.get("source_case_id") == source_id]
    kept = [item for item in existing if item.get("source_case_id") != source_id]
    entry = {
        "source_case_id": source_id,
        "exposure_date": exposure_date,
        "exposure_location": data.get("exposure_location"),
    }
    if previous and _parse_date(previous[0].get("exposure_date"), "exposure_date") >= exposure_ordinal:
        # 同一人与同一来源多次暴露时保留最近一次
        entry = previous[0]
    kept.append(entry)
    primary = max(kept, key=lambda item: _parse_date(item.get("exposure_date"), "exposure_date"))
    return {
        "exposures": kept,
        "source_case_id": primary["source_case_id"],
        "exposure_date": primary["exposure_date"],
        "exposure_location": primary["exposure_location"],
    }


def _validate_release(actor, entity, data, lookup):
    outcome = str(data.get("outcome", "")).lower()
    if any(token in outcome for token in INFECTED_OUTCOME_TOKENS):
        raise ValidationError("接触者结局提示已感染，不能解除，请转为病例管理")


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


def _case_graph(cases):
    by_id = {case["id"]: case for case in cases}
    parent = {}
    children = {}
    for case in cases:
        source_id = case["data"].get("source_case_id")
        if source_id and source_id != case["id"] and source_id in by_id:
            parent[case["id"]] = source_id
            children.setdefault(source_id, []).append(case["id"])
    return parent, children


def _chain_member(case, generation):
    data = case["data"]
    return {
        "case_id": case["id"],
        "person_id": data.get("person_id"),
        "status": case["status"],
        "onset_date": data.get("onset_date"),
        "generation": generation,
        "source_case_id": data.get("source_case_id"),
        "exposure_date": data.get("exposure_date"),
        "exposure_location": data.get("exposure_location"),
    }


def build_chains(cases, contacts):
    """按来源关系把病例串成传播链，计算代际，并挂上每条链的接触者。"""
    parent, children = _case_graph(cases)

    def root_of(case_id):
        seen = set()
        current = case_id
        while current in parent and current not in seen:
            seen.add(current)
            current = parent[current]
        return current

    generations = {}
    for case in cases:
        root = root_of(case["id"])
        if root in generations:
            continue
        queue = [(root, 1)]
        while queue:
            node, generation = queue.pop(0)
            if node in generations:
                continue
            generations[node] = generation
            for child in children.get(node, []):
                queue.append((child, generation + 1))

    grouped = {}
    for case in cases:
        grouped.setdefault(root_of(case["id"]), []).append(case)

    contacts_by_case = {}
    for contact in contacts:
        contacts_by_case.setdefault(contact["data"].get("case_id"), []).append(contact)

    chains = []
    for root, members in grouped.items():
        member_entries = sorted(
            (_chain_member(case, generations.get(case["id"], 1)) for case in members),
            key=lambda item: (item["generation"], str(item["onset_date"]), item["case_id"]),
        )
        contact_entries = []
        for case in members:
            for contact in contacts_by_case.get(case["id"], []):
                contact_entries.append({
                    "contact_id": contact["id"],
                    "person_id": contact["data"].get("person_id"),
                    "case_id": case["id"],
                    "status": contact["status"],
                    "pending": contact["status"] in PENDING_CONTACT_STATUSES,
                })
        contact_entries.sort(key=lambda item: item["contact_id"])
        chains.append({
            "chain_id": root,
            "size": len(member_entries),
            "members": member_entries,
            "contacts": contact_entries,
            "pending_contacts": [entry for entry in contact_entries if entry["pending"]],
        })
    chains.sort(key=lambda item: (-item["size"], item["chain_id"]))
    return chains


def chain_lineage(chain, case_id):
    """在单条链内给出某病例的上游（来源方向）和下游（被传染方向）。"""
    members = {member["case_id"]: member for member in chain["members"]}
    if case_id not in members:
        return {"upstream": [], "downstream": []}
    seen = {case_id}
    upstream = []
    current = members[case_id].get("source_case_id")
    while current and current in members and current not in seen:
        seen.add(current)
        upstream.append(members[current])
        current = members[current].get("source_case_id")
    downstream = []
    queue = [case_id]
    while queue:
        node = queue.pop(0)
        for member in chain["members"]:
            if member.get("source_case_id") == node and member["case_id"] not in seen:
                seen.add(member["case_id"])
                downstream.append(member)
                queue.append(member["case_id"])
    return {"upstream": upstream, "downstream": downstream}


CUSTOM_CREATE = {'case': _validate_case}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable, ('case', 'register_exposure'): _validate_register_exposure, ('contact', 'release'): _validate_release}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'register_exposure': (('confirmed', 'probable'), None), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed'), 'release': (('identified', 'following'), 'released')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'register_exposure'): ('source_case_id', 'exposure_date', 'exposure_location'), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',), ('contact', 'release'): ('outcome',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'register_exposure': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator'), 'release': ('admin', 'investigator')}

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
        if next_status is None:
            next_status = entity["status"]
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
