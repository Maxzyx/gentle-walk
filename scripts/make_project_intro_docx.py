"""生成《邻里缓行·项目介绍》Word 文档。

用法：
    python scripts/make_project_intro_docx.py

输出：docs/邻里缓行-项目介绍.docx
内容来自项目用途、公开数据说明与可复现命令；实际数据和测试结果以运行输出为准。
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "邻里缓行-项目介绍.docx"

BODY_EAST = "宋体"
BODY_ASCII = "Calibri"
HEAD_EAST = "微软雅黑"
ACCENT = RGBColor(0x1F, 0x3B, 0x63)
ACCENT_LIGHT = RGBColor(0x2E, 0x5C, 0x8A)
GRAY = RGBColor(0x59, 0x59, 0x59)


# --------------------------------------------------------------------------- #
# 基础排版工具
# --------------------------------------------------------------------------- #
def _set_east_asia(run, name_east: str) -> None:
    """python-docx 不能直接设置中文字体，需要写 eastAsia。"""
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:ascii"), BODY_ASCII)
    fonts.set(qn("w:hAnsi"), BODY_ASCII)
    fonts.set(qn("w:eastAsia"), name_east)


def _style_base(document: Document) -> None:
    """设置正文与标题的默认中英文字体。"""
    normal = document.styles["Normal"]
    normal.font.name = BODY_ASCII
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_EAST)
    normal.paragraph_format.line_spacing = 1.35
    normal.paragraph_format.space_after = Pt(4)

    for level, size in ((1, 16), (2, 13), (3, 11.5)):
        style = document.styles[f"Heading {level}"]
        style.font.name = BODY_ASCII
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = ACCENT if level == 1 else ACCENT_LIGHT
        style.element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_EAST)
        style.paragraph_format.space_before = Pt(14 if level == 1 else 10)
        style.paragraph_format.space_after = Pt(6)


def _heading(document: Document, text: str, level: int) -> None:
    paragraph = document.add_heading(level=level)
    run = paragraph.add_run(text)
    run.font.name = BODY_ASCII
    run.font.size = Pt({1: 16, 2: 13, 3: 11.5}[level])
    run.font.bold = True
    run.font.color.rgb = ACCENT if level == 1 else ACCENT_LIGHT
    _set_east_asia(run, HEAD_EAST)


def _para(document: Document, text: str, *, bold_prefix: str = "", size: float = 10.5,
          gray: bool = False, align: WD_ALIGN_PARAGRAPH | None = None,
          space_after: float = 4) -> None:
    paragraph = document.add_paragraph()
    if align is not None:
        paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(space_after)
    if bold_prefix and text.startswith(bold_prefix):
        head = paragraph.add_run(bold_prefix)
        head.bold = True
        head.font.size = Pt(size)
        _set_east_asia(head, BODY_EAST)
        rest = paragraph.add_run(text[len(bold_prefix):])
        rest.font.size = Pt(size)
        if gray:
            rest.font.color.rgb = GRAY
        _set_east_asia(rest, BODY_EAST)
        return
    run = paragraph.add_run(text)
    run.font.size = Pt(size)
    if gray:
        run.font.color.rgb = GRAY
    _set_east_asia(run, BODY_EAST)


def _bullet(document: Document, text: str, *, level: int = 0) -> None:
    paragraph = document.add_paragraph(style="List Bullet" if level == 0 else "List Bullet 2")
    paragraph.paragraph_format.space_after = Pt(2)
    run = paragraph.add_run(text)
    run.font.size = Pt(10.5)
    _set_east_asia(run, BODY_EAST)


def _shade(cell_or_paragraph_element, fill: str) -> None:
    """给单元格或段落加底色。"""
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    cell_or_paragraph_element.append(shd)


def _table(document: Document, headers: list[str], rows: list[list[str]],
           widths: list[float] | None = None, font_size: float = 9.5) -> None:
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True

    header_cells = table.rows[0].cells
    for index, header in enumerate(headers):
        cell = header_cells[index]
        cell.text = ""
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(header)
        run.bold = True
        run.font.size = Pt(font_size)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        _set_east_asia(run, HEAD_EAST)
        _shade(cell._tc.get_or_add_tcPr(), "1F3B63")

    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = ""
            paragraph = cells[index].paragraphs[0]
            run = paragraph.add_run(str(value))
            run.font.size = Pt(font_size)
            _set_east_asia(run, BODY_EAST)

    if widths:
        for row in table.rows:
            for index, width in enumerate(widths):
                row.cells[index].width = Cm(width)
    document.add_paragraph().paragraph_format.space_after = Pt(2)


def _code_block(document: Document, lines: list[str]) -> None:
    """等宽代码块 + 浅灰底色。"""
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.line_spacing = 1.15
    _shade(paragraph._p.get_or_add_pPr(), "F2F2F2")
    for index, line in enumerate(lines):
        run = paragraph.add_run(line + ("\n" if index < len(lines) - 1 else ""))
        run.font.name = "Consolas"
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
        fonts = run._element.get_or_add_rPr()
        rfonts = fonts.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            fonts.append(rfonts)
        rfonts.set(qn("w:ascii"), "Consolas")
        rfonts.set(qn("w:hAnsi"), "Consolas")


def _callout(document: Document, title: str, text: str) -> None:
    """一条带底色的提示框（用 1x1 表格实现）。"""
    table = document.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = table.rows[0].cells[0]
    cell.text = ""
    _shade(cell._tc.get_or_add_tcPr(), "FFF4E5")
    paragraph = cell.paragraphs[0]
    head = paragraph.add_run(title + " ")
    head.bold = True
    head.font.size = Pt(10)
    head.font.color.rgb = RGBColor(0x8A, 0x4B, 0x00)
    _set_east_asia(head, HEAD_EAST)
    body = paragraph.add_run(text)
    body.font.size = Pt(10)
    _set_east_asia(body, BODY_EAST)
    document.add_paragraph().paragraph_format.space_after = Pt(2)


def _footer(document: Document) -> None:
    """页脚：项目名 + 页码。"""
    footer = document.sections[0].footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("邻里缓行 · 项目介绍　第 ")
    run.font.size = Pt(9)
    run.font.color.rgb = GRAY
    _set_east_asia(run, BODY_EAST)

    field_run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    field_run._r.append(begin)
    field_run._r.append(instr)
    field_run._r.append(end)

    tail = paragraph.add_run(" 页")
    tail.font.size = Pt(9)
    tail.font.color.rgb = GRAY
    _set_east_asia(tail, BODY_EAST)


# --------------------------------------------------------------------------- #
# 文档内容
# --------------------------------------------------------------------------- #
def _cover(document: Document) -> None:
    for _ in range(5):
        document.add_paragraph()

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("邻里缓行")
    run.font.name = BODY_ASCII
    run.font.size = Pt(32)
    run.font.bold = True
    run.font.color.rgb = ACCENT
    _set_east_asia(run, HEAD_EAST)

    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run("适老步行路线助手｜买菜与看诊，平缓优先")
    run.font.size = Pt(15)
    run.font.color.rgb = ACCENT_LIGHT
    _set_east_asia(run, HEAD_EAST)

    document.add_paragraph()
    line = document.add_paragraph()
    line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = line.add_run("项 目 介 绍")
    run.font.size = Pt(12)
    run.font.color.rgb = GRAY
    _set_east_asia(run, HEAD_EAST)

    for _ in range(6):
        document.add_paragraph()

    meta = [
        "版本：MVP v0.1",
        "试点片区：西安市碑林区",
        "面向用户：老人及其家属、社区工作者",
        "更新日期：2026 年 11 月",
    ]
    for text in meta:
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(text)
        run.font.size = Pt(10.5)
        run.font.color.rgb = GRAY
        _set_east_asia(run, BODY_EAST)

    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _section_purpose(document: Document) -> None:
    _heading(document, "一、这个工具解决什么问题", 1)
    _para(
        document,
        "对年轻人来说，去一趟菜店或社区卫生服务站只是「几百米的路」；"
        "但对行动不便的老人，这段路可能包含十几级台阶、一个陡坡、两次过街，"
        "走远一点就可能体力不支、没有地方坐下休息。",
    )
    _para(
        document,
        "常见的地图导航按「最短时间」或「最短距离」规划，默认使用者年轻、腿脚方便，"
        "并不会告诉你哪条路台阶少、坡度缓、过街少、沿途有地方歇脚。",
    )
    _para(
        document,
        "本工具把「平缓、少台阶、少过街、能歇脚」作为路线评分的主要目标，"
        "为老人从起点到社区菜店 / 社区医院规划一条更友好的步行路线，"
        "并用通俗的语言说明这条路线好在哪里、需要注意什么、还有哪些备选。",
    )
    _callout(
        document,
        "边界说明：",
        "本工具只回答「怎么走更平缓」，不提供任何医疗建议；"
        "所有路线与设施信息均提示「以现场为准」，不能替代实地察看。",
    )


def _section_users(document: Document) -> None:
    _heading(document, "二、谁在使用、用来做什么", 1)
    _table(
        document,
        ["使用角色", "典型用法", "得到的帮助"],
        [
            ["老人 / 家属", "选择起点与目的地（菜店或医院），读文字路线与风险提示",
             "知道哪条路台阶少、哪里要过街、多远有休息点，心里有底"],
            ["社区工作者", "查看片区内的菜店、卫生站清单与分布",
             "了解服务设施覆盖情况，为老人提供出行建议"],
            ["项目评审 / 开发者", "运行命令行与自检脚本、查看测试与数据",
             "完整复现「数据 → 路线 → 风险提示」的证据链"],
        ],
        widths=[3.0, 7.5, 6.0],
    )


def _section_features(document: Document) -> None:
    _heading(document, "三、核心功能", 1)

    _heading(document, "3.1 目的地选择：碑林区内 127 家社区设施", 2)
    _para(
        document,
        "工具内置碑林区的两类目的地：111 家社区菜店（菜市场、生鲜超市、蔬菜店）"
        "与 16 家社区医院 / 卫生服务站。全部经过坐标纠偏与片区边界裁剪，"
        "并校验过「从主路网可达」，不会出现「列表里有、却永远走不到」的地点。",
    )

    _heading(document, "3.2 平缓优先的路线推荐", 2)
    _para(
        document,
        "每条路段的分数统一换算成「等效分钟」——可以理解为「这段路对老人来说，"
        "相当于多走了几分钟」，最后把整条路线累加，分数越低越平缓。评分考虑：",
    )
    for item in (
        "步行时间：按老人慢速步速 0.9 米/秒估算；",
        "台阶：每级台阶折算固定时间，台阶越多分越高；",
        "坡度：坡度越陡分越高，超过 8% 额外加重；",
        "过街：每过一次街单独加分；",
        "障碍：记录在案的障碍点按严重程度加分；",
        "疲劳与休息点：连续走 200 米以上没有休息点会加分，沿途有休息点会适当减分；",
        "目的地坡道：有坡道减分（更友好），无坡道且路线有台阶则加分。",
    ):
        _bullet(document, item)

    _heading(document, "3.3 风险提示", 2)
    _para(
        document,
        "规划结果会按「高 / 中 / 低」三级给出风险提示，并写明触发原因和建议，例如："
        "「需要过街，请走人行横道并留意右转车辆」「连续路段缺少休息点，建议中途停歇」。"
        "即使判断为低风险，也会提示「实际路况以现场为准」。",
    )

    _heading(document, "3.4 备选方案", 2)
    _para(
        document,
        "除了推荐的 1 条主路线，还会给出最多 2 条备选路线（例如台阶更少、过街更少），"
        "并分别列出距离、台阶数、最大坡度、过街次数与评分，方便老人和家属按实际体力取舍。",
    )

    _heading(document, "3.5 纯文字路线说明", 2)
    _para(
        document,
        "路线以「方向 + 距离 + 特殊提示」的文字步骤输出，例如"
        "「东向步行约 138 米（经人行横道过街）」。即使手机没有地图、"
        "或页面地图组件不可用，也能完整地读出路线的每一步。",
    )

    _heading(document, "3.6 离线可用与自动降级", 2)
    _para(
        document,
        "项目默认离线运行：无需网络、无需任何地图账号，"
        "用本地数据就能完整演示「选目的地 → 出路线 → 看风险」。"
        "接入在线数据时，若网络超时、限流或未配置 Key，会自动退回本地数据并明确提示，"
        "不会卡住或崩溃。",
    )


def _section_demo(document: Document) -> None:
    _heading(document, "四、实际使用演示", 1)
    _para(document, "命令行是最稳定的使用方式，两条命令即可看到完整结果：")

    _code_block(
        document,
        [
            "# 1) 列出碑林区的社区菜店",
            "python -m src.cli --config config/beilin.json --list --category greengrocer",
            "",
            "# 2) 从指定起点规划到最近的社区医院（平缓优先）",
            "python -m src.cli --config config/beilin.json --from 108.9533,34.2509 --category clinic",
        ],
    )

    _para(document, "运行后会显示目的地、估计距离、步行时间、台阶/坡度、风险及备选路线。")
    _para(document, "具体结果随起点、数据集与评分参数变化，使用时应结合现场路况判断。", gray=True)


def _section_data(document: Document) -> None:
    _heading(document, "五、碑林区数据现状", 1)
    _para(
        document,
        "项目以碑林区为试点片区，数据来源、规模与用途如下。所有记录都带来源标注，"
        "不是「看起来像真的」的演示数据。",
    )
    _table(
        document,
        ["数据", "来源", "规模", "说明"],
        [
            ["步行路网", "OpenStreetMap", "9116 个节点、10405 段、约 405.8 公里",
             "公开数据（ODbL 1.0），含台阶、过街、路面材质等标签"],
            ["社区菜店", "高德开放平台", "111 家", "按碑林区行政区划检索，坐标已转 WGS84"],
            ["社区医院", "高德开放平台", "16 家", "含社区卫生服务中心 / 服务站"],
            ["障碍点", "样例合成", "3 条", "人工编写的演示数据，明确标注为虚构"],
            ["休息点", "样例合成", "2 条", "同上"],
        ],
        widths=[2.4, 3.2, 6.2, 5.2],
    )
    _para(document, "数据使用说明：")
    for item in (
        "目的地数据按片区边界与主路网筛选，记录中保留来源和更新时间；",
        "地图数据会随时间变化，连通性和实际入口需现场核实；",
        "人工障碍和休息点仅供演示，不代表真实设施分布。",
    ):
        _bullet(document, item)


def _section_scoring(document: Document) -> None:
    _heading(document, "六、评分与风险规则（一页看懂）", 1)
    _table(
        document,
        ["评分项", "折算方式（等效分钟）"],
        [
            ["步行时间", "距离 ÷ 0.9 米/秒 ÷ 60"],
            ["台阶", "0.5 + 0.9 × 台阶数；未标注台阶数的按 8 级估算"],
            ["斜坡", "3.0 + 0.6 × 坡度百分比；坡度 ≥8% 再乘 1.5"],
            ["过街", "每次 +2.5"],
            ["障碍", "严重程度 1/2/3 分别 +2 / +4 / +8"],
            ["疲劳", "连续超过 200 米没有休息点，每段 +3.0"],
            ["休息点", "沿途每个休息点 −1.0，最多 −5.0"],
            ["目的地坡道", "有坡道 −1.5；无坡道且路线含台阶 +2.0"],
        ],
        widths=[3.6, 12.0],
    )
    _table(
        document,
        ["风险等级", "触发条件（满足其一即可）"],
        [
            ["高", "台阶 ≥11 级、或最大坡度 ≥8%、或过街 >3 次、或障碍严重度 =3"],
            ["中", "有台阶、或坡度 >5%、或过街 2–3 次、或存在无护栏路段 / 疲劳段 / 障碍点"],
            ["低", "以上都不触发（仍提示以现场为准）"],
        ],
        widths=[2.6, 13.0],
    )
    _para(
        document,
        "上述系数是 MVP 阶段的启发式假设（例如步速 0.9 米/秒），已在文档中标注「假设/待验证」，"
        "后续需要用真实调研数据校准。",
        gray=True,
    )


def _section_reliability(document: Document) -> None:
    _heading(document, "七、可靠性设计（为什么它不容易「跑不起来」）", 1)
    for item in (
        "离线优先：无网络、无账号也能完整演示主流程；",
        "优雅降级：在线接口失败时自动回到本地数据，并明确告诉用户「已降级」；",
        "数据可信：每条记录都标注来源（现场 / 高德 / OSM / 样例合成），样例数据如实写「虚构」；",
        "坐标规范：内部统一使用 WGS84；高德坐标先纠偏再入库，缺转换库时标「待复核」，不猜坐标；",
        "可复现：提供离线自动化测试及自检脚本，测试数量和结果以 pytest 输出为准；",
        "错误提示：所有报错为中文，并给出「下一步怎么做」。",
    ):
        _bullet(document, item)


def _section_limits(document: Document) -> None:
    _heading(document, "八、已知边界与诚实说明", 1)
    for item in (
        "评分为启发式：步速、台阶、坡度等系数是初始假设，需真实数据校准；",
        "POI 信息不全：高德数据只有名称、位置与类型，开放时间与入口坡道未知，统一提示「以现场为准」；",
        "路网存在缺口：部分内部步道与主路网没有连接，"
        "已把不可达的目的地过滤掉，但主干路网仍需持续补充；",
        "不是实时导航：不提供实时路况、施工与拥挤信息；",
        "只做碑林区：换片区需要新的路网与 POI 数据（已提供可复现的抓取命令）；",
        "不提供医疗建议，仅回答「怎么走更平缓」。",
    ):
        _bullet(document, item)


def _section_quickstart(document: Document) -> None:
    _heading(document, "九、快速开始（三步）", 1)
    _code_block(
        document,
        [
            "# 1) 零配置体验默认样例片区",
            "python -m src.cli --list",
            "python -m src.cli --from 108.9455,34.2611 --category greengrocer",
            "",
            "# 2) 切换到碑林区真实数据",
            "python -m src.cli --config config/beilin.json --list --category greengrocer",
            "",
            "# 3) 自检与测试",
            "python -m src.selfcheck",
            "python -m pytest -q",
        ],
    )
    _para(
        document,
        "数据来源：路网 © OpenStreetMap contributors（ODbL 1.0）；"
        "POI 来自高德开放平台，仅用于本项目演示；样例数据为人工编写。"
        "使用前请阅读项目 README 中的数据来源声明与已知限制。",
        gray=True,
    )


def build() -> Path:
    """生成并保存文档，返回输出路径。"""
    document = Document()
    _style_base(document)
    _footer(document)

    section = document.sections[0]
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)

    _cover(document)
    _section_purpose(document)
    _section_users(document)
    _section_features(document)
    _section_demo(document)
    _section_data(document)
    _section_scoring(document)
    _section_reliability(document)
    _section_limits(document)
    _section_quickstart(document)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    document.save(OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    path = build()
    print(f"已生成：{path}")
