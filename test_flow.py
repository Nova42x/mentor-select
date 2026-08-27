# -*- coding: utf-8 -*-
"""兼容入口：V2完整流程验收由 test_realtime.py 统一维护。"""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(__file__), 'test_realtime.py'), run_name='__main__')
