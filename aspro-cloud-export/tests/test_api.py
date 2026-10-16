import pytest
import requests
import ssl

from export_aspro import ApiClient, ExportError


class Response:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self.data, self.headers = status, data, headers or {}

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def client(responses):
    waits = []
    session = Session(responses)
    api = ApiClient("https://example.aspro.cloud", "synthetic-test-secret",
                    session=session, sleep=waits.append, clock=lambda: 0)
    return api, session, waits


def page(number, items, total):
    return Response(data={"response": {"page": number, "count": len(items),
                    "total_result": total, "items": items}})


def test_pagination_and_authentication():
    api, session, waits = client([page(1, [{"id": n} for n in range(100)], 101),
                                  page(2, [{"id": 100}], 101)])
    assert len(list(api.records("crm/account/list"))) == 101
    assert session.calls[1][1]["params"]["page"] == 2
    assert session.calls[0][1]["params"]["api_key"] == "synthetic-test-secret"
    assert session.calls[0][1]["allow_redirects"] is False
    assert waits == [1.1]


@pytest.mark.parametrize("responses", [
    [page(1, [{"id": 1}], 2), page(2, [{"id": 1}], 2)],
    [page(1, [{"id": 1}], 2), page(2, [], 2)],
    [page(1, [{"id": 1}], 2), page(2, [{"id": 2}], 3)],
    [page(1, [{}], 1)],
    [Response(data={"response": {"items": []}})],
])
def test_invalid_or_incomplete_pages_are_rejected(responses):
    api, _, _ = client(responses)
    with pytest.raises(ExportError):
        list(api.records("crm/account/list"))


def test_stable_id_boundary_excludes_new_records_from_later_pages():
    api, session, _ = client([page(1,[{'id':101},{'id':100}],101),
                              page(1,[{'id':n} for n in range(1,101)],101),
                              page(2,[{'id':101}],101)])
    api.stable_ids = True
    rows=list(api.records('crm/account/list',{'order_by[asc][]':'id','filter[type]':'1'}))
    assert len(rows)==101
    assert session.calls[0][1]['params']['order_by[desc][]']=='id'
    assert 'order_by[asc][]' not in session.calls[0][1]['params']
    import json
    for _,call in session.calls[1:]:
        assert json.loads(call['params']['filter[id]'])=={'type':'less','value':102}
        assert call['params']['filter[type]']=='1'


def test_stable_id_filter_must_be_enforced_by_api():
    api, _, _ = client([page(1,[{'id':5},{'id':4}],5),page(1,[{'id':6}],1)])
    api.stable_ids=True
    with pytest.raises(ExportError,match='ignored stable ID'):
        list(api.records('crm/lead/list',{'order_by[asc][]':'id'}))


def test_stable_id_probe_rejects_ignored_descending_sort():
    api, _, _ = client([page(1,[{'id':1},{'id':2}],5)])
    api.stable_ids=True
    with pytest.raises(ExportError,match='ignored descending'):
        list(api.records('crm/lead/list',{'order_by[asc][]':'id'}))


def test_empty_page():
    api, _, _ = client([page(1, [], 0)])
    assert list(api.records("crm/account/list")) == []


def test_core_user_total_fallback_only_without_filters():
    response = Response(data={"response": {"page": 1, "count": 1,
                        "total": 1, "items": [{"id": 1}]}})
    api, _, _ = client([response])
    assert list(api.records("core/user/list")) == [{"id": 1}]
    api, _, _ = client([response])
    with pytest.raises(ExportError):
        list(api.records("core/user/list", {"filter[id]": "1"}))


@pytest.mark.parametrize("field", ["page", "count", "total_result"])
def test_boolean_pagination_metadata_rejected(field):
    metadata = {"page": 1, "count": 1, "total_result": 1, "items": [{"id": 1}]}
    metadata[field] = True
    api, _, _ = client([Response(data={"response": metadata})])
    with pytest.raises(ExportError):
        list(api.records("crm/account/list"))


def test_429_retry_after():
    api, session, waits = client([Response(429, headers={"Retry-After": "3"}), page(1, [], 0)])
    assert list(api.records("crm/account/list")) == []
    assert waits == [3, 1.1]
    assert len(session.calls) == 2


def test_network_retry_exhaustion_does_not_leak_key():
    secret = "synthetic-test-secret"
    api, _, _ = client([requests.Timeout("https://example/?api_key=" + secret)] * 4)
    with pytest.raises(ExportError) as exc:
        api.get("crm/account/list")
    assert secret not in str(exc.value)


def test_tls_eof_is_retried_without_disabling_verification():
    error = requests.exceptions.SSLError(ssl.SSLEOFError(8, "synthetic-test-secret"))
    api, session, _ = client([error, page(1, [], 0)])
    assert list(api.records("crm/account/list")) == []
    assert len(session.calls) == 2
    assert all(call[1].get("verify", True) is True for call in session.calls)


def test_certificate_verification_failure_is_not_retried():
    error = requests.exceptions.SSLError(ssl.SSLCertVerificationError("synthetic-test-secret"))
    api, session, _ = client([error])
    with pytest.raises(ExportError) as exc:
        api.get("crm/account/list")
    assert "synthetic-test-secret" not in str(exc.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize("response", [Response(401), Response(403), Response(302),
    Response(data={"error": {"error_msg": "synthetic-test-secret"}}),
    Response(data=ValueError("synthetic-test-secret")), Response(data=[]),
    requests.exceptions.SSLError("synthetic-test-secret")])
def test_permanent_errors_do_not_retry_or_leak(response):
    api, session, _ = client([response])
    with pytest.raises(ExportError) as exc:
        api.get("crm/account/list")
    assert "synthetic-test-secret" not in str(exc.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize("path", ["crm/lead/delete/1", "https://attacker/list", "../list"])
def test_only_read_methods(path):
    api, session, _ = client([])
    with pytest.raises(ExportError):
        api.get(path)
    assert not session.calls


def test_supplied_customlist_read_path():
    api, session, _ = client([Response(data={"response": {"items": []}})])
    assert api.get("customlists/lists/3/items") == {"items": []}
    assert session.calls[0][0].endswith("/customlists/lists/3/items")
    assert set(session.calls[0][1]["params"]) == {"api_key"}


def test_module_authorization_error_is_explicit_and_sanitized():
    api, _, _ = client([Response(data={"error": {"error_code": 22,
                       "error_msg": "Apikey has not been granted to the module"}})])
    with pytest.raises(ExportError, match="not granted access.*code 22"):
        api.get("customlists/lists/3/items")


@pytest.mark.parametrize("path", ["customlists/lists/3/delete", "customlists/lists/3/items/delete",
                                  "customlists/lists/../items", "customlists/lists/3/items?delete=1"])
def test_customlist_write_or_malformed_paths_rejected(path):
    api, session, _ = client([])
    with pytest.raises(ExportError):
        api.get(path)
    assert not session.calls


def test_customlist_uses_verified_standard_pagination():
    api, session, _ = client([
        Response(data={"response": {"page": 1, "count": 1, "total_result": 2, "items": [{"id": 1}]}}),
        Response(data={"response": {"page": 2, "count": 1, "total_result": 2, "items": [{"id": 2}]}}),
    ])
    assert [r["id"] for r in api.records("customlists/lists/3/items")] == [1, 2]
    assert len(session.calls) == 2
