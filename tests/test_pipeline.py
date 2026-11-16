"""运行：.venv/bin/python -m pytest tests -q   （扫描件用例较慢，可加 -m "not slow" 跳过）"""
import re
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import tables as T  # noqa: E402
from app.chunker import ChunkOptions, to_markdown  # noqa: E402
from app.pipeline import ConvertOptions, convert  # noqa: E402
from app.structure import assign_heading_levels, normalize_heading  # noqa: E402
from app.textutil import clean_text, join_lines  # noqa: E402

SAMPLES = ROOT / "samples"
SEP = "===CHUNK==="


@pytest.fixture(scope="session", autouse=True)
def samples():
    if not (SAMPLES / "complex_cn.pdf").exists():
        subprocess.run([sys.executable, str(ROOT / "tests" / "make_samples.py"), str(SAMPLES)], check=True)


def run(name, **kw):
    conv = ConvertOptions.from_dict(kw)
    opts = ChunkOptions.from_dict(kw)
    out = convert(str(SAMPLES / name), conv, opts)
    return out["result"], out["elements"], opts


def chunks_of(md, sep=SEP):
    return [c.strip() for c in md.split(sep) if c.strip()]


# ------------------------------------------------------------ 单元


def test_text_cleanup():
    assert clean_text("X X 科技 有限公司  内部资料") == "X X 科技有限公司内部资料"
    assert clean_text("提交申请;下一步,完成", ocr=True) == "提交申请；下一步，完成"
    assert join_lines(["infor-", "mation security", "中文", "续行"]) == "information security中文续行"


def test_heading_levels_by_numbering():
    hs = [{"text": t, "size": s} for t, s in [
        ("信息安全管理制度", 20), ("第一章 总则", 15), ("1.1 目的", 12), ("1.2 范围", 14),
        ("第二章 资产", 15), ("2.1 清单", 12), ("2.1.1 明细", 11)]]
    assign_heading_levels(hs)
    assert [h["level"] for h in hs] == [1, 2, 3, 3, 2, 3, 4]
    hs = [{"text": t, "size": 11} for t in ["一、总体要求", "（一）原则", "1. 收支平衡", "（二）明细", "二、监督"]]
    assign_heading_levels(hs)
    assert [h["level"] for h in hs] == [1, 2, 3, 2, 1]
    assert normalize_heading("第一章总则") == "第一章 总则"
    assert normalize_heading("1.1目的") == "1.1 目的"


def test_table_normalize_and_merge():
    t = T.normalize_table([["序号", "名称", "", "备注"], ["1", "a", "x", ""], ["2", "b", "y", ""]])
    # 空表头列与“备注”列互补 → 合并为一列
    assert t["header"] == ["序号", "名称", "备注"]
    assert t["rows"] == [["1", "a", "x"], ["2", "b", "y"]]
    t1 = {"header": ["A", "B"], "rows": [["1", "x"]], "row_pages": [1]}
    t2 = {"header": ["A", "B"], "rows": [["2", "y"]], "row_pages": [2]}
    m = T.merge_tables(t1, t2)
    assert m["rows"] == [["1", "x"], ["2", "y"]] and m["row_pages"] == [1, 2]
    # 表头不重复时第二段首行作为数据行
    t3 = {"header": ["3", "z"], "rows": [["4", "w"]], "row_pages": [2]}
    assert T.merge_tables(t1, t3)["rows"] == [["1", "x"], ["3", "z"], ["4", "w"]]
    assert T.render_markdown(["a|b", "c"], [["1", "2"]]).splitlines()[0] == "| a\\|b | c |"


def test_table_sections_split_on_full_width_rows():
    # 一个框线表格内两张小表：通栏标题隔开，第二张的列边界多出来，合并单元格横向填充
    rows = [
        ["设备名称：门机 设备编号：B1", "设备名称：门机 设备编号：B1", "设备名称：门机 设备编号：B1", "设备名称：门机 设备编号：B1"],
        ["油 品 更 换 标 准", "油 品 更 换 标 准", "油 品 更 换 标 准", "油 品 更 换 标 准"],
        ["机构", "部位", "部位", "周期"],
        ["起升", "减速箱", "减速箱", "60"],
        ["滤 芯 更 换", "滤 芯 更 换", "滤 芯 更 换", "滤 芯 更 换"],
        ["机构", "名称", "数量", "周期"],
        ["供料", "滤芯", "1个", "48月"],
    ]
    F, T_ = False, True
    hspan = [[F, T_, T_, T_], [F, T_, T_, T_], [F, F, T_, F], [F, F, T_, F],
             [F, T_, T_, T_], [F, F, F, F], [F, F, F, F]]
    secs = T.split_sections(rows, hspan, [10, 20, 30, 40])
    assert [s["title"] for s in secs] == ["设备名称：门机 设备编号：B1 · 油品更换标准", "滤芯更换"]
    assert secs[0]["rows"] == [["机构", "部位", "周期"], ["起升", "减速箱", "60"]] and secs[0]["col_x"] == [10, 20, 40]
    assert secs[1]["rows"][0] == ["机构", "名称", "数量", "周期"]
    # 没有横向覆盖信息（版面识别的表格）：原样返回
    assert T.split_sections(rows, None) == [{"title": "", "rows": rows, "col_x": None}]


def test_header_cut_by_page_break_is_joined():
    # 页底只剩表头上半部分（“保养”），下一页首行是下半部分（“级别”）
    top = T.normalize_table([["保养", "保养", "保养内容", "保养"]], min_rows=1)
    assert top == {"header": ["保养", "保养", "保养内容", "保养"], "rows": [], "col_x": None}
    assert T.normalize_table([["保养", "保养", "保养内容", "保养"]]) is None  # 默认至少两行
    nxt = T.normalize_table([["级别", "项目", "", "周期"], ["例保", "清洁", "清理地面", "当班"]])
    merged = T.merge_tables({**top, "row_pages": []}, {**nxt, "row_pages": [13]})
    assert merged["header"] == ["保养级别", "保养项目", "保养内容", "保养周期"]
    assert merged["rows"] == [["例保", "清洁", "清理地面", "当班"]]


def test_long_table_split_repeats_header():
    rows = [[str(i), f"设备-{i:03d}", "服务器"] for i in range(200)]
    els = [{"type": "heading", "text": "清单", "level": 1, "pages": [1, 1]},
           {"type": "table", "header": ["序号", "编号", "类型"], "rows": rows, "row_pages": [1] * 100 + [2] * 100,
            "pages": [1, 2]}]
    res = to_markdown(els, ChunkOptions(max_chars=600))
    cs = chunks_of(res["markdown"])
    assert len(cs) > 3
    for c in cs:
        assert len(c) <= 600
        assert "| 序号 | 编号 | 类型 |" in c
    assert "页码：2" in cs[-1]
    res = to_markdown(els, ChunkOptions(max_chars=5000, table_max_rows=50))
    assert len(chunks_of(res["markdown"])) == 4


def test_small_sections_merge_within_parent_only():
    els = [
        {"type": "heading", "text": "第一章", "level": 1, "pages": [1, 1]},
        {"type": "heading", "text": "1.1 小节", "level": 2, "pages": [1, 1]},
        {"type": "paragraph", "text": "短内容。", "role": None, "pages": [1, 1]},
        {"type": "heading", "text": "1.2 小节", "level": 2, "pages": [1, 1]},
        {"type": "paragraph", "text": "也很短。", "role": None, "pages": [1, 1]},
        {"type": "heading", "text": "第二章", "level": 1, "pages": [2, 2]},
        {"type": "paragraph", "text": "第二章内容" * 50, "role": None, "pages": [2, 2]},
    ]
    cs = chunks_of(to_markdown(els, ChunkOptions(min_chars=200))["markdown"])
    assert len(cs) == 2
    assert "# 第一章" in cs[0] and "## 1.1 小节" in cs[0] and "## 1.2 小节" in cs[0]
    assert "第二章" not in cs[0]


def test_context_modes_and_separator():
    els = [{"type": "heading", "text": "总则", "level": 1, "pages": [1, 1]},
           {"type": "heading", "text": "目的", "level": 2, "pages": [1, 1]},
           {"type": "paragraph", "text": "句子。" * 300, "role": None, "pages": [1, 3]}]
    md = to_markdown(els, ChunkOptions(max_chars=400, separator="@@@"))["markdown"]
    cs = chunks_of(md, "@@@")
    assert all(len(c) <= 400 for c in cs)
    assert all(c.startswith("> 章节：总则") or c.startswith("> 页码") for c in cs)
    assert "> 章节：总则 > 目的" in cs[1]
    md = to_markdown(els, ChunkOptions(max_chars=400, context="headings", include_pages=False))["markdown"]
    assert chunks_of(md)[1].startswith("# 总则\n\n## 目的")
    md = to_markdown(els, ChunkOptions(max_chars=400, context="none", include_pages=False))["markdown"]
    assert not chunks_of(md)[1].startswith(">")


# ------------------------------------------------------------ 端到端


def test_text_pdf_end_to_end():
    res, els, opts = run("text_cn.pdf")
    md = res["markdown"]
    cs = chunks_of(md)
    # 页眉、页码已清理
    assert "内部资料" not in md and "第 1 页" not in md and "共 3 页" not in md
    # 标题层级
    assert "# 信息安全管理制度" in md and "\n## 第一章 总则" in md and "\n### 1.1 目的" in md
    # 列表
    assert "- 制定信息安全策略和规划；" in md and "1. 提交报废申请；" in md
    # 跨页表格合并为一张表：60 行数据、表头只在每个表格块开头出现
    tables = [e for e in els if e["type"] == "table"]
    assert len(tables) == 1 and len(tables[0]["rows"]) == 60 and tables[0]["pages"] == [1, 3]
    assert tables[0]["header"] == ["序号", "设备编号", "类型", "数量", "存放位置"]
    for i in (1, 30, 60):
        assert f"| {i} | 设备-{i:03d} |" in md
    for c in cs:
        assert len(c) <= opts.max_chars
        if "| 设备-" in c or "设备-0" in c:
            assert c.count("| 序号 | 设备编号 |") == 1
            assert "章节：信息安全管理制度 > 第二章 资产管理 > 2.1 资产清单" in c or "## 第二章 资产管理" in c
    assert res["doc_stats"]["tables_merged"] == 2


def test_complex_pdf_merged_cells_and_toc():
    res, els, _ = run("complex_cn.pdf")
    md = res["markdown"]
    assert "| 部门 | 项目 | 2024年 / 预算 | 2024年 / 决算 | 2025年 / 预算 | 2025年 / 增幅 |" in md
    assert "| 研发部 | 设备购置 | 300 | 310 | 280 | -7% |" in md
    assert "| 市场部 | 差旅费用 | 80 | 75 | 90 | 13% |" in md
    heads = {e["text"]: e["level"] for e in els if e["type"] == "heading"}
    assert heads["2025年度预算管理办法"] == 1 and heads["一、总体要求"] == 2 and heads["（一）编制原则"] == 3
    assert heads["1. 收支平衡"] == 4 and heads["二、执行与监督"] == 2
    assert "各部门预算如下表所示。" not in heads
    # 跨页段落被合并，没有断在半句
    para = next(e for e in els if e["type"] == "paragraph" and e["text"].startswith("第1句"))
    assert "第59句" in para["text"] and para["pages"] == [1, 3]
    assert "确需调整的，应当按程序报批。" in md
    assert not re.search(r"^-\s*\d+\s*-$", md, re.M)


def test_two_column_reading_order():
    res, _, _ = run("two_column.pdf")
    md = res["markdown"]
    order = [md.index(s) for s in ("左栏标题", "左栏第1段", "左栏第3段", "右栏标题", "右栏第1段", "右栏第3段")]
    assert order == sorted(order)


@pytest.mark.slow
def test_scanned_pdf_matches_text_version():
    res, els, _ = run("scanned_cn.pdf")
    assert res["doc_stats"]["ocr_pages"] == 3
    md = res["markdown"]
    assert "\n## 第一章 总则" in md and "\n### 1.2 适用范围" in md
    tables = [e for e in els if e["type"] == "table"]
    assert len(tables) == 1 and len(tables[0]["rows"]) == 60
    ok = sum(1 for i in range(1, 61) if f"| 设备-{i:03d} |" in md)
    assert ok >= 58, f"OCR 表格行识别 {ok}/60"


# ------------------------------------------------------------ 真实文档回归（文件不存在时跳过）

NJG = "南京港（集团）班组建设考核评价办法.pdf"
QZ = "《起重机械安全技术规程》 (1).pdf"


@pytest.mark.skipif(not (SAMPLES / NJG).exists(), reason="缺少真实样例")
def test_hidden_ocr_layer_and_cross_page_table():
    res, els, _ = run(NJG)
    md = res["markdown"]
    # 叠加的旧 OCR 隐藏文字层被丢弃：无重复、无错字
    assert "南泉恭" not in md and "评价项评价项" not in md and md.count("第一条 为推动班组精细化管理") == 1
    # 首页（带公章图片）不被误 OCR 擦除
    assert "宁港安制〔2025〕83 号" in md and "经研究决定" in md
    heads = [e["text"] for e in els if e["type"] == "heading"]
    assert "第一章 总则" in heads and "第六章 附则" in heads
    assert not any(h.startswith("附件：") for h in heads)
    assert "第十五条 集团对班组建设" in md
    # 跨页续表列数不同仍能合并，并沿用纵向合并的分类列
    assert "| 四 | 创新创效 | 推进双重预防提级建设工作。 | 10 |" in md
    assert len([e for e in els if e["type"] == "table"]) == 1


@pytest.mark.slow
@pytest.mark.skipif(not (SAMPLES / QZ).exists(), reason="缺少真实样例")
def test_numbered_headings_lists_and_toc():
    res, els, opts = run(QZ)
    md = res["markdown"]
    assert "\n## 1 总则\n" in md and "\n### 1.2 适用范围\n" in md and "\n###### 2.3.4.3.1 外观\n" in md
    assert "- (1) 符合法律法规、安全技术规范的规定和相关标准的要求；\n- (2) 符合使用条件和合同的要求；" in md
    assert "消除或者减小其风险；" in md
    assert "总则 ····" not in md
    assert "| 机构工作级别 | 起升 / 单层缠绕 / 标准钢丝绳 |" in md
    assert all(c["chars"] <= opts.max_chars for c in res["chunks"])


# ------------------------------------------------------------ 图片

def _make_figure_pdf(path):
    doc = pymupdf.open()
    # 一张有内容的图：渐变+图形
    src = pymupdf.open(); p0 = src.new_page(width=300, height=200)
    for i in range(20):
        p0.draw_rect(pymupdf.Rect(i*15, 0, i*15+15, 200), color=None, fill=(i/20, 0.4, 1-i/20))
    p0.draw_rect(pymupdf.Rect(0, 140, 300, 200), color=None, fill=(1, 1, 1))
    p0.draw_circle((150,80), 50, color=(0,0,0), fill=(1,1,0.6), width=3)
    for k in range(6):
        p0.draw_line((20 + k * 45, 150), (40 + k * 45, 190), color=(0, 0, 0), width=1.5)
    photo = p0.get_pixmap(dpi=144).tobytes("png")
    logo_pg = pymupdf.open(); lp = logo_pg.new_page(width=60, height=30); lp.draw_rect(lp.rect, fill=(0.8,0,0)); lp.insert_text((8,20),"LOGO",fontsize=12,color=(1,1,1))
    logo = lp.get_pixmap(dpi=144).tobytes("png")
    for n in range(3):
        page = doc.new_page(width=595, height=842)
        page.insert_image(pymupdf.Rect(40, 20, 100, 50), stream=logo)
        y = 100
        page.insert_text((72, y), f"第{n+1}章 设备结构", fontname="china-s", fontsize=18); y += 40
        for k in range(4):
            page.insert_text((72, y), "本章介绍设备的组成部分与主要结构，检修时应先断电并挂牌上锁，确认无误后再开展作业。", fontname="china-s", fontsize=10.5); y += 18
        if n == 0:
            page.insert_image(pymupdf.Rect(147, y+20, 447, y+220), stream=photo)
            page.insert_text((230, y+240), "图1 设备结构示意图", fontname="china-s", fontsize=10.5)
            y += 270
        for k in range(4):
            page.insert_text((72, y), "检修完成后应清理现场，恢复防护装置，并做好检修记录，由负责人签字确认后方可投入运行。", fontname="china-s", fontsize=10.5); y += 18
    doc.save(path)


def test_picture_filters():
    from app.images import _skip

    def pix(draw, w=300, h=200):
        doc = pymupdf.open()
        page = doc.new_page(width=w, height=h)
        draw(page)
        return page.get_pixmap(colorspace=pymupdf.csRGB, alpha=False)

    def gradient(page):
        for i in range(30):
            page.draw_rect(pymupdf.Rect(0, i * 7, 300, i * 7 + 7), color=None, fill=(0.55 + i * 0.015, 0.8, 0.95))

    def seal(page):
        page.draw_circle((150, 100), 80, color=(0.9, 0.1, 0.1), width=6)
        page.insert_text((105, 105), "STAMP", fontsize=22, color=(0.9, 0.1, 0.1))

    def line_art(page):
        for k in range(8):
            page.draw_line((20 + k * 30, 30), (40 + k * 30, 170), color=(0, 0, 0), width=0.8)

    assert _skip(pix(lambda p: None))  # 空白
    assert _skip(pix(gradient))  # 渐变底图
    assert _skip(pix(seal))  # 印章
    assert not _skip(pix(line_art))  # 白底细线图保留


def test_pictures_saved_and_linked(tmp_path, monkeypatch):
    from app import images

    monkeypatch.setenv("STRATUM_DATA_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("STRATUM_PUBLIC_URL", "http://10.0.0.5:18080/")
    pdf = tmp_path / "figs.pdf"
    _make_figure_pdf(str(pdf))
    out = convert(str(pdf), ConvertOptions(), ChunkOptions())
    imgs = [e for e in out["elements"] if e["type"] == "image"]
    # 正文插图保留；每页页眉的徽标去掉
    assert len(imgs) == 1 and imgs[0]["pages"] == [1, 1]
    name = imgs[0]["name"]
    assert images.path_for(name).is_file()
    md = out["result"]["markdown"]
    assert f"![图1 设备结构示意图](http://10.0.0.5:18080/api/images/{name})" in md
    # 图片与图题在同一块
    first = chunks_of(md)[0]
    assert "api/images/" in first and "\n\n图1 设备结构示意图\n\n" in first
    assert not any("STRATUM_PUBLIC_URL" in w for w in out["result"]["warnings"])

    out = convert(str(pdf), ConvertOptions(extract_images=False), ChunkOptions())
    assert not any(e["type"] == "image" for e in out["elements"])


def test_image_chunk_rendering_and_url_warning(monkeypatch):
    from app import images

    monkeypatch.delenv("STRATUM_PUBLIC_URL", raising=False)
    monkeypatch.setattr(images, "_seen_base", "")
    name = "a" * 40 + ".png"
    els = [
        {"type": "heading", "text": "1 概述", "level": 1, "pages": [1, 1]},
        {"type": "paragraph", "text": "接线如下图所示：", "role": None, "pages": [1, 1]},
        {"type": "image", "name": name, "width": 100, "height": 80, "pages": [2, 2]},
    ]
    r = to_markdown(els, ChunkOptions())
    assert f"![图片（第2页）](/api/images/{name})" in r["markdown"]
    assert any("STRATUM_PUBLIC_URL" in w for w in r["warnings"])
    images.remember_base("http://192.162.10.41:18080/api/kbs")
    images.remember_base("http://127.0.0.1:8000/api/kbs")  # 本机访问不覆盖
    r = to_markdown(els, ChunkOptions())
    assert f"(http://192.162.10.41:18080/api/images/{name})" in r["markdown"]
    assert not any("STRATUM_PUBLIC_URL" in w for w in r["warnings"])
    monkeypatch.setattr(images, "_seen_base", "")
