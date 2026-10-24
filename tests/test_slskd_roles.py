"""slskd API key roles: Crate must work with a ReadWrite key (slskd's /server
endpoint is Administrator-only) and explain 401/403 clearly.

Run:  python3 tests/test_slskd_roles.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import mock_services  # noqa: E402
from crate.slskd import Slskd, SlskdError  # noqa: E402

tmp = tempfile.mkdtemp(prefix="crate-roles-")
srv, state = mock_services.start(os.path.join(tmp, "dl"))
url = f"http://127.0.0.1:{srv.server_address[1]}"
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


st = Slskd(url, mock_services.API_KEY).server_state()
check(st.get("isLoggedIn") and st.get("username") == "tester", "Administrator key: /server works")

rw = Slskd(url, mock_services.READWRITE_KEY)
st = rw.server_state()
check(st.get("isLoggedIn") and st.get("username") == "tester", "ReadWrite key: login state read from /application")
before = len(state.server_calls)
for _ in range(3):
    Slskd(url, mock_services.READWRITE_KEY).server_state()
check(len(state.server_calls) == before, "after the first 403, /server isn't asked again (no 403 every tick)")
sid = rw.start_search("Test Band First Light", 1)
check(bool(sid), "ReadWrite key can search")

state.logged_in = False
check(Slskd(url, mock_services.READWRITE_KEY).server_state().get("isLoggedIn") is False,
      "ReadWrite key still sees a Soulseek disconnect")
state.logged_in = True

try:
    Slskd(url, "wrong-key-000000000").server_state()
    check(False, "wrong key raises")
except SlskdError as e:
    check(e.status == 401 and "cidr" in str(e), f"wrong key → clear 401 message ({e})")

print()
print("FAILED:" if fails else "ALL PASSED", len(fails) or "")
sys.exit(1 if fails else 0)
