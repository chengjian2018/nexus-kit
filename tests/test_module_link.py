"""Tests for ModuleLink normalization and module header projection."""

from nexus.model.module import AgentModule, FSMModule, ModuleLink


def test_str_link_normalized_with_knowledge_default():
    mod = AgentModule(module_code="a", sub_modules=["b"])
    assert len(mod.sub_modules) == 1
    link = mod.sub_modules[0]
    assert isinstance(link, ModuleLink)
    assert link.target == "b"
    assert link.lend_knowledge is True   # the legacy string form lends knowledge by default
    assert link.lend_tools == []


def test_modulelink_direct_config():
    link = ModuleLink(target="b", lend_knowledge=False, lend_tools=["t1"])
    assert link.lend_tools == ["t1"]
    mod = AgentModule(module_code="a", sub_modules=[link, "c"])
    assert mod.sub_modules[0] is link
    assert mod.sub_modules[1].target == "c"


def test_modulelink_defaults():
    link = ModuleLink(target="b")
    assert link.lend_knowledge is True
    assert link.lend_tools == []


def test_answer_examples_field():
    mod = AgentModule(module_code="a", answer_examples=["好的，已为您改到{time}。"])
    assert mod.answer_examples == ["好的，已为您改到{time}。"]
    mod2 = AgentModule(module_code="b")
    assert mod2.answer_examples == []


def test_to_projection_text_full():
    mod = FSMModule(
        module_code="after_sales",
        module_name="售后维保",
        module_description="保养预约、维修工单、保险理赔的查询与办理",
        module_todo_description="查改保养预约、跟踪维修工单进度",
        answer_examples=["已为您把保养预约改到{时间}，请按时到店。"],
    )
    text = mod.to_projection_text()
    assert "售后维保" in text
    assert "保养预约" in text
    assert "查改保养预约" in text
    assert "已为您把保养预约改到" in text


def test_to_projection_text_empty_module():
    mod = AgentModule(module_code="x")
    assert mod.to_projection_text() == ""
