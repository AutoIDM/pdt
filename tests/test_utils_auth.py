from email.message import Message

from pdt.utils.auth import EMAIL_HEADER, USER_HEADER, current_user


def test_the_email_header_wins():
    headers = Message()
    headers[EMAIL_HEADER] = " ann@example.com "
    headers[USER_HEADER] = "1234"
    assert current_user(headers) == "ann@example.com"


def test_the_user_header_is_the_fallback():
    assert current_user({USER_HEADER: "1234"}) == "1234"
    assert current_user({"x-forwarded-user": "5678"}) == "5678"


def test_no_proxy_means_no_user():
    assert current_user({}) == ""
    assert current_user(Message()) == ""


def test_each_platform_header_is_read():
    assert current_user({"X-Goog-Authenticated-User-Email": "accounts.google.com:bo@example.com"}) \
        == "bo@example.com"
    assert current_user({"x-ms-client-principal-name": "cy@example.com"}) == "cy@example.com"
