"""
Middleware that lets callers name a Google account instead of giving its email.

See core/accounts.py for the accounts file format.
"""

import logging
from typing import Sequence

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool

from auth.oauth_config import is_oauth21_enabled, is_trust_gateway_identity
from core.accounts import AccountsConfigError, UnknownAccountError, get_accounts

logger = logging.getLogger(__name__)

EMAIL_PARAM = "user_google_email"


def _names_apply() -> bool:
    # In OAuth 2.1 and gateway modes the verified sign-in picks the account and
    # user_google_email is overridden or removed, so names have nothing to do.
    return not (is_oauth21_enabled() or is_trust_gateway_identity())


class AccountAliasMiddleware(Middleware):
    """Swap account names for email addresses in ``user_google_email``.

    Must run after CamelCaseArgumentsMiddleware so ``userGoogleEmail`` has
    already been renamed to ``user_google_email``.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        arguments = context.message.arguments
        try:
            accounts = get_accounts() if _names_apply() else None
        except AccountsConfigError as exc:
            # Only reachable when the server was started without the startup
            # check in main.py / fastmcp_server.py.
            raise ToolError(f"Google account names are misconfigured: {exc}") from exc
        if accounts is not None and arguments:
            value = arguments.get(EMAIL_PARAM)
            if isinstance(value, str) and value:
                try:
                    email = accounts.resolve(value)
                except UnknownAccountError as exc:
                    raise ToolError(str(exc)) from exc
            else:
                email = value
            if email != value:
                logger.debug(
                    "[AccountAliasMiddleware] %s: account name %r -> %s",
                    context.message.name,
                    value,
                    email,
                )
                context = context.copy(
                    message=context.message.model_copy(
                        update={"arguments": {**arguments, EMAIL_PARAM: email}}
                    )
                )
        return await call_next(context)

    async def on_list_tools(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        accounts = get_accounts() if _names_apply() else None
        if accounts is None:
            return tools

        description = (
            "Google account to act as: one of the account names "
            f"{', '.join(accounts.names)}, or a Google email address. "
            "list_google_accounts shows which email each name stands for."
        )
        patched = []
        for tool in tools:
            properties = (tool.parameters or {}).get("properties") or {}
            if EMAIL_PARAM not in properties:
                patched.append(tool)
                continue
            new_properties = dict(properties)
            new_properties[EMAIL_PARAM] = {
                **properties[EMAIL_PARAM],
                "description": description,
            }
            parameters = {**tool.parameters, "properties": new_properties}
            patched.append(tool.model_copy(update={"parameters": parameters}))
        return patched
