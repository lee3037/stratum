"""浏览器端冒烟测试（需另装 playwright）。

用法：先启动服务，再在同一台机器上运行，STRATUM_DATA_DIR 与服务保持一致：
    python tests/fake_fastgpt.py 3999 &
    STRATUM_DATA_DIR=/tmp/lib \
      STRATUM_FASTGPT_URL=http://127.0.0.1:3999 STRATUM_FASTGPT_API_KEY=secret ./run.sh 8000
    STRATUM_DATA_DIR=/tmp/lib FAKE_FASTGPT=http://127.0.0.1:3999 python tests/ui_smoke.py [http://localhost:8000/] [截图目录]
测试会新建一个随机名称的知识库，结束后不删除，便于检查磁盘上的结果。
"""
import json
import os
import sys
import urllib.request
import uuid
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/stratum-ui")
OUT.mkdir(parents=True, exist_ok=True)
DATA = Path(os.environ["STRATUM_DATA_DIR"])
SAMPLES = Path(__file__).resolve().parents[1] / "samples"
SAMPLE = SAMPLES / "complex_cn.pdf"
KB = f"冒烟-{uuid.uuid4().hex[:6]}"

errors = []
checks = {}


def disk(rel):
    return (DATA / KB / rel).read_text("utf-8")


def parse_from_sidebar(page, name):
    """侧栏点选 PDF → 抽屉里选“开始解析分块” → 确认配置后开始解析。"""
    page.locator(f'aside a[href="#{name}"]').click()
    page.click("[role=dialog] >> text=开始解析分块")
    page.click("[role=dialog] >> button:has-text('开始解析')")


def version_link(page, name):
    return page.get_by_role("navigation", name="Markdown 产物").get_by_text(name, exact=True)


def open_reparse(page):
    page.get_by_role("group", name="Markdown 产物操作").hover()
    page.wait_for_timeout(500)
    page.get_by_role("button", name="重新解析分块", exact=True).click()
    drawer = page.get_by_role("dialog")
    drawer.wait_for()
    return drawer


def confirm_reparse(page, name):
    page.get_by_role("dialog").get_by_role("button", name="重新解析分块", exact=True).click()
    version_link(page, name).wait_for(timeout=180000)
    page.get_by_role("dialog").wait_for(state="detached")


def save_current(page):
    page.get_by_role("button", name="保存到知识库", exact=True).click()
    page.get_by_role("button", name="已保存", exact=True).wait_for()
    page.get_by_role("button", name="保存到知识库", exact=True).wait_for()


def upload(page, button, files):
    with page.expect_file_chooser() as chooser:
        page.locator("aside").get_by_role("button", name=button, exact=True).click()
    chooser.value.set_files(files)


with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page = context.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    # 修改冲突用例会故意触发 409，浏览器会把它记为资源加载错误
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "409" not in m.text else None)

    # 新建知识库并进入。配置了 FastGPT 时首页只列 FastGPT 知识库、不提供新建，改用接口新建，
    # 它出现在「服务器上的其他知识库」中
    page.goto(BASE)
    if os.environ.get("FAKE_FASTGPT"):
        req = urllib.request.Request(BASE + "api/kbs", json.dumps({"name": KB}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req)
        page.reload()
        page.get_by_text("服务器上的其他知识库").wait_for()
        page.get_by_text(KB, exact=True).click()
    else:
        page.get_by_role("button", name="新建知识库").first.click()
        page.get_by_label("新知识库名称").fill(KB)
        page.get_by_role("button", name="创建并打开").click()
    page.locator("aside").get_by_role("button", name="上传 PDF", exact=True).wait_for()
    checks["新建知识库"] = (DATA / KB).is_dir() and f"kb=" in page.url
    checks["空白时无产物操作"] = page.get_by_role("group", name="Markdown 产物操作").count() == 0

    # 侧栏上传多个文件，只上传不解析
    pdf_bytes = SAMPLE.read_bytes()
    upload(page, "上传 PDF", [
        {"name": SAMPLE.name, "mimeType": "application/pdf", "buffer": pdf_bytes},
        {"name": "two_column.pdf", "mimeType": "application/pdf", "buffer": (SAMPLES / "two_column.pdf").read_bytes()},
    ])
    page.locator('aside a[href="#two_column.pdf"]').wait_for()
    checks["上传写入服务器"] = (DATA / KB / SAMPLE.name).read_bytes() == pdf_bytes
    checks["上传后不自动解析"] = page.get_by_role("dialog").count() == 0 and page.get_by_role("group", name="Markdown 产物操作").count() == 0

    # 同名同内容跳过，同名不同内容改名
    upload(page, "上传 PDF", [{"name": SAMPLE.name, "mimeType": "application/pdf", "buffer": pdf_bytes}])
    page.get_by_text("跳过 1 个（内容相同）", exact=False).first.wait_for()
    other = (SAMPLES / "text_cn.pdf").read_bytes()
    upload(page, "上传 PDF", [{"name": SAMPLE.name, "mimeType": "application/pdf", "buffer": other}])
    page.locator('aside a[href="#complex_cn(2).pdf"]').wait_for()
    checks["同名不同内容自动改名"] = (DATA / KB / SAMPLE.name).read_bytes() == pdf_bytes

    # 预览原文：直接显示服务器上的 PDF
    page.locator('aside a[href="#two_column.pdf"]').click()
    page.click("[role=dialog] >> text=预览原文件")
    page.get_by_role("button", name="关闭预览").wait_for()
    checks["预览服务器 PDF"] = "/pdf?path=" in (page.locator("main iframe").get_attribute("src") or "")
    page.get_by_role("button", name="关闭预览").click()

    parse_from_sidebar(page, SAMPLE.name)
    version_link(page, "complex_cn_v1.md").wait_for(timeout=180000)
    n0 = page.locator("article").count()
    checks["首次生成 v1"] = n0 > 0
    checks["未保存不写盘"] = not (DATA / KB / "complex_cn_v1.md").exists()
    bar = page.get_by_role("group", name="Markdown 产物操作")
    page.mouse.move(5, 5)
    page.wait_for_timeout(500)
    width = bar.bounding_box()["width"]
    copy = page.get_by_role("button", name="复制 Markdown", exact=True)
    copy.hover()
    page.wait_for_timeout(500)
    checks["操作栏悬停展开"] = bar.bounding_box()["width"] > width
    copy.click()
    checks["复制 Markdown"] = "预算" in page.evaluate("navigator.clipboard.readText()")
    save_current(page)
    v1 = disk("complex_cn_v1.md")
    checks["保存 v1 到服务器"] = "预算" in v1

    # 修改配置后取消，不创建版本
    drawer = open_reparse(page)
    minus = drawer.locator("[role=group][aria-label=最多字数] button[aria-label=减少]")
    for _ in range(6):
        minus.click()
    drawer.get_by_role("button", name="取消", exact=True).click()
    checks["取消不创建版本"] = version_link(page, "complex_cn_v2.md").count() == 0
    drawer = open_reparse(page)
    checks["取消恢复配置"] = "1000" in drawer.locator("[role=group][aria-label=最多字数]").inner_text()
    for _ in range(6):
        minus.click()
    confirm_reparse(page, "complex_cn_v2.md")
    checks["重新分块生成 v2"] = page.locator("article").count() > n0
    settings = (DATA / KB / ".stratum" / "settings.json").read_text("utf-8")
    checks["解析时调整的参数不改变知识库默认值"] = '"max_chars": 1000' in settings

    # v2 未保存：编辑后生成 v3，返回 v2 时编辑仍在
    page.get_by_role("radio", name="Markdown", exact=True).click()
    v2 = page.locator("textarea").input_value() + "\n手工补充的一句话。\n"
    page.locator("textarea").fill(v2)
    drawer = open_reparse(page)
    drawer.locator("[role=group][aria-label=最多字数] button[aria-label=增加]").click()
    confirm_reparse(page, "complex_cn_v3.md")
    version_link(page, "complex_cn_v2.md").click()
    page.get_by_role("radio", name="Markdown", exact=True).click()
    checks["未保存旧版保留编辑"] = page.locator("textarea").input_value() == v2
    save_current(page)
    checks["保存旧版不覆盖 v1"] = disk("complex_cn_v1.md") == v1 and disk("complex_cn_v2.md") == v2

    # 仅改分隔符保留这次编辑
    drawer = open_reparse(page)
    drawer.get_by_label("分隔符", exact=True).fill("@@@")
    drawer.get_by_label("分隔符", exact=True).press("Enter")
    confirm_reparse(page, "complex_cn_v4.md")
    page.get_by_role("radio", name="Markdown", exact=True).click()
    v4 = page.locator("textarea").input_value()
    checks["仅改分隔符保留编辑"] = "@@@" in v4 and "手工补充的一句话" in v4 and "===CHUNK===" not in v4
    with page.expect_download() as download:
        page.get_by_role("button", name="下载 .md", exact=True).click()
    download.value.save_as(str(OUT / "out.md"))
    checks["下载使用版本文件名"] = download.value.suggested_filename == "complex_cn_v4.md" and (OUT / "out.md").read_text() == v4
    save_current(page)
    checks["新旧版本分别保存"] = disk("complex_cn_v4.md") == v4 and disk("complex_cn_v2.md") == v2
    page.get_by_role("radio", name="分块", exact=True).click()
    if page.get_by_role("button", name="对照原文", exact=True).count():
        page.get_by_role("button", name="对照原文", exact=True).click()
    page.click("article >> nth=1 >> header button >> nth=0")
    checks["页码跳转"] = "#page=" in (page.locator("section iframe").get_attribute("src") or "")

    # 修改已保存版本：别人先改过时提示冲突，不覆盖
    page.get_by_role("radio", name="Markdown", exact=True).click()
    (DATA / KB / "complex_cn_v4.md").write_text("其他人的修改", "utf-8")
    page.locator("textarea").fill(v4 + "\n我的修改\n")  # 本会话保存过的版本，再次保存不再弹确认
    page.get_by_role("button", name="保存到知识库", exact=True).click()
    page.get_by_text("已被其他人修改", exact=False).first.wait_for()
    checks["修改冲突不覆盖"] = disk("complex_cn_v4.md") == "其他人的修改"

    # 只有一种解析方案：不显示方案选择器，直接显示本地 OCR 参数
    page.wait_for_timeout(2000)
    drawer = open_reparse(page)
    checks["单一解析方案不显示选择器"] = (
        drawer.get_by_role("radio", name="OXR").count() == 0
        and drawer.get_by_role("radiogroup", name="解析方案").count() == 0
        and drawer.get_by_role("radiogroup", name="OCR", exact=True).count() == 1
    )
    drawer.get_by_role("button", name="取消", exact=True).click()
    drawer.wait_for(state="detached")

    # 其他人保存的版本：刷新后出现；重开知识库后按服务器最大版本继续递增
    page.screenshot(path=str(OUT / "versions.png"))
    page2 = context.new_page()
    page2.on("pageerror", lambda e: errors.append(str(e)))
    page2.goto(BASE)  # 记住上次的知识库，直接进入
    page2.locator('aside a[href="#complex_cn.pdf"]').wait_for()
    checks["重开直接进入上次知识库"] = page2.locator("header").first.get_by_text(KB).count() == 1
    page2.locator('aside a[href="#complex_cn.pdf"]').click()
    page2.get_by_role("radio", name="Markdown", exact=True).click()
    checks["点击已转换 PDF 打开最新保存版本"] = page2.locator("textarea").input_value() == "其他人的修改"
    checks["打开已保存版本识别分隔符"] = version_link(page2, "complex_cn_v4.md").count() == 1
    open_reparse(page2)
    confirm_reparse(page2, "complex_cn_v5.md")
    checks["重开后按最大版本递增"] = version_link(page2, "complex_cn_v3.md").count() == 0
    page2.close()

    # 未保存的 v3 在服务器最大版本已是 v4 时保存：顺延为 v5，导航同步改名
    version_link(page, "complex_cn_v3.md").click()
    save_current(page)
    version_link(page, "complex_cn_v5.md").wait_for()
    checks["预定版本号过小时顺延"] = (
        (DATA / KB / "complex_cn_v5.md").exists() and not (DATA / KB / "complex_cn_v3.md").exists()
        and version_link(page, "complex_cn_v3.md").count() == 0
    )

    # 批量处理：筛选未转换 → 全选 → 批量解析，结果自动保存；下载本批结果
    page.get_by_role("button", name="批量处理", exact=True).click()
    stage = page.get_by_role("region", name="选择文件")
    stage.wait_for()
    stage.get_by_role("radio", name="未转换").click()
    stage.get_by_label("全选当前列表").check()
    pending_count = stage.locator("tbody input[type=checkbox]:checked").count()
    checks["筛选未转换并全选"] = pending_count == 2  # two_column.pdf 与 complex_cn(2).pdf
    stage.get_by_role("button", name=f"批量解析（{pending_count}）").click()
    page.get_by_role("dialog").get_by_role("button", name=f"开始批量解析（{pending_count}）").click()
    batches = page.get_by_role("region", name="最近的批次")
    batches.get_by_text("已结束").wait_for(timeout=300000)
    checks["批量结果自动保存"] = (DATA / KB / "two_column_v1.md").exists() and (DATA / KB / "complex_cn(2)_v1.md").exists()
    checks["批次明细显示完成"] = batches.get_by_text("完成", exact=True).count() == 2
    page.wait_for_timeout(1500)
    checks["列表刷新为已转换"] = page.locator('aside a[href="#two_column.pdf"]').get_by_text("已转换").count() == 1
    with page.expect_download() as download:
        batches.get_by_role("button", name="下载本批结果").click()
    zpath = OUT / "batch.zip"
    download.value.save_as(str(zpath))
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
    checks["下载本批结果（FastGPT 格式）"] = names == {"two_column.md", "complex_cn(2).md", "汇总.csv"}
    with page.expect_download() as download:
        stage.get_by_role("button", name="下载所选（2）").click()
    checks["下载所选"] = download.value.suggested_filename.endswith(".zip")
    page.screenshot(path=str(OUT / "batch.png"))
    batches.get_by_role("button", name="two_column_v1.md").click()
    version_link(page, "two_column_v1.md").wait_for()
    checks["从批次打开结果"] = page.locator("article").count() > 0

    # FastGPT：在知识库设置中绑定，批量导入待导入的文件，再导入单个版本
    fake_url = os.environ.get("FAKE_FASTGPT")
    if fake_url:
        page.get_by_role("button", name="知识库设置", exact=True).click()
        drawer = page.get_by_role("dialog")
        select = drawer.get_by_label("绑定的 FastGPT 知识库")
        select.locator("option", has_text="安全规程").wait_for(state="attached")
        select.select_option(label="安全规程（bge-m3）")
        drawer.get_by_role("button", name="保存设置").click()
        drawer.wait_for(state="detached")
        settings = json.loads((DATA / KB / ".stratum" / "settings.json").read_text("utf-8"))
        checks["绑定 FastGPT 知识库"] = settings.get("fastgpt", {}).get("dataset_id") == "ds1"
        page.get_by_role("button", name="批量处理", exact=True).click()
        stage = page.get_by_role("region", name="选择文件")
        stage.get_by_role("button", name="导入全部待导入（3）").click()
        page.get_by_text("导入完成：成功 3 个").wait_for(timeout=30000)
        state = json.load(urllib.request.urlopen(fake_url + "/_state"))
        names = sorted(c["name"] for c in state["collections"])
        checks["批量导入 FastGPT"] = names == ["complex_cn(2).md", "complex_cn.md", "two_column.md"]
        checks["按分隔符分块导入"] = all(c["chunkSplitter"] and c["chunkSettingMode"] == "custom" for c in state["collections"])
        page.wait_for_timeout(500)
        checks["FastGPT 状态列"] = stage.get_by_text("已导入 v1").count() == 2
        page.screenshot(path=str(OUT / "fastgpt.png"))
        # complex_cn 有 v5（未导入的是最新版本 v5；导入的是 v5）——打开 v1，单独导入旧版本
        version_link(page, "complex_cn_v1.md").click()
        page.get_by_text("FastGPT 中为 complex_cn_v5.md").wait_for()
        page.get_by_role("button", name="导入 FastGPT", exact=True).click()
        page.get_by_text("已导入「安全规程」", exact=False).first.wait_for()
        page.get_by_text("已导入 FastGPT", exact=False).first.wait_for()
        state = json.load(urllib.request.urlopen(fake_url + "/_state"))
        checks["导入单个版本并替换旧集合"] = sorted(c["name"] for c in state["collections"]) == names
        checks["侧栏显示待导入"] = page.locator('aside a[href="#complex_cn.pdf"]').get_by_text("待导入").count() == 1

        # 开启自动导入：修改并保存当前版本后自动导入，替换旧集合
        page.get_by_role("button", name="知识库设置", exact=True).click()
        drawer = page.get_by_role("dialog")
        drawer.get_by_label("保存后自动导入 FastGPT").check()
        drawer.get_by_role("button", name="保存设置").click()
        drawer.wait_for(state="detached")
        settings = json.loads((DATA / KB / ".stratum" / "settings.json").read_text("utf-8"))
        checks["开启自动导入"] = settings.get("fastgpt_auto_import") is True
        page.get_by_role("radio", name="Markdown", exact=True).click()
        page.locator("textarea").fill(page.locator("textarea").input_value() + "\n自动导入的修改\n")
        page.once("dialog", lambda d: d.accept())
        page.get_by_role("button", name="保存到知识库", exact=True).click()
        page.get_by_text("已保存并自动导入", exact=False).first.wait_for(timeout=30000)
        state = json.load(urllib.request.urlopen(fake_url + "/_state"))
        checks["保存后自动导入"] = (
            sorted(c["name"] for c in state["collections"]) == names
            and any("自动导入的修改" in c["text"] for c in state["collections"])
        )

    # 切换知识库：回到列表
    page.locator("header button", has_text=KB).click()
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("menuitem", name="全部知识库 / 新建…").click()
    if fake_url:
        # 已绑定 ds1：首页以 FastGPT 知识库的名称显示它
        page.get_by_text("与 FastGPT 同步", exact=False).first.wait_for()
        checks["返回知识库列表"] = page.get_by_text("安全规程", exact=True).count() == 1 and page.get_by_text(KB, exact=True).count() == 0
    else:
        page.get_by_text(KB, exact=True).wait_for()
        checks["返回知识库列表"] = True
    context.close()

    # 触屏：先展开操作栏，再执行操作；没有 PDF 的旧 .md 兼容
    touch_context = browser.new_context(viewport={"width": 1024, "height": 768}, has_touch=True)
    touch_page = touch_context.new_page()
    touch_page.on("pageerror", lambda e: errors.append(str(e)))
    (DATA / KB / "legacy.pdf").write_bytes(pdf_bytes)
    (DATA / KB / "legacy.md").write_text("# 已保存的旧产物\n", "utf-8")
    touch_page.goto(BASE + f"?kb={KB}")
    version_link(touch_page, "legacy.md").click()
    touch_action = touch_page.get_by_role("button", name="重新解析分块", exact=True)
    touch_action.tap()
    checks["触屏首次点击仅展开"] = touch_page.get_by_role("dialog").count() == 0
    touch_action.tap()
    touch_page.get_by_role("dialog").wait_for()
    touch_page.get_by_role("dialog").get_by_role("button", name="取消", exact=True).tap()
    touch_page.get_by_role("dialog").wait_for(state="detached")
    open_reparse(touch_page)
    confirm_reparse(touch_page, "legacy_v2.md")
    save_current(touch_page)
    checks["兼容旧文件且不覆盖"] = disk("legacy.md") == "# 已保存的旧产物\n" and (DATA / KB / "legacy_v2.md").exists()
    touch_context.close()
    browser.close()

for k, v in checks.items():
    print(("✓" if v else "✗"), k)
print("页面错误:", errors or "无")
sys.exit(0 if all(checks.values()) and not errors else 1)
