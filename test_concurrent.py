# -*- coding: utf-8 -*-
"""兼容入口：实时互选验收已包含最后一个名额的并发竞争测试。"""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(__file__), 'test_realtime.py'), run_name='__main__')
