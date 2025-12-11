from trader import OKXTrader, load_config


def main():
    # 1. 读配置
    cfg = load_config()

    # 2. 初始化 trader（use_demo=True 表示模拟盘）
    trader = OKXTrader(cfg, use_demo=True)

    inst_id = "BTC-USDT-SWAP"  # 你也可以改成 ETH-USDT-SWAP 测试

    # 3. 打印一下最新价格
    last = trader.get_last_price(inst_id)
    print(f"{inst_id} last price =", last)

    # 4. 尝试开一个小多单（会按 risk.max_pos 自动算张数）
    print("Opening long ...")
    resp_open = trader.open_long(inst_id, last)
    print("open_long resp:", resp_open)

    # 5. 再查一下仓位
    positions = trader.get_positions(inst_id)
    print("positions after open:", positions)

    # 6. 尝试平掉多单
    print("Closing long ...")
    resp_close = trader.close_long(inst_id)
    print("close_long resp:", resp_close)

    positions = trader.get_positions(inst_id)
    print("positions after close:", positions)


if __name__ == "__main__":
    main()
