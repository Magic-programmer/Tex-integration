import logging

from .api import ApiApplicationError, ExportError

log = logging.getLogger(__name__)


def join_labels(values, spec):
    if any(not isinstance(value, str) for value in values):
        raise ExportError("Invalid related string value")
    if spec.get("clean_email_list"):
        values = [value.replace("\ufeff", "").strip() for value in values]
        values = [value for value in values if value]
    if spec.get("unique"):
        values = list(dict.fromkeys(values))
    return spec["separator"].join(values)


class LookupTable(dict):
    """Resolve a missing ID with a documented get method and cache the result."""
    def __init__(self, values, client, spec):
        super().__init__(values)
        self.client, self.spec = client, spec

    def __getitem__(self, identifier):
        value = super().__getitem__(identifier)
        if (value is None or value == "") and self.spec.get("unavailable_label") == "id":
            return self._original_id(identifier)
        return value

    def __missing__(self, identifier):
        path = self.spec.get("get_path")
        if not path:
            raise KeyError(identifier)
        try:
            record = self.client.get(path.format(id=identifier))
        except ApiApplicationError:
            if self.spec.get("unavailable_label") != "id":
                raise
            return self._original_id(identifier)
        label = self.spec["label"]
        if str(record.get("id")) != identifier or label not in record:
            raise ExportError("Individual lookup returned an invalid related record")
        value = record[label]
        if value is None or value == "":
            if self.spec.get("unavailable_label") == "id":
                return self._original_id(identifier)
            raise ExportError("Individual lookup label is unavailable")
        self[identifier] = value
        return value

    def _original_id(self, identifier):
        self[identifier] = identifier
        log.warning("Related user name unavailable; original ID retained (value omitted)")
        return identifier
