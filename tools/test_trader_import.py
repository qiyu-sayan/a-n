# tools/test_trader_import.py
"""
简单自检：确认新的 bot.trader 能正常 import
不依赖任何 secrets，只检查语法和依赖
"""

from bot.trader import Trader, Env, MarketType, Side, PositionSide, OrderRequest

print("✅ trader.py 导入成功")
