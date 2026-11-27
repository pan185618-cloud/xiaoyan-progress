---
name: research-progress
description: 打开小研科研大盘流程图（S0-S7 全部课题进度）或查看当前 Codex/Work 会话实时运行步骤。
---

# 小研科研大盘 (Xiaoyan Research Progress)

严格只读的科研工作流全景进度与实时步骤监控插件（绝不修改任何科研课题数据、合同、代码或证据链）。

- 调用 `open_research_dashboard` 打开全屏「科研总览流程图」（支持左侧边栏一键进入）。
- 调用 `open_live_progress` 打开「当前运行步骤（实时）」面板（垂直箭头步骤流、子代理 120k 预算、主上下文 180k/220k 水位与门禁状态）。
- 调用 `get_research_progress` 获取结构化的宏观课题进度与微观实时会话快照。
- 调用 `get_project_detail` 查询单个课题的合同列表、受控证据链、门禁结果与只读审批终端命令。
