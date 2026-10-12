import pytest

from export_aspro import ExportError
from export_aspro import transform
from export_aspro import task_depths


def apply(value, kind="text", **kwargs):
    return transform({"value": value}, {"header": "Test", "field": "value",
                     "transform": kind, **kwargs}, {"users": {"2": "Synthetic user"}})


@pytest.mark.parametrize("value,expected", [(None, ""), ("", ""), (0, "0"),
    ("00123", "00123"), ("text, with\nnew line", "text, with\nnew line")])
def test_text_preserves_values(value, expected):
    assert apply(value) == expected


def test_dates_and_api_empty_sentinel():
    assert apply("2026-10-12 01:02:03", "datetime") == "12.10.2026 01:02"
    assert apply("2026-10-11T22:00:00Z", "datetime") == "12.10.2026 01:00"
    assert apply("0000-00-00 00:00:00", "date") == ""
    with pytest.raises(ExportError):
        apply("invalid sensitive value", "date")


def test_decimal_and_money():
    assert apply("0.00", "decimal") == "0.00"
    assert apply("1234567.89", "money") == "1 234 567.89 p."
    for value in ["NaN", "Infinity", "1.234"]:
        with pytest.raises(ExportError):
            apply(value, "money")


def test_lookup_and_unknown_enum():
    assert apply(2, "lookup", lookup="users") == "Synthetic user"
    assert apply(0, "lookup", lookup="users") == ""
    with pytest.raises(ExportError):
        apply(3, "lookup", lookup="users")
    with pytest.raises(ExportError):
        apply(3, "enum", values={"1": "Synthetic state"})


def test_custom_field_boolean():
    assert apply([{"id": "4", "value": "0"}], "custom", custom_id=4,
                 value_transform="enum", values={"0": "Нет", "1": "Да"}) == "Нет"
    with pytest.raises(ExportError):
        apply([], "custom", custom_id=4)


def test_project_discriminator():
    column = {"header": "Проект", "field": "model_id", "transform": "project", "lookup": "projects"}
    assert transform({"module": "crm", "model": "lead", "model_id": 2}, column, {}) == ""
    assert transform({"module": "st", "model": "project", "model_id": 2}, column,
                     {"projects": {"2": "Synthetic project"}}) == "Synthetic project"


def test_missing_field_not_silently_blank():
    with pytest.raises(ExportError):
        transform({}, {"header": "Test", "field": "absent"}, {})


def test_manual_csv_presentation():
    assert apply(0, blank_if_zero=True) == ""
    assert apply(2, blank_if_zero=True) == "2"
    assert apply("  Synthetic task  ", strip=True) == "Synthetic task"
    assert transform({"active": 2}, {"header": "Этап", "closed_labels":
                     {"2": "Проиграна"}}, {}) == "Проиграна"
    assert transform({}, {"header": "Дела", "transform": "constant", "value": ""}, {}) == ""


@pytest.mark.parametrize("closed,deadline,expected", [
    ("2026-01-01 10:00:01", "2026-01-01 10:00:00", "1"),
    ("2026-01-01 10:00:00", "2026-01-01 10:00:01", "0"),
    ("2026-01-01 10:00:00", "2026-01-01 10:00:00", "0"),
    ("2026-01-01 10:00:00", "0000-00-00 00:00:00", "0"),
])
def test_overdue_uses_seconds_not_csv_minutes(closed, deadline, expected):
    assert transform({"closed_date": closed, "deadline": deadline},
                     {"header": "Просроченность", "field": "deadline",
                      "transform": "overdue_flag"}, {}) == expected


def test_open_task_overdue_and_nullable_boolean():
    column = {"header": "Просроченность", "field": "deadline", "transform": "overdue_flag"}
    assert transform({"deadline": "2000-01-01 00:00:00", "closed_date": None}, column, {}) == "1"
    assert transform({"deadline": "2999-01-01 00:00:00", "closed_date": "0000-00-00 00:00:00"}, column, {}) == "0"
    assert apply([{"id": 4, "value": None}], "custom", custom_id=4,
                 value_transform="enum", values={"0": "Нет", "1": "Да"}, empty_value="Нет") == "Нет"


def test_zero_owner_label_is_specific_to_confirmed_record():
    col = {"header": "Ответственный", "field": "owner_id", "transform": "lookup",
           "lookup": "users", "zero_record_labels": {"10": "Robot"}}
    assert transform({"id": 10, "owner_id": 0}, col, {}) == "Robot"
    assert transform({"id": 11, "owner_id": 0}, col, {}) == ""


def test_task_hierarchy_prefix_and_cycle():
    rows = [{"id": 1, "parent_id": 0}, {"id": 2, "parent_id": 1}, {"id": 3, "parent_id": 2}]
    depths = task_depths(rows)
    assert transform(rows[2], {"header": "ID", "field": "id", "transform": "task_id"},
                     {"task_depths": depths}) == "— — 3"
    with pytest.raises(ExportError):
        task_depths([{"id": 1, "parent_id": 2}, {"id": 2, "parent_id": 1}])


def test_manual_export_quote_and_nbsp_rules():
    assert apply('Synthetic &quot; name', blank_if_unpaired_quotes=True) == ""
    assert apply('Synthetic "name"', blank_if_unpaired_quotes=True) == 'Synthetic "name"'
    assert apply('Synthetic\xa0name', normalize_nbsp=True) == 'Synthetic name'
    assert apply("Synthetic' name", escape_apostrophe=True) == 'Synthetic&#039; name'


def test_loss_reason_only_for_lost_lead():
    column = {"header": "Reason", "field": "reason", "only_active": "2"}
    assert transform({"active": 1, "reason": "Synthetic"}, column, {}) == ""
    assert transform({"active": 2, "reason": "Synthetic"}, column, {}) == "Synthetic"
