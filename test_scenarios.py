# -*- coding: utf-8 -*-
"""兼容入口：V2异常场景和权限测试集中在 test_realtime.py。"""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(__file__), 'test_realtime.py'), run_name='__main__')
