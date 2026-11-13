from email.message import Message

from pdt.utils.auth import IAP_EMAIL_HEADER, current_user


def test_the_iap_header_names_the_visitor():
    headers = Message()
    headers[IAP_EMAIL_HEADER] = " accounts.google.com:bo@example.com "
    assert current_user(headers) == "bo@example.com"
    assert current_user({IAP_EMAIL_HEADER.lower(): "accounts.google.com:cy@example.com"}) \
        == "cy@example.com"


def test_no_gate_means_no_user():
    assert current_user({}) == ""
    assert current_user(Message()) == ""


def test_a_header_the_visitor_can_send_names_no_one():
    headers = Message()
    headers["X-Forwarded-Email"] = "admin@example.com"
    headers[IAP_EMAIL_HEADER] = "accounts.google.com:bo@example.com"
    assert current_user(headers) == "bo@example.com"
    assert current_user({"X-Forwarded-Email": "admin@example.com"}) == ""
