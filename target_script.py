import base64
import re
import zlib
import sys
import requests
import time
import gmpy2

def solve_rsw_puzzle_fast(x, t, n):
  n_mpz = gmpy2.mpz(n, 16)
  v = gmpy2.mpz(x, 16)
  for _ in range(t):
    v = gmpy2.powmod(v, 2, n_mpz)
  return int(v)

def solve_rsw_puzzle(x, t, n):
  """Simulates the client-side RSW puzzle solver using sequential modular squaring.
  :param x: Initial base number (hex string)
  :param t: Number of sequential squarings
  :param n: The modulus N (hex string)
  """
  n = int(n, 16)
  x = int(x, 16)
  print(f"Starting RSW puzzle solver (t = {t} steps)...")
  v = x
  for step in range(1, t + 1):
    # The core sequential squaring step: v = (v^2) mod N
    v = pow(v, 2, n)
  return v


def resulthex(y, n_hex):
  """Return y as hex WITHOUT the 0x prefix, zero-padded to the modulus width.

  Cap validates RSW solutions by comparing the hex string length/bytes against
  the modulus N. Dropping leading zeros causes `invalid_solution`.
  """
  h = format(y, "x")

  # Strip optional 0x/0X prefix safely
  clean = n_hex[2:] if n_hex[:2].lower() == "0x" else n_hex

  # The width of N in hex characters
  width = len(clean)

  # Zero-pad to match N's exact hex character width
  if len(h) < width:
    h = h.zfill(width)

  # Ensure even length if required by byte conversion standards
  if len(h) % 2 != 0:
    h = "0" + h

  return h


def to_int32(val: int) -> int:
  """Clamps Python arbitrary-precision ints to JavaScript's 32-bit signed integer behavior."""
  val = val & 0xFFFFFFFF
  return val - 0x100000000 if val >= 0x80000000 else val


def js_remainder(value: int, divisor: int) -> int:
  """Integer remainder with JavaScript's sign behavior.

  Unlike Python %, the result follows the dividend's sign.
  Negative zero is represented as integer zero.
  """
  if divisor == 0:
    raise ValueError("Remainder divisor must not be zero")
  remainder = abs(value) % abs(divisor)
  return -remainder if value < 0 else remainder


def emulate_dom_fn(x: int, y: int, z: int) -> int:
  """Emulate the eight-step DOM helper found in the supplied fixtures.

  Only even-valued nodes become part of the final ancestor path.
  The first node stores the original input; subsequent nodes store
  values produced by JavaScript's signed 32-bit right shift.
  """
  total = 0
  for v in (x, y, z):
    cur = v
    for _ in range(8):
      if (to_int32(cur) & 1) == 0:
        total += cur
      cur = to_int32(cur) >> 1
  return js_remainder(total, 256)


def emulate_proto_fn(a: int, b: int, c: int) -> int:
  """Pure mathematical emulation of the Cap prototype chaining function.
  (b ^ a) | (c ^ b)
  """
  return to_int32((b ^ a) | (c ^ b))


def solve_instrumentation_telemetry(script_js: str, has_user_agent: bool = True):
  """Interpret the supported telemetry pipeline patterns.

  Helper semantics are based on the supplied fixtures, not arbitrary JS.
  Unknown statements and missing variables raise instead of being ignored.
  Browser-environment checks outside the state pipeline are not emulated.
  """
  # 1. Extract ALL initial integer seeds
  seeds = re.findall(
      r"(?:var|let|const)?\s*([a-zA-Z0-9_$]+)\s*=\s*([0-9]+)\s*;", script_js
  )
  state = {}
  for name, val in seeds:
    state[name] = int(val)

  # 2. Find the Authoritative Output Builder Map:
  #    var OBJ={}; OBJ["key1"]=varA; OBJ["key2"]=varB; ... return OBJ;
  builder_match = re.search(
      r"(?:var|let|const)\s+([a-zA-Z0-9_$]+)\s*=\s*\{\s*\}\s*;"  # OBJ = {}
      r"((?:\s*\1\s*\[\s*[\"'][^\"']+[\"']\s*\]\s*=\s*[a-zA-Z0-9_$]+\s*;)+)"  # OBJ["k"]=v;
      r"\s*return\s+\1\s*;",  # return OBJ;
      script_js,
  )
  if not builder_match:
    raise ValueError(
        'Could not locate the state-builder object (OBJ["key"]=var;).'
    )

  # Map each output KEY -> the SOURCE VARIABLE feeding it
  output_map = re.findall(
      r"\[\s*[\"']([^\"']+)[\"']\s*\]\s*=\s*([a-zA-Z0-9_$]+)\s*;",
      builder_match.group(2),
  )

  # 3. Extract the names of the two dynamic helper functions
  proto_fn_match = re.search(
      r"function\s+([a-zA-Z0-9_]+)\s*\([a-z]\s*,\s*[a-z]\s*,\s*[a-z]\)\s*\{\s*function\s+F",
      script_js,
  )
  dom_fn_match = re.search(
      r"function\s+([a-zA-Z0-9_]+)\s*\([a-z]\s*,\s*[a-z]\s*,\s*[a-z]\)\s*\{\s*var\s+[a-z]\s*=\s*document\.createElement",
      script_js,
  )

  proto_fn_name = proto_fn_match.group(1) if proto_fn_match else None
  dom_fn_name = dom_fn_match.group(1) if dom_fn_match else None

  if proto_fn_name is None or dom_fn_name is None:
    raise ValueError("Could not identify both supported telemetry helpers.")

  # 4. Landmark-based robust execution pipeline slicing
  # Starts at the first statement of the pipeline and ends where the state object is initialized
  start_match = re.search(
      r"\b([a-zA-Z0-9_]+)\s*=\s*\1\s*\^\s*\(navigator\.userAgent", script_js
  )
  # Use the authoritative output builder, not an unrelated empty object.
  end_match = builder_match

  if not start_match or start_match.start() >= end_match.start():
    raise ValueError("Could not locate execution pipeline boundaries.")

  pipeline_str = script_js[start_match.start() : end_match.start()]

  # Safe for the supported arithmetic-only pipeline, which contains no
  # string or regex literals. Do not apply this to arbitrary JavaScript.
  # Remove comments rather than skipping comment-prefixed statements.
  pipeline_str = re.sub(
      r"/\*.*?\*/|//[^\r\n]*",
      " ",
      pipeline_str,
      flags=re.DOTALL,
  )
  raw_statements = [s.strip() for s in pipeline_str.split(";") if s.strip()]

  def read_operand(operand: str) -> int:
    operand = operand.strip()
    if re.fullmatch(r"-?0[xX][0-9a-fA-F]+", operand):
      return int(operand, 16)
    if re.fullmatch(r"-?\d+", operand):
      return int(operand, 10)
    if operand not in state:
      raise ValueError(f"Uninitialized telemetry variable: {operand!r}")
    return state[operand]

  identifier = r"[A-Za-z_$][A-Za-z0-9_$]*"
  helper_call_pattern = re.compile(
      rf"({identifier})\s*=\s*({identifier})\s*\(([^()]*)\)"
  )
  helpers = {
      proto_fn_name: emulate_proto_fn,
      dom_fn_name: emulate_dom_fn,
  }

  # 5. Step through each operation logically
  for stmt in raw_statements:
    # A. Navigator UserAgent check: var = var ^ (navigator.userAgent ? A : B)
    ua_match = re.fullmatch(
        r"([a-zA-Z0-9_]+)\s*=\s*\1\s*\^\s*\(navigator\.userAgent\s*\?\s*(\d+)\s*:\s*(\d+)\)",
        stmt,
    )
    if ua_match:
      var_name = ua_match.group(1)
      val = (
          int(ua_match.group(2)) if has_user_agent else int(ua_match.group(3))
      )
      state[var_name] = to_int32(read_operand(var_name) ^ val)
      continue

    # B. Final masking & modulo: var = ((var ^ salt) & 0x7FFFFFFF) % mod + offset
    final_match = re.fullmatch(
        r"([a-zA-Z0-9_]+)\s*=\s*\(\s*\(\s*\1\s*\^\s*(\d+)\s*\)\s*&\s*(?:0x7FFFFFFF|2147483647)\s*\)\s*%\s*(\d+)\s*\+\s*(\d+)",
        stmt,
    )
    if final_match:
      var_name = final_match.group(1)
      salt = int(final_match.group(2))
      mod = int(final_match.group(3))
      offset = int(final_match.group(4))
      if mod == 0:
        raise ValueError(f"Zero modulus in telemetry statement: {stmt!r}")
      state[var_name] = (
          ((read_operand(var_name) ^ salt) & 0x7FFFFFFF) % mod
      ) + offset
      continue

    # C/D. Supported helper calls, including whitespace before "(".
    call_match = helper_call_pattern.fullmatch(stmt)
    if call_match:
      target, function_name, arguments = call_match.groups()
      if function_name not in helpers:
        raise ValueError(f"Unsupported telemetry helper: {function_name!r}")
      args = [argument.strip() for argument in arguments.split(",")]
      if len(args) != 3 or not all(args):
        raise ValueError(f"Expected three helper arguments: {stmt!r}")
      state[target] = helpers[function_name](
          *(read_operand(argument) for argument in args)
      )
      continue

    # E. Bitwise Inversion: a = ~(b & c)
    inv_match = re.fullmatch(
        r"([a-zA-Z0-9_]+)\s*=\s*~\s*\(\s*([a-zA-Z0-9_]+)\s*&\s*([a-zA-Z0-9_]+)\s*\)",
        stmt,
    )
    if inv_match:
      target, left, right = inv_match.groups()
      state[target] = to_int32(~(read_operand(left) & read_operand(right)))
      continue

    # F. Standard Binary operations: a = b OP c (where OP is &, |, ^)
    bin_match = re.fullmatch(
        r"([a-zA-Z0-9_]+)\s*=\s*([a-zA-Z0-9_]+)\s*([&|^])\s*([a-zA-Z0-9_]+)",
        stmt,
    )
    if bin_match:
      target, left, op, right = bin_match.groups()
      l_val, r_val = read_operand(left), read_operand(right)
      if op == "&":
        state[target] = to_int32(l_val & r_val)
      elif op == "|":
        state[target] = to_int32(l_val | r_val)
      elif op == "^":
        state[target] = to_int32(l_val ^ r_val)
      continue

    raise ValueError(f"Unsupported telemetry statement: {stmt!r}")

  # 6. Extract Session Nonce (i) from postMessage payload
  nonce_match = re.search(r'nonce\s*:\s*["\']([a-f0-9]+)["\']', script_js)
  nonce = nonce_match.group(1) if nonce_match else ""

  # 7. Build final state dictionary matching authoritative output keys
  out_state = {}
  for key, source_var in output_map:
    out_state[key] = read_operand(source_var)

  return {"i": nonce, "state": out_state}


def decompress_cap_blob(b64_string: str, *, prefixed: bool = False) -> str:
  """Decode a Base64 raw-DEFLATE payload, allowing missing padding.

  Set prefixed=True only if the payload format specifies a leading "00".
  Those characters can also legitimately begin ordinary Base64.
  """
  if not isinstance(b64_string, str) or not b64_string.strip():
    raise ValueError("Missing or empty instrumentation blob")

  encoded = re.sub(r"\s+", "", b64_string)
  if prefixed:
    if not encoded.startswith("00"):
      raise ValueError("Expected instrumentation blob prefix '00'")
    encoded = encoded[2:]

  encoded += "=" * (-len(encoded) % 4)
  compressed = base64.b64decode(encoded, validate=True)
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
      "sec-ch-ua": (
          '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"'
      ),
      "sec-ch-ua-mobile": "?0",
      "sec-ch-ua-platform": '"Windows"',
      "sec-fetch-dest": "empty",
      "sec-fetch-mode": "cors",
      "sec-fetch-site": "same-site",
      "user-agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/152.0.0.0 Safari/537.36"
      ),
  }
  croute = "https://cap.dunkirk.sh/cbe403f57a/challenge"
  print("getting challenge")
  try:
    req = requests.post(croute, headers=headers, timeout=15)
  except Exception:
    timeouts += 1
    print(f"Timeout ({timeouts} total timeouts)", flush=True)
    return ""

  if req.status_code in (502, 403):
    timeouts += 1
    print(f"Timeout ({timeouts} total timeouts)", flush=True)
    return ""

  try:
    data = req.json()
  except Exception:
    timeouts += 1
    print(f"Timeout ({timeouts} total timeouts)", flush=True)
    return ""

  print(f"[{worker_id}] Performing task iteration...", flush=True)
  rsw_payload = None
  instrumentation_blob = None
  token = data.get("token")

  for challenge in data.get("challenges", []):
    if challenge.get("protocol") == "rsw":
      rsw_payload = challenge.get("payload", {})
    elif challenge.get("protocol") == "instrumentation":
      instrumentation_blob = challenge.get("payload", {}).get("blob")

  # Extract and solve RSW parameters
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

  rsw = solve_rsw_puzzle_fast(str(x), t, str(N))
  y_hex = resulthex(rsw, N)

  # Decompress and dynamically solve instrumentation telemetry
  script = decompress_cap_blob(instrumentation_blob)
  ins = solve_instrumentation_telemetry(script)

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
                  "i": ins["i"],  # Dynamic session nonce extracted from script
                  "state": (
                      ins["state"]
                  ),  # Dynamically emulated telemetry state dictionary
                  "ts": int(
                      time.time() * 1000
                  ),  # Current timestamp in milliseconds
              }
          },
      ],
  }

  redeem_url = "https://cap.dunkirk.sh/cbe403f57a/redeem"
  response = requests.post(
      redeem_url, json=redemption_payload, headers=headers
  )
  print("Status Code:", response.status_code)
  print("Response:", response.text)
  token2 = response.json()['token']
  if response.status_code == 502:
    timeouts += 1
    print(f"Timeout ({timeouts} total timeouts)", flush=True)
    return ""
  if response.status_code == 403:
    fails += 1
    print(f"Fail! ({fails} total fails, {timeouts} total timeouts)", flush=True)
    return ""

  # token2 = response.json()['token']
  verify_headers = {
      "accept": "*/*",
      "accept-encoding": "gzip, deflate, br, zstd",
      "accept-language": "en-US,en;q=0.9",
      "cache-control": "no-cache",
      "content-length": "70",
      "content-type": "application/json",
      "dnt": "1",
      "origin": "https://botme.idk.dunkirk.sh",
      "pragma": "no-cache",
      "priority": "u=1, i",
      "referer": (
          "https://botme.idk.dunkirk.sh/captchas/cap-default?name=awsome-webdev"
      ),
      "sec-ch-ua": (
          '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"'
      ),
      "sec-ch-ua-mobile": "?0",
      "sec-ch-ua-platform": '"Windows"',
      "sec-fetch-dest": "empty",
      "sec-fetch-mode": "cors",
      "sec-fetch-site": "same-origin",
      "user-agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/152.0.0.0 Safari/537.36"
      ),
  }

  req = requests.post('https://botme.idk.dunkirk.sh/captchas/verify/cap-default?name=awsome-webdev', headers=verify_headers, json={'token': token2})
  print(req.json(), flush=True)
  solves += 1
  print(f"Success! Solved {solves} items", flush=True)


if __name__ == "__main__":
  worker_id = sys.argv[1] if len(sys.argv) > 1 else "standalone"
  print(f"[{worker_id}] Starting custom bot script...", flush=True)
  timeouts = 0
  solves = 0
  fails = 0
  while True:
    main()
