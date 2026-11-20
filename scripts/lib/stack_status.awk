# scripts/status.sh's JSON without Python, for a host with neither the pinned
# Hermes image nor python3 >= 3.9.
#
#   docker compose config --format json | awk -f scripts/lib/stack_status.awk
#
# stdin: the compose config document only. Every service's state is "unknown"
# and its ports come from the config, as stack_status.py prints them with
# --engine-down. The other fields come from the environment:
# STATUS_VERSION, STATUS_HERMES_AGENT, STATUS_HERMES_WEBUI, STATUS_OS,
# STATUS_ARCH and STATUS_ENGINE ("true" or "false").
#
# It walks the JSON with a small tokenizer and keeps only the top-level name,
# service names and services.*.ports[] fields, so it reads no secrets. Strings
# are re-emitted with their original escapes, which keeps them valid JSON.
# POSIX awk; run with LC_ALL=C so names sort like Python's sorted().

function jstr(s) {
  gsub(/\\/, "\\\\", s)
  gsub(/"/, "\\\"", s)
  return "\"" s "\""
}

function in_port() {
  return d == 5 && key[1] == "services" && ctype[2] == "o" && key[3] == "ports" && ctype[4] == "a"
}

function scalar(v, isstr) {
  if (d == 1 && key[1] == "name" && isstr) project = v
  if (in_port() && ctype[5] == "o") p[key[5]] = v
}

function open_c(c) {
  d++
  ctype[d] = c
  key[d] = ""
  expect_key[d] = (c == "o")
  if (d == 3 && key[1] == "services" && ctype[2] == "o") {
    svc = key[2]
    nsvc++
    names[nsvc] = svc
    ports[svc] = ""
  }
  if (in_port() && c == "o") {
    p["host_ip"] = ""
    p["published"] = ""
    p["target"] = ""
    p["protocol"] = ""
  }
}

function close_c() {
  if (in_port() && ctype[5] == "o") add_port()
  d--
}

# Same rules as stack_status.py's _port: whole-number ports, host port > 0.
function add_port(   host, ctr, ip, proto, entry) {
  host = p["published"]
  ctr = p["target"]
  if (host !~ /^[0-9]+$/ || ctr !~ /^[0-9]+$/ || host + 0 <= 0) return
  ip = p["host_ip"] == "" ? "0.0.0.0" : p["host_ip"]
  proto = p["protocol"] == "" ? "tcp" : p["protocol"]
  entry = "{\"host_ip\":\"" ip "\",\"host\":" (host + 0) ",\"container\":" (ctr + 0) \
    ",\"protocol\":\"" proto "\"}"
  ports[svc] = ports[svc] == "" ? entry : ports[svc] "," entry
}

{ s = s $0 "\n" }

END {
  n = length(s)
  i = 1
  while (i <= n) {
    ch = substr(s, i, 1)
    if (ch == "{" || ch == "[") {
      open_c(ch == "{" ? "o" : "a")
      i++
    } else if (ch == "}" || ch == "]") {
      close_c()
      i++
    } else if (ch == ",") {
      if (ctype[d] == "o") expect_key[d] = 1
      i++
    } else if (ch == ":") {
      expect_key[d] = 0
      i++
    } else if (ch == "\"") {
      j = i + 1
      while (j <= n) {
        c2 = substr(s, j, 1)
        if (c2 == "\\") j += 2
        else if (c2 == "\"") break
        else j++
      }
      v = substr(s, i + 1, j - i - 1)
      i = j + 1
      if (ctype[d] == "o" && expect_key[d]) key[d] = v
      else scalar(v, 1)
    } else if (ch ~ /[-0-9A-Za-z.+]/) {
      j = i
      while (j <= n && substr(s, j, 1) ~ /[-0-9A-Za-z.+]/) j++
      v = substr(s, i, j - i)
      i = j
      scalar(v == "null" ? "" : v, 0)
    } else {
      i++
    }
  }
  if (d != 0) {
    print "error: docker compose config did not return JSON" > "/dev/stderr"
    exit 1
  }

  # Insertion sort of the service names (byte order under LC_ALL=C).
  for (a = 2; a <= nsvc; a++) {
    t = names[a]
    for (b = a - 1; b >= 1 && (names[b] "") > (t ""); b--) names[b + 1] = names[b]
    names[b + 1] = t
  }

  out = "{\"version\":" jstr(ENVIRON["STATUS_VERSION"]) \
    ",\"images\":{\"hermes_agent\":" jstr(ENVIRON["STATUS_HERMES_AGENT"]) \
    ",\"hermes_webui\":" jstr(ENVIRON["STATUS_HERMES_WEBUI"]) "}" \
    ",\"project\":\"" project "\"" \
    ",\"host\":{\"os\":" jstr(ENVIRON["STATUS_OS"]) ",\"arch\":" jstr(ENVIRON["STATUS_ARCH"]) "}" \
    ",\"engine\":" (ENVIRON["STATUS_ENGINE"] == "true" ? "true" : "false") \
    ",\"services\":["
  for (a = 1; a <= nsvc; a++) {
    out = out (a > 1 ? "," : "") "{\"name\":\"" names[a] "\",\"state\":\"unknown\",\"ports\":[" \
      ports[names[a]] "]}"
  }
  print out "]}"
}
