from botocore.exceptions import ClientError

from pdt.deploy_aws import not_found


def client_error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Describe")


def test_a_missing_task_definition_family_counts_as_not_found():
    assert not_found(client_error("ClientException", "Unable to describe task definition."))


def test_other_ecs_client_exceptions_still_raise():
    assert not not_found(client_error("ClientException", "Invalid revision number."))


def test_the_named_not_found_codes_still_count():
    assert not_found(client_error("ResourceNotFoundException"))
