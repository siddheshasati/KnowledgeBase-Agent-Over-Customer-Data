import csv
import hashlib
import re
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any


@dataclass
class ParsedRecord:
    id: str
    source_file: str
    record_type: str
    title: str
    text: str
    metadata: dict[str, Any]
    entities: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def source_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_file": self.source_file,
            "record_type": self.record_type,
            "title": self.title,
            "text": self.text,
            "content_hash": self.content_hash,
            "version": self.content_hash[:12],
            "metadata": self.metadata,
        }


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def parse_dataset(dataset_dir: Path) -> list[ParsedRecord]:
    records: list[ParsedRecord] = []
    for path in sorted(dataset_dir.glob("*.md")):
        if path.name == "meeting_notes.md":
            records.extend(parse_meetings(path))
        else:
            records.extend(parse_markdown_table(path))
    return records


def parse_markdown_table(path: Path) -> list[ParsedRecord]:
    lines = path.read_text(encoding="utf-8").splitlines()
    table_lines = [line for line in lines if line.startswith("|") and not set(line.replace("|", "").strip()) <= {"-", ":"}]
    if len(table_lines) < 2:
        return []
    headers = _split_table_row(table_lines[0])
    rows = [_split_table_row(line) for line in table_lines[1:]]
    parsed: list[ParsedRecord] = []
    for row in rows:
        if len(row) != len(headers):
            continue
        data = dict(zip(headers, row, strict=True))
        parsed.append(_record_from_table_row(path.name, data))
    return parsed


def _split_table_row(line: str) -> list[str]:
    clean = line.strip().strip("|")
    reader = csv.reader(StringIO(clean), delimiter="|", skipinitialspace=True)
    return [cell.strip() for cell in next(reader)]


def _record_from_table_row(source_file: str, data: dict[str, str]) -> ParsedRecord:
    if source_file == "accounts.md":
        return _account_record(source_file, data)
    if source_file == "issues.md":
        return _issue_record(source_file, data)
    if source_file == "feature_requests.md":
        return _feature_request_record(source_file, data)
    if source_file == "tasks.md":
        return _task_record(source_file, data)
    stable = data.get("ID") or slugify("|".join(data.values()))
    return ParsedRecord(
        id=f"{source_file}:{stable}",
        source_file=source_file,
        record_type="GenericRecord",
        title=data.get("Title", stable),
        text=_row_text(data),
        metadata=data,
    )


def _account_record(source_file: str, data: dict[str, str]) -> ParsedRecord:
    account_id = data["ID"]
    account_entity = {
        "id": f"account-name:{slugify(data['Name'])}",
        "type": "Account",
        "name": data["Name"],
        "source_account_id": account_id,
        "industry": data["Industry"],
        "region": data["Region"],
        "tier": data["Tier"],
        "health": data["Health"],
        "arr": _money_to_int(data["ARR"]),
        "devices": data["Devices"],
        "text": _row_text(data),
    }
    owner = {"id": f"person:{slugify(data['Owner'])}", "type": "Person", "name": data["Owner"], "role": "account_owner"}
    plan = {"id": f"plan:{slugify(data['Tier'])}", "type": "Plan", "name": data["Tier"], "tier": data["Tier"]}
    entities = [account_entity, owner, plan]
    relationships = [
        (account_entity["id"], owner["id"], "OWNED_BY", {}),
        (account_entity["id"], plan["id"], "HAS_PLAN", {"tier": data["Tier"]}),
    ]
    for device in _split_list(data["Devices"]):
        device_entity = {"id": f"product-feature:{slugify(device)}", "type": "ProductFeature", "name": device, "category": "device"}
        entities.append(device_entity)
        relationships.append((account_entity["id"], device_entity["id"], "USES", {}))
    return ParsedRecord(
        id=f"account:{account_id}",
        source_file=source_file,
        record_type="Account",
        title=data["Name"],
        text=_row_text(data),
        metadata=data,
        entities=entities,
        relationships=relationships,
    )


def _issue_record(source_file: str, data: dict[str, str]) -> ParsedRecord:
    issue_id = data["ID"]
    account_id = f"account-name:{slugify(data['Account'])}"
    issue_entity = {
        "id": f"issue:{issue_id.lower()}",
        "type": "Issue",
        "name": data["Title"],
        "title": data["Title"],
        "category": data["Category"],
        "status": data["Status"],
        "account_name": data["Account"],
        "text": _row_text(data),
    }
    account_ref = {"id": account_id, "type": "Account", "name": data["Account"]}
    return ParsedRecord(
        id=f"issue:{issue_id.lower()}",
        source_file=source_file,
        record_type="Issue",
        title=data["Title"],
        text=_row_text(data),
        metadata=data,
        entities=[issue_entity, account_ref],
        relationships=[(issue_entity["id"], account_ref["id"], "AFFECTS", {"status": data["Status"]})],
    )


def _feature_request_record(source_file: str, data: dict[str, str]) -> ParsedRecord:
    feature_id = f"feature-request:{slugify(data['Title'])}"
    accounts = _split_list(data["Accounts Requesting"])
    request = {
        "id": feature_id,
        "type": "FeatureRequest",
        "name": data["Title"],
        "title": data["Title"],
        "product_area": data["Product Area"],
        "status": data["Status"],
        "mentions": _safe_int(data["Mentions"]),
        "revenue_impact": _money_to_int(data["Est. Revenue Impact"]),
        "accounts": ", ".join(accounts),
        "text": _row_text(data),
    }
    feature = {
        "id": f"product-feature:{slugify(data['Title'])}",
        "type": "ProductFeature",
        "name": data["Title"],
        "product_area": data["Product Area"],
    }
    entities = [request, feature]
    relationships = [(request["id"], feature["id"], "REQUESTS_FEATURE", {"status": data["Status"]})]
    for account in accounts:
        account_ref = {"id": f"account-name:{slugify(account)}", "type": "Account", "name": account}
        entities.append(account_ref)
        relationships.append((account_ref["id"], request["id"], "REQUESTED", {"mentions": _safe_int(data["Mentions"])}))
    return ParsedRecord(
        id=feature_id,
        source_file=source_file,
        record_type="FeatureRequest",
        title=data["Title"],
        text=_row_text(data),
        metadata=data,
        entities=entities,
        relationships=relationships,
    )


def _task_record(source_file: str, data: dict[str, str]) -> ParsedRecord:
    task_id = data["ID"]
    task = {
        "id": f"task:{task_id.lower()}",
        "type": "Task",
        "name": data["Title"],
        "title": data["Title"],
        "priority": data["Priority"],
        "status": data["Status"],
        "due": data["Due"],
        "account_name": data["Account"],
        "text": _row_text(data),
    }
    account = {"id": f"account-name:{slugify(data['Account'])}", "type": "Account", "name": data["Account"]}
    assignee = {"id": f"person:{slugify(data['Assignee'])}", "type": "Person", "name": data["Assignee"], "role": "assignee"}
    return ParsedRecord(
        id=f"task:{task_id.lower()}",
        source_file=source_file,
        record_type="Task",
        title=data["Title"],
        text=_row_text(data),
        metadata=data,
        entities=[task, account, assignee],
        relationships=[(task["id"], account["id"], "FOR_ACCOUNT", {}), (task["id"], assignee["id"], "ASSIGNED_TO", {})],
    )


def parse_meetings(path: Path) -> list[ParsedRecord]:
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=## MTG-)", text)
    records: list[ParsedRecord] = []
    for block in blocks:
        header = re.search(r"##\s+(MTG-\d+):\s+(.+)", block)
        if not header:
            continue
        meeting_id, account_name = header.group(1), header.group(2).strip()
        topic = _extract_bold(block, "Topic")
        attendees = _split_list(_extract_bold(block, "Attendees"))
        date = _extract_bold(block, "Date")
        action_items = [line.strip("- ").strip() for line in block.splitlines() if line.strip().startswith("- ")]
        meeting = {
            "id": f"meeting:{meeting_id.lower()}",
            "type": "Meeting",
            "name": f"{meeting_id}: {account_name}",
            "title": topic,
            "account_name": account_name,
            "date": date,
            "action_items": "; ".join(action_items),
            "text": block.strip(),
        }
        account = {"id": f"account-name:{slugify(account_name)}", "type": "Account", "name": account_name}
        entities = [meeting, account]
        relationships = [(meeting["id"], account["id"], "FOR_ACCOUNT", {})]
        for attendee in attendees:
            person = {"id": f"person:{slugify(attendee)}", "type": "Person", "name": attendee}
            entities.append(person)
            relationships.append((meeting["id"], person["id"], "ATTENDED_BY", {}))
        records.append(
            ParsedRecord(
                id=f"meeting:{meeting_id.lower()}",
                source_file=path.name,
                record_type="Meeting",
                title=f"{meeting_id}: {topic}",
                text=block.strip(),
                metadata={"id": meeting_id, "account": account_name, "topic": topic, "attendees": attendees, "date": date, "action_items": action_items},
                entities=entities,
                relationships=relationships,
            )
        )
    return records


def _extract_bold(block: str, label: str) -> str:
    match = re.search(rf"\*\*{re.escape(label)}:\*\*\s*(.+)", block)
    return match.group(1).strip() if match else ""


def _row_text(data: dict[str, str]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in data.items())


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _money_to_int(value: str) -> int:
    return _safe_int(value.replace("$", "").replace(",", ""))


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
