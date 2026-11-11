from conftest import add_app
from pdt.config import validate_app


def test_a_profile_in_the_platform_block_validates_clean(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n  profile: work\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert validate_app("my-report") == []
