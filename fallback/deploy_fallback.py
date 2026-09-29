"""Deploy fallback.js as a Twilio Function and set it as the Twilio number's
"Primary handler fails" URL. Run where TWILIO_AUTH_TOKEN, TWILIO_NUMBER, MY_CELL and
OWNER_NAME are set (e.g. on the Fly machine), so the token never leaves the server:

    python deploy_fallback.py <ACCOUNT_SID> fallback.js
"""
import os
import sys
import time

import requests

SID, JS_PATH = sys.argv[1], sys.argv[2]
E = os.environ
AUTH = (SID, E["TWILIO_AUTH_TOKEN"])
API = "https://serverless.twilio.com/v1"
NAME = "callguard-fallback"


def call(method, url, **kw):
    r = requests.request(method, url, auth=AUTH, timeout=30, **kw)
    if r.status_code >= 400:
        sys.exit(f"{method} {url} -> {r.status_code}: {r.text}")
    return r.json()


# Service (reuse on re-run), environment, variables.
svc = next((s for s in call("GET", f"{API}/Services")["services"] if s["unique_name"] == NAME),
           None) or call("POST", f"{API}/Services",
                         data={"UniqueName": NAME, "FriendlyName": "CallGuard fallback",
                               "UiEditable": "true", "IncludeCredentials": "false"})
S = f"{API}/Services/{svc['sid']}"
env = next((x for x in call("GET", f"{S}/Environments")["environments"]
            if x["unique_name"] == "prod"), None) or call(
    "POST", f"{S}/Environments", data={"UniqueName": "prod", "DomainSuffix": "prod"})
existing = {v["key"]: v for v in call("GET", f"{S}/Environments/{env['sid']}/Variables")["variables"]}
for key in ("MY_CELL", "TWILIO_NUMBER", "OWNER_NAME"):
    url = f"{S}/Environments/{env['sid']}/Variables"
    if key in existing:
        call("POST", f"{url}/{existing[key]['sid']}", data={"Value": E[key]})
    else:
        call("POST", url, data={"Key": key, "Value": E[key]})

# Function + new version (content upload goes to a separate host).
fn = next((f for f in call("GET", f"{S}/Functions")["functions"]
           if f["friendly_name"] == "fallback"), None) or call(
    "POST", f"{S}/Functions", data={"FriendlyName": "fallback"})
with open(JS_PATH, "rb") as fh:
    ver = call("POST", f"https://serverless-upload.twilio.com/v1/Services/{svc['sid']}"
                       f"/Functions/{fn['sid']}/Versions",
               data={"Path": "/fallback", "Visibility": "protected"},
               files={"Content": ("fallback.js", fh, "application/javascript")})

# Build, wait, deploy.
build = call("POST", f"{S}/Builds", data={"FunctionVersions": ver["sid"]})
for _ in range(60):
    status = call("GET", f"{S}/Builds/{build['sid']}")["status"]
    if status in ("completed", "failed"):
        break
    time.sleep(3)
if status != "completed":
    sys.exit(f"build {status}")
call("POST", f"{S}/Environments/{env['sid']}/Deployments", data={"BuildSid": build["sid"]})
url = f"https://{env['domain_name']}/fallback"

# Point the phone number's fallback at it.
nums = call("GET", f"https://api.twilio.com/2010-04-01/Accounts/{SID}/IncomingPhoneNumbers.json",
            params={"PhoneNumber": E["TWILIO_NUMBER"]})["incoming_phone_numbers"]
num = nums[0]
call("POST", f"https://api.twilio.com/2010-04-01/Accounts/{SID}/IncomingPhoneNumbers/{num['sid']}.json",
     data={"VoiceFallbackUrl": url, "VoiceFallbackMethod": "POST"})
print("fallback url:", url)
print("voice url:   ", num["voice_url"])
