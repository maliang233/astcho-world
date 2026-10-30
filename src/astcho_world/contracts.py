from __future__ import annotations

import re
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Tool(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    effect: Literal["read", "write"] = "read"
    timeout: float = Field(default=10, gt=0, le=120)
    observed_url_fields: list[str] = Field(default_factory=list)
    idempotency_field: str | None = None

    @model_validator(mode="after")
    def declared_fields(self):
        fields = set(self.observed_url_fields)
        if self.idempotency_field:
            fields.add(self.idempotency_field)
        if not fields <= self.input_schema.get("properties", {}).keys():
            raise ValueError("Resource and intent fields must exist in the tool schema")
        return self

    @field_validator("input_schema", "output_schema")
    @classmethod
    def valid_schema(cls, value):
        Draft202012Validator.check_schema(value)
        if value.get("type") != "object":
            raise ValueError("Tool schemas must describe objects")
        return value


class DashboardView(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    source: Literal["operations", "observations", "notifications"]
    component: Literal["table", "cards"] = "table"
    columns: dict[str, str] = Field(default_factory=dict)


class Dashboard(Contract):
    version: Literal[1] = 1
    name: str
    icon: str = "◇"
    views: list[DashboardView] = Field(default_factory=list)
    activities: dict[str, DisplayActivity] = Field(default_factory=dict)


class DisplayActivity(Contract):
    kind: str = Field(pattern=r"^activity\.[a-z][a-z0-9_.-]{0,79}$")
    label: str = Field(min_length=1, max_length=100)
    fallback: str = Field(default="base.idle", pattern=r"^(base|activity)\.[a-z][a-z0-9_.-]{0,79}$")
    hold_seconds: int = Field(default=20, ge=0, le=60)


class Manifest(Contract):
    app_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str
    protocol: Literal[1] = 1
    description: str
    entrypoint: str
    tools: list[Tool]
    dashboard: Dashboard | None = None
    notification_tool: str | None = None
    events: list[str] = Field(default_factory=list)
    ambient_schema: dict[str, Any] | None = None
    admin_tools: list[str] = Field(default_factory=list)

    @field_validator("entrypoint")
    @classmethod
    def valid_entrypoint(cls, value):
        if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*", value):
            raise ValueError("Expected module:factory")
        return value

    @model_validator(mode="after")
    def unique_tools(self):
        if self.app_id == "world" or "__" in self.app_id:
            raise ValueError("Reserved application ID")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("Duplicate tool names")
        if self.notification_tool and self.notification_tool not in {t.name for t in self.tools}:
            raise ValueError("Notification tool must be declared")
        if not set(self.admin_tools) <= {t.name for t in self.tools}:
            raise ValueError("Administrative tools must be declared")
        if self.dashboard:
            if not self.dashboard.activities.keys() <= {t.name for t in self.tools}:
                raise ValueError("Display activities must refer to declared tools")
            ids = [view.id for view in self.dashboard.views]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate dashboard view IDs")
        return self

    def tool(self, name: str) -> Tool:
        return next(tool for tool in self.tools if tool.name == name)


class Result(Contract):
    operation_id: str
    status: Literal["succeeded", "failed", "unknown", "cancelled", "running"]
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def object_schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }
