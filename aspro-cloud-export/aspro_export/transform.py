from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from html import unescape

from .api import ExportError


def task_depths(records):
    parents = {str(r["id"]): str(r.get("parent_id") or "0") for r in records}
    depths = {}
    def resolve(identifier, pending):
        if identifier in depths:
            return depths[identifier]
        if identifier in pending:
            raise ExportError("Task hierarchy contains a cycle")
        parent = parents[identifier]
        depth = 0 if parent == "0" else 1 + (resolve(parent, pending | {identifier}) if parent in parents else 0)
        depths[identifier] = depth
        return depth
    for identifier in parents:
        resolve(identifier, set())
    return depths


def transform(record, column, lookups):
    if column.get("unresolved"):
        raise ExportError("Unresolved column: " + column["header"])
    if column.get("transform") == "constant":
        return column["value"]
    if column.get("closed_labels"):
        state = str(record.get("active"))
        if state in column["closed_labels"]:
            return column["closed_labels"][state]
    field = column.get("field")
    if field not in record:
        raise ExportError("Missing API field for column: " + column["header"])
    value = record[field]
    if column.get("transform") == "task_id":
        return "— " * lookups["task_depths"][str(value)] + str(value)
    if column.get("only_active") is not None and str(record.get("active")) != str(column["only_active"]):
        return ""
    if str(value) == "0" and str(record.get("id")) in column.get("zero_record_labels", {}):
        return column["zero_record_labels"][str(record["id"])]
    if column.get("transform") == "overdue_flag":
        if not value or str(value).startswith("0000-00-00"):
            return "0"
        try:
            deadline = datetime.fromisoformat(str(value))
            closed = record["closed_date"]
            end = (datetime.fromisoformat(str(closed)) if closed and
                   not str(closed).startswith("0000-00-00") else
                   datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None))
            return str(int(end > deadline))
        except (KeyError, ValueError, TypeError):
            raise ExportError("Invalid overdue dates") from None
    if column.get("blank_if_zero") and str(value) == "0":
        return ""
    kind = column.get("transform", "text")
    if kind == "project":
        if "module" not in record or "model" not in record:
            raise ExportError("Project relation discriminator is missing")
        if record["module"] != "st" or record["model"] != "project":
            return ""
        kind = "lookup"
    if kind == "custom":
        if not isinstance(value, list):
            raise ExportError("Invalid customfields structure")
        matches = [f for f in value if str(f.get("id")) == str(column["custom_id"])]
        if len(matches) != 1:
            raise ExportError("Custom field missing or ambiguous: " + column["header"])
        value = matches[0].get("value")
        kind = column.get("value_transform", "text")
    if value is None or value == "":
        return column.get("empty_value", "")
    if kind == "lookup":
        if str(value) == "0":
            return column.get("zero_label", "")
        table = lookups[column["lookup"]]
        try:
            value = table[str(value)]
        except KeyError:
            if column.get("empty_if_missing"):
                return ""
            raise ExportError("Missing related record for column: " + column["header"]) from None
    elif kind == "enum":
        values = column["values"]
        if str(value) not in values:
            raise ExportError("Unknown enum value for column: " + column["header"])
        value = values[str(value)]
    elif kind in ("date", "datetime"):
        if value in ("0000-00-00", "0000-00-00 00:00:00"):
            return ""
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo("Europe/Moscow"))
            value = parsed.strftime("%d.%m.%Y" if kind == "date" else "%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            raise ExportError("Invalid date for column: " + column["header"]) from None
    elif kind in ("decimal", "money"):
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                raise InvalidOperation
            if kind == "money":
                if number != number.quantize(Decimal("0.01")):
                    raise InvalidOperation
                value = format(number, ",.2f").replace(",", " ") + " p."
            else:
                value = format(number, "f")
        except (ValueError, InvalidOperation):
            raise ExportError("Invalid number for column: " + column["header"]) from None
    elif kind != "text":
        raise ExportError("Unknown transformation")
    if isinstance(value, (list, dict)):
        raise ExportError("Complex value needs an explicit transformation: " + column["header"])
    if value is None:
        return ""
    value = str(value)
    if column.get("normalize_nbsp"):
        value = value.replace('\xa0', ' ')
    if column.get("blank_if_unpaired_quotes") and unescape(value).count('"') % 2:
        return ""
    if column.get("escape_apostrophe"):
        value = value.replace("'", '&#039;')
    value = value.strip() if column.get("strip") else value
    return value
