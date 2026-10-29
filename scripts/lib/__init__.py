# -*- coding: utf-8 -*-
"""项目内部公共库（OCR 四轨基础设施）。

模块：
  gpu_lock  —— iGPU 跨进程互斥（OpenVINO 与 Vulkan 共用一块核显）
  footnotes —— 脚注多信号识别与对账
  cache     —— 结果缓存 / 断点续传 / 原子写
  artifacts —— 中间产物分层与清理
"""
