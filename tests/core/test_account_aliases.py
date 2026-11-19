"""Tests for account names (WORKSPACE_MCP_ACCOUNTS_FILE)."""

import json
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

import core.account_alias_middleware as alias_middleware
import core.server as server_module
from core import accounts as accounts_module
from core import tool_registry
from core.account_alias_middleware import AccountAliasMiddleware
from core.accounts import (
    ACCOUNTS_FILE_ENV,
    AccountsConfigError,
    UnknownAccountError,
    get_accounts,
    parse_accounts,
)
from core.camel_case_middleware import CamelCaseArgumentsMiddleware

ACCOUNTS = {"work": "me@work.example.com", "personal": "Me@Home.example.org"}


@pytest.fixture
def accounts_file(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(ACCOUNTS))
    monkeypatch.setenv(ACCOUNTS_FILE_ENV, str(path))
    accounts_module.reset_accounts_cache()
    yield path
    accounts_module.reset_accounts_cache()


@pytest.fixture
def legacy_oauth(monkeypatch):
    for module in (alias_middleware, server_module):
        monkeypatch.setattr(module, "is_oauth21_enabled", lambda: False)
        monkeypatch.setattr(module, "is_trust_gateway_identity", lambda: False)


# --- parsing and resolving ---------------------------------------------------


def test_resolve_names_and_emails():
    accounts = parse_accounts(ACCOUNTS, "test")
    assert accounts.names == ["work", "personal"]
    assert accounts.resolve("work") == "me@work.example.com"
    # Emails from the file are stored lowercase.
    assert accounts.resolve(" Personal ") == "me@home.example.org"
    # Email addresses pass through unchanged, named or not.
    assert accounts.resolve("someone@else.example") == "someone@else.example"


@pytest.mark.parametrize("value", ["work@", "@work", "a@b", "two words@x.co"])
def test_malformed_email_is_rejected(value):
    accounts = parse_accounts(ACCOUNTS, "test")
    with pytest.raises(UnknownAccountError, match="neither a configured"):
        accounts.resolve(value)


def test_unknown_name_lists_configured_names():
    accounts = parse_accounts(ACCOUNTS, "test")
    with pytest.raises(UnknownAccountError, match="work, personal"):
        accounts.resolve("wrok")


@pytest.mark.parametrize(
    "data, message",
    [
        ([], "JSON object"),
        ({}, "no accounts"),
        ({"has space": "a@b.co"}, "invalid account name"),
        ({"a@b": "a@b.co"}, "invalid account name"),
        ({"-lead": "a@b.co"}, "invalid account name"),
        ({"work": "not-an-email"}, "must map to an email"),
        ({"work": 42}, "must map to an email"),
        ({"Work": "a@b.co", "work": "c@d.co"}, "more than once"),
        ({"one": "a@b.co", "two": "A@B.co"}, "same email"),
    ],
)
def test_parse_rejects_bad_files(data, message):
    with pytest.raises(AccountsConfigError, match=message):
        parse_accounts(data, "test")


def test_get_accounts_without_env_is_none(monkeypatch):
    monkeypatch.delenv(ACCOUNTS_FILE_ENV, raising=False)
    accounts_module.reset_accounts_cache()
    try:
        assert get_accounts() is None
    finally:
        accounts_module.reset_accounts_cache()


def test_get_accounts_reports_missing_and_malformed_files(tmp_path, monkeypatch):
    monkeypatch.setenv(ACCOUNTS_FILE_ENV, str(tmp_path / "missing.json"))
    accounts_module.reset_accounts_cache()
    with pytest.raises(AccountsConfigError, match="not found"):
        get_accounts()

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(ACCOUNTS_FILE_ENV, str(bad))
    accounts_module.reset_accounts_cache()
    with pytest.raises(AccountsConfigError, match="could not read"):
        get_accounts()
    # The error is remembered: fixing the file needs a restart, not a retry.
    bad.write_text(json.dumps(ACCOUNTS))
    with pytest.raises(AccountsConfigError, match="could not read"):
        get_accounts()

    latin1 = tmp_path / "latin1.json"
    latin1.write_bytes('{"caf\u00e9": "a@b.co"}'.encode("latin-1"))
    monkeypatch.setenv(ACCOUNTS_FILE_ENV, str(latin1))
    accounts_module.reset_accounts_cache()
    with pytest.raises(AccountsConfigError, match="could not read"):
        get_accounts()

    dupes = tmp_path / "dupes.json"
    dupes.write_text('{"work": "a@b.co", "work": "c@d.co"}')
    monkeypatch.setenv(ACCOUNTS_FILE_ENV, str(dupes))
    accounts_module.reset_accounts_cache()
    with pytest.raises(AccountsConfigError, match="'work' appears more than once"):
        get_accounts()
    accounts_module.reset_accounts_cache()


# --- middleware --------------------------------------------------------------


def _build_server() -> FastMCP:
    server = FastMCP("alias-test")
    server.add_middleware(CamelCaseArgumentsMiddleware())
    server.add_middleware(AccountAliasMiddleware())

    @server.tool
    def whoami(user_google_email: str, query: str = "") -> str:
        return user_google_email

    @server.tool
    def no_account(query: str = "") -> str:
        return query

    return server


@pytest.mark.asyncio
async def test_call_tool_swaps_name_for_email(accounts_file, legacy_oauth):
    async with Client(_build_server()) as client:
        by_name = await client.call_tool("whoami", {"user_google_email": "work"})
        camel = await client.call_tool("whoami", {"userGoogleEmail": "personal"})
        by_email = await client.call_tool(
            "whoami", {"user_google_email": "other@example.com"}
        )
    assert by_name.content[0].text == "me@work.example.com"
    assert camel.content[0].text == "me@home.example.org"
    assert by_email.content[0].text == "other@example.com"


@pytest.mark.asyncio
async def test_call_tool_rejects_unknown_name(accounts_file, legacy_oauth):
    async with Client(_build_server()) as client:
        with pytest.raises(ToolError, match="Unknown Google account name 'nope'"):
            await client.call_tool("whoami", {"user_google_email": "nope"})
        with pytest.raises(ToolError, match="neither a configured"):
            await client.call_tool("whoami", {"user_google_email": "work@"})


@pytest.mark.asyncio
async def test_call_tool_reports_bad_accounts_file(tmp_path, monkeypatch, legacy_oauth):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(ACCOUNTS_FILE_ENV, str(bad))
    accounts_module.reset_accounts_cache()
    try:
        async with Client(_build_server()) as client:
            with pytest.raises(ToolError, match="misconfigured"):
                await client.call_tool("whoami", {"user_google_email": "work"})
    finally:
        accounts_module.reset_accounts_cache()


@pytest.mark.asyncio
async def test_list_tools_describes_names(accounts_file, legacy_oauth):
    async with Client(_build_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    description = tools["whoami"].input_schema["properties"]["user_google_email"][
        "description"
    ]
    assert "work, personal" in description
    assert "user_google_email" not in tools["no_account"].input_schema["properties"]


@pytest.mark.asyncio
async def test_names_ignored_in_oauth21_mode(accounts_file, monkeypatch):
    monkeypatch.setattr(alias_middleware, "is_oauth21_enabled", lambda: True)
    monkeypatch.setattr(alias_middleware, "is_trust_gateway_identity", lambda: False)
    async with Client(_build_server()) as client:
        result = await client.call_tool("whoami", {"user_google_email": "work"})
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert result.content[0].text == "work"
    schema = tools["whoami"].input_schema["properties"]["user_google_email"]
    assert "description" not in schema


@pytest.mark.asyncio
async def test_no_accounts_file_changes_nothing(monkeypatch, legacy_oauth):
    monkeypatch.delenv(ACCOUNTS_FILE_ENV, raising=False)
    accounts_module.reset_accounts_cache()
    async with Client(_build_server()) as client:
        result = await client.call_tool("whoami", {"user_google_email": "work"})
    assert result.content[0].text == "work"
    accounts_module.reset_accounts_cache()


# --- list_google_accounts ----------------------------------------------------


def _fake_store(monkeypatch, users):
    store = SimpleNamespace(list_users=lambda: list(users))
    monkeypatch.setattr(server_module, "get_credential_store", lambda: store)


@pytest.mark.asyncio
async def test_list_google_accounts_shows_names_and_logins(
    accounts_file, legacy_oauth, monkeypatch
):
    _fake_store(monkeypatch, ["Me@Home.example.org", "stray@example.com"])
    text = await server_module.list_google_accounts()
    assert "- work: me@work.example.com (no saved login yet)" in text
    assert "- personal: me@home.example.org (saved login)" in text
    # Logins with no name aren't listed: on a shared server they belong to
    # whoever else uses it.
    assert "stray@example.com" not in text


@pytest.mark.asyncio
async def test_list_google_accounts_without_store(
    accounts_file, legacy_oauth, monkeypatch
):
    def broken():
        raise RuntimeError("backend can't list users")

    monkeypatch.setattr(server_module, "get_credential_store", broken)
    text = await server_module.list_google_accounts()
    assert "- work: me@work.example.com (login status unknown)" in text


@pytest.mark.asyncio
async def test_list_google_accounts_with_nothing_configured(monkeypatch, legacy_oauth):
    monkeypatch.delenv(ACCOUNTS_FILE_ENV, raising=False)
    accounts_module.reset_accounts_cache()
    _fake_store(monkeypatch, ["someone@example.com"])
    text = await server_module.list_google_accounts()
    assert text.startswith("No Google account names are configured")
    assert "someone@example.com" not in text
    accounts_module.reset_accounts_cache()


@pytest.mark.asyncio
async def test_list_google_accounts_in_oauth21_mode(accounts_file, monkeypatch):
    monkeypatch.setattr(server_module, "is_oauth21_enabled", lambda: True)
    text = await server_module.list_google_accounts()
    assert "nothing to list" in text


def test_toolsets_header_keeps_service_independent_tools(monkeypatch):
    import core.toolset_header_middleware as toolset_middleware

    monkeypatch.setattr(
        toolset_middleware,
        "get_http_headers",
        lambda: {"x-mcp-toolsets": "drive"},
    )
    allowed = toolset_middleware.requested_tools()
    assert "list_google_accounts" in allowed
    assert "search_drive_files" in allowed

    # An explicit tool list stays exactly what the client asked for.
    monkeypatch.setattr(
        toolset_middleware,
        "get_http_headers",
        lambda: {"x-mcp-tools": "search_drive_files"},
    )
    assert toolset_middleware.requested_tools() == {"search_drive_files"}


def test_oauth21_removes_list_google_accounts(monkeypatch):
    monkeypatch.setattr(tool_registry, "is_oauth21_enabled", lambda: True)
    monkeypatch.setattr(tool_registry, "is_permissions_mode", lambda: False)
    monkeypatch.setattr(tool_registry, "is_read_only_mode", lambda: False)
    removed = []
    components = {
        f"tool:{name}@1": SimpleNamespace()
        for name in ("search_drive_files", "list_google_accounts")
    }
    provider = SimpleNamespace(
        _components=components, remove_tool=lambda name: removed.append(name)
    )
    tool_registry.set_enabled_tools(None)
    tool_registry.set_disabled_tools(set())
    tool_registry.filter_server_tools(SimpleNamespace(local_provider=provider))
    assert removed == ["list_google_accounts"]


def test_tier_filtering_keeps_list_google_accounts(monkeypatch):
    monkeypatch.setattr(tool_registry, "is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(tool_registry, "is_permissions_mode", lambda: False)
    monkeypatch.setattr(tool_registry, "is_read_only_mode", lambda: False)
    removed = []
    components = {
        f"tool:{name}@1": SimpleNamespace()
        for name in (
            "search_gmail_messages",
            "send_gmail_message",
            "list_google_accounts",
        )
    }
    provider = SimpleNamespace(
        _components=components, remove_tool=lambda name: removed.append(name)
    )
    tool_registry.set_enabled_tools({"search_gmail_messages"})
    tool_registry.set_disabled_tools(set())
    try:
        tool_registry.filter_server_tools(SimpleNamespace(local_provider=provider))
    finally:
        tool_registry.set_enabled_tools(None)
    assert removed == ["send_gmail_message"]
