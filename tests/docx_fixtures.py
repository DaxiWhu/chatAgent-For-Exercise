"""测试夹具：手工组装一份"含原生图表 + 截图 + OLE"的最小 docx。

为什么不直接用 Word 生成：本项目没有真实 Word 语料（用户确认暂无），
而 python-docx **无法写入**图表与 OLE —— 它两个都不支持，正好是我们要测的两类。
所以只能自己拼包。好处是完全可控：每个 part 的字节都看得见，
断言失败时能立刻分清"解析器错"还是"夹具造错了"。

XML 按**真实 Word 输出的形态**写，这点很关键：
  - 图表的类目/数值放在 `c:strCache` / `c:numCache` 里，
    并且**故意带上 `c:f` 公式串**（`Sheet1!$A$2:$A$5`）—— 解析器若误把公式串当数据，
    这条夹具就会立刻暴露它（这是最容易犯的错）。
  - 替代文字写在 `wp:docPr@descr`，且**排在 `<a:blip/>` 之前**（OOXML 的实际顺序）。
  - OLE 同时带 `v:imagedata`（图标预览）与 `o:OLEObject`，与 Word 的真实行为一致：
    同一段落里会出现两个引用，解析器必须都能拿到。
"""
from __future__ import annotations

import base64
import io
import zipfile

# --- 命名空间（与真实 Word 一致） ---
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_NS_O = "urn:schemas-microsoft-com:office:office"
_NS_V = "urn:schemas-microsoft-com:vml"
_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

# 1x1 透明 PNG（合法文件，避免解析器去校验图片内容）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

# 图表标题、系列名、类目、数值 —— 测试直接拿这些常量做断言，改夹具时不会漏改测试
CHART_TITLE = "年度销售额"
SERIES_NAME = "销售额"
CHART_CATS = ["2019", "2020", "2021", "2022"]
CHART_VALS = ["120", "145", "138", "180"]
# 公式串：**不是数据**，解析器绝不能把它当值
CHART_CAT_FORMULA = "Sheet1!$A$2:$A$5"
CHART_VAL_FORMULA = "Sheet1!$B$2:$B$5"

# 各元素的锚点文本（位置还原的断言依据）
PARA_TITLE = "季度销售报告"
PARA_BEFORE_CHART = "下面给出各年度的销售额走势。"
PARA_BEFORE_IMAGE = "下图是组织架构示意。"
PARA_BEFORE_OLE = "附件为明细表。"
ALT_CHART = "2019-2022 年销售额折线图"
ALT_IMAGE = "组织架构示意图"
ALT_OLE = "嵌入的 Excel 明细表"
OLE_PROG_ID = "Excel.Sheet.12"

# OLE 内层工作簿的内容（用于验证"内层原件能被 loaders 解析出来"）
OLE_SHEET_NAME = "明细"
OLE_CELL_TEXT = "服务器采购"
OLE_CELL_VALUE = 50000


def _document_xml() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_NS_W}" xmlns:r="{_NS_R}" xmlns:wp="{_NS_WP}"
            xmlns:a="{_NS_A}" xmlns:c="{_NS_C}" xmlns:o="{_NS_O}" xmlns:v="{_NS_V}">
  <w:body>
    <w:p><w:r><w:t>{PARA_TITLE}</w:t></w:r></w:p>
    <w:p><w:r><w:t>{PARA_BEFORE_CHART}</w:t></w:r></w:p>
    <w:p>
      <w:r><w:drawing><wp:inline>
        <wp:extent cx="5486400" cy="3200400"/>
        <wp:docPr id="1" name="图表 1" descr="{ALT_CHART}"/>
        <a:graphic><a:graphicData uri="{_NS_C}">
          <c:chart r:id="rId10"/>
        </a:graphicData></a:graphic>
      </wp:inline></w:drawing></w:r>
    </w:p>
    <w:p><w:r><w:t>{PARA_BEFORE_IMAGE}</w:t></w:r></w:p>
    <w:p>
      <w:r><w:drawing><wp:inline>
        <wp:extent cx="2000000" cy="1200000"/>
        <wp:docPr id="2" name="图片 2" descr="{ALT_IMAGE}"/>
        <a:graphic><a:graphicData uri="{_NS_A}">
          <a:blip r:embed="rId11"/>
        </a:graphicData></a:graphic>
      </wp:inline></w:drawing></w:r>
    </w:p>
    <w:p><w:r><w:t>{PARA_BEFORE_OLE}</w:t></w:r></w:p>
    <w:p>
      <w:r><w:object w:dxaOrig="1440" w:dyaOrig="1440">
        <v:shape id="_x0000_i1025" type="#_x0000_t75" style="width:120pt;height:80pt"
                 alt="{ALT_OLE}">
          <v:imagedata r:id="rId13" o:title="{ALT_OLE}"/>
        </v:shape>
        <o:OLEObject Type="Embed" ProgID="{OLE_PROG_ID}" ShapeID="_x0000_i1025"
                     DrawAspect="Content" ObjectID="_1234567890" r:id="rId12"/>
      </w:object></w:r>
    </w:p>
  </w:body>
</w:document>'''


def _rels_xml() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}">
  <Relationship Id="rId10" Type="{_NS_R}/chart" Target="charts/chart1.xml"/>
  <Relationship Id="rId11" Type="{_NS_R}/image" Target="media/image1.png"/>
  <Relationship Id="rId12" Type="{_NS_R}/oleObject" Target="embeddings/oleObject1.bin"/>
  <Relationship Id="rId13" Type="{_NS_R}/image" Target="media/image2.emf"/>
</Relationships>'''


def chart1_xml() -> str:
    cats = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>'
                   for i, v in enumerate(CHART_CATS))
    vals = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>'
                   for i, v in enumerate(CHART_VALS))
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<c:chartSpace xmlns:c="{_NS_C}" xmlns:a="{_NS_A}" xmlns:r="{_NS_R}">
  <c:chart>
    <c:title><c:tx><c:rich>
      <a:bodyPr/><a:lstStyle/>
      <a:p><a:r><a:t>{CHART_TITLE}</a:t></a:r></a:p>
    </c:rich></c:tx></c:title>
    <c:plotArea>
      <c:layout/>
      <c:lineChart>
        <c:grouping val="standard"/>
        <c:ser>
          <c:idx val="0"/><c:order val="0"/>
          <c:tx><c:strRef><c:f>{CHART_VAL_FORMULA}</c:f>
            <c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>{SERIES_NAME}</c:v></c:pt></c:strCache>
          </c:strRef></c:tx>
          <c:cat><c:strRef><c:f>{CHART_CAT_FORMULA}</c:f>
            <c:strCache><c:ptCount val="{len(CHART_CATS)}"/>{cats}</c:strCache>
          </c:strRef></c:cat>
          <c:val><c:numRef><c:f>{CHART_VAL_FORMULA}</c:f>
            <c:numCache><c:ptCount val="{len(CHART_VALS)}"/>{vals}</c:numCache>
          </c:numRef></c:val>
        </c:ser>
      </c:lineChart>
    </c:plotArea>
  </c:chart>
</c:chartSpace>'''


def ole_inner_xlsx_bytes() -> bytes:
    """造一个真的 xlsx（openpyxl），当作 OLE 的内层原件。

    用真文件而不是假字节：这样才能验证"OLE 内层能被 loaders 解析成正文"
    这条链路是真的通，而不是只验证了我们的文件头判断。
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = OLE_SHEET_NAME
    ws["A1"], ws["B1"] = "项目", "金额"
    ws["A2"], ws["B2"] = OLE_CELL_TEXT, OLE_CELL_VALUE
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_minimal_docx(path: str, *, ole_blob: bytes | None = None) -> str:
    """组装 docx。

    ole_blob 默认是一份**真的 xlsx**（模拟"内层可直接解析"的情况，
    某些转换器就是直接把原始文件塞进 embeddings/）。
    传入其他字节可测别的分支，例如 CFB 魔数开头的字节 → 应优雅降级为 .bin。
    """
    blob = ole_inner_xlsx_bytes() if ole_blob is None else ole_blob
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Default Extension="png" ContentType="image/png"/>'
                   '<Default Extension="emf" ContentType="image/x-emf"/>'
                   '<Default Extension="bin" ContentType="application/octet-stream"/>'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   # 这条不能省：python-docx 靠它把 word/document.xml 认成"主文档部件"，
                   # 只有 Default Extension="xml" 的话它拿不到正确的 content-type，
                   # 会直接判 "Input document is not valid"（实测踩过）。
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '<Override PartName="/word/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<Relationships xmlns="{_NS_REL}">'
                   f'<Relationship Id="rId1" Type="{_NS_R}/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", _document_xml())
        z.writestr("word/_rels/document.xml.rels", _rels_xml())
        z.writestr("word/charts/chart1.xml", chart1_xml())
        z.writestr("word/media/image1.png", PNG_1PX)
        z.writestr("word/media/image2.emf", b"\x01\x00\x00\x00" + b"\x00" * 64)
        z.writestr("word/embeddings/oleObject1.bin", blob)
    return path
