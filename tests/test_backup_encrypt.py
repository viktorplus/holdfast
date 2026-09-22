from pathlib import Path

import pytest
from support import config

from holdfast.backup import BackupError
from holdfast.backup.encrypt import Encryption

AGE = "age1" + "qy" * 29
AGE_SPARE = "age1" + "zx" * 29
# Assembled rather than written out: forty hex characters are indistinguishable
# from a real token, and the privacy guard is right to stop one. Same reasoning
# as the checksums in the audit fixtures, recorded in CONTRIBUTING.md.
GPG = ("0123456789ABCDEF" * 3)[:40]


def test_the_fixtures_are_the_shape_the_tools_actually_use():
    """Guards the fixtures themselves, since they are assembled, not written."""
    assert len(AGE) == 62
    assert len(AGE_SPARE) == 62
    assert len(GPG) == 40


def test_encryption_off_changes_nothing():
    enc = Encryption.from_config(config(**{"encryption.enabled": False}))

    assert enc.enabled is False
    assert enc.suffix == ""
    assert enc.wrap("pg_dump signal") == "pg_dump signal"
    assert enc.describe()["enabled"] is False


def test_encryption_on_without_a_recipient_refuses_to_produce_anything():
    """Fail closed. There is no branch here that writes a plaintext archive."""
    with pytest.raises(BackupError, match="recipient"):
        Encryption.from_config(config())


def test_the_list_and_the_file_add_up(tmp_path: Path):
    listed = tmp_path / "recipients"
    listed.write_text(
        f"# the spare key, kept offline\n{AGE_SPARE}\n\n", encoding="utf-8"
    )
    enc = Encryption.from_config(
        config(
            **{
                "encryption.recipients": [AGE],
                "encryption.recipients_file": str(listed),
            }
        )
    )

    assert enc.recipients == (AGE, AGE_SPARE)


def test_a_recipients_file_that_is_not_there_is_an_error(tmp_path: Path):
    """Silently reading nothing here means encrypting to nobody."""
    with pytest.raises(BackupError, match="recipients_file"):
        Encryption.from_config(
            config(**{"encryption.recipients_file": str(tmp_path / "gone")})
        )


@pytest.mark.parametrize(
    "recipient",
    ["age1short", "AGE1" + "qy" * 29, "age2" + "qy" * 29, "ssh-ed25519 AAAA"],
)
def test_an_age_recipient_that_is_not_one_is_refused(recipient: str):
    with pytest.raises(BackupError, match="age1"):
        Encryption.from_config(config(**{"encryption.recipients": [recipient]}))


def test_a_full_gpg_fingerprint_is_accepted():
    enc = Encryption.from_config(
        config(**{"encryption.tool": "gpg", "encryption.recipients": [GPG]})
    )

    assert enc.suffix == ".gpg"
    assert enc.recipients == (GPG,)


@pytest.mark.parametrize("recipient", ["0123456789ABCDEF", "not-a-key", "0x123"])
def test_a_short_gpg_key_id_is_refused(recipient: str):
    """Short key ids collide by construction, and a collision here encrypts the
    backup to someone else."""
    with pytest.raises(BackupError, match="fingerprint"):
        Encryption.from_config(
            config(**{"encryption.tool": "gpg", "encryption.recipients": [recipient]})
        )


def test_an_unknown_tool_is_refused():
    with pytest.raises(BackupError, match="age"):
        Encryption.from_config(
            config(**{"encryption.tool": "openssl", "encryption.recipients": [AGE]})
        )


def test_a_passphrase_is_refused_with_a_reason():
    """Likely to be carried over from backup_s1.conf, so it says why not."""
    with pytest.raises(BackupError, match="passphrase"):
        Encryption.from_config(
            config(
                **{
                    "encryption.tool": "gpg",
                    "encryption.mode": "passphrase",
                    "encryption.recipients": [GPG],
                }
            )
        )


def test_the_pipeline_encrypts_to_every_recipient():
    enc = Encryption.from_config(config(**{"encryption.recipients": [AGE, AGE_SPARE]}))
    line = enc.wrap("tar -cf - -C / opt")

    assert "tar -cf - -C / opt" in line
    assert f"-r {AGE}" in line
    assert f"-r {AGE_SPARE}" in line
    assert "age --encrypt" in line


def test_the_pipeline_carries_no_shell_option_of_its_own():
    """pipefail belongs to the runner, which starts bash with it.

    Written into the line, it made dash - /bin/sh on Ubuntu - refuse every
    encrypted artifact with "Illegal option -o pipefail".
    """
    enc = Encryption.from_config(config(**{"encryption.recipients": [AGE]}))

    assert enc.wrap("pg_dump signal") == f"pg_dump signal | age --encrypt -r {AGE}"


def test_the_pipeline_writes_to_stdout_and_never_to_a_file():
    """The engine opens the destination, so nothing here has to be quoted."""
    enc = Encryption.from_config(config(**{"encryption.recipients": [AGE]}))

    assert ">" not in enc.wrap("tar -cf - -C / opt")


def test_a_recipient_reaches_the_shell_quoted():
    enc = Encryption(
        enabled=True, tool="age", recipients=("age1 ; rm -rf /",), suffix=".age"
    )

    assert "'age1 ; rm -rf /'" in enc.wrap("printf x")


def test_the_manifest_learns_nothing_beyond_the_encryption():
    enc = Encryption.from_config(config(**{"encryption.recipients": [AGE]}))

    assert enc.describe() == {
        "enabled": True,
        "tool": "age",
        "suffix": ".age",
        "recipients": [AGE],
    }
