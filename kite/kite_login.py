from kiteconnect import KiteConnect
import os
from dotenv import load_dotenv
load_dotenv()

kite = KiteConnect(api_key=os.getenv("ZAPI-KEY"))
# print("Login URL:", kite.login_url())
acc = os.getenv("ZAPI-ACCESS-TOKEN")
print("Access Token:", acc)

kite.set_access_token(acc)



profile = kite.profile()
print("User ID:", profile['user_id'])
print("User Name:", profile['user_name'])

# margin = kite.margins()
# print("Equity Margin:", margin)


