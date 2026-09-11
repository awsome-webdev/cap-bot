import base64
import re
import zlib
import sys
import requests
import time
from collections import defaultdict

def solve_rsw_puzzle(x, t, n):
    """Simulates the client-side RSW puzzle solver using sequential modular squaring.   
    :param x: Initial base number
    :param t: Number of sequential squarings
    :param n: The modulus N
    """
    n = int(n, 16)
    x = int(x, 16)
    print(f"Starting RSW puzzle solver (t = {t} steps)...") 
    v = x
    for step in range(1, t + 1):
      # The core sequential squaring step: v = (v^2) mod N
      v = pow(v, 2, n)  
    return v

def resulthex(y):
   y = hex(y)[2:]
   return y


def to_int32(val: int) -> int:
  """Clamps Python arbitrary-precision ints to JavaScript's 32-bit signed integer behavior."""
  val = val & 0xFFFFFFFF
  return val - 0x100000000 if val >= 0x80000000 else val


def emulate_dom_fn(x: int, y: int, z: int) -> int:
  """Pure mathematical emulation of the Cap DOM div-tree traversal function (e.g. ffdl6s8my6).

  No browser or DOM required.
  """
  total = 0
  for v in (x, y, z):
    cur = v
    for _ in range(8):
      if (cur & 1) == 0:
        total += cur
      cur >>= 1
  return total % 256


def emulate_proto_fn(a: int, b: int, c: int) -> int:
  """Pure mathematical emulation of the Cap prototype function (e.g. qqpnp7sc9).

  (b ^ a) | (c ^ b)
  """
  return to_int32((b ^ a) | (c ^ b))


def solve_instrumentation_telemetry(script_js: str, has_user_agent: bool = True):
  # 1. Extract ALL initial integer seeds (initialize state, but do NOT
  #    assume these are the 4 output keys).
  seeds = re.findall(r"(?:var|let|const)?\s*([a-zA-Z0-9_$]+)\s*=\s*([0-9]+)\s*;", script_js)
  state = defaultdict(int)
  for name, val in seeds:
    state[name] = int(val)

  # 1b. Find the AUTHORITATIVE output builder:
  #     var OBJ={}; OBJ["key1"]=varA; OBJ["key2"]=varB; ... return OBJ;
  #
  #     First locate the object that is `{}` then assigned string-keyed props.
  builder_match = re.search(
      r"(?:var|let|const)\s+([a-zA-Z0-9_$]+)\s*=\s*\{\s*\}\s*;"     # OBJ = {}
      r"((?:\s*\1\s*\[\s*[\"'][^\"']+[\"']\s*\]\s*=\s*[a-zA-Z0-9_$]+\s*;)+)"  # OBJ["k"]=v;
      r"\s*return\s+\1\s*;",                                          # return OBJ;
      script_js,
  )
  if not builder_match:
    raise ValueError("Could not locate the state-builder object (OBJ[\"key\"]=var;).")

  # Map each output KEY -> the SOURCE VARIABLE feeding it, preserving order.
  output_map = re.findall(
      r"\[\s*[\"']([^\"']+)[\"']\s*\]\s*=\s*([a-zA-Z0-9_$]+)\s*;",
      builder_match.group(2),
  )
  # output_map == [("fnw6wvpi0zj5", "fnw6wvpi0zj5"), ...]

  # ... (steps 2-4 unchanged: extract helper fns, slice pipeline, run ops) ...

  # 2. Extract the names of the two dynamic helper functions
  # Proto fn signature: function <name>(a,b,c){function F(d)...}
  proto_fn_match = re.search(
      r"function\s+([a-zA-Z0-9_]+)\s*\([a-z],[a-z],[a-z]\)\s*\{\s*function\s+F",
      script_js,
  )
  # DOM fn signature: function <name>(x,y,z){var d=document.createElement('div')...}
  dom_fn_match = re.search(
      r"function\s+([a-zA-Z0-9_]+)\s*\([a-z],[a-z],[a-z]\)\s*\{\s*var\s+[a-z]=document\.createElement",
      script_js,
  )

  proto_fn_name = proto_fn_match.group(1) if proto_fn_match else None
  dom_fn_name = dom_fn_match.group(1) if dom_fn_match else None

  # 3. Locate the execution pipeline section
  # Starts after the DOM function ends and stops before the output object formatting
  body_match = re.search(
      rf"{dom_fn_name}.*?return\s+s;\s*\}}\s*(.*?)(?=\bvar\s+[a-zA-Z0-9_]+=\{{}})",
      script_js,
      re.DOTALL,
  )
  if not body_match:
    raise ValueError("Could not extract operation pipeline.")

  raw_statements = [
      s.strip() for s in body_match.group(1).split(";") if s.strip()
  ]

  # 4. Step through each operation logically
  for stmt in raw_statements:
    # Navigator UserAgent check: var = var ^ (navigator.userAgent ? A : B)
    ua_match = re.match(
        r"([a-zA-Z0-9_]+)\s*=\s*\1\s*\^\s*\(navigator\.userAgent\s*\?\s*(\d+)\s*:\s*(\d+)\)",
        stmt,
    )
    if ua_match:
      var_name = ua_match.group(1)
      if var_name not in state:
        state[var_name] = 0

      val = int(ua_match.group(2)) if has_user_agent else int(ua_match.group(3))
      state[var_name] = to_int32(state[var_name] ^ val)
      continue

    # Final masking & modulo: var = ((var ^ salt) & 0x7FFFFFFF) % mod + offset
    final_match = re.match(
        r"([a-zA-Z0-9_]+)\s*=\s*\(\s*\(\s*\1\s*\^\s*(\d+)\s*\)\s*&\s*(?:0x7FFFFFFF|2147483647)\s*\)\s*%\s*(\d+)\s*\+\s*(\d+)",
        stmt,
    )
    if final_match:
      var_name = final_match.group(1)
      if var_name not in state:
        state[var_name] = 0
      salt = int(final_match.group(2))
      mod = int(final_match.group(3))
      offset = int(final_match.group(4))
      state[var_name] = (((state[var_name] ^ salt) & 0x7FFFFFFF) % mod) + offset
      continue

    # Proto function calls: target = proto_fn(a, b, c)
    if proto_fn_name and f"{proto_fn_name}(" in stmt:
      m = re.match(
          rf"([a-zA-Z0-9_]+)\s*=\s*{proto_fn_name}\(([^)]+)\)", stmt
      )
      if m:
        target, args = m.group(1), [a.strip() for a in m.group(2).split(",")]
        if target not in state:
          state[target] = 0
        state[target] = emulate_proto_fn(
            state[args[0]], state[args[1]], state[args[2]]
        )
        continue

    # DOM tree function calls: target = dom_fn(x, y, z)
    if dom_fn_name and f"{dom_fn_name}(" in stmt:
      m = re.match(rf"([a-zA-Z0-9_]+)\s*=\s*{dom_fn_name}\(([^)]+)\)", stmt)
      if m:
        target, args = m.group(1), [a.strip() for a in m.group(2).split(",")]
        if target not in state:
          state[target] = 0
        state[target] = emulate_dom_fn(
            state[args[0]], state[args[1]], state[args[2]]
        )
        continue

    # Bitwise Inversion: a = ~(b & c)
    inv_match = re.match(
        r"([a-zA-Z0-9_]+)\s*=\s*~\s*\(\s*([a-zA-Z0-9_]+)\s*&\s*([a-zA-Z0-9_]+)\s*\)",
        stmt,
    )
    if inv_match:
      target, left, right = inv_match.groups()
      if target not in state:
        state[target] = 0
      if left not in state:
        state[left] = 0
      if right not in state:
        state[right] = 0

      state[target] = to_int32(~(state[left] & state[right]))
      continue

    # Standard Binary operations: a = b OP c (where OP is &, |, ^)
    bin_match = re.match(
        r"([a-zA-Z0-9_]+)\s*=\s*([a-zA-Z0-9_]+)\s*([&|^])\s*([a-zA-Z0-9_]+)",
        stmt,
    )
    if bin_match:
      target, left, op, right = bin_match.groups()
      if target not in state:
        state[target] = 0
      if left not in state:
        state[left] = 0
      if right not in state:
        state[right] = 0

      l_val, r_val = state[left], state[right]
      if op == "&":
        state[target] = to_int32(l_val & r_val)
      elif op == "|":
        state[target] = to_int32(l_val | r_val)
      elif op == "^":
        state[target] = to_int32(l_val ^ r_val)
      continue

  # 5. Extract Session Nonce (i)
  # 5. Extract Session Nonce (i) — read from the postMessage nonce/result.
  nonce_match = re.search(r'nonce\s*:\s*["\']([a-f0-9]+)["\']', script_js)
  nonce = nonce_match.group(1) if nonce_match else ""

  # Build the state dict using the AUTHORITATIVE key->source-variable map.
  # Emit each server-expected KEY with the value of its SOURCE variable.
  out_state = {}
  for key, source_var in output_map:
    out_state[key] = state[source_var]

  return {"i": nonce, "state": out_state}



def decompress_cap_blob(b64_string: str) -> str:
  """Helper to decompress raw Base64 deflate payloads from Cap challenges."""
  compressed = base64.b64decode(b64_string)
  # -15 enables raw DEFLATE stream decompression without zlib/gzip headers
  return zlib.decompress(compressed, -15).decode("utf-8")

def main():
  global timeouts
  global fails
  global solves
  headers = {
    "accept": "*/*",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en-US,en;q=0.9",
    "content-length": "0",
    "dnt": "1",
    "origin": "https://botme.idk.dunkirk.sh",
    "priority": "u=1, i",
    "referer": "https://botme.idk.dunkirk.sh/",
    "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
  }
  croute = "https://cap.dunkirk.sh/cbe403f57a/challenge"  # replace with your endpoint
  print('getting challenge')
  req = 0
  try:
    req = requests.post(croute, headers=headers, timeout=15)
  except Exception:
    timeouts += 1
    print(f"Timeout ({timeouts} total timeouts)", flush=True)
    return ''
  print(req, flush=True)
  if req.status_code == 502 or req.status_code == 403:
    timeouts += 1
    print(f"Timeout ({timeouts} total timeouts)", flush=True)
    return ''
  data = req.json()
  print(f"[{worker_id}] Performing task iteration...", flush=True)
  rsw_payload = None
  instrumentation_blob = None
  token = data.get("token")

  for challenge in data.get("challenges", []):
    if challenge.get("protocol") == "rsw":
      rsw_payload = challenge.get("payload", {})
    elif challenge.get("protocol") == "instrumentation":
      instrumentation_blob = challenge.get("payload", {}).get("blob")

  # Extract RSW parameters safely
  N = 0
  t = 0
  x = 0
  if rsw_payload:
    N = rsw_payload["N"]
    t = rsw_payload["t"]
    x = rsw_payload["x"]
    print(f"Found RSW Challenge: t={t}")
  else:
    print("RSW challenge protocol not found in response.")
  rsw = solve_rsw_puzzle(str(x), t, str(N))
  script = decompress_cap_blob(instrumentation_blob)
  ins = solve_instrumentation_telemetry(script)
  y_hex = resulthex(rsw)
  redemption_payload = {
      "token": token,
      "solutions": [
          {
              # RSW protocol solution
              "y": y_hex
          },
          {
              # Instrumentation protocol solution
              "instr": {
                  "i": ins["i"],  # Session nonce extracted from the script
                  "state": ins["state"],  # The emulated telemetry numbers dictionary
                  "ts": int(time.time() * 1000),  # Current timestamp in milliseconds
              }
          },
      ],
  }
  # 3. Send the POST request to your redemption/verify endpoint
  redeem_url = "https://cap.dunkirk.sh/cbe403f57a/redeem"  # update as needed

  response = requests.post(redeem_url, json=redemption_payload, headers=headers)
  if response.status_code == 502:
    timeouts += 1
    print(f"Timeout ({timeouts} total timeouts)", flush=True)
    return ''
  if response.status_code == 403:
    fails += 1
    print(f'Fail! (womp womp) ({fails} total fails, {timeouts} total timeouts)', flush=True)
    return ''
  req = requests.post('https://botme.idk.dunkirk.sh/captchas/verify/cap-default?name=awsome-webdev', headers=headers, json={'token': token})
  print(req.json())
  print(f"[{worker_id}] Performing task iteration...", flush=True)
  print(f"Success! Solved {solves} items", flush=True)
  solves +=1
  print("Status Code:", response.status_code)
  print("Response:", response.text)



worker_id = sys.argv[1] if len(sys.argv) > 1 else "standalone"
print(f"[{worker_id}] Starting custom bot script...", flush=True)
global timeouts
global solves
global fails
timeouts = 0
solves = 0
fails = 0
while True:
  main()
