"""Playwright（patchright）浏览器采集层。

模块组成（详见各模块 docstring）：
- config:     配置项与环境变量（D12 配置表），Phase 0 已实现
- quota:      会话/每日配额计数器，Phase 2 实现
- safety:     风控信号检测 + 冷却状态机，Phase 2 实现
- browser_session: 常驻单浏览器上下文 + 串行锁 + 生命周期，Phase 3 实现
- instagram_collector: 单帖/Profile 采集 + 网络响应解析，Phase 1/2 实现
"""
