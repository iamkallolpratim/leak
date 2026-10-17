import pytest
from pydantic import BaseModel

from leak.harness import ToolRegistry, json_schema_for


class Inner(BaseModel):
    currency: str
    amount: float


class Outer(BaseModel):
    title: str  # a *field* called title must survive title-stripping
    items: list[Inner]
    note: Inner | None = None


def _no_refs(node):
    if isinstance(node, dict):
        assert "$ref" not in node and "$defs" not in node
        assert "title" not in node
        for k, v in node.items():
            # keys under "properties" are field names, so recurse into values only
            for sub in v.values() if k == "properties" else [v]:
                _no_refs(sub)
    elif isinstance(node, list):
        for x in node:
            _no_refs(x)


def test_schema_is_inlined_and_clean():
    s = json_schema_for(Outer)
    _no_refs(s)
    assert set(s["properties"]) == {"title", "items", "note"}
    assert s["properties"]["items"]["items"]["properties"]["currency"]["type"] == "string"
    assert s["required"] == ["title", "items"]


def test_tool_must_take_one_model():
    reg = ToolRegistry()
    with pytest.raises(TypeError):

        @reg.tool(description="bad")
        def bad(a: int, b: int) -> int:
            return a + b


def test_duplicate_names_rejected():
    reg = ToolRegistry()

    @reg.tool(description="x")
    def t(args: Inner) -> None: ...

    def t2(args: Inner) -> None: ...

    with pytest.raises(ValueError):
        reg.tool(description="y", name="t")(t2)
