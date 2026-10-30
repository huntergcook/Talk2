import sys
import time


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, file=sys.stderr, flush=True)
