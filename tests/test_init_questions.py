import pytest

from pdt.scaffold import PROVIDER_CHOICES, PROVIDER_QUESTIONS


def keys(provider):
    return [key for key, _q, _default, _check in PROVIDER_QUESTIONS[provider]]


def test_every_provider_choice_has_questions_defined():
    for provider, _label in PROVIDER_CHOICES:
        assert provider in PROVIDER_QUESTIONS


def test_azure_asks_only_for_the_region():
    assert keys("azure") == ["region"]


@pytest.mark.parametrize("provider,expected", [
    ("aws", ["region"]),
    ("google-cloud", ["region"]),
    ("windows", []),
    ("", []),
])
def test_the_other_providers_ask_only_what_pdt_cannot_supply(provider, expected):
    assert keys(provider) == expected


@pytest.mark.parametrize("provider", list(PROVIDER_QUESTIONS))
def test_no_question_accepts_an_empty_answer(provider):
    for key, _q, _default, check in PROVIDER_QUESTIONS[provider]:
        assert check("") != "", f"{provider}.{key} accepts an empty answer"


@pytest.mark.parametrize("provider", list(PROVIDER_QUESTIONS))
def test_every_question_default_passes_its_own_check(provider):
    for key, _q, default, check in PROVIDER_QUESTIONS[provider]:
        if default() != "":
            assert check(default()) == "", f"{provider}.{key} offers a default it rejects"
