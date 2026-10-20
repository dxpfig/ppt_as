import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from agent_service.docx_markdown import docx_to_markdown


W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def docx(path):
    document = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}"><w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>一、总体判断</w:t></w:r></w:p>
  <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>1.1 市场</w:t></w:r></w:p>
  <w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>市场规模达到 120 亿元</w:t></w:r></w:p>
  <w:tbl><w:tr><w:tc><w:p><w:r><w:t>阶段</w:t></w:r></w:p></w:tc>
  <w:tc><w:p><w:r><w:t>目标</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>近期</w:t></w:r></w:p></w:tc>
  <w:tc><w:p><w:r><w:t>完成样机</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>'''
    styles = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{W}">
  <w:style w:styleId="Heading1"><w:name w:val="Heading 1"/></w:style>
  <w:style w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
</w:styles>'''
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', document)
        archive.writestr('word/styles.xml', styles)


class DocxMarkdownTests(unittest.TestCase):
    def test_preserves_headings_lists_and_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.docx'
            docx(path)
            markdown = docx_to_markdown(path)
        self.assertIn('# 一、总体判断', markdown)
        self.assertIn('## 1.1 市场', markdown)
        self.assertIn('- 市场规模达到 120 亿元', markdown)
        self.assertIn('| 阶段 | 目标 |', markdown)
        self.assertIn('| 近期 | 完成样机 |', markdown)


if __name__ == '__main__':
    unittest.main()
