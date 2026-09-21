"""The Skills extension, alongside the existing resource and tool surfaces.

A skill:// resource is not itself an extension declaration. Skills-aware
clients discover complete entries through skills/list and skills/get, then
verify each file against that entry before loading it. Catalogue remains the
authority for scope and content; a getter follows every snapshot replacement.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from fastmcp.server.extensions import MethodBinding, ServerExtension
from fastmcp.server.middleware import Middleware
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, PaginatedRequestParams, RequestParams
from pydantic import Field

from ..catalogue.uris import Catalogue
from .request import requested_scope
from .scope import Scope

EXTENSION_ID = "io.modelcontextprotocol/skills"


class GetSkillParams(RequestParams):
    uri: str = Field(min_length=1)


class AdvertiseLegacySkills(Middleware):
    """Keep the declaration in initialize for handshake-era clients.

    FastMCP 4 advertises registered extensions through server/discover, but
    its SDK's legacy serializer removes that capability from initialize.
    This public middleware hook restores our extension after serialization;
    otherwise existing Skills clients see a server with no Skills support.
    """

    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        if result is not None:
            result.capabilities.extensions = {
                **(result.capabilities.extensions or {}),
                EXTENSION_ID: {},
            }
        return result


class SkillsExtension(ServerExtension):
    """Publish complete scoped entries without expanding resources/list."""

    identifier = EXTENSION_ID

    def __init__(
        self,
        catalogue: Callable[[], Catalogue],
        scope_problem: Callable[[Scope], str | None],
    ):
        self._catalogue = catalogue
        self._scope_problem = scope_problem

    def methods(self) -> Sequence[MethodBinding]:
        return (
            MethodBinding("skills/list", PaginatedRequestParams, self.list_skills),
            MethodBinding("skills/get", GetSkillParams, self.get_skill),
        )

    def _scope(self) -> Scope:
        # Keep extension requests under the same invalid-scope contract as
        # resources and prompts, including direct handler invocation.
        scope = requested_scope()
        problem = self._scope_problem(scope)
        if problem:
            raise MCPError(code=INVALID_PARAMS, message=problem)
        return scope

    @staticmethod
    def _result(**fields: object) -> dict:
        # Local/live sources can change between calls. Clients may retain an
        # entry for verification, but must not reuse it as a fresh listing.
        return {
            "resultType": "complete",
            "ttlMs": 0,
            "cacheScope": "private",
            **fields,
        }

    async def list_skills(self, ctx, params: PaginatedRequestParams) -> dict:
        scope = self._scope()
        if params.cursor is not None:
            raise MCPError(code=INVALID_PARAMS, message="Invalid skills cursor")
        catalogue = self._catalogue()
        entries = []
        for uri in catalogue.skill_uris(scope):
            entry = catalogue.skill_entry(uri, scope)
            if entry is not None:
                entries.append(entry)
        return self._result(skills=entries)

    async def get_skill(self, ctx, params: GetSkillParams) -> dict:
        scope = self._scope()
        entry = self._catalogue().skill_entry(params.uri, scope)
        if entry is None:
            # Out-of-scope and absent skills have exactly the same error.
            raise MCPError(code=INVALID_PARAMS, message="Skill not found")
        return self._result(skill=entry)
