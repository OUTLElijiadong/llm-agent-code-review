# CI 覆盖文本断言复现与修复

2026-10-22，候选提交 `44ec8cda2a73c44b293bed6387c107eedf34924a` 的业务 CI #39722853400：后端 8338 通过、1 失败；前端、部署/执行器/故障注入通过，CodeQL #39722853942 两语言任务通过。该失败候选未发布。

唯一失败：`tests/test_report_exporter.py::test_scope_coverage_is_human_readable_in_html_word_and_pdf_exports`。本地单独运行也复现同一断言 `已完成文件数：2`。PDF 的中英混排字体标签分隔汉字与数字，测试把段落内部排版标记当可见文本。

修复三个 PDF 可读性用例的采集方式：先构建真实 ReportLab Paragraph，再用 `getPlainText()` 获取读者文本。保留覆盖状态、完成文件数、未扫描文件、错误、来源及敏感内部字段不得泄露的原有严格断言；没有删除用例或降低预期。

本地整文件验证：109 passed in 3.49s；`git diff --check` 通过。后续发布以新提交的完整 CI 为准，不把本地整文件验证充当完整 CI。
