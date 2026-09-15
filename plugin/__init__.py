"""插件运行时子包：设置解析、分段缓存、补发引擎与切分编排。

原 main.py 单一巨型类按职责拆分为四个 Mixin 模块，组合关系与实例状态
的初始化仍集中在 main.SmartSegmentationPlugin：

- ``settings``：配置读取、清洗与 segmenter 迁移；
- ``prepared_store``：预处理分段缓存、对齐自愈与事件直取；
- ``follow_up``：补发引擎、会话串行锁与后台任务追踪；
- ``orchestrator``：LLM 切分编排、锚点路径与 provider 解析。

各 Mixin 仅通过类级注解声明自己读写的实例属性（赋值统一发生在
main 的 ``__init__``），模块之间不互相导入，依赖方向单向指向
``segmentation/`` 逻辑包；本 ``__init__`` 只存放跨模块共享的常量，
不导入子模块，避免循环依赖。
"""
from __future__ import annotations

_PREPARED_SEGMENT_TTL_SECONDS = 60.0
_PENDING_FOLLOW_UP_TTL_SECONDS = 60.0
_PENDING_REAPER_INTERVAL_SECONDS = 5.0
_PENDING_EXTRA_KEY = "smart_segmentation_pending_id"
_PREPARED_EXTRA_KEY = "smart_segmentation_prepared_segments"
