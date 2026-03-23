from truedata_ws.websocket.TD import TD
import time
import os
from dotenv import load_dotenv

load_dotenv()

USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")

# Port 8084 is your assigned port
td_obj = TD(USERNAME, PASSWORD, live_port=8084, url='push.truedata.in')

# Subscribe to symbols (use correct TrueData symbol format)
symbols = ['RELIANCE','HDFCBANK']  # Example symbols; replace with actual ones
req_ids = td_obj.start_live_data(symbols)

time.sleep(2)  # Allow touchline data to populate

last_price = {}

while True:
    time.sleep(0.5)
    for req_id in req_ids:
        data=td_obj.live_data.get(req_id)
        # print(data)

        if last_price.get(req_id) != data.ltp:
            print(data.symbol, data.ltp)
            last_price[req_id] = data.ltp

{'timestamp': None, 'symbol_id': None, 'symbol': 'RELIANCE', 'ltp': None, 'ltq': None, 'atp': None, 'ttq': None, 'day_open': None, 'day_high': None, 'day_low': None, 'prev_day_close': None, 'oi': None, 'prev_day_oi': None, 'turnover': None, 'special_tag': '', 'tick_seq': None, 'best_bid_price': None, 'best_bid_qty': None, 'best_ask_price': None, 'best_ask_qty': None, 'tick_type': None, 'change': None, 'change_perc': None, 'oi_change': None, 'oi_change_perc': None}
{'timestamp': None, 'symbol_id': 900000596, 'symbol': 'NIFTY-I', 'ltp': 24393.6, 'ltq': None, 'atp': None, 'ttq': 7045025, 'day_open': 24286.6, 'day_high': 24419.6, 'day_low': 0.0, 'prev_day_close': 24122.5, 'oi': 17367675, 'prev_day_oi': 17452305, 'turnover': None, 'special_tag': '', 'tick_seq': None, 'best_bid_price': None, 'best_bid_qty': None, 'best_ask_price': None, 'best_ask_qty': None, 'tick_type': 0, 'change': 271.09999999999854, 'change_perc': 1.1238470307803856, 'oi_change': -84630, 'oi_change_perc': -0.4849216192359691}
{'timestamp': None, 'symbol_id': None, 'symbol': 'RELIANCE', 'ltp': None, 'ltq': None, 'atp': None, 'ttq': None, 'day_open': None, 'day_high': None, 'day_low': None, 'prev_day_close': None, 'oi': None, 'prev_day_oi': None, 'turnover': None, 'special_tag': '', 'tick_seq': None, 'best_bid_price': None, 'best_bid_qty': None, 'best_ask_price': None, 'best_ask_qty': None, 'tick_type': None, 'change': None, 'change_perc': None, 'oi_change': None, 'oi_change_perc': None}
{'timestamp': None, 'symbol_id': 900000596, 'symbol': 'NIFTY-I', 'ltp': 24393.6, 'ltq': None, 'atp': None, 'ttq': 7045025, 'day_open': 24286.6, 'day_high': 24419.6, 'day_low': 0.0, 'prev_day_close': 24122.5, 'oi': 17367675, 'prev_day_oi': 17452305, 'turnover': None, 'special_tag': '', 'tick_seq': None, 'best_bid_price': None, 'best_bid_qty': None, 'best_ask_price': None, 'best_ask_qty': None, 'tick_type': 0, 'change': 271.09999999999854, 'change_perc': 1.1238470307803856, 'oi_change': -84630, 'oi_change_perc': -0.4849216192359691}