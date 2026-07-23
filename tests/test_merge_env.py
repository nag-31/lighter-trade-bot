import os
from pathlib import Path

from scripts.merge_env import merge_env


def test_merge_env_replaces_matching_adds_new_and_preserves_remote_only(
    tmp_path: Path,
):
    target = tmp_path / ".env"
    incoming = tmp_path / "incoming.env"
    target.write_text(
        "# live\nSHARED=old\nREMOTE_ONLY=keep\n",
        encoding="utf-8",
    )
    incoming.write_text(
        "# local\nSHARED=new\nLOCAL_NEW=added\n",
        encoding="utf-8",
    )
    target.chmod(0o600)

    replaced, added = merge_env(target, incoming)

    assert (replaced, added) == (1, 1)
    assert target.read_text(encoding="utf-8") == (
        "# live\n"
        "SHARED=new\n"
        "REMOTE_ONLY=keep\n"
        "\n"
        "# Added from the latest local .env deployment\n"
        "LOCAL_NEW=added\n"
    )
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_merge_env_does_not_print_or_transform_secret_values(tmp_path: Path):
    target = tmp_path / ".env"
    incoming = tmp_path / "incoming.env"
    target.write_text("TOKEN=old\n", encoding="utf-8")
    incoming.write_text("TOKEN=a=b=c#still-value\n", encoding="utf-8")

    merge_env(target, incoming)

    assert target.read_text(encoding="utf-8") == "TOKEN=a=b=c#still-value\n"
