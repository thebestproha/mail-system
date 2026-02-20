#!/bin/bash

python server1.py &
python server2.py &
python server3.py &
gunicorn load_balancer:app --bind 0.0.0.0:$PORT
