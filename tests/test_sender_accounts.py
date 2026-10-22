import pytest

from outreach_agent.sender_accounts import add_account, list_accounts, set_default


def test_sender_account_is_persistent_but_fail_closed_until_oauth(tmp_path):
    path = tmp_path / "registry.db"
    account = add_account("researcher@example.com", display_name="Researcher", path=path)
    assert account["connection_status"] == "NOT_CONNECTED"
    assert list_accounts(path)[0]["email"] == "researcher@example.com"
    with pytest.raises(PermissionError):
        set_default(account["account_id"], path=path)
