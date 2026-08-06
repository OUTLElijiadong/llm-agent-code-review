# 圆桌讨论群聊语义完善 - TODO

## 本任务待办

无。实现、测试、文档、生产同步和线上验证均已完成。

## 已解决的关联事项

- [x] `test_platform_service_handlers_render_lists_and_mutations` 已按非空 `file_ids` 契约更新。
- [x] 额外发现并修复生产自动查询误用 `CodeFile.status == 1` 的问题；现只选择同项目 active 文件。
- [x] 补充 Planner 动态文件引用边界后，后端全量 `1039 passed`，原唯一失败已清零。详见 `docs/小助手启动审查契约对齐/`。
