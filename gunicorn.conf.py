import os

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{int(os.environ.get('PORT', 10000))}"
