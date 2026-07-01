from kiteconnect import KiteConnect
import os
from dotenv import load_dotenv
load_dotenv()

kite = KiteConnect(api_key=os.getenv("ZAPI-KEY"))
print("Login URL:", kite.login_url())

# kite.set_access_token(os.getenv("ZAPI-ACCESS-TOKEN"))

request_token = input("Enter the request token: ").strip()

data = kite.generate_session(request_token, api_secret=os.getenv("ZAPI-SECRET"))


access_token = data["access_token"]
print("Access Token:", access_token)

# profile = kite.profile()
# print("User ID:", profile['user_id'])
# print("User Name:", profile['user_name'])

# margin = kite.margins()
# print("Equity Margin:", margin)


