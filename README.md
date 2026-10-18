# ARMA4OS
面向openEuler的自适应资源管控Agent

## 核心目标
设计并实现一个运行在用户态的自适应资源管控 Agent 框架。Agent 能够感知系统中的 workload，并根据负载特征动态选择或生成资源管理策略，重点方向是借助 Linux sched_ext / scx 调整 CPU 调度策略，从而改善目标 workload 的性能。

扩展方向：除 CPU 调度外，可进一步把 eBPF 作为系统侧 hook，由 Agent 负责策略决策，扩展到网络策略、安全策略、cgroup 资源控制等操作系统资源管理场景。

**赛题要求：**
- 构建可扩展的用户态资源管控 Agent 框架，并设计标准化的 Tool / Skills 能力接口，便于快速新增 Agent 能力。
- 能够感知 workload 的运行状态和资源需求，并据此调整 CPU 调度策略。
- 集成 sched_ext / scx 调度器，形成可实际运行的调度闭环，并用实验验证对 workload 的性能优化效果。
- 支持以 eBPF 作为 hook 扩展到 network policy、security policy、resource control（如 cgroup）等方向。
- 提供完整的性能对比数据，以及可复现的实验环境、配置和测试方法。


**参考资料：**
https://mp.weixin.qq.com/s/ewC8gTMRvm2NO6ywoproAA
Towards Agentic OS: An LLM Agent Framework for Linux Schedulers
Mixture-of-Schedulers: An Adaptive Scheduling Agent as a Learned Router for Expert Policies
https://github.com/sched-ext/scx

评分细则
评分项	分值	重点
创新性	30	Agent 架构设计与调度策略创新
功能完整性	25	workload 感知、策略调整准确性与灵活性
性能提升	25	相对默认调度器的实际性能改善
代码质量	10	代码结构、注释、可维护性
演示效果	10	现场演示的完整性、流畅度与可验证性