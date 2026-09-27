from copy import deepcopy

import pytest

from ankisense.figure_context import compose_context


def diagram(box=None):
    return {"bbox": box or [.1, .2, .45, .7], "kind": "picture", "caption_refs": []}


def text(box, value, label="text", ref="text-1"):
    return {"bbox": box, "text": value, "label": label, "ref": ref}


def test_adjacent_formula_and_caption_are_preserved_without_mutating_layout():
    region = diagram()
    region["caption_refs"] = ["caption-1"]
    page = {"regions": [region], "text_regions": [
        text([.12, .72, .43, .75], "(a) 栈示意图", ref="caption-1"),
        text([.14, .79, .43, .82], "栈顶元素 = s[top]")]}
    before = deepcopy(page)
    result = compose_context(region, page, "栈顶元素如何表示？ s[top]")
    assert result["bbox"] == [.085, .185, .465, .835]
    assert result["original_bbox"] == region["bbox"]
    assert len(result["context_notes"]) == 2
    assert page == before


def test_unrelated_second_column_and_distant_false_caption_are_excluded():
    region = diagram()
    region["caption_refs"] = ["bad-caption"]
    page = {"regions": [region], "text_regions": [
        text([.48, .35, .85, .5], "树的深度与结点的层次有关"),
        text([.52, .73, .95, .8], "图 2 队列", label="caption", ref="bad-caption")]}
    result = compose_context(region, page, "栈顶位置在哪里？")
    assert result["bbox"] == [.085, .185, .465, .715]


def test_specific_side_context_includes_split_line_fragments():
    region = diagram([.4, .3, .95, .85])
    page = {"regions": [region], "text_regions": [
        text([.13, .6, .38, .65], "θ1=#，为开始符"),
        text([.13, .7, .21, .75], "θ2=#"),
        text([.23, .7, .38, .75], "为结束符")]}
    result = compose_context(region, page, "开始符和结束符怎么表示？")
    assert result["bbox"] == [.115, .285, .965, .865]
    assert "context_unresolved" not in result["quality_flags"]
    assert any("开始符" in note for note in result["context_notes"])


def test_expansion_does_not_sweep_an_independent_diagram():
    region = diagram([.4, .3, .95, .85])
    page = {"regions": [region, diagram([.15, .32, .37, .54])], "text_regions": [
        text([.13, .6, .38, .65], "θ1=#，为开始符")]}
    result = compose_context(region, page, "开始符怎么表示？")
    assert result["bbox"] == [.385, .285, .965, .865]
    assert "context_unresolved" in result["quality_flags"]


def test_footer_formula_kept_but_not_unrelated_header_or_corner_ornament():
    region = diagram([.08, .3, .44, .79])
    page = {"regions": [region, diagram([.01, .75, .25, .98])], "text_regions": [
        text([.13, .88, .43, .92], "栈顶元素=s[top]", label="page_footer"),
        text([.1, .23, .43, .28], "课程名称", label="page_header")]}
    result = compose_context(region, page, "栈顶元素 s[top]")
    assert result["bbox"] == [.065, .285, .455, .935]


def test_combining_two_safe_extensions_cannot_enclose_another_figure():
    region = diagram([.4, .4, .8, .8])
    page = {"regions": [region, diagram([.21, .23, .39, .39])], "text_regions": [
        text([.41, .33, .79, .39], "s[top]=1", label="formula"),
        text([.22, .41, .39, .45], "栈顶指针 top")]}
    result = compose_context(region, page, "栈顶指针 top")
    # Each extension alone fits; their union encloses the top-left diagram.
    assert "context_unresolved" in result["quality_flags"]
    assert not (result["bbox"][0] < .3 and result["bbox"][1] < .35)


def test_merged_footer_text_is_flagged_as_mixed_context():
    region = diagram([.08, .3, .44, .79])
    page = {"regions": [region], "text_regions": [
        text([.13, .88, .43, .92], "课程页脚\n栈顶元素=s[top]")]}
    result = compose_context(region, page, "栈顶元素 s[top]")
    assert "mixed_context" in result["quality_flags"]


def test_formula_above_picture_avoids_chopping_an_expression():
    region = diagram([.1, .2, .75, .5])
    page = {"regions": [region], "text_regions": [
        text([.08, .15, .6, .195], "例：4 + 2 * 3 / (7 - 5)")]}
    result = compose_context(region, page, "表达式求值次序")
    assert result["bbox"][1] == .135


def test_padding_intersection_warns_instead_of_absorbing_unrelated_prose():
    region = diagram()
    page = {"regions": [region], "text_regions": [
        text([.46, .3, .9, .34], "无关的第二栏解释") ]}
    result = compose_context(region, page, "栈顶元素")
    assert result["bbox"] == [.085, .185, .465, .715]
    # A tiny contact below 3% area is not enough to reliably infer clipping.
    page["text_regions"][0]["bbox"][0] = .445
    result = compose_context(region, page, "栈顶元素")
    assert "text_at_crop_edge" in result["quality_flags"]


def test_padding_near_another_figure_is_a_visible_quality_flag():
    region = diagram([.4, .4, .9, .85])
    page = {"regions": [region, diagram([.2, .2, .8, .395])], "text_regions": []}
    result = compose_context(region, page, "表格")
    assert "figure_at_crop_edge" in result["quality_flags"]


def test_legacy_layouts_keep_existing_padding_and_explicit_unknown_flag():
    region = diagram([.001, .005, .99, .99])
    result = compose_context(region, {"regions": [region]}, "栈")
    assert result["bbox"] == [0., 0., 1., 1.]
    assert result["quality_flags"] == ["layout_context_unavailable"]


@pytest.mark.parametrize("box", [[0, 0, float("nan"), 1], [0, 0, 2, 3], [True, 0, 1, 1], []])
def test_invalid_positioned_text_is_not_used(box):
    region = diagram()
    result = compose_context(region, {"regions": [region], "text_regions": [text(box, "s[top]=1")]}, "s[top]")
    assert result["bbox"] == [.085, .185, .465, .715]
